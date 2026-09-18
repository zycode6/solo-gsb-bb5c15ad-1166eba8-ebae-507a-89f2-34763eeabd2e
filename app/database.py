"""SQLite 存储层（标准库 sqlite3，按请求建连接，线程安全）。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "coldchain.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_rules (
    device_id          TEXT PRIMARY KEY,
    upper_limit        REAL NOT NULL,
    lower_limit        REAL NOT NULL,
    tolerance_seconds  INTEGER NOT NULL DEFAULT 300,
    max_gap_seconds    INTEGER NOT NULL DEFAULT 1800,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    ts          TEXT NOT NULL,          -- ISO 8601, 统一存 UTC
    temperature REAL NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (device_id, ts)              -- 同一时间重复上报按最新覆盖，保证幂等
);

CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings (device_id, ts);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    if DB_PATH != ":memory:":
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- 设备规则 ----------

def upsert_rule(device_id: str, upper: float, lower: float,
                tolerance: int, max_gap: int) -> dict:
    now = utcnow_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO device_rules
                (device_id, upper_limit, lower_limit, tolerance_seconds,
                 max_gap_seconds, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                upper_limit=excluded.upper_limit,
                lower_limit=excluded.lower_limit,
                tolerance_seconds=excluded.tolerance_seconds,
                max_gap_seconds=excluded.max_gap_seconds,
                updated_at=excluded.updated_at
            """,
            (device_id, upper, lower, tolerance, max_gap, now, now),
        )
    return get_rule(device_id)


def get_rule(device_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM device_rules WHERE device_id = ?", (device_id,)
        ).fetchone()
    return dict(row) if row else None


def list_rules() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM device_rules ORDER BY device_id"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_device(device_id: str) -> bool:
    """删除设备规则及其全部读数，返回设备是否存在过。"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM device_rules WHERE device_id = ?", (device_id,)
        )
        conn.execute("DELETE FROM readings WHERE device_id = ?", (device_id,))
        return cur.rowcount > 0


# ---------- 温度读数 ----------

def insert_readings(readings: list[tuple[str, str, float]]) -> int:
    """readings: [(device_id, ts_iso, temperature), ...]，返回写入条数。"""
    now = utcnow_iso()
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO readings (device_id, ts, temperature, received_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id, ts) DO UPDATE SET
                temperature=excluded.temperature,
                received_at=excluded.received_at
            """,
            [(d, ts, t, now) for d, ts, t in readings],
        )
    return len(readings)


def fetch_readings(device_id: str, end_iso: str | None = None) -> list[dict]:
    """取某设备截至 end（含）的全部读数，按时间升序。"""
    sql = "SELECT device_id, ts, temperature FROM readings WHERE device_id = ?"
    params: list = [device_id]
    if end_iso is not None:
        sql += " AND ts <= ?"
        params.append(end_iso)
    sql += " ORDER BY ts"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_readings_window(device_id: str, start_iso: str | None,
                          end_iso: str | None, limit: int) -> list[dict]:
    sql = "SELECT device_id, ts, temperature, received_at FROM readings WHERE device_id = ?"
    params: list = [device_id]
    if start_iso is not None:
        sql += " AND ts >= ?"
        params.append(start_iso)
    if end_iso is not None:
        sql += " AND ts <= ?"
        params.append(end_iso)
    sql += " ORDER BY ts LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_device_ids_with_readings() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT DISTINCT device_id FROM readings").fetchall()
    return [r["device_id"] for r in rows]
