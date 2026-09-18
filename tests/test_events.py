"""事件识别算法单元测试。"""

from datetime import datetime, timedelta, timezone

from app.events import Reading, Rule, detect_events

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
# 上限 8℃、下限 2℃、容忍 5 分钟、最大间隔 30 分钟
RULE = Rule(upper_limit=8.0, lower_limit=2.0, tolerance_seconds=300, max_gap_seconds=1800)


def r(minutes: float, temp: float) -> Reading:
    return Reading(ts=BASE + timedelta(minutes=minutes), temperature=temp)


class TestNoEvent:
    def test_all_in_range(self):
        assert detect_events([r(0, 5.0), r(10, 6.0), r(20, 4.0)], RULE) == []

    def test_empty(self):
        assert detect_events([], RULE) == []

    def test_boundary_values_are_not_violations(self):
        assert detect_events([r(0, 8.0), r(10, 2.0)], RULE) == []


class TestSingleEvent:
    def test_basic_event_fields(self):
        events = detect_events([
            r(0, 5.0),    # 正常
            r(10, 9.0),   # 超限开始
            r(20, 10.5),  # 峰值
            r(30, 8.5),
            r(40, 5.0),   # 恢复
            r(50, 5.0),
        ], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["start_time"] == BASE + timedelta(minutes=10)
        assert ev["end_time"] == BASE + timedelta(minutes=40)
        assert ev["ongoing"] is False
        assert ev["event_type"] == "high"
        assert ev["peak_temp"] == 10.5
        assert ev["peak_time"] == BASE + timedelta(minutes=20)
        assert ev["max_temp"] == 10.5
        assert ev["min_temp"] == 5.0
        # 累计超限：10→20、20→30、30→40 三段左端点均超限，共 30 分钟
        assert ev["violation_seconds"] == 30 * 60
        assert ev["duration_seconds"] == 30 * 60

    def test_low_temperature_event(self):
        events = detect_events([
            r(0, 1.0),   # 低于下限
            r(10, 0.5),  # 峰值（最低）
            r(20, 4.0),  # 恢复
        ], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["event_type"] == "low"
        assert ev["peak_temp"] == 0.5
        assert ev["violation_seconds"] == 20 * 60

    def test_ongoing_event_uses_last_violation_as_end(self):
        events = detect_events([r(0, 9.0), r(10, 9.5)], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["ongoing"] is True
        assert ev["end_time"] == BASE + timedelta(minutes=10)
        # 最后一个超限点之后没有读数，不计时长
        assert ev["violation_seconds"] == 10 * 60

    def test_consecutive_violations_stay_one_event(self):
        # 持续超限、上报间隔 20 分钟（< max_gap），中间没有恢复 → 同一事件
        events = detect_events([
            r(0, 9.0),
            r(20, 9.5),
            r(40, 8.5),
            r(60, 5.0),
        ], RULE)
        assert len(events) == 1
        assert events[0]["violation_seconds"] == 60 * 60


class TestToleranceMerging:
    def test_brief_recovery_does_not_split(self):
        # 恢复正常持续 2 分钟（< 容忍 5 分钟）后再次超限 → 同一事件
        events = detect_events([
            r(0, 9.0),
            r(2, 5.0),   # 短暂恢复
            r(4, 9.2),   # 再次超限
        ], RULE)
        assert len(events) == 1
        ev = events[0]
        assert ev["start_time"] == BASE
        assert ev["end_time"] == BASE + timedelta(minutes=4)
        assert ev["ongoing"] is True
        # 只有 0→2 这一段左端点超限
        assert ev["violation_seconds"] == 2 * 60

    def test_recovery_longer_than_tolerance_splits(self):
        # 恢复正常持续 9 分钟（> 容忍 5 分钟）→ 两个事件
        events = detect_events([
            r(0, 9.0),
            r(1, 5.0),    # 恢复
            r(10, 9.0),   # 已恢复 9 分钟 → 新事件
            r(20, 5.0),
        ], RULE)
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[0]["end_time"] == BASE + timedelta(minutes=1)
        assert events[1]["start_time"] == BASE + timedelta(minutes=10)
        assert events[1]["end_time"] == BASE + timedelta(minutes=20)

    def test_recovery_exactly_at_tolerance_stays_one_event(self):
        events = detect_events([
            r(0, 9.0),
            r(1, 5.0),   # 恢复
            r(6, 9.0),   # 恢复恰好 5 分钟 = 容忍时长，不切断
            r(7, 5.0),
        ], RULE)
        assert len(events) == 1


class TestGapSplitting:
    def test_long_gap_splits_even_when_still_violating(self):
        # 两次超限读数间隔 40 分钟 > max_gap 30 分钟 → 强制分段
        events = detect_events([
            r(0, 9.0),
            r(40, 9.5),
            r(50, 5.0),
        ], RULE)
        assert len(events) == 2
        assert events[0]["start_time"] == BASE
        assert events[0]["end_time"] == BASE  # 单点事件，起止相同
        assert events[0]["violation_seconds"] == 0
        assert events[1]["start_time"] == BASE + timedelta(minutes=40)
        assert events[1]["end_time"] == BASE + timedelta(minutes=50)

    def test_gap_in_normal_region_also_splits(self):
        events = detect_events([
            r(0, 9.0),
            r(1, 5.0),    # 恢复
            r(40, 9.0),   # 间隔 39 分钟 > max_gap → 新事件
        ], RULE)
        assert len(events) == 2


class TestOrdering:
    def test_unsorted_input_is_handled(self):
        events = detect_events([
            r(20, 5.0),
            r(0, 9.0),
            r(10, 9.5),
        ], RULE)
        assert len(events) == 1
        assert events[0]["start_time"] == BASE
        assert events[0]["violation_seconds"] == 20 * 60
