"""
ExperienceMemory 经验记忆系统测试
覆盖: 指纹归一化 / 历史方案聚合排序 / 动态策略修正四条规则
      (高成功率覆盖 / 平手取轻量 / 低成功率升级+L4封顶 / L5永不修正 / 样本不足)
"""
import pytest

from error_types import RetryLevel
from experience_memory import ExperienceMemory, MIN_SAMPLES


@pytest.fixture
def mem(fresh_db):
    return ExperienceMemory(fresh_db)


def record_batch(mem, n_success, n_fail, error_type="upload_submit_timeout",
                 fail_stage="submit_upload", page_state="home",
                 action_sequence=None, retry_level="L2"):
    """写入 n_success+n_fail 条同指纹经验并回填结果"""
    seq = action_sequence or ["close_dialog", "verify_recovery",
                              f"enqueue_retry({retry_level})"]
    for i in range(n_success + n_fail):
        exp_id = mem.record_disposal(
            error_type, fail_stage, page_state,
            record_id=1000 + i, file_name=f"t{i}.docx",
            action_sequence=seq, decision_action="enqueue",
            retry_level=retry_level, source="react")
        mem.mark_outcome(exp_id, i < n_success)


class TestFingerprint:
    def test_normal(self, mem):
        assert mem.build_fingerprint("a", "b", "c") == "a|b|c"

    def test_empty_parts_normalized_to_unknown(self, mem):
        assert mem.build_fingerprint(None, "", "  ") == "unknown|unknown|unknown"
        assert mem.build_fingerprint("a", None, "home") == "a|unknown|home"


class TestHistoryHint:
    def test_hint_sorted_by_success_rate(self, mem):
        # 方案A: 3/3 成功；方案B: 1/2 成功 → A 应排在前面
        record_batch(mem, 3, 0, action_sequence=["navigate_home",
                                                 "verify_recovery",
                                                 "enqueue_retry(L2)"])
        record_batch(mem, 1, 1, action_sequence=["refresh_page",
                                                 "enqueue_retry(L2)"])
        hint = mem.build_history_hint("upload_submit_timeout",
                                      "submit_upload", "home")
        assert "历史处置经验" in hint
        pos_a = hint.index("navigate_home")
        pos_b = hint.index("refresh_page")
        assert pos_a < pos_b
        assert "成功3/尝试3" in hint
        assert "成功1/尝试2" in hint

    def test_no_success_no_hint(self, mem):
        """只有失败经验 → 不注入（避免误导 LLM 复用失败方案）"""
        record_batch(mem, 0, 3)
        assert mem.build_history_hint("upload_submit_timeout",
                                      "submit_upload", "home") == ""

    def test_pending_outcomes_excluded(self, mem):
        """未回填结果(pending)的经验不参与统计"""
        exp_id = mem.record_disposal(
            "upload_submit_timeout", "submit_upload", "home",
            record_id=1, file_name="t.docx",
            action_sequence=["full_recovery"], decision_action="enqueue",
            retry_level="L3", source="react")  # outcome 默认 pending
        assert exp_id
        assert mem.build_history_hint("upload_submit_timeout",
                                      "submit_upload", "home") == ""

    def test_different_fingerprint_isolated(self, mem):
        record_batch(mem, 3, 0, page_state="home")
        assert mem.build_history_hint("upload_submit_timeout",
                                      "submit_upload", "no_browser") == ""


class TestAdjustedStrategy:
    def test_high_success_rate_overrides_static(self, mem):
        """规则2: 静态L2，L3 样本≥5 且成功率≥0.6 → 覆盖为 L3"""
        record_batch(mem, 5, 1, retry_level="L3",
                     action_sequence=["full_recovery", "enqueue_retry(L3)"])
        level, max_r, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "upload_submit_timeout")
        assert adjusted and level == RetryLevel.L3_ENV_RESET

    def test_below_threshold_rate_no_override(self, mem):
        """成功率 < 0.6 → 不覆盖"""
        record_batch(mem, 2, 3, retry_level="L3")  # 40%
        level, _, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "upload_submit_timeout")
        assert not adjusted and level == RetryLevel.L2_PAGE_RESET

    def test_insufficient_samples_no_override(self, mem):
        """样本 < MIN_SAMPLES → 不修正（哪怕成功率100%）"""
        record_batch(mem, MIN_SAMPLES - 1, 0, retry_level="L3")
        level, _, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "upload_submit_timeout")
        assert not adjusted and level == RetryLevel.L2_PAGE_RESET

    def test_same_level_high_rate_not_marked_adjusted(self, mem):
        """静态级别自身成功率高 → 无需修正 adjusted=False"""
        record_batch(mem, 5, 0, retry_level="L2")
        level, _, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "upload_submit_timeout")
        assert not adjusted and level == RetryLevel.L2_PAGE_RESET

    def test_tie_prefers_lighter_level(self, mem):
        """规则2平手: 两级别成功率相同 → 取更轻量级别"""
        # 静态 (school_check, school_switch_fail) → L3
        record_batch(mem, 5, 1, error_type="school_switch_fail",
                     fail_stage="school_check", retry_level="L1")
        record_batch(mem, 5, 1, error_type="school_switch_fail",
                     fail_stage="school_check", retry_level="L4")
        level, _, adjusted = mem.get_adjusted_strategy(
            "school_check", "school_switch_fail")
        assert adjusted and level == RetryLevel.L1_LIGHT_RETRY

    def test_poor_static_rate_upgrades_one_level(self, mem):
        """规则3: 静态级别样本≥5且成功率<0.2 → 升一级"""
        record_batch(mem, 0, 5, retry_level="L2")  # 0%
        level, _, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "upload_submit_timeout")
        assert adjusted and level == RetryLevel.L3_ENV_RESET

    def test_upgrade_capped_at_l4(self, mem):
        """规则3: 静态已是 L4 → 不再升级（L5 是人工专属，不可自动到达）"""
        record_batch(mem, 0, 5, error_type="browser_start_fail",
                     fail_stage="browser_init", retry_level="L4")
        level, _, adjusted = mem.get_adjusted_strategy(
            "browser_init", "browser_start_fail")
        assert not adjusted and level == RetryLevel.L4_SERVICE_RESTART

    def test_l5_never_adjusted(self, mem):
        """安全红线: L5_MANUAL 永不被经验统计修正"""
        # 即使有人伪造了大量"L1成功"经验
        record_batch(mem, 10, 0, error_type="permission_denied",
                     fail_stage="submit_upload", retry_level="L1")
        level, max_r, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "permission_denied")
        assert not adjusted
        assert level == RetryLevel.L5_MANUAL
        assert max_r == 0

    def test_unknown_type_defaults_l5_and_not_adjusted(self, mem):
        level, _, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "完全未知的错误类型")
        assert not adjusted and level == RetryLevel.L5_MANUAL

    def test_invalid_retry_level_rows_ignored(self, mem, fresh_db):
        """经验表中存在非法 retry_level(脏数据) → 统计时安全忽略"""
        for i in range(6):
            fresh_db.add_repair_experience(
                fingerprint="upload_submit_timeout|submit_upload|home",
                error_type="upload_submit_timeout",
                fail_stage="submit_upload", page_state="home",
                record_id=i, file_name=f"x{i}.docx",
                action_sequence="[]", decision_action="enqueue",
                retry_level="L99", decision_source="react",
                outcome="success")
        level, _, adjusted = mem.get_adjusted_strategy(
            "submit_upload", "upload_submit_timeout")
        assert not adjusted and level == RetryLevel.L2_PAGE_RESET
