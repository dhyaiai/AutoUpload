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
from db_manager import DatabaseManager
from info_extractor import InfoExtractor
from subject_classifier import SubjectClassifier
from browser_automation import BrowserAutomation
from config_manager import ConfigManager
from error_types import UploadStage, ErrorCategory, ErrorType, RetryLevel


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

        # Agent 回调相关
        self.auto_retry_agent = None           # AutoRetryAgent 引用
        self._agent_retry_map: dict = {}       # file_path -> record_id (Agent触发的重试)
        self._agent_retry_level_map: dict = {} # file_path -> retry_level (重试自愈级别)

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
        record_id = self._agent_retry_map.pop(file_path)
        # 同步清理 level map
        self._agent_retry_level_map.pop(file_path, None)
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
            if not self.browser.reset_to_home():
                self._send_log(f"Agent重试: {level.value}浏览器复位失败，继续尝试上传流程")
                # 非致命：reset_to_home 内部已尝试 restart_browser，
                # 后续 ensure_initialized 还会再次尝试恢复
            else:
                self._send_log(f"Agent重试: {level.value}浏览器复位完成")

        elif level == RetryLevel.L4_SERVICE_RESTART:
            self._send_log(f"Agent重试: L4 已在Agent中重启浏览器，跳过预处理")

        # L1 和 L5 无需预处理
        return True

    def run(self):
        """
        主运行循环
        持续从任务队列中取出文件并处理,直到收到停止信号
        """
        print("上传处理器已启动")
        
        while not self.stop_event.is_set():
            try:
                # 从队列中获取任务(超时1秒,以便检查停止信号)
                file_path = self.task_queue.get(timeout=1)
                
                # 处理文件上传
                self._process_file(file_path)
                
                # 标记任务完成
                self.task_queue.task_done()
            
            except Empty:
                # 队列为空,继续循环
                continue
            
            except Exception as e:
                # 捕获未预期的异常,确保程序不崩溃
                error_msg = f"上传处理器异常: {e}"
                print(error_msg)
                self._send_log(error_msg)
        
        print("上传处理器已停止")
    
    def _process_file(self, file_path: str):
        """
        处理单个文件的完整上传流程
        每个阶段设置 _current_stage 用于失败时精准定位

        Args:
            file_path: 文件的完整路径
        """
        file_name = os.path.basename(file_path)
        folder_path = os.path.dirname(file_path)
        folder_name = os.path.basename(folder_path)

        self._send_log(f"开始处理文件: {file_name}")

        # 当前阶段变量（用于失败定位）
        current_stage = None
        error_context = {}

        # 步骤1: 检查是否已经上传过(防重复,按文件名+文件夹名精确匹配)
        if self.db.is_file_uploaded(file_name, folder_name):
            self._send_log(f"文件已上传过,跳过: {file_name}")
            # Agent 触发的重试：通知 Agent 已有成功记录
            self._notify_agent_result(file_path, True)
            return

        # 捕获 Agent 重试信息（必须在 finally 弹出 _agent_retry_map 之前保存；
        # 失败分支在 finally 之后执行，届时 map 已弹出，故需提前持有 record_id）
        is_agent_retry = file_path in self._agent_retry_map
        agent_retry_record_id = self._agent_retry_map.get(file_path)
        agent_retry_level = self._agent_retry_level_map.get(file_path)

        # 步骤2: 解析学校和年级
        current_stage = UploadStage.PARSE_FOLDER
        school, grade = self.info_extractor.parse_folder_name(folder_path)

        if not school or not grade:
            error_msg = f"无法解析文件夹名称: {folder_name}"
            self._send_log(f"错误: {error_msg}")
            self._handle_failure(file_name, file_path, folder_name, "未知", "未知", "未知",
                                 error_msg, current_stage,
                                 ErrorCategory.FILE_PROCESS_ERROR, ErrorType.FILE_UNREADABLE,
                                 error_context, existing_record_id=agent_retry_record_id)
            self._notify_agent_result(file_path, False)
            return

        self._send_log(f"解析成功 - 学校: {school}, 年级: {grade}")

        # 步骤3: 读取文件内容
        current_stage = UploadStage.READ_FILE
        error_context = {"folder_name": folder_name, "school": school, "grade": grade}
        file_content = self.info_extractor.read_file_content(file_path)

        if not file_content:
            error_msg = "无法读取文件内容或文件格式不支持"
            self._send_log(f"警告: {error_msg}")
            # 继续处理,但科目标记为"未知"

        # 步骤4: 识别科目(优先从文件名匹配,否则AI识别)
        current_stage = UploadStage.AI_CLASSIFY
        self._send_log("正在识别科目...")
        subject = self.classifier.classify(file_content, file_name=file_name) if file_content else self.classifier.extract_subject_from_filename(file_name)

        if not subject:
            subject = "未知"
            self._send_log(f"警告: 科目识别失败,标记为'未知'")
        else:
            self._send_log(f"识别结果: {subject}")

        # 标记处理中，防止后端在任务处理期间误关浏览器
        self.processing = True
        upload_success = False

        # Agent 重试预处理：在单线程内执行浏览器复位（L2/L3），避免与正常上传并发操作浏览器
        if is_agent_retry:
            if not self._preprocess_agent_retry(file_path):
                self._send_log("Agent重试: 预处理失败，放弃本次重试")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                     "Agent重试预处理失败: 浏览器无法恢复", UploadStage.BROWSER_INIT,
                                     ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL,
                                     error_context, existing_record_id=agent_retry_record_id)
                self.processing = False
                self._notify_agent_result(file_path, False)
                return

        try:
            # 步骤5: 确保浏览器已启动（延迟初始化，首次调用时才打开浏览器）
            current_stage = UploadStage.BROWSER_INIT
            if not self.browser.ensure_initialized():
                error_msg = "浏览器启动失败"
                self._send_log(f"错误: {error_msg}")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                     error_msg, current_stage,
                                     ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL,
                                     error_context, existing_record_id=agent_retry_record_id)
                return

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
                    return

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
                    return

            # 步骤6: 校验并切换学校(每次上传前都实际检查网页上的学校)
            current_stage = UploadStage.SCHOOL_CHECK
            self._send_log(f"正在校验学校: {school}")
            if not self.browser.check_and_switch_school(school):
                error_msg = f"学校校验/切换失败: {school}"
                self._send_log(f"错误: {error_msg}")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                     error_msg, current_stage,
                                     ErrorCategory.BROWSER_ERROR, ErrorType.SCHOOL_SWITCH_FAIL,
                                     error_context, existing_record_id=agent_retry_record_id)
                return
            self._send_log(f"✓ 学校校验通过: {school}")

            # 步骤7: 执行上传(传递学校参数,用于在上传对话框中选择)
            current_stage = UploadStage.SUBMIT_UPLOAD
            self._send_log(f"正在上传到平台...")
            upload_success = self.browser.upload_file(file_path, grade, subject, school)
        except Exception as e:
            # 兜底：捕获浏览器操作中未预期的异常，确保写入失败记录
            error_msg = f"浏览器操作异常: {e}"
            self._send_log(f"✗ 上传异常: {file_name} - {error_msg}")
            import traceback
            traceback.print_exc()
            upload_success = False
        finally:
            self.processing = False
            # Agent 回调：通知 AutoRetryAgent 上传结果
            self._notify_agent_result(file_path, upload_success)

        if upload_success:
            # 上传成功,记录到数据库
            if not is_agent_retry:
                # Agent 重试：原失败记录已由 on_upload_result 标记为 success，无需新建
                self.db.add_record(
                    file_name=file_name,
                    file_path=file_path,
                    folder_name=folder_name,
                    school=school,
                    grade=grade,
                    subject=subject,
                    status='success'
                )
            # 同步写入分析表，确保持久化统计（无论是否 Agent 重试均写入）
            self.db.add_analysis_record(
                file_name=file_name,
                file_path=file_path,
                folder_name=folder_name,
                school=school,
                grade=grade,
                subject=subject,
                status='success'
            )
            self._send_log(f"✓ 上传成功: {file_name} ({subject})")
        else:
            # 上传失败,记录失败信息
            current_stage = UploadStage.SUBMIT_UPLOAD
            error_msg = "上传操作失败(详见浏览器日志)"
            self._send_log(f"✗ 上传失败: {file_name} - {error_msg}")
            self._handle_failure(file_name, file_path, folder_name, school, grade, subject,
                                 error_msg, current_stage,
                                 ErrorCategory.BROWSER_ERROR, ErrorType.UPLOAD_SUBMIT_TIMEOUT,
                                 error_context, existing_record_id=agent_retry_record_id)

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

        # 同步写入分析表，确保失败记录在数据统计面板的失败记录表中可见
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

        # 向GUI发送刷新失败列表的信号
        self._send_log("REFRESH_FAILED_LIST")
    
    def retry_upload(self, record_id: int, file_path: str):
        """
        重新上传失败的文件（含阶段埋点）

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
            return

        # 重新执行上传流程
        current_stage = UploadStage.PARSE_FOLDER
        school, grade = self.info_extractor.parse_folder_name(folder_path)

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
            return

        # 读取文件内容并识别科目
        current_stage = UploadStage.READ_FILE
        file_content = self.info_extractor.read_file_content(file_path)

        current_stage = UploadStage.AI_CLASSIFY
        subject = self.classifier.classify(file_content, file_name=file_name) if file_content else self.classifier.extract_subject_from_filename(file_name)

        if not subject:
            subject = "未知"

        # 确保浏览器已启动（延迟初始化）
        current_stage = UploadStage.BROWSER_INIT
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
            return

        # 检查浏览器状态
        if not self.browser.check_browser_status():
            self.browser.restart_browser()

        # 校验学校(每次上传前都实际检查网页上的学校)
        current_stage = UploadStage.SCHOOL_CHECK
        if not self.browser.check_and_switch_school(school):
            error_msg = "学校校验/切换失败"
            self.db.update_record_structured_error(
                record_id, error_message=error_msg,
                fail_stage=current_stage.value,
                error_category=ErrorCategory.BROWSER_ERROR.value,
                error_type=ErrorType.SCHOOL_SWITCH_FAIL.value)
            self.db.update_retry_status(record_id, 'pending')
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            return

        # 执行上传
        current_stage = UploadStage.SUBMIT_UPLOAD
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
            # 更新错误信息
            error_msg = "重新上传失败"
            self.db.update_record_structured_error(
                record_id, error_message=error_msg,
                fail_stage=current_stage.value,
                error_category=ErrorCategory.BROWSER_ERROR.value,
                error_type=ErrorType.UPLOAD_SUBMIT_TIMEOUT.value)
            self.db.update_retry_status(record_id, 'pending')
            self._send_log(f"✗ 重新上传失败: {file_name} - {error_msg}")

        # 刷新失败列表
        self._send_log("REFRESH_FAILED_LIST")
    
    def _send_log(self, message: str):
        """
        向日志队列发送消息
        
        Args:
            message: 日志消息
        """
        try:
            self.log_queue.put(message)
        except Exception as e:
            print(f"发送日志失败: {e}")
