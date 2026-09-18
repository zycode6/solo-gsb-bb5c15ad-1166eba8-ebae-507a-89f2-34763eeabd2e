"""连续超温事件识别算法（支持规则版本）。

判定规则（按读数当时已生效的设备规则版本）：
- 温度 > upper_limit 或 < lower_limit 视为超限读数；
- 超限后若读数回到正常区间，但恢复正常持续的时长不超过
  tolerance_seconds 就又再次超限，则视为同一次事件（短暂恢复不切断）；
- 恢复正常超过 tolerance_seconds 后再超限，则开启新事件；
- 相邻两条读数（无论是否超限）的时间间隔超过 max_gap_seconds，
  视为数据中断，强制分段；
- 连续的超限读数（中间没有恢复正常）始终属于同一事件；
- **规则版本切换强制分段**：新版本生效的那一刻旧事件立即结束，
  即使旧事件尚未观察到恢复读数，也不允许跨版本合并事件。

规则选择：对时间戳 ts 的读数，应用 effective_at <= ts 的最新版本；
没有任何版本在 ts 时已生效的（早于首个版本生效时间的历史读数），
应用最早的版本（首个版本，视为设备自始的初始规则）。

时长口径（基于离散采样点的区间归属近似）：
- 事件起点 = 首个超限读数时间；
- 事件终点 = 最后一次超限之后首个恢复正常读数的时间；
  若之后没有读数（事件仍在持续）或在规则切换/数据中断处被强制结束，
  终点 = 最后超限读数时间（强制结束时 ongoing=False）；
- violation_seconds（累计超限时长）= 事件窗口内，凡左端点超限的
  相邻读数区间长度之和。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Rule:
    upper_limit: float
    lower_limit: float
    tolerance_seconds: int = 300
    max_gap_seconds: int = 1800


@dataclass(frozen=True)
class RuleVersion:
    """一条规则版本：rule 自 effective_at（含）起生效。"""
    version: int
    effective_at: datetime
    rule: Rule


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


def _index_versions(versions: list[RuleVersion]) -> tuple[list[datetime], list[RuleVersion]]:
    ordered = sorted(versions, key=lambda v: v.effective_at)
    return [v.effective_at for v in ordered], ordered


def rule_at(ts: datetime, effective_times: list[datetime],
            ordered: list[RuleVersion]) -> RuleVersion:
    """返回 ts 时刻已生效的最新规则版本；早于首个版本时回退到首个版本。"""
    idx = bisect_right(effective_times, ts) - 1
    if idx < 0:
        idx = 0
    return ordered[idx]


def detect_events_versioned(
    readings: list[Reading],
    versions: list[RuleVersion],
) -> list[dict]:
    """按规则版本对单个设备的历史读数做事件切分。

    同一事件窗口内的读数必然属于同一规则版本：版本切换点会强制闭合旧事件。
    返回事件按 start_time 排序，每条事件带 rule_version / rule_effective_at。
    """
    if not versions:
        return []
    ordered = sorted(readings, key=lambda r: r.ts)
    effective_times, ordered_versions = _index_versions(versions)

    events: list[dict] = []
    current: list[Reading] | None = None  # 当前未闭合事件窗口内的全部读数
    current_version: RuleVersion | None = None
    recovery_start: datetime | None = None  # 当前事件内恢复正常区间的起点
    prev_ts: datetime | None = None

    def close_current(force_closed: bool) -> None:
        nonlocal current, current_version, recovery_start
        if current is not None and current_version is not None:
            events.append(
                _summarize(current, current_version.rule,
                           current_version, force_closed)
            )
        current = None
        current_version = None
        recovery_start = None

    for r in ordered:
        rv = rule_at(r.ts, effective_times, ordered_versions)

        # 规则切换点：强制结束旧事件，禁止跨版本合并
        if current is not None and current_version is not None \
                and rv.version != current_version.version:
            close_current(force_closed=True)

        # 数据间隔过长：强制切断当前事件（使用当前版本的 max_gap）
        if (
            current is not None
            and prev_ts is not None
            and (r.ts - prev_ts).total_seconds() > rv.rule.max_gap_seconds
        ):
            close_current(force_closed=True)

        if is_violation(r.temperature, rv.rule):
            if current is None:
                current = [r]
                current_version = rv
            elif (
                recovery_start is not None
                and (r.ts - recovery_start).total_seconds()
                > current_version.rule.tolerance_seconds
            ):
                # 恢复正常已超过容忍时长，旧事件闭合、新事件开始
                close_current(force_closed=False)
                current = [r]
                current_version = rv
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

    close_current(force_closed=False)
    return events


def detect_events(readings: list[Reading], rule: Rule) -> list[dict]:
    """单规则（无版本概念）的事件切分，保持原有行为与返回字段。"""
    if not readings:
        return []
    # 全部读数使用同一规则；effective_at 取最早读数时间即可命中
    first_ts = min(r.ts for r in readings)
    single = [RuleVersion(version=1, effective_at=first_ts, rule=rule)]
    events = detect_events_versioned(readings, single)
    for ev in events:
        ev.pop("rule_version", None)
        ev.pop("rule_effective_at", None)
    return events


def _summarize(window: list[Reading], rule: Rule,
               version: RuleVersion, force_closed: bool) -> dict:
    """把一个事件窗口内的读数汇总为事件记录。window 已按时间排序。"""
    violating = [r for r in window if is_violation(r.temperature, rule)]
    start = violating[0].ts
    last_violation = violating[-1].ts

    # 终点：最后超限之后首个恢复正常的读数；
    # 没有则说明事件在最后超限读数处结束（持续中，或被规则切换/数据中断强制结束）
    end = last_violation
    recovered = False
    for r in window:
        if r.ts > last_violation:
            end = r.ts
            recovered = True
            break
    ongoing = not recovered and not force_closed

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
        "rule_version": version.version,
        "rule_effective_at": version.effective_at,
    }
