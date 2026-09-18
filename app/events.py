"""连续超温事件识别算法。

判定规则（按设备配置）：
- 温度 > upper_limit 或 < lower_limit 视为超限读数；
- 超限后若读数回到正常区间，但恢复正常持续的时长不超过
  tolerance_seconds 就又再次超限，则视为同一次事件（短暂恢复不切断）；
- 恢复正常超过 tolerance_seconds 后再超限，则开启新事件；
- 相邻两条读数（无论是否超限）的时间间隔超过 max_gap_seconds，
  视为数据中断，强制分段；
- 连续的超限读数（中间没有恢复正常）始终属于同一事件。

时长口径（基于离散采样点的区间归属近似）：
- 事件起点 = 首个超限读数时间；
- 事件终点 = 最后一次超限之后首个恢复正常读数的时间；
  若之后没有读数（事件仍在持续），终点 = 最后超限读数时间且 ongoing=True；
- violation_seconds（累计超限时长）= 事件窗口内，凡左端点超限的
  相邻读数区间长度之和。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Rule:
    upper_limit: float
    lower_limit: float
    tolerance_seconds: int = 300
    max_gap_seconds: int = 1800


@dataclass(frozen=True)
class Reading:
    ts: datetime
    temperature: float


def is_violation(temperature: float, rule: Rule) -> bool:
    return temperature > rule.upper_limit or temperature < rule.lower_limit


def _deviation(temperature: float, rule: Rule) -> float:
    """超出限值的幅度（限值内为 0）。"""
    if temperature > rule.upper_limit:
        return temperature - rule.upper_limit
    if temperature < rule.lower_limit:
        return rule.lower_limit - temperature
    return 0.0


def detect_events(readings: list[Reading], rule: Rule) -> list[dict]:
    """对单个设备的历史读数做事件切分，返回按时间排序的事件列表。"""
    ordered = sorted(readings, key=lambda r: r.ts)
    events: list[dict] = []
    current: list[Reading] | None = None  # 当前未闭合事件窗口内的全部读数
    recovery_start: datetime | None = None  # 当前事件内恢复正常区间的起点
    prev_ts: datetime | None = None

    for r in ordered:
        # 数据间隔过长：强制切断当前事件
        if (
            current is not None
            and prev_ts is not None
            and (r.ts - prev_ts).total_seconds() > rule.max_gap_seconds
        ):
            events.append(_summarize(current, rule))
            current = None
            recovery_start = None

        if is_violation(r.temperature, rule):
            if current is None:
                current = [r]
            elif (
                recovery_start is not None
                and (r.ts - recovery_start).total_seconds() > rule.tolerance_seconds
            ):
                # 恢复正常已超过容忍时长，旧事件闭合、新事件开始
                events.append(_summarize(current, rule))
                current = [r]
            else:
                # 连续超限，或短暂恢复后再超限：同一事件延续
                current.append(r)
            recovery_start = None
        elif current is not None:
            # 事件窗口内的正常读数（短暂恢复或最终恢复），保留用于时长计算
            current.append(r)
            if recovery_start is None:
                recovery_start = r.ts

        prev_ts = r.ts

    if current:
        events.append(_summarize(current, rule))
    return events


def _summarize(window: list[Reading], rule: Rule) -> dict:
    """把一个事件窗口内的读数汇总为事件记录。window 已按时间排序。"""
    violating = [r for r in window if is_violation(r.temperature, rule)]
    start = violating[0].ts
    last_violation = violating[-1].ts

    # 终点：最后超限之后首个恢复正常的读数；没有则说明事件仍在持续
    end = last_violation
    ongoing = True
    for r in window:
        if r.ts > last_violation:
            end = r.ts
            ongoing = False
            break

    span = [r for r in window if start <= r.ts <= end]

    violation_seconds = 0.0
    for a, b in zip(span, span[1:]):
        if is_violation(a.temperature, rule):
            violation_seconds += (b.ts - a.ts).total_seconds()

    has_high = any(r.temperature > rule.upper_limit for r in violating)
    has_low = any(r.temperature < rule.lower_limit for r in violating)
    event_type = "mixed" if (has_high and has_low) else ("high" if has_high else "low")

    peak = max(span, key=lambda r: _deviation(r.temperature, rule))

    return {
        "start_time": start,
        "end_time": end,
        "ongoing": ongoing,
        "duration_seconds": (end - start).total_seconds(),
        "violation_seconds": violation_seconds,
        "event_type": event_type,
        "peak_temp": peak.temperature,
        "peak_time": peak.ts,
        "max_temp": max(r.temperature for r in span),
        "min_temp": min(r.temperature for r in span),
        "reading_count": len(span),
    }
