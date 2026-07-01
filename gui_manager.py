"""
GUI管理界面模块
功能:提供图形化用户界面,管理文件夹、查看失败文件、显示日志
技术:使用tkinter构建桌面应用
支持:关闭窗口时最小化到系统托盘
"""
import os
import shutil
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from queue import Queue, Empty
from threading import Thread
from typing import Optional
from db_manager import DatabaseManager
from config_manager import ConfigManager


class MainApplication:
    """
    主应用程序窗口
    包含文件夹管理、失败文件处理、日志显示等功能
    """
    
    def __init__(self, root: tk.Tk, stop_event, task_queue: Queue, log_queue: Queue, upload_processor):
        """
        初始化GUI应用
        
        Args:
            root: tkinter根窗口
            stop_event: 停止信号
            task_queue: 任务队列(用于重新上传)
            log_queue: 日志队列(接收后台日志)
            upload_processor: 上传处理器实例(用于调用重试方法)
        """
        self.root = root
        self.stop_event = stop_event
        self.task_queue = task_queue
        self.log_queue = log_queue
        self.upload_processor = upload_processor

        # 初始化数据库和配置
        self.db = DatabaseManager()
        self.config = ConfigManager()

        # 存储 Treeview 行 iid 与实际数据的映射
        self._folder_data = {}  # iid -> (folder_path, folder_name)
        self._failed_data = {}  # iid -> (record_id, file_path, retry_count)

        # 系统托盘相关
        self.tray_icon = None
        self.tray_thread = None
        self._tray_setup_done = False
        
        # 设置窗口属性
        self.root.title("作业自动上传管理工具")
        self.root.geometry("900x700")
        
        # 创建界面组件
        self._create_widgets()
        
        # 启动日志更新定时器
        self._update_logs()
        
        # 启动失败列表刷新定时器
        self._refresh_failed_list()
        
        # 绑定窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
    
    def _create_widgets(self):
        """
        创建所有界面组件
        按照设计文档的布局结构实现
        """
        # === 1. 创建文件夹区域 ===
        create_frame = ttk.LabelFrame(self.root, text="【创建新文件夹】", padding=10)
        create_frame.pack(fill="x", padx=10, pady=5)
        
        # 学校名称输入
        ttk.Label(create_frame, text="学校名称:").grid(row=0, column=0, sticky="w", padx=5)
        self.school_entry = ttk.Entry(create_frame, width=20)
        self.school_entry.grid(row=0, column=1, padx=5)
        
        # 年级下拉选择
        ttk.Label(create_frame, text="年级:").grid(row=0, column=2, sticky="w", padx=5)
        self.grade_combo = ttk.Combobox(create_frame, width=10, state="readonly")
        self.grade_combo['values'] = ('高一', '高二', '高三', '初一', '初二', '初三', 
                                      '小一', '小二', '小三', '小四', '小五', '小六')
        self.grade_combo.current(0)
        self.grade_combo.grid(row=0, column=3, padx=5)
        
        # 创建按钮
        create_btn = ttk.Button(create_frame, text="创建", command=self._create_folder)
        create_btn.grid(row=0, column=4, padx=10)
        
        # === 2. 文件夹列表区域 ===
        folder_frame = ttk.LabelFrame(self.root, text="文件夹列表:", padding=10)
        folder_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # 创建Treeview显示文件夹列表
        columns = ("序号", "文件夹名称", "清空文件", "删除文件夹")
        self.folder_tree = ttk.Treeview(folder_frame, columns=columns, show="headings", height=6)
        self.folder_tree.heading("序号", text="序号")
        self.folder_tree.heading("文件夹名称", text="文件夹名称")
        self.folder_tree.heading("清空文件", text="清空文件")
        self.folder_tree.heading("删除文件夹", text="删除文件夹")

        self.folder_tree.column("序号", width=50, anchor="center")
        self.folder_tree.column("文件夹名称", width=300, anchor="w")
        self.folder_tree.column("清空文件", width=100, anchor="center")
        self.folder_tree.column("删除文件夹", width=100, anchor="center")

        # 配置操作列的可点击样式
        self.folder_tree.tag_configure("action", foreground="#0066CC", font=("Arial", 9, "underline"))

        # 绑定点击事件：点击"清空文件"或"删除文件夹"列时触发操作
        self.folder_tree.bind("<ButtonRelease-1>", self._on_folder_tree_click)
        self.folder_tree.bind("<Motion>", self._on_folder_tree_motion)
        
        # 添加滚动条
        folder_scrollbar = ttk.Scrollbar(folder_frame, orient="vertical", command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=folder_scrollbar.set)
        
        self.folder_tree.pack(side="left", fill="both", expand=True)
        folder_scrollbar.pack(side="right", fill="y")
        
        # 刷新文件夹列表
        self._refresh_folder_list()
        
        # === 3. 上传失败文件列表 ===
        failed_frame = ttk.LabelFrame(self.root, text="⚠️ 上传失败文件(需处理):", padding=10)
        failed_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # 创建Treeview显示失败文件
        failed_columns = ("ID", "文件名", "失败原因", "重试次数", "重新上传", "忽略")
        self.failed_tree = ttk.Treeview(failed_frame, columns=failed_columns, show="headings", height=6)
        self.failed_tree.heading("ID", text="ID")
        self.failed_tree.heading("文件名", text="文件名")
        self.failed_tree.heading("失败原因", text="失败原因")
        self.failed_tree.heading("重试次数", text="重试次数")
        self.failed_tree.heading("重新上传", text="重新上传")
        self.failed_tree.heading("忽略", text="忽略")

        self.failed_tree.column("ID", width=40, anchor="center")
        self.failed_tree.column("文件名", width=160, anchor="w")
        self.failed_tree.column("失败原因", width=220, anchor="w")
        self.failed_tree.column("重试次数", width=60, anchor="center")
        self.failed_tree.column("重新上传", width=100, anchor="center")
        self.failed_tree.column("忽略", width=80, anchor="center")

        # 配置操作列的可点击样式
        self.failed_tree.tag_configure("action", foreground="#0066CC", font=("Arial", 9, "underline"))
        self.failed_tree.tag_configure("action_disabled", foreground="#999999")
        self.failed_tree.tag_configure("failed_row", background="#FFE6E6")

        # 绑定点击事件
        self.failed_tree.bind("<ButtonRelease-1>", self._on_failed_tree_click)
        self.failed_tree.bind("<Motion>", self._on_failed_tree_motion)
        
        # 添加滚动条
        failed_scrollbar = ttk.Scrollbar(failed_frame, orient="vertical", command=self.failed_tree.yview)
        self.failed_tree.configure(yscrollcommand=failed_scrollbar.set)
        
        self.failed_tree.pack(side="left", fill="both", expand=True)
        failed_scrollbar.pack(side="right", fill="y")
        
        # 初始加载失败列表
        self._load_failed_records()
        
        # === 4. 日志区域 ===
        log_frame = ttk.LabelFrame(self.root, text="运行日志:", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # 创建滚动文本框
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        
        # 配置日志文本样式
        self.log_text.tag_configure("error", foreground="red")
        self.log_text.tag_configure("success", foreground="green")
        self.log_text.tag_configure("info", foreground="blue")
        
        # === 5. 状态栏 ===
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", padx=10, pady=5)
        
        self.status_label = ttk.Label(status_frame, text="浏览器状态: 🔴 未启动 (等待文件...)",
                                      font=("Arial", 10), foreground="gray")
        self.status_label.pack(side="left")
    
    def _create_folder(self):
        """
        创建新文件夹
        拼接学校+年级作为文件夹名称
        """
        school = self.school_entry.get().strip()
        grade = self.grade_combo.get()
        
        if not school:
            messagebox.showwarning("警告", "请输入学校名称!")
            return
        
        # 拼接文件夹名称
        folder_name = f"{school}{grade}"
        root_dir = self.config.root_dir
        folder_path = os.path.join(root_dir, folder_name)
        
        # 检查是否已存在
        if os.path.exists(folder_path):
            messagebox.showwarning("警告", f"文件夹已存在: {folder_name}")
            return
        
        try:
            # 创建文件夹
            os.makedirs(folder_path, exist_ok=True)
            messagebox.showinfo("成功", f"文件夹创建成功: {folder_name}")
            
            # 清空输入框
            self.school_entry.delete(0, tk.END)
            
            # 刷新列表
            self._refresh_folder_list()
        
        except Exception as e:
            messagebox.showerror("错误", f"创建文件夹失败: {e}")
    
    def _refresh_folder_list(self):
        """
        刷新文件夹列表
        从根目录读取所有子文件夹并显示
        """
        # 清空现有列表和数据映射
        for item in self.folder_tree.get_children():
            self.folder_tree.delete(item)
        self._folder_data.clear()

        # 获取根目录下所有子文件夹
        root_dir = self.config.root_dir
        if not os.path.exists(root_dir):
            return

        index = 1
        for folder_name in sorted(os.listdir(root_dir)):
            folder_path = os.path.join(root_dir, folder_name)
            if os.path.isdir(folder_path):
                # 插入到Treeview，操作列显示可点击链接文本
                iid = self.folder_tree.insert(
                    "", "end",
                    values=(index, folder_name, "🧹 清空", "🗑️ 删除"),
                    tags=("action_row",)
                )
                # 存储映射：iid -> (folder_path, folder_name)
                self._folder_data[iid] = (folder_path, folder_name)
                index += 1
    
    def _clear_folder(self, folder_path: str, folder_name: str):
        """
        清空文件夹内的所有文件,并删除数据库记录
        
        Args:
            folder_path: 文件夹完整路径
            folder_name: 文件夹名称
        """
        # 确认对话框
        if not messagebox.askyesno("确认", f"确定要清空文件夹 '{folder_name}' 内的所有文件吗?\n此操作不可恢复!"):
            return
        
        try:
            # 删除文件夹内所有文件
            for filename in os.listdir(folder_path):
                file_path = os.path.join(folder_path, filename)
                if os.path.isfile(file_path):
                    os.remove(file_path)
            
            # 删除数据库记录
            self.db.delete_records_by_folder(folder_name)
            
            messagebox.showinfo("成功", f"文件夹 '{folder_name}' 已清空")
            
            # 刷新失败列表
            self._load_failed_records()
        
        except Exception as e:
            messagebox.showerror("错误", f"清空文件夹失败: {e}")
    
    def _delete_folder(self, folder_path: str, folder_name: str):
        """
        删除整个文件夹(含所有文件),并删除数据库记录
        
        Args:
            folder_path: 文件夹完整路径
            folder_name: 文件夹名称
        """
        # 确认对话框
        if not messagebox.askyesno("确认", f"确定要删除整个文件夹 '{folder_name}' 吗?\n此操作不可恢复!"):
            return
        
        try:
            # 删除整个文件夹
            shutil.rmtree(folder_path)
            
            # 删除数据库记录
            self.db.delete_records_by_folder(folder_name)
            
            messagebox.showinfo("成功", f"文件夹 '{folder_name}' 已删除")
            
            # 刷新列表
            self._refresh_folder_list()
            self._load_failed_records()
        
        except Exception as e:
            messagebox.showerror("错误", f"删除文件夹失败: {e}")
    
    def _on_folder_tree_click(self, event):
        """
        处理文件夹列表的点击事件
        根据点击的列来判断执行清空还是删除操作
        """
        # 获取点击位置对应的列和行
        column = self.folder_tree.identify_column(event.x)
        item = self.folder_tree.identify_row(event.y)

        if not item or item not in self._folder_data:
            return

        folder_path, folder_name = self._folder_data[item]

        # 列 '#3' = 清空文件, '#4' = 删除文件夹
        if column == '#3':
            self._clear_folder(folder_path, folder_name)
        elif column == '#4':
            self._delete_folder(folder_path, folder_name)

    def _on_folder_tree_motion(self, event):
        """鼠标在文件夹列表上移动时切换光标样式"""
        column = self.folder_tree.identify_column(event.x)
        if column in ('#3', '#4'):
            self.folder_tree.configure(cursor="hand2")
        else:
            self.folder_tree.configure(cursor="")

    def _load_failed_records(self):
        """
        加载并显示失败的上传记录
        """
        # 清空现有列表和数据映射
        for item in self.failed_tree.get_children():
            self.failed_tree.delete(item)
        self._failed_data.clear()

        # 获取失败记录
        failed_records = self.db.get_failed_records()
        max_retry = self.config.max_retry_count

        if not failed_records:
            # 无失败记录,显示提示
            self.failed_tree.insert("", "end", values=("", "当前无失败文件", "", "", "", ""))
            return

        # 显示每条失败记录
        for record in failed_records:
            rid = record['id']
            retry_count = record['retry_count']
            file_path = record['file_path']

            # 根据重试次数决定"重新上传"按钮状态
            if retry_count >= max_retry:
                retry_text = "已达上限"
                retry_tag = "action_disabled"
            else:
                retry_text = "🔄 重传"
                retry_tag = "action"

            iid = self.failed_tree.insert("", "end", values=(
                rid,
                record['file_name'],
                record['error_message'] or "未知错误",
                retry_count,
                retry_text,
                "🗑️ 忽略"
            ), tags=("failed_row",))

            # 存储映射：iid -> (record_id, file_path, retry_count)
            self._failed_data[iid] = (rid, file_path, retry_count)
    
    def _retry_upload(self, record_id: int, file_path: str):
        """
        重新上传失败的文件
        
        Args:
            record_id: 数据库记录ID
            file_path: 文件路径
        """
        # 检查文件是否还存在
        if not os.path.exists(file_path):
            messagebox.showerror("错误", f"文件不存在: {file_path}")
            return
        
        # 在新线程中执行重新上传,避免阻塞GUI
        thread = Thread(target=self.upload_processor.retry_upload, args=(record_id, file_path))
        thread.daemon = True
        thread.start()
        
        messagebox.showinfo("提示", "重新上传任务已提交")
    
    def _ignore_record(self, record_id: int):
        """
        忽略某条失败记录(从数据库中删除)
        
        Args:
            record_id: 记录ID
        """
        if not messagebox.askyesno("确认", "确定要忽略这条记录吗?"):
            return
        
        try:
            self.db.delete_record(record_id)
            self._load_failed_records()
            messagebox.showinfo("成功", "记录已忽略")
        except Exception as e:
            messagebox.showerror("错误", f"删除记录失败: {e}")
    
    def _on_failed_tree_click(self, event):
        """
        处理失败文件列表的点击事件
        根据点击的列来判断执行重新上传还是忽略操作
        """
        column = self.failed_tree.identify_column(event.x)
        item = self.failed_tree.identify_row(event.y)

        if not item or item not in self._failed_data:
            return

        record_id, file_path, retry_count = self._failed_data[item]
        max_retry = self.config.max_retry_count

        # 列 '#5' = 重新上传, '#6' = 忽略
        if column == '#5':
            if retry_count >= max_retry:
                messagebox.showwarning("提示", f"已达到最大重试次数({max_retry})，无法继续重试")
                return
            self._retry_upload(record_id, file_path)
        elif column == '#6':
            self._ignore_record(record_id)

    def _on_failed_tree_motion(self, event):
        """鼠标在失败列表上移动时切换光标样式"""
        column = self.failed_tree.identify_column(event.x)
        # 只有"重新上传"列(且非disabled状态)和"忽略"列显示手型
        if column == '#6':
            self.failed_tree.configure(cursor="hand2")
        elif column == '#5':
            item = self.failed_tree.identify_row(event.y)
            if item in self._failed_data:
                _, _, retry_count = self._failed_data[item]
                if retry_count < self.config.max_retry_count:
                    self.failed_tree.configure(cursor="hand2")
                else:
                    self.failed_tree.configure(cursor="")
            else:
                self.failed_tree.configure(cursor="")
        else:
            self.failed_tree.configure(cursor="")

    def _update_logs(self):
        """
        从日志队列中读取消息并显示在界面上
        使用after方法定时检查,确保线程安全
        """
        try:
            while True:
                # 非阻塞方式读取日志队列
                message = self.log_queue.get_nowait()
                
                # 如果是特殊指令,执行相应操作
                if message == "REFRESH_FAILED_LIST":
                    self._load_failed_records()
                    continue

                if message.startswith("BROWSER_STATUS:"):
                    status = message.split(":", 1)[1]
                    if status == "CONNECTED":
                        self.status_label.config(
                            text="浏览器状态: 🟢 已连接", foreground="green")
                    elif status == "DISCONNECTED":
                        self.status_label.config(
                            text="浏览器状态: 🔴 未启动 (等待文件...)", foreground="gray")
                    continue
                
                # 显示日志消息
                self.log_text.configure(state="normal")
                
                # 根据消息类型设置颜色
                tag = "info"
                if "错误" in message or "失败" in message or "✗" in message:
                    tag = "error"
                elif "成功" in message or "✓" in message:
                    tag = "success"
                
                self.log_text.insert("end", f"{message}\n", tag)
                self.log_text.see("end")  # 滚动到底部
                self.log_text.configure(state="disabled")
        
        except Empty:
            # 队列为空,正常情况
            pass
        
        except Exception as e:
            print(f"更新日志失败: {e}")
        
        # 每100ms检查一次日志队列
        self.root.after(100, self._update_logs)
    
    def _refresh_failed_list(self):
        """
        定时刷新失败列表
        每5秒检查一次是否有新的失败记录
        """
        self._load_failed_records()
        # 每5秒刷新一次
        self.root.after(5000, self._refresh_failed_list)
    
    def update_browser_status(self, status: str):
        """
        更新浏览器状态显示
        
        Args:
            status: 状态字符串,如"已连接"、"未连接"、"重启中"
        """
        if status == "已连接":
            self.status_label.config(text="浏览器状态: 🟢 已连接", foreground="green")
        elif status == "未连接":
            self.status_label.config(text="浏览器状态: 🔴 未连接", foreground="red")
        elif status == "重启中":
            self.status_label.config(text="浏览器状态: 🟡 重启中...", foreground="orange")
    
    def _setup_tray(self):
        """
        初始化系统托盘图标（延迟加载，仅在首次最小化时创建）
        返回 True 表示托盘创建成功，False 表示失败
        """
        if self._tray_setup_done and self.tray_icon is not None \
                and self.tray_thread is not None and self.tray_thread.is_alive():
            return True

        try:
            import pystray
            from PIL import Image, ImageDraw

            # 生成托盘图标（蓝底白色上传箭头）
            def _make_icon():
                img = Image.new('RGB', (64, 64), color='#4A90D9')
                draw = ImageDraw.Draw(img)
                draw.rectangle([8, 14, 56, 50], fill='white')
                draw.polygon([(32, 6), (8, 22), (56, 22)], fill='white')
                return img

            menu = pystray.Menu(
                pystray.MenuItem("显示窗口", self._show_window, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit_app),
            )

            self.tray_icon = pystray.Icon(
                "auto_upload",
                _make_icon(),
                "作业自动上传 - 运行中",
                menu
            )
            self.tray_thread = Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()

            self._tray_setup_done = True
            self._log_to_gui("程序已最小化到系统托盘，双击托盘图标可恢复窗口", "info")
            return True

        except ImportError as e:
            self._log_to_gui(f"无法启动系统托盘（缺少依赖）: {e}", "error")
            self._tray_setup_done = False
            return False
        except Exception as e:
            import traceback
            self._log_to_gui(f"系统托盘启动失败: {e}", "error")
            self._log_to_gui(traceback.format_exc(), "error")
            self._tray_setup_done = False
            return False

    def _show_window(self):
        """从系统托盘恢复显示主窗口"""
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        """在主线程中恢复窗口"""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _quit_app(self):
        """从托盘菜单完全退出程序"""
        self._perform_exit()

    def _perform_exit(self):
        """统一的退出流程：设停止信号、等队列清空、销毁窗口"""
        self.stop_event.set()
        if self.tray_icon:
            self.tray_icon.stop()
            self.tray_icon = None

        def _wait_and_destroy():
            try:
                self.task_queue.join()
            except Exception:
                pass
            finally:
                try:
                    self.root.destroy()
                except Exception:
                    pass

        if not self.task_queue.empty():
            messagebox.showinfo("提示", "等待当前任务完成...")
        Thread(target=_wait_and_destroy, daemon=True).start()

    def _log_to_gui(self, message: str, tag: str = "info"):
        """向GUI日志区域写入消息（线程安全）"""
        try:
            self.root.after(0, lambda: self._insert_log(message, tag))
        except Exception:
            pass

    def _insert_log(self, message: str, tag: str):
        """实际执行日志插入（必须在主线程调用）"""
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"{message}\n", tag)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _on_closing(self):
        """
        窗口关闭事件处理
        - 如果启用托盘模式：最小化到系统托盘
        - 如果禁用托盘模式：直接确认退出
        """
        use_tray = self.config.get("MINIMIZE_TO_TRAY", True)

        if use_tray:
            # 尝试最小化到托盘，失败则回退到退出确认
            try:
                if self._setup_tray():
                    self.root.withdraw()
                    if self.tray_icon and hasattr(self.tray_icon, 'notify'):
                        try:
                            self.tray_icon.notify(
                                "作业自动上传工具仍在后台运行\n双击托盘图标可恢复窗口",
                                title="作业自动上传"
                            )
                        except Exception:
                            pass
                    return  # 成功隐藏到托盘，不退出
                else:
                    self._log_to_gui("托盘启动失败，回退到退出确认模式", "error")
            except Exception:
                import traceback
                self._log_to_gui(f"托盘异常: {traceback.format_exc()}", "error")

            # 回退：传统退出确认
            if not messagebox.askokcancel("退出", "托盘不可用，确定要退出程序吗？\n正在进行的上传任务将被中断。"):
                return
            self._perform_exit()
        else:
            # 传统退出模式
            if not messagebox.askokcancel("退出", "确定要退出程序吗?\n正在进行的上传任务将被中断。"):
                return
            self._perform_exit()
