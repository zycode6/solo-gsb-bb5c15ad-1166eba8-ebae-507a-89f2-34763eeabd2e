"""API 集成测试（每个用例使用独立的临时 SQLite 库）。"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    with TestClient(app) as c:
        yield c


def _put_rule(client, device_id="truck-01", **overrides):
    payload = {
        "upper_limit": 8.0,
        "lower_limit": 2.0,
        "tolerance_seconds": 300,
        "max_gap_seconds": 1800,
    }
    payload.update(overrides)
    return client.put(f"/api/v1/devices/{device_id}/rule", json=payload)


def _send_readings(client, device_id, points):
    """points: [(分钟偏移, 温度), ...]，基准时间 2026-01-01T00:00:00Z"""
    readings = [
        {
            "device_id": device_id,
            "timestamp": (BASE + timedelta(minutes=m)).isoformat(),
            "temperature": t,
        }
        for m, t in points
    ]
    return client.post("/api/v1/readings", json={"readings": readings})


class TestRuleApi:
    def test_put_and_get_rule(self, client):
        resp = _put_rule(client, "truck-01")
        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == "truck-01"
        assert body["upper_limit"] == 8.0

        got = client.get("/api/v1/devices/truck-01/rule")
        assert got.status_code == 200
        assert got.json()["tolerance_seconds"] == 300

    def test_rule_validation(self, client):
        resp = _put_rule(client, "bad", upper_limit=1.0, lower_limit=5.0)
        assert resp.status_code == 422

    def test_get_missing_rule_404(self, client):
        assert client.get("/api/v1/devices/ghost/rule").status_code == 404

    def test_list_devices(self, client):
        _put_rule(client, "truck-01")
        _put_rule(client, "truck-02")
        devices = client.get("/api/v1/devices").json()
        ids = {d["device_id"] for d in devices}
        assert ids == {"truck-01", "truck-02"}

    def test_delete_device(self, client):
        _put_rule(client, "truck-01")
        _send_readings(client, "truck-01", [(0, 9.0)])
        assert client.delete("/api/v1/devices/truck-01").status_code == 200
        assert client.get("/api/v1/devices/truck-01/rule").status_code == 404
        assert client.get("/api/v1/devices/truck-01/readings").json() == []


class TestIngestAndEvents:
    def test_full_flow(self, client):
        _put_rule(client, "truck-01")
        resp = _send_readings(client, "truck-01", [
            (0, 5.0),
            (10, 9.0),    # 超限开始
            (20, 10.0),   # 峰值
            (24, 6.0),    # 短暂恢复 4 分钟（< 容忍 5 分钟）
            (28, 9.5),    # 再次超限 → 同一事件
            (40, 5.0),    # 恢复
            (200, 9.0),   # 间隔 160 分钟 > max_gap 30 分钟 → 新事件
            (210, 5.0),
        ])
        assert resp.status_code == 201
        assert resp.json()["inserted"] == 8

        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 2

        ev1 = events[0]
        assert ev1["start_time"].startswith("2026-01-01T00:10")
        assert ev1["end_time"].startswith("2026-01-01T00:40")
        assert ev1["event_type"] == "high"
        assert ev1["peak_temp"] == 10.0
        assert ev1["ongoing"] is False
        # 超限区间：10→20、20→24、28→40，共 10+4+12 = 26 分钟
        assert ev1["violation_seconds"] == 26 * 60

        ev2 = events[1]
        assert ev2["start_time"].startswith("2026-01-01T03:20")
        assert ev2["violation_seconds"] == 10 * 60

    def test_time_range_filter(self, client):
        _put_rule(client, "truck-01")
        _send_readings(client, "truck-01", [
            (0, 9.0),
            (10, 5.0),
            (100, 9.0),
            (110, 5.0),
        ])
        # 只查覆盖第二个事件的区间
        events = client.get(
            "/api/v1/devices/truck-01/events",
            params={"start": "2026-01-01T01:00:00Z", "end": "2026-01-01T02:00:00Z"},
        ).json()
        assert len(events) == 1
        assert events[0]["start_time"].startswith("2026-01-01T01:40")

    def test_events_require_rule(self, client):
        _send_readings(client, "no-rule-device", [(0, 9.0)])
        resp = client.get("/api/v1/devices/no-rule-device/events")
        assert resp.status_code == 404

    def test_duplicate_timestamp_is_idempotent(self, client):
        _put_rule(client, "truck-01")
        _send_readings(client, "truck-01", [(0, 9.0), (10, 5.0)])
        _send_readings(client, "truck-01", [(0, 9.0), (10, 5.0)])  # 重复上报
        readings = client.get("/api/v1/devices/truck-01/readings").json()
        assert len(readings) == 2

    def test_naive_timestamp_treated_as_utc(self, client):
        _put_rule(client, "truck-01")
        resp = client.post("/api/v1/readings", json={"readings": [
            {"device_id": "truck-01", "timestamp": "2026-01-01T00:00:00", "temperature": 9.0},
            {"device_id": "truck-01", "timestamp": "2026-01-01T00:10:00+00:00", "temperature": 5.0},
        ]})
        assert resp.status_code == 201
        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 1

    def test_all_devices_events(self, client):
        _put_rule(client, "truck-01")
        _put_rule(client, "truck-02")
        _send_readings(client, "truck-01", [(0, 9.0), (10, 5.0)])
        _send_readings(client, "truck-02", [(0, 1.0), (10, 5.0)])
        events = client.get("/api/v1/events").json()
        assert {e["device_id"] for e in events} == {"truck-01", "truck-02"}
        types = {e["device_id"]: e["event_type"] for e in events}
        assert types["truck-01"] == "high"
        assert types["truck-02"] == "low"
