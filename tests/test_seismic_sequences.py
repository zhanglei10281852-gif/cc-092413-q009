from __future__ import annotations


BASE = {
    "latitude": 30.1,
    "longitude": 103.2,
    "depth_km": 12.0,
    "magnitude_type": "ML",
    "source": "test",
}


def make_event(client, external_id: str, origin: str, magnitude: float, lat=30.1, lon=103.2):
    payload = {
        **BASE,
        "external_id": external_id,
        "origin_time": origin,
        "magnitude": magnitude,
        "latitude": lat,
        "longitude": lon,
    }
    response = client.post("/api/seismic/events", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_aftershock_auto_association_basis_and_windows(client):
    main = make_event(client, "EQ-AS-001", "2026-09-24T10:00:00+00:00", 5.8)
    after = make_event(client, "EQ-AS-002", "2026-09-24T11:00:00+00:00", 4.1, lat=30.2, lon=103.2)
    after2 = make_event(client, "EQ-AS-003", "2026-09-24T11:30:00+00:00", 3.8, lat=30.22, lon=103.22)
    assert main["sequence_id"] == after["sequence_id"] == after2["sequence_id"]

    attribution = client.get(f"/api/seismic/events/{after['id']}/attribution").json()
    info = attribution["attribution"]
    assert info["role"] == "aftershock"
    assert info["join_basis"] == "auto"
    basis = info["basis"]
    assert basis["rule"] == "time-distance-magnitude/v1"
    assert basis["anchor_event_id"] == main["id"]
    # 归属依据：实测差值与阈值快照都可解释
    assert basis["distance_km"] <= 15
    assert basis["delta_seconds"] == 3600
    assert basis["thresholds"]["distance_km"] == 50.0
    assert info["sequence_code"].startswith("SEQ-")

    seq = client.get(f"/api/seismic/sequences/{main['sequence_id']}").json()
    assert seq["member_count"] == 3
    assert seq["stats_version"] == 3
    windows = seq["windows"]
    assert windows, "事件到达后应立即物化滑动窗口"
    # 每个窗口都标注统计版本，且与序列版本一致（成员变更后重算）
    assert all(w["stats_version"] == 3 for w in windows)
    # 滑动窗口（1 小时窗、30 分钟滑动）：[11:00,12:00) 含两个余震
    assert any(w["event_count"] == 2 for w in windows)
    assert any(w["max_magnitude"] == 5.8 for w in windows)
    # 相邻窗口频次 1 -> 2 判定为上升
    assert any(w["trend"] == "rising" and w["delta_count"] == 1 for w in windows)
    assert seq["cursor"]["stats_version"] == 3

    ops = client.get(f"/api/seismic/sequences/{main['sequence_id']}/ops").json()["ops"]
    actions = [op["action"] for op in ops]
    assert "auto.sequence_created" in actions
    assert "auto.join" in actions


def test_unrelated_events_form_separate_sequences(client):
    first = make_event(client, "EQ-SEP-001", "2026-09-24T10:00:00+00:00", 5.8)
    # 距离超出阈值
    far = make_event(client, "EQ-SEP-002", "2026-09-24T11:00:00+00:00", 4.0, lat=32.0, lon=103.2)
    # 时间超出阈值
    late = make_event(client, "EQ-SEP-003", "2026-09-28T11:00:00+00:00", 4.0, lat=30.12, lon=103.2)
    assert first["sequence_id"] != far["sequence_id"]
    assert first["sequence_id"] != late["sequence_id"]


def test_alert_suppression_window(client):
    main = make_event(client, "EQ-AL-001", "2026-09-24T10:00:00+00:00", 5.8)
    seq_id = main["sequence_id"]
    policy = client.put(
        f"/api/seismic/sequences/{seq_id}/alert-policy",
        json={"min_magnitude": 4.0, "suppression_seconds": 3600},
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["generation"] == 1

    second = make_event(client, "EQ-AL-002", "2026-09-24T11:30:00+00:00", 4.5, lat=30.11, lon=103.2)
    third = make_event(client, "EQ-AL-003", "2026-09-24T11:35:00+00:00", 4.6, lat=30.12, lon=103.2)
    assert second["sequence_id"] == third["sequence_id"] == seq_id

    alerts = client.get(f"/api/seismic/sequences/{seq_id}/alerts").json()["alerts"]
    statuses = [a["status"] for a in alerts]
    assert statuses == ["fired", "suppressed"]
    # 首条告警按发震时刻开启抑制窗口（11:30 + 1h = 12:30），11:35 的事件落入其中
    assert alerts[0]["suppression_until"] == "2026-09-24T12:30:00+00:00"
    assert alerts[1]["suppression_until"] == alerts[0]["suppression_until"]


def test_new_mainshock_supersedes_policy_and_invalidates_suppression(client):
    main = make_event(client, "EQ-NM-001", "2026-09-24T10:00:00+00:00", 5.8)
    old_seq = main["sequence_id"]
    policy = client.put(
        f"/api/seismic/sequences/{old_seq}/alert-policy",
        json={"min_magnitude": 4.0, "suppression_seconds": 3600},
    ).json()
    assert policy["status"] == "active"

    # 强 0.7 级（>= 0.5 裕度）且在邻域内 -> 新主震另立序列
    bigger = make_event(client, "EQ-NM-002", "2026-09-24T12:00:00+00:00", 6.5, lat=30.15, lon=103.2)
    new_seq = bigger["sequence_id"]
    assert new_seq != old_seq

    new_sequence = client.get(f"/api/seismic/sequences/{new_seq}").json()
    assert new_sequence["mainshock_event_id"] == bigger["id"]
    attribution = client.get(f"/api/seismic/events/{bigger['id']}/attribution").json()["attribution"]
    assert attribution["basis"]["rule"] == "new-mainshock/v1"
    assert attribution["basis"]["predecessor_sequence_id"] == old_seq

    old_detail = client.get(f"/api/seismic/sequences/{old_seq}").json()
    assert old_detail["alert_policy"]["status"] == "superseded"
    assert old_detail["alert_policy"]["suppress_until"] == ""
    assert "新主震" in old_detail["alert_policy"]["supersede_reason"]
    # 新主震序列不会继承旧策略，因此该事件不触发旧世代告警
    alerts = client.get(f"/api/seismic/sequences/{old_seq}/alerts").json()["alerts"]
    assert all(a["event_id"] != bigger["id"] for a in alerts)

    ops = client.get(f"/api/seismic/sequences/{old_seq}/ops").json()["ops"]
    assert any(op["action"] == "alert.policy_superseded" for op in ops)


def test_manual_split_and_merge_keeps_history_and_bumps_version(client):
    main = make_event(client, "EQ-MX-001", "2026-09-24T10:00:00+00:00", 5.8)
    a1 = make_event(client, "EQ-MX-002", "2026-09-24T11:00:00+00:00", 4.2, lat=30.2, lon=103.25)
    a2 = make_event(client, "EQ-MX-003", "2026-09-24T12:00:00+00:00", 4.0, lat=30.25, lon=103.3)
    seq_id = main["sequence_id"]

    # 把两个余震拆分为独立序列，最强者成为新主震
    split = client.post(
        f"/api/seismic/sequences/{seq_id}/split",
        json={"event_ids": [a1["id"], a2["id"]], "actor": "analyst", "reason": "空间簇分离"},
    )
    assert split.status_code == 201, split.text
    new_seq = split.json()
    new_id = new_seq["id"]
    assert new_seq["member_count"] == 2
    assert new_seq["mainshock_event_id"] == a1["id"]
    moved = client.get(f"/api/seismic/events/{a1['id']}/attribution").json()["attribution"]
    assert moved["role"] == "mainshock"
    assert moved["join_basis"] == "manual"
    assert moved["basis"]["source_sequence_id"] == seq_id

    old_detail = client.get(f"/api/seismic/sequences/{seq_id}").json()
    assert old_detail["member_count"] == 1
    # 两次自动归入各推一次版本，拆分再推一次
    assert old_detail["stats_version"] == 4
    assert all(w["stats_version"] == 4 for w in old_detail["windows"])

    # 不能拆主震
    bad = client.post(
        f"/api/seismic/sequences/{seq_id}/split",
        json={"event_ids": [main["id"]]},
    )
    assert bad.status_code == 400

    # 再合并回去
    merged = client.post(
        f"/api/seismic/sequences/{seq_id}/merge",
        json={"source_sequence_ids": [new_id], "actor": "analyst", "reason": "确认为同一序列"},
    )
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["member_count"] == 3
    assert body["stats_version"] == 5
    source = client.get(f"/api/seismic/sequences/{new_id}").json()
    assert source["status"] == "merged"

    ops = client.get(f"/api/seismic/sequences/{seq_id}/ops").json()["ops"]
    actions = [op["action"] for op in ops]
    assert "manual.split" in actions
    assert "manual.merge" in actions
    assert all(op["actor"] == "analyst" for op in ops if op["action"].startswith("manual."))


def test_window_cursor_recovers_after_restart(client, tmp_path):
    from app.database import close_connection
    from app.seismic.sequences import SequenceService, recover_state

    main = make_event(client, "EQ-RC-001", "2026-09-24T10:00:00+00:00", 5.8)
    seq_id = main["sequence_id"]

    # 模拟崩溃：游标丢失且窗口未物化（序列与成员已持久化）
    import sqlite3

    path = next(tmp_path.glob("*.db"))
    direct = sqlite3.connect(path)
    direct.execute("PRAGMA journal_mode=WAL")
    direct.execute("DELETE FROM seismic_window_cursors WHERE sequence_id=?", (seq_id,))
    direct.execute("DELETE FROM seismic_sequence_windows WHERE sequence_id=?", (seq_id,))
    direct.commit()
    direct.close()
    close_connection()

    result = recover_state()
    assert result["sequences"] >= 1
    seq = SequenceService().get_sequence(seq_id)
    assert seq["cursor"] is not None
    assert seq["cursor"]["cursor_end"] >= main["origin_time"]
    assert seq["windows"], "恢复后窗口应被补齐"
