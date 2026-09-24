"""余震序列关联、滑动窗口统计与告警抑制领域。

设计要点：
- 新事件按时间、距离、震级差与活动序列的主震比较，自动归入已有序列或建立新序列，
  每次归属都会记录可解释的依据（阈值、实测差值、命中规则）。
- 支持人工拆分/合并，序列成员与操作历史完整留痕，成员变动推高 ``stats_version``。
- 每个序列可按多个 (窗口长度, 滑动步长) 做增量窗口统计，窗口游标持久化，
  重启后从游标继续；成员版本变化后游标自动失效并重算。
- 告警策略挂在序列上，主震触发后开启抑制窗口；出现震级更大的新主震时，
  原序列的抑制窗口立即失效。
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now

# 默认关联参数：时间窗 7 天、空间半径 100 km、超过主震 0.5 级视为新主震（Båth 定律量级）
DEFAULT_TIME_WINDOW_SECONDS = 7 * 24 * 3600
DEFAULT_DISTANCE_KM = 100.0
DEFAULT_NEW_MAINSHOCK_MARGIN = 0.5
DEFAULT_SUPPRESSION_SECONDS = 3600

MAX_BUCKETS_PER_ADVANCE = 10000

SEQUENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','merged','closed')),
    mainshock_event_id INTEGER REFERENCES seismic_events(id),
    parent_sequence_id INTEGER REFERENCES seismic_sequences(id),
    succeeded_sequence_id INTEGER REFERENCES seismic_sequences(id),
    merged_into_sequence_id INTEGER REFERENCES seismic_sequences(id),
    params_json TEXT NOT NULL DEFAULT '{}',
    stats_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_seismic_sequences_code ON seismic_sequences(code) WHERE code <> '';
CREATE TABLE IF NOT EXISTS seismic_sequence_members (
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id),
    event_id INTEGER NOT NULL REFERENCES seismic_events(id),
    role TEXT NOT NULL CHECK(role IN ('mainshock','aftershock')),
    basis TEXT NOT NULL CHECK(basis IN ('auto','manual')),
    association_json TEXT NOT NULL DEFAULT '{}',
    joined_by TEXT NOT NULL,
    joined_at_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(event_id)
);
CREATE INDEX IF NOT EXISTS idx_seq_members_seq ON seismic_sequence_members(sequence_id, event_id);
CREATE TABLE IF NOT EXISTS seismic_sequence_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seq_ops_seq ON seismic_sequence_operations(sequence_id, id);
CREATE TABLE IF NOT EXISTS seismic_window_cursors (
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id),
    window_seconds INTEGER NOT NULL,
    slide_seconds INTEGER NOT NULL,
    window_end TEXT NOT NULL,
    last_count INTEGER,
    stats_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(sequence_id, window_seconds, slide_seconds)
);
CREATE TABLE IF NOT EXISTS seismic_window_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id),
    window_seconds INTEGER NOT NULL,
    slide_seconds INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    max_magnitude REAL,
    rate_per_hour REAL NOT NULL,
    trend TEXT NOT NULL CHECK(trend IN ('rising','falling','steady')),
    stats_version INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(sequence_id, window_seconds, slide_seconds, window_end)
);
CREATE INDEX IF NOT EXISTS idx_window_stats_seq ON seismic_window_stats(sequence_id, window_seconds, slide_seconds, window_end);
CREATE TABLE IF NOT EXISTS seismic_alert_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL UNIQUE REFERENCES seismic_sequences(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    alert_aftershocks INTEGER NOT NULL DEFAULT 1 CHECK(alert_aftershocks IN (0,1)),
    suppression_window_seconds INTEGER NOT NULL,
    suppress_until TEXT,
    invalidated_by_event_id INTEGER REFERENCES seismic_events(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id),
    event_id INTEGER REFERENCES seismic_events(id),
    policy_id INTEGER NOT NULL REFERENCES seismic_alert_policies(id),
    decision TEXT NOT NULL CHECK(decision IN ('fired','suppressed','suppression_invalidated')),
    reason TEXT NOT NULL DEFAULT '',
    suppress_until TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_seq ON seismic_alerts(sequence_id, id);
"""


@dataclass(frozen=True)
class AssociationParams:
    time_window_seconds: int = DEFAULT_TIME_WINDOW_SECONDS
    distance_km: float = DEFAULT_DISTANCE_KM
    new_mainshock_margin: float = DEFAULT_NEW_MAINSHOCK_MARGIN
    suppression_window_seconds: int = DEFAULT_SUPPRESSION_SECONDS

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "AssociationParams":
        if not raw:
            return cls()
        return cls(**{key: value for key, value in json.loads(raw).items() if key in cls.__dataclass_fields__})


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return round(radius * 2 * math.asin(math.sqrt(a)), 3)


def _trend(current: int, previous: int | None) -> str:
    if previous is None or current == previous:
        return "steady"
    return "rising" if current > previous else "falling"


def _log_operation(
    connection: sqlite3.Connection,
    sequence_id: int,
    action: str,
    actor: str,
    now: str,
    summary: str = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        "INSERT INTO seismic_sequence_operations(sequence_id,action,actor,summary,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            sequence_id,
            action,
            actor,
            summary,
            json.dumps(before or {}, ensure_ascii=False),
            json.dumps(after or {}, ensure_ascii=False),
            now,
        ),
    )


def _create_sequence(
    connection: sqlite3.Connection,
    name: str,
    mainshock_event_id: int,
    now: str,
    params: AssociationParams,
    *,
    parent_sequence_id: int | None = None,
    succeeded_sequence_id: int | None = None,
) -> sqlite3.Row:
    cursor = connection.execute(
        "INSERT INTO seismic_sequences(name,mainshock_event_id,parent_sequence_id,succeeded_sequence_id,params_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (name, mainshock_event_id, parent_sequence_id, succeeded_sequence_id, params.to_json(), now, now),
    )
    sequence_id = cursor.lastrowid
    code = f"SEQ-{sequence_id:06d}"
    connection.execute("UPDATE seismic_sequences SET code=? WHERE id=?", (code, sequence_id))
    connection.execute(
        "INSERT INTO seismic_alert_policies(sequence_id,suppression_window_seconds,created_at,updated_at) VALUES(?,?,?,?)",
        (sequence_id, params.suppression_window_seconds, now, now),
    )
    return connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()


def _add_member(
    connection: sqlite3.Connection,
    sequence_id: int,
    event_id: int,
    role: str,
    basis: str,
    joined_by: str,
    version: int,
    now: str,
    association: dict[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO seismic_sequence_members(sequence_id,event_id,role,basis,association_json,joined_by,joined_at_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            sequence_id,
            event_id,
            role,
            basis,
            json.dumps(association, ensure_ascii=False),
            joined_by,
            version,
            now,
            now,
        ),
    )


def _bump_version(connection: sqlite3.Connection, sequence_id: int, now: str) -> int:
    connection.execute(
        "UPDATE seismic_sequences SET stats_version=stats_version+1, updated_at=? WHERE id=?",
        (now, sequence_id),
    )
    return connection.execute("SELECT stats_version FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()[0]


def _record_alert(
    connection: sqlite3.Connection,
    sequence_id: int,
    event_id: int | None,
    decision: str,
    reason: str,
    now: str,
    suppress_until: datetime | None = None,
) -> None:
    policy = connection.execute("SELECT * FROM seismic_alert_policies WHERE sequence_id=?", (sequence_id,)).fetchone()
    connection.execute(
        "INSERT INTO seismic_alerts(sequence_id,event_id,policy_id,decision,reason,suppress_until,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            sequence_id,
            event_id,
            policy["id"],
            decision,
            reason,
            to_storage(suppress_until) if suppress_until else None,
            now,
        ),
    )


def _invalidate_parent_suppression(
    connection: sqlite3.Connection,
    parent_sequence_id: int,
    new_sequence_id: int,
    new_event_id: int,
    now: str,
) -> None:
    connection.execute(
        "UPDATE seismic_alert_policies SET suppress_until=NULL, invalidated_by_event_id=?, updated_at=? WHERE sequence_id=?",
        (new_event_id, now, parent_sequence_id),
    )
    _record_alert(
        connection,
        parent_sequence_id,
        new_event_id,
        "suppression_invalidated",
        f"出现新主震事件 #{new_event_id}（序列 SEQ-{new_sequence_id:06d}），原抑制窗口提前失效",
        now,
    )
    _log_operation(
        connection,
        parent_sequence_id,
        "suppression_invalidated",
        "system",
        now,
        f"新主震事件 #{new_event_id} 出现，抑制窗口失效",
        after={"new_sequence_id": new_sequence_id, "new_event_id": new_event_id},
    )


def associate_new_event(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    now: str,
    params: AssociationParams | None = None,
) -> dict[str, Any]:
    """把刚入库的事件归入序列，并驱动告警判定。必须在调用方事务内执行。"""
    params = params or AssociationParams()
    event_time = from_storage(event["origin_time"])
    candidates: list[tuple[sqlite3.Row, sqlite3.Row, float, float]] = []
    active = connection.execute(
        """
        SELECT s.*, e.origin_time AS main_origin_time, e.latitude AS main_latitude,
               e.longitude AS main_longitude, e.magnitude AS main_magnitude,
               e.external_id AS main_external_id
        FROM seismic_sequences s JOIN seismic_events e ON e.id = s.mainshock_event_id
        WHERE s.status='active'
        """
    ).fetchall()
    for sequence in active:
        sequence_params = AssociationParams.from_json(sequence["params_json"])
        main_time = from_storage(sequence["main_origin_time"])
        delta_seconds = (event_time - main_time).total_seconds()
        distance = haversine_km(
            float(event["latitude"]), float(event["longitude"]),
            float(sequence["main_latitude"]), float(sequence["main_longitude"]),
        )
        magnitude_diff = round(float(event["magnitude"]) - float(sequence["main_magnitude"]), 3)
        time_ok = -60 <= delta_seconds <= sequence_params.time_window_seconds
        distance_ok = distance <= sequence_params.distance_km
        if time_ok and distance_ok:
            candidates.append((sequence, sequence_params, distance, magnitude_diff))

    if candidates:
        # 距离最近的活动序列优先；震级更大则按新主震处理
        sequence, sequence_params, distance, magnitude_diff = min(candidates, key=lambda item: item[2])
        main_time = from_storage(sequence["main_origin_time"])
        delta_seconds = int((event_time - main_time).total_seconds())
        threshold_view = {
            "time_window_seconds": sequence_params.time_window_seconds,
            "distance_km": sequence_params.distance_km,
            "new_mainshock_margin": sequence_params.new_mainshock_margin,
        }
        if magnitude_diff >= sequence_params.new_mainshock_margin:
            return _open_new_mainshock_sequence(
                connection, event, now, sequence_params, sequence, distance, delta_seconds,
                magnitude_diff, threshold_view,
            )
        association = {
            "basis": "auto",
            "rule": "aftershock",
            "reference_sequence_id": sequence["id"],
            "reference_sequence_code": sequence["code"],
            "reference_event_id": sequence["mainshock_event_id"],
            "reference_external_id": sequence["main_external_id"],
            "delta_seconds": delta_seconds,
            "distance_km": distance,
            "magnitude_diff": magnitude_diff,
            "thresholds": threshold_view,
            "checks": {"time": True, "distance": True, "magnitude": "below_mainshock"},
        }
        version = sequence["stats_version"]
        _add_member(connection, sequence["id"], event["id"], "aftershock", "auto", "system", version, now, association)
        connection.execute("UPDATE seismic_sequences SET updated_at=? WHERE id=?", (now, sequence["id"]))
        _log_operation(
            connection, sequence["id"], "associate", "system", now,
            f"事件 #{event['id']} 自动关联为余震（相距 {distance} km，震级差 {magnitude_diff}）",
            after={"event_id": event["id"], "association": association},
        )
        _evaluate_alert(connection, sequence["id"], event, "aftershock", now)
        return {"sequence_id": sequence["id"], "sequence_code": sequence["code"], "role": "aftershock",
                "stats_version": version, "association": association}

    return _open_standalone_sequence(connection, event, now, params)


def _open_new_mainshock_sequence(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    now: str,
    params: AssociationParams,
    parent: sqlite3.Row,
    distance: float,
    delta_seconds: int,
    magnitude_diff: float,
    threshold_view: dict[str, Any],
) -> dict[str, Any]:
    sequence = _create_sequence(
        connection,
        f"主震序列 {event['external_id']}",
        event["id"],
        now,
        params,
        parent_sequence_id=parent["id"],
        succeeded_sequence_id=parent["id"],
    )
    association = {
        "basis": "auto",
        "rule": "new_mainshock",
        "reference_sequence_id": parent["id"],
        "reference_sequence_code": parent["code"],
        "reference_event_id": parent["mainshock_event_id"],
        "reference_external_id": parent["main_external_id"],
        "delta_seconds": delta_seconds,
        "distance_km": distance,
        "magnitude_diff": magnitude_diff,
        "thresholds": threshold_view,
        "checks": {"time": True, "distance": True, "magnitude": "exceeds_mainshock_margin"},
    }
    _add_member(connection, sequence["id"], event["id"], "mainshock", "auto", "system", 1, now, association)
    _log_operation(
        connection, sequence["id"], "create", "system", now,
        f"事件 #{event['id']} 震级超过原主震 {magnitude_diff}，开列为新主震序列",
        after={"event_id": event["id"], "parent_sequence_id": parent["id"], "association": association},
    )
    _invalidate_parent_suppression(connection, parent["id"], sequence["id"], event["id"], now)
    _evaluate_alert(connection, sequence["id"], event, "mainshock", now)
    return {"sequence_id": sequence["id"], "sequence_code": sequence["code"], "role": "mainshock",
            "stats_version": 1, "association": association}


def _open_standalone_sequence(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    now: str,
    params: AssociationParams,
) -> dict[str, Any]:
    sequence = _create_sequence(connection, f"独立序列 {event['external_id']}", event["id"], now, params)
    association = {
        "basis": "auto",
        "rule": "standalone",
        "reference_sequence_id": None,
        "delta_seconds": None,
        "distance_km": None,
        "magnitude_diff": None,
        "thresholds": {
            "time_window_seconds": params.time_window_seconds,
            "distance_km": params.distance_km,
            "new_mainshock_margin": params.new_mainshock_margin,
        },
        "checks": {"time": False, "distance": False, "magnitude": "no_candidate"},
    }
    _add_member(connection, sequence["id"], event["id"], "mainshock", "auto", "system", 1, now, association)
    _log_operation(
        connection, sequence["id"], "create", "system", now,
        f"事件 #{event['id']} 未匹配活动序列，建立独立序列",
        after={"event_id": event["id"], "association": association},
    )
    _evaluate_alert(connection, sequence["id"], event, "mainshock", now)
    return {"sequence_id": sequence["id"], "sequence_code": sequence["code"], "role": "mainshock",
            "stats_version": 1, "association": association}


def _evaluate_alert(
    connection: sqlite3.Connection,
    sequence_id: int,
    event: sqlite3.Row,
    role: str,
    now: str,
) -> None:
    policy = connection.execute("SELECT * FROM seismic_alert_policies WHERE sequence_id=?", (sequence_id,)).fetchone()
    event_time = from_storage(event["origin_time"])
    if not policy["enabled"]:
        _record_alert(connection, sequence_id, event["id"], "suppressed", "策略已停用", now)
        return
    if role == "mainshock":
        suppress_until = event_time + timedelta(seconds=int(policy["suppression_window_seconds"]))
        connection.execute(
            "UPDATE seismic_alert_policies SET suppress_until=?, updated_at=? WHERE id=?",
            (to_storage(suppress_until), now, policy["id"]),
        )
        _record_alert(
            connection, sequence_id, event["id"], "fired",
            "主震告警，触发后进入抑制窗口", now, suppress_until,
        )
        return
    suppress_until = from_storage(policy["suppress_until"])
    if not policy["alert_aftershocks"]:
        _record_alert(connection, sequence_id, event["id"], "suppressed", "策略不对余震告警", now)
        return
    if suppress_until is not None and event_time <= suppress_until:
        _record_alert(
            connection, sequence_id, event["id"], "suppressed",
            f"处于主震抑制窗口内（截止 {policy['suppress_until']}）", now, suppress_until,
        )
        return
    _record_alert(connection, sequence_id, event["id"], "fired", "抑制窗口外的余震告警", now)


class SequenceService:
    """序列查询、人工编排、窗口统计与策略维护。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        from app.database import get_connection

        self.connection = connection or get_connection()
        self.connection.executescript(SEQUENCE_SCHEMA)

    # ---- 查询 ----

    def list_sequences(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT * FROM seismic_sequences WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM seismic_sequences ORDER BY id").fetchall()
        return [self._sequence_view(dict(row)) for row in rows]

    def get_sequence(self, sequence_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()
        if row is None:
            return None
        view = self._sequence_view(dict(row))
        members = self.connection.execute(
            """
            SELECT m.*, e.external_id, e.origin_time, e.latitude, e.longitude,
                   e.depth_km, e.magnitude, e.magnitude_type
            FROM seismic_sequence_members m JOIN seismic_events e ON e.id=m.event_id
            WHERE m.sequence_id=? ORDER BY e.origin_time, e.id
            """,
            (sequence_id,),
        ).fetchall()
        view["members"] = [self._member_view(row) for row in members]
        policy = self.connection.execute("SELECT * FROM seismic_alert_policies WHERE sequence_id=?", (sequence_id,)).fetchone()
        view["policy"] = self._policy_view(policy)
        view["windows"] = self._window_view(sequence_id)
        alert_counts = self.connection.execute(
            "SELECT decision, COUNT(*) AS count FROM seismic_alerts WHERE sequence_id=? GROUP BY decision",
            (sequence_id,),
        ).fetchall()
        view["alert_counts"] = {row["decision"]: row["count"] for row in alert_counts}
        return view

    def get_membership(self, event_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT m.*, s.code AS sequence_code, s.name AS sequence_name, s.status AS sequence_status,
                   s.stats_version AS sequence_stats_version
            FROM seismic_sequence_members m JOIN seismic_sequences s ON s.id=m.sequence_id
            WHERE m.event_id=?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        result = {
            "sequence_id": row["sequence_id"],
            "sequence_code": row["sequence_code"],
            "sequence_name": row["sequence_name"],
            "sequence_status": row["sequence_status"],
            "event_id": event_id,
            "role": row["role"],
            "basis": row["basis"],
            "joined_by": row["joined_by"],
            "joined_at_version": row["joined_at_version"],
            "stats_version": row["sequence_stats_version"],
            "association": json.loads(row["association_json"] or "{}"),
        }
        window_rows = self.connection.execute(
            """
            SELECT window_seconds, slide_seconds, window_end, event_count, max_magnitude,
                   rate_per_hour, trend, stats_version AS window_stats_version
            FROM seismic_window_stats
            WHERE id IN (
                SELECT MAX(id) FROM seismic_window_stats WHERE sequence_id=?
                GROUP BY window_seconds, slide_seconds
            )
            ORDER BY window_seconds, slide_seconds
            """,
            (row["sequence_id"],),
        ).fetchall()
        result["latest_window_stats"] = [dict(item) for item in window_rows]
        return result

    def list_operations(self, sequence_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM seismic_sequence_operations WHERE sequence_id=? ORDER BY id",
            (sequence_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "sequence_id": row["sequence_id"],
                "action": row["action"],
                "actor": row["actor"],
                "summary": row["summary"],
                "before": json.loads(row["before_json"] or "{}"),
                "after": json.loads(row["after_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def list_alerts(self, sequence_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM seismic_alerts WHERE sequence_id=? ORDER BY id",
            (sequence_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 人工拆分 / 合并 ----

    def split_sequence(
        self,
        sequence_id: int,
        event_ids: list[int],
        actor: str,
        name: str | None = None,
        reason: str = "",
        new_mainshock_event_id: int | None = None,
    ) -> dict[str, Any]:
        from app.database import transaction

        with transaction(immediate=True) as connection:
            sequence = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()
            if sequence is None:
                raise KeyError("sequence_not_found")
            if sequence["status"] != "active":
                raise KeyError("sequence_not_active")
            if not event_ids:
                raise ValueError("event_ids_required")
            placeholders = ",".join("?" for _ in event_ids)
            members = connection.execute(
                f"SELECT * FROM seismic_sequence_members WHERE sequence_id=? AND event_id IN ({placeholders})",
                (sequence_id, *event_ids),
            ).fetchall()
            found = {row["event_id"]: row for row in members}
            missing = sorted(set(event_ids) - set(found))
            if missing:
                raise KeyError(f"events_not_in_sequence:{missing}")
            if sequence["mainshock_event_id"] in found:
                raise ValueError("mainshock_cannot_move")
            if new_mainshock_event_id is not None and new_mainshock_event_id not in found:
                raise ValueError("new_mainshock_must_be_moved")
            moved_events = connection.execute(
                f"SELECT * FROM seismic_events WHERE id IN ({placeholders}) ORDER BY magnitude DESC, origin_time, id",
                (*event_ids,),
            ).fetchall()
            mainshock = next((item for item in moved_events if item["id"] == new_mainshock_event_id), moved_events[0])
            params = AssociationParams.from_json(sequence["params_json"])
            now = to_storage(utc_now())
            new_sequence = _create_sequence(
                connection, name or f"拆分序列 {mainshock['external_id']}", mainshock["id"], now, params,
                parent_sequence_id=sequence_id,
            )
            before_ids = [row["event_id"] for row in connection.execute(
                "SELECT event_id FROM seismic_sequence_members WHERE sequence_id=? ORDER BY event_id", (sequence_id,)
            ).fetchall()]
            for item in moved_events:
                role = "mainshock" if item["id"] == mainshock["id"] else "aftershock"
                association = {
                    "basis": "manual",
                    "rule": "split",
                    "reference_sequence_id": sequence_id,
                    "reference_sequence_code": sequence["code"],
                    "actor": actor,
                    "reason": reason,
                    "moved_event_ids": sorted(set(event_ids)),
                }
                connection.execute(
                    "UPDATE seismic_sequence_members SET sequence_id=?, role=?, basis='manual', association_json=?, "
                    "joined_by=?, joined_at_version=1, updated_at=? WHERE event_id=?",
                    (new_sequence["id"], role, json.dumps(association, ensure_ascii=False), actor, now, item["id"]),
                )
            new_version = _bump_version(connection, sequence_id, now)
            after_ids = [row["event_id"] for row in connection.execute(
                "SELECT event_id FROM seismic_sequence_members WHERE sequence_id=? ORDER BY event_id", (sequence_id,)
            ).fetchall()]
            split_payload = {
                "new_sequence_id": new_sequence["id"],
                "moved_event_ids": sorted(set(event_ids)),
                "new_mainshock_event_id": mainshock["id"],
                "actor": actor,
                "reason": reason,
            }
            _log_operation(connection, sequence_id, "split", actor, now, reason or "人工拆分序列",
                           {"member_event_ids": before_ids, "stats_version": new_version - 1},
                           {"member_event_ids": after_ids, "stats_version": new_version, **split_payload})
            _log_operation(connection, new_sequence["id"], "split_from", actor, now,
                           f"自序列 {sequence['code']} 拆分", after=split_payload)
            return self.get_sequence(new_sequence["id"]) or {}

    def merge_sequences(
        self,
        target_sequence_id: int,
        source_sequence_ids: list[int],
        actor: str,
        reason: str = "",
    ) -> dict[str, Any]:
        from app.database import transaction

        with transaction(immediate=True) as connection:
            target = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (target_sequence_id,)).fetchone()
            if target is None:
                raise KeyError("sequence_not_found")
            if target["status"] != "active":
                raise KeyError("sequence_not_active")
            if not source_sequence_ids:
                raise ValueError("source_sequences_required")
            if target_sequence_id in source_sequence_ids:
                raise ValueError("cannot_merge_into_self")
            now = to_storage(utc_now())
            moved_all: list[int] = []
            for source_id in source_sequence_ids:
                source = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (source_id,)).fetchone()
                if source is None:
                    raise KeyError(f"sequence_not_found:{source_id}")
                if source["status"] != "active":
                    raise ValueError(f"sequence_not_active:{source_id}")
                members = connection.execute(
                    "SELECT event_id, role FROM seismic_sequence_members WHERE sequence_id=? ORDER BY event_id",
                    (source_id,),
                ).fetchall()
                before = {"member_event_ids": [row["event_id"] for row in members]}
                for row in members:
                    association = {
                        "basis": "manual",
                        "rule": "merge",
                        "reference_sequence_id": target_sequence_id,
                        "reference_sequence_code": target["code"],
                        "from_sequence_id": source_id,
                        "from_sequence_code": source["code"],
                        "actor": actor,
                        "reason": reason,
                    }
                    connection.execute(
                        "UPDATE seismic_sequence_members SET sequence_id=?, role='aftershock', basis='manual', "
                        "association_json=?, joined_by=?, updated_at=? WHERE event_id=?",
                        (target_sequence_id, json.dumps(association, ensure_ascii=False), actor, now, row["event_id"]),
                    )
                connection.execute(
                    "UPDATE seismic_sequences SET status='merged', merged_into_sequence_id=?, updated_at=? WHERE id=?",
                    (target_sequence_id, now, source_id),
                )
                connection.execute(
                    "UPDATE seismic_alert_policies SET enabled=0, updated_at=? WHERE sequence_id=?",
                    (now, source_id),
                )
                moved_all.extend(before["member_event_ids"])
                _log_operation(connection, source_id, "merge_out", actor, now,
                               reason or f"合并入序列 {target['code']}", before,
                               {"target_sequence_id": target_sequence_id, "moved_event_ids": before["member_event_ids"]})
                _log_operation(connection, target_sequence_id, "merge_in", actor, now,
                               f"并入序列 {source['code']}",
                               {"source_sequence_id": source_id, "moved_event_ids": before["member_event_ids"]},
                               None)
            new_version = _bump_version(connection, target_sequence_id, now)
            _log_operation(connection, target_sequence_id, "merge", actor, now, reason or "人工合并序列",
                           after={"source_sequence_ids": source_sequence_ids, "moved_event_ids": moved_all,
                                  "stats_version": new_version})
            return self.get_sequence(target_sequence_id) or {}

    # ---- 策略 ----

    def update_policy(
        self,
        sequence_id: int,
        enabled: bool | None = None,
        suppression_window_seconds: int | None = None,
        alert_aftershocks: bool | None = None,
    ) -> dict[str, Any]:
        from app.database import transaction

        with transaction(immediate=True) as connection:
            policy = connection.execute("SELECT * FROM seismic_alert_policies WHERE sequence_id=?", (sequence_id,)).fetchone()
            if policy is None:
                raise KeyError("sequence_not_found")
            assignments: list[str] = []
            values: list[Any] = []
            if enabled is not None:
                assignments.append("enabled=?")
                values.append(1 if enabled else 0)
            if alert_aftershocks is not None:
                assignments.append("alert_aftershocks=?")
                values.append(1 if alert_aftershocks else 0)
            if suppression_window_seconds is not None:
                assignments.append("suppression_window_seconds=?")
                values.append(int(suppression_window_seconds))
            if not assignments:
                return self._policy_view(policy)
            now = to_storage(utc_now())
            values.extend([now, policy["id"]])
            connection.execute(f"UPDATE seismic_alert_policies SET {', '.join(assignments)}, updated_at=? WHERE id=?", values)
            refreshed = connection.execute("SELECT * FROM seismic_alert_policies WHERE id=?", (policy["id"],)).fetchone()
            after_view = self._policy_view(refreshed)
            _log_operation(connection, sequence_id, "policy_update", "operator", now,
                           "更新告警策略", self._policy_view(policy), after_view)
            return after_view

    # ---- 滑动窗口统计 ----

    def advance_windows(
        self,
        sequence_id: int,
        window_seconds: int,
        slide_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        from app.database import transaction

        if window_seconds <= 0 or slide_seconds <= 0 or slide_seconds > window_seconds:
            raise ValueError("invalid_window_params")
        anchor_now = now or utc_now()
        if anchor_now.tzinfo is None:
            anchor_now = anchor_now.replace(tzinfo=UTC)
        with transaction(immediate=True) as connection:
            sequence = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()
            if sequence is None:
                raise KeyError("sequence_not_found")
            events = connection.execute(
                """
                SELECT e.id, e.origin_time, e.magnitude
                FROM seismic_sequence_members m JOIN seismic_events e ON e.id=m.event_id
                WHERE m.sequence_id=? ORDER BY e.origin_time, e.id
                """,
                (sequence_id,),
            ).fetchall()
            rebuilt = False
            cursor = connection.execute(
                "SELECT * FROM seismic_window_cursors WHERE sequence_id=? AND window_seconds=? AND slide_seconds=?",
                (sequence_id, window_seconds, slide_seconds),
            ).fetchone()
            if cursor is not None and cursor["stats_version"] != sequence["stats_version"]:
                # 成员在旧版本之后被人工改动，废弃旧桶与游标后重算
                connection.execute(
                    "DELETE FROM seismic_window_stats WHERE sequence_id=? AND window_seconds=? AND slide_seconds=?",
                    (sequence_id, window_seconds, slide_seconds),
                )
                connection.execute(
                    "DELETE FROM seismic_window_cursors WHERE sequence_id=? AND window_seconds=? AND slide_seconds=?",
                    (sequence_id, window_seconds, slide_seconds),
                )
                cursor = None
                rebuilt = True
            if not events:
                return {"sequence_id": sequence_id, "window_seconds": window_seconds,
                        "slide_seconds": slide_seconds, "buckets": [], "rebuilt": rebuilt,
                        "cursor": None, "stats_version": sequence["stats_version"]}
            timed_events = [(item, from_storage(item["origin_time"])) for item in events]
            t0 = timed_events[0][1]
            if cursor is not None:
                window_end = from_storage(cursor["window_end"])
                previous_count = cursor["last_count"]
            else:
                window_end = t0 + timedelta(seconds=window_seconds)
                previous_count = None
            produced: list[dict[str, Any]] = []
            scans = 0
            while window_end <= anchor_now and scans < MAX_BUCKETS_PER_ADVANCE:
                window_start = window_end - timedelta(seconds=window_seconds)
                inside = [(item, when) for item, when in timed_events if window_start <= when < window_end]
                count = len(inside)
                max_magnitude = max((float(item["magnitude"]) for item, _ in inside), default=None)
                trend = _trend(count, previous_count)
                now_text = to_storage(utc_now())
                connection.execute(
                    """
                    INSERT INTO seismic_window_stats(sequence_id,window_seconds,slide_seconds,window_start,window_end,
                        event_count,max_magnitude,rate_per_hour,trend,stats_version,computed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sequence_id, window_seconds, slide_seconds,
                        to_storage(window_start), to_storage(window_end),
                        count, max_magnitude, round(count * 3600.0 / window_seconds, 4),
                        trend, sequence["stats_version"], now_text,
                    ),
                )
                produced.append({
                    "window_start": to_storage(window_start),
                    "window_end": to_storage(window_end),
                    "event_count": count,
                    "max_magnitude": max_magnitude,
                    "trend": trend,
                    "stats_version": sequence["stats_version"],
                })
                previous_count = count
                window_end += timedelta(seconds=slide_seconds)
                scans += 1
            cursor_now = to_storage(utc_now())
            connection.execute(
                """
                INSERT INTO seismic_window_cursors(sequence_id,window_seconds,slide_seconds,window_end,last_count,stats_version,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(sequence_id,window_seconds,slide_seconds) DO UPDATE SET
                    window_end=excluded.window_end, last_count=excluded.last_count,
                    stats_version=excluded.stats_version, updated_at=excluded.updated_at
                """,
                (sequence_id, window_seconds, slide_seconds, to_storage(window_end),
                 previous_count, sequence["stats_version"], cursor_now),
            )
            return {
                "sequence_id": sequence_id,
                "window_seconds": window_seconds,
                "slide_seconds": slide_seconds,
                "stats_version": sequence["stats_version"],
                "rebuilt": rebuilt,
                "cursor": {"window_end": to_storage(window_end), "last_count": previous_count,
                           "stats_version": sequence["stats_version"], "updated_at": cursor_now},
                "buckets": produced,
            }

    def get_windows(self, sequence_id: int) -> list[dict[str, Any]]:
        return self._window_view(sequence_id)

    # ---- 重启恢复 ----

    def recover(self) -> dict[str, Any]:
        """服务重启后核对序列与窗口游标：游标落后于统计版本时标记为待重算。"""
        with self.connection:
            sequences = self.connection.execute("SELECT COUNT(*) AS count FROM seismic_sequences").fetchone()["count"]
            active = self.connection.execute("SELECT COUNT(*) AS count FROM seismic_sequences WHERE status='active'").fetchone()["count"]
            cursors = self.connection.execute(
                """
                SELECT c.sequence_id, c.window_seconds, c.slide_seconds, c.window_end, c.stats_version AS cursor_version,
                       s.stats_version AS sequence_version
                FROM seismic_window_cursors c JOIN seismic_sequences s ON s.id=c.sequence_id
                """
            ).fetchall()
            stale = [dict(row) for row in cursors if row["cursor_version"] != row["sequence_version"]]
            return {
                "sequences_total": sequences,
                "sequences_active": active,
                "cursors_recovered": len(cursors),
                "cursors_stale": stale,
            }

    # ---- 视图组装 ----

    @staticmethod
    def _member_view(row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in (
            "event_id", "role", "basis", "joined_by", "joined_at_version", "created_at", "updated_at")}
        result["association"] = json.loads(row["association_json"] or "{}")
        for key in ("external_id", "origin_time", "latitude", "longitude", "depth_km", "magnitude", "magnitude_type"):
            result[key] = row[key]
        return result

    @staticmethod
    def _policy_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "sequence_id": row["sequence_id"],
            "enabled": bool(row["enabled"]),
            "alert_aftershocks": bool(row["alert_aftershocks"]),
            "suppression_window_seconds": row["suppression_window_seconds"],
            "suppress_until": row["suppress_until"],
            "invalidated_by_event_id": row["invalidated_by_event_id"],
            "updated_at": row["updated_at"],
        }

    def _window_view(self, sequence_id: int) -> list[dict[str, Any]]:
        cursors = self.connection.execute(
            "SELECT * FROM seismic_window_cursors WHERE sequence_id=? ORDER BY window_seconds, slide_seconds",
            (sequence_id,),
        ).fetchall()
        views = []
        for cursor in cursors:
            latest = self.connection.execute(
                "SELECT * FROM seismic_window_stats WHERE sequence_id=? AND window_seconds=? AND slide_seconds=? "
                "ORDER BY window_end DESC, id DESC LIMIT 1",
                (sequence_id, cursor["window_seconds"], cursor["slide_seconds"]),
            ).fetchone()
            views.append({
                "window_seconds": cursor["window_seconds"],
                "slide_seconds": cursor["slide_seconds"],
                "stats_version": cursor["stats_version"],
                "cursor": {"window_end": cursor["window_end"], "last_count": cursor["last_count"],
                           "updated_at": cursor["updated_at"]},
                "latest": dict(latest) if latest else None,
            })
        return views

    @staticmethod
    def _sequence_view(row: dict[str, Any]) -> dict[str, Any]:
        row["params"] = json.loads(row.pop("params_json") or "{}")
        return row
