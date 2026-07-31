"""
上传处理器模块
功能:从任务队列中取出文件,执行完整的上传流程
特点:单线程顺序处理,防止科目混淆,支持失败重试
"""
import json
import os
import time
import threading
from queue import Queue, Empty
from typing import Optional
from selenium.webdriver.support.ui import WebDriverWait
from db_manager import DatabaseManager
from info_extractor import InfoExtractor
from subject_classifier import SubjectClassifier
from browser_automation import BrowserAutomation
from config_manager import ConfigManager
from error_types import UploadStage, ErrorCategory, ErrorType, RetryLevel
from pipeline_watchdog import PipelineHeartbeat, RECENT_LOGS


class UploadProcessor:
    """
    上传处理器
    在独立线程中运行,循环从任务队列取任务并执行上传

    优化策略: 按学校分组批量上传,减少学校切换次数
    - 当前处理的学校缓存起来
    - 如果下一个文件的学校与当前一致,无需切换
    - 如果不一致,才执行学校切换
    """

    def __init__(self, task_queue: Queue, stop_event: threading.Event, log_queue: Queue):
        """
        初始化上传处理器

        Args:
            task_queue: 任务队列(存放待上传文件路径)
            stop_event: 停止信号
            log_queue: 日志队列(用于向GUI发送日志消息)
        """
        self.task_queue = task_queue
        self.stop_event = stop_event
        self.log_queue = log_queue

        # 初始化各个模块
        self.db = DatabaseManager()
        self.config = ConfigManager()
        self.info_extractor = InfoExtractor()
        self.classifier = SubjectClassifier()
        self.browser = BrowserAutomation(log_queue=log_queue)
        # 处理中标记，防止后端在任务处理期间误关浏览器
        self.processing = False
        # 流水线心跳（供 PipelineWatchdog 检测卡死）
        self.heartbeat = PipelineHeartbeat()

        # Agent 回调相关
        self.auto_retry_agent = None           # AutoRetryAgent 引用
        self._agent_retry_map: dict = {}       # file_path -> record_id (Agent触发的重试)
        self._agent_retry_level_map: dict = {} # file_path -> retry_level (重试自愈级别)
        self._agent_retry_lock = threading.Lock()  # 保护 _agent_retry_map/_agent_retry_level_map 跨线程访问

        # 会话丢失标志：上传过程中检测到账号被踢下线时置位，
        # run() 据此暂停队列消费，等待 Agent 恢复后再继续。
        # 使用 threading.Event 保证跨线程读写安全（Agent 线程 ↔ UploadProcessor 线程）
        self._session_lost = threading.Event()

        # 浏览器定时重启计数器：累计上传次数，达到阈值自动重启浏览器防内存泄漏
        self._upload_count = 0

    def _on_session_lost(self):
        """会话丢失时置位标志，Agent 主循环检测到后自动切换为 5s 快速轮询。
        同时立即释放 processing 锁，让 Agent 可以接管浏览器执行恢复，
        避免死锁：UploadProcessor 等浏览器响应 → Agent 等 UploadProcessor 释放锁。"""
        self._session_lost.set()
        self.processing = False  # 立即释放浏览器锁，让 Agent 可以恢复

    def set_agent(self, agent):
        """
        注入 AutoRetryAgent 引用，用于上传完成后回调通知

        Args:
            agent: AutoRetryAgent 实例
        """
        self.auto_retry_agent = agent

    def register_agent_retry(self, file_path: str, record_id: int, retry_level=None):
        """
        Agent 触发重试前注册映射关系，上传完成后根据此映射回调通知 Agent

        Args:
            file_path: 文件完整路径
            record_id: 对应的失败记录ID
            retry_level: 自愈级别(RetryLevel枚举值)，用于上传前执行对应级别的预处理
        """
        with self._agent_retry_lock:
            self._agent_retry_map[file_path] = record_id
            if retry_level is not None:
                self._agent_retry_level_map[file_path] = retry_level

    def _notify_agent_result(self, file_path: str, success: bool):
        """
        如果本次上传是 Agent 触发的重试，通知 Agent 结果并更新数据库

        Args:
            file_path: 文件完整路径
            success: 上传是否成功
        """
        if file_path not in self._agent_retry_map:
            return
        with self._agent_retry_lock:
            record_id = self._agent_retry_map.pop(file_path, None)
            # 同步清理 level map
            self._agent_retry_level_map.pop(file_path, None)
        if record_id is None:
            return
        if self.auto_retry_agent is not None:
            self.auto_retry_agent.on_upload_result(record_id, success, file_path)

    def _preprocess_agent_retry(self, file_path: str) -> bool:
        """
        Agent 重试上传前预处理：根据自愈级别执行环境复位或浏览器重启。
        在 processing=True 之后、上传 try 块之前调用，确保单线程操作浏览器。

        Args:
            file_path: 文件路径

        Returns:
            True=预处理成功或无需处理, False=致命错误应放弃本次重试
        """
        retry_level_value = self._agent_retry_level_map.get(file_path)
        if not retry_level_value:
            return True  # 非 Agent 重试，无需预处理

        try:
            level = RetryLevel(retry_level_value)
        except (ValueError, TypeError):
            return True

        if level in (RetryLevel.L2_PAGE_RESET, RetryLevel.L3_ENV_RESET):
            self._send_log(f"Agent重试: 执行{level.value}浏览器复位...")
            # 浏览器未初始化时只需保证可用，不需要完整的 reset_to_home
            # （reset_to_home 会在初始化后做 driver.get(base_url)+refresh，干扰登录流程）
            if not self.browser.driver or not self.browser.is_logged_in:
                self._send_log("Agent重试: 浏览器未就绪，先初始化...")
                if not self.browser.ensure_initialized():
                    self._send_log("Agent重试: 浏览器初始化失败，继续尝试上传流程")
                else:
                    self._send_log("Agent重试: 浏览器初始化完成")
            else:
                if not self.browser.reset_to_home():
                    self._send_log(f"Agent重试: {level.value}浏览器复位失败，继续尝试上传流程")
                else:
                    self._send_log(f"Agent重试: {level.value}浏览器复位完成")

        elif level == RetryLevel.L4_SERVICE_RESTART:
            self._send_log(f"Agent重试: L4 已在Agent中重启浏览器，跳过预处理")

        # L1 和 L5 无需预处理
        return True

    def run(self):
        """
        主运行循环
        持续从任务队列中取出任务并处理,直到收到停止信号
        Agent 忙碌时暂停消费新任务，等待恢复完成

        支持两种任务格式：
        - str: 桌面端文件路径（文件夹监控产生）
        - dict: 小程序结构化任务 {file_path, school, grade, subject, record_id}
        """
        print("上传处理器已启动")

        while not self.stop_event.is_set():
            try:
                # Agent 正在执行恢复操作时等待，避免在环境未修复时处理新任务
                if (self.auto_retry_agent is not None
                        and self.auto_retry_agent.agent_busy.is_set()):
                    time.sleep(0.5)
                    continue

                # 会话失效时暂停消费新任务，等待Agent恢复登录
                # 同时检查 is_logged_in 作为兜底（detect_page_state 未覆盖到的边界情况）
                if self._session_lost.is_set() or (
                    self.browser.driver and not self.browser.is_logged_in
                ):
                    # 检查浏览器是否已恢复登录
                    if self.browser.driver and self.browser.is_logged_in:
                        if self._session_lost.is_set():
                            self._send_log("检测到浏览器会话已恢复，继续处理任务")
                            self._session_lost.clear()
                    else:
                        if not self._session_lost.is_set():
                            self._send_log("检测到浏览器未登录(is_logged_in=False)，暂停消费新任务")
                            self._session_lost.set()
                        time.sleep(2)
                        continue

                # 从队列中获取任务(超时1秒,以便检查停止信号)
                raw_task = self.task_queue.get(timeout=1)

                # 根据任务类型分发
                if isinstance(raw_task, str):
                    # 桌面端：纯文件路径
                    self._process_file(raw_task)
                elif isinstance(raw_task, dict):
                    # 小程序：结构化任务
                    self._process_miniprogram_task(raw_task)
                else:
                    self._send_log(f"警告: 未知任务格式,跳过: {type(raw_task)}")
                    self.task_queue.task_done()
                    continue

                # 检测到会话丢失 → 重新排队当前任务，暂停队列等待Agent恢复
                if self._session_lost.is_set():
                    # 标记登录态失效，使 Agent 能正确判断
                    self.browser.is_logged_in = False
                    self._send_log(
                        "会话丢失(账号被踢下线)，当前任务已重新排队，"
                        "暂停消费新任务，等待Agent恢复登录..."
                    )
                    # 当前任务放回队尾，等恢复后重试
                    # 注意：不调用 task_done()——任务未完成仅重新排队，
                    # 下次 get() 会重新递增 unfinished 计数
                    self.task_queue.put(raw_task)
                else:
                    # 标记任务完成（仅在未重新排队时调用）
                    self.task_queue.task_done()

            except Empty:
                # 队列为空,继续循环
                continue

            except Exception as e:
                # 捕获未预期的异常,确保程序不崩溃
                error_msg = f"上传处理器异常: {e}"
                print(error_msg)
                self._send_log(error_msg)
                # 确保 task_done() 被调用，防止 Queue.join() 永久阻塞
                try:
                    self.task_queue.task_done()
                except ValueError:
                    pass  # 如果 task_done 被调用次数超过 get，忽略

        print("上传处理器已停止")
    
    def _execute_browser_upload(self, file_name: str, file_path: str, folder_name: str,
                                 school: str, grade: str, subject: str,
                                 error_context: dict, is_agent_retry: bool = False,
                                 agent_retry_record_id: int = None,
                                 agent_retry_level: str = None):
        """
        执行浏览器上传流水线：BrowserInit → SchoolCheck → SubmitUpload
        从 _process_file 中提取，供桌面端和小程序两种任务共用。

        Args:
            file_name: 文件名
            file_path: 文件完整路径
            folder_name: 文件夹名
            school: 学校
            grade: 年级
            subject: 科目
            error_context: 错误上下文
            is_agent_retry: 是否Agent触发的重试
            agent_retry_record_id: Agent重试对应的记录ID
            agent_retry_level: Agent重试级别

        Returns:
            (upload_success: bool, current_stage: UploadStage, already_handled: bool)
            upload_success=True 表示上传成功
            current_stage 表示失败的阶段（仅失败时有意义）
            already_handled=True 表示 _handle_failure 已在内部调用，外层无需重复处理
        """
        current_stage = None

        # Agent 重试预处理：在单线程内执行浏览器复位（L2/L3）
        if is_agent_retry:
            if not self._preprocess_agent_retry(file_path):
                self._send_log("Agent重试: 预处理失败，放弃本次重试")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                     "Agent重试预处理失败: 浏览器无法恢复", UploadStage.BROWSER_INIT,
                                     ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL,
                                     error_context, existing_record_id=agent_retry_record_id)
                return False, UploadStage.BROWSER_INIT, True

        try:
            # 确保浏览器已启动（延迟初始化，首次调用时才打开浏览器）
            current_stage = UploadStage.BROWSER_INIT
            self.heartbeat.beat(current_stage.value, file_path)
            if not self.browser.ensure_initialized():
                error_msg = "浏览器启动失败"
                self._send_log(f"错误: {error_msg}")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                     error_msg, current_stage,
                                     ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL,
                                     error_context, existing_record_id=agent_retry_record_id)
                return False, current_stage, True

            # 检查浏览器是否崩溃
            if not self.browser.check_browser_status():
                self._send_log("检测到浏览器异常,正在重启...")
                if not self.browser.restart_browser():
                    error_msg = "浏览器重启失败"
                    self._send_log(f"错误: {error_msg}")
                    self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                         error_msg, current_stage,
                                         ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL,
                                         error_context, existing_record_id=agent_retry_record_id)
                    return False, current_stage, True

            # 检查登录状态
            if not self.browser.check_login_status():
                self._send_log("检测到登录失效,正在重新登录...")
                if not self.browser.restart_browser():
                    error_msg = "重新登录失败"
                    self._send_log(f"错误: {error_msg}")
                    self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                         error_msg, current_stage,
                                         ErrorCategory.BROWSER_ERROR, ErrorType.LOGIN_EXPIRED,
                                         error_context, existing_record_id=agent_retry_record_id)
                    return False, current_stage, True

            # 浏览器复用场景：确保当前在首页，否则后续上传按钮可能找不到
            # ensure_initialized 对已打开的浏览器不会导航，页面可能停留在任何位置
            # 只对明确不在首页的状态才导航（login/dialog/error），unknown 可能是检测未匹配URL
            NON_HOME_STATES = ("login", "role_select", "upload_dialog", "school_dialog", "error")
            page_state = self.browser.detect_page_state()
            state = page_state.get("state", "unknown") if page_state.get("success") else "unknown"
            if state in NON_HOME_STATES:
                self._send_log(f"当前页面状态: {state}，导航回首页...")
                try:
                    base_url = self.config.website_url.rstrip('/')
                    self.browser.driver.get(base_url)
                    WebDriverWait(self.browser.driver, 10).until(
                        lambda d: d.execute_script("return document.readyState;") == "complete"
                    )
                    time.sleep(0.5)
                    self._send_log("已导航回首页")
                except Exception as e:
                    self._send_log(f"警告: 导航回首页失败 - {e}，继续尝试...")

            # 校验并切换学校(每次上传前都实际检查网页上的学校)
            current_stage = UploadStage.SCHOOL_CHECK
            self.heartbeat.beat(current_stage.value, file_path)
            self._send_log(f"正在校验学校: {school}")
            if not self.browser.check_and_switch_school(school):
                # ── 分层检测：优先 detect_page_state，再回退到关键词/页面文本 ──
                school_error_type = ErrorType.SCHOOL_SWITCH_FAIL
                error_msg = f"学校校验/切换失败: {school}"
                is_session_lost = False

                # 第0层：check_and_switch_school 内部已检测到登录页URL
                if not self.browser.is_logged_in:
                    is_session_lost = True
                    error_msg = f"学校校验/切换失败: 会话丢失(页面已跳转到登录页，账号被踢下线)"

                # 第1层：detect_page_state 全面检测页面状态
                if not is_session_lost:
                    try:
                        page_state = self.browser.detect_page_state()
                        if page_state.get("success"):
                            state = page_state.get("state", "unknown")
                            self._send_log(f"学校校验失败-页面状态检测: {state}")
                            if state == "login":
                                is_session_lost = True
                                error_msg = (f"学校校验/切换失败: 会话丢失"
                                             f"(页面状态={state}, 账号被踢下线)")
                            elif state == "role_select":
                                is_session_lost = True
                                error_msg = (f"学校校验/切换失败: 会话丢失"
                                             f"(页面状态={state}, 需重新登录)")
                    except Exception as e:
                        self._send_log(f"页面状态检测异常(非致命): {e}")

                # 第2层：页面文本关键词扫描
                page_hint = ""
                if not is_session_lost:
                    page_info = self.browser.get_page_text()
                    if page_info and page_info.get("success"):
                        page_text = page_info.get("text", "")
                        if "该校未开通数智作业服务" in page_text:
                            error_msg = f"学校未开通数智作业服务: {school}"
                            self._send_log(f"错误: {error_msg}")
                            self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                                 error_msg, current_stage,
                                                 ErrorCategory.PLATFORM_BIZ_ERROR, ErrorType.SCHOOL_NOT_ACTIVATED,
                                                 error_context, existing_record_id=agent_retry_record_id)
                            return False, current_stage, True
                        error_keywords = ["被迫下线", "异地登录", "登录失效", "重新登录",
                                         "没有权限", "已过期", "账号异常"]
                        for kw in error_keywords:
                            if kw in page_text:
                                idx = page_text.find(kw)
                                page_hint = page_text[max(0, idx-20):idx+80].replace('\n', ' ')
                                if self._is_session_lost_error(page_hint):
                                    is_session_lost = True
                                    error_msg = (f"学校校验/切换失败: {school}"
                                                 f"（页面提示: {page_hint}）")
                                break

                # 统一处理会话丢失
                if is_session_lost:
                    school_error_type = ErrorType.LOGIN_EXPIRED
                    self._on_session_lost()
                elif page_hint:
                    error_msg += f"（页面提示: {page_hint}）"

                self._send_log(f"错误: {error_msg}")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                     error_msg, current_stage,
                                     ErrorCategory.BROWSER_ERROR, school_error_type,
                                     error_context, existing_record_id=agent_retry_record_id)
                return False, current_stage, True
            self._send_log(f"✓ 学校校验通过: {school}")

            # 执行上传(传递学校参数,用于在上传对话框中选择)
            current_stage = UploadStage.SUBMIT_UPLOAD
            self.heartbeat.beat(current_stage.value, file_path)
            self._send_log(f"正在上传到平台...")
            upload_success = self.browser.upload_file(file_path, grade, subject, school)
            return upload_success, current_stage, False

        except Exception as e:
            # 兜底：捕获浏览器操作中未预期的异常
            error_msg = f"浏览器操作异常: {e}"
            self._send_log(f"✗ 上传异常: {file_name} - {error_msg}")
            import traceback
            traceback.print_exc()
            return False, current_stage or UploadStage.SUBMIT_UPLOAD, False

    def _on_upload_success(self, file_name: str, file_path: str, folder_name: str,
                            school: str, grade: str, subject: str, is_agent_retry: bool):
        """上传成功后的后处理：记录数据库、清理旧失败、通知Agent"""
        if self.auto_retry_agent is not None:
            self.auto_retry_agent.on_any_upload_success()
        if not is_agent_retry:
            self.db.add_record(
                file_name=file_name, file_path=file_path, folder_name=folder_name,
                school=school, grade=grade, subject=subject, status='success'
            )
        resolved = self.db.resolve_pending_by_file(file_name, folder_name)
        if resolved > 0:
            self._send_log(f"已清理 {resolved} 条旧失败记录: {file_name}")
        self.db.add_analysis_record(
            file_name=file_name, file_path=file_path, folder_name=folder_name,
            school=school, grade=grade, subject=subject, status='success'
        )
        self._send_log(f"✓ 上传成功: {file_name} ({subject})")

    def _update_upload_counter(self):
        """上传计数器：达到阈值自动重启浏览器防内存泄漏"""
        self._upload_count += 1
        restart_interval = self.config.browser_restart_interval
        if restart_interval > 0 and self._upload_count >= restart_interval:
            self._send_log(f"达到{restart_interval}次上传阈值，主动重启浏览器...")
            try:
                self.browser.restart_browser()
            except Exception as e:
                self._send_log(f"浏览器定时重启失败: {e}")
            self._upload_count = 0

    def _process_file(self, file_path: str):
        """
        处理单个文件（桌面端文件夹监控）→ 解析学校/年级后委托 _process_task
        """
        file_name = os.path.basename(file_path)
        folder_path = os.path.dirname(file_path)
        folder_name = os.path.basename(folder_path)

        if self.db.is_file_uploaded(file_name, folder_name):
            self._send_log(f"文件已上传过,跳过: {file_name}")
            self._notify_agent_result(file_path, True)
            return

        is_agent_retry = file_path in self._agent_retry_map
        agent_retry_record_id = self._agent_retry_map.get(file_path)
        agent_retry_level = self._agent_retry_level_map.get(file_path)

        school, grade = self.info_extractor.parse_folder_name(folder_path)
        if agent_retry_record_id:
            record = self.db.get_record_by_id(agent_retry_record_id)
            if record:
                db_school = (record.get('school', '') or '').strip()
                db_grade = (record.get('grade', '') or '').strip()
                is_miniprogram = (record.get('source', '') == 'miniprogram')
                if is_miniprogram and db_school and db_grade:
                    school, grade = db_school, db_grade
                    folder_name = 'miniprogram'
                else:
                    if not school and db_school:
                        school = db_school
                    if not grade and db_grade:
                        grade = db_grade

        if not school or not grade:
            error_context = {"folder_name": folder_name}
            error_msg = f"无法解析文件夹名称: {folder_name}"
            self._send_log(f"错误: {error_msg}")
            self._handle_failure(file_name, file_path, folder_name, "未知", "未知", "未知",
                                 error_msg, UploadStage.PARSE_FOLDER,
                                 ErrorCategory.FILE_PROCESS_ERROR, ErrorType.FILE_UNREADABLE,
                                 error_context, existing_record_id=agent_retry_record_id)
            self._notify_agent_result(file_path, False)
            return

        self._process_task({
            'file_name': file_name, 'file_path': file_path, 'folder_name': folder_name,
            'school': school, 'grade': grade, 'subject': None,
            'record_id': agent_retry_record_id,
            'is_agent_retry': is_agent_retry,
            'agent_retry_record_id': agent_retry_record_id,
            'agent_retry_level': agent_retry_level,
            'source': 'desktop',
        })

    def _process_task(self, task_info: dict):
        """
        统一的任务处理管线：parse→read→classify→browser_upload→handle_result。
        _process_file（桌面端）和 _process_miniprogram_task（小程序）共用。

        task_info 字段:
            file_name, file_path, folder_name, school, grade, subject (可为None),
            record_id (可为None), is_agent_retry, agent_retry_record_id,
            agent_retry_level, source ('desktop'|'miniprogram'),
        """
        file_name = task_info['file_name']
        file_path = task_info['file_path']
        folder_name = task_info['folder_name']
        school = task_info['school']
        grade = task_info['grade']
        subject = task_info.get('subject')
        record_id = task_info.get('record_id')
        is_agent_retry = task_info.get('is_agent_retry', False)
        agent_retry_record_id = task_info.get('agent_retry_record_id')
        agent_retry_level = task_info.get('agent_retry_level')
        source = task_info.get('source', 'desktop')
        error_context = task_info.get('error_context', {"folder_name": folder_name,
                                                         "school": school, "grade": grade,
                                                         "source": source})

        self._send_log(f"[{source}] 开始处理: {file_name} ({school}/{grade}/{subject or '待识别'})")

        current_stage = None
        upload_success = False

        # 更新记录状态为 processing
        if record_id:
            self.db.update_retry_status(record_id, 'processing')

        # 检查文件是否存在
        if not os.path.exists(file_path):
            error_msg = f"文件不存在: {file_path}"
            self._send_log(f"错误: {error_msg}")
            self._handle_failure(file_name, file_path, folder_name, school, grade,
                                 subject or "未知", error_msg, UploadStage.READ_FILE,
                                 ErrorCategory.FILE_PROCESS_ERROR, ErrorType.FILE_NOT_EXIST,
                                 error_context, existing_record_id=record_id or agent_retry_record_id)
            return

        # 读取文件内容用于科目识别
        current_stage = UploadStage.READ_FILE
        self.heartbeat.beat(current_stage.value, file_path)
        file_content = self.info_extractor.read_file_content(file_path)
        if not file_content:
            self._send_log("警告: 无法读取文件内容或文件格式不支持")

        # AI科目识别（用户未传subject时自动识别）
        current_stage = UploadStage.AI_CLASSIFY
        self.heartbeat.beat(current_stage.value, file_path)
        if not subject:
            self._send_log("正在AI识别科目...")
            subject = self.classifier.classify(file_content, file_name=file_name)
            if not subject:
                subject = "未知"
                self._send_log(f"警告: 科目识别失败,标记为'未知'")
            else:
                self._send_log(f"识别结果: {subject}")
        else:
            self._send_log(f"科目: {subject}")

        self.processing = True

        try:
            upload_success, failed_stage, already_handled = self._execute_browser_upload(
                file_name, file_path, folder_name, school, grade, subject,
                error_context, is_agent_retry=is_agent_retry,
                agent_retry_record_id=agent_retry_record_id,
                agent_retry_level=agent_retry_level
            )
        finally:
            self.processing = False
            self.heartbeat.clear()
            if source == 'desktop':
                self._notify_agent_result(file_path, upload_success)

        if upload_success:
            if source == 'miniprogram':
                if record_id:
                    self.db.mark_record_success(record_id)
                    self.db.update_retry_status(record_id, 'finished')
                self.db.resolve_pending_by_file(file_name, folder_name)
                self.db.add_analysis_record(
                    file_name=file_name, file_path=file_path, folder_name=folder_name,
                    school=school, grade=grade, subject=subject, status='success'
                )
                self._send_log(f"✓ [{source}] 上传成功: {file_name} ({subject})")
                if self.auto_retry_agent is not None:
                    self.auto_retry_agent.on_any_upload_success()
                self._update_upload_counter()
            else:
                self._on_upload_success(file_name, file_path, folder_name, school, grade, subject,
                                        is_agent_retry)
                self._update_upload_counter()
        elif already_handled:
            # _execute_browser_upload 内部已调用 _handle_failure，此处仅补状态
            if record_id:
                self.db.update_retry_status(record_id, 'pending')
            self._send_log("REFRESH_FAILED_LIST")
        else:
            # 通用失败处理
            current_stage = failed_stage
            # 优先读取看门狗强制打断原因（读后清空，避免污染后续任务），
            # 不用 last_upload_error 是因为 upload_file 的兕底 except 会用
            # Selenium 异常文本覆盖它，导致 [WATCHDOG] 标记丢失
            watchdog_reason = getattr(self.browser, 'watchdog_interrupt_reason', '')
            if watchdog_reason:
                self.browser.watchdog_interrupt_reason = ""
                last_error = watchdog_reason
            else:
                last_error = getattr(self.browser, 'last_upload_error', '')
            if self._is_school_not_activated_error(last_error):
                error_msg = f"学校未开通数智作业服务: {school}"
            elif last_error and last_error.strip():
                error_msg = self._clean_error_marker(last_error)
            else:
                error_msg = "上传操作失败(详见浏览器日志)"
            self._send_log(f"✗ [{source}] 上传失败: {file_name} - {error_msg}")

            if record_id:
                self.db.update_retry_status(record_id, 'pending')
                self.db.update_record_structured_error(
                    record_id,
                    error_message=error_msg,
                    fail_stage=current_stage.value if current_stage else None
                )
                self.db.mark_record_failed(record_id)
            else:
                error_cat, error_type = self._classify_browser_error(error_msg) if last_error else \
                    (ErrorCategory.BROWSER_ERROR, ErrorType.UPLOAD_SUBMIT_TIMEOUT)
                self._handle_failure(file_name, file_path, folder_name, school, grade,
                                     subject, error_msg, current_stage,
                                     error_cat, error_type, error_context)
            self._send_log("REFRESH_FAILED_LIST")

    def _process_miniprogram_task(self, task: dict):
        """
        处理小程序提交的结构化任务 → 委托 _process_task

        任务格式: {file_path, school, grade, subject (可选), record_id}
        """
        file_path = task['file_path']
        file_name = task.get('original_name') or os.path.basename(file_path)

        self._process_task({
            'file_name': file_name,
            'file_path': file_path,
            'folder_name': 'miniprogram',
            'school': task['school'],
            'grade': task['grade'],
            'subject': task.get('subject'),
            'record_id': task.get('record_id'),
            'is_agent_retry': task.get('is_retry', False),
            'agent_retry_record_id': task.get('record_id'),
            'agent_retry_level': None,
            'source': 'miniprogram',
        })

    # ─── 错误分类辅助方法 ───

    @classmethod
    def _is_session_lost_error(cls, error_text: str) -> bool:
        """检测错误是否由会话丢失（账号被踢下线）引起"""
        if not error_text:
            return False
        from error_types import SESSION_LOST_KEYWORDS
        return any(kw in error_text for kw in SESSION_LOST_KEYWORDS)

    @classmethod
    def _classify_browser_error(cls, error_text: str) -> tuple:
        """
        根据浏览器错误信息推断 ErrorType，替代硬编码的 UPLOAD_SUBMIT_TIMEOUT

        Returns:
            (ErrorCategory, ErrorType) 元组
        """
        if not error_text:
            return (ErrorCategory.BROWSER_ERROR, ErrorType.UPLOAD_SUBMIT_TIMEOUT)

        if cls._is_session_lost_error(error_text):
            return (ErrorCategory.BROWSER_ERROR, ErrorType.LOGIN_EXPIRED)
        if "[WATCHDOG]" in error_text or "流水线卡死" in error_text:
            # 看门狗强制打断 → 流水线卡死（需 L4 完整恢复）
            return (ErrorCategory.BROWSER_ERROR, ErrorType.PIPELINE_STUCK)
        if "只能上传一个文件" in error_text:
            # 可能是级联错误（上一文件因会话丢失已提交但响应丢失），
            # 也可能是真正的重复提交，先归类为表单校验失败
            return (ErrorCategory.BROWSER_ERROR, ErrorType.FORM_VALIDATE_FAIL)
        if "学校未开通" in error_text or "数智作业服务" in error_text:
            return (ErrorCategory.PLATFORM_BIZ_ERROR, ErrorType.SCHOOL_NOT_ACTIVATED)
        if "权限" in error_text or "无权" in error_text:
            return (ErrorCategory.PLATFORM_BIZ_ERROR, ErrorType.PERMISSION_DENIED)

        return (ErrorCategory.BROWSER_ERROR, ErrorType.UPLOAD_SUBMIT_TIMEOUT)

    @staticmethod
    def _is_school_not_activated_error(error_text: str) -> bool:
        """检查错误文本是否为'学校未开通数智作业服务'"""
        return "[SCHOOL_NOT_ACTIVATED]" in error_text or "该校未开通数智作业服务" in error_text

    @staticmethod
    def _clean_error_marker(error_text: str) -> str:
        """清理内部标记前缀（如 [SCHOOL_NOT_ACTIVATED]），对外展示用"""
        return error_text.replace("[SCHOOL_NOT_ACTIVATED] ", "") if error_text else ""

    def _handle_failure(self, file_name: str, file_path: str, folder_name: str,
                       school: str, grade: str, subject: str, error_message: str,
                       fail_stage: UploadStage = None,
                       error_category: ErrorCategory = None,
                       error_type: ErrorType = None,
                       error_context: dict = None,
                       existing_record_id: int = None):
        """
        处理上传失败的情况（结构化版本）

        Args:
            file_name: 文件名
            file_path: 文件路径
            folder_name: 文件夹名称
            school: 学校
            grade: 年级
            subject: 科目
            error_message: 错误信息
            fail_stage: 失败阶段
            error_category: 错误一级分类
            error_type: 错误二级类型
            error_context: 错误上下文字典
            existing_record_id: Agent 重试对应的数据库记录ID（finally 之后调用时必传；
                                其他时机可选，方法内部会回退到 _agent_retry_map 查找）
        """
        # Agent 触发的重试：更新已有记录而非创建新记录
        # 优先使用传入的 record_id（finally 弹出 map 后仍可用），回退到 map 查找
        record_id = existing_record_id or self._agent_retry_map.get(file_path)
        if record_id is not None:
            self.db.update_record_structured_error(
                record_id,
                error_message=error_message,
                fail_stage=fail_stage.value if fail_stage else None,
                error_category=error_category.value if error_category else None,
                error_type=error_type.value if error_type else None,
                error_context=json.dumps(error_context, ensure_ascii=False) if error_context else None
            )
            # 防御性设置：确保状态不会卡在 processing（正常流程由 _notify_agent_result 兜底）
            self.db.update_retry_status(record_id, 'pending')
            # 关键：将 status 从 pending 改为 failed，否则小程序轮询永远看不到失败，
            # AutoRetryAgent 也不会接管
            self.db.mark_record_failed(record_id)
        else:
            # 首次失败：创建新记录
            self.db.add_failed_record_structured(
                file_name=file_name,
                file_path=file_path,
                folder_name=folder_name,
                school=school,
                grade=grade,
                subject=subject,
                error_message=error_message,
                fail_stage=fail_stage.value if fail_stage else None,
                error_category=error_category.value if error_category else None,
                error_type=error_type.value if error_type else None,
                error_context=json.dumps(error_context, ensure_ascii=False) if error_context else None
            )

        # 同步写入分析表（首次失败才写入，重试时避免重复记录）
        if not existing_record_id:
            self.db.add_analysis_record(
                file_name=file_name,
                file_path=file_path,
                folder_name=folder_name,
                school=school,
                grade=grade,
                subject=subject,
                status='failed',
                error_message=error_message
            )

        # 失败落库后立即唤醒 Agent（替代纯轮询，秒级接管）
        if self.auto_retry_agent is not None and hasattr(self.auto_retry_agent, 'wake'):
            self.auto_retry_agent.wake()

        # 向GUI发送刷新失败列表的信号
        self._send_log("REFRESH_FAILED_LIST")
    
    def retry_upload(self, record_id: int, file_path: str):
        """
        重新上传失败的文件（含阶段埋点）
        支持桌面端（从文件夹名解析学校/年级）和小程序（从数据库记录读取学校/年级）

        Args:
            record_id: 数据库记录ID
            file_path: 文件路径
        """
        file_name = os.path.basename(file_path)
        folder_path = os.path.dirname(file_path)
        folder_name = os.path.basename(folder_path)
        current_stage = None
        error_context = {}

        self._send_log(f"重新上传文件: {file_name}")
        self.heartbeat.beat(UploadStage.PARSE_FOLDER.value, file_path)

        # 先标记为 processing 防止 Agent 并发重试
        self.db.update_retry_status(record_id, 'processing')

        # 增加重试次数
        retry_count = self.db.increment_retry(record_id)

        # 检查是否超过最大重试次数
        if retry_count > self.config.max_retry_count:
            error_msg = f"已达到最大重试次数({self.config.max_retry_count})"
            self._send_log(f"错误: {error_msg}")
            self.db.update_record_structured_error(
                record_id, error_message=error_msg,
                fail_stage=UploadStage.SUBMIT_UPLOAD.value)
            self.db.update_retry_status(record_id, 'finished')
            self._send_log("REFRESH_FAILED_LIST")
            self.heartbeat.clear()
            return

        # 解析学校/年级：小程序任务优先从数据库记录读取（文件夹名无法解析）
        current_stage = UploadStage.PARSE_FOLDER
        record = self.db.get_record_by_id(record_id)
        db_school = (record.get('school', '') or '').strip() if record else ''
        db_grade = (record.get('grade', '') or '').strip() if record else ''
        is_miniprogram = (record.get('source', '') == 'miniprogram') if record else False

        if is_miniprogram and db_school and db_grade:
            school, grade = db_school, db_grade
            self._send_log(f"小程序任务: 使用数据库中的学校/年级 - {school}/{grade}")
        else:
            school, grade = self.info_extractor.parse_folder_name(folder_path)
            if not school and db_school:
                school = db_school
            if not grade and db_grade:
                grade = db_grade

        if not school or not grade:
            error_msg = "无法解析文件夹名称"
            self.db.update_record_structured_error(
                record_id, error_message=error_msg,
                fail_stage=current_stage.value,
                error_category=ErrorCategory.FILE_PROCESS_ERROR.value,
                error_type=ErrorType.FILE_UNREADABLE.value)
            self.db.update_retry_status(record_id, 'finished')
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            self.heartbeat.clear()
            return

        # 读取文件内容并识别科目
        current_stage = UploadStage.READ_FILE
        self.heartbeat.beat(current_stage.value, file_path)
        file_content = self.info_extractor.read_file_content(file_path)

        current_stage = UploadStage.AI_CLASSIFY
        self.heartbeat.beat(current_stage.value, file_path)
        subject = self.classifier.classify(file_content, file_name=file_name)

        if not subject:
            subject = "未知"

        # 确保浏览器已启动（延迟初始化）
        current_stage = UploadStage.BROWSER_INIT
        self.heartbeat.beat(current_stage.value, file_path)
        if not self.browser.ensure_initialized():
            error_msg = "浏览器启动失败"
            self.db.update_record_structured_error(
                record_id, error_message=error_msg,
                fail_stage=current_stage.value,
                error_category=ErrorCategory.BROWSER_ERROR.value,
                error_type=ErrorType.BROWSER_START_FAIL.value)
            self.db.update_retry_status(record_id, 'pending')
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            self.heartbeat.clear()
            return

        # 检查浏览器状态
        if not self.browser.check_browser_status():
            self.browser.restart_browser()

        # 校验学校(每次上传前都实际检查网页上的学校)
        current_stage = UploadStage.SCHOOL_CHECK
        self.heartbeat.beat(current_stage.value, file_path)
        if not self.browser.check_and_switch_school(school):
            # 检查是否因学校未开通导致
            page_info = self.browser.get_page_text()
            if page_info and page_info.get("success") and "该校未开通数智作业服务" in str(page_info.get("text", "")):
                error_msg = f"学校未开通数智作业服务: {school}"
                self.db.update_record_structured_error(
                    record_id, error_message=error_msg,
                    fail_stage=current_stage.value,
                    error_category=ErrorCategory.PLATFORM_BIZ_ERROR.value,
                    error_type=ErrorType.SCHOOL_NOT_ACTIVATED.value)
                self.db.update_retry_status(record_id, 'finished')
            else:
                error_msg = "学校校验/切换失败"
                self.db.update_record_structured_error(
                    record_id, error_message=error_msg,
                    fail_stage=current_stage.value,
                    error_category=ErrorCategory.BROWSER_ERROR.value,
                    error_type=ErrorType.SCHOOL_SWITCH_FAIL.value)
                self.db.update_retry_status(record_id, 'pending')
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            self.heartbeat.clear()
            return

        # 执行上传
        current_stage = UploadStage.SUBMIT_UPLOAD
        self.heartbeat.beat(current_stage.value, file_path)
        upload_success = self.browser.upload_file(file_path, grade, subject, school)

        if upload_success:
            # 更新记录为成功
            self.db.mark_record_success(record_id)
            self.db.update_retry_status(record_id, 'finished')
            # 同步写入分析表
            self.db.add_analysis_record(
                file_name=file_name,
                file_path=file_path,
                folder_name=folder_name,
                school=school,
                grade=grade,
                subject=subject,
                status='success'
            )
            self._send_log(f"✓ 重新上传成功: {file_name}")
        else:
            # 更新错误信息：优先读取看门狗强制打断原因（读后清空），再检查是否为学校未开通错误
            watchdog_reason = getattr(self.browser, 'watchdog_interrupt_reason', '')
            if watchdog_reason:
                self.browser.watchdog_interrupt_reason = ""
                last_error = watchdog_reason
            else:
                last_error = getattr(self.browser, 'last_upload_error', '')
            if self._is_school_not_activated_error(last_error):
                error_msg = f"学校未开通数智作业服务: {school}"
                self.db.update_record_structured_error(
                    record_id, error_message=error_msg,
                    fail_stage=current_stage.value,
                    error_category=ErrorCategory.PLATFORM_BIZ_ERROR.value,
                    error_type=ErrorType.SCHOOL_NOT_ACTIVATED.value)
                self.db.update_retry_status(record_id, 'finished')
            else:
                # 优先使用网页实际错误信息，并根据错误文本推断错误类型
                error_msg = self._clean_error_marker(last_error) if last_error and last_error.strip() else "重新上传失败"
                error_cat, error_type = self._classify_browser_error(error_msg)
                self.db.update_record_structured_error(
                    record_id, error_message=error_msg,
                    fail_stage=current_stage.value,
                    error_category=error_cat.value,
                    error_type=error_type.value)
                self.db.update_retry_status(record_id, 'pending')
            self._send_log(f"✗ 重新上传失败: {file_name} - {error_msg}")

        # 刷新失败列表
        self._send_log("REFRESH_FAILED_LIST")
        self.heartbeat.clear()
    
    def _send_log(self, message: str):
        """
        向日志队列发送消息，同时写入近期日志缓冲（供 Agent 回看）
        
        Args:
            message: 日志消息
        """
        RECENT_LOGS.append(message)
        try:
            self.log_queue.put(message)
        except Exception as e:
            print(f"发送日志失败: {e}")
