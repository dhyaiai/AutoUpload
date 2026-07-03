"""
失败自动接管 Agent (AutoRetryAgent)
功能: 后台常驻,自动扫描失败记录,精准定位失败阶段与根因,执行分级自愈策略
特点:
  - 独立后台线程,随程序启停
  - 通过 task_queue 串行重试,不直接操作浏览器
  - 交叉验证 + 熔断保护 + 指数退避
  - 全程更新数据库状态,与手动重试兼容
"""
import json
import os
import time
import threading
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from queue import Queue
from typing import Dict, List, Optional, Set

from db_manager import DatabaseManager
from config_manager import ConfigManager
from browser_automation import BrowserAutomation
from error_types import (
    UploadStage, ErrorCategory, ErrorType, RetryLevel,
    get_strategy, classify_error,
)


class CircuitBreaker:
    """熔断器：防止大面积故障时无效重试"""

    def __init__(self,
                 threshold: int = 10,
                 duration_seconds: int = 1800,
                 browser_max_restarts: int = 3,
                 global_failure_rate_threshold: float = 0.30):
        self.threshold = threshold
        self.duration_seconds = duration_seconds
        self.browser_max_restarts = browser_max_restarts
        self.global_failure_rate_threshold = global_failure_rate_threshold

        # 按错误类型记录失败时间戳
        self._error_timestamps: Dict[str, List[float]] = defaultdict(list)
        # 熔断状态：error_type -> 熔断解除时间
        self._tripped: Dict[str, float] = {}
        # 浏览器连续重启计数
        self._browser_restart_count: int = 0
        # 全量熔断标记
        self._global_tripped: bool = False

    def record_error(self, error_type: str):
        """记录一次错误"""
        now = time.time()
        self._error_timestamps[error_type].append(now)
        # 清理过期记录
        cutoff = now - self.duration_seconds
        self._error_timestamps[error_type] = [
            t for t in self._error_timestamps[error_type] if t > cutoff
        ]

    def record_browser_restart(self):
        """记录一次浏览器重启"""
        self._browser_restart_count += 1

    def reset_browser_restart_count(self):
        """重置浏览器重启计数（重启成功后调用）"""
        self._browser_restart_count = 0

    def is_tripped(self, error_type: str) -> bool:
        """
        检查某类错误是否已熔断

        Args:
            error_type: ErrorType 枚举值

        Returns:
            True=已熔断,不应重试
        """
        # 全量熔断
        if self._global_tripped:
            return True

        # 检查是否在熔断期内
        if error_type in self._tripped:
            if time.time() < self._tripped[error_type]:
                return True
            else:
                # 熔断期已过，解除
                del self._tripped[error_type]

        # 检查是否达到熔断阈值
        recent_count = len(self._error_timestamps.get(error_type, []))
        if recent_count >= self.threshold:
            self._tripped[error_type] = time.time() + self.duration_seconds
            return True

        return False

    def is_browser_tripped(self) -> bool:
        """检查浏览器是否已熔断"""
        return self._browser_restart_count >= self.browser_max_restarts

    def check_global_trip(self, db: DatabaseManager, window_minutes: int = 30):
        """
        检查是否需要全量熔断

        Args:
            db: 数据库管理器
            window_minutes: 统计窗口（分钟）
        """
        cutoff = (datetime.now() - timedelta(minutes=window_minutes)).strftime(
            '%Y-%m-%d %H:%M:%S')
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        stats = db.get_failed_stats_by_period(cutoff, now_str)
        total = stats['total_uploads']
        failed = stats['total_failed']

        if total > 0 and (failed / total) >= self.global_failure_rate_threshold:
            self._global_tripped = True
        else:
            self._global_tripped = False

    def get_tripped_types(self) -> Set[str]:
        """获取当前熔断中的错误类型列表"""
        tripped = set()
        for etype, until in list(self._tripped.items()):
            if time.time() < until:
                tripped.add(etype)
            else:
                del self._tripped[etype]
        return tripped


class AutoRetryAgent:
    """
    失败自动接管 Agent
    后台线程：定时扫描 → 诊断 → 自愈 → 入队重试
    """

    def __init__(self, task_queue: Queue, stop_event: threading.Event, log_queue: Queue):
        """
        Args:
            task_queue: 上传任务队列（重试文件通过此队列交给 UploadProcessor）
            stop_event: 停止信号
            log_queue: 日志队列
        """
        self.task_queue = task_queue
        self.stop_event = stop_event
        self.log_queue = log_queue

        self.db = DatabaseManager()
        self.config = ConfigManager()
        self.browser = BrowserAutomation(log_queue=log_queue)

        # 从配置读取参数
        self.enabled = self.config.get("AUTO_RETRY_ENABLE", True)
        self.scan_interval = self.config.get("AUTO_RETRY_SCAN_INTERVAL", 60)
        self.backoff_seconds = self.config.get("AUTO_RETRY_BACKOFF_SECONDS", [30, 120, 600])

        # 熔断器
        self.circuit_breaker = CircuitBreaker(
            threshold=self.config.get("AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD", 10),
            duration_seconds=self.config.get("AUTO_RETRY_CIRCUIT_BREAKER_DURATION", 1800),
        )

        # 已提交重试的文件集合（防止重复入队）
        self._in_retry: Set[str] = set()

        # UploadProcessor 引用（用于注册重试映射）
        self.upload_processor = None

    def set_upload_processor(self, processor):
        """
        注入 UploadProcessor 引用，用于重试前注册 file_path → record_id 映射

        Args:
            processor: UploadProcessor 实例
        """
        self.upload_processor = processor

    def run(self):
        """
        主运行循环
        定时扫描失败记录，执行分级自愈
        """
        self._log("AutoRetryAgent 已启动" if self.enabled else "AutoRetryAgent 已禁用")
        if not self.enabled:
            return

        while not self.stop_event.is_set():
            try:
                self._scan_and_retry()
            except Exception as e:
                self._log(f"AutoRetryAgent 扫描异常: {e}")
                traceback.print_exc()

            # 使用 stop_event.wait 替代 time.sleep，收到停止信号立即退出
            self.stop_event.wait(self.scan_interval)

        self._log("AutoRetryAgent 已停止")

    def _scan_and_retry(self):
        """扫描失败记录并执行自愈策略"""
        # 更新全量熔断检查
        self.circuit_breaker.check_global_trip(self.db)

        # 获取待处理失败记录
        records = self.db.get_pending_failed_records(limit=20)
        if not records:
            return

        tripped_types = self.circuit_breaker.get_tripped_types()
        self._log(f"AutoRetryAgent: 扫描到 {len(records)} 条待处理失败记录"
                  + (f", 熔断中: {tripped_types}" if tripped_types else ""))

        for record in records:
            if self.stop_event.is_set():
                break

            try:
                self._process_one_record(record, tripped_types)
            except Exception as e:
                self._log(f"AutoRetryAgent: 处理记录 {record.get('id')} 异常 - {e}")
                traceback.print_exc()
                # 标记为 pending 避免卡住
                try:
                    self.db.update_retry_status(record['id'], 'pending')
                except Exception:
                    pass

    def _process_one_record(self, record: Dict, tripped_types: Set[str]):
        """
        处理单条失败记录

        Args:
            record: 数据库记录字典
            tripped_types: 当前熔断的错误类型集合
        """
        record_id = record['id']
        file_path = record.get('file_path', '')
        file_name = record.get('file_name', '')
        retry_count = record.get('retry_count', 0)
        fail_stage = record.get('fail_stage')
        error_type = record.get('error_type')
        error_category = record.get('error_category')
        error_message = record.get('error_message', '')

        # ---- 检查文件是否存在 ----
        if not os.path.exists(file_path):
            self._log(f"AutoRetryAgent: 文件不存在，标记人工处理 - {file_name}")
            self.db.update_record_structured_error(
                record_id,
                error_message="文件已被删除或移动",
                fail_stage=UploadStage.READ_FILE.value,
                error_category=ErrorCategory.FILE_PROCESS_ERROR.value,
                error_type=ErrorType.FILE_NOT_EXIST.value,
            )
            self.db.update_retry_status(record_id, 'finished')
            return

        # ---- 交叉验证：推断缺失的结构化字段 ----
        if not error_type or not fail_stage:
            inferred_category, inferred_type = classify_error(error_message, fail_stage)
            if not error_category:
                error_category = inferred_category.value
            if not error_type:
                error_type = inferred_type.value
            if not fail_stage:
                fail_stage = UploadStage.SUBMIT_UPLOAD.value
            # 更新数据库
            self.db.update_record_structured_error(
                record_id,
                fail_stage=fail_stage,
                error_category=error_category,
                error_type=error_type,
            )
            self._log(f"AutoRetryAgent: 交叉验证推断 - stage={fail_stage}, type={error_type}")

        # ---- 查询自愈策略 ----
        retry_level, max_retries = get_strategy(fail_stage, error_type)

        # ---- 检查重试次数上限 ----
        if retry_count >= self.config.max_retry_count:
            self._log(f"AutoRetryAgent: 已达全局最大重试次数,标记人工处理 - {file_name}")
            self.db.update_retry_status(record_id, 'finished')
            return

        if retry_count >= max_retries and retry_level != RetryLevel.L5_MANUAL:
            self._log(f"AutoRetryAgent: 已达该错误类型最大重试次数,标记人工处理 - {file_name}({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return

        # ---- 熔断检查 ----
        if error_type and error_type in tripped_types:
            self._log(f"AutoRetryAgent: {error_type} 已熔断,跳过 - {file_name}")
            return

        if self.circuit_breaker.is_tripped(error_type or 'unknown'):
            self._log(f"AutoRetryAgent: {error_type} 触发熔断,跳过 - {file_name}")
            return

        # ---- L5 人工兜底 ----
        if retry_level == RetryLevel.L5_MANUAL:
            self._log(f"AutoRetryAgent: L5人工兜底,标记finished - {file_name} ({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return

        # ---- L4 服务重启 ----
        if retry_level == RetryLevel.L4_SERVICE_RESTART:
            if self.circuit_breaker.is_browser_tripped():
                self._log(f"AutoRetryAgent: 浏览器连续重启已达上限,跳过 - {file_name}")
                self.db.update_retry_status(record_id, 'finished')
                return

            self._log(f"AutoRetryAgent: L4 重启浏览器 - {file_name}")
            self.circuit_breaker.record_browser_restart()
            if self.browser.is_initialized:
                self.browser.close()
                time.sleep(2)
            if self.browser.ensure_initialized():
                self.circuit_breaker.reset_browser_restart_count()
                self._log("AutoRetryAgent: L4 浏览器重启成功")
            else:
                self._log("AutoRetryAgent: L4 浏览器重启失败，稍后重试")
                self.db.update_retry_status(record_id, 'pending')
                return

        # ---- L3 环境重置 ----
        if retry_level in (RetryLevel.L3_ENV_RESET,):
            self._log(f"AutoRetryAgent: L3 环境重置 - {file_name}")
            if not self.browser.reset_to_home():
                self._log(f"AutoRetryAgent: L3 环境复位失败 - {file_name}")
                self.db.update_retry_status(record_id, 'pending')
                return

        # ---- L2 页面复位 ----
        if retry_level in (RetryLevel.L2_PAGE_RESET,):
            self._log(f"AutoRetryAgent: L2 页面复位 - {file_name}")
            if not self.browser.reset_to_home():
                self._log(f"AutoRetryAgent: L2 页面复位失败 - {file_name}")
                self.db.update_retry_status(record_id, 'pending')
                return

        # ---- L1 轻量重试 ----
        # (L1 无特殊前置动作，直接重试)

        # ---- 指数退避等待 ----
        backoff_idx = min(retry_count, len(self.backoff_seconds) - 1)
        wait_seconds = self.backoff_seconds[backoff_idx]
        if retry_count > 0:
            self._log(f"AutoRetryAgent: 退避等待 {wait_seconds}s - {file_name} (第{retry_count+1}次重试)")
            self.stop_event.wait(wait_seconds)
            if self.stop_event.is_set():
                return

        # ---- 防重复入队 ----
        if file_path in self._in_retry:
            self._log(f"AutoRetryAgent: 文件已在重试队列中,跳过 - {file_name}")
            return
        self._in_retry.add(file_path)

        # ---- 入队重试 ----
        self._log(f"AutoRetryAgent: 入队重试 [{retry_level.value}] - {file_name} (stage={fail_stage}, type={error_type})")
        # 注册映射：让 UploadProcessor 知道这个文件是 Agent 触发的重试
        if self.upload_processor is not None:
            self.upload_processor.register_agent_retry(file_path, record_id)
        self.task_queue.put(file_path)

        # 记录错误用于熔断统计
        if error_type:
            self.circuit_breaker.record_error(error_type)

    def on_upload_result(self, record_id: int, success: bool, file_path: str):
        """
        由外部（UploadProcessor/GUI）在上传完成后回调
        更新 Agent 状态和数据库

        Args:
            record_id: 记录ID
            success: 是否上传成功
            file_path: 文件路径
        """
        # 从重试集合中移除
        self._in_retry.discard(file_path)

        # 更新数据库
        self.db.set_agent_retry_success(record_id, success)
        if success:
            self._log(f"AutoRetryAgent: ✓ 自动重试成功 - {os.path.basename(file_path)}")
        else:
            self._log(f"AutoRetryAgent: ✗ 自动重试失败 - {os.path.basename(file_path)}")

    def _log(self, message: str):
        """通过日志队列发送消息"""
        try:
            self.log_queue.put(message)
        except Exception:
            print(message)


# ─── CLI 独立测试入口 ───
if __name__ == "__main__":
    """
    独立测试 AutoRetryAgent 基本功能
    用法: python auto_retry_agent.py
    """
    import signal

    stop_event = threading.Event()
    task_queue = Queue()
    log_queue = Queue()

    def log_printer():
        while not stop_event.is_set():
            try:
                msg = log_queue.get(timeout=0.5)
                print(f"[LOG] {msg}")
            except Exception:
                pass

    printer_thread = threading.Thread(target=log_printer, daemon=True)
    printer_thread.start()

    agent = AutoRetryAgent(task_queue, stop_event, log_queue)
    agent_thread = threading.Thread(target=agent.run, daemon=True)
    agent_thread.start()

    print("AutoRetryAgent 测试运行中... 按 Ctrl+C 退出")
    try:
        while agent_thread.is_alive():
            agent_thread.join(1)
    except KeyboardInterrupt:
        print("\n正在停止...")
        stop_event.set()
        agent_thread.join(timeout=5)
        print("已退出")
