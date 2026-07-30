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

# 导入各个功能模块
from db_manager import DatabaseManager
from config_manager import ConfigManager
from file_monitor import FileMonitor
from upload_processor import UploadProcessor
from browser_automation import BrowserAutomation
from gui_manager import MainApplication
from auto_retry_agent import AutoRetryAgent
from pipeline_watchdog import PipelineWatchdog


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

        # 步骤3.5: 启动失败自动接管 Agent（AutoRetryAgent）
        auto_retry_agent = AutoRetryAgent(task_queue, stop_event, log_queue)
        # 双向绑定：Agent ↔ UploadProcessor
        auto_retry_agent.set_upload_processor(upload_processor)
        upload_processor.set_agent(auto_retry_agent)
        retry_agent_thread = threading.Thread(target=auto_retry_agent.run, daemon=True)
        retry_agent_thread.start()

        # 步骤4: 使用 BrowserAutomation 共享的浏览器空闲监控器（daemon 线程）
        browser = BrowserAutomation(log_queue=log_queue)
        BrowserAutomation.start_idle_monitor(
            stop_event, task_queue, log_queue, upload_processor, db, browser=browser)

        # 步骤4.5: 启动流水线看门狗（卡死检测，daemon 线程）
        watchdog = PipelineWatchdog(
            upload_processor.heartbeat, browser, upload_processor,
            auto_retry_agent, log_queue, stop_event)
        watchdog_thread = threading.Thread(
            target=watchdog.run, daemon=True, name="PipelineWatchdog")
        watchdog_thread.start()

        # 步骤5: 等待停止信号（监控器在独立 daemon 线程运行）
        while not stop_event.is_set():
            stop_event.wait(5)

        # 步骤6: 收到停止信号,等待任务队列清空(最多等30秒防止无限阻塞)
        log_queue.put("收到停止信号,正在等待任务完成...")
        drain_deadline = time.time() + 30
        while not task_queue.empty() and time.time() < drain_deadline:
            time.sleep(0.5)
        if not task_queue.empty():
            log_queue.put(f"警告: 等待超时,队列中仍有{task_queue.qsize()}个未完成任务,强制退出")

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

    支持两种模式:
    - python main.py            → GUI 桌面模式
    - python main.py --api-only → 纯 API 服务模式（无 GUI）
    """
    # API-only 模式
    if "--api-only" in sys.argv:
        from api_server import run_api_server
        run_api_server()
        return

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
        # 创建现代化根窗口（CTk圆角控件 + 文件拖拽支持）
        from ui_theme import create_root
        root = create_root()

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
            worker_thread.join(timeout=15)
            if worker_thread.is_alive():
                print("警告: 后台线程未能在15秒内退出, 强制清理浏览器进程...")
                # 强制关闭浏览器，防止 ChromeDriver/Chrome 进程残留
                try:
                    browser = BrowserAutomation()
                    browser.close()
                except Exception:
                    pass

        print("程序已退出")


if __name__ == "__main__":
    main()
