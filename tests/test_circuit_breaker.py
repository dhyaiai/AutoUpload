"""
CircuitBreaker 熔断器单元测试
覆盖: 阈值熔断 / 时间窗过期 / 熔断期解除 / 浏览器重启熔断 / 全量熔断 / 重置语义
"""
import time
from datetime import datetime, timedelta

import pytest

from auto_retry_agent import CircuitBreaker
from conftest import insert_failed_record


class FakeClock:
    """可控时钟，替换 time.time 实现确定性时间推进"""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(time, "time", fake)
    return fake


class TestErrorTypeTrip:
    def test_threshold_trips(self, clock):
        cb = CircuitBreaker(threshold=3, duration_seconds=600)
        for _ in range(2):
            cb.record_error("login_expired")
        assert not cb.is_tripped("login_expired")
        cb.record_error("login_expired")
        assert cb.is_tripped("login_expired")

    def test_types_are_isolated(self, clock):
        cb = CircuitBreaker(threshold=2, duration_seconds=600)
        cb.record_error("network_error")
        cb.record_error("network_error")
        assert cb.is_tripped("network_error")
        assert not cb.is_tripped("login_expired")

    def test_window_expiry_without_new_record(self, clock):
        """旧时间戳过期后即使不再 record_error 也不应熔断（回归历史 bug）"""
        cb = CircuitBreaker(threshold=3, duration_seconds=600)
        for _ in range(3):
            cb.record_error("element_timeout")
        # 未触发 is_tripped 前时间窗直接过期
        cb2 = CircuitBreaker(threshold=3, duration_seconds=600)
        for _ in range(2):
            cb2.record_error("element_timeout")
        clock.advance(601)
        cb2.record_error("element_timeout")  # 窗口内只剩这1条
        assert not cb2.is_tripped("element_timeout")

    def test_trip_expires_after_duration(self, clock):
        cb = CircuitBreaker(threshold=2, duration_seconds=600)
        cb.record_error("network_error")
        cb.record_error("network_error")
        assert cb.is_tripped("network_error")
        clock.advance(601)
        # 熔断期已过 → 解除；且窗口内时间戳也已过期，不会立刻再次熔断
        assert not cb.is_tripped("network_error")

    def test_get_tripped_types_cleans_expired(self, clock):
        cb = CircuitBreaker(threshold=1, duration_seconds=300)
        cb.record_error("a")
        cb.record_error("b")
        assert cb.is_tripped("a") and cb.is_tripped("b")
        assert cb.get_tripped_types() == {"a", "b"}
        clock.advance(301)
        assert cb.get_tripped_types() == set()

    def test_reset_error_clears_trip_and_counts(self, clock):
        cb = CircuitBreaker(threshold=2, duration_seconds=600)
        cb.record_error("login_expired")
        cb.record_error("login_expired")
        assert cb.is_tripped("login_expired")
        cb.reset_error("login_expired")
        assert not cb.is_tripped("login_expired")
        assert cb.error_counts.get("login_expired") is None
        # 重置后重新计数，只有再次达到阈值才熔断
        cb.record_error("login_expired")
        assert not cb.is_tripped("login_expired")

    def test_reset_all_errors(self, clock):
        cb = CircuitBreaker(threshold=1, duration_seconds=600)
        cb.record_error("a")
        cb.record_error("b")
        cb._global_tripped = True
        assert cb.is_tripped("a")
        cb.reset_all_errors()
        assert not cb.is_tripped("a")
        assert not cb.is_tripped("b")
        assert not cb.global_tripped


class TestBrowserTrip:
    def test_browser_restart_limit(self):
        cb = CircuitBreaker(browser_max_restarts=3)
        for _ in range(2):
            cb.record_browser_restart()
        assert not cb.is_browser_tripped()
        cb.record_browser_restart()
        assert cb.is_browser_tripped()

    def test_reset_after_successful_restart(self):
        cb = CircuitBreaker(browser_max_restarts=3)
        for _ in range(3):
            cb.record_browser_restart()
        assert cb.is_browser_tripped()
        cb.reset_browser_restart_count()
        assert not cb.is_browser_tripped()
        assert cb.browser_restart_count == 0


class TestGlobalTrip:
    def test_global_trip_on_high_failure_rate(self, fresh_db):
        """30分钟窗口内失败率 ≥ 30% → 全量熔断，任何错误类型都被拦截"""
        cb = CircuitBreaker(global_failure_rate_threshold=0.30)
        # 10条记录，4条失败 → 40%
        for i in range(6):
            fresh_db.add_record(f"ok_{i}.docx", f"/tmp/ok_{i}.docx",
                                "测试中学高二", "测试中学", "高二", "数学",
                                status="success")
        for i in range(4):
            insert_failed_record(fresh_db, f"/tmp/bad_{i}.docx")
        cb.check_global_trip(fresh_db)
        assert cb.global_tripped
        assert cb.is_tripped("any_error_type_at_all")

    def test_global_trip_recovers_when_rate_drops(self, fresh_db):
        cb = CircuitBreaker(global_failure_rate_threshold=0.30)
        for i in range(2):
            insert_failed_record(fresh_db, f"/tmp/bad_{i}.docx")
        cb.check_global_trip(fresh_db)
        assert cb.global_tripped  # 2/2 = 100%
        # 补充大量成功记录，失败率降到 30% 以下
        for i in range(8):
            fresh_db.add_record(f"ok_{i}.docx", f"/tmp/ok_{i}.docx",
                                "测试中学高二", "测试中学", "高二", "数学",
                                status="success")
        cb.check_global_trip(fresh_db)
        assert not cb.global_tripped
        assert not cb.is_tripped("network_error")

    def test_no_records_no_trip(self, fresh_db):
        cb = CircuitBreaker()
        cb.check_global_trip(fresh_db)
        assert not cb.global_tripped
