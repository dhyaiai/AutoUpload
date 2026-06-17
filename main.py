"""
主控制模块
功能:协调所有模块,启动GUI和后台线程,管理程序生命周期
特点:多线程架构,线程安全通信,优雅退出机制
"""
import sys
import os
import time
import threading
import traceback
from queue import Queue
import tkinter as tk

# 导入各个功能模块
from db_manager import DatabaseManager
from config_manager import ConfigManager
from file_monitor import FileMonitor
from upload_processor import UploadProcessor
from browser_automation import BrowserAutomation
from gui_manager import MainApplication


def backend_worker(stop_event: threading.Event, task_queue: Queue,
                   log_queue: Queue, upload_processor: UploadProcessor):
    """
    后台工作线程函数
    负责启动文件监控、按需启动浏览器、处理上传任务

    优化: 浏览器延迟初始化 — 启动时只监控文件夹,
    只有当检测到新文件时才会打开浏览器进行上传。

    Args:
        stop_event: 停止信号
        task_queue: 任务队列
        log_queue: 日志队列
        upload_processor: 共享的上传处理器实例
    """
    try:
        # 步骤1: 初始化数据库
        log_queue.put("正在初始化数据库...")
        db = DatabaseManager()
        log_queue.put("数据库初始化成功")

        # 步骤2: 启动文件监控（不启动浏览器，后台静默运行）
        log_queue.put("正在启动文件监控...")
        file_monitor = FileMonitor(task_queue, stop_event)
        file_monitor.start()
        log_queue.put("文件监控已启动,等待新文件...")
        log_queue.put("BROWSER_STATUS:DISCONNECTED")

        # 步骤3: 在后台线程中运行上传处理器
        # 上传处理器会在收到第一个文件时自动启动浏览器
        processor_thread = threading.Thread(target=upload_processor.run, daemon=True)
        processor_thread.start()

        # 步骤4: 进入主循环,仅在浏览器运行时检查其状态
        browser = BrowserAutomation(log_queue=log_queue)
        browser_error_logged = False
        while not stop_event.is_set():
            time.sleep(5)  # 每5秒检查一次

            # 浏览器未启动时跳过所有检查
            if not browser.is_initialized:
                browser_error_logged = False
                continue

            # 检查浏览器空闲超时
            if browser.is_idle_timeout():
                log_queue.put("检测到浏览器空闲超时,正在关闭...")
                browser.close()
                browser_error_logged = False

            # 检查浏览器是否仍然可用
            elif not browser.check_browser_status():
                if not browser_error_logged:
                    log_queue.put("警告: 浏览器异常关闭,尝试重启...")
                    browser_error_logged = True
                if browser.restart_browser():
                    browser_error_logged = False
                # restart_browser() 内部会发送 BROWSER_STATUS 消息

            # 检查登录状态
            elif not browser.check_login_status():
                if not browser_error_logged:
                    log_queue.put("警告: 登录态失效,尝试重新登录...")
                    browser_error_logged = True
                if browser.restart_browser():
                    browser_error_logged = False

            # 浏览器正常时重置标记
            else:
                browser_error_logged = False

        # 步骤5: 收到停止信号,等待任务队列清空
        log_queue.put("收到停止信号,正在等待任务完成...")
        task_queue.join()  # 等待所有任务完成

        # 步骤6: 停止文件监控
        log_queue.put("正在停止文件监控...")
        file_monitor.stop()

        # 步骤7: 关闭浏览器（如果已启动）
        if browser.is_initialized:
            log_queue.put("正在关闭浏览器...")
            browser.close()

        # 步骤8: 关闭数据库
        log_queue.put("正在关闭数据库...")
        db.close()

        log_queue.put("后台服务已完全停止")

    except Exception as e:
        log_queue.put(f"后台工作线程异常: {e}")
        log_queue.put(traceback.format_exc())


def main():
    """
    主函数
    程序入口,启动所有组件
    """
    print("=" * 60)
    print("作业自动上传工具 v1.0")
    print("=" * 60)

    # 创建停止信号
    stop_event = threading.Event()

    # 创建任务队列(用于存放待上传文件)
    task_queue = Queue()

    # 创建日志队列(用于后台向GUI发送日志)
    log_queue = Queue()

    # 创建共享的上传处理器实例（唯一实例，供后台线程和GUI共同使用）
    upload_processor = UploadProcessor(task_queue, stop_event, log_queue)

    worker_thread = None
    try:
        # 启动后台工作线程
        print("启动后台服务线程...")
        worker_thread = threading.Thread(
            target=backend_worker,
            args=(stop_event, task_queue, log_queue, upload_processor),
            daemon=True
        )
        worker_thread.start()

        # 等待一下让后台服务初始化
        time.sleep(2)

        # 启动GUI主循环
        print("启动图形界面...")
        root = tk.Tk()

        # 创建主应用窗口
        app = MainApplication(root, stop_event, task_queue, log_queue, upload_processor)

        # 进入tkinter主循环
        print("程序已启动,请在GUI中进行操作")
        root.mainloop()

    except KeyboardInterrupt:
        print("\n用户中断程序")
        stop_event.set()

    except Exception as e:
        print(f"程序异常: {e}")
        traceback.print_exc()
        stop_event.set()

    finally:
        # 确保程序退出时清理资源
        print("正在退出程序...")
        stop_event.set()

        # 等待后台线程结束
        if worker_thread is not None:
            worker_thread.join(timeout=10)

        print("程序已退出")


if __name__ == "__main__":
    main()
