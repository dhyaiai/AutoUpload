"""
enqueue_retry 闸门 / 修复后强制验证 三重保障测试（核心安全测试）

三重机制:
  1. _mark_repair 标记      — 任何修复动作后 _repair_performed=True、_recovery_verified=False
  2. enqueue_retry 硬闸门   — 修复后未验证时自动执行 verify_home_ready，不通过拒绝入队
  3. 最终决策回溯检查        — LLM 绕过工具直接输出 enqueue 时强制补验证，失败降级 skip

全部用"不听话的脚本化 LLM"验证 LLM 无法绕过工具层约束。
"""
import json

import pytest

from error_types import RetryLevel
from conftest import (ScriptedLLM, FakeBrowser, FakeConfig, make_agent,
                      insert_failed_record, call, final, tool_observations)


def run_react(agent, record, tripped_types=None, page_context=None, hint=""):
    return agent._run_react_loop(record, tripped_types or set(),
                                 page_context, hint)


def last_messages(llm):
    """LLM 最后一次收到的完整对话（含全部 tool observation）"""
    return llm.requests[-1]["messages"] if llm.requests else []


@pytest.fixture
def failed_record(fresh_db, tmp_path):
    f = tmp_path / "作业.docx"
    f.write_text("test")
    return insert_failed_record(fresh_db, str(f))


class TestRepairThenVerifyGate:
    """闸门2: 修复动作后未验证 → enqueue_retry 自动验证"""

    def test_repair_without_verify_rejected_when_page_broken(
            self, fresh_db, failed_record):
        """LLM 修复后跳过 verify_recovery 直接入队 + 页面实际没修好
        → 工具拒绝入队 → 最终 enqueue 决策也被回溯检查降级为 skip"""
        browser = FakeBrowser()
        browser.default_verify = {"verified": False, "state": "upload_dialog",
                                  "details": "弹窗仍在"}
        llm = ScriptedLLM([
            call("close_dialog"),
            call("enqueue_retry", retry_level="L2"),   # 跳过 verify_recovery
            final({"action": "enqueue", "retry_level": "L2",
                   "reason": "强行入队"}),               # 还想绕过
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)

        # 工具层拒绝
        obs = tool_observations(last_messages(llm), "enqueue_retry")
        assert obs and "拒绝入队" in obs[0]
        # 回溯检查降级
        assert decision["action"] == "skip"
        assert "验证未通过" in decision["reason"]
        # 自动验证被强制执行了两次（工具闸门1次 + 回溯检查1次）
        assert browser.verify_calls == 2

    def test_repair_with_auto_verify_pass_allows_enqueue(
            self, fresh_db, failed_record):
        """修复后未显式验证但页面确实修好了 → 闸门自动验证放行"""
        browser = FakeBrowser()  # default_verify verified=True
        llm = ScriptedLLM([
            call("close_dialog"),
            call("enqueue_retry", retry_level="L2"),
            final({"action": "enqueue", "retry_level": "L2",
                   "reason": "弹窗已清理"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)

        obs = tool_observations(last_messages(llm), "enqueue_retry")
        assert json.loads(obs[0]).get("success") is True
        assert decision["action"] == "enqueue"
        assert decision["retry_level"] == RetryLevel.L2_PAGE_RESET
        # 自动验证通过后置位，回溯检查不再重复验证
        assert browser.verify_calls == 1
        # 动作序列被正确提取（供经验记忆）
        assert decision["_action_sequence"] == ["close_dialog",
                                                "enqueue_retry(L2)"]

    def test_explicit_verify_pass_no_double_check(self, fresh_db, failed_record):
        """按规范流程 修复→verify_recovery→入队 → 只验证一次"""
        browser = FakeBrowser()
        llm = ScriptedLLM([
            call("refresh_page"),
            call("verify_recovery"),
            call("enqueue_retry", retry_level="L2"),
            final({"action": "enqueue", "retry_level": "L2", "reason": "ok"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert decision["action"] == "enqueue"
        assert browser.verify_calls == 1

    def test_no_repair_enqueue_skips_verification(self, fresh_db, failed_record):
        """未执行任何修复动作 → 入队不触发强制验证"""
        browser = FakeBrowser()
        llm = ScriptedLLM([
            call("capture_page_error"),
            call("enqueue_retry", retry_level="L1"),
            final({"action": "enqueue", "retry_level": "L1", "reason": "偶发"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert decision["action"] == "enqueue"
        assert browser.verify_calls == 0

    def test_verify_fail_then_repair_again_and_pass(self, fresh_db, failed_record):
        """验证失败 → 换下一个修复动作 → 再验证通过 → 入队成功（试探链路）"""
        browser = FakeBrowser()
        browser.verify_results = [
            {"verified": False, "state": "upload_dialog", "details": "弹窗还在"},
            {"verified": True, "state": "home", "details": ""},
        ]
        llm = ScriptedLLM([
            call("close_dialog"),
            call("verify_recovery"),        # 第1次验证失败
            call("navigate_home"),          # 升级动作（重置验证标记）
            call("verify_recovery"),        # 第2次验证通过
            call("enqueue_retry", retry_level="L2"),
            final({"action": "enqueue", "retry_level": "L2", "reason": "ok"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert decision["action"] == "enqueue"
        assert browser.verify_calls == 2
        obs = tool_observations(last_messages(llm), "enqueue_retry")
        assert json.loads(obs[0]).get("success") is True


class TestFinalDecisionBackstop:
    """闸门3: LLM 完全绕过 enqueue_retry 工具直接输出最终 enqueue 决策"""

    def test_bypass_tool_final_enqueue_forced_verify_fail_downgrades(
            self, fresh_db, failed_record):
        browser = FakeBrowser()
        browser.default_verify = {"verified": False, "state": "error",
                                  "details": "页面错误"}
        llm = ScriptedLLM([
            call("refresh_page"),
            final({"action": "enqueue", "retry_level": "L2",
                   "reason": "我觉得修好了"}),   # 不调用任何验证/入队工具
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert decision["action"] == "skip"
        assert browser.verify_calls == 1

    def test_bypass_tool_final_enqueue_verify_pass_allowed(
            self, fresh_db, failed_record):
        browser = FakeBrowser()  # 验证会通过
        llm = ScriptedLLM([
            call("refresh_page"),
            final({"action": "enqueue", "retry_level": "L2", "reason": "ok"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert decision["action"] == "enqueue"
        assert browser.verify_calls == 1


class TestEnqueueSoftGuards:
    """enqueue_retry 工具内的软校验（重试上限/熔断/重复入队）"""

    def test_retry_count_cap(self, fresh_db, tmp_path):
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f), retry_count=3)
        llm = ScriptedLLM([
            call("enqueue_retry", retry_level="L1"),
            final({"action": "manual", "reason": "重试次数用尽"}),
        ])
        agent = make_agent(fresh_db, FakeBrowser(), llm,
                           config=FakeConfig(max_retry_count=3))
        decision = run_react(agent, record)
        obs = tool_observations(last_messages(llm), "enqueue_retry")
        assert "最大重试次数" in obs[0]
        assert decision["action"] == "manual"

    def test_tripped_error_type_rejected(self, fresh_db, failed_record):
        llm = ScriptedLLM([
            call("enqueue_retry", retry_level="L1"),
            final({"action": "skip", "reason": "熔断中"}),
        ])
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        decision = run_react(agent, failed_record,
                             tripped_types={"upload_submit_timeout"})
        obs = tool_observations(last_messages(llm), "enqueue_retry")
        assert "已熔断" in obs[0]
        assert decision["action"] == "skip"

    def test_duplicate_in_retry_rejected(self, fresh_db, failed_record):
        llm = ScriptedLLM([
            call("enqueue_retry", retry_level="L1"),
            final({"action": "skip", "reason": "已在队列"}),
        ])
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        agent._in_retry.add(failed_record["file_path"])
        run_react(agent, failed_record)
        obs = tool_observations(last_messages(llm), "enqueue_retry")
        assert "已在重试队列" in obs[0]


class TestDangerousActionHardLimits:
    """危险动作的工具层硬约束"""

    def test_restart_browser_blocked_by_circuit_breaker(
            self, fresh_db, failed_record):
        """浏览器重启次数超限 → restart_browser 工具被熔断器硬拦截"""
        browser = FakeBrowser()
        llm = ScriptedLLM([
            call("restart_browser"),
            final({"action": "manual", "reason": "浏览器熔断"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        for _ in range(3):  # browser_max_restarts 默认 3
            agent.circuit_breaker.record_browser_restart()
        decision = run_react(agent, failed_record)

        obs = tool_observations(last_messages(llm), "restart_browser")
        assert "熔断" in obs[0]
        # 浏览器完全没有被真正重启
        assert "close" not in browser.calls
        assert browser.restart_cycles == 0
        assert decision["action"] == "manual"

    def test_restart_browser_allowed_and_counted(self, fresh_db, failed_record):
        """未熔断时重启放行，且成功后计数重置、失败路径正确"""
        browser = FakeBrowser()
        llm = ScriptedLLM([
            call("restart_browser"),
            call("verify_recovery"),
            call("enqueue_retry", retry_level="L3"),
            final({"action": "enqueue", "retry_level": "L3", "reason": "ok"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert browser.restart_cycles == 1
        # 重启成功 → 熔断计数被重置
        assert agent.circuit_breaker.browser_restart_count == 0
        assert decision["action"] == "enqueue"
        assert decision["_browser_restarted"] is True

    def test_re_login_refused_outside_login_page(self, fresh_db, failed_record):
        """页面不在 login/role_select 时 re_login 拒绝执行（不误伤正常会话）"""
        browser = FakeBrowser()
        browser.default_state = "home"
        llm = ScriptedLLM([
            call("re_login"),
            call("enqueue_retry", retry_level="L1"),
            final({"action": "enqueue", "retry_level": "L1", "reason": "ok"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)

        obs = tool_observations(last_messages(llm), "re_login")
        assert "无需重新登录" in obs[0]
        assert "_login" not in browser.calls
        # 拒绝执行 = 不算修复动作 → 入队不触发强制验证
        assert browser.verify_calls == 0
        assert decision["action"] == "enqueue"

    def test_re_login_executes_on_login_page(self, fresh_db, failed_record):
        browser = FakeBrowser()
        browser.default_state = "login"
        browser.verify_results = [{"verified": True, "state": "home",
                                   "details": ""}]
        llm = ScriptedLLM([
            call("re_login"),
            call("verify_recovery"),
            call("enqueue_retry", retry_level="L3"),
            final({"action": "enqueue", "retry_level": "L3", "reason": "ok"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert "_login" in browser.calls
        assert decision["action"] == "enqueue"
        assert decision["retry_level"] == RetryLevel.L3_ENV_RESET


class TestReactResultShape:
    """ReAct 返回值结构与降级行为"""

    def test_invalid_retry_level_falls_back_to_l1(self, fresh_db, failed_record):
        llm = ScriptedLLM([
            final({"action": "enqueue", "retry_level": "L99", "reason": "x"}),
        ])
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        decision = run_react(agent, failed_record)
        assert decision["retry_level"] == RetryLevel.L1_LIGHT_RETRY

    def test_react_failure_returns_none_for_rule_fallback(
            self, fresh_db, failed_record):
        """LLM 调用失败 → 返回 None，让主流程回退规则引擎"""
        llm = ScriptedLLM([("none", None)])
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        assert run_react(agent, failed_record) is None

    def test_full_recovery_success_flags_propagated(self, fresh_db, failed_record):
        """full_recovery 成功 → _full_recovery_succeeded 透传给统一后处理"""
        browser = FakeBrowser()
        browser.default_state = "home"
        llm = ScriptedLLM([
            call("full_recovery"),
            call("enqueue_retry", retry_level="L3"),
            final({"action": "enqueue", "retry_level": "L3", "reason": "已恢复"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        decision = run_react(agent, failed_record)
        assert decision["_full_recovery_succeeded"] is True
        assert decision["action"] == "enqueue"
        assert "full_recovery" in decision["_action_sequence"]
