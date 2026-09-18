"""冷链温度事件分析 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from . import database as db
from .events import Reading, Rule, RuleVersion, detect_events_versioned
from .schemas import (
    DeviceRule,
    DeviceRuleIn,
    EventOut,
    IngestResult,
    ReadingBatchIn,
    ReadingOut,
    to_utc,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="Cold Chain Temperature Event API",
    version="1.1.0",
    description=(
        "接收设备按时间上报的温度读数，按每个设备配置的上下限与容忍时长"
        "识别连续超温事件，返回事件起止时间、峰值与累计超限时长。"
        "规则支持版本化：每条读数应用其当时已生效的规则版本，"
        "规则切换点强制结束旧事件，事件结果带 rule_version / rule_effective_at。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- 设备规则配置（版本化） ----------

@app.put("/api/v1/devices/{device_id}/rule", response_model=DeviceRule)
def put_device_rule(device_id: str, rule: DeviceRuleIn):
    """创建或追加设备的规则版本。

    - 不传 effective_at：立即生效（生效时间取当前 UTC 时间）；
    - 传 effective_at：自该时间起生效，按读数时间选择版本；
    - effective_at 必须严格晚于该设备最新版本的生效时间，否则 409。
    """
    effective_at = rule.effective_at
    if effective_at is None:
        # 未传 effective_at：立即生效
        effective_at = datetime.now(timezone.utc)
    try:
        return db.add_rule_version(
            device_id,
            rule.upper_limit,
            rule.lower_limit,
            rule.tolerance_seconds,
            rule.max_gap_seconds,
            effective_at.isoformat(),
        )
    except db.RuleOrderingError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/devices", response_model=list[DeviceRule])
def list_devices():
    """列出全部设备（每个设备返回其最新版本规则）。"""
    return db.list_rules()


@app.get("/api/v1/devices/{device_id}/rule/versions", response_model=list[DeviceRule])
def list_device_rule_versions(device_id: str):
    """按版本号（生效时间）升序列出某设备的全部规则版本。"""
    versions = db.list_rule_versions(device_id)
    if not versions:
        raise HTTPException(404, f"设备 {device_id!r} 尚未配置规则")
    return versions


@app.get("/api/v1/devices/{device_id}/rule", response_model=DeviceRule)
def get_device_rule(device_id: str):
    """查询设备当前最新版本的规则。"""
    rule = db.get_rule(device_id)
    if rule is None:
        raise HTTPException(404, f"设备 {device_id!r} 尚未配置规则")
    return rule


@app.delete("/api/v1/devices/{device_id}")
def delete_device(device_id: str):
    """删除设备全部规则版本及其全部温度读数。"""
    if not db.delete_device(device_id):
        raise HTTPException(404, f"设备 {device_id!r} 不存在")
    return {"deleted": device_id}


# ---------- 温度读数上报 ----------

@app.post("/api/v1/readings", response_model=IngestResult, status_code=201)
def ingest_readings(batch: ReadingBatchIn):
    """批量上报温度读数（同一 device_id + timestamp 重复上报会覆盖旧值）。"""
    rows = [
        (r.device_id, r.timestamp.isoformat(), r.temperature)
        for r in batch.readings
    ]
    return IngestResult(inserted=db.insert_readings(rows))


@app.get("/api/v1/devices/{device_id}/readings", response_model=list[ReadingOut])
def list_readings(
    device_id: str,
    start: datetime | None = Query(None, description="起始时间（含），ISO 8601"),
    end: datetime | None = Query(None, description="结束时间（含），ISO 8601"),
    limit: int = Query(1000, ge=1, le=100000),
):
    """按时间范围查询原始读数。"""
    return db.fetch_readings_window(
        device_id,
        to_utc(start).isoformat() if start else None,
        to_utc(end).isoformat() if end else None,
        limit,
    )


# ---------- 事件查询 ----------

def _events_for_device(device_id: str,
                       start: datetime | None,
                       end: datetime | None) -> list[EventOut]:
    version_rows = db.list_rule_versions(device_id)
    if not version_rows:
        raise HTTPException(404, f"设备 {device_id!r} 尚未配置规则，无法分析事件")
    versions = [
        RuleVersion(
            version=row["version"],
            effective_at=to_utc(datetime.fromisoformat(row["effective_at"])),
            rule=Rule(
                upper_limit=row["upper_limit"],
                lower_limit=row["lower_limit"],
                tolerance_seconds=row["tolerance_seconds"],
                max_gap_seconds=row["max_gap_seconds"],
            ),
        )
        for row in version_rows
    ]
    end_iso = to_utc(end).isoformat() if end else None
    rows = db.fetch_readings(device_id, end_iso)
    readings = [
        Reading(ts=to_utc(datetime.fromisoformat(r["ts"])), temperature=r["temperature"])
        for r in rows
    ]
    events = detect_events_versioned(readings, versions)

    start_utc = to_utc(start) if start else None
    end_utc = to_utc(end) if end else None
    result = []
    for ev in events:
        # 只返回与查询区间 [start, end] 有交集的事件
        if start_utc is not None and ev["end_time"] < start_utc:
            continue
        if end_utc is not None and ev["start_time"] > end_utc:
            continue
        result.append(EventOut(device_id=device_id, **ev))
    return result


@app.get("/api/v1/devices/{device_id}/events", response_model=list[EventOut])
def device_events(
    device_id: str,
    start: datetime | None = Query(None, description="起始时间（含），ISO 8601"),
    end: datetime | None = Query(None, description="结束时间（含），ISO 8601"),
):
    """查询单个设备在指定时间范围内的超温事件（按读数当时生效的规则版本计算）。"""
    return _events_for_device(device_id, start, end)


@app.get("/api/v1/events", response_model=list[EventOut])
def all_events(
    start: datetime | None = Query(None, description="起始时间（含），ISO 8601"),
    end: datetime | None = Query(None, description="结束时间（含），ISO 8601"),
):
    """查询全部已配置设备在指定时间范围内的超温事件。"""
    result: list[EventOut] = []
    for rule in db.list_rules():
        result.extend(_events_for_device(rule["device_id"], start, end))
    result.sort(key=lambda e: (e.start_time, e.device_id))
    return result
