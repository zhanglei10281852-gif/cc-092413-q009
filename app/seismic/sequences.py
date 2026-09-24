"""余震序列关联、滑动窗口统计与告警抑制领域。

设计要点：
- 新事件入库后在同一事务内执行 ``associate_event``，按时间差、空间距离
  （haversine）和震级差与既有活动序列的锚点事件匹配，命中即记为余震，
  明显更强者判定为新主震，全部未命中则自立序列。每次归属都落库可解释
  的 ``basis_json``（实测差值 + 阈值快照 + 规则版本）。
- 序列成员、操作历史、窗口物化行、窗口游标、告警策略与告警记录全部持久化，
  服务无状态，重启后通过 ``recover_state`` 从游标处继续滚动窗口。
- 成员变化（自动归入 / 拆分 / 合并 / 新主震）会推高 ``stats_version`` 并
  作废既有窗口行、把游标重置到序列起始时刻，保证查询所见统计版本一致。
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import from_storage, to_storage, utc_now
from app.database import get_connection, transaction

# ---------------------------------------------------------------------------
# 关联与统计的默认参数（序列创建时快照到行内，保证历史归属可复现解释）
# ---------------------------------------------------------------------------
DEFAULT_TIME_WINDOW_SECONDS = 3 * 24 * 3600
DEFAULT_DISTANCE_KM = 50.0
DEFAULT_MAGNITUDE_GAP = 2.0
DEFAULT_NEW_MAINSHOCK_MARGIN = 0.5
DEFAULT_WINDOW_SECONDS = 3600
DEFAULT_SLIDE_SECONDS = 1800

ASSOCIATION_RULE_VERSION = "time-distance-magnitude/v1"

SEQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_sequences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    mainshock_event_id INTEGER REFERENCES seismic_events(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','merged','closed')),
    time_window_seconds INTEGER NOT NULL,
    distance_km REAL NOT NULL,
    max_magnitude_gap REAL NOT NULL,
    new_mainshock_margin REAL NOT NULL,
    window_seconds INTEGER NOT NULL,
    slide_seconds INTEGER NOT NULL,
    stats_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_sequence_events (
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id) ON DELETE RESTRICT,
    event_id INTEGER NOT NULL PRIMARY KEY REFERENCES seismic_events(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK(role IN ('mainshock','aftershock')),
    join_basis TEXT NOT NULL CHECK(join_basis IN ('auto','manual')),
    anchor_event_id INTEGER REFERENCES seismic_events(id),
    basis_json TEXT NOT NULL DEFAULT '{}',
    joined_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_sequence_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_sequence_windows (
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id) ON DELETE RESTRICT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    max_magnitude REAL,
    trend TEXT NOT NULL CHECK(trend IN ('rising','falling','steady')),
    delta_count INTEGER NOT NULL DEFAULT 0,
    stats_version INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY(sequence_id, window_start)
);
CREATE TABLE IF NOT EXISTS seismic_window_cursors (
    sequence_id INTEGER PRIMARY KEY REFERENCES seismic_sequences(id) ON DELETE RESTRICT,
    window_seconds INTEGER NOT NULL,
    slide_seconds INTEGER NOT NULL,
    cursor_end TEXT NOT NULL,
    stats_version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_alert_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL REFERENCES seismic_sequences(id) ON DELETE RESTRICT,
    min_magnitude REAL NOT NULL,
    suppression_seconds INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    generation INTEGER NOT NULL DEFAULT 1,
    suppress_until TEXT NOT NULL DEFAULT '',
    superseded_at TEXT NOT NULL DEFAULT '',
    supersede_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    policy_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('fired','suppressed')),
    reason TEXT NOT NULL DEFAULT '',
    suppression_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seq_events_seq ON seismic_sequence_events(sequence_id);
CREATE INDEX IF NOT EXISTS idx_seq_ops_seq ON seismic_sequence_ops(sequence_id, id);
CREATE INDEX IF NOT EXISTS idx_alerts_seq ON seismic_alerts(sequence_id, id);
"""


def _now() -> str:
    return to_storage(utc_now())


def ensure_sequence_schema() -> None:
    get_connection().executescript(SEQ_SCHEMA)


def recover_state() -> dict[str, int]:
    """启动恢复：补齐缺失游标，并把所有活动序列的窗口滚动到最新事件。"""
    ensure_sequence_schema()
    rolled = 0
    with transaction(immediate=True) as connection:
        rows = connection.execute("SELECT id FROM seismic_sequences WHERE status='active'").fetchall()
        for row in rows:
            if _roll_windows(connection, row["id"], None):
                rolled += 1
    return {"sequences": len(rows), "caught_up": rolled}


# ---------------------------------------------------------------------------
# 地理与时间工具
# ---------------------------------------------------------------------------
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return round(2 * radius * math.asin(math.sqrt(a)), 3)


def _thresholds(seq: sqlite3.Row) -> dict[str, Any]:
    return {
        "time_window_seconds": seq["time_window_seconds"],
        "distance_km": seq["distance_km"],
        "max_magnitude_gap": seq["max_magnitude_gap"],
        "new_mainshock_margin": seq["new_mainshock_margin"],
    }


def _op(connection: sqlite3.Connection, sequence_id: int | None, action: str, actor: str, detail: dict[str, Any], now: str) -> None:
    connection.execute(
        "INSERT INTO seismic_sequence_ops(sequence_id,action,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
        (sequence_id, action, actor, json.dumps(detail, ensure_ascii=False), now),
    )


def _members(connection: sqlite3.Connection, sequence_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT e.*, se.role AS seq_role, se.join_basis, se.anchor_event_id,
               se.basis_json, se.joined_version
          FROM seismic_sequence_events se
          JOIN seismic_events e ON e.id = se.event_id
         WHERE se.sequence_id = ?
         ORDER BY e.origin_time, e.id
        """,
        (sequence_id,),
    ).fetchall()


def _mainshock(connection: sqlite3.Connection, sequence_id: int) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT e.* FROM seismic_sequence_events se
          JOIN seismic_events e ON e.id = se.event_id
         WHERE se.sequence_id = ? AND se.role = 'mainshock'
        """,
        (sequence_id,),
    ).fetchone()
    if row is None:
        raise KeyError("sequence_without_mainshock")
    return row


def _create_sequence(
    connection: sqlite3.Connection,
    event: sqlite3.Row,
    *,
    role: str,
    join_basis: str,
    basis: dict[str, Any],
    anchor_event_id: int | None,
    now: str,
    action: str,
    actor: str,
    thresholds: dict[str, Any] | None = None,
) -> int:
    thresholds = thresholds or {
        "time_window_seconds": DEFAULT_TIME_WINDOW_SECONDS,
        "distance_km": DEFAULT_DISTANCE_KM,
        "max_magnitude_gap": DEFAULT_MAGNITUDE_GAP,
        "new_mainshock_margin": DEFAULT_NEW_MAINSHOCK_MARGIN,
    }
    cursor = connection.execute(
        """
        INSERT INTO seismic_sequences(code,name,mainshock_event_id,status,
            time_window_seconds,distance_km,max_magnitude_gap,new_mainshock_margin,
            window_seconds,slide_seconds,stats_version,created_at,updated_at)
        VALUES('','',?,'active',?,?,?,?,?,?,1,?,?)
        """,
        (
            event["id"],
            thresholds["time_window_seconds"],
            thresholds["distance_km"],
            thresholds["max_magnitude_gap"],
            thresholds["new_mainshock_margin"],
            DEFAULT_WINDOW_SECONDS,
            DEFAULT_SLIDE_SECONDS,
            now,
            now,
        ),
    )
    sequence_id = cursor.lastrowid
    code = f"SEQ-{sequence_id:06d}"
    connection.execute("UPDATE seismic_sequences SET code=? WHERE id=?", (code, sequence_id))
    connection.execute(
        """
        INSERT INTO seismic_sequence_events(sequence_id,event_id,role,join_basis,
            anchor_event_id,basis_json,joined_version,created_at,updated_at)
        VALUES(?,?,?,?,?,?,1,?,?)
        """,
        (
            sequence_id,
            event["id"],
            role,
            join_basis,
            anchor_event_id,
            json.dumps(basis, ensure_ascii=False),
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO seismic_window_cursors(sequence_id,window_seconds,slide_seconds,
            cursor_end,stats_version,updated_at)
        VALUES(?,?,?,?,1,?)
        """,
        (sequence_id, DEFAULT_WINDOW_SECONDS, DEFAULT_SLIDE_SECONDS, event["origin_time"], now),
    )
    _op(connection, sequence_id, action, actor, {"event_id": event["id"], "code": code, "basis": basis}, now)
    return sequence_id


def _bump_and_invalidate(connection: sqlite3.Connection, sequence_id: int, reset_to: str, now: str) -> int:
    """成员变化后推高统计版本，作废物化窗口并把游标重置到序列起点。"""
    connection.execute(
        "UPDATE seismic_sequences SET stats_version=stats_version+1, updated_at=? WHERE id=?",
        (now, sequence_id),
    )
    connection.execute("DELETE FROM seismic_sequence_windows WHERE sequence_id=?", (sequence_id,))
    version = connection.execute("SELECT stats_version FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()[0]
    window = connection.execute(
        "SELECT window_seconds, slide_seconds FROM seismic_window_cursors WHERE sequence_id=?",
        (sequence_id,),
    ).fetchone()
    if window is None:
        connection.execute(
            """
            INSERT INTO seismic_window_cursors(sequence_id,window_seconds,slide_seconds,
                cursor_end,stats_version,updated_at)
            VALUES(?,?,?,?,?,?)
            """,
            (sequence_id, DEFAULT_WINDOW_SECONDS, DEFAULT_SLIDE_SECONDS, reset_to, version, now),
        )
    else:
        connection.execute(
            "UPDATE seismic_window_cursors SET cursor_end=?, stats_version=?, updated_at=? WHERE sequence_id=?",
            (reset_to, version, now, sequence_id),
        )
    return version


def _supersede_policies(connection: sqlite3.Connection, sequence_ids: list[int], reason: str, now: str) -> None:
    """新主震出现或序列被合并时，旧主震世代的告警策略立即失效（含抑制窗口）。"""
    for sequence_id in sequence_ids:
        cursor = connection.execute(
            """
            UPDATE seismic_alert_policies
               SET status='superseded', superseded_at=?, supersede_reason=?,
                   suppress_until='', updated_at=?
             WHERE sequence_id=? AND status='active'
            """,
            (now, reason, now, sequence_id),
        )
        if cursor.rowcount:
            _op(
                connection,
                sequence_id,
                "alert.policy_superseded",
                "system",
                {"reason": reason},
                now,
            )


def _evaluate_alert(connection: sqlite3.Connection, sequence_id: int, event: sqlite3.Row, now: str) -> None:
    policy = connection.execute(
        "SELECT * FROM seismic_alert_policies WHERE sequence_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (sequence_id,),
    ).fetchone()
    if policy is None or event["magnitude"] < policy["min_magnitude"]:
        return
    # 以发震时刻判定抑制窗口，使历史/补录事件的告警结果确定可复现。
    origin_dt = from_storage(event["origin_time"])
    until = from_storage(policy["suppress_until"])
    if until is not None and origin_dt < until:
        connection.execute(
            """
            INSERT INTO seismic_alerts(sequence_id,event_id,policy_id,status,reason,
                suppression_until,created_at)
            VALUES(?,?,?,'suppressed',?,?,?)
            """,
            (
                sequence_id,
                event["id"],
                policy["id"],
                f"发震时刻处于抑制窗口内（抑制截止 {policy['suppress_until']}），本事件不重复告警",
                policy["suppress_until"],
                now,
            ),
        )
        return
    new_until = to_storage(origin_dt + timedelta(seconds=policy["suppression_seconds"]))
    connection.execute(
        "UPDATE seismic_alert_policies SET suppress_until=?, updated_at=? WHERE id=?",
        (new_until, now, policy["id"]),
    )
    connection.execute(
        """
        INSERT INTO seismic_alerts(sequence_id,event_id,policy_id,status,reason,
            suppression_until,created_at)
        VALUES(?,?,?,'fired',?,?,?)
        """,
        (
            sequence_id,
            event["id"],
            policy["id"],
            f"震级 {event['magnitude']} 达到阈值 {policy['min_magnitude']}，触发告警并开启 {policy['suppression_seconds']} 秒抑制窗口",
            new_until,
            now,
        ),
    )


# ---------------------------------------------------------------------------
# 自动关联（在 create_event 的同一事务内执行）
# ---------------------------------------------------------------------------
def associate_event(connection: sqlite3.Connection, event: sqlite3.Row, now: str) -> int:
    """把新事件归入可解释序列，返回序列 ID。"""
    event_time = from_storage(event["origin_time"])
    sequences = connection.execute("SELECT * FROM seismic_sequences WHERE status='active'").fetchall()

    new_mainshock: tuple | None = None
    aftershock: tuple | None = None
    for seq in sequences:
        members = _members(connection, seq["id"])
        main = next((m for m in members if m["seq_role"] == "mainshock"), None)
        if main is None:
            continue
        best: tuple | None = None
        for member in members:
            member_time = from_storage(member["origin_time"])
            delta_seconds = (event_time - member_time).total_seconds()
            if delta_seconds < 0 or delta_seconds > seq["time_window_seconds"]:
                continue
            distance = haversine_km(
                event["latitude"], event["longitude"], member["latitude"], member["longitude"]
            )
            if distance > seq["distance_km"]:
                continue
            candidate = (distance, delta_seconds, member)
            if best is None or (candidate[0], candidate[1]) < (best[0], best[1]):
                best = candidate
        if best is None:
            continue
        distance, delta_seconds, anchor = best
        magnitude_diff = round(event["magnitude"] - main["magnitude"], 3)
        if event["magnitude"] >= main["magnitude"] + seq["new_mainshock_margin"] - 1e-9:
            if new_mainshock is None or distance < new_mainshock[0]:
                new_mainshock = (distance, delta_seconds, anchor, seq, main, magnitude_diff)
        # 未达到新主震裕度（含略强于主震）且与主震震级差在关联范围内 -> 余震
        elif (main["magnitude"] - event["magnitude"]) <= seq["max_magnitude_gap"] + 1e-9:
            candidate = (distance, delta_seconds, anchor, seq, main, magnitude_diff)
            if aftershock is None or (candidate[0], candidate[1]) < (aftershock[0], aftershock[1]):
                aftershock = candidate

    if new_mainshock is not None:
        return _join_as_new_mainshock(connection, event, new_mainshock, now)
    if aftershock is not None:
        return _join_as_aftershock(connection, event, aftershock, now)
    return _join_as_singleton(connection, event, now)


def _join_as_singleton(connection: sqlite3.Connection, event: sqlite3.Row, now: str) -> int:
    basis = {
        "rule": "singleton/v1",
        "reason": "在时间窗口、空间距离、震级差阈值内未匹配到任何既有活动序列的锚点事件，自立为主震",
        "thresholds": {
            "time_window_seconds": DEFAULT_TIME_WINDOW_SECONDS,
            "distance_km": DEFAULT_DISTANCE_KM,
            "max_magnitude_gap": DEFAULT_MAGNITUDE_GAP,
            "new_mainshock_margin": DEFAULT_NEW_MAINSHOCK_MARGIN,
        },
    }
    sequence_id = _create_sequence(
        connection,
        event,
        role="mainshock",
        join_basis="auto",
        basis=basis,
        anchor_event_id=None,
        now=now,
        action="auto.sequence_created",
        actor="system",
    )
    _roll_windows(connection, sequence_id, event["origin_time"])
    _evaluate_alert(connection, sequence_id, event, now)
    return sequence_id


def _join_as_aftershock(connection: sqlite3.Connection, event: sqlite3.Row, match: tuple, now: str) -> int:
    distance, delta_seconds, anchor, seq, main, magnitude_diff = match
    sequence_id = seq["id"]
    version = connection.execute("SELECT stats_version FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()[0]
    basis = {
        "rule": ASSOCIATION_RULE_VERSION,
        "anchor_event_id": anchor["id"],
        "anchor_external_id": anchor["external_id"],
        "mainshock_event_id": main["id"],
        "delta_seconds": round(delta_seconds, 1),
        "distance_km": distance,
        "magnitude_diff": magnitude_diff,
        "thresholds": _thresholds(seq),
        "matched_checks": [
            f"时间差 {round(delta_seconds, 1)}s ≤ {seq['time_window_seconds']}s",
            f"距离 {distance}km ≤ {seq['distance_km']}km",
            f"震级差(事件-主震) {magnitude_diff}：未达到新主震裕度 {seq['new_mainshock_margin']}，"
            f"且与主震差值 ≤ {seq['max_magnitude_gap']}",
        ],
        "explanation": (
            f"事件晚于最近锚点 {anchor['external_id']} {round(delta_seconds / 3600, 2)} 小时、"
            f"相距 {distance}km，与主震震级差 {magnitude_diff:+.1f} 级（不构成新主震），"
            f"三项关联条件全部满足"
        ),
    }
    connection.execute(
        """
        INSERT INTO seismic_sequence_events(sequence_id,event_id,role,join_basis,
            anchor_event_id,basis_json,joined_version,created_at,updated_at)
        VALUES(?,?, 'aftershock','auto',?,?,?,?,?)
        """,
        (
            sequence_id,
            event["id"],
            anchor["id"],
            json.dumps(basis, ensure_ascii=False),
            version + 1,
            now,
            now,
        ),
    )
    _bump_and_invalidate(connection, sequence_id, main["origin_time"], now)
    _op(
        connection,
        sequence_id,
        "auto.join",
        "system",
        {"event_id": event["id"], "anchor_event_id": anchor["id"], "basis": basis},
        now,
    )
    _roll_windows(connection, sequence_id, event["origin_time"])
    _evaluate_alert(connection, sequence_id, event, now)
    return sequence_id


def _join_as_new_mainshock(connection: sqlite3.Connection, event: sqlite3.Row, match: tuple, now: str) -> int:
    distance, delta_seconds, anchor, seq, main, magnitude_diff = match
    old_sequence_id = seq["id"]
    basis = {
        "rule": "new-mainshock/v1",
        "predecessor_sequence_id": old_sequence_id,
        "predecessor_code": seq["code"],
        "predecessor_mainshock_event_id": main["id"],
        "anchor_event_id": anchor["id"],
        "delta_seconds": round(delta_seconds, 1),
        "distance_km": distance,
        "magnitude_diff": magnitude_diff,
        "thresholds": _thresholds(seq),
        "explanation": (
            f"事件较原主震 {main['external_id']} 高 {magnitude_diff} 级"
            f"（≥ 新主震裕度 {seq['new_mainshock_margin']}），且距离 {distance}km、"
            f"时间差 {round(delta_seconds / 3600, 2)} 小时仍在邻域内，判定为新主震并另立序列；"
            f"原序列 {seq['code']} 的告警策略与抑制窗口自动失效"
        ),
    }
    sequence_id = _create_sequence(
        connection,
        event,
        role="mainshock",
        join_basis="auto",
        basis=basis,
        anchor_event_id=main["id"],
        now=now,
        action="auto.new_mainshock",
        actor="system",
        thresholds=_thresholds(seq),
    )
    _op(
        connection,
        old_sequence_id,
        "auto.new_mainshock_detected",
        "system",
        {"new_event_id": event["id"], "new_sequence_id": sequence_id, "basis": basis},
        now,
    )
    _supersede_policies(
        connection,
        [old_sequence_id],
        f"检测到新主震事件 {event['external_id']}（新序列 SEQ-{sequence_id:06d}），旧主震世代策略失效",
        now,
    )
    _roll_windows(connection, sequence_id, event["origin_time"])
    _evaluate_alert(connection, sequence_id, event, now)
    return sequence_id


# ---------------------------------------------------------------------------
# 滑动窗口统计
# ---------------------------------------------------------------------------
def _roll_windows(connection: sqlite3.Connection, sequence_id: int, as_of: str | None) -> list[dict[str, Any]]:
    seq = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()
    if seq is None:
        raise KeyError("sequence_not_found")
    members = _members(connection, sequence_id)
    if not members:
        return []
    start_origin = min(from_storage(m["origin_time"]) for m in members)
    as_of_dt = from_storage(as_of) if as_of else max(from_storage(m["origin_time"]) for m in members)

    cursor_row = connection.execute(
        "SELECT * FROM seismic_window_cursors WHERE sequence_id=?", (sequence_id,)
    ).fetchone()
    if cursor_row is None:
        # 游标缺失（异常/历史数据）也能恢复：从首个事件重新开始。
        cursor_dt = start_origin
        window_seconds, slide_seconds = DEFAULT_WINDOW_SECONDS, DEFAULT_SLIDE_SECONDS
    else:
        cursor_dt = from_storage(cursor_row["cursor_end"])
        window_seconds, slide_seconds = cursor_row["window_seconds"], cursor_row["slide_seconds"]
    if cursor_dt < start_origin:
        cursor_dt = start_origin

    version = seq["stats_version"]
    produced: list[dict[str, Any]] = []
    now = _now()
    # 物化所有起点不晚于最新事件的窗口：当前进行中的窗口也会落库，
    # 后续成员变化通过 stats_version 作废重算。
    while cursor_dt <= as_of_dt:
        window_start = cursor_dt
        window_end = cursor_dt + timedelta(seconds=window_seconds)
        count = 0
        max_magnitude: float | None = None
        for member in members:
            origin = from_storage(member["origin_time"])
            if window_start <= origin < window_end:
                count += 1
                max_magnitude = member["magnitude"] if max_magnitude is None else max(max_magnitude, member["magnitude"])
        previous = connection.execute(
            """
            SELECT event_count FROM seismic_sequence_windows
             WHERE sequence_id=? AND window_start < ?
             ORDER BY window_start DESC LIMIT 1
            """,
            (sequence_id, to_storage(window_start)),
        ).fetchone()
        previous_count = previous["event_count"] if previous else None
        if previous_count is None:
            trend, delta = "steady", 0
        else:
            delta = count - previous_count
            trend = "rising" if delta > 0 else "falling" if delta < 0 else "steady"
        start_text, end_text = to_storage(window_start), to_storage(window_end)
        connection.execute(
            """
            INSERT OR REPLACE INTO seismic_sequence_windows(sequence_id,window_start,
                window_end,event_count,max_magnitude,trend,delta_count,
                stats_version,computed_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (sequence_id, start_text, end_text, count, max_magnitude, trend, delta, version, now),
        )
        produced.append(
            {
                "window_start": start_text,
                "window_end": end_text,
                "event_count": count,
                "max_magnitude": max_magnitude,
                "trend": trend,
                "delta_count": delta,
                "stats_version": version,
            }
        )
        cursor_dt = window_start + timedelta(seconds=slide_seconds)

    connection.execute(
        """
        INSERT INTO seismic_window_cursors(sequence_id,window_seconds,slide_seconds,
            cursor_end,stats_version,updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(sequence_id) DO UPDATE SET
            cursor_end=excluded.cursor_end,
            stats_version=excluded.stats_version,
            updated_at=excluded.updated_at
        """,
        (sequence_id, window_seconds, slide_seconds, to_storage(cursor_dt), version, now),
    )
    return produced


# ---------------------------------------------------------------------------
# 查询/人工操作服务
# ---------------------------------------------------------------------------
class SequenceService:
    """余震序列查询、人工拆分合并、窗口统计与告警策略服务。"""

    def __init__(self) -> None:
        ensure_sequence_schema()

    # -- 查询 -------------------------------------------------------------
    def list_sequences(self, status: str | None = None) -> list[dict[str, Any]]:
        connection = get_connection()
        sql = (
            """
            SELECT s.*, e.external_id AS mainshock_external_id,
                   (SELECT COUNT(*) FROM seismic_sequence_events se WHERE se.sequence_id=s.id) AS member_count
              FROM seismic_sequences s
              LEFT JOIN seismic_events e ON e.id = s.mainshock_event_id
            """
        )
        rows = connection.execute(sql + (" WHERE s.status=?" if status else "") + " ORDER BY s.id", (status,) if status else ()).fetchall()
        return [dict(row) for row in rows]

    def get_sequence(self, sequence_id: int) -> dict[str, Any] | None:
        connection = get_connection()
        seq = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()
        if seq is None:
            return None
        result = dict(seq)
        members = _members(connection, sequence_id)
        result["member_count"] = len(members)
        result["members"] = [self._attribution_dict(row) for row in members]
        result["windows"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM seismic_sequence_windows WHERE sequence_id=? ORDER BY window_start",
                (sequence_id,),
            ).fetchall()
        ]
        cursor = connection.execute(
            "SELECT * FROM seismic_window_cursors WHERE sequence_id=?", (sequence_id,)
        ).fetchone()
        result["cursor"] = dict(cursor) if cursor else None
        policy = connection.execute(
            "SELECT * FROM seismic_alert_policies WHERE sequence_id=? ORDER BY id DESC LIMIT 1",
            (sequence_id,),
        ).fetchone()
        result["alert_policy"] = dict(policy) if policy else None
        return result

    @staticmethod
    def _attribution_dict(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["basis"] = json.loads(item.pop("basis_json") or "{}")
        except json.JSONDecodeError:
            item["basis"] = {}
        return item

    def get_attribution(self, event_id: int) -> dict[str, Any] | None:
        connection = get_connection()
        event = connection.execute("SELECT * FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            return None
        membership = connection.execute(
            """
            SELECT se.*, s.code AS sequence_code, s.stats_version
              FROM seismic_sequence_events se
              JOIN seismic_sequences s ON s.id = se.sequence_id
             WHERE se.event_id=?
            """,
            (event_id,),
        ).fetchone()
        result: dict[str, Any] = {"event": dict(event)}
        if membership is None:
            result["attribution"] = None
            return result
        attribution = dict(membership)
        try:
            attribution["basis"] = json.loads(attribution.pop("basis_json") or "{}")
        except json.JSONDecodeError:
            attribution["basis"] = {}
        latest_window = connection.execute(
            "SELECT MAX(stats_version) AS v FROM seismic_sequence_windows WHERE sequence_id=?",
            (membership["sequence_id"],),
        ).fetchone()
        attribution["window_stats_version"] = latest_window["v"]
        result["attribution"] = attribution
        return result

    def list_ops(self, sequence_id: int) -> list[dict[str, Any]]:
        connection = get_connection()
        rows = connection.execute(
            "SELECT * FROM seismic_sequence_ops WHERE sequence_id=? ORDER BY id", (sequence_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- 人工拆分 / 合并 --------------------------------------------------
    def split_sequence(
        self, sequence_id: int, event_ids: list[int], actor: str, reason: str
    ) -> dict[str, Any]:
        event_ids = sorted(set(event_ids))
        if not event_ids:
            raise ValueError("event_ids 不能为空")
        now = _now()
        with transaction(immediate=True) as connection:
            source = connection.execute(
                "SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)
            ).fetchone()
            if source is None:
                raise KeyError("sequence_not_found")
            placeholders = ",".join("?" for _ in event_ids)
            moving = connection.execute(
                f"SELECT e.*, se.role FROM seismic_sequence_events se JOIN seismic_events e ON e.id=se.event_id "
                f"WHERE se.sequence_id=? AND se.event_id IN ({placeholders}) ORDER BY e.origin_time,e.id",
                (sequence_id, *event_ids),
            ).fetchall()
            if len(moving) != len(event_ids):
                raise ValueError("存在不属于该序列的事件 ID")
            if any(row["role"] == "mainshock" for row in moving):
                raise ValueError("不能把主震拆分出序列，请仅选择余震")
            winner = min(moving, key=lambda r: (-r["magnitude"], r["origin_time"], r["id"]))
            # 先解除旧归属（event_id 为主键），再在新序列中重建。
            connection.execute(
                f"DELETE FROM seismic_sequence_events WHERE sequence_id=? AND event_id IN ({placeholders})",
                (sequence_id, *event_ids),
            )
            new_id = _create_sequence(
                connection,
                winner,
                role="mainshock",
                join_basis="manual",
                basis={"rule": "manual-split/v1", "source_sequence_id": sequence_id,
                       "source_code": source["code"], "reason": reason or "人工拆分"},
                anchor_event_id=None,
                now=now,
                action="manual.split_created",
                actor=actor,
                thresholds=_thresholds(source),
            )
            for row in moving:
                if row["id"] == winner["id"]:
                    continue
                connection.execute(
                    """
                    INSERT INTO seismic_sequence_events(sequence_id,event_id,role,join_basis,
                        anchor_event_id,basis_json,joined_version,created_at,updated_at)
                    VALUES(?,?,'aftershock','manual',NULL,?,1,?,?)
                    """,
                    (
                        new_id,
                        row["id"],
                        json.dumps(
                            {"rule": "manual-split/v1", "source_sequence_id": sequence_id,
                             "source_code": source["code"], "reason": reason or "人工拆分"},
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
            remaining_origin = _mainshock(connection, sequence_id)["origin_time"]
            _bump_and_invalidate(connection, sequence_id, remaining_origin, now)
            _bump_and_invalidate(connection, new_id, winner["origin_time"], now)
            _roll_windows(connection, sequence_id, None)
            _roll_windows(connection, new_id, None)
            _op(
                connection,
                sequence_id,
                "manual.split",
                actor,
                {"event_ids": event_ids, "new_sequence_id": new_id, "reason": reason},
                now,
            )
            return self.get_sequence(new_id) or {}

    def merge_sequences(
        self, target_id: int, source_ids: list[int], actor: str, reason: str
    ) -> dict[str, Any]:
        source_ids = sorted(set(source_ids))
        if not source_ids:
            raise ValueError("source_sequence_ids 不能为空")
        if target_id in source_ids:
            raise ValueError("目标序列不能同时出现在待合并序列中")
        now = _now()
        with transaction(immediate=True) as connection:
            target = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (target_id,)).fetchone()
            if target is None:
                raise KeyError("sequence_not_found")
            sources = []
            for source_id in source_ids:
                source = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (source_id,)).fetchone()
                if source is None:
                    raise ValueError(f"序列 {source_id} 不存在")
                if source["status"] != "active":
                    raise ValueError(f"序列 {source['code']} 状态为 {source['status']}，不可合并")
                sources.append(source)
            for source in sources:
                members = _members(connection, source["id"])
                for member in members:
                    basis = {
                        "rule": "manual-merge/v1",
                        "source_sequence_id": source["id"],
                        "source_code": source["code"],
                        "target_sequence_id": target_id,
                        "target_code": target["code"],
                        "reason": reason or "人工合并",
                    }
                    connection.execute(
                        """
                        INSERT INTO seismic_sequence_events(sequence_id,event_id,role,join_basis,
                            anchor_event_id,basis_json,joined_version,created_at,updated_at)
                        VALUES(?,?,'aftershock','manual',NULL,?,1,?,?)
                        ON CONFLICT(event_id) DO UPDATE SET
                            sequence_id=excluded.sequence_id, role='aftershock',
                            join_basis='manual', anchor_event_id=NULL,
                            basis_json=excluded.basis_json, updated_at=excluded.updated_at
                        """,
                        (
                            target_id,
                            member["id"],
                            json.dumps(basis, ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
            union = _members(connection, target_id)
            winner = min(union, key=lambda r: (-r["magnitude"], r["origin_time"], r["id"]))
            connection.execute(
                "UPDATE seismic_sequence_events SET role='aftershock', updated_at=? WHERE sequence_id=?",
                (now, target_id),
            )
            connection.execute(
                "UPDATE seismic_sequence_events SET role='mainshock', updated_at=? WHERE sequence_id=? AND event_id=?",
                (now, target_id, winner["id"]),
            )
            connection.execute(
                "UPDATE seismic_sequences SET mainshock_event_id=?, updated_at=? WHERE id=?",
                (winner["id"], now, target_id),
            )
            for source in sources:
                connection.execute(
                    "UPDATE seismic_sequences SET status='merged', updated_at=? WHERE id=?",
                    (now, source["id"]),
                )
                connection.execute("DELETE FROM seismic_sequence_windows WHERE sequence_id=?", (source["id"],))
                connection.execute("DELETE FROM seismic_window_cursors WHERE sequence_id=?", (source["id"],))
                _op(
                    connection,
                    source["id"],
                    "manual.merged_into",
                    actor,
                    {"target_sequence_id": target_id, "target_code": target["code"], "reason": reason},
                    now,
                )
            _supersede_policies(
                connection,
                source_ids,
                f"序列人工合并入 {target['code']}，旧告警策略与抑制窗口失效",
                now,
            )
            reset_to = min(row["origin_time"] for row in union)
            _bump_and_invalidate(connection, target_id, reset_to, now)
            _roll_windows(connection, target_id, None)
            _op(
                connection,
                target_id,
                "manual.merge",
                actor,
                {"source_sequence_ids": source_ids, "mainshock_event_id": winner["id"],
                 "reason": reason},
                now,
            )
            return self.get_sequence(target_id) or {}

    # -- 窗口统计 ---------------------------------------------------------
    def roll_windows(self, sequence_id: int, as_of: str | None = None) -> list[dict[str, Any]]:
        with transaction(immediate=True) as connection:
            return _roll_windows(connection, sequence_id, as_of)

    def roll_all(self, as_of: str | None = None) -> dict[str, int]:
        caught_up = 0
        with transaction(immediate=True) as connection:
            rows = connection.execute("SELECT id FROM seismic_sequences WHERE status='active'").fetchall()
            for row in rows:
                if _roll_windows(connection, row["id"], as_of):
                    caught_up += 1
        return {"sequences": len(rows), "caught_up": caught_up}

    # -- 告警策略 ---------------------------------------------------------
    def upsert_policy(
        self, sequence_id: int, min_magnitude: float, suppression_seconds: int
    ) -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            seq = connection.execute("SELECT * FROM seismic_sequences WHERE id=?", (sequence_id,)).fetchone()
            if seq is None:
                raise KeyError("sequence_not_found")
            if seq["status"] != "active":
                raise ValueError(f"序列 {seq['code']} 状态为 {seq['status']}，不可设置告警策略")
            existing = connection.execute(
                "SELECT * FROM seismic_alert_policies WHERE sequence_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                (sequence_id,),
            ).fetchone()
            if existing is None:
                generation = connection.execute(
                    "SELECT COALESCE(MAX(generation),0)+1 AS g FROM seismic_alert_policies WHERE sequence_id=?",
                    (sequence_id,),
                ).fetchone()["g"]
                cursor = connection.execute(
                    """
                    INSERT INTO seismic_alert_policies(sequence_id,min_magnitude,
                        suppression_seconds,status,generation,created_at,updated_at)
                    VALUES(?,?,?,'active',?,?,?)
                    """,
                    (sequence_id, min_magnitude, suppression_seconds, generation, now, now),
                )
                policy_id = cursor.lastrowid
            else:
                policy_id = existing["id"]
                connection.execute(
                    """
                    UPDATE seismic_alert_policies SET min_magnitude=?, suppression_seconds=?,
                        suppress_until='', updated_at=? WHERE id=?
                    """,
                    (min_magnitude, suppression_seconds, now, policy_id),
                )
            _op(
                connection,
                sequence_id,
                "alert.policy_upserted",
                "operator",
                {"policy_id": policy_id, "min_magnitude": min_magnitude,
                 "suppression_seconds": suppression_seconds},
                now,
            )
            return dict(
                connection.execute(
                    "SELECT * FROM seismic_alert_policies WHERE id=?", (policy_id,)
                ).fetchone()
            )

    def list_alerts(self, sequence_id: int | None = None) -> list[dict[str, Any]]:
        connection = get_connection()
        sql = (
            """
            SELECT a.*, e.external_id, s.code AS sequence_code
              FROM seismic_alerts a
              JOIN seismic_events e ON e.id = a.event_id
              JOIN seismic_sequences s ON s.id = a.sequence_id
            """
        )
        if sequence_id is None:
            rows = connection.execute(sql + " ORDER BY a.id").fetchall()
        else:
            rows = connection.execute(sql + " WHERE a.sequence_id=? ORDER BY a.id", (sequence_id,)).fetchall()
        return [dict(row) for row in rows]
