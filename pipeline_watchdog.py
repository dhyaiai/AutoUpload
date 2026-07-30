"""
流水线看门狗模块
功能:
  - PipelineHeartbeat: UploadProcessor 各阶段上报心跳（阶段名+时间戳）
  - RecentLogBuffer: 近期运行日志环形缓冲（供 Agent 的 read_recent_logs 工具回看）
  - PipelineWatchdog: 独立线程检测流水线卡死，强制打断浏览器并唤醒 Agent

命名说明: 不能命名为 watchdog.py，否则会遮蔽 file_monitor.py 依赖的 PyPI watchdog 包
"""
import threading
import time
from collections import deque
from datetime import datetime
from typing import Dict, Optional

from config_manager import ConfigManager
from error_types import UploadStage


class PipelineHeartbeat:
    """
    流水线心跳状态（线程安全）
    UploadProcessor 在每个处理阶段调用 beat() 上报，任务结束调用 clear()
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stage: Optional[str] = None
        self._file_path: Optional[str] = None
        self._stage_started_at: float = 0.0
        self._updated_at: float = 0.0

    def beat(self, stage: str, file_path: str):
        """
        上报心跳：记录当前阶段与时间戳，阶段变化时刷新 stage_started_at

        Args:
            stage: UploadStage 枚举值（如 "submit_upload"）
            file_path: 当前处理的文件路径
        """
        now = time.time()
        with self._lock:
            if stage != self._stage or file_path != self._file_path:
                self._stage_started_at = now
            self._stage = stage
            self._file_path = file_path
            self._updated_at = now

    def clear(self):
        """任务结束时清空心跳（看门狗据此跳过检查）"""
        with self._lock:
            self._stage = None
            self._file_path = None
            self._stage_started_at = 0.0
            self._updated_at = 0.0

    def snapshot(self) -> Dict:
        """
        返回当前心跳快照（加锁拷贝）

        Returns:
            {"stage": str|None, "file_path": str|None,
             "stage_started_at": float, "updated_at": float}
        """
        with self._lock:
            return {
                "stage": self._stage,
                "file_path": self._file_path,
                "stage_started_at": self._stage_started_at,
                "updated_at": self._updated_at,
            }


class RecentLogBuffer:
    """近期日志环形缓冲（线程安全），供 Agent 回看卡死/失败前的运行轨迹"""

    def __init__(self, maxlen: int = 300):
        self._lock = threading.Lock()
        self._buffer = deque(maxlen=maxlen)

    def append(self, msg: str):
        """带时间戳追加一条日志"""
        ts = datetime.now().strftime('%H:%M:%S')
        with self._lock:
            self._buffer.append(f"[{ts}] {msg}")

    def tail(self, n: int = 50) -> list:
        """返回最近 n 条日志（旧→新）"""
        with self._lock:
            items = list(self._buffer)
        return items[-n:] if n > 0 else []


# 模块级单例：全项目共享的近期日志缓冲
RECENT_LOGS = RecentLogBuffer()


class PipelineWatchdog:
    """
    流水线看门狗
    独立 daemon 线程，定期检查心跳，发现某阶段耗时超阈值即判定卡死：
      - 浏览器阶段：强制 close() 打断被阻塞的 Selenium 调用（等同真人关掉无响应浏览器），
        异常沿 UploadProcessor 既有失败路径落库（错误消息含 [WATCHDOG] 标记 → PIPELINE_STUCK）
      - 非浏览器阶段：仅告警（requests 自带超时，理论上不会永久卡）
    处理后立即唤醒 AutoRetryAgent 接管
    """

    # 需要强制打断浏览器的阶段
    BROWSER_STAGES = {
        UploadStage.BROWSER_INIT.value,
        UploadStage.SCHOOL_CHECK.value,
        UploadStage.FORM_FILL.value,
        UploadStage.SUBMIT_UPLOAD.value,
    }

    # 各阶段卡死判定阈值默认值(秒)，可被 WATCHDOG_STAGE_TIMEOUTS 配置覆盖
    DEFAULT_STAGE_TIMEOUTS = {
        UploadStage.READ_FILE.value: 60,
        UploadStage.AI_CLASSIFY.value: 180,
        UploadStage.BROWSER_INIT.value: 240,
        UploadStage.SCHOOL_CHECK.value: 240,
        UploadStage.FORM_FILL.value: 240,
        UploadStage.SUBMIT_UPLOAD.value: 300,  # 需大于 UPLOAD_TIMEOUT(120s)
    }

    def __init__(self, heartbeat: PipelineHeartbeat, browser, upload_processor,
                 agent, log_queue, stop_event: threading.Event):
        """
        Args:
            heartbeat: UploadProcessor 共享的心跳对象
            browser: BrowserAutomation 实例
            upload_processor: UploadProcessor 实例
            agent: AutoRetryAgent 实例（卡死后调用其 wake()）
            log_queue: 日志队列
            stop_event: 停止信号
        """
        self.heartbeat = heartbeat
        self.browser = browser
        self.upload_processor = upload_processor
        self.agent = agent
        self.log_queue = log_queue
        self.stop_event = stop_event

        self.config = ConfigManager()
        self.enabled = self.config.get("WATCHDOG_ENABLE", True)
        self.check_interval = self.config.get("WATCHDOG_CHECK_INTERVAL", 10)
        # 合并配置与默认阈值（配置项覆盖默认）
        cfg_timeouts = self.config.get("WATCHDOG_STAGE_TIMEOUTS", {}) or {}
        self.stage_timeouts = {**self.DEFAULT_STAGE_TIMEOUTS, **cfg_timeouts}

        # 去重：同一次卡死（同文件+同阶段起始时间）只处理一次
        self._handled_episode = None

    def run(self):
        """看门狗主循环（daemon 线程入口）"""
        if not self.enabled:
            self._log("PipelineWatchdog 已禁用")
            return
        self._log(f"PipelineWatchdog 已启动 (检查间隔={self.check_interval}s)")
        while not self.stop_event.is_set():
            try:
                self._check_once()
            except Exception as e:
                self._log(f"PipelineWatchdog 检查异常(非致命): {e}")
            self.stop_event.wait(self.check_interval)
        self._log("PipelineWatchdog 已停止")

    def _check_once(self):
        """单次卡死检查"""
        snap = self.heartbeat.snapshot()
        stage = snap.get("stage")
        if not stage:
            return
        # 任务已结束但心跳未清（防御）：processing=False 时跳过
        if not getattr(self.upload_processor, "processing", False):
            return

        timeout = self.stage_timeouts.get(stage)
        if not timeout:
            return

        elapsed = time.time() - snap.get("stage_started_at", 0)
        if elapsed < timeout:
            return

        # 去重：同一 episode 只处理一次
        episode = (snap.get("file_path"), snap.get("stage_started_at"))
        if episode == self._handled_episode:
            return
        self._handled_episode = episode

        self._handle_stuck(snap, elapsed, timeout)

    def _handle_stuck(self, snap: Dict, elapsed: float, timeout: int):
        """
        处理卡死：浏览器阶段强制打断，非浏览器阶段仅告警，随后唤醒 Agent
        """
        stage = snap.get("stage", "")
        file_path = snap.get("file_path", "")
        self._log(f"[WATCHDOG] 检测到流水线卡死: 阶段={stage} 文件={file_path} "
                  f"已持续{int(elapsed)}s (阈值{timeout}s)")

        if stage in self.BROWSER_STAGES:
            # 先写入独立的打断原因属性（UploadProcessor 失败路径优先读取它，
            # 不用 last_upload_error 是因为 upload_file 的兜底 except 会用
            # Selenium 异常文本覆盖它，导致 [WATCHDOG] 标记丢失），
            # 再强制关闭浏览器：被阻塞的 Selenium 调用随即抛 WebDriverException，
            # 异常沿 _execute_browser_upload 的兜底 except 走既有失败落库路径
            self.browser.watchdog_interrupt_reason = (
                f"[WATCHDOG] 流水线卡死于{stage}阶段超过{int(elapsed)}秒，"
                f"已强制关闭浏览器打断")
            self._log(f"[WATCHDOG] 强制关闭浏览器以打断卡死的{stage}操作...")
            try:
                self.browser.close()
            except Exception as e:
                self._log(f"[WATCHDOG] 关闭浏览器异常(非致命): {e}")
        else:
            self._log(f"[WATCHDOG] {stage}为非浏览器阶段，仅告警不强制打断"
                      f"（该阶段自带超时机制）")

        # 立即唤醒 Agent 接管（失败记录落库后 Agent 会在扫描中处理）
        if self.agent is not None and hasattr(self.agent, "wake"):
            self.agent.wake()
            self._log("[WATCHDOG] 已唤醒 AutoRetryAgent")

    def _log(self, message: str):
        """日志输出（GUI 队列 + 近期日志缓冲）"""
        RECENT_LOGS.append(message)
        try:
            self.log_queue.put(message)
        except Exception:
            print(message)
