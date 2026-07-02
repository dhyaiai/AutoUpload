"""
GUI管理界面模块
功能:提供图形化用户界面,管理文件夹、查看失败文件、显示日志
技术:使用tkinter构建桌面应用
支持:关闭窗口时最小化到系统托盘
"""
import os
import shutil
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from queue import Queue, Empty
from threading import Thread
from typing import Optional
from db_manager import DatabaseManager
from config_manager import ConfigManager
from file_merger import FileMerger
from stats_panel import StatsPanel


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

        # 合并文件相关
        self.question_file_path = None  # 试题文件路径
        self.answer_file_path = None    # 答案文件路径

        # 系统托盘相关
        self.tray_icon = None
        self.tray_thread = None
        self._tray_setup_done = False
        
        # 设置窗口属性
        self.root.title("作业自动上传管理工具")
        self.root.geometry("900x750")
        self.root.minsize(700, 500)

        # 可滚动画布（状态栏固定在底部不滚动）
        self._canvas = tk.Canvas(self.root, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self._canvas.yview)
        self.content_frame = ttk.Frame(self._canvas)

        self.content_frame.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self.content_frame, anchor="nw")

        # 画布宽度跟随窗口变化时同步内容宽度
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        # 鼠标滚轮滚动
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)

        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")
        
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
        使用Notebook实现标签页切换
        """
        # 创建Notebook标签页容器
        self.notebook = ttk.Notebook(self.content_frame)
        self.notebook.pack(fill="both", expand=True, padx=0, pady=0)

        # 标签页1: 上传管理（原有内容）
        self.tab_upload = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_upload, text="📤 上传管理")

        # 标签页2: 数据统计
        self.tab_stats = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_stats, text="📊 作业上传数据统计")

        # 创建上传管理标签页内容
        self._create_upload_tab()

        # 创建数据统计标签页内容
        self._create_stats_tab()

        # 启动统计面板定时刷新
        self._start_stats_refresh()

    def _create_upload_tab(self):
        """
        创建上传管理标签页内容（原 _create_widgets 的主体部分）
        """
        parent = self.tab_upload

        # === 1. 创建文件夹区域 ===
        create_frame = ttk.LabelFrame(parent, text="【创建新文件夹】", padding=10)
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

        # === 2. 合并文件区域 ===
        merge_frame = ttk.LabelFrame(parent, text="【合并文件】", padding=10)
        merge_frame.pack(fill="x", padx=10, pady=5)

        merge_frame.columnconfigure(0, weight=1)
        merge_frame.columnconfigure(1, weight=1)
        merge_frame.columnconfigure(2, weight=0)

        # --- 左侧：上传试题 ---
        question_sub = ttk.Frame(merge_frame)
        question_sub.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        self._question_status_label = tk.Label(
            question_sub, text="上传试题：点击浏览或拖拽文件到下方区域",
            bg="white", relief="sunken", anchor="center", height=3,
            fg="gray"
        )
        self._question_status_label.pack(fill="x", pady=(0, 2))
        self._question_status_label.bind('<Button-1>', lambda e: self._browse_question_file())
        self._register_drop_target(self._question_status_label, self._on_question_drop)

        ttk.Button(question_sub, text="浏览...",
                   command=self._browse_question_file).pack()

        # --- 中间：上传答案 ---
        answer_sub = ttk.Frame(merge_frame)
        answer_sub.grid(row=0, column=1, sticky="nsew", padx=(5, 5))

        self._answer_status_label = tk.Label(
            answer_sub, text="上传答案：点击浏览或拖拽文件到下方区域",
            bg="white", relief="sunken", anchor="center", height=3,
            fg="gray"
        )
        self._answer_status_label.pack(fill="x", pady=(0, 2))
        self._answer_status_label.bind('<Button-1>', lambda e: self._browse_answer_file())
        self._register_drop_target(self._answer_status_label, self._on_answer_drop)

        ttk.Button(answer_sub, text="浏览...",
                   command=self._browse_answer_file).pack()

        # --- 右侧：合并按钮 ---
        merge_btn = ttk.Button(merge_frame, text="合  并", command=self._on_merge_click, width=10)
        merge_btn.grid(row=0, column=2, padx=(5, 0), sticky="ns")

        # === 3. 文件夹列表区域 ===
        folder_frame = ttk.LabelFrame(parent, text="文件夹列表:", padding=10)
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

        # === 4. 上传失败文件列表 ===
        failed_frame = ttk.LabelFrame(parent, text="⚠️ 上传失败文件(需处理):", padding=10)
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

        # === 5. 日志区域 ===
        log_frame = ttk.LabelFrame(parent, text="运行日志:", padding=10)
        log_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # 创建滚动文本框
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

        # 配置日志文本样式
        self.log_text.tag_configure("error", foreground="red")
        self.log_text.tag_configure("success", foreground="green")
        self.log_text.tag_configure("info", foreground="blue")

        # === 6. 状态栏 ===
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", padx=10, pady=5)

        self.status_label = ttk.Label(status_frame, text="浏览器状态: 🔴 未启动 (等待文件...)",
                                      font=("Arial", 10), foreground="gray")
        self.status_label.pack(side="left")

    def _create_stats_tab(self):
        """创建作业上传数据统计标签页"""
        self.stats_panel = StatsPanel(self.tab_stats, self.db, self.root)

    def _start_stats_refresh(self):
        """定时刷新统计面板表格数据（仅当统计标签页可见时）"""
        def _auto_refresh():
            try:
                if hasattr(self, 'stats_panel') and self._is_stats_tab_selected():
                    if self.stats_panel._data_copied:
                        self.stats_panel._refresh_upload_table()
                        self.stats_panel._refresh_failed_table()
            finally:
                self.root.after(30000, _auto_refresh)
        self.root.after(30000, _auto_refresh)

    def _is_stats_tab_selected(self) -> bool:
        """判断当前是否选中了统计标签页"""
        if hasattr(self, 'notebook'):
            return self.notebook.index(self.notebook.select()) == 1
        return False

    def _on_canvas_configure(self, event):
        """画布宽度变化时同步内容帧宽度，确保内容填满画布"""
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _bind_mousewheel(self, event):
        """鼠标进入画布时绑定滚轮"""
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, event):
        """鼠标离开画布时解绑滚轮"""
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        """鼠标滚轮滚动画布"""
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    
    # ==================== 合并文件相关方法 ====================

    @staticmethod
    def _register_drop_target(widget, callback):
        """为控件注册拖拽放下目标，失败时静默忽略（DnD 库不可用时回退到点击上传）"""
        try:
            widget.drop_target_register('*')
            widget.dnd_bind('<<Drop>>', callback)
        except Exception:
            pass

    def _browse_question_file(self):
        """浏览选择试题文件"""
        path = filedialog.askopenfilename(
            title="选择试题文件",
            filetypes=[("文档文件", "*.doc *.docx *.pdf"),
                       ("Word文档", "*.doc *.docx"),
                       ("PDF文件", "*.pdf"),
                       ("所有文件", "*.*")]
        )
        if path:
            self._set_question_file(path)

    def _browse_answer_file(self):
        """浏览选择答案文件"""
        path = filedialog.askopenfilename(
            title="选择答案文件",
            filetypes=[("文档文件", "*.doc *.docx *.pdf"),
                       ("Word文档", "*.doc *.docx"),
                       ("PDF文件", "*.pdf"),
                       ("所有文件", "*.*")]
        )
        if path:
            self._set_answer_file(path)

    def _on_question_drop(self, event):
        """拖拽放下试题文件"""
        path = self._parse_drop_path(event.data)
        if path:
            self._set_question_file(path)

    def _on_answer_drop(self, event):
        """拖拽放下答案文件"""
        path = self._parse_drop_path(event.data)
        if path:
            self._set_answer_file(path)

    @staticmethod
    def _parse_drop_path(data: str) -> Optional[str]:
        """
        解析拖拽事件中的文件路径。
        tkinterdnd2 格式: "{C:/path/file.ext}" 或 "{path1} {path2} ..."
        取第一个文件路径返回。
        """
        if not data:
            return None
        data = data.strip()
        # 按 } { 分割多文件路径
        parts = data.split('} {')
        first = parts[0].strip()
        # 去掉首尾花括号
        if first.startswith('{'):
            first = first[1:]
        if first.endswith('}'):
            first = first[:-1]
        return first if first else None

    def _set_question_file(self, path: str):
        """设置试题文件路径并更新UI"""
        if not os.path.isfile(path):
            messagebox.showwarning("警告", f"文件不存在: {path}")
            return
        ext = FileMerger.get_format(path)
        if ext not in FileMerger.SUPPORTED_EXTENSIONS:
            messagebox.showwarning("警告",
                f"不支持的文件格式 ({ext})\n仅支持: {', '.join(FileMerger.SUPPORTED_EXTENSIONS)}")
            return
        self.question_file_path = path
        display = os.path.basename(path)
        # 截断过长文件名
        if len(display) > 50:
            display = display[:47] + "..."
        self._question_status_label.config(text=display, fg="black")

    def _set_answer_file(self, path: str):
        """设置答案文件路径并更新UI"""
        if not os.path.isfile(path):
            messagebox.showwarning("警告", f"文件不存在: {path}")
            return
        ext = FileMerger.get_format(path)
        if ext not in FileMerger.SUPPORTED_EXTENSIONS:
            messagebox.showwarning("警告",
                f"不支持的文件格式 ({ext})\n仅支持: {', '.join(FileMerger.SUPPORTED_EXTENSIONS)}")
            return
        self.answer_file_path = path
        display = os.path.basename(path)
        if len(display) > 50:
            display = display[:47] + "..."
        self._answer_status_label.config(text=display, fg="black")

    def _on_merge_click(self):
        """点击合并按钮 — 校验文件 → 弹窗选目录 → 执行合并"""
        # 校验
        if not self.question_file_path:
            messagebox.showwarning("提示", "请先选择试题文件")
            return
        if not self.answer_file_path:
            messagebox.showwarning("提示", "请先选择答案文件")
            return
        if not os.path.isfile(self.question_file_path):
            messagebox.showerror("错误", f"试题文件不存在: {self.question_file_path}")
            return
        if not os.path.isfile(self.answer_file_path):
            messagebox.showerror("错误", f"答案文件不存在: {self.answer_file_path}")
            return

        q_fmt = FileMerger.get_format(self.question_file_path)
        a_fmt = FileMerger.get_format(self.answer_file_path)
        if q_fmt != a_fmt:
            messagebox.showerror("错误",
                f"试题和答案文件格式不一致，无法合并。\n试题: {q_fmt}  答案: {a_fmt}")
            return

        # 弹出子目录选择对话框
        target_dir = self._show_subdir_dialog()
        if not target_dir:
            return  # 用户取消

        # 输出文件名 = 试题文件名
        output_name = os.path.basename(self.question_file_path)
        output_path = os.path.join(target_dir, output_name)

        # 目标文件已存在则询问
        if os.path.exists(output_path):
            if not messagebox.askyesno("确认覆盖",
                    f"目标文件已存在，是否覆盖？\n{output_path}"):
                return

        # 在后台线程执行合并，避免阻塞 GUI
        thread = Thread(target=self._do_merge, args=(output_path, target_dir), daemon=True)
        thread.start()

    def _show_subdir_dialog(self) -> Optional[str]:
        """
        弹出子目录选择对话框。
        显示 ROOT_DIR 下所有子文件夹，用户选择一个。
        返回完整路径，取消返回 None。
        """
        root_dir = self.config.root_dir
        if not os.path.exists(root_dir):
            messagebox.showerror("错误", f"根目录不存在:\n{root_dir}")
            return None

        subdirs = sorted([
            d for d in os.listdir(root_dir)
            if os.path.isdir(os.path.join(root_dir, d))
        ])
        if not subdirs:
            messagebox.showwarning("提示",
                f"根目录下没有子文件夹，请先在「创建新文件夹」区域创建。\n{root_dir}")
            return None

        dialog = tk.Toplevel(self.root)
        dialog.title("选择保存目录")
        dialog.geometry("420x380")
        dialog.transient(self.root)
        dialog.grab_set()
        # 居中
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 420) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 380) // 2
        dialog.geometry(f"+{x}+{y}")

        result = {"path": None}

        ttk.Label(dialog, text="请选择保存的子目录:", font=("", 10)).pack(pady=(15, 5))
        ttk.Label(dialog, text=root_dir, foreground="gray", font=("", 8)).pack()

        # 列表
        list_frame = ttk.Frame(dialog)
        list_frame.pack(fill="both", expand=True, padx=15, pady=10)

        listbox = tk.Listbox(list_frame, font=("", 10))
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)

        for d in subdirs:
            listbox.insert("end", d)

        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 按钮区
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", padx=15, pady=(0, 15))

        def on_confirm():
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("提示", "请选择一个子目录", parent=dialog)
                return
            result["path"] = os.path.join(root_dir, subdirs[sel[0]])
            dialog.destroy()

        def on_cancel():
            dialog.destroy()

        ttk.Button(btn_frame, text="确定", command=on_confirm).pack(side="right", padx=5)
        ttk.Button(btn_frame, text="取消", command=on_cancel).pack(side="right", padx=5)

        # 双击确定
        listbox.bind("<Double-Button-1>", lambda e: on_confirm())

        dialog.wait_window()
        return result["path"]

    def _do_merge(self, output_path: str, target_dir: str):
        """在后台线程中执行文件合并"""
        try:
            self._log_to_gui("=" * 50, "info")
            self._log_to_gui(f"开始合并文件...", "info")
            self._log_to_gui(f"  试题: {self.question_file_path}", "info")
            self._log_to_gui(f"  答案: {self.answer_file_path}", "info")
            self._log_to_gui(f"  输出: {output_path}", "info")

            FileMerger.merge(
                self.question_file_path,
                self.answer_file_path,
                output_path
            )

            self._log_to_gui(f"合并成功！文件已保存到: {output_path}", "success")
            self.root.after(0, lambda: messagebox.showinfo(
                "合并完成", f"文件已保存到:\n{output_path}"))

            # 将合并后的文件加入上传队列，触发自动上传
            self.task_queue.put(output_path)
            self._log_to_gui(f"已将合并文件加入上传队列: {os.path.basename(output_path)}", "info")

        except Exception as e:
            err_msg = str(e)
            self._log_to_gui(f"合并失败: {err_msg}", "error")
            self.root.after(0, lambda: messagebox.showerror(
                "合并失败", f"{err_msg}"))

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
        # 清理matplotlib资源
        if hasattr(self, 'stats_panel'):
            self.stats_panel.destroy()
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
