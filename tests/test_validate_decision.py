"""
_validate_react_decision 硬安全门禁测试 + _process_one_record 主流程闭环测试
门禁顺序: action合法性 → 全局重试上限 → 类型重试上限 → 类型熔断 → 熔断器 → L5强制人工
"""
import time

import pytest

from error_types import RetryLevel
from conftest import (ScriptedLLM, FakeBrowser, FakeConfig, make_agent,
                      insert_failed_record, get_record, get_experiences,
                      call, final)


def make_simple_agent(db, **kwargs):
    return make_agent(db, FakeBrowser(), ScriptedLLM(), **kwargs)


ENQUEUE_L1 = {"action": "enqueue", "retry_level": RetryLevel.L1_LIGHT_RETRY,
              "reason": "test"}


class TestValidateReactDecision:
    def test_invalid_action_marked_finished(self, fresh_db):
        """LLM 返回无效 action → 拒绝并标记 finished，防止记录卡 pending"""
        record = insert_failed_record(fresh_db, "/tmp/a.docx")
        agent = make_simple_agent(fresh_db)
        decision = {"action": "retry_forever", "retry_level": None}
        assert agent._validate_react_decision(decision, record, set()) is None
        assert get_record(fresh_db, record["id"])["retry_status"] == "finished"

    def test_global_retry_cap(self, fresh_db):
        record = insert_failed_record(fresh_db, "/tmp/a.docx", retry_count=3)
        agent = make_simple_agent(fresh_db,
                                  config=FakeConfig(max_retry_count=3))
        assert agent._validate_react_decision(
            dict(ENQUEUE_L1), record, set()) is None
        assert get_record(fresh_db, record["id"])["retry_status"] == "finished"

    def test_per_type_retry_cap(self, fresh_db):
        """(submit_upload, form_validate_fail) 静态策略 max_retries=1"""
        record = insert_failed_record(fresh_db, "/tmp/a.docx",
                                      error_type="form_validate_fail",
                                      retry_count=1)
        agent = make_simple_agent(fresh_db)
        assert agent._validate_react_decision(
            dict(ENQUEUE_L1), record, set()) is None
        assert get_record(fresh_db, record["id"])["retry_status"] == "finished"

    def test_tripped_type_skipped_stays_pending(self, fresh_db):
        """类型熔断 → 跳过但保持 pending（熔断解除后还能重试）"""
        record = insert_failed_record(fresh_db, "/tmp/a.docx")
        agent = make_simple_agent(fresh_db)
        assert agent._validate_react_decision(
            dict(ENQUEUE_L1), record, {"upload_submit_timeout"}) is None
        assert get_record(fresh_db, record["id"])["retry_status"] == "pending"

    def test_circuit_breaker_trip_skipped(self, fresh_db):
        record = insert_failed_record(fresh_db, "/tmp/a.docx")
        agent = make_simple_agent(fresh_db)
        agent.circuit_breaker._tripped["upload_submit_timeout"] = \
            time.time() + 9999
        assert agent._validate_react_decision(
            dict(ENQUEUE_L1), record, set()) is None
        assert get_record(fresh_db, record["id"])["retry_status"] == "pending"

    def test_l5_strategy_forces_finished_even_if_llm_says_enqueue(
            self, fresh_db):
        """永久性错误(L5) → 即使 LLM 决策 enqueue 也强制转人工"""
        record = insert_failed_record(fresh_db, "/tmp/a.docx",
                                      error_type="permission_denied")
        agent = make_simple_agent(fresh_db)
        assert agent._validate_react_decision(
            dict(ENQUEUE_L1), record, set()) is None
        assert get_record(fresh_db, record["id"])["retry_status"] == "finished"

    def test_valid_decision_passes_through(self, fresh_db):
        record = insert_failed_record(fresh_db, "/tmp/a.docx")
        agent = make_simple_agent(fresh_db)
        decision = dict(ENQUEUE_L1)
        assert agent._validate_react_decision(decision, record, set()) \
            is decision


class TestProcessOneRecord:
    """_process_one_record 端到端（全 Mock）闭环"""

    def test_missing_file_marked_manual_without_llm(self, fresh_db):
        """文件已删除 → 直接转人工，LLM 一次都不该被调用"""
        record = insert_failed_record(fresh_db, "/tmp/不存在的文件.docx")
        llm = ScriptedLLM()
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        agent._process_one_record(record, set())

        row = get_record(fresh_db, record["id"])
        assert row["retry_status"] == "finished"
        assert row["error_type"] == "file_not_exist"
        assert llm.requests == []

    def test_permanent_page_error_short_circuits_before_react(
            self, fresh_db, tmp_path):
        """预捕获发现永久性业务错误 → 不进 ReAct 直接转人工"""
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f))
        browser = FakeBrowser()
        browser.capture_result = {
            "success": True, "has_error": True,
            "errors": ["该校未开通数智作业服务"],
            "combined_text": "该校未开通数智作业服务",
            "is_permanent": True,
            "suggested_error_type": "school_not_activated",
            "page_state": "home"}
        llm = ScriptedLLM()
        agent = make_agent(fresh_db, browser, llm)
        agent._process_one_record(record, set())

        row = get_record(fresh_db, record["id"])
        assert row["retry_status"] == "finished"
        assert row["error_type"] == "school_not_activated"
        assert "[不可恢复]" in row["error_message"]
        assert llm.requests == []

    def test_full_enqueue_flow_with_experience_roundtrip(
            self, fresh_db, tmp_path):
        """完整闭环: ReAct决策enqueue → 入队/计数/经验落库 → 上传成功回调回填"""
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f))
        browser = FakeBrowser()
        llm = ScriptedLLM([
            call("capture_page_error"),
            call("enqueue_retry", retry_level="L1"),
            final({"action": "enqueue", "retry_level": "L1", "reason": "偶发超时"}),
        ])
        agent = make_agent(fresh_db, browser, llm)
        agent._process_one_record(record, set())

        # 入队副作用
        assert agent.task_queue.get_nowait() == str(f)
        row = get_record(fresh_db, record["id"])
        assert row["retry_status"] == "processing"
        assert row["retry_count"] == 1
        assert str(f) in agent._in_retry
        # 经验落库(pending) + 指纹三元组
        exps = get_experiences(fresh_db)
        assert len(exps) == 1
        assert exps[0]["outcome"] == "pending"
        assert exps[0]["fingerprint"] == "upload_submit_timeout|submit_upload|home"
        assert exps[0]["decision_source"] == "react"

        # 上传成功回调 → 记录标记成功 + 经验回填 success
        agent.on_upload_result(record["id"], True, str(f))
        row = get_record(fresh_db, record["id"])
        assert row["status"] == "success"
        assert row["retry_status"] == "finished"
        assert get_experiences(fresh_db)[0]["outcome"] == "success"
        assert str(f) not in agent._in_retry

    def test_retry_failure_feeds_circuit_breaker(self, fresh_db, tmp_path):
        """重试再失败 → 回调记录熔断统计 + 经验回填 failed"""
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f))
        llm = ScriptedLLM([
            call("capture_page_error"),
            call("enqueue_retry", retry_level="L1"),
            final({"action": "enqueue", "retry_level": "L1", "reason": "重试"}),
        ])
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        agent._process_one_record(record, set())
        agent.on_upload_result(record["id"], False, str(f))

        assert get_record(fresh_db, record["id"])["retry_status"] == "pending"
        assert get_experiences(fresh_db)[0]["outcome"] == "failed"
        # 真实失败进入熔断计数（入队时预判计数已废弃，只在失败回调计数）
        assert agent.circuit_breaker.error_counts.get(
            "upload_submit_timeout") == 2  # 入队时1次 + 失败回调1次

    def test_manual_decision_records_experience(self, fresh_db, tmp_path):
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f))
        llm = ScriptedLLM([
            call("capture_page_error"),
            call("mark_manual_review", reason="无法自动恢复"),
            final({"action": "manual", "reason": "无法自动恢复"}),
        ])
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        agent._process_one_record(record, set())

        assert get_record(fresh_db, record["id"])["retry_status"] == "finished"
        exps = get_experiences(fresh_db)
        assert len(exps) == 1
        assert exps[0]["outcome"] == "manual"
        assert exps[0]["decision_action"] == "manual"


class TestScanGate:
    """_scan_and_retry 扫描层门禁"""

    def test_tripped_records_skipped_without_processing(
            self, fresh_db, tmp_path):
        """已熔断类型的记录在扫描层被跳过，LLM 不被调用、状态不变"""
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f))
        llm = ScriptedLLM()
        agent = make_agent(fresh_db, FakeBrowser(), llm)
        agent.circuit_breaker._tripped["upload_submit_timeout"] = \
            time.time() + 9999
        agent._scan_and_retry()

        assert llm.requests == []
        assert get_record(fresh_db, record["id"])["retry_status"] == "pending"
        assert not agent.agent_busy.is_set()  # 扫描结束后必须清除忙碌标志

    def test_processing_exception_resets_to_pending(self, fresh_db, tmp_path,
                                                    monkeypatch):
        """单条记录处理异常 → 兜底重置为 pending，不卡死"""
        f = tmp_path / "a.docx"
        f.write_text("x")
        record = insert_failed_record(fresh_db, str(f))
        agent = make_agent(fresh_db, FakeBrowser(), ScriptedLLM())
        monkeypatch.setattr(agent, "_process_one_record",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("模拟崩溃")))
        agent._scan_and_retry()
        assert get_record(fresh_db, record["id"])["retry_status"] == "pending"
        assert not agent.agent_busy.is_set()
