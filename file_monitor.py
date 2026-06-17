"""
文件监控模块
功能:使用watchdog库监控文件夹中的新增文件,将任务放入队列
特点:异步事件驱动,支持文件稳定等待,避免读取未完成文件
"""
import os
import time
import threading
from queue import Queue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from config_manager import ConfigManager


class FileMonitorHandler(FileSystemEventHandler):
    """
    文件系统事件处理器
    继承自watchdog的FileSystemEventHandler,重写文件创建事件
    """
    
    def __init__(self, task_queue: Queue, stop_event: threading.Event):
        """
        初始化事件处理器
        
        Args:
            task_queue: 任务队列,用于存放新发现的文件路径
            stop_event: 停止信号,用于优雅退出监控
        """
        super().__init__()
        self.task_queue = task_queue
        self.stop_event = stop_event
        self.config = ConfigManager()
    
    def on_created(self, event):
        """
        文件创建事件回调
        当监控到有新文件创建时触发
        
        Args:
            event: 文件系统事件对象
        """
        # 如果收到停止信号,不再处理新事件
        if self.stop_event.is_set():
            return
        
        # 忽略目录创建事件,只处理文件
        if event.is_directory:
            return
        
        file_path = event.src_path
        
        # 过滤根目录下的直接文件(只处理子文件夹中的文件)
        root_dir = os.path.abspath(self.config.root_dir)
        file_dir = os.path.dirname(os.path.abspath(file_path))
        
        # 如果文件直接在根目录下,跳过
        if file_dir == root_dir:
            print(f"忽略根目录下的文件: {file_path}")
            return
        
        print(f"检测到新文件: {file_path}")
        
        # 延迟等待文件写入完成(避免读取未完成的文件)
        time.sleep(self.config.file_stable_delay)
        
        # 再次检查文件是否存在(防止文件被快速删除)
        if os.path.exists(file_path):
            print(f"文件已稳定,加入任务队列: {file_path}")
            self.task_queue.put(file_path)
        else:
            print(f"文件已不存在,跳过: {file_path}")


class FileMonitor:
    """
    文件监控器
    负责启动和停止watchdog观察者
    """
    
    def __init__(self, task_queue: Queue, stop_event: threading.Event):
        """
        初始化文件监控器
        
        Args:
            task_queue: 任务队列
            stop_event: 停止信号
        """
        self.task_queue = task_queue
        self.stop_event = stop_event
        self.config = ConfigManager()
        self.observer = None
    
    def start(self):
        """
        启动文件监控
        创建Observer并注册事件处理器,开始递归监控根目录
        """
        try:
            # 确保监控目录存在
            root_dir = self.config.root_dir
            if not os.path.exists(root_dir):
                print(f"创建监控目录: {root_dir}")
                os.makedirs(root_dir, exist_ok=True)
            
            # 创建事件处理器
            event_handler = FileMonitorHandler(self.task_queue, self.stop_event)
            
            # 创建观察者
            self.observer = Observer()
            
            # 注册监控:递归监控根目录
            self.observer.schedule(event_handler, root_dir, recursive=True)
            
            # 启动观察者
            self.observer.start()
            print(f"文件监控已启动,监控目录: {os.path.abspath(root_dir)}")
        
        except Exception as e:
            print(f"错误: 启动文件监控失败 - {e}")
            raise
    
    def stop(self):
        """
        停止文件监控
        等待所有事件处理完成后关闭观察者
        """
        if self.observer:
            print("正在停止文件监控...")
            self.observer.stop()
            self.observer.join(timeout=5)  # 最多等待5秒
            print("文件监控已停止")
    
    def is_running(self) -> bool:
        """
        检查监控是否正在运行
        
        Returns:
            True表示正在运行,False表示已停止
        """
        return self.observer is not None and self.observer.is_alive()
