"""
失败自动接管 Agent (AutoRetryAgent) — AI Agent 版
功能: 后台常驻,自动扫描失败记录,ReAct 循环驱动 LLM 自主诊断与自愈决策
特点:
  - 独立后台线程,随程序启停
  - ReAct (Thought→Action→Observation) 循环: LLM 自主调用工具完成诊断和决策
  - 安全守护(熔断/重试上限)在工具层硬编码,LLM 无法绕过
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

from deepseek_helper import DeepSeekHelper
from react_loop import ReActLoop
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

        # 按时间窗口过滤后计数（修复：旧时间戳在 record_error 未被调用时也会过期）
        now = time.time()
        cutoff = now - self.duration_seconds
        recent_count = sum(
            1 for t in self._error_timestamps.get(error_type, [])
            if t > cutoff
        )
        if recent_count >= self.threshold:
            self._tripped[error_type] = now + self.duration_seconds
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

    def reset_error(self, error_type: str):
        """
        清除某类错误的熔断状态和计数。
        在 full_recovery 成功后调用，避免已修复的错误类型阻塞入队。
        """
        self._error_timestamps.pop(error_type, None)
        self._tripped.pop(error_type, None)

    def reset_all_errors(self):
        """清除所有错误的熔断状态（全量恢复成功后调用）"""
        self._error_timestamps.clear()
        self._tripped.clear()
        self._global_tripped = False

    def reset_global_trip(self):
        """仅清除全量熔断标记，保留各错误类型的独立计数"""
        self._global_tripped = False

    # ─── 公共属性（供外部安全读取状态，避免直接访问私有属性）───

    @property
    def error_counts(self) -> Dict[str, int]:
        """各错误类型近期计数（只读快照）"""
        return {et: len(ts) for et, ts in self._error_timestamps.items()}

    @property
    def global_tripped(self) -> bool:
        """全量熔断状态"""
        return self._global_tripped

    @property
    def browser_restart_count(self) -> int:
        """浏览器连续重启计数"""
        return self._browser_restart_count


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

        # LLM 提供商：优先 Qwen，否则 DeepSeek
        qwen_key = self.config.qwen_api_key
        if qwen_key:
            self.deepseek = DeepSeekHelper(
                api_url="https://llm-nwnb3n9ni4k5ebc2.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                api_key=qwen_key,
                model=self.config.qwen_model
            )
            self._log(f"AutoRetryAgent: 使用 Qwen/{self.config.qwen_model}")
        else:
            self.deepseek = DeepSeekHelper()
            self._log("AutoRetryAgent: 使用 DeepSeek")

        # 复用全局浏览器单例（不传参避免覆盖已有实例的 log_queue）
        self.browser = BrowserAutomation()

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
        self._in_retry_lock = threading.Lock()  # 保护 _in_retry 的跨线程访问
        # 重试文件的错误类型映射（供 on_upload_result 失败时记录熔断统计）
        self._retry_error_types: Dict[str, str] = {}
        self._retry_error_lock = threading.Lock()

        # Agent 忙碌标志：Agent 正在执行恢复操作时阻塞 UploadProcessor 消费新任务
        self.agent_busy = threading.Event()

        # UploadProcessor 引用（用于注册重试映射）
        self.upload_processor = None

        # 临时性错误类型：环境恢复后重试即可成功，成功上传后应重置其熔断
        self._TRANSIENT_ERROR_TYPES = {
            ErrorType.UPLOAD_SUBMIT_TIMEOUT.value,
            ErrorType.FORM_VALIDATE_FAIL.value,
            ErrorType.ELEMENT_TIMEOUT.value,
            ErrorType.PAGE_LOAD_TIMEOUT.value,
            ErrorType.SCHOOL_SWITCH_FAIL.value,
            ErrorType.LOGIN_EXPIRED.value,
            ErrorType.NETWORK_ERROR.value,
            ErrorType.BROWSER_START_FAIL.value,
        }

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

            # 会话丢失时缩短轮询间隔为 5s（正常为 scan_interval，默认 60s），
            # 确保 Agent 快速响应踢下线事件，避免 UploadProcessor 长时间空等
            if (self.upload_processor is not None
                    and self.upload_processor._session_lost.is_set()):
                self.stop_event.wait(5)
            else:
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

        # 清理 _in_retry 中已不在待处理列表中的过期条目
        # （UploadProcessor 早期返回时可能未触发 on_upload_result 回调）
        pending_paths = {r.get('file_path', '') for r in records if r.get('file_path')}
        with self._in_retry_lock:
            self._in_retry = {fp for fp in self._in_retry if fp in pending_paths}
        with self._retry_error_lock:
            self._retry_error_types = {
                fp: et for fp, et in self._retry_error_types.items()
                if fp in pending_paths
            }

        tripped_types = self.circuit_breaker.get_tripped_types()
        self._log(f"AutoRetryAgent: 扫描到 {len(records)} 条待处理失败记录"
                  + (f", 熔断中: {tripped_types}" if tripped_types else ""))

        # 熔断跳过统计（避免每个记录各打一条日志刷屏）
        skipped_by_trip: Dict[str, list] = {}
        processed = 0
        skipped_trip_count = 0

        # 标记 Agent 忙碌，阻塞 UploadProcessor 消费新任务
        self.agent_busy.set()
        try:
            for record in records:
                if self.stop_event.is_set():
                    break

                # 快速门禁：错误类型已熔断 → 跳过，由汇总日志统一报告
                rec_error_type = record.get('error_type')
                if rec_error_type and rec_error_type in tripped_types:
                    skipped_trip_count += 1
                    et = rec_error_type or 'unknown'
                    if et not in skipped_by_trip:
                        skipped_by_trip[et] = []
                    skipped_by_trip[et].append(record.get('file_name', str(record.get('id'))))
                    continue
                if self.circuit_breaker.is_tripped(rec_error_type or 'unknown'):
                    skipped_trip_count += 1
                    et = rec_error_type or 'unknown'
                    if et not in skipped_by_trip:
                        skipped_by_trip[et] = []
                    skipped_by_trip[et].append(record.get('file_name', str(record.get('id'))))
                    continue

                try:
                    # tripped_types 传入可变集合：_process_one_record 在
                    # full_recovery 成功后从中移除已恢复的错误类型，
                    # 使批次中后续记录的门禁检查能反映最新状态
                    self._process_one_record(record, tripped_types)
                    processed += 1
                except Exception as e:
                    self._log(f"AutoRetryAgent: 处理记录 {record.get('id')} 异常 - {e}")
                    traceback.print_exc()
                    # 标记为 pending 避免卡住
                    try:
                        self.db.update_retry_status(record['id'], 'pending')
                    except Exception:
                        pass

            # ── 汇总日志 ──
            if skipped_trip_count > 0:
                parts = []
                for et, names in skipped_by_trip.items():
                    if len(names) <= 3:
                        parts.append(f"{et}({', '.join(names)})")
                    else:
                        parts.append(f"{et}({len(names)}条: {names[0]} 等)")
                self._log(f"AutoRetryAgent: 熔断跳过 {skipped_trip_count} 条 — "
                          + "; ".join(parts))
            if processed > 0:
                self._log(f"AutoRetryAgent: 本轮实际处理 {processed} 条记录")
        finally:
            self.agent_busy.clear()

    def _process_one_record(self, record: Dict, tripped_types: Set[str]):
        """
        AI Agent 主入口：ReAct 循环处理单条失败记录

        流程: 预检查 → ReAct 循环(LLM自主决策) → 后处理(退避+入队)
        安全守护(熔断/重试上限)在工具函数内部硬编码,LLM 无法绕过

        注意：调用方 _scan_and_retry 已在循环中做了熔断快速门禁检查，
        传入此方法的 record 理论上不应该是已熔断类型。此处保留
        _validate_react_decision 和 _rule_engine_decision 内部的二次门禁作为安全网。

        Args:
            record: 数据库记录字典
            tripped_types: 当前熔断的错误类型集合（可变引用）。
                           full_recovery 成功后会从中移除已恢复的错误类型，
                           使同一扫描批次中后续记录的门禁检查能反映最新状态。
        """
        record_id = record['id']
        file_path = record.get('file_path', '')
        file_name = record.get('file_name', '')
        retry_count = record.get('retry_count', 0)
        fail_stage = record.get('fail_stage')
        error_type = record.get('error_type')
        error_category = record.get('error_category')
        error_message = record.get('error_message', '')
        school = record.get('school', '')
        grade = record.get('grade', '')
        subject = record.get('subject', '')

        self._log(f"AutoRetryAgent: 开始处理记录#{record_id} - {file_name} "
                  f"(stage={fail_stage}, error={error_type}, retry_count={retry_count})")

        # ---- 预检查：文件是否存在（硬错误，LLM 无需参与）----
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

        # ---- 快速通道：LOGIN_EXPIRED + 浏览器不可用 → 直接恢复，跳过所有页面操作 ----
        # 核心原因：会话丢失后页面状态异常，capture_page_error/detect_page_state
        # 等 Selenium 操作可能卡住或返回无意义结果，不如直接重启浏览器。
        if (error_type == ErrorType.LOGIN_EXPIRED.value
                and (not self.browser.driver or not self.browser.is_logged_in)):
            self._log(f"AutoRetryAgent: 快速通道 - LOGIN_EXPIRED且浏览器不可用"
                      f"(driver={'有' if self.browser.driver else '无'},"
                      f" logged_in={self.browser.is_logged_in})，跳过页面诊断直接恢复")
            if self.circuit_breaker.is_browser_tripped():
                self._log(f"AutoRetryAgent: 快速通道 - 浏览器熔断，标记pending")
                self.db.update_retry_status(record_id, 'pending')
                return
            # 防重复入队
            with self._in_retry_lock:
                if file_path in self._in_retry:
                    self._log(f"AutoRetryAgent: 快速通道 - 文件已在重试队列中,跳过 - {file_name}")
                    self.db.update_retry_status(record_id, 'pending')
                    return
                self._in_retry.add(file_path)
            recovery_result = self._execute_recovery_pipeline(school=school)
            if recovery_result.get("success"):
                self._log(f"AutoRetryAgent: 快速通道 - 恢复成功"
                          f"(path={recovery_result.get('recovery_path')})，入队重试")
                self.circuit_breaker.reset_error(error_type)
                for related in ('login_expired', 'upload_submit_timeout',
                                'form_validate_fail', 'school_switch_fail'):
                    self.circuit_breaker.reset_error(related)
                self.circuit_breaker.reset_global_trip()
                if self.upload_processor is not None:
                    self.upload_processor._session_lost.clear()
                # 直接入队（复用现有的入队逻辑）
                self.db.update_retry_status(record_id, 'processing')
                self.db.increment_retry(record_id)
                if self.upload_processor is not None:
                    self.upload_processor.register_agent_retry(
                        file_path, record_id, RetryLevel.L3_ENV_RESET)
                if self.browser.driver:
                    self.browser.update_activity_time()
                self.task_queue.put(file_path)
                self._log(f"AutoRetryAgent: 快速通道完成 - {file_name} 已入队")
                return
            else:
                self._log(f"AutoRetryAgent: 快速通道 - 恢复失败"
                          f"(path={recovery_result.get('recovery_path')}, "
                          f"error={recovery_result.get('error', '')})，标记pending等待下次重试")
                self.db.update_retry_status(record_id, 'pending')
                return

        # ---- 预检查：交叉验证推断缺失的结构化字段 ----
        if not error_type or not fail_stage:
            inferred_category, inferred_type = classify_error(error_message, fail_stage)
            if not error_category:
                error_category = inferred_category.value
            if not error_type:
                error_type = inferred_type.value
            if not fail_stage:
                fail_stage = UploadStage.SUBMIT_UPLOAD.value
            self.db.update_record_structured_error(
                record_id,
                fail_stage=fail_stage,
                error_category=error_category,
                error_type=error_type,
            )
            # 回写 record 字典，确保 _rule_engine_decision 读到最新值
            record['fail_stage'] = fail_stage
            record['error_category'] = error_category
            record['error_type'] = error_type
            self._log(f"AutoRetryAgent: 交叉验证推断 - stage={fail_stage}, type={error_type}")

        # ---- 预捕获：先抓取页面当前错误信息，再决策 ----
        # 在 SUBMIT_UPLOAD / FORM_FILL 失败时，页面可能仍显示错误信息。
        # 将页面错误记录到局部变量供后续决策使用；数据库写入由 ReAct 循环中的
        # capture_page_error 工具统一负责，避免重复写入。
        if fail_stage in (UploadStage.SUBMIT_UPLOAD.value, UploadStage.FORM_FILL.value):
            if self.browser.driver:
                self._log(f"AutoRetryAgent: 预捕获 - 开始抓取页面错误信息...")
                try:
                    page_error = self.browser.capture_page_error()
                    self._log(f"AutoRetryAgent: 预捕获 - 完成, has_error={page_error.get('has_error')}, "
                              f"page_state={page_error.get('page_state')}")
                    if page_error.get("success") and page_error.get("has_error"):
                        combined = page_error.get("combined_text", "")
                        new_error_type = page_error.get("suggested_error_type") or error_type
                        is_permanent = page_error.get("is_permanent", False)
                        prefix = "[不可恢复] " if is_permanent else "[页面捕获] "

                        # 仅更新局部变量供后续决策，不写数据库（由 ReAct 工具统一写入）
                        error_type = new_error_type
                        error_message = prefix + combined
                        error_category = (
                            ErrorCategory.PLATFORM_BIZ_ERROR.value if is_permanent
                            else error_category
                        )
                        record['error_type'] = error_type
                        record['error_message'] = error_message
                        record['error_category'] = error_category

                        self._log(f"AutoRetryAgent: 预捕获页面错误 - "
                                  f"{'[不可恢复]' if is_permanent else '[可恢复]'} "
                                  f"{combined[:120]}")

                        # 永久性业务错误 → 直接终止，不浪费 ReAct 推理
                        # 此时仍需写数据库（ReAct 不会运行，无重复写入风险）
                        if is_permanent:
                            self.db.update_record_structured_error(
                                record_id,
                                error_message=(prefix + combined)[:1000],
                                fail_stage=fail_stage,
                                error_category=ErrorCategory.PLATFORM_BIZ_ERROR.value,
                                error_type=new_error_type,
                                error_context=json.dumps({
                                    "page_state": page_error.get("page_state"),
                                    "is_permanent": True,
                                    "error_count": len(page_error.get("errors", [])),
                                    "captured_at": "pre_decision",
                                }, ensure_ascii=False),
                            )
                            self._log(f"AutoRetryAgent: 页面显示不可恢复错误，"
                                      f" 直接标记人工处理 - {file_name}")
                            self.db.update_retry_status(record_id, 'finished')
                            return
                except Exception as e:
                    self._log(f"AutoRetryAgent: 预捕获页面错误异常(非致命) - {e}")

        # ---- 预检查：检测浏览器当前页面状态，防止页面已变化而Agent不知情 ----
        # 核心场景：上传填表时账号被挤下线 → 页面回退到登录页，
        # 原始错误记录的是 submit_upload 但实际页面已是 login，
        # Agent 必须先知道页面在登录页才能正确决策（full_recovery 而不是盲目重试）。
        page_context = {}
        if self.browser.driver:
            try:
                state_result = self.browser.detect_page_state()
                if state_result.get("success"):
                    current_state = state_result.get("state", "unknown")
                    current_url = ""
                    try:
                        current_url = self.browser.driver.current_url
                    except Exception:
                        pass

                    page_context = {
                        "current_page_state": current_state,
                        "current_url": current_url,
                        "state_details": state_result.get("details", ""),
                    }

                    # 判断页面状态与原始错误阶段是否不匹配
                    stage_to_expected_state = {
                        UploadStage.SUBMIT_UPLOAD.value: ("home", "upload_dialog"),
                        UploadStage.FORM_FILL.value: ("home", "upload_dialog"),
                        UploadStage.BROWSER_INIT.value: ("home", "login"),
                        UploadStage.SCHOOL_CHECK.value: ("home",),
                    }
                    expected_states = stage_to_expected_state.get(fail_stage, ())
                    state_mismatch = (
                        expected_states
                        and current_state not in expected_states
                        and current_state not in ("unknown", "busy", "no_browser")
                    )

                    if state_mismatch:
                        page_context["state_mismatch"] = True
                        page_context["mismatch_detail"] = (
                            f"⚠️ 页面状态已变化！原始错误发生在「{fail_stage}」阶段，"
                            f"预期页面应在 {expected_states}，"
                            f"但当前页面是「{current_state}」({current_url})。"
                            f"原始错误信息：「{error_message}」。"
                            f"请先通过 full_recovery 恢复到正常状态，不要盲目按原错误阶段重试。"
                        )
                        self._log(f"AutoRetryAgent: 页面状态不匹配 - "
                                  f"原始阶段={fail_stage}, 预期={expected_states}, "
                                  f"实际={current_state}")
                    else:
                        page_context["state_mismatch"] = False
                        self._log(f"AutoRetryAgent: 页面状态检测 - {current_state} "
                                  f"(原始阶段={fail_stage})")
                else:
                    self._log(f"AutoRetryAgent: 页面状态检测失败 - {state_result.get('details', '')}")
            except Exception as e:
                self._log(f"AutoRetryAgent: 页面状态检测异常(非致命) - {e}")

        # ---- 决策：AI ReAct 优先，规则引擎兜底 ----
        decision = None  # {"retry_level": RetryLevel, "action": "enqueue"|"skip"|"manual"}

        # 确定性短路：login_expired + 浏览器不可用 → 无需 LLM 分析，直接恢复+入队
        # 两种情况：浏览器进程已关闭(driver=None)，或浏览器进程存在但会话已失效(is_logged_in=False)
        browser_unavailable = (
            not self.browser.driver
            or not self.browser.is_logged_in
        )
        self._log(f"AutoRetryAgent: 决策前诊断 - error_type={error_type}, "
                  f"driver={'有' if self.browser.driver else '无'}, "
                  f"is_logged_in={self.browser.is_logged_in}, "
                  f"browser_unavailable={browser_unavailable}, "
                  f"browser_tripped={self.circuit_breaker.is_browser_tripped()}, "
                  f"ai_enabled={self.config.ai_retry_agent_enable}, "
                  f"has_api_key={bool(self.deepseek.api_key)}")
        if (error_type == ErrorType.LOGIN_EXPIRED.value
                and browser_unavailable
                and not self.circuit_breaker.is_browser_tripped()):
            self._log(f"AutoRetryAgent: 确定性短路 - login_expired+浏览器不可用"
                      f"(driver={'有' if self.browser.driver else '无'},"
                      f" logged_in={self.browser.is_logged_in})，直接执行恢复管线")
            recovery_result = self._execute_recovery_pipeline(school=school)
            if recovery_result.get("success"):
                decision = {"action": "enqueue",
                            "retry_level": RetryLevel.L3_ENV_RESET,
                            "_browser_restarted": recovery_result.get("browser_restarted", False),
                            "_full_recovery_succeeded": True,
                            "reason": "确定性恢复: 重启浏览器+登录+学校校验"}
            elif recovery_result.get("recovery_path") == "blocked_by_circuit_breaker":
                self._log(f"AutoRetryAgent: 确定性短路 - 浏览器熔断，跳过")
                return
            else:
                # 恢复管线失败（非熔断原因），标记 pending 等待下次扫描重试
                self._log(f"AutoRetryAgent: 确定性短路 - 恢复管线失败"
                          f"(path={recovery_result.get('recovery_path')}, "
                          f"error={recovery_result.get('error', '')})，"
                          f"标记pending等待下次重试")
                self.db.update_retry_status(record_id, 'pending')
                return

        if decision is None and self.config.ai_retry_agent_enable and self.deepseek.api_key:
            decision = self._run_react_loop(record, tripped_types, page_context)
            if decision is not None:
                # ReAct 决策需要通过硬安全门禁校验
                decision = self._validate_react_decision(decision, record, tripped_types)

        if decision is None:
            # 回退到规则引擎（传入页面上下文，使其能做智能升级决策）
            decision = self._rule_engine_decision(record, tripped_types, page_context)

        if decision is None:
            return  # 规则引擎决定跳过

        # ---- 统一后处理：full_recovery 成功的善后工作（ReAct 和规则引擎共享）----
        if decision.get("_full_recovery_succeeded"):
            self._log(f"AutoRetryAgent: full_recovery成功，"
                      f" 清除 {error_type} 及相关错误熔断")
            self.circuit_breaker.reset_error(error_type or 'unknown')
            # 也清除登录失效相关的熔断（恢复登录后这些都不再适用）
            for related in ('login_expired', 'upload_submit_timeout',
                            'form_validate_fail', 'school_switch_fail'):
                self.circuit_breaker.reset_error(related)
            self.circuit_breaker.reset_global_trip()
            # 同步更新 tripped_types（扫描批次的快照），否则门禁仍会拦截
            tripped_types.discard(error_type or '')
            for related in ('login_expired', 'upload_submit_timeout',
                            'form_validate_fail', 'school_switch_fail'):
                tripped_types.discard(related)
            # 通知 UploadProcessor 会话已恢复，可继续消费队列
            if self.upload_processor is not None:
                self.upload_processor._session_lost.clear()

        # 兜底：只要浏览器已恢复登录，就清除 _session_lost（防止 rule engine 路径漏清）
        if (self.upload_processor is not None
                and self.upload_processor._session_lost.is_set()
                and self.browser.driver
                and self.browser.is_logged_in):
            self._log("AutoRetryAgent: 检测到浏览器已恢复，清除 session_lost 标志")
            self.upload_processor._session_lost.clear()

        action = decision.get("action", "")
        retry_level = decision.get("retry_level")

        # ---- 执行决策 ----
        if action == "manual":
            self._log(f"AutoRetryAgent: 标记人工处理 - {file_name} ({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return

        if action == "skip":
            self._log(f"AutoRetryAgent: 跳过 - {file_name} ({decision.get('reason', '')})")
            return

        # ---- 后处理：退避等待 + 入队 ----
        if action == "enqueue":
            backoff_idx = min(retry_count, len(self.backoff_seconds) - 1)
            wait_seconds = self.backoff_seconds[backoff_idx]
            if retry_count > 0:
                self._log(f"AutoRetryAgent: 退避等待 {wait_seconds}s - {file_name} (第{retry_count+1}次重试)")
                self.stop_event.wait(wait_seconds)
                if self.stop_event.is_set():
                    return

            with self._in_retry_lock:
                if file_path in self._in_retry:
                    self._log(f"AutoRetryAgent: 文件已在重试队列中,跳过 - {file_name}")
                    return
                self._in_retry.add(file_path)

            self.db.update_retry_status(record_id, 'processing')
            self.db.increment_retry(record_id)

            self._log(f"AutoRetryAgent: 入队重试 [{retry_level.value if retry_level else 'L1'}] - {file_name}")
            if self.upload_processor is not None:
                self.upload_processor.register_agent_retry(file_path, record_id, retry_level or RetryLevel.L1_LIGHT_RETRY)
            # 刷新浏览器活跃时间，防止刚入队就被空闲超时关闭
            if self.browser.driver:
                self.browser.update_activity_time()
            self.task_queue.put(file_path)

            # 记录 error_type 映射，供 on_upload_result 失败时熔断统计
            if error_type:
                with self._retry_error_lock:
                    self._retry_error_types[file_path] = error_type

            # 记录错误供熔断器统计（仅在非 full_recovery 成功路径时；
            # full_recovery 成功后的入队已在上面 reset_error，此处跳过避免重复计数）
            if not decision.get("_full_recovery_succeeded") and error_type:
                self.circuit_breaker.record_error(error_type)

    # ─── 恢复管线 ───

    def _restart_browser(self) -> tuple:
        """
        浏览器完整重启（close → ensure_initialized），含熔断保护和计数管理。

        Returns:
            (True, None) — 重启成功，调用方设置 browser_restarted=True 并自行记录日志
            (False, error_dict) — 重启失败或被熔断拒绝，调用方应直接 return error_dict
        """
        if self.circuit_breaker.is_browser_tripped():
            return False, {
                "success": False, "school_verified": False,
                "browser_restarted": False,
                "error": "浏览器熔断保护，拒绝重启",
                "recovery_path": "restart_blocked"
            }
        self.circuit_breaker.record_browser_restart()
        self.browser.close()
        time.sleep(0.3)  # 极短等待，仅给 OS 释放资源的缓冲
        if not self.browser.ensure_initialized():
            return False, {
                "success": False, "school_verified": False,
                "browser_restarted": False,
                "error": "浏览器重启失败",
                "recovery_path": "restart_failed"
            }
        self.circuit_breaker.reset_browser_restart_count()
        return True, None

    def _execute_recovery_pipeline(self, school: str = None) -> dict:
        """
        确定性恢复管线：按 browser_automation.py 的标准流程将浏览器恢复到
        "首页 + 正确学校" 状态，供 ReAct 的 full_recovery 工具和规则引擎调用。

        流程完全遵循 browser_automation.py 的执行顺序：
        1. 检测页面状态 (detect_page_state)
        2. 按状态分支处理：
           - login → _login() → _handle_role_selection()
           - role_select → _handle_role_selection()
           - upload_dialog/school_dialog/error → reset_to_home()
           - unknown → recover_session()，失败则 close() + ensure_initialized()
           - home → 跳过恢复
        3. 验证学校 (check_and_switch_school)

        Args:
            school: 目标学校名称。如果提供，恢复后自动验证并切换学校。

        Returns:
            {"success": bool, "school_verified": bool, "browser_restarted": bool,
             "error": str, "recovery_path": str}
            - success: 恢复是否成功
            - school_verified: 学校是否匹配（仅 school 参数不为空时有效）
            - browser_restarted: 是否执行了浏览器重启（供调用方跳过重复熔断计数）
            - error: 错误描述
            - recovery_path: 实际执行的恢复路径描述
        """
        browser_restarted = False

        # ── Step 1: 确保浏览器存在 ──
        if not self.browser.driver:
            self._log("AutoRetryAgent: 恢复管线 - 浏览器未启动，执行 ensure_initialized")
            if self.circuit_breaker.is_browser_tripped():
                return {"success": False, "school_verified": False,
                        "browser_restarted": False,
                        "error": "浏览器熔断保护，拒绝初始化",
                        "recovery_path": "blocked_by_circuit_breaker"}
            if not self.browser.ensure_initialized():
                return {"success": False, "school_verified": False,
                        "browser_restarted": False,
                        "error": "浏览器初始化失败",
                        "recovery_path": "init_failed"}
            browser_restarted = True
            self.circuit_breaker.reset_browser_restart_count()
            # ensure_initialized → initialize → _login → _handle_role_selection
            # 跳过状态检测，直接验证学校（加重试，页面可能尚未完全渲染）
            if school:
                school_ok = False
                for attempt in range(1, 4):
                    wait = 2 + attempt  # 2s, 3s, 4s 递增等待
                    self._log(f"AutoRetryAgent: 恢复管线 - 第{attempt}次学校校验: {school} (等待{wait}s)")
                    time.sleep(wait)
                    if self.browser.check_and_switch_school(school):
                        school_ok = True
                        break
                    self._log(f"AutoRetryAgent: 恢复管线 - 第{attempt}次学校校验失败")
                if school_ok:
                    self._log(f"AutoRetryAgent: 恢复管线完成 - 初始化+学校验证通过: {school}")
                    return {"success": True, "school_verified": True,
                            "browser_restarted": True,
                            "error": "", "recovery_path": "fresh_init"}
                else:
                    # 学校校验失败但浏览器会话已成功恢复，
                    # 上传步骤中 UploadProcessor 会再次校验学校，不在此处阻断流程
                    self._log(f"AutoRetryAgent: 恢复管线 - 初始化成功但学校校验3次均失败: {school}"
                              f"（会话已恢复，学校将在上传时再校验）")
                    return {"success": True, "school_verified": False,
                            "browser_restarted": True,
                            "error": "", "recovery_path": "init_school_deferred"}
            return {"success": True, "school_verified": True,
                    "browser_restarted": True,
                    "error": "", "recovery_path": "fresh_init_no_school"}

        # ── Step 2: 检测页面状态（is_logged_in=False 时跳过，直接走重启流程）──
        # 核心优化：已知会话丢失时跳过 detect_page_state()，避免 Selenium 操作
        # 在异常页面上卡住。直接 close + ensure_initialized 重建整个浏览器会话。
        if not self.browser.is_logged_in:
            self._log("AutoRetryAgent: 恢复管线 - is_logged_in=False，跳过页面检测直接重启浏览器")
            ok, err = self._restart_browser()
            if not ok:
                return err
            browser_restarted = True
            # 重启后必须检测实际页面状态，不能假设为"home"。
            # 场景：登录成功但角色选择未完成/被踢下线 → 页面可能是 login 或 role_select
            ps = self.browser.detect_page_state()
            current = ps.get("state", "unknown") if ps.get("success") else "unknown"
            self._log(f"AutoRetryAgent: 恢复管线 - 浏览器重启后页面状态: {current}")
        else:
            page_state = self.browser.detect_page_state()
            current = page_state.get("state", "unknown") if page_state.get("success") else "unknown"
            self._log(f"AutoRetryAgent: 恢复管线 - 当前页面状态: {current}")

            # ── Step 2.5: 全局会话丢失检测（is_logged_in=True但页面可能有踢下线信号）──
            if current != "login" and self.browser._detect_session_lost_on_page():
                self._log(f"AutoRetryAgent: 恢复管线 - 页面({current})检测到踢下线信号，"
                          "执行完整重启...")
                ok, err = self._restart_browser()
                if not ok:
                    return err
                browser_restarted = True
                # 重启后检测实际页面状态
                ps = self.browser.detect_page_state()
                current = ps.get("state", "unknown") if ps.get("success") else "unknown"
                self._log(f"AutoRetryAgent: 恢复管线 - 浏览器重启后页面状态: {current}")

        # ── Step 3: 按状态分支恢复（while 循环，状态变更后 continue 重新分发）──
        # 设计要点：home 分支重启浏览器后页面可能变为 login/role_select，
        # 通过 continue 回到循环开头重新判断，避免内联重复登录/角色选择代码。

        _MAX_STATE_ITERATIONS = 5  # 防止死循环

        for _ in range(_MAX_STATE_ITERATIONS):
            if current == "login":
                self._log("AutoRetryAgent: 恢复管线 - 检测到登录页，执行登录...")
                try:
                    if not self.browser._login():
                        return {"success": False, "school_verified": False,
                                "browser_restarted": False,
                                "error": "登录失败", "recovery_path": "login_failed"}
                    # _login() 内部已调用 _handle_role_selection()，无需重复
                    self.browser.is_logged_in = True
                    self._log("AutoRetryAgent: 恢复管线 - 登录成功")
                    time.sleep(0.5)
                except Exception as e:
                    return {"success": False, "school_verified": False,
                            "browser_restarted": False,
                            "error": f"登录异常: {str(e)[:200]}",
                            "recovery_path": "login_exception"}
                break  # 登录完成，退出循环

            elif current == "role_select":
                self._log("AutoRetryAgent: 恢复管线 - 检测到角色选择页，执行角色选择...")
                try:
                    if not self.browser._handle_role_selection():
                        return {"success": False, "school_verified": False,
                                "browser_restarted": False,
                                "error": "角色选择失败", "recovery_path": "role_select_failed"}
                    self._log("AutoRetryAgent: 恢复管线 - 角色选择完成")
                    time.sleep(0.5)
                except Exception as e:
                    return {"success": False, "school_verified": False,
                            "browser_restarted": False,
                            "error": f"角色选择异常: {str(e)[:200]}",
                            "recovery_path": "role_select_exception"}
                break  # 角色选择完成，退出循环

            elif current in ("upload_dialog", "school_dialog", "error"):
                self._log(f"AutoRetryAgent: 恢复管线 - 检测到{current}，执行reset_to_home...")
                try:
                    reset_ok = self.browser.reset_to_home()
                except Exception as e:
                    self._log(f"AutoRetryAgent: 恢复管线 - reset_to_home异常: {e}")
                    return {"success": False, "school_verified": False,
                            "browser_restarted": browser_restarted,
                            "error": f"reset_to_home异常: {str(e)[:200]}",
                            "recovery_path": "reset_to_home_exception"}
                if not reset_ok:
                    self._log("AutoRetryAgent: 恢复管线 - reset_to_home失败(含浏览器重启)，"
                              "中止恢复，稍后重试")
                    return {"success": False, "school_verified": False,
                            "browser_restarted": browser_restarted,
                            "error": "reset_to_home失败，浏览器重启未成功",
                            "recovery_path": "reset_to_home_failed"}
                self._log("AutoRetryAgent: 恢复管线 - reset_to_home成功")
                time.sleep(0.5)
                break  # 弹窗已关闭，退出循环

            elif current == "home":
                # SPA 页面的 URL 不随会话丢失而变化（仍是 jobManager），
                # 需额外检查 is_logged_in 标志和页面文本中的踢下线信号
                need_restart = False
                restart_reason = ""
                if not self.browser.is_logged_in:
                    need_restart = True
                    restart_reason = "is_logged_in=False，会话已丢失"
                elif self.browser._detect_session_lost_on_page():
                    need_restart = True
                    restart_reason = "检测到踢下线信号"

                if need_restart:
                    self._log(f"AutoRetryAgent: 恢复管线 - 页面显示home但{restart_reason}，"
                              "执行完整重启...")
                    ok, err = self._restart_browser()
                    if not ok:
                        return err
                    browser_restarted = True
                    # 重启后检测实际页面状态（可能变为 login/role_select），
                    # 更新 current 并 continue 回到循环开头重新分发
                    ps = self.browser.detect_page_state()
                    current = ps.get("state", "unknown") if ps.get("success") else "unknown"
                    self._log(f"AutoRetryAgent: 恢复管线 - 浏览器重启后页面状态: {current}")
                    continue  # ← 回到循环开头，按新状态重新分发
                else:
                    self._log("AutoRetryAgent: 恢复管线 - 已在首页，跳过页面恢复")
                    break  # 无需恢复，退出循环

            else:  # unknown / no_browser
                self._log("AutoRetryAgent: 恢复管线 - 未知状态，先尝试轻量恢复...")
                try:
                    recover_result = self.browser.recover_session()
                    if recover_result.get("success"):
                        self._log(f"AutoRetryAgent: 恢复管线 - 轻量恢复成功 "
                                  f"(action={recover_result.get('action_taken')})")
                        time.sleep(0.5)
                        break
                    else:
                        # 轻量恢复失败 → 完整重启
                        self._log("AutoRetryAgent: 恢复管线 - 轻量恢复失败，执行完整重启...")
                        ok, err = self._restart_browser()
                        if not ok:
                            return err
                        browser_restarted = True
                        # 重启后检测实际页面状态，continue 重新分发
                        ps = self.browser.detect_page_state()
                        current = ps.get("state", "unknown") if ps.get("success") else "unknown"
                        self._log(f"AutoRetryAgent: 恢复管线 - 重启后页面状态: {current}")
                        continue
                except Exception as e:
                    return {"success": False, "school_verified": False,
                            "browser_restarted": False,
                            "error": f"恢复异常: {str(e)[:200]}",
                            "recovery_path": "recovery_exception"}
        else:
            # for 循环正常结束（未 break）→ 超过最大迭代次数
            self._log(f"AutoRetryAgent: 恢复管线 - 状态恢复超过{_MAX_STATE_ITERATIONS}次迭代,"
                      f" 中止(current={current})")
            return {"success": False, "school_verified": False,
                    "browser_restarted": browser_restarted,
                    "error": f"状态恢复迭代超限({_MAX_STATE_ITERATIONS}次), 最后状态={current}",
                    "recovery_path": "state_loop_exceeded"}

        # ── Step 4: 验证学校（加重试，会话已恢复但页面可能尚未就绪）──
        if school:
            self._log(f"AutoRetryAgent: 恢复管线 - 验证学校: {school}")
            school_ok = False
            for attempt in range(1, 4):
                wait = 1 + attempt  # 1s, 2s, 3s 递增等待
                if attempt > 1:
                    self._log(f"AutoRetryAgent: 恢复管线 - 学校校验重试 {attempt-1}/3...")
                time.sleep(wait)
                if self.browser.check_and_switch_school(school):
                    school_ok = True
                    break
                self._log(f"AutoRetryAgent: 恢复管线 - 第{attempt}次学校校验失败")
            if school_ok:
                self._log(f"AutoRetryAgent: 恢复管线完成 - 学校验证通过: {school}")
                return {"success": True, "school_verified": True,
                        "browser_restarted": browser_restarted,
                        "error": "", "recovery_path": "full_recovery_ok"}
            else:
                # 会话已恢复，学校校验在上传步骤中 UploadProcessor 会再做一次
                self._log(f"AutoRetryAgent: 恢复管线 - 学校校验3次均失败: {school}"
                          f"（会话已恢复，学校将在上传时再校验）")
                return {"success": True, "school_verified": False,
                        "browser_restarted": browser_restarted,
                        "error": "", "recovery_path": "school_deferred"}

        self._log("AutoRetryAgent: 恢复管线完成 - 无需学校验证")
        return {"success": True, "school_verified": True,
                "browser_restarted": browser_restarted,
                "error": "", "recovery_path": "recovery_no_school"}

    def _validate_react_decision(self, decision: Dict, record: Dict,
                                 tripped_types: Set[str]) -> Optional[Dict]:
        """
        对 ReAct LLM 返回的决策进行硬安全门禁校验，
        防止 LLM 绕过熔断/重试上限/L5 等保护。

        所有门禁与 _rule_engine_decision 保持一致。

        Returns:
            校验后的决策（可能被覆写），返回 None 表示跳过该记录
        """
        record_id = record['id']
        file_name = record.get('file_name', '')
        retry_count = record.get('retry_count', 0)
        fail_stage = record.get('fail_stage')
        error_type = record.get('error_type')
        action = decision.get("action", "")

        # 门禁 0：校验 action 合法性（防止 LLM 返回无效 action 导致记录卡 pending）
        VALID_ACTIONS = {"enqueue", "manual", "skip"}
        if action not in VALID_ACTIONS:
            self._log(f"AutoRetryAgent: 安全门禁-ReAct返回无效action '{action}'，"
                      f"标记人工处理 - {file_name}")
            self.db.update_retry_status(record_id, 'finished')
            return None

        # 获取该错误类型的自愈策略
        strategy_level, strategy_max_retries = get_strategy(fail_stage, error_type)

        # 门禁 1：全局最大重试次数
        if retry_count >= self.config.max_retry_count:
            self._log(f"AutoRetryAgent: 安全门禁-已达全局最大重试({self.config.max_retry_count}), "
                      f"标记人工处理 - {file_name}")
            self.db.update_retry_status(record_id, 'finished')
            return None

        # 门禁 2：每错误类型最大重试次数（L5 类型 max_retries=0，由门禁 4 处理）
        if retry_count >= strategy_max_retries and strategy_level != RetryLevel.L5_MANUAL:
            self._log(f"AutoRetryAgent: 安全门禁-已达该类型最大重试({strategy_max_retries}), "
                      f"标记人工处理 - {file_name}({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return None

        # 门禁 3：错误类型熔断
        if error_type and error_type in tripped_types:
            self._log(f"AutoRetryAgent: 安全门禁-{error_type}已熔断,跳过 - {file_name}")
            return None

        # 门禁 4：全量熔断（含 _global_tripped）
        if self.circuit_breaker.is_tripped(error_type or 'unknown'):
            self._log(f"AutoRetryAgent: 安全门禁-熔断保护,跳过 - {file_name}")
            return None

        # 门禁 5：L5 人工兜底 → 强制标记 finished
        if strategy_level == RetryLevel.L5_MANUAL:
            self._log(f"AutoRetryAgent: 安全门禁-策略为L5人工兜底,"
                      f" 强制标记finished - {file_name} ({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return None

        # 通过所有门禁
        return decision

    # ─── ReAct 循环 ───

    REACT_SYSTEM_PROMPT = """你是一个作业上传系统的故障诊断 AI Agent。你的职责是分析上传失败原因并决定恢复策略。

## 核心原则（必须严格遵守）

1. **先确认浏览器状态, 再决策** — 如果浏览器未启动(detect_page_state返回no_browser或capture_page_error返回page_state=no_browser)，必须先 full_recovery，禁止在无浏览器时反复诊断
2. **full_recovery 成功 → 直接 enqueue_retry(L3)** — 恢复成功就不要继续诊断，不要纠结 school_verified 字段，上传步骤会再次校验学校
3. **永久性业务错误 → mark_manual_review** — 权限不足、学校未开通、科目不匹配等错误重试无法修复
4. **full_recovery 失败 → mark_manual_review** — 不要反复尝试其他方案
5. **捕获到 is_permanent=True → 立即 mark_manual_review** — 禁止继续重试

## 可用工具

### 诊断工具（按使用优先级排列）

1. **capture_page_error**:
   全面抓取网页当前显示的所有错误信息（toast/通知/表单校验/对话框），
   自动判断是否永久性业务错误，并记录到数据库。
   无参数。返回: has_error, errors列表, is_permanent, suggested_error_type, page_state等
   注意: page_state=no_browser 时说明浏览器未启动，has_error必然为False，此时不要再分析错误，直接 full_recovery

2. **detect_page_state**:
   检测浏览器当前页面状态。无参数。
   返回state: login/role_select/home/error/upload_dialog/school_dialog/unknown/no_browser

3. **check_current_school**:
   检查当前登录学校是否与目标学校一致。无参数

4. **query_error_history**:
   查询同类错误近期发生频率和熔断状态。参数: error_type=错误类型(可选)

5. **check_circuit_breaker**:
   查看熔断保护状态和浏览器重启次数。无参数

6. **check_file_exists**:
   检查文件是否还在磁盘上。参数: file_path=文件路径(可选)

### 操作工具

7. **full_recovery**:
   执行完整的浏览器恢复流程（启动浏览器→登录→角色选择→关闭弹窗→验证学校）。
   一次性完成所有恢复操作。无参数
   ⚠️ 只要返回 success=True，就说明浏览器会话已恢复，应立即 enqueue_retry(L3)

8. **enqueue_retry**:
   将文件加入重试队列。参数: retry_level=L1|L2|L3

9. **mark_manual_review**:
   标记为待人工处理。参数: reason=原因

10. **skip_and_wait**:
    暂时跳过等待下次扫描。参数: reason=原因

## 自愈策略级别

- L1（轻量重试）：页面干净、错误为偶发网络/超时，直接重试上传
- L2（页面复位）：表单校验失败或弹窗残留，full_recovery关闭弹窗后重试
- L3（环境重置）：登录失效/角色过期/浏览器被关闭，full_recovery完整恢复后重试

## 强制决策流程

### 流程0: 浏览器未启动（capture_page_error返回page_state=no_browser，或 detect_page_state返回state=no_browser）

```
这是最常见也最简单的场景 — 只需执行恢复，无需诊断。

Step 1 [必须]: full_recovery
Step 2: 如果 full_recovery 返回 success=True:
          → enqueue_retry(L3)（不要调用任何诊断工具！）
          → Final: {"action": "enqueue", "retry_level": "L3", "reason": "浏览器已恢复"}
        如果 full_recovery 返回 success=False:
          → mark_manual_review(reason=full_recovery的error字段)

禁止行为:
  - 不要在浏览器未启动时反复调用 capture_page_error
  - 不要在 full_recovery 成功后继续调用任何诊断工具
  - 不要纠结 school_verified 字段 — 上传步骤会再次校验
```

### 流程A: 浏览器正常但 SUBMIT_UPLOAD / FORM_FILL 失败

```
Step 0 [优先]: 如果任务描述中包含「页面状态不匹配」或「当前页面是 login」的提示
  → 页面已发生回退/跳转，原始错误信息可能已不可见
  → 直接: full_recovery → enqueue_retry(L3)
  → 不要浪费时间在登录页面上尝试 capture_page_error！

Step 1: capture_page_error
  → 如果 is_permanent=True:
      → mark_manual_review → Final
  → 如果 has_error=True 且 is_permanent=False:
      → 错误类型是 login_expired/会话失效 → full_recovery → enqueue_retry(L3)
      → 表单校验失败 → full_recovery → enqueue_retry(L2)
      → 网络/超时 → enqueue_retry(L1)
  → 如果 has_error=False:
      → 检查 page_state:
        - login/role_select → full_recovery → enqueue_retry(L3)
        - home → enqueue_retry(L1)
        - upload_dialog → full_recovery → enqueue_retry(L2)
        - error/unknown → full_recovery → 失败则 mark_manual_review

⚠️ 关键规则: full_recovery 成功后立即 enqueue_retry，不要继续诊断！
```

### 流程B: BROWSER_INIT / SCHOOL_CHECK 失败

```
Step 1: detect_page_state（浏览器未启动时直接跳到Step 2）
Step 2: full_recovery
Step 3: 成功 → enqueue_retry(L3) | 失败 → mark_manual_review
```

### 流程C: AI_CLASSIFY 失败

```
Step 1: query_error_history
Step 2: API超时/限流 → enqueue_retry(L1) | API密钥无效 → mark_manual_review | 科目识别为空 → enqueue_retry(L1)
```

## 输出格式

Thought: [你的分析推理]
Action: tool_name(key1=value1, key2=value2)

任务完成时：
Thought: [总结]
Final: {"action": "enqueue"|"manual"|"skip", "retry_level": "L1"|"L2"|"L3", "reason": "..."}

注意：
- Final 中的 action 只能是 enqueue、manual、skip 之一
- 如果 capture_page_error 返回 is_permanent=True，必须用 mark_manual_review 并输出 Final action=manual
- 绝对不要在没调用 capture_page_error 的情况下就直接 enqueue_retry"""

    def _run_react_loop(self, record: Dict, tripped_types: set,
                        page_context: Dict = None) -> Optional[Dict]:
        """
        启动 ReAct 循环让 LLM 自主诊断和决策。
        LLM 只负责诊断和决策，不直接操作浏览器页面。
        所有页面恢复操作由 full_recovery 工具统一执行。

        Args:
            record: 错误记录字典
            tripped_types: 当前熔断类型集合
            page_context: 当前页面状态预检测结果（含 state_mismatch 等）

        Returns:
            {"action": str, "retry_level": RetryLevel, "reason": str} 或 None(失败)
        """
        # 提取记录字段，构建闭包捕获的变量
        record_id = record['id']
        file_path = record.get('file_path', '')
        file_name = record.get('file_name', '')
        error_type = record.get('error_type', '')
        error_message = record.get('error_message', '')
        retry_count = record.get('retry_count', 0)
        school = record.get('school', '')
        grade = record.get('grade', '')
        subject = record.get('subject', '')

        # 浏览器是否已由 full_recovery 内部重启
        _browser_restarted = [False]
        # full_recovery 是否执行成功（用于决定是否重置熔断）
        _full_recovery_succeeded = [False]

        # ── 诊断工具 ──

        def tool_check_file_exists(file_path_arg=None):
            path = file_path_arg or file_path
            exists = os.path.exists(path)
            return {"exists": exists, "path": path}

        def tool_query_error_history(error_type_arg=None):
            et = error_type_arg or error_type
            recent = self.circuit_breaker.error_counts.get(et, 0)
            tripped = self.circuit_breaker.is_tripped(et)
            return {"error_type": et, "recent_count": recent, "is_tripped": tripped}

        def tool_check_circuit_breaker():
            return {
                "tripped_types": list(self.circuit_breaker.get_tripped_types()),
                "browser_tripped": self.circuit_breaker.is_browser_tripped(),
                "global_tripped": self.circuit_breaker.global_tripped,
                "browser_restart_count": self.circuit_breaker.browser_restart_count,
            }

        def _browser_busy():
            """检查 UploadProcessor 是否正在使用浏览器，防止并发操作。
            但如果 UploadProcessor 已检测到会话丢失（_session_lost 已置位），
            说明它已暂停上传等待恢复，Agent 可以安全接管浏览器。"""
            if self.upload_processor is None:
                return False
            # 会话丢失时 UploadProcessor 已暂停，Agent 可接管浏览器恢复
            if self.upload_processor._session_lost.is_set():
                return False
            return self.upload_processor.processing

        def tool_capture_page_error():
            """
            【必须第一步调用】全面抓取网页当前显示的所有错误信息，
            自动判断是否永久性业务错误，并将错误详情记录到数据库。
            这是决策流程的起点，禁止跳过此工具直接 enqueue_retry。
            注意：只读操作，不阻塞于 UploadProcessor 处理状态。
            """
            if not self.browser.driver:
                return {"success": False, "has_error": False,
                        "errors": [], "combined_text": "浏览器未启动",
                        "is_permanent": False, "page_state": "no_browser"}

            result = self.browser.capture_page_error()

            # 如果抓到了页面错误，立即更新数据库记录
            if result.get("has_error") and result.get("combined_text"):
                # 根据抓取结果更新结构化错误字段
                new_error_type = result.get("suggested_error_type") or error_type
                new_error_msg = (
                    f"[页面捕获] {result.get('combined_text', '')}"
                )
                if result.get("is_permanent"):
                    new_error_msg = f"[不可恢复] {new_error_msg}"

                try:
                    self.db.update_record_structured_error(
                        record_id,
                        error_message=new_error_msg[:1000],
                        fail_stage=fail_stage or UploadStage.SUBMIT_UPLOAD.value,
                        error_category=(
                            ErrorCategory.PLATFORM_BIZ_ERROR.value if result.get("is_permanent")
                            else ErrorCategory.BROWSER_ERROR.value
                        ),
                        error_type=new_error_type,
                        error_context=json.dumps({
                            "page_state": result.get("page_state"),
                            "is_permanent": result.get("is_permanent"),
                            "error_count": len(result.get("errors", [])),
                        }, ensure_ascii=False),
                    )
                except Exception:
                    pass  # 记录失败不阻塞诊断流程

            return result

        def tool_detect_page_state():
            """检测浏览器当前页面状态（登录页/角色选择/首页/错误页等）。只读操作，不阻塞。"""
            if not self.browser.driver:
                return {"success": False, "state": "no_browser",
                        "details": "浏览器未启动"}
            return self.browser.detect_page_state()

        def tool_check_current_school():
            """读取浏览器当前登录的学校名称，用于验证学校是否匹配目标。只读操作，不阻塞。"""
            if not self.browser.is_initialized:
                return {"success": False, "error": "浏览器未初始化"}
            result = self.browser.get_current_school()
            if result.get("success"):
                current = result.get("school", "")
                return {
                    "success": True,
                    "current_school": current,
                    "target_school": school,
                    "match": (current == school),
                    "message": f"当前学校: {current}, 目标学校: {school}"
                }
            return {"success": False, "error": result.get("error", "读取学校失败")}

        # ── 操作工具 ──

        def tool_full_recovery():
            """
            执行完整的浏览器恢复流程，按 browser_automation.py 的标准顺序：
            检测页面状态 → 登录/角色选择/弹窗关闭 → 验证学校。
            一次性完成所有恢复操作，LLM 无需分步操作页面。
            """
            if _browser_busy():
                return {"success": False, "error": "UploadProcessor 正在上传中，浏览器被占用"}
            result = self._execute_recovery_pipeline(school=school)
            if result.get("browser_restarted"):
                _browser_restarted[0] = True
            if result.get("success"):
                _full_recovery_succeeded[0] = True
            return result

        def tool_enqueue_retry(retry_level="L1"):
            # 安全守护：检查重试上限和熔断（软校验，硬门禁在 _validate_react_decision 中）
            if retry_count >= self.config.max_retry_count:
                return {"success": False, "error": f"已达全局最大重试次数({self.config.max_retry_count})，应转人工处理"}
            if error_type and error_type in tripped_types:
                return {"success": False, "error": f"错误类型 {error_type} 已熔断，应等待或转人工处理"}
            with self._in_retry_lock:
                if file_path in self._in_retry:
                    return {"success": False, "error": "文件已在重试队列中"}
            return {"success": True, "message": f"准备以 {retry_level} 级别加入重试队列", "retry_level": retry_level}

        def tool_mark_manual_review(reason=""):
            self.db.update_retry_status(record_id, 'finished')
            return {"success": True, "message": f"已标记人工处理: {reason}", "reason": reason}

        def tool_skip_and_wait(reason=""):
            return {"success": True, "message": f"已跳过: {reason}", "reason": reason}

        tools = {
            "capture_page_error": tool_capture_page_error,
            "detect_page_state": tool_detect_page_state,
            "check_current_school": tool_check_current_school,
            "query_error_history": tool_query_error_history,
            "check_circuit_breaker": tool_check_circuit_breaker,
            "check_file_exists": tool_check_file_exists,
            "full_recovery": tool_full_recovery,
            "enqueue_retry": tool_enqueue_retry,
            "mark_manual_review": tool_mark_manual_review,
            "skip_and_wait": tool_skip_and_wait,
        }

        tool_descs = {
            "capture_page_error": "【必须第一步调用】全面抓取网页当前显示的所有错误信息(toast/通知/表单校验/对话框)，自动判断是否永久性业务错误，并记录到数据库。禁止跳过此工具直接入队重试。无参数。返回has_error/is_permanent/suggested_error_type/page_state等",
            "detect_page_state": "检测浏览器当前页面状态，返回state(login/role_select/home/error/dialog/unknown)和详情。无参数",
            "check_current_school": "读取浏览器当前学校名称，返回当前学校、目标学校和匹配结果。无参数",
            "query_error_history": "查询同类错误近期发生频率和熔断状态。参数: error_type=错误类型(可选)",
            "check_circuit_breaker": "查看全局熔断/浏览器熔断/全量熔断状态列表。无参数",
            "check_file_exists": "检查文件是否还在磁盘上。参数: file_path=文件路径(可选)",
            "full_recovery": "执行完整的浏览器恢复流程。自动检测页面状态并按browser_automation.py标准流程恢复(登录→角色选择→学校切换)。一次性完成所有操作，无需分步干预。无参数",
            "enqueue_retry": "将文件加入重试队列。仅在capture_page_error确认无永久错误后才能调用。参数: retry_level=L1|L2|L3",
            "mark_manual_review": "标记为待人工处理。参数: reason=原因",
            "skip_and_wait": "暂时跳过该记录。参数: reason=原因",
        }

        # 构建任务描述
        task = f"""处理一条上传失败记录：

- 文件名: {file_name}
- 文件路径: {file_path}
- 学校: {school}
- 年级: {grade}
- 科目: {subject}
- 失败阶段: {record.get('fail_stage', '未知')}
- 错误类型: {error_type or '未知'}
- 错误分类: {record.get('error_category', '未知')}
- 错误信息: {error_message}
- 当前重试次数: {retry_count}
- 全局最大重试: {self.config.max_retry_count}"""

        # 注入页面状态预检测结果（如果有）
        if page_context:
            current_state = page_context.get("current_page_state", "unknown")
            current_url = page_context.get("current_url", "")
            task += f"""
- 当前浏览器页面状态: {current_state}
- 当前页面URL: {current_url}"""

            if page_context.get("state_mismatch"):
                task += f"""
⚠️ 重要提示 - 页面状态不匹配:
{page_context.get('mismatch_detail', '')}
这意味着原始错误发生时页面状态和现在不同。请根据当前实际页面状态来决策，不要盲目按原始错误类型处理。
例如：如果当前页面是登录页(login)，说明会话已失效，必须先执行 full_recovery 重新登录，而不是尝试重试上传。"""

        # 浏览器未启动时，注入明确提示（比 page_context 为空更紧急的信号）
        if not page_context and not self.browser.driver:
            task += """
- ⚠️ 当前浏览器状态: 未启动（浏览器已关闭）
- 原始上传时的页面错误信息已不可见，无需尝试 capture_page_error 诊断
- 正确流程: full_recovery → 成功后直接 enqueue_retry(L3)
- 禁止在浏览器未启动时反复调用 capture_page_error 或 detect_page_state！"""

        task += "\n\n请先确认浏览器状态，然后做出恢复决策。"

        max_steps = self.config.get("AI_AGENT_MAX_STEPS", 10)
        agent = ReActLoop(
            llm=self.deepseek,
            system_prompt=self.REACT_SYSTEM_PROMPT,
            tools=tools,
            tool_descriptions=tool_descs,
            max_steps=max_steps,
            log_fn=lambda msg: self._log(f"AutoRetryAgent ReAct: {msg}")
        )

        result = agent.run(task)
        if result["success"] and result["result"]:
            decision = result["result"]
            action = decision.get("action", "skip")
            level_str = decision.get("retry_level", "L1")
            try:
                retry_level = RetryLevel(level_str) if level_str else RetryLevel.L1_LIGHT_RETRY
            except ValueError:
                retry_level = RetryLevel.L1_LIGHT_RETRY

            self._log(f"AutoRetryAgent: ReAct决策 [{action}] level={level_str} "
                      f"(steps={result['steps']}, reason={decision.get('reason', '')})")
            return {
                "action": action,
                "retry_level": retry_level,
                "reason": decision.get("reason", ""),
                "_browser_restarted": _browser_restarted[0],
                "_full_recovery_succeeded": _full_recovery_succeeded[0],
            }
        else:
            self._log(f"AutoRetryAgent: ReAct 失败(steps={result['steps']}), 回退规则引擎")
            return None

    # ─── 规则引擎兜底 ───

    def _rule_engine_decision(self, record: Dict, tripped_types: Set[str],
                               page_context: Dict = None) -> Optional[Dict]:
        """
        传统规则引擎决策（AI 禁用或失败时的兜底）
        保留原有的 get_strategy() + 全部安全检查逻辑

        2.0 增强：接收页面状态上下文，对 L2/L3 策略做智能升级：
        - 页面在登录页 → 升级为 L4（完整恢复），避免盲目重试必然失败
        - 页面状态与失败阶段不匹配 → 升级为 L4
        """
        record_id = record['id']
        file_name = record.get('file_name', '')
        retry_count = record.get('retry_count', 0)
        fail_stage = record.get('fail_stage')
        error_type = record.get('error_type')

        retry_level, max_retries = get_strategy(fail_stage, error_type)

        # 检查重试次数上限
        if retry_count >= self.config.max_retry_count:
            self._log(f"AutoRetryAgent: 已达全局最大重试次数,标记人工处理 - {file_name}")
            self.db.update_retry_status(record_id, 'finished')
            return None

        if retry_count >= max_retries and retry_level != RetryLevel.L5_MANUAL:
            self._log(f"AutoRetryAgent: 已达该错误类型最大重试次数,标记人工处理 - {file_name}({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return None

        # 熔断检查
        if error_type and error_type in tripped_types:
            self._log(f"AutoRetryAgent: {error_type} 已熔断,跳过 - {file_name}")
            return None

        if self.circuit_breaker.is_tripped(error_type or 'unknown'):
            self._log(f"AutoRetryAgent: {error_type} 触发熔断,跳过 - {file_name}")
            return None

        # ── 2.0 智能升级：页面状态感知 ──
        # 当页面上下文显示状态不匹配或已在登录页时，
        # 将 L1/L2/L3 升级为 L4（完整恢复），避免在错误页面上盲目重试
        if page_context and retry_level in (RetryLevel.L1_LIGHT_RETRY,
                                             RetryLevel.L2_PAGE_RESET,
                                             RetryLevel.L3_ENV_RESET):
            current_state = page_context.get("current_page_state", "")
            if (page_context.get("state_mismatch")
                    or current_state == "login"
                    or current_state == "role_select"):
                self._log(f"AutoRetryAgent: 规则引擎-页面状态不匹配"
                          f"(state={current_state}, mismatch={page_context.get('state_mismatch')}), "
                          f"策略从{retry_level.value}升级为L4 - {file_name}")
                retry_level = RetryLevel.L4_SERVICE_RESTART

        # 决策映射
        if retry_level == RetryLevel.L5_MANUAL:
            self._log(f"AutoRetryAgent: L5规则引擎决策,标记finished - {file_name}")
            self.db.update_retry_status(record_id, 'finished')
            return None

        if retry_level == RetryLevel.L4_SERVICE_RESTART:
            if self.circuit_breaker.is_browser_tripped():
                self._log(f"AutoRetryAgent: 浏览器熔断,标记finished - {file_name}")
                self.db.update_retry_status(record_id, 'finished')
                return None
            if self.upload_processor is not None and self.upload_processor.processing:
                # 会话丢失时 UploadProcessor 已暂停（_on_session_lost 会立即释放 processing），
                # 此时不应延迟，Agent 应接管浏览器执行恢复
                if not self.upload_processor._session_lost.is_set():
                    self._log(f"AutoRetryAgent: UploadProcessor 处理中,延迟 - {file_name}")
                    return None

            # 通过恢复管线执行浏览器恢复
            school = record.get('school', '')
            self._log(f"AutoRetryAgent: 规则引擎L4 - 执行恢复管线,目标学校: {school}")
            recovery_result = self._execute_recovery_pipeline(school=school)
            if recovery_result["success"]:
                self._log(f"AutoRetryAgent: 规则引擎L4 - 恢复管线成功,入队重试"
                          f"(school_verified={recovery_result.get('school_verified', False)})")
                # 恢复成功 → 清除相关错误熔断，避免误杀后续入队
                self.circuit_breaker.reset_error(error_type or 'unknown')
                for related in ('login_expired', 'upload_submit_timeout',
                                'form_validate_fail', 'school_switch_fail'):
                    self.circuit_breaker.reset_error(related)
                self.circuit_breaker.reset_global_trip()
                return {"action": "enqueue", "retry_level": retry_level,
                        "_browser_restarted": recovery_result.get("browser_restarted", False),
                        "_full_recovery_succeeded": True}
            else:
                self._log(f"AutoRetryAgent: 规则引擎L4 - 恢复管线失败: "
                          f"{recovery_result.get('error', '')}, 标记finished")
                self.db.update_record_structured_error(
                    record_id,
                    error_message=recovery_result.get("error", "恢复管线失败"),
                    fail_stage=UploadStage.SCHOOL_CHECK.value,
                    error_category=ErrorCategory.PLATFORM_BIZ_ERROR.value,
                    error_type=ErrorType.SCHOOL_NOT_ACTIVATED.value,
                )
                self.db.update_retry_status(record_id, 'finished')
                return None

        # L1/L2/L3（未被升级的）
        # 注意：L2/L3 的浏览器复位由 UploadProcessor._preprocess_agent_retry()
        # 在消费队列时执行，此处不重复操作以免阻塞 Agent 扫描循环。
        self._log(f"AutoRetryAgent: 规则引擎决策 [{retry_level.value}] - {file_name}")
        return {"action": "enqueue", "retry_level": retry_level}

    # ─── 上传结果回调 ───

    def on_any_upload_success(self):
        """
        由 UploadProcessor 在任意文件上传成功后调用（不限于Agent重试）。
        成功上传证明浏览器/网络环境正常，重置临时性错误的熔断器，
        避免之前级联失败触发的熔断继续拦截可恢复的待重试文件。
        同时清除 _session_lost 标志（成功上传证明会话正常）。
        """
        for et in self._TRANSIENT_ERROR_TYPES:
            self.circuit_breaker.reset_error(et)
        self.circuit_breaker.reset_global_trip()
        self._log("AutoRetryAgent: 上传成功，已重置临时性错误熔断（环境已恢复正常）")
        # 成功上传证明会话正常，清除 session_lost 以避免阻塞后续任务
        if self.upload_processor is not None:
            self.upload_processor._session_lost.clear()

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
        with self._in_retry_lock:
            self._in_retry.discard(file_path)

        # 获取并清理 error_type 映射
        with self._retry_error_lock:
            retry_error_type = self._retry_error_types.pop(file_path, None)

        # 更新数据库
        self.db.set_agent_retry_success(record_id, success)
        if success:
            self.db.mark_record_success(record_id)
            self.db.update_retry_status(record_id, 'finished')
            self._log(f"AutoRetryAgent: ✓ 自动重试成功 - {os.path.basename(file_path)}")
        else:
            self.db.update_retry_status(record_id, 'pending')
            self._log(f"AutoRetryAgent: ✗ 自动重试失败 - {os.path.basename(file_path)}")
            # 重试失败时记录错误供熔断器统计（精确追踪真实失败，
            # 替代入队时的预判计数，避免熔断器过早触发）
            if retry_error_type:
                self.circuit_breaker.record_error(retry_error_type)

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
