"""
上传处理器模块
功能:从任务队列中取出文件,执行完整的上传流程
特点:单线程顺序处理,防止科目混淆,支持失败重试
"""
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
        
        Args:
            file_path: 文件的完整路径
        """
        file_name = os.path.basename(file_path)
        folder_path = os.path.dirname(file_path)
        folder_name = os.path.basename(folder_path)
        
        self._send_log(f"开始处理文件: {file_name}")
        
        # 步骤1: 检查是否已经上传过(防重复,按文件名+文件夹名精确匹配)
        if self.db.is_file_uploaded(file_name, folder_name):
            self._send_log(f"文件已上传过,跳过: {file_name}")
            return
        
        # 步骤2: 解析学校和年级
        school, grade = self.info_extractor.parse_folder_name(folder_path)
        
        if not school or not grade:
            error_msg = f"无法解析文件夹名称: {folder_name}"
            self._send_log(f"错误: {error_msg}")
            self._handle_failure(file_name, file_path, folder_name, "未知", "未知", "未知", error_msg)
            return
        
        self._send_log(f"解析成功 - 学校: {school}, 年级: {grade}")
        
        # 步骤3: 读取文件内容
        file_content = self.info_extractor.read_file_content(file_path)
        
        if not file_content:
            error_msg = "无法读取文件内容或文件格式不支持"
            self._send_log(f"警告: {error_msg}")
            # 继续处理,但科目标记为"未知"
        
        # 步骤4: AI识别科目
        self._send_log("正在识别科目...")
        subject = self.classifier.classify(file_content) if file_content else None
        
        if not subject:
            subject = "未知"
            self._send_log(f"警告: 科目识别失败,标记为'未知'")
        else:
            self._send_log(f"识别结果: {subject}")
        
        # 标记处理中，防止后端在任务处理期间误关浏览器
        self.processing = True
        try:
            # 步骤5: 确保浏览器已启动（延迟初始化，首次调用时才打开浏览器）
            if not self.browser.ensure_initialized():
                error_msg = "浏览器启动失败"
                self._send_log(f"错误: {error_msg}")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject, error_msg)
                return

            # 检查浏览器是否崩溃
            if not self.browser.check_browser_status():
                self._send_log("检测到浏览器异常,正在重启...")
                if not self.browser.restart_browser():
                    error_msg = "浏览器重启失败"
                    self._send_log(f"错误: {error_msg}")
                    self._handle_failure(file_name, file_path, folder_name, school, grade, subject, error_msg)
                    return

            # 检查登录状态
            if not self.browser.check_login_status():
                self._send_log("检测到登录失效,正在重新登录...")
                if not self.browser.restart_browser():
                    error_msg = "重新登录失败"
                    self._send_log(f"错误: {error_msg}")
                    self._handle_failure(file_name, file_path, folder_name, school, grade, subject, error_msg)
                    return

            # 步骤6: 校验并切换学校(每次上传前都实际检查网页上的学校)
            self._send_log(f"正在校验学校: {school}")
            if not self.browser.check_and_switch_school(school):
                error_msg = f"学校校验/切换失败: {school}"
                self._send_log(f"错误: {error_msg}")
                self._handle_failure(file_name, file_path, folder_name, school, grade, subject, error_msg)
                return
            self._send_log(f"✓ 学校校验通过: {school}")

            # 步骤7: 执行上传(传递学校参数,用于在上传对话框中选择)
            self._send_log(f"正在上传到平台...")
            upload_success = self.browser.upload_file(file_path, grade, subject, school)
        finally:
            self.processing = False

        if upload_success:
            # 上传成功,记录到数据库
            self.db.add_record(
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
            error_msg = "上传操作失败(详见浏览器日志)"
            self._send_log(f"✗ 上传失败: {file_name} - {error_msg}")
            self._handle_failure(file_name, file_path, folder_name, school, grade, subject, error_msg)
    
    def _handle_failure(self, file_name: str, file_path: str, folder_name: str,
                       school: str, grade: str, subject: str, error_message: str):
        """
        处理上传失败的情况
        
        Args:
            file_name: 文件名
            file_path: 文件路径
            folder_name: 文件夹名称
            school: 学校
            grade: 年级
            subject: 科目
            error_message: 错误信息
        """
        # 添加失败记录到数据库
        record_id = self.db.add_record(
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
        重新上传失败的文件
        
        Args:
            record_id: 数据库记录ID
            file_path: 文件路径
        """
        file_name = os.path.basename(file_path)
        folder_path = os.path.dirname(file_path)
        folder_name = os.path.basename(folder_path)
        
        self._send_log(f"重新上传文件: {file_name}")
        
        # 增加重试次数
        retry_count = self.db.increment_retry(record_id)
        
        # 检查是否超过最大重试次数
        if retry_count > self.config.max_retry_count:
            error_msg = f"已达到最大重试次数({self.config.max_retry_count})"
            self._send_log(f"错误: {error_msg}")
            self.db.update_error_message(record_id, error_msg)
            self._send_log("REFRESH_FAILED_LIST")
            return
        
        # 重新执行上传流程(简化版,假设学校年级科目已知)
        # TODO: 如果需要,可以从数据库读取之前的信息
        school, grade = self.info_extractor.parse_folder_name(folder_path)
        
        if not school or not grade:
            error_msg = "无法解析文件夹名称"
            self.db.update_error_message(record_id, error_msg)
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            return
        
        # 读取文件内容并识别科目
        file_content = self.info_extractor.read_file_content(file_path)
        subject = self.classifier.classify(file_content) if file_content else "未知"
        
        if not subject:
            subject = "未知"
        
        # 确保浏览器已启动（延迟初始化）
        if not self.browser.ensure_initialized():
            error_msg = "浏览器启动失败"
            self.db.update_error_message(record_id, error_msg)
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            return

        # 检查浏览器状态
        if not self.browser.check_browser_status():
            self.browser.restart_browser()

        # 校验学校(每次上传前都实际检查网页上的学校)
        if not self.browser.check_and_switch_school(school):
            error_msg = "学校校验/切换失败"
            self.db.update_error_message(record_id, error_msg)
            self._send_log(f"错误: {error_msg}")
            self._send_log("REFRESH_FAILED_LIST")
            return
        
        # 执行上传
        upload_success = self.browser.upload_file(file_path, grade, subject, school)
        
        if upload_success:
            # 更新记录为成功
            self.db.mark_record_success(record_id)
            self._send_log(f"✓ 重新上传成功: {file_name}")
        else:
            # 更新错误信息
            error_msg = "重新上传失败"
            self.db.update_error_message(record_id, error_msg)
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
