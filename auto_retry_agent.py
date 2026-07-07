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

        # Agent 忙碌标志：Agent 正在执行恢复操作时阻塞 UploadProcessor 消费新任务
        self.agent_busy = threading.Event()

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

        # 清理 _in_retry 中已不在待处理列表中的过期条目
        # （UploadProcessor 早期返回时可能未触发 on_upload_result 回调）
        pending_paths = {r.get('file_path', '') for r in records if r.get('file_path')}
        with self._in_retry_lock:
            self._in_retry = {fp for fp in self._in_retry if fp in pending_paths}

        tripped_types = self.circuit_breaker.get_tripped_types()
        self._log(f"AutoRetryAgent: 扫描到 {len(records)} 条待处理失败记录"
                  + (f", 熔断中: {tripped_types}" if tripped_types else ""))

        # 标记 Agent 忙碌，阻塞 UploadProcessor 消费新任务
        self.agent_busy.set()
        try:
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
        finally:
            self.agent_busy.clear()

    def _process_one_record(self, record: Dict, tripped_types: Set[str]):
        """
        AI Agent 主入口：ReAct 循环处理单条失败记录

        流程: 预检查 → ReAct 循环(LLM自主决策) → 后处理(退避+入队)
        安全守护(熔断/重试上限)在工具函数内部硬编码,LLM 无法绕过

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
        school = record.get('school', '')
        grade = record.get('grade', '')
        subject = record.get('subject', '')

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

        # ---- 决策：AI ReAct 优先，规则引擎兜底 ----
        decision = None  # {"retry_level": RetryLevel, "action": "enqueue"|"restart_browser"|"skip"|"manual"}

        if self.config.ai_retry_agent_enable and self.deepseek.api_key:
            decision = self._run_react_loop(record, tripped_types)
            if decision is not None:
                # ReAct 决策需要通过硬安全门禁校验
                decision = self._validate_react_decision(decision, record, tripped_types)

        if decision is None:
            # 回退到规则引擎
            decision = self._rule_engine_decision(record, tripped_types)

        if decision is None:
            return  # 规则引擎决定跳过

        action = decision.get("action", "")
        retry_level = decision.get("retry_level")

        # ---- 执行决策 ----
        if action == "manual":
            self._log(f"AutoRetryAgent: 标记人工处理 - {file_name} ({error_type})")
            self.db.update_retry_status(record_id, 'finished')
            return

        # 浏览器重启：ReAct 路径中 LLM 可能已通过 restart_browser 工具执行过，
        # 此时 _browser_restarted 标记为 True，跳过重复重启
        browser_restarted_in_react = decision.get("_browser_restarted", False)
        if action == "restart_browser" and not browser_restarted_in_react:
            if self.circuit_breaker.is_browser_tripped():
                self._log(f"AutoRetryAgent: 浏览器熔断,标记finished - {file_name}")
                self.db.update_retry_status(record_id, 'finished')
                return
            if self.upload_processor is not None and self.upload_processor.processing:
                self._log(f"AutoRetryAgent: UploadProcessor 处理中,延迟 - {file_name}")
                return

            self._log(f"AutoRetryAgent: 重启浏览器 - {file_name}")
            restart_result = self._do_restart_browser(school=school)
            if restart_result["success"]:
                self._log("AutoRetryAgent: 浏览器重启成功")
                if not restart_result.get("school_verified", True):
                    # 学校验证失败 → 标记人工处理，避免上传到错误学校
                    self._log(f"AutoRetryAgent: 重启后学校验证失败，标记人工处理 - {file_name}")
                    self.db.update_record_structured_error(
                        record_id,
                        error_message=restart_result.get("error", "重启后学校验证失败"),
                        fail_stage=UploadStage.SCHOOL_CHECK.value,
                        error_category=ErrorCategory.PLATFORM_BIZ_ERROR.value,
                        error_type=ErrorType.SCHOOL_NOT_ACTIVATED.value,
                    )
                    self.db.update_retry_status(record_id, 'finished')
                    return
            else:
                self._log("AutoRetryAgent: 浏览器重启失败，稍后重试")
                self.db.update_retry_status(record_id, 'pending')
                return

        if action == "skip":
            self._log(f"AutoRetryAgent: 跳过 - {file_name} ({decision.get('reason', '')})")
            return

        # ---- 后处理：退避等待 + 入队 ----
        if action in ("enqueue", "restart_browser"):
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

            if error_type:
                self.circuit_breaker.record_error(error_type)

    # ─── 共享工具方法 ───

    def _do_restart_browser(self, school: str = None) -> dict:
        """
        智能浏览器恢复流程：
        1. 先尝试轻量 recover_session()（登录页→重新登录，角色选择→选角色，弹窗→关闭）
        2. 轻量恢复失败才执行完整浏览器重启（close → ensure_initialized → login）
        3. 如果提供了学校参数，恢复后验证当前学校是否匹配并自动切换。

        供 tool_restart_browser 和 _process_one_record 共用。

        Args:
            school: 目标学校名称。如果提供，恢复后自动验证并切换学校。

        Returns:
            {"success": bool, "school_verified": bool, "school": str, "error": str,
             "recovery_method": str}
            - success: 浏览器恢复是否成功
            - school_verified: 学校是否匹配（仅 school 参数不为空时有效）
            - school: 当前页面学校名称
            - error: 错误描述
            - recovery_method: "lightweight" | "full_restart" | "none"
        """
        # ── 策略1: 轻量恢复（不重启浏览器）──
        if self.browser.driver:
            self._log("AutoRetryAgent: 尝试轻量会话恢复...")
            try:
                recover_result = self.browser.recover_session()
                if recover_result.get("success"):
                    self._log(f"AutoRetryAgent: 轻量恢复成功 "
                              f"(action={recover_result.get('action_taken')})")

                    # 恢复后验证学校
                    if school:
                        time.sleep(2)
                        school_info = self.browser.get_current_school()
                        if school_info.get("success"):
                            current = school_info.get("school", "")
                            if current == school:
                                self._log(f"[OK] 轻量恢复后学校验证通过: {current}")
                                return {"success": True, "school_verified": True,
                                        "school": current, "error": "",
                                        "recovery_method": "lightweight"}
                            else:
                                self._log(f"[WARN] 轻量恢复后学校不匹配: "
                                          f"目标={school}, 当前={current}，尝试切换...")
                                if self.browser.check_and_switch_school(school):
                                    self._log(f"[OK] 已切换到目标学校: {school}")
                                    return {"success": True, "school_verified": True,
                                            "school": school, "error": "",
                                            "recovery_method": "lightweight"}
                                else:
                                    self._log("[WARN] 轻量恢复后学校切换失败，回退完整重启...")
                        else:
                            self._log("[WARN] 轻量恢复后无法读取学校，回退完整重启...")
                    else:
                        return {"success": True, "school_verified": True,
                                "school": "", "error": "",
                                "recovery_method": "lightweight"}
                else:
                    self._log(f"AutoRetryAgent: 轻量恢复失败 "
                              f"({recover_result.get('error', '')})，回退完整重启...")
            except Exception as e:
                self._log(f"AutoRetryAgent: 轻量恢复异常 ({e})，回退完整重启...")

        # ── 策略2: 完整浏览器重启 ──
        self._log("AutoRetryAgent: 执行完整浏览器重启...")
        self.circuit_breaker.record_browser_restart()
        if self.browser.driver:
            self.browser.close()
            time.sleep(2)
        if self.browser.ensure_initialized():
            self.circuit_breaker.reset_browser_restart_count()

            # 学校验证（重启后给页面一点渲染稳定时间）
            if school:
                time.sleep(2)
                # 先轻量读取当前学校
                school_info = self.browser.get_current_school()
                if school_info.get("success"):
                    current = school_info.get("school", "")
                    if current == school:
                        self._log(f"[OK] 完整重启后学校验证通过: {current}")
                        return {"success": True, "school_verified": True,
                                "school": current, "error": "",
                                "recovery_method": "full_restart"}
                    else:
                        self._log(f"[WARN] 完整重启后学校不匹配: 目标={school}, "
                                  f"当前={current}，尝试切换...")
                        if self.browser.check_and_switch_school(school):
                            self._log(f"[OK] 已切换到目标学校: {school}")
                            return {"success": True, "school_verified": True,
                                    "school": school, "error": "",
                                    "recovery_method": "full_restart"}
                        else:
                            self._log(f"[FAIL] 学校切换失败: {school}")
                            return {"success": True, "school_verified": False,
                                    "school": current,
                                    "error": f"学校切换失败: {school}",
                                    "recovery_method": "full_restart"}
                else:
                    self._log(f"[WARN] 完整重启后无法读取学校: "
                              f"{school_info.get('error', '')}")
                    if self.browser.check_and_switch_school(school):
                        self._log(f"[OK] 已切换到目标学校: {school}")
                        return {"success": True, "school_verified": True,
                                "school": school, "error": "",
                                "recovery_method": "full_restart"}
                    else:
                        return {"success": True, "school_verified": False,
                                "school": "",
                                "error": school_info.get("error", "读取学校失败"),
                                "recovery_method": "full_restart"}

            return {"success": True, "school_verified": True, "school": "", "error": "",
                    "recovery_method": "full_restart"}

        return {"success": False, "school_verified": False, "school": "",
                "error": "浏览器重启失败", "recovery_method": "none"}

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

    REACT_SYSTEM_PROMPT = """你是一个作业上传系统的故障恢复 AI Agent。你拥有浏览器操作能力，可以直接观察页面状态、检查元素、甚至执行点击/输入操作来修复问题。

## 可用工具

### 诊断工具
- check_file_exists: 检查文件是否还在磁盘上。参数: file_path=文件路径
- query_error_history: 查询同类错误近期发生频率和熔断状态。参数: error_type=错误类型
- check_circuit_breaker: 查看全局熔断/浏览器熔断/全量熔断状态。无参数
- inspect_page: 读取浏览器当前页面的可见文本内容（前3000字符）。无参数
- take_screenshot: 截取当前页面并保存为PNG文件。无参数
- check_element: 检查指定元素是否存在/可见/可用/选中。参数: selector=选择器, selector_type=xpath或css(默认xpath)
- check_current_school: 读取浏览器右上角教师姓名下拉框中显示的当前学校名称，返回当前学校、目标学校和匹配结果。无参数
- detect_page_state: 检测浏览器当前页面状态（登录页/角色选择/首页/错误/上传对话框/学校切换对话框/未知）。无参数

### 操作工具
- recover_session: 智能会话恢复。自动检测页面状态并执行轻量恢复——登录页自动登录，角色选择自动选角色，弹窗自动关闭。不会重启浏览器。无参数
- browser_action: 在浏览器中执行操作。参数: action=click|type|select|scroll_down|refresh, selector=选择器, value=输入值(type时), selector_type=xpath或css
- restart_browser: 重启浏览器并重新登录（L4级，最后手段，受熔断保护）。优先尝试 recover_session。无参数
- enqueue_retry: 将文件加入重试队列。参数: file_path=文件路径, retry_level=L1|L2|L3
- mark_manual_review: 标记为待人工处理。参数: reason=原因
- skip_and_wait: 暂时跳过等待下次扫描。参数: reason=原因

## 自愈策略

- L1（轻量重试）：原地重试。适用于偶发超时、网络抖动。
- L2（页面复位）：browser_action(refresh) → 重新进入上传流程。
- L3（环境重置）：browser_action(refresh) → 重新校验学校。
- L4（重启浏览器）：restart_browser（最后手段，先尝试 recover_session）

## 页面状态自适应恢复流程（核心！）

上传失败往往是因为页面状态发生了变化（会话过期→跳转登录页、角色过期→回到角色选择页等）。
你必须按照以下优先级处理，从最轻量到最重量：

### 第一步：检测页面状态
上传失败时，首先调用 detect_page_state 判断当前页面：
- state="login" → 被踢到登录页，调用 recover_session 自动登录
- state="role_select" → 回到角色选择，调用 recover_session 自动选角色
- state="error" → 页面有错误弹窗，调用 recover_session 尝试关闭
- state="upload_dialog" / "school_dialog" → 有残留对话框，调用 recover_session 关闭
- state="home" → 已登录在首页，检查学校是否正确
- state="unknown" → 调用 inspect_page 读取页面文本进一步判断

### 第二步：轻量恢复（recover_session）
如果 detect_page_state 返回 login/role_select/error/upload_dialog/school_dialog：
1. 调用 recover_session 执行自动恢复
2. 恢复成功后，调用 check_current_school 验证学校
3. 学校正确 → enqueue_retry 重新上传
4. 学校不对 → 如果是首页，调用 browser_action 配合页面元素切换学校

### 第三步：针对性手动修复
如果 recover_session 不适用，根据具体情况：
- 页面卡住/弹窗遮挡 → inspect_page → 找到关闭按钮CSS选择器 → browser_action(click)
- 表单校验失败 → check_element 确认哪些字段 → browser_action(type/select) 修正
- 被踢到登录页但自动登录失败 → inspect_page看原因 → 还是失败就 restart_browser

### 第四步：最后手段（restart_browser）
只有在以下情况才调用 restart_browser：
- recover_session 失败
- detect_page_state 返回 unknown 且 inspect_page 也无法判断
- 浏览器明显崩溃/无响应
- 页面进入了无法自动恢复的状态
restart_browser 后必须调用 check_current_school 验证学校！

## 工作流程

1. 先诊断：分析错误信息 → 调用 detect_page_state 检测页面状态
2. 页面异常时优先轻量恢复：recover_session → 验证 → 重试
3. 页面正常时针对性修复：inspect_page → check_element → browser_action
4. 验证结果：检测修复是否生效
5. 做出最终决策：enqueue_retry（恢复后重试）或 mark_manual_review（无法修复）

## 输出格式

Thought: [你的分析推理]
Action: tool_name(key1=value1, key2=value2)

任务完成时：
Thought: [总结]
Final: {"action": "enqueue"|"restart_browser"|"skip"|"manual", "retry_level": "L1"|"L2"|"L3"|"L4"|"L5", "reason": "..."}

## 重要原则

1. 上传失败先检测页面状态(detect_page_state)，再决定恢复策略
2. 优先使用 recover_session 轻量恢复，避免不必要的浏览器重启
3. recover_session 失败后再用 restart_browser 作为最后手段
4. 页面卡住/弹窗遮挡 → inspect_page → 找到关闭按钮 → browser_action(click)
5. 表单校验失败 → check_element 确认哪些字段 → browser_action(type/select) 修正
6. 重启浏览器后务必用 check_current_school 验证学校，学校不对会导致上传到错误的学校
7. 工具返回 Error 时不要放弃，分析原因尝试替代方案
8. file_path/file_name 等关键参数已通过上下文提供，不需要作为工具函数参数传递"""

    def _run_react_loop(self, record: Dict, tripped_types: set) -> Optional[Dict]:
        """
        启动 ReAct 循环让 LLM 自主诊断和决策

        Args:
            record: 错误记录字典
            tripped_types: 当前熔断类型集合

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

        # ── 构建工具（闭包捕获 self + 当前记录上下文）──

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

        # mutable container: 用于标记 restart_browser 工具是否已执行过重启
        _browser_restarted = [False]

        def tool_restart_browser():
            if self.circuit_breaker.is_browser_tripped():
                return {"success": False, "error": "浏览器熔断保护：连续重启已达上限，拒绝操作"}
            if self.upload_processor is not None and self.upload_processor.processing:
                return {"success": False, "error": "UploadProcessor 正在处理中，无法重启浏览器"}

            restart_result = self._do_restart_browser(school=school)
            if restart_result["success"]:
                _browser_restarted[0] = True
                result = {"success": True, "message": "浏览器重启成功"}
                if not restart_result.get("school_verified", True):
                    result["warning"] = (
                        f"重启后学校不匹配: 目标={school}, "
                        f"当前={restart_result.get('school', '?')}。"
                        f"请调用 check_current_school 查看详情。"
                    )
                return result
            else:
                return {"success": False, "error": restart_result.get("error", "浏览器重启失败")}

        def tool_enqueue_retry(file_path_arg=None, retry_level="L1"):
            # 安全守护：检查重试上限和熔断（软校验，硬门禁在 _validate_react_decision 中）
            if retry_count >= self.config.max_retry_count:
                return {"success": False, "error": f"已达全局最大重试次数({self.config.max_retry_count})，应转人工处理"}
            if error_type and error_type in tripped_types:
                return {"success": False, "error": f"错误类型 {error_type} 已熔断，应等待或转人工处理"}
            path = file_path_arg or file_path
            with self._in_retry_lock:
                if path in self._in_retry:
                    return {"success": False, "error": "文件已在重试队列中"}
            return {"success": True, "message": f"准备以 {retry_level} 级别加入重试队列", "retry_level": retry_level}

        def tool_mark_manual_review(reason=""):
            self.db.update_retry_status(record_id, 'finished')
            return {"success": True, "message": f"已标记人工处理: {reason}", "reason": reason}

        def tool_skip_and_wait(reason=""):
            return {"success": True, "message": f"已跳过: {reason}", "reason": reason}

        # ── 浏览器交互工具 ──

        def _browser_busy():
            """检查 UploadProcessor 是否正在使用浏览器，防止并发操作"""
            return (self.upload_processor is not None
                    and self.upload_processor.processing)

        def tool_inspect_page():
            if not self.browser.is_initialized:
                return {"success": False, "error": "浏览器未初始化，请先重启浏览器"}
            if _browser_busy():
                return {"success": False, "error": "UploadProcessor 正在上传中，浏览器被占用"}
            return self.browser.get_page_text()

        def tool_take_screenshot():
            if not self.browser.is_initialized:
                return {"success": False, "error": "浏览器未初始化"}
            if _browser_busy():
                return {"success": False, "error": "UploadProcessor 正在上传中，浏览器被占用"}
            return self.browser.get_page_screenshot()

        def tool_check_element(selector, selector_type="xpath"):
            if not self.browser.is_initialized:
                return {"success": False, "error": "浏览器未初始化"}
            if _browser_busy():
                return {"success": False, "error": "UploadProcessor 正在上传中，浏览器被占用"}
            return self.browser.check_element(selector, selector_type)

        def tool_browser_action(action, selector="", value="", selector_type="xpath"):
            if not self.browser.is_initialized:
                return {"success": False, "error": "浏览器未初始化，请先重启浏览器"}
            if _browser_busy():
                return {"success": False, "error": "UploadProcessor 正在上传中，浏览器被占用"}
            return self.browser.execute_browser_action(action, selector, value, selector_type)

        def tool_check_current_school():
            """读取浏览器当前登录的学校名称，用于验证学校是否匹配目标"""
            if not self.browser.is_initialized:
                return {"success": False, "error": "浏览器未初始化，请先重启浏览器"}
            if _browser_busy():
                return {"success": False, "error": "UploadProcessor 正在上传中，浏览器被占用"}
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

        def tool_detect_page_state():
            """检测浏览器当前页面状态（登录页/角色选择/首页/错误页等）"""
            if not self.browser.driver:
                return {"success": False, "state": "no_browser",
                        "details": "浏览器未启动"}
            if _browser_busy():
                return {"success": False, "state": "busy",
                        "details": "UploadProcessor 正在上传中，浏览器被占用"}
            return self.browser.detect_page_state()

        def tool_recover_session():
            """
            智能会话恢复：检测页面状态并自动执行轻量恢复。
            登录页→自动登录，角色选择→自动选角色，弹窗→自动关闭。
            如果轻量恢复失败，调用方应使用 restart_browser 进行完整重启。
            """
            if not self.browser.driver:
                return {"success": False, "state_before": "no_browser",
                        "action_taken": "none",
                        "error": "浏览器未启动，请先调用 restart_browser"}
            if _browser_busy():
                return {"success": False, "state_before": "busy",
                        "action_taken": "none",
                        "error": "UploadProcessor 正在上传中，浏览器被占用"}
            return self.browser.recover_session()

        tools = {
            "check_file_exists": tool_check_file_exists,
            "query_error_history": tool_query_error_history,
            "check_circuit_breaker": tool_check_circuit_breaker,
            "inspect_page": tool_inspect_page,
            "take_screenshot": tool_take_screenshot,
            "check_element": tool_check_element,
            "check_current_school": tool_check_current_school,
            "detect_page_state": tool_detect_page_state,
            "recover_session": tool_recover_session,
            "browser_action": tool_browser_action,
            "restart_browser": tool_restart_browser,
            "enqueue_retry": tool_enqueue_retry,
            "mark_manual_review": tool_mark_manual_review,
            "skip_and_wait": tool_skip_and_wait,
        }

        tool_descs = {
            "check_file_exists": "检查文件是否还在磁盘上。参数: file_path=文件路径(可选)",
            "query_error_history": "查询同类错误近期发生频率和熔断状态。参数: error_type=错误类型(可选)",
            "check_circuit_breaker": "查看全局熔断/浏览器熔断/全量熔断状态列表。无参数",
            "inspect_page": "读取浏览器当前页面的可见文本。用于观察页面状态、错误提示、弹窗内容。无参数",
            "take_screenshot": "截取当前页面保存为PNG文件，返回文件路径供人工查看。无参数",
            "check_element": "检查指定元素是否存在/可见/可用/选中。参数: selector=XPath或CSS选择器, selector_type=xpath或css(默认xpath)",
            "check_current_school": "读取浏览器右上角教师下拉框显示的当前学校名称。返回当前学校、目标学校和匹配结果。无参数",
            "detect_page_state": "检测浏览器当前页面状态，返回state(login/role_select/home/error/upload_dialog/school_dialog/unknown)和详情。无参数",
            "recover_session": "智能会话恢复：自动检测页面状态并执行轻量恢复(登录页→重新登录, 角色选择→选角色, 弹窗→关闭)。失败时不会重启浏览器，由调用方按需调用restart_browser。无参数",
            "browser_action": "在浏览器中执行操作。参数: action=click|type|select|scroll_down|refresh, selector=选择器, value=输入值(仅type需要), selector_type=xpath或css",
            "restart_browser": "重启浏览器并重新登录(L4级，最后手段)。重启后会自动验证学校匹配。优先尝试recover_session轻量恢复。受熔断保护。无参数",
            "enqueue_retry": "将文件加入重试队列。参数: file_path=文件路径(可选), retry_level=L1|L2|L3",
            "mark_manual_review": "标记为待人工处理。参数: reason=原因",
            "skip_and_wait": "暂时跳过该记录。参数: reason=原因",
        }

        # 构建任务描述
        task = f"""处理一条上传失败记录：

- 文件名: {file_name}
- 文件路径: {file_path}
- 学校: {record.get('school', '')}
- 年级: {record.get('grade', '')}
- 科目: {record.get('subject', '')}
- 失败阶段: {record.get('fail_stage', '未知')}
- 错误类型: {error_type or '未知'}
- 错误分类: {record.get('error_category', '未知')}
- 错误信息: {error_message}
- 当前重试次数: {retry_count}
- 全局最大重试: {self.config.max_retry_count}

请调用工具调查情况，然后做出恢复决策。"""

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
            }
        else:
            self._log(f"AutoRetryAgent: ReAct 失败(steps={result['steps']}), 回退规则引擎")
            return None

    # ─── 规则引擎兜底 ───

    def _rule_engine_decision(self, record: Dict, tripped_types: Set[str]) -> Optional[Dict]:
        """
        传统规则引擎决策（AI 禁用或失败时的兜底）
        保留原有的 get_strategy() + 全部安全检查逻辑
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
                self._log(f"AutoRetryAgent: UploadProcessor 处理中,延迟 - {file_name}")
                return None
            return {"action": "restart_browser", "retry_level": retry_level}

        # L1/L2/L3
        self._log(f"AutoRetryAgent: 规则引擎决策 [{retry_level.value}] - {file_name}")
        return {"action": "enqueue", "retry_level": retry_level}

    # ─── 上传结果回调 ───

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

        # 更新数据库
        self.db.set_agent_retry_success(record_id, success)
        if success:
            self.db.mark_record_success(record_id)
            self.db.update_retry_status(record_id, 'finished')
            self._log(f"AutoRetryAgent: ✓ 自动重试成功 - {os.path.basename(file_path)}")
        else:
            self.db.update_retry_status(record_id, 'pending')
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
