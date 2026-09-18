"""Pydantic 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator, model_validator


def to_utc(dt: datetime) -> datetime:
    """naive 时间按 UTC 处理，aware 时间统一换算到 UTC。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class DeviceRuleIn(BaseModel):
    upper_limit: float = Field(..., description="温度上限（℃），超过视为超限")
    lower_limit: float = Field(..., description="温度下限（℃），低于视为超限")
    tolerance_seconds: int = Field(
        300, ge=0,
        description="容忍时长（秒）：恢复正常不超过该时长再超限，仍算同一事件")
    max_gap_seconds: int = Field(
        1800, ge=0,
        description="最大上报间隔（秒）：相邻读数间隔超过该值则强制分段")

    @model_validator(mode="after")
    def _check_limits(self):
        if self.lower_limit >= self.upper_limit:
            raise ValueError("lower_limit 必须小于 upper_limit")
        return self


class DeviceRule(DeviceRuleIn):
    device_id: str
    created_at: datetime
    updated_at: datetime


class ReadingIn(BaseModel):
    device_id: str = Field(..., min_length=1)
    timestamp: datetime = Field(..., description="ISO 8601 时间，无时区按 UTC 处理")
    temperature: float

    @field_validator("timestamp")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        return to_utc(v)


class ReadingBatchIn(BaseModel):
    readings: list[ReadingIn] = Field(..., min_length=1, max_length=10000)


class IngestResult(BaseModel):
    inserted: int


class ReadingOut(BaseModel):
    device_id: str
    ts: datetime
    temperature: float


class EventOut(BaseModel):
    device_id: str
    start_time: datetime = Field(..., description="事件起点（首个超限读数时间）")
    end_time: datetime = Field(..., description="事件终点（恢复正常的首个读数时间；未恢复则为最后超限时间）")
    ongoing: bool = Field(..., description="事件是否仍在持续（尚未观察到恢复）")
    duration_seconds: float = Field(..., description="事件起止跨度（秒）")
    violation_seconds: float = Field(..., description="累计超限时长（秒）")
    event_type: str = Field(..., description="high=超上限 / low=超下限 / mixed=两者都有")
    peak_temp: float = Field(..., description="峰值温度（偏离限值最大的读数）")
    peak_time: datetime
    max_temp: float
    min_temp: float
    reading_count: int
