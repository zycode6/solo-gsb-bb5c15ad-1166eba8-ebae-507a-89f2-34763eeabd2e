"""规则版本化事件识别算法单元测试。"""

from datetime import datetime, timedelta, timezone

from app.events import Reading, Rule, RuleVersion, detect_events_versioned, rule_at

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
# 上限 8℃、下限 2℃、容忍 5 分钟、最大间隔 30 分钟
RULE_V1 = Rule(upper_limit=8.0, lower_limit=2.0, tolerance_seconds=300, max_gap_seconds=1800)
SWITCH = BASE + timedelta(minutes=30)


def r(minutes: float, temp: float) -> Reading:
    return Reading(ts=BASE + timedelta(minutes=minutes), temperature=temp)


def v1(rule: Rule = RULE_V1, at: datetime = BASE) -> RuleVersion:
    return RuleVersion(version=1, effective_at=at, rule=rule)


def v2(rule: Rule, at: datetime = SWITCH) -> RuleVersion:
    return RuleVersion(version=2, effective_at=at, rule=rule)


class TestRuleAt:
    def test_uses_latest_version_effective_at_ts(self):
        versions = [v1(), v2(RULE_V1)]
        eff = [v1().effective_at, SWITCH]
        ordered = [v1(), v2(RULE_V1)]
        assert rule_at(BASE + timedelta(minutes=29), eff, ordered).version == 1
        # 生效时间点本身即应用新版本（effective_at <= ts）
        assert rule_at(SWITCH, eff, ordered).version == 2
        assert rule_at(BASE + timedelta(days=1), eff, ordered).version == 2

    def test_before_first_version_falls_back_to_first(self):
        versions = [v1(at=BASE + timedelta(days=1))]
        eff = [versions[0].effective_at]
        assert rule_at(BASE, eff, versions).version == 1


class TestVersionedDetection:
    def test_single_version_behaves_like_plain_rule(self):
        events = detect_events_versioned([
            r(0, 5.0),
            r(10, 9.0),
            r(20, 10.0),
            r(30, 5.0),
        ], [v1()])
        assert len(events) == 1
        ev = events[0]
        assert ev["rule_version"] == 1
        assert ev["rule_effective_at"] == BASE
        assert ev["start_time"] == BASE + timedelta(minutes=10)
        assert ev["end_time"] == BASE + timedelta(minutes=30)

    def test_empty_versions_returns_empty(self):
        assert detect_events_versioned([r(0, 9.0)], []) == []

    def test_rule_switch_forces_split_even_when_still_violating(self):
        # v2 与 v1 限值相同；切换点两侧都在超限，仍必须切成两个事件
        events = detect_events_versioned([
            r(0, 9.0),    # v1 超限
            r(10, 10.0),  # v1 超限
            r(30, 9.5),   # v2 生效，超限 → 新事件
            r(40, 5.0),   # v2 恢复
        ], [v1(), v2(RULE_V1)])
        assert len(events) == 2

        old, new = events
        assert old["rule_version"] == 1
        assert old["start_time"] == BASE
        # 旧事件在切换点结束：终点为最后超限读数，非持续中
        assert old["end_time"] == BASE + timedelta(minutes=10)
        assert old["ongoing"] is False

        assert new["rule_version"] == 2
        assert new["rule_effective_at"] == SWITCH
        assert new["start_time"] == SWITCH
        assert new["end_time"] == BASE + timedelta(minutes=40)

    def test_brief_recovery_does_not_merge_across_versions(self):
        # 单规则下 0→10 恢复→18 再超限（间隔 8 分钟内）会合并，
        # 但 10→18 之间发生了版本切换，必须禁止跨版本合并
        events = detect_events_versioned([
            r(0, 9.0),    # v1 超限
            r(10, 5.0),   # v1 恢复
            r(32, 5.0),   # v2 正常
            r(34, 9.0),   # v2 超限 → 必须是新事件
            r(50, 5.0),
        ], [v1(), v2(RULE_V1)])
        assert len(events) == 2
        assert [e["rule_version"] for e in events] == [1, 2]
        assert events[0]["end_time"] == BASE + timedelta(minutes=10)
        assert events[1]["start_time"] == BASE + timedelta(minutes=34)

    def test_each_reading_uses_rule_effective_at_that_time(self):
        # v1 上限 10（9 度正常），v2 收紧到上限 8（9 度超限）
        relaxed = Rule(upper_limit=10.0, lower_limit=2.0,
                       tolerance_seconds=300, max_gap_seconds=1800)
        events = detect_events_versioned([
            r(0, 9.0),    # v1：正常
            r(10, 9.0),   # v1：正常
            r(30, 9.0),   # v2：超限 → 事件起点
            r(40, 5.0),
        ], [v1(relaxed), v2(RULE_V1)])
        assert len(events) == 1
        ev = events[0]
        assert ev["rule_version"] == 2
        assert ev["start_time"] == SWITCH

    def test_loosened_limit_closes_old_event_at_switch(self):
        # v1 上限 8（9 度超限），v2 放宽到上限 10（9 度正常）
        relaxed = Rule(upper_limit=10.0, lower_limit=2.0,
                       tolerance_seconds=300, max_gap_seconds=1800)
        events = detect_events_versioned([
            r(0, 9.0),    # v1：超限
            r(10, 9.0),   # v1：超限
            r(30, 9.0),   # v2：已正常 → 旧事件在切换点结束
        ], [v1(RULE_V1), v2(relaxed)])
        assert len(events) == 1
        ev = events[0]
        assert ev["rule_version"] == 1
        assert ev["end_time"] == BASE + timedelta(minutes=10)
        assert ev["ongoing"] is False

    def test_switch_reading_at_exact_effective_time_uses_new_rule(self):
        # 读数时间戳恰好等于新版本生效时间 → 应用新版本
        events = detect_events_versioned([
            r(0, 9.0),
            r(30, 9.0),
        ], [v1(RULE_V1), v2(Rule(upper_limit=10.0, lower_limit=2.0,
                                 tolerance_seconds=300, max_gap_seconds=1800))])
        assert len(events) == 1
        assert events[0]["rule_version"] == 1
        assert events[0]["end_time"] == BASE  # 第二个读数已不算超限

    def test_readings_before_first_version_use_first_version(self):
        # 首个版本晚生效：更早的历史读数回退应用首个版本
        late = BASE + timedelta(days=1)
        events = detect_events_versioned([
            r(0, 9.0),
            r(10, 5.0),
        ], [v1(RULE_V1, at=late)])
        assert len(events) == 1
        assert events[0]["rule_version"] == 1
        assert events[0]["rule_effective_at"] == late

    def test_versions_unsorted_are_indexed_by_effective_time(self):
        events = detect_events_versioned([
            r(0, 9.0),
            r(40, 5.0),
        ], [v2(RULE_V1), v1(RULE_V1)])
        assert [e["rule_version"] for e in events] == [1]

    def test_force_closed_old_event_violation_seconds(self):
        # 旧事件被切换强制结束：超限时长只统计切换前区间
        events = detect_events_versioned([
            r(0, 9.0),
            r(10, 10.0),
            r(30, 11.0),
        ], [v1(), v2(RULE_V1)])
        old = events[0]
        # 0→10 一段，左端点超限，共 10 分钟
        assert old["violation_seconds"] == 600
        assert old["duration_seconds"] == 600
