from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def make_event(external_id: str, when: datetime, magnitude: float, *, latitude: float = 30.1, longitude: float = 103.2, source: str = "catalog"):
    return {
        "external_id": external_id,
        "origin_time": when.isoformat(),
        "latitude": latitude,
        "longitude": longitude,
        "depth_km": 12.0,
        "magnitude": magnitude,
        "magnitude_type": "ML",
        "source": source,
    }


BASE = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def create(client, external_id, when, magnitude, **kwargs):
    response = client.post("/api/seismic/events", json=make_event(external_id, when, magnitude, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def test_events_associate_into_sequence_with_explainable_basis(client):
    main = create(client, "EQ-AF-001", BASE, 5.8)
    after = create(client, "EQ-AF-002", BASE + timedelta(minutes=5), 4.1)
    far = create(client, "EQ-AF-003", BASE + timedelta(hours=2), 4.3, latitude=35.0, longitude=110.0)

    main_membership = main["sequence_membership"]
    assert main_membership["role"] == "mainshock"
    assert main_membership["association"]["rule"] == "standalone"
    assert main_membership["stats_version"] == 1

    after_membership = after["sequence_membership"]
    assert after_membership["sequence_id"] == main_membership["sequence_id"]
    assert after_membership["role"] == "aftershock"
    association = after_membership["association"]
    assert association["rule"] == "aftershock"
    assert association["reference_event_id"] == main["id"]
    assert association["delta_seconds"] == 300
    assert association["distance_km"] == 0.0
    assert association["magnitude_diff"] == pytest.approx(-1.7)
    assert association["thresholds"]["distance_km"] == 100.0
    assert association["checks"] == {"time": True, "distance": True, "magnitude": "below_mainshock"}

    # 查询事件时仍能拿到归属依据和统计版本
    fetched = client.get(f"/api/seismic/events/{after['id']}").json()
    assert fetched["sequence_membership"]["association"]["rule"] == "aftershock"
    assert fetched["sequence_membership"]["stats_version"] == 1

    # 时空不相邻的事件独立成序列
    far_membership = far["sequence_membership"]
    assert far_membership["association"]["rule"] == "standalone"
    assert far_membership["sequence_id"] != main_membership["sequence_id"]


def test_larger_event_opens_new_mainshock_sequence_and_invalidates_suppression(client):
    main = create(client, "EQ-MS-001", BASE, 5.5)
    create(client, "EQ-MS-002", BASE + timedelta(minutes=10), 3.8)
    sequence_id = main["sequence_membership"]["sequence_id"]

    detail = client.get(f"/api/seismic/sequences/{sequence_id}").json()
    assert detail["mainshock_event_id"] == main["id"]
    alerts = client.get(f"/api/seismic/sequences/{sequence_id}/alerts").json()
    assert alerts[0]["decision"] == "fired"  # 主震告警
    assert alerts[1]["decision"] == "suppressed"  # 余震落在抑制窗口
    policy = detail["policy"]
    assert policy["suppress_until"] is not None

    # 更大事件（震级差 >= 0.5）出现：新序列 + 旧抑制窗口失效
    new_main = create(client, "EQ-MS-003", BASE + timedelta(hours=1), 6.3)
    new_membership = new_main["sequence_membership"]
    assert new_membership["role"] == "mainshock"
    assert new_membership["association"]["rule"] == "new_mainshock"
    assert new_membership["association"]["magnitude_diff"] == pytest.approx(0.8)
    new_sequence_id = new_membership["sequence_id"]
    assert new_sequence_id != sequence_id

    old_detail = client.get(f"/api/seismic/sequences/{sequence_id}").json()
    assert old_detail["policy"]["suppress_until"] is None
    assert old_detail["policy"]["invalidated_by_event_id"] == new_main["id"]
    old_alerts = client.get(f"/api/seismic/sequences/{sequence_id}/alerts").json()
    assert old_alerts[-1]["decision"] == "suppression_invalidated"

    new_detail = client.get(f"/api/seismic/sequences/{new_sequence_id}").json()
    assert new_detail["parent_sequence_id"] == sequence_id
    new_alerts = client.get(f"/api/seismic/sequences/{new_sequence_id}/alerts").json()
    assert new_alerts[0]["decision"] == "fired"


def test_manual_split_records_history_and_bumps_version(client):
    main = create(client, "EQ-SP-001", BASE, 5.9)
    a1 = create(client, "EQ-SP-002", BASE + timedelta(minutes=2), 3.5)
    a2 = create(client, "EQ-SP-003", BASE + timedelta(minutes=8), 3.9)
    a3 = create(client, "EQ-SP-004", BASE + timedelta(minutes=20), 4.0)
    sequence_id = main["sequence_membership"]["sequence_id"]

    split = client.post(f"/api/seismic/sequences/{sequence_id}/split", json={
        "event_ids": [a2["id"], a3["id"]], "reason": "台站判定属于相邻断裂", "new_mainshock_event_id": a3["id"],
    })
    assert split.status_code == 201, split.text
    new_sequence = split.json()
    assert new_sequence["parent_sequence_id"] == sequence_id
    moved_ids = {member["event_id"] for member in new_sequence["members"]}
    assert moved_ids == {a2["id"], a3["id"]}
    assert new_sequence["mainshock_event_id"] == a3["id"]
    for member in new_sequence["members"]:
        assert member["basis"] == "manual"
        assert member["association"]["rule"] == "split"

    old = client.get(f"/api/seismic/sequences/{sequence_id}").json()
    assert {member["event_id"] for member in old["members"]} == {main["id"], a1["id"]}
    assert old["stats_version"] == 2

    # 操作历史双侧留痕
    operations = client.get(f"/api/seismic/sequences/{sequence_id}/operations").json()
    actions = [item["action"] for item in operations]
    assert "split" in actions
    split_op = next(item for item in operations if item["action"] == "split")
    assert split_op["actor"] == "operator"
    assert split_op["after"]["new_sequence_id"] == new_sequence["id"]
    new_operations = client.get(f"/api/seismic/sequences/{new_sequence['id']}/operations").json()
    assert any(item["action"] == "split_from" for item in new_operations)

    # 不能拆走主震
    bad = client.post(f"/api/seismic/sequences/{sequence_id}/split", json={"event_ids": [main["id"]]})
    assert bad.status_code == 400


def test_manual_merge_closes_source_sequences(client):
    s1_main = create(client, "EQ-MG-001", BASE, 5.2)
    s1_after = create(client, "EQ-MG-002", BASE + timedelta(minutes=4), 3.1)
    s2_main = create(client, "EQ-MG-003", BASE + timedelta(hours=30), 4.9,
                     latitude=35.0, longitude=110.0)
    target = s1_main["sequence_membership"]["sequence_id"]
    source = s2_main["sequence_membership"]["sequence_id"]
    assert target != source

    merged = client.post(f"/api/seismic/sequences/{target}/merge",
                         json={"source_sequence_ids": [source], "reason": "同一会话序列"})
    assert merged.status_code == 201, merged.text
    body = merged.json()
    assert body["status"] == "active"
    assert body["stats_version"] == 2
    assert {member["event_id"] for member in body["members"]} == {
        s1_main["id"], s1_after["id"], s2_main["id"],
    }

    source_detail = client.get(f"/api/seismic/sequences/{source}")
    assert source_detail.status_code == 200
    assert source_detail.json()["status"] == "merged"
    assert source_detail.json()["policy"]["enabled"] is False

    membership = client.get(f"/api/seismic/events/{s2_main['id']}").json()["sequence_membership"]
    assert membership["sequence_id"] == target
    assert membership["basis"] == "manual"
    assert membership["association"]["rule"] == "merge"

    # 已合并的序列不能再被操作
    again = client.post(f"/api/seismic/sequences/{source}/merge", json={"source_sequence_ids": [target]})
    assert again.status_code == 409


def test_sliding_window_stats_and_cursor_resume(client, tmp_path):
    main = create(client, "EQ-WN-001", BASE, 5.6)
    create(client, "EQ-WN-002", BASE + timedelta(minutes=10), 4.0)
    create(client, "EQ-WN-003", BASE + timedelta(minutes=40), 4.4)
    create(client, "EQ-WN-004", BASE + timedelta(minutes=70), 3.2)
    sequence_id = main["sequence_membership"]["sequence_id"]

    as_of = BASE + timedelta(hours=2)
    advance = client.post(f"/api/seismic/sequences/{sequence_id}/windows/advance",
                          json={"window_seconds": 3600, "slide_seconds": 1800, "as_of": as_of.isoformat()})
    assert advance.status_code == 200, advance.text
    first = advance.json()
    assert len(first["buckets"]) == 3  # 窗口右端 1:00、1:30、2:00
    assert first["cursor"]["window_end"] == (BASE + timedelta(hours=2, minutes=30)).isoformat(timespec="seconds")
    bucket_end = {item["window_end"]: item for item in first["buckets"]}
    at_one = bucket_end[(BASE + timedelta(hours=1)).isoformat(timespec="seconds")]
    assert at_one["event_count"] == 3
    assert at_one["max_magnitude"] == 5.6
    half_past = bucket_end[(BASE + timedelta(hours=1, minutes=30)).isoformat(timespec="seconds")]
    assert half_past["event_count"] == 2
    assert half_past["max_magnitude"] == 4.4
    at_two = bucket_end[(BASE + timedelta(hours=2)).isoformat(timespec="seconds")]
    assert at_two["event_count"] == 1
    assert at_two["max_magnitude"] == 3.2
    trends = [item["trend"] for item in first["buckets"]]
    assert trends == ["steady", "falling", "falling"]

    # 游标持久化：再次推进只产出新桶，不重复计数
    again = client.post(f"/api/seismic/sequences/{sequence_id}/windows/advance",
                        json={"window_seconds": 3600, "slide_seconds": 1800,
                              "as_of": (BASE + timedelta(hours=3)).isoformat()}).json()
    assert len(again["buckets"]) == 2
    assert {item["window_end"] for item in again["buckets"]} == {
        (BASE + timedelta(hours=2, minutes=30)).isoformat(timespec="seconds"),
        (BASE + timedelta(hours=3)).isoformat(timespec="seconds"),
    }
    assert all(item["event_count"] == 0 for item in again["buckets"])

    windows = client.get(f"/api/seismic/sequences/{sequence_id}/windows").json()
    assert windows[0]["stats_version"] == 1
    assert windows[0]["latest"]["event_count"] == 0

    # 模拟服务重启：新服务实例从数据库恢复游标后继续推进，不重放历史
    from app.database import close_connection
    from app.seismic.sequences import SequenceService

    close_connection()
    recovered = SequenceService().recover()
    assert recovered["sequences_active"] >= 1
    assert recovered["cursors_recovered"] >= 1

    resume = client.post(f"/api/seismic/sequences/{sequence_id}/windows/advance",
                         json={"window_seconds": 3600, "slide_seconds": 1800,
                               "as_of": (BASE + timedelta(hours=3, minutes=30)).isoformat()}).json()
    assert [item["window_end"] for item in resume["buckets"]] == [
        (BASE + timedelta(hours=3, minutes=30)).isoformat(timespec="seconds")
    ]


def test_window_stats_rebuilt_after_membership_change(client):
    main = create(client, "EQ-RB-001", BASE, 5.4)
    stray = create(client, "EQ-RB-002", BASE + timedelta(hours=50), 4.0, latitude=20.0, longitude=90.0)
    sequence_id = main["sequence_membership"]["sequence_id"]
    stray_sequence = stray["sequence_membership"]["sequence_id"]

    client.post(f"/api/seismic/sequences/{sequence_id}/windows/advance",
                json={"window_seconds": 3600, "slide_seconds": 1800,
                      "as_of": (BASE + timedelta(hours=2)).isoformat()})

    # 把独立序列并入后，成员版本上升，旧窗口桶应被丢弃重算
    client.post(f"/api/seismic/sequences/{sequence_id}/merge", json={"source_sequence_ids": [stray_sequence]})
    rebuilt = client.post(f"/api/seismic/sequences/{sequence_id}/windows/advance",
                          json={"window_seconds": 3600, "slide_seconds": 1800,
                                "as_of": (BASE + timedelta(hours=52)).isoformat()}).json()
    assert rebuilt["rebuilt"] is True
    assert rebuilt["stats_version"] == 2
    assert all(item["stats_version"] == 2 for item in rebuilt["buckets"])


def test_policy_suppression_window_and_aftershock_toggle(client):
    main = create(client, "EQ-PL-001", BASE, 5.7)
    sequence_id = main["sequence_membership"]["sequence_id"]

    patched = client.patch(f"/api/seismic/sequences/{sequence_id}/policy",
                           json={"suppression_window_seconds": 7200, "alert_aftershocks": True})
    assert patched.status_code == 200, patched.text
    assert patched.json()["suppression_window_seconds"] == 7200

    suppressed = create(client, "EQ-PL-002", BASE + timedelta(minutes=30), 4.2)
    alerts = client.get(f"/api/seismic/sequences/{sequence_id}/alerts").json()
    assert alerts[-1]["decision"] == "suppressed"

    # 抑制窗口外的余震恢复告警
    fired = create(client, "EQ-PL-003", BASE + timedelta(hours=3), 4.6)
    alerts = client.get(f"/api/seismic/sequences/{sequence_id}/alerts").json()
    assert alerts[-1]["decision"] == "fired"

    # 关闭余震告警后全部抑制
    client.patch(f"/api/seismic/sequences/{sequence_id}/policy", json={"alert_aftershocks": False})
    create(client, "EQ-PL-004", BASE + timedelta(hours=5), 4.5)
    alerts = client.get(f"/api/seismic/sequences/{sequence_id}/alerts").json()
    assert alerts[-1]["decision"] == "suppressed"
    assert suppressed["id"] != fired["id"]
