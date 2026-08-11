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

# 支持的文档扩展名（小写）
SUPPORTED_EXTS = ('.docx', '.doc', '.pdf', '.txt')

# 删除后同路径重建的"编辑器保存"抑制窗口(秒)。
# 部分软件(如 PDF 编辑器)保存 = 删旧写新: 先产生 on_deleted 再紧接着 on_created,
# 若不区分会被误判为新文件重复上传。窗口内的同路径重建视为编辑器保存,跳过;
# 超过窗口的删除后重建(教师删除上周作业、隔天放同名新作业)视为真正的新文件,照常入队。
RECREATE_SUPPRESS_SECONDS = 5.0


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
        # 墓碑表: 归一化路径 → 删除时间戳。
        # on_deleted 记录, on_created 用它区分"编辑器保存重建"(窗口内)
        # 与"删除后放置的真新文件"(超窗)。与旧的只增不减 _known_paths 不同,
        # 墓碑有过期机制: 删除超过窗口后同路径重建照常上传, 不会永久压制。
        self._tombstones = {}

    @staticmethod
    def _normalize(path: str) -> str:
        """路径归一化：绝对路径 + 小写（Windows 路径大小写不敏感）"""
        return os.path.abspath(path).lower()

    def on_deleted(self, event):
        """
        文件删除事件回调: 记录墓碑, 供 on_created 判断删除后重建是否
        属于编辑器保存(部分编辑器保存 = 删旧写新, 会产生删除+创建两个事件)。
        """
        if self.stop_event.is_set():
            return
        if event.is_directory:
            return
        self._tombstones[self._normalize(event.src_path)] = time.time()

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
        file_name = os.path.basename(file_path)

        # 过滤临时文件和系统文件
        if file_name.startswith('~$'):          # Office 锁文件 (~$xxx.docx)
            return
        if file_name.startswith('.'):           # 隐藏文件 (.DS_Store 等)
            return
        if file_name in ('Thumbs.db', 'desktop.ini'):  # Windows 系统文件
            return

        # 过滤不支持的文件扩展名（只接受文档格式）
        _, ext = os.path.splitext(file_name)
        if ext.lower() not in SUPPORTED_EXTS:
            print(f"忽略不支持的文件类型: {file_path}")
            return

        # 过滤根目录下的直接文件(只处理子文件夹中的文件)
        root_dir = os.path.abspath(self.config.root_dir)
        file_dir = os.path.dirname(os.path.abspath(file_path))

        # 如果文件直接在根目录下,跳过
        if file_dir == root_dir:
            print(f"忽略根目录下的文件: {file_path}")
            return

        # 判断是否编辑器保存重建: 该路径刚被删除(窗口内) = 删旧写新, 跳过;
        # 墓碑不存在或已过期(删除超过 RECREATE_SUPPRESS_SECONDS) = 真正的新文件, 照常入队
        norm_path = self._normalize(file_path)
        tombstone_ts = self._tombstones.get(norm_path)
        if tombstone_ts is not None:
            if time.time() - tombstone_ts < RECREATE_SUPPRESS_SECONDS:
                print(f"忽略修改保存重建的文件: {file_path}")
                return
            # 墓碑过期: 删除后放置的真新文件, 清除墓碑避免其继续膨胀
            del self._tombstones[norm_path]

        print(f"检测到新文件: {file_path}")

        # 延迟等待文件写入完成(避免读取未完成的文件)
        time.sleep(self.config.file_stable_delay)

        # 再次检查文件是否存在(防止文件被快速删除)
        if os.path.exists(file_path):
            print(f"文件已稳定,加入任务队列: {file_path}")
            self.task_queue.put(file_path)
            # 入队成功, 清除该路径墓碑(否则下次删除+重建会误当成保存)
            self._tombstones.pop(norm_path, None)
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
            if not root_dir or not str(root_dir).strip():
                # ROOT_DIR 被清空(设置页允许清空必填字段时)会导致 os.path.exists('')
                # 返回 False 后 makedirs('') 抛异常; 在这里给出明确错误
                raise ValueError("ROOT_DIR 未配置或为空，请在设置页填写监控文件夹")
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
