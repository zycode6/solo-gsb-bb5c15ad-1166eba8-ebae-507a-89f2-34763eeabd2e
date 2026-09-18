"""规则版本机制 API 集成测试。"""

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


def _rule_payload(**overrides):
    payload = {
        "upper_limit": 8.0,
        "lower_limit": 2.0,
        "tolerance_seconds": 300,
        "max_gap_seconds": 1800,
    }
    payload.update(overrides)
    return payload


def _iso(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat()


def _put_version(client, device_id="truck-01", *, at=None, **overrides):
    payload = _rule_payload(**overrides)
    if at is not None:
        payload["effective_at"] = at if isinstance(at, str) else at.isoformat()
    return client.put(f"/api/v1/devices/{device_id}/rule", json=payload)


def _send(client, device_id, points):
    readings = [
        {"device_id": device_id, "timestamp": _iso(m), "temperature": t}
        for m, t in points
    ]
    return client.post("/api/v1/readings", json={"readings": readings})


class TestRuleVersioningApi:
    def test_put_without_effective_at_is_immediate_v1(self, client):
        before = datetime.now(timezone.utc)
        resp = _put_version(client, "truck-01")
        after = datetime.now(timezone.utc)
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 1
        effective = datetime.fromisoformat(body["effective_at"])
        assert before <= effective <= after
        assert body["created_at"] == body["updated_at"]

        got = client.get("/api/v1/devices/truck-01/rule").json()
        assert got["version"] == 1

    def test_versions_listed_in_order(self, client):
        _put_version(client, at=BASE)
        _put_version(client, at=BASE + timedelta(hours=6), upper_limit=9.0)
        _put_version(client, at=BASE + timedelta(hours=12), upper_limit=10.0)

        versions = client.get("/api/v1/devices/truck-01/rule/versions").json()
        assert [v["version"] for v in versions] == [1, 2, 3]
        assert [v["upper_limit"] for v in versions] == [8.0, 9.0, 10.0]
        # 单规则接口返回最新版本
        latest = client.get("/api/v1/devices/truck-01/rule").json()
        assert latest["version"] == 3
        assert latest["upper_limit"] == 10.0
        # 设备列表同样返回最新版本
        devices = client.get("/api/v1/devices").json()
        assert devices[0]["version"] == 3

    def test_out_of_order_effective_at_rejected_409(self, client):
        _put_version(client, at=BASE + timedelta(hours=12))
        resp = _put_version(client, at=BASE + timedelta(hours=6), upper_limit=9.0)
        assert resp.status_code == 409
        # 与最新版本生效时间相同（倒序的一种）同样拒绝
        same = _put_version(client, at=BASE + timedelta(hours=12), upper_limit=9.0)
        assert same.status_code == 409
        # 被拒绝的写入没有产生新版本
        versions = client.get("/api/v1/devices/truck-01/rule/versions").json()
        assert [v["version"] for v in versions] == [1]

    def test_immediate_update_before_future_version_rejected(self, client):
        # 最新版本在未来生效；此刻立即生效的更新属于倒序，拒绝
        future = datetime.now(timezone.utc) + timedelta(days=1)
        _put_version(client, at=future, upper_limit=9.0)
        resp = _put_version(client, upper_limit=10.0)
        assert resp.status_code == 409

    def test_versions_of_unknown_device_404(self, client):
        assert client.get("/api/v1/devices/ghost/rule/versions").status_code == 404

    def test_naive_effective_at_treated_as_utc(self, client):
        resp = _put_version(client, at="2026-01-01T00:00:00")
        assert resp.status_code == 200
        effective = datetime.fromisoformat(resp.json()["effective_at"])
        assert effective == BASE

    def test_delete_device_removes_all_versions(self, client):
        _put_version(client, at=BASE)
        _put_version(client, at=BASE + timedelta(hours=6))
        assert client.delete("/api/v1/devices/truck-01").status_code == 200
        assert client.get("/api/v1/devices/truck-01/rule/versions").status_code == 404
        # 删除后可以重新从 v1 开始
        resp = _put_version(client, at=BASE)
        assert resp.status_code == 200
        assert resp.json()["version"] == 1


class TestVersionedEventsApi:
    def test_switch_splits_event_and_result_carries_version(self, client):
        _put_version(client, at=BASE, upper_limit=8.0)
        _put_version(client, at=BASE + timedelta(minutes=30), upper_limit=8.0)
        _send(client, "truck-01", [
            (0, 9.0),    # v1 超限
            (10, 10.0),  # v1 超限
            (30, 9.5),   # v2 生效：新事件
            (40, 5.0),
        ])
        events = client.get("/api/v1/devices/truck-01/events").json()
        assert len(events) == 2
        assert [e["rule_version"] for e in events] == [1, 2]
        assert events[0]["rule_effective_at"].startswith("2026-01-01T00:00")
        assert events[1]["rule_effective_at"].startswith("2026-01-01T00:30")
        # 旧事件在切换点被强制结束，不标记为持续中
        assert events[0]["ongoing"] is False
        assert events[0]["end_time"].startswith("2026-01-01T00:10")
        assert events[1]["start_time"].startswith("2026-01-01T00:30")

    def test_event_query_window_applies_historical_rule(self, client):
        _put_version(client, at=BASE, upper_limit=10.0)  # 早期宽松
        _put_version(client, at=BASE + timedelta(minutes=30), upper_limit=8.0)  # 30 分收紧
        _send(client, "truck-01", [
            (0, 9.0),    # v1：正常
            (10, 9.0),   # v1：正常
            (30, 9.0),   # v2：超限
            (40, 5.0),
        ])
        # 查全量：只有收紧后产生一个 v2 事件
        events = client.get(
            "/api/v1/devices/truck-01/events",
            params={"start": _iso(0), "end": _iso(60)},
        ).json()
        assert len(events) == 1
        assert events[0]["rule_version"] == 2
        assert events[0]["start_time"].startswith("2026-01-01T00:30")

        # 只查早期窗口：宽松规则下无事件
        early = client.get(
            "/api/v1/devices/truck-01/events",
            params={"start": _iso(0), "end": _iso(20)},
        ).json()
        assert early == []

    def test_all_devices_events_carry_version(self, client):
        _put_version(client, "truck-01", at=BASE)
        _put_version(client, "truck-02", at=BASE)
        _send(client, "truck-01", [(0, 9.0), (10, 5.0)])
        _send(client, "truck-02", [(0, 1.0), (10, 5.0)])
        events = client.get("/api/v1/events").json()
        assert len(events) == 2
        assert all(e["rule_version"] == 1 for e in events)


class TestLegacyMigration:
    def test_legacy_single_row_table_migrates_to_v1(self, tmp_path, monkeypatch):
        import sqlite3

        db_path = str(tmp_path / "legacy.db")
        monkeypatch.setattr(database, "DB_PATH", db_path)

        # 手工构造旧版库（单值 device_rules 表）
        created = "2025-12-31T23:59:00+00:00"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE device_rules (
                    device_id TEXT PRIMARY KEY,
                    upper_limit REAL NOT NULL,
                    lower_limit REAL NOT NULL,
                    tolerance_seconds INTEGER NOT NULL DEFAULT 300,
                    max_gap_seconds INTEGER NOT NULL DEFAULT 1800,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO device_rules VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("truck-01", 8.0, 2.0, 300, 1800, created, created),
            )
            conn.commit()

        database.init_db()

        with TestClient(app) as c:
            versions = c.get("/api/v1/devices/truck-01/rule/versions").json()
            assert len(versions) == 1
            assert versions[0]["version"] == 1
            assert datetime.fromisoformat(versions[0]["effective_at"]) == \
                datetime.fromisoformat(created)
            assert versions[0]["upper_limit"] == 8.0

        # 旧表已迁移并移除；再次初始化不报错、不产生重复版本
        database.init_db()
        assert len(database.list_rule_versions("truck-01")) == 1
