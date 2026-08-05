"""
批次完成通知单元测试
覆盖:
  - get_batch_stats: 按 file_path 聚合终态统计（同文件多条记录不重复计数）
  - count_pending_by_paths: 待重试记录计数
  - _check_batch_complete: 批次结束判定（无 pending / Agent 禁用 / 超时兜底 / 队列未空）
  - _finalize_batch: 统计汇总 + BATCH_DONE 指令 + 批次状态重置
"""
import json
import threading
import time
from queue import Queue
from types import SimpleNamespace

from conftest import insert_failed_record

from db_manager import DatabaseManager
from upload_processor import UploadProcessor, BATCH_DONE_TIMEOUT


# ─── 构造 UploadProcessor（绕过 __init__，避免真实 ConfigManager/浏览器单例） ───

def make_processor(db: DatabaseManager, task_queue=None, log_queue=None,
                   auto_retry_enable=True, processing=False):
    proc = UploadProcessor.__new__(UploadProcessor)
    proc.task_queue = task_queue or Queue()
    proc.stop_event = threading.Event()
    proc.log_queue = log_queue or Queue()
    proc.db = db
    proc.config = SimpleNamespace(auto_retry_enable=auto_retry_enable)
    proc.processing = processing
    proc._batch_active = True
    proc._batch_files = set()
    proc._batch_start_time = time.time()
    return proc


def drain(log_queue):
    """取出队列全部消息"""
    items = []
    while not log_queue.empty():
        items.append(log_queue.get_nowait())
    return items


def set_agent_recovered(db, file_path):
    """模拟 AutoRetryAgent 修复成功：更新记录为 success + agent 标记"""
    cursor = db._connection.cursor()
    cursor.execute('''
        UPDATE upload_records
        SET status = 'success', retry_status = 'finished', agent_retry_success = '是'
        WHERE file_path = ?
    ''', (file_path,))
    db._connection.commit()


def mark_manual(db, file_path):
    """模拟 Agent 判 manual/skip：失败记录标记为 finished"""
    cursor = db._connection.cursor()
    cursor.execute('''
        UPDATE upload_records SET retry_status = 'finished' WHERE file_path = ?
    ''', (file_path,))
    db._connection.commit()


class TestGetBatchStats:
    def test_mixed_states(self, fresh_db):
        """四种终态各自计数正确"""
        fresh_db.add_record("a.docx", "/p/a.docx", "测试中学高二",
                            "测试中学", "高二", "数学", status="success")
        # b: 失败后 Agent 修复
        insert_failed_record(fresh_db, "/p/b.docx", file_name="b.docx")
        set_agent_recovered(fresh_db, "/p/b.docx")
        # c: 需手动处理
        insert_failed_record(fresh_db, "/p/c.docx", file_name="c.docx")
        mark_manual(fresh_db, "/p/c.docx")
        # d: 仍在重试
        insert_failed_record(fresh_db, "/p/d.docx", file_name="d.docx")

        stats = fresh_db.get_batch_stats(
            ["/p/a.docx", "/p/b.docx", "/p/c.docx", "/p/d.docx"])
        assert stats == {"total": 4, "direct_success": 1, "agent_recovered": 1,
                         "manual": 1, "retrying": 1}

    def test_fail_then_direct_success_counts_once(self, fresh_db):
        """同一文件先失败后直接上传成功：只计一次 direct_success，不重复计数"""
        file_path = "/p/a.docx"
        insert_failed_record(fresh_db, file_path, file_name="a.docx")
        # 模拟 _on_upload_success 流程：新建成功记录 + 旧失败记录置 success
        fresh_db.add_record("a.docx", file_path, "测试中学高二",
                            "测试中学", "高二", "数学", status="success")
        fresh_db.resolve_pending_by_file("a.docx", "测试中学高二")

        stats = fresh_db.get_batch_stats([file_path])
        assert stats == {"total": 1, "direct_success": 1, "agent_recovered": 0,
                         "manual": 0, "retrying": 0}

    def test_fail_then_agent_recovered_counts_once(self, fresh_db):
        """同一文件先失败后 Agent 修复：只计一次 agent_recovered"""
        file_path = "/p/b.docx"
        insert_failed_record(fresh_db, file_path, file_name="b.docx")
        set_agent_recovered(fresh_db, file_path)

        stats = fresh_db.get_batch_stats([file_path])
        assert stats == {"total": 1, "direct_success": 0, "agent_recovered": 1,
                         "manual": 0, "retrying": 0}

    def test_agent_recovered_outranks_direct_success(self, fresh_db):
        """同一文件同时存在直接成功记录和 Agent 修复记录：取最高终态 agent_recovered"""
        file_path = "/p/c.docx"
        fresh_db.add_record("c.docx", file_path, "测试中学高二",
                            "测试中学", "高二", "数学", status="success")
        insert_failed_record(fresh_db, file_path, file_name="c.docx")
        set_agent_recovered(fresh_db, file_path)

        stats = fresh_db.get_batch_stats([file_path])
        assert stats == {"total": 1, "direct_success": 0, "agent_recovered": 1,
                         "manual": 0, "retrying": 0}

    def test_no_db_record_ignored(self, fresh_db):
        """已上传过被跳过的文件（无记录）不计入 total"""
        stats = fresh_db.get_batch_stats(["/p/skipped.docx"])
        assert stats == {"total": 0, "direct_success": 0, "agent_recovered": 0,
                         "manual": 0, "retrying": 0}

    def test_empty_paths(self, fresh_db):
        stats = fresh_db.get_batch_stats([])
        assert stats == {"total": 0, "direct_success": 0, "agent_recovered": 0,
                         "manual": 0, "retrying": 0}


class TestCountPendingByPaths:
    def test_counts_only_pending(self, fresh_db):
        insert_failed_record(fresh_db, "/p/a.docx", file_name="a.docx")          # pending
        insert_failed_record(fresh_db, "/p/b.docx", file_name="b.docx")
        mark_manual(fresh_db, "/p/b.docx")                                       # finished
        fresh_db.add_record("c.docx", "/p/c.docx", "测试中学高二",
                            "测试中学", "高二", "数学", status="success")

        assert fresh_db.count_pending_by_paths(
            ["/p/a.docx", "/p/b.docx", "/p/c.docx", "/p/other.docx"]) == 1

    def test_empty_paths(self, fresh_db):
        assert fresh_db.count_pending_by_paths([]) == 0


class TestCheckBatchComplete:
    def test_no_pending_finalizes(self, fresh_db):
        """全部落定 → 结束批次并发通知"""
        proc = make_processor(fresh_db)
        fresh_db.add_record("a.docx", "/p/a.docx", "测试中学高二",
                            "测试中学", "高二", "数学", status="success")
        proc._batch_files = {"/p/a.docx"}
        proc._check_batch_complete()
        assert not proc._batch_active
        messages = drain(proc.log_queue)
        assert any(m.startswith("BATCH_DONE:") for m in messages)

    def test_pending_waits_for_agent(self, fresh_db):
        """有 pending 且 Agent 启用且未超时 → 批次继续，不通知"""
        proc = make_processor(fresh_db)
        insert_failed_record(fresh_db, "/p/a.docx", file_name="a.docx")
        proc._batch_files = {"/p/a.docx"}
        proc._check_batch_complete()
        assert proc._batch_active
        assert drain(proc.log_queue) == []

    def test_agent_disabled_finalizes(self, fresh_db):
        """Agent 禁用时失败记录不会落定 → 直接结束（失败归入需手动处理）"""
        proc = make_processor(fresh_db, auto_retry_enable=False)
        insert_failed_record(fresh_db, "/p/a.docx", file_name="a.docx")
        proc._batch_files = {"/p/a.docx"}
        proc._check_batch_complete()
        assert not proc._batch_active

    def test_timeout_finalizes(self, fresh_db):
        """pending 持续超过 BATCH_DONE_TIMEOUT → 强制结束兜底"""
        proc = make_processor(fresh_db)
        insert_failed_record(fresh_db, "/p/a.docx", file_name="a.docx")
        proc._batch_files = {"/p/a.docx"}
        proc._batch_start_time = time.time() - BATCH_DONE_TIMEOUT - 1
        proc._check_batch_complete()
        assert not proc._batch_active
        messages = drain(proc.log_queue)
        assert any(m.startswith("BATCH_DONE:") for m in messages)

    def test_queue_not_empty_holds(self, fresh_db):
        """队列还有任务 → 批次未结束"""
        queue = Queue()
        queue.put("/p/b.docx")
        proc = make_processor(fresh_db, task_queue=queue)
        proc._batch_files = {"/p/a.docx"}
        proc._check_batch_complete()
        assert proc._batch_active

    def test_processing_holds(self, fresh_db):
        """处理中 → 批次未结束"""
        proc = make_processor(fresh_db, processing=True)
        proc._batch_files = {"/p/a.docx"}
        proc._check_batch_complete()
        assert proc._batch_active

    def test_inactive_batch_noop(self, fresh_db):
        proc = make_processor(fresh_db)
        proc._batch_active = False
        proc._check_batch_complete()  # 不抛异常即可
        assert drain(proc.log_queue) == []


class TestFinalizeBatch:
    def test_sends_stats_and_resets(self, fresh_db):
        """汇总统计发 BATCH_DONE 指令（JSON），并重置批次状态"""
        proc = make_processor(fresh_db)
        fresh_db.add_record("a.docx", "/p/a.docx", "测试中学高二",
                            "测试中学", "高二", "数学", status="success")
        insert_failed_record(fresh_db, "/p/b.docx", file_name="b.docx")
        set_agent_recovered(fresh_db, "/p/b.docx")
        insert_failed_record(fresh_db, "/p/c.docx", file_name="c.docx")
        mark_manual(fresh_db, "/p/c.docx")
        proc._batch_files = {"/p/a.docx", "/p/b.docx", "/p/c.docx"}

        proc._finalize_batch()

        assert not proc._batch_active
        assert proc._batch_files == set()
        messages = drain(proc.log_queue)
        done = [m for m in messages if m.startswith("BATCH_DONE:")]
        assert len(done) == 1
        stats = json.loads(done[0][len("BATCH_DONE:"):])
        assert stats == {"total": 3, "direct_success": 1, "agent_recovered": 1,
                         "manual": 1, "retrying": 0}
