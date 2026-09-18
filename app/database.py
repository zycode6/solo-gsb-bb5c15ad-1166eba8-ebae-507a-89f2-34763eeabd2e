"""SQLite 存储层（标准库 sqlite3，按请求建连接，线程安全）。

规则以「设备 + 版本」的方式只追加（append-only）存储：同一设备的每个版本
带有 effective_at，版本号按设备递增。事件查询时按每条读数当时已生效的
最新版本计算，规则切换点强制结束旧事件。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "coldchain.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS device_rule_versions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id          TEXT NOT NULL,
    version            INTEGER NOT NULL,        -- 设备内递增，从 1 开始
    upper_limit        REAL NOT NULL,
    lower_limit        REAL NOT NULL,
    tolerance_seconds  INTEGER NOT NULL DEFAULT 300,
    max_gap_seconds    INTEGER NOT NULL DEFAULT 1800,
    effective_at       TEXT NOT NULL,           -- ISO 8601, UTC，生效时间
    created_at         TEXT NOT NULL,           -- ISO 8601, UTC，写入时间
    UNIQUE (device_id, version)
);

CREATE INDEX IF NOT EXISTS idx_rule_versions_device_eff
    ON device_rule_versions (device_id, effective_at);

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

_LATEST_RULE_SELECT = """
SELECT device_id, version, upper_limit, lower_limit, tolerance_seconds,
       max_gap_seconds, effective_at, created_at
FROM device_rule_versions
WHERE device_id = ?
ORDER BY version DESC
LIMIT 1
"""


class RuleOrderingError(ValueError):
    """新版本生效时间不晚于该设备最新版本的生效时间（倒序写入）。"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_legacy_rules(conn: sqlite3.Connection) -> None:
    """把旧版单值 device_rules 表迁移为版本表（旧记录作为 v1，生效时间取创建时间）。"""
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "device_rules" not in tables:
        return
    legacy = conn.execute(
        """
        SELECT device_id, upper_limit, lower_limit, tolerance_seconds,
               max_gap_seconds, created_at, updated_at
        FROM device_rules
        """
    ).fetchall()
    for row in legacy:
        # 旧库时间直接沿用；异常时间戳兜底为 UTC now
        effective_at = row["created_at"] or utcnow_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO device_rule_versions
                (device_id, version, upper_limit, lower_limit,
                 tolerance_seconds, max_gap_seconds, effective_at, created_at)
            VALUES (?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (row["device_id"], row["upper_limit"], row["lower_limit"],
             row["tolerance_seconds"], row["max_gap_seconds"],
             effective_at, effective_at),
        )
    migrated = {r["device_id"] for r in legacy}
    if migrated:
        placeholders = ",".join("?" for _ in migrated)
        remaining = conn.execute(
            f"SELECT COUNT(*) AS n FROM device_rules "
            f"WHERE device_id IN ({placeholders})",
            tuple(migrated),
        ).fetchone()["n"]
        if remaining == len(migrated):
            conn.execute("DROP TABLE device_rules")


def init_db() -> None:
    if DB_PATH != ":memory:":
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_legacy_rules(conn)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- 设备规则版本 ----------

def add_rule_version(device_id: str, upper: float, lower: float,
                     tolerance: int, max_gap: int,
                     effective_at_iso: str) -> dict:
    """追加一条规则版本。

    effective_at_iso 必须晚于该设备最新版本的 effective_at，
    否则抛出 RuleOrderingError（同一事务内校验，避免并发倒序）。
    """
    created_at = utcnow_iso()
    with _connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(_LATEST_RULE_SELECT, (device_id,)).fetchone()
            if latest is not None and effective_at_iso <= latest["effective_at"]:
                conn.rollback()
                raise RuleOrderingError(
                    f"设备 {device_id!r} 最新规则版本 v{latest['version']} 的生效时间为 "
                    f"{latest['effective_at']}，新版本生效时间必须严格晚于该时间"
                )
            next_version = 1 if latest is None else latest["version"] + 1
            conn.execute(
                """
                INSERT INTO device_rule_versions
                    (device_id, version, upper_limit, lower_limit,
                     tolerance_seconds, max_gap_seconds, effective_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (device_id, next_version, upper, lower, tolerance, max_gap,
                 effective_at_iso, created_at),
            )
            conn.commit()
        except RuleOrderingError:
            raise
        except Exception:
            conn.rollback()
            raise
    return get_rule(device_id)


def _rule_from_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    # 兼容旧响应字段：updated_at 取写入时间
    data["updated_at"] = data["created_at"]
    return data


def get_rule(device_id: str) -> dict | None:
    """取设备当前最新版本的规则。"""
    with _connect() as conn:
        row = conn.execute(_LATEST_RULE_SELECT, (device_id,)).fetchone()
    return _rule_from_row(row) if row else None


def list_rules() -> list[dict]:
    """列出全部设备的最新版本规则。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT v.device_id, v.version, v.upper_limit, v.lower_limit,
                   v.tolerance_seconds, v.max_gap_seconds,
                   v.effective_at, v.created_at
            FROM device_rule_versions v
            JOIN (
                SELECT device_id, MAX(version) AS max_version
                FROM device_rule_versions
                GROUP BY device_id
            ) m ON m.device_id = v.device_id AND m.max_version = v.version
            ORDER BY v.device_id
            """
        ).fetchall()
    return [_rule_from_row(r) for r in rows]


def list_rule_versions(device_id: str) -> list[dict]:
    """按版本号（生效时间）升序列出某设备的全部规则版本。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT device_id, version, upper_limit, lower_limit,
                   tolerance_seconds, max_gap_seconds,
                   effective_at, created_at
            FROM device_rule_versions
            WHERE device_id = ?
            ORDER BY version
            """,
            (device_id,),
        ).fetchall()
    return [_rule_from_row(r) for r in rows]


def delete_device(device_id: str) -> bool:
    """删除设备全部规则版本及其全部读数，返回设备是否存在过。"""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM device_rule_versions WHERE device_id = ?", (device_id,)
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
