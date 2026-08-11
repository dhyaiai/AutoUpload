"""
GUI管理界面模块
功能:提供图形化用户界面,管理文件夹、查看失败文件、显示日志
技术:使用 customtkinter 构建现代化桌面应用(圆角卡片 + 侧边栏导航 + 低饱和配色)
支持:关闭窗口时最小化到系统托盘
"""
import os
import json
import shutil
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from queue import Queue, Empty
from threading import Thread
from typing import Optional

import customtkinter as ctk

import ui_theme as theme
import win32_helpers
from db_manager import DatabaseManager
from config_manager import ConfigManager
from file_merger import FileMerger
from stats_panel import StatsPanel


class MainApplication:
    """
    主应用程序窗口
    包含文件夹管理、失败文件处理、日志显示等功能
    """

    def __init__(self, root, stop_event, task_queue: Queue, log_queue: Queue, upload_processor):
        """
        初始化GUI应用

        Args:
            root: 根窗口(CTk)
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
        self._tray_ready = threading.Event()  # 托盘图标创建完成信号（NIM_ADD 完成后置位）

        # 保存原始窗口样式（供任务栏隐藏/恢复用）
        self._original_exstyle = None
        self._taskbar_hidden = False

        # 批次完成通知窗口（自绘右下角置顶 toast，不依赖系统通知）
        self._batch_toast = None

        # 当前页面: "upload" | "stats"
        self._current_page = "upload"

        # 设置窗口属性
        self.root.title("作业自动上传管理工具")
        self.root.geometry("1200x760")
        self.root.minsize(1024, 680)
        try:
            self.root.configure(fg_color=theme.BG)
        except Exception:
            pass

        # 配置全局 Treeview 扁平样式
        theme.setup_treeview_style(self.root)

        # 创建界面组件
        self._create_widgets()

        # 启动日志更新定时器
        self._update_logs()

        # 启动失败列表刷新定时器
        self._refresh_failed_list()

        # 绑定窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        # 监听窗口隐藏/恢复, 普通最小化路径恢复时用透明重绘避免黑屏首帧
        # (托盘路径由 _restore_window_impl 主动处理, 通过 _taskbar_hidden 区分)
        self._ever_unmapped = False
        self._restoring_from_tray = False
        self.root.bind("<Unmap>", self._on_window_unmap)
        self.root.bind("<Map>", self._on_window_map)

    # ==================== 整体布局 ====================

    def _create_widgets(self):
        """创建整体布局: 左侧导航栏 + 右侧内容区(两个页面切换)"""
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # ===== 左侧导航栏 =====
        self._create_sidebar()

        # ===== 右侧内容区 =====
        self.content = ctk.CTkFrame(self.root, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew", padx=(4, 16), pady=16)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        # 三个页面预先 grid 到同一个 cell, 切换时用 tkraise 改变 Z 序,
        # 避免 pack_forget/pack 全量重排导致的界面消失与跳动抽搐
        # 页面1: 上传管理
        self.page_upload = ctk.CTkFrame(self.content, fg_color="transparent")
        self.page_upload.grid(row=0, column=0, sticky="nsew")
        self._create_upload_page()

        # 页面2: 数据统计
        self.page_stats = ctk.CTkFrame(self.content, fg_color="transparent")
        self.page_stats.grid(row=0, column=0, sticky="nsew")
        self._create_stats_page()

        # 页面3: 设置
        self.page_settings = ctk.CTkFrame(self.content, fg_color="transparent")
        self.page_settings.grid(row=0, column=0, sticky="nsew")
        self._create_settings_page()

        # 默认显示上传管理页
        self._show_page("upload")

        # 启动统计面板定时刷新
        self._start_stats_refresh()

        # 保存顶层窗口 HWND 与原始扩展样式(供任务栏隐藏/恢复)
        # winfo_id 返回的是客户区子窗口(TkChild),其父级(TkTopLevel)才是
        # 顶层窗口;旧代码用 winfo_id 操作样式,任务栏隐藏从未生效
        self._top_hwnd = win32_helpers.get_top_level_hwnd(self.root)
        self._original_exstyle = win32_helpers.get_ex_style(self._top_hwnd)

    def _create_sidebar(self):
        """左侧导航栏: 应用标题 + 页面导航 + 浏览器状态"""
        sidebar = ctk.CTkFrame(self.root, width=208, corner_radius=0,
                               fg_color=theme.CARD)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(3, weight=1)

        # 应用标题
        title_box = ctk.CTkFrame(sidebar, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="ew", padx=20, pady=(28, 4))
        ctk.CTkLabel(title_box, text="作业自动上传", font=theme.font(17, "bold"),
                     text_color=theme.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(title_box, text="AUTO UPLOAD", font=theme.font(10),
                     text_color=theme.TEXT_FAINT, anchor="w").pack(fill="x")

        # 分隔留白
        ctk.CTkFrame(sidebar, fg_color="transparent", height=16).grid(row=1, column=0)

        # 导航按钮
        nav_box = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav_box.grid(row=2, column=0, sticky="ew", padx=12)

        self._nav_buttons = {}
        for key, label in [("upload", "上传管理"), ("stats", "数据统计"), ("settings", "设置")]:
            btn = ctk.CTkButton(
                nav_box, text=label, font=theme.font(13),
                anchor="w", height=40, corner_radius=8,
                fg_color="transparent", hover_color=theme.PRIMARY_SOFT,
                text_color=theme.TEXT_MUTED,
                command=lambda k=key: self._show_page(k))
            btn.pack(fill="x", pady=3)
            self._nav_buttons[key] = btn

        # 底部: 浏览器状态指示
        status_box = ctk.CTkFrame(sidebar, fg_color=theme.CARD_INNER,
                                  corner_radius=10)
        status_box.grid(row=4, column=0, sticky="ew", padx=12, pady=16)

        ctk.CTkLabel(status_box, text="浏览器状态", font=theme.font(10),
                     text_color=theme.TEXT_FAINT, anchor="w"
                     ).pack(fill="x", padx=14, pady=(10, 0))

        row = ctk.CTkFrame(status_box, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(2, 10))
        self._status_dot = ctk.CTkLabel(row, text="●", font=theme.font(12),
                                        text_color=theme.TEXT_FAINT, width=14,
                                        anchor="w")
        self._status_dot.pack(side="left")
        self.status_label = ctk.CTkLabel(row, text="未启动 · 等待文件",
                                         font=theme.font(11),
                                         text_color=theme.TEXT_MUTED, anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

    def _show_page(self, name: str):
        """切换页面并更新导航高亮

        三个页面已预先 grid 在同一 cell, 这里只改变 Z 顺序(tkraise),
        不触发任何布局重算, 切换无闪烁
        """
        self._current_page = name
        if name == "upload":
            page = self.page_upload
        elif name == "stats":
            page = self.page_stats
        else:
            page = self.page_settings
        page.tkraise()

        # 同步滚动修复的活动页标记: 被盖住(隐藏)的页面跳过 after_idle 重绘,
        # 避免对不可见窗口做无谓 update() 阻塞主循环
        stats_panel = getattr(self, "stats_panel", None)
        if stats_panel is not None:
            try:
                stats_panel._ghost_fix.active = (name == "stats")
            except Exception:
                pass
        settings_fix = getattr(self, "_settings_ghost_fix", None)
        if settings_fix is not None:
            try:
                settings_fix.active = (name == "settings")
            except Exception:
                pass

        for key, btn in self._nav_buttons.items():
            if key == name:
                btn.configure(fg_color=theme.PRIMARY_SOFT,
                              text_color=theme.PRIMARY)
            else:
                btn.configure(fg_color="transparent",
                              text_color=theme.TEXT_MUTED)

    def _set_browser_status(self, state: str):
        """更新侧边栏浏览器状态指示(state: connected/disconnected/restarting/error)"""
        mapping = {
            "connected": (theme.SUCCESS, "已连接"),
            "disconnected": (theme.TEXT_FAINT, "未启动 · 等待文件"),
            "restarting": (theme.WARNING, "重启中…"),
            "error": (theme.DANGER, "未连接"),
        }
        color, text = mapping.get(state, mapping["disconnected"])
        self._status_dot.configure(text_color=color)
        self.status_label.configure(text=text)

    # ==================== 上传管理页 ====================

    def _create_upload_page(self):
        """
        创建上传管理页
        布局: 左列(创建文件夹 + 合并文件 + 文件夹列表 + 失败列表) | 右列(运行日志)
        """
        page = self.page_upload
        page.grid_columnconfigure(0, weight=3, uniform="upload_cols")
        page.grid_columnconfigure(1, weight=1, uniform="upload_cols", minsize=240)
        page.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(page, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)
        left.grid_rowconfigure(3, weight=1)

        right = ctk.CTkFrame(page, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        self._build_create_card(left)
        self._build_merge_card(left)
        self._build_folder_card(left)
        self._build_failed_card(left)
        self._build_log_card(right)

    def _build_create_card(self, parent):
        """卡片: 创建新文件夹"""
        card = theme.card(parent)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        theme.card_title(card, "创建新文件夹").pack(fill="x", padx=20, pady=(16, 8))

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 18))

        ctk.CTkLabel(row, text="学校名称", font=theme.font(12),
                     text_color=theme.TEXT_MUTED).pack(side="left")
        self.school_entry = ctk.CTkEntry(
            row, width=220, height=34, corner_radius=8,
            font=theme.font(12), placeholder_text="输入学校名称",
            fg_color=theme.CARD_INNER, border_color=theme.BORDER,
            text_color=theme.TEXT)
        self.school_entry.pack(side="left", padx=(10, 24))

        ctk.CTkLabel(row, text="年级", font=theme.font(12),
                     text_color=theme.TEXT_MUTED).pack(side="left")
        self.grade_combo = ctk.CTkComboBox(
            row, width=110, height=34, corner_radius=8, state="readonly",
            font=theme.font(12), dropdown_font=theme.font(12),
            values=['高三', '高二', '高一', '九年级', '八年级', '七年级',
                    '六年级', '五年级', '四年级', '三年级', '二年级', '一年级'],
            fg_color=theme.CARD_INNER, border_color=theme.BORDER,
            button_color=theme.CARD_INNER, button_hover_color=theme.PRIMARY_SOFT,
            text_color=theme.TEXT, dropdown_fg_color=theme.CARD,
            dropdown_hover_color=theme.PRIMARY_SOFT,
            dropdown_text_color=theme.TEXT)
        self.grade_combo.set('高三')
        self.grade_combo.pack(side="left", padx=(10, 24))

        theme.primary_button(row, "创建", self._create_folder,
                             width=88).pack(side="left")

    def _build_merge_card(self, parent):
        """卡片: 合并文件(试题 + 答案拖拽区)"""
        card = theme.card(parent)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 8))
        theme.card_title(head, "合并文件").pack(side="left")
        ctk.CTkLabel(head, text="仅支持相同格式合并，完成后自动加入上传队列",
                     font=theme.font(11), text_color=theme.TEXT_FAINT
                     ).pack(side="right")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 18))
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=1)

        # 试题拖拽区
        q_zone, self._question_status_label = self._build_drop_zone(
            row, "试题文件", self._browse_question_file)
        q_zone.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        theme.register_drop_target_recursive(q_zone, self._on_question_drop)
        self._question_zone = q_zone

        # 答案拖拽区
        a_zone, self._answer_status_label = self._build_drop_zone(
            row, "答案文件", self._browse_answer_file)
        a_zone.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        theme.register_drop_target_recursive(a_zone, self._on_answer_drop)
        self._answer_zone = a_zone

        # 合并按钮
        theme.primary_button(row, "合 并", self._on_merge_click,
                             width=92, height=72, font=theme.font(13, "bold")
                             ).grid(row=0, column=2)

    @staticmethod
    def _build_drop_zone(parent, title: str, browse_command):
        """构建单个文件拖拽/点击选择区域，返回 (区域Frame, 状态Label)"""
        zone = ctk.CTkFrame(parent, fg_color=theme.CARD_INNER, corner_radius=10,
                            border_width=1, border_color=theme.BORDER, height=72)
        zone.pack_propagate(False)

        ctk.CTkLabel(zone, text=title, font=theme.font(12, "bold"),
                     text_color=theme.TEXT_MUTED).pack(pady=(14, 0))
        status = ctk.CTkLabel(zone, text="点击选择或拖拽文件到此处",
                              font=theme.font(11), text_color=theme.TEXT_FAINT)
        status.pack(pady=(2, 0))

        theme.bind_click_recursive(zone, lambda e: browse_command())
        return zone, status

    def _build_folder_card(self, parent):
        """卡片: 文件夹列表"""
        card = theme.card(parent)
        card.grid(row=2, column=0, sticky="nsew", pady=(0, 12))

        theme.card_title(card, "文件夹列表").pack(fill="x", padx=20, pady=(16, 8))

        tree_box = ctk.CTkFrame(card, fg_color="transparent")
        tree_box.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        columns = ("序号", "文件夹名称", "清空文件", "删除文件夹")
        self.folder_tree = ttk.Treeview(tree_box, columns=columns,
                                        show="headings", height=5,
                                        style="Card.Treeview")
        for col in columns:
            self.folder_tree.heading(col, text=col)
        self.folder_tree.column("序号", width=56, anchor="center", stretch=False)
        self.folder_tree.column("文件夹名称", width=300, anchor="w")
        self.folder_tree.column("清空文件", width=90, anchor="center", stretch=False)
        self.folder_tree.column("删除文件夹", width=90, anchor="center", stretch=False)

        # 绑定点击事件：点击"清空文件"或"删除文件夹"列时触发操作
        self.folder_tree.bind("<ButtonRelease-1>", self._on_folder_tree_click)
        self.folder_tree.bind("<Motion>", self._on_folder_tree_motion)

        sb = theme.attach_tree_scrollbar(tree_box, self.folder_tree)
        self.folder_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(6, 0))

        # 刷新文件夹列表
        self._refresh_folder_list()

    def _build_failed_card(self, parent):
        """卡片: 上传失败文件列表"""
        card = theme.card(parent)
        card.grid(row=3, column=0, sticky="nsew")

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(16, 8))
        ctk.CTkLabel(head, text="●", font=theme.font(11),
                     text_color=theme.DANGER, width=14, anchor="w").pack(side="left")
        theme.card_title(head, "上传失败文件（需处理）").pack(side="left")

        tree_box = ctk.CTkFrame(card, fg_color="transparent")
        tree_box.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        failed_columns = ("ID", "文件名", "失败原因", "重试次数", "Agent接管", "重新上传", "忽略")
        self.failed_tree = ttk.Treeview(tree_box, columns=failed_columns,
                                        show="headings", height=5,
                                        style="Card.Treeview")
        for col in failed_columns:
            self.failed_tree.heading(col, text=col)
        self.failed_tree.column("ID", width=44, anchor="center", stretch=False)
        self.failed_tree.column("文件名", width=150, anchor="w")
        self.failed_tree.column("失败原因", width=190, anchor="w")
        self.failed_tree.column("重试次数", width=64, anchor="center", stretch=False)
        self.failed_tree.column("Agent接管", width=76, anchor="center", stretch=False)
        self.failed_tree.column("重新上传", width=80, anchor="center", stretch=False)
        self.failed_tree.column("忽略", width=60, anchor="center", stretch=False)

        # 失败行浅红底色
        self.failed_tree.tag_configure("failed_row", background=theme.DANGER_SOFT)

        # 绑定点击事件
        self.failed_tree.bind("<ButtonRelease-1>", self._on_failed_tree_click)
        self.failed_tree.bind("<Motion>", self._on_failed_tree_motion)

        sb = theme.attach_tree_scrollbar(tree_box, self.failed_tree)
        self.failed_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(6, 0))

        # 初始加载失败列表
        self._load_failed_records()

    def _build_log_card(self, parent):
        """卡片: 运行日志"""
        card = theme.card(parent)
        card.grid(row=0, column=0, sticky="nsew")

        theme.card_title(card, "运行日志").pack(fill="x", padx=20, pady=(16, 8))

        log_box = ctk.CTkFrame(card, fg_color="transparent")
        log_box.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self.log_text = tk.Text(
            log_box, wrap="word", state="disabled", width=10,
            font=(theme.FONT_FAMILY, 9), bg=theme.CARD, fg=theme.TEXT_MUTED,
            relief="flat", bd=0, highlightthickness=0,
            spacing1=3, spacing3=3, padx=4,
            selectbackground=theme.PRIMARY_SOFT, selectforeground=theme.TEXT)

        sb = ctk.CTkScrollbar(log_box, command=self.log_text.yview,
                              button_color=theme.TEXT_FAINT,
                              button_hover_color=theme.TEXT_MUTED)
        self.log_text.configure(yscrollcommand=sb.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y", padx=(6, 0))

        # 配置日志文本样式
        self.log_text.tag_configure("error", foreground=theme.DANGER)
        self.log_text.tag_configure("success", foreground=theme.SUCCESS)
        self.log_text.tag_configure("info", foreground=theme.TEXT_MUTED)

    # ==================== 数据统计页 ====================

    def _create_stats_page(self):
        """创建作业上传数据统计页"""
        self.stats_panel = StatsPanel(self.page_stats, self.db, self.root)

    def _start_stats_refresh(self):
        """定时刷新统计面板（仅当统计页可见且窗口未隐藏时）"""
        def _auto_refresh():
            try:
                if (hasattr(self, 'stats_panel') and self._is_stats_tab_selected()
                        and self.root.state() == "normal"):
                    self.stats_panel._refresh_bar_chart()
                    self.stats_panel._refresh_line_chart()
                    self.stats_panel._refresh_upload_table()
                    self.stats_panel._refresh_failed_table()
            finally:
                self.root.after(30000, _auto_refresh)
        self.root.after(30000, _auto_refresh)

    def _is_stats_tab_selected(self) -> bool:
        """判断当前是否选中了统计页"""
        return self._current_page == "stats"

    # ==================== 合并文件相关方法 ====================

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

    def _validate_merge_file(self, path: str) -> bool:
        """校验合并文件的存在性与格式"""
        if not os.path.isfile(path):
            messagebox.showwarning("警告", f"文件不存在: {path}")
            return False
        ext = FileMerger.get_format(path)
        if ext not in FileMerger.SUPPORTED_EXTENSIONS:
            messagebox.showwarning("警告",
                f"不支持的文件格式 ({ext})\n仅支持: {', '.join(FileMerger.SUPPORTED_EXTENSIONS)}")
            return False
        return True

    @staticmethod
    def _truncate_name(path: str, limit: int = 40) -> str:
        """截断过长文件名用于展示"""
        display = os.path.basename(path)
        if len(display) > limit:
            display = display[:limit - 3] + "..."
        return display

    def _set_question_file(self, path: str):
        """设置试题文件路径并更新UI"""
        if not self._validate_merge_file(path):
            return
        self.question_file_path = path
        self._question_status_label.configure(
            text=self._truncate_name(path), text_color=theme.TEXT)
        self._question_zone.configure(border_color=theme.PRIMARY)

    def _set_answer_file(self, path: str):
        """设置答案文件路径并更新UI"""
        if not self._validate_merge_file(path):
            return
        self.answer_file_path = path
        self._answer_status_label.configure(
            text=self._truncate_name(path), text_color=theme.TEXT)
        self._answer_zone.configure(border_color=theme.PRIMARY)

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

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("选择保存目录")
        dialog.geometry("440x430")
        dialog.configure(fg_color=theme.BG)
        dialog.transient(self.root)
        # 居中
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 440) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 430) // 2
        dialog.geometry(f"+{x}+{y}")
        try:
            dialog.wait_visibility()
            dialog.grab_set()
        except Exception:
            pass

        result = {"path": None}
        selected = {"name": None}

        ctk.CTkLabel(dialog, text="请选择保存的子目录", font=theme.font(14, "bold"),
                     text_color=theme.TEXT).pack(pady=(20, 2))
        ctk.CTkLabel(dialog, text=root_dir, font=theme.font(10),
                     text_color=theme.TEXT_FAINT).pack()

        # 目录列表(圆角滚动区 + 可选中条目)
        list_frame = ctk.CTkScrollableFrame(
            dialog, fg_color=theme.CARD, corner_radius=12,
            border_width=1, border_color=theme.BORDER,
            scrollbar_button_color=theme.TEXT_FAINT,
            scrollbar_button_hover_color=theme.TEXT_MUTED)
        list_frame.pack(fill="both", expand=True, padx=20, pady=14)

        item_buttons = {}

        def on_select(name):
            selected["name"] = name
            for n, b in item_buttons.items():
                if n == name:
                    b.configure(fg_color=theme.PRIMARY_SOFT,
                                text_color=theme.PRIMARY)
                else:
                    b.configure(fg_color="transparent",
                                text_color=theme.TEXT)

        def on_confirm():
            if not selected["name"]:
                messagebox.showwarning("提示", "请选择一个子目录", parent=dialog)
                return
            result["path"] = os.path.join(root_dir, selected["name"])
            dialog.destroy()

        for d in subdirs:
            btn = ctk.CTkButton(
                list_frame, text=d, font=theme.font(12), anchor="w",
                height=36, corner_radius=8, fg_color="transparent",
                hover_color=theme.PRIMARY_SOFT, text_color=theme.TEXT,
                command=lambda n=d: on_select(n))
            btn.pack(fill="x", pady=1)
            # 双击直接确定
            btn.bind("<Double-Button-1>", lambda e, n=d: (on_select(n), on_confirm()))
            item_buttons[d] = btn

        # 按钮区
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 20))

        theme.primary_button(btn_frame, "确定", on_confirm,
                             width=96).pack(side="right")
        theme.ghost_button(btn_frame, "取消", dialog.destroy,
                           width=96).pack(side="right", padx=(0, 10))

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

    # ==================== 文件夹管理 ====================

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
                # 插入到Treeview，操作列显示可点击文本
                iid = self.folder_tree.insert(
                    "", "end",
                    values=(index, folder_name, "清空", "删除"),
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

    # ==================== 失败文件管理 ====================

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
            self.failed_tree.insert("", "end", values=("", "当前无失败文件", "", "", "", "", ""))
            return

        # 显示每条失败记录
        for record in failed_records:
            rid = record['id']
            retry_count = record['retry_count']
            file_path = record['file_path']

            # Agent 接管结果展示
            agent_result = record.get('agent_retry_success')
            if agent_result == '是':
                agent_display = '成功'
            elif agent_result == '否':
                agent_display = '失败'
            else:
                agent_display = '—'

            # 根据重试次数决定"重新上传"按钮状态
            if retry_count >= max_retry:
                retry_text = "已达上限"
            else:
                retry_text = "重传"

            iid = self.failed_tree.insert("", "end", values=(
                rid,
                record['file_name'],
                record['error_message'] or "未知错误",
                retry_count,
                agent_display,
                retry_text,
                "忽略"
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

        # 列 '#6' = 重新上传, '#7' = 忽略
        if column == '#6':
            if retry_count >= max_retry:
                messagebox.showwarning("提示", f"已达到最大重试次数({max_retry})，无法继续重试")
                return
            self._retry_upload(record_id, file_path)
        elif column == '#7':
            self._ignore_record(record_id)

    def _on_failed_tree_motion(self, event):
        """鼠标在失败列表上移动时切换光标样式"""
        column = self.failed_tree.identify_column(event.x)
        # 只有"重新上传"列(且非disabled状态)和"忽略"列显示手型
        if column == '#7':
            self.failed_tree.configure(cursor="hand2")
        elif column == '#6':
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

    # ==================== 日志与状态 ====================

    def _update_logs(self):
        """
        从日志队列中读取消息并显示在界面上
        使用after方法定时检查,确保线程安全
        """
        try:
            # 每回调最多处理50条普通日志,避免恢复窗口瞬间被整队列排空
            # 阻塞主循环
            processed = 0
            failed_list_dirty = False
            while True:
                # 非阻塞方式读取日志队列
                message = self.log_queue.get_nowait()

                # 如果是特殊指令,执行相应操作
                if message == "REFRESH_FAILED_LIST":
                    # 同一轮 drain 内合并为一次刷新(批量失败会连发多条,
                    # 每条都做 SQLite 查询 + 失败树全量重建, 100条就是
                    # 100次全量重建)。合并后一批只重建一次。
                    failed_list_dirty = True
                    continue

                if message.startswith("BROWSER_STATUS:"):
                    status = message.split(":", 1)[1]
                    if status == "CONNECTED":
                        self._set_browser_status("connected")
                    elif status == "DISCONNECTED":
                        self._set_browser_status("disconnected")
                    continue

                if message.startswith("BATCH_DONE:"):
                    # 批次上传完成 → 右下角托盘气泡通知
                    self._notify_batch_done(message[len("BATCH_DONE:"):])
                    continue

                # 显示日志消息
                tag = "info"
                if "错误" in message or "失败" in message or "✗" in message:
                    tag = "error"
                elif "成功" in message or "✓" in message:
                    tag = "success"
                self._append_log_line(message, tag)
                processed += 1
                if processed >= 50:
                    break

        except Empty:
            # 队列为空,正常情况
            pass

        except Exception as e:
            print(f"更新日志失败: {e}")

        if failed_list_dirty:
            try:
                self._load_failed_records()
            except Exception as e:
                print(f"刷新失败列表失败: {e}")

        # 队列仍有积压则 25ms 内续排(保持最小节拍而非 0ms 自续排:
        # 0ms 链会独占事件循环, 挤压统计刷新/透明重绘/toast 等其他回调)
        if processed >= 50 and not self.log_queue.empty():
            self.root.after(25, self._update_logs)
        else:
            self.root.after(100, self._update_logs)

    def _append_log_line(self, message: str, tag: str):
        """追加日志行并裁剪超限行数(两处插入共用的唯一入口,上限1000行)"""
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"{message}\n", tag)
            try:
                line_count = int(self.log_text.index("end-1c").split(".")[0])
                if line_count > 1000:
                    # 保留最近1000行: line_count-999 而不是 line_count-1000,
                    # 否则 1001 行时 delete("1.0","1.0") 是空区间 no-op,
                    # 上限实际漂移到 1001-1002 行
                    self.log_text.delete("1.0", f"{line_count - 999}.0")
            except Exception:
                pass
            self.log_text.see("end")  # 滚动到底部
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _refresh_failed_list(self):
        """
        定时刷新失败列表
        每5秒检查一次是否有新的失败记录（窗口隐藏时暂停重建，避免恢复时阻塞）
        """
        if self.root.state() == "normal":
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
            self._set_browser_status("connected")
        elif status == "未连接":
            self._set_browser_status("error")
        elif status == "重启中":
            self._set_browser_status("restarting")

    # ==================== 系统托盘与退出 ====================

    def _notify_batch_done(self, stats_json: str):
        """
        批次上传完成通知：右下角自绘置顶 toast 窗口提醒本次上传统计。
        在 tkinter 主线程中调用（由 _update_logs 分发）。
        不依赖系统通知/托盘气泡（部分系统会拦截 Shell_NotifyIcon 气泡），
        窗口最小化到托盘时 toast 同样可见。

        Args:
            stats_json: BATCH_DONE 指令携带的统计 JSON
                        {total, direct_success, agent_recovered, manual, retrying}
        """
        if self.stop_event.is_set():
            return  # 程序正在退出，不再通知
        try:
            stats = json.loads(stats_json)
        except (ValueError, TypeError):
            stats = {}
        self._show_batch_toast(stats)

    def _get_toast_position(self, w: int, h: int):
        """
        计算通知卡片在屏幕右下角的位置（物理像素，与 Tk geometry 一致）。

        实测（125% DPI）：Tk 窗口 geometry 坐标与 GetWindowRect 物理像素 1:1
        对应，GetMonitorInfo 的 Work 区域同为物理像素，可直接使用，无需按
        DPI 缩放换算（winfo_screenwidth 报告的是缩放后的逻辑尺寸，用于定位
        会偏到屏幕中间）。优先主窗口所在显示器，其次鼠标所在显示器，支持
        多屏——主窗口/鼠标在副屏时按副屏右下角定位。

        Returns:
            (x, y) 或 None（win32 不可用时调用方回退）
        """
        try:
            import win32api
            try:
                import win32con
                _nearest = win32con.MONITOR_DEFAULTTONEAREST
            except Exception:
                _nearest = 2  # MONITOR_DEFAULTTONEAREST

            # 目标点：主窗口中心（窗口可见时），否则鼠标位置
            tk_x, tk_y = self.root.winfo_pointerx(), self.root.winfo_pointery()
            if self.root.state() != "withdrawn":
                cx = self.root.winfo_x() + max(self.root.winfo_width() // 2, 0)
                cy = self.root.winfo_y() + max(self.root.winfo_height() // 2, 0)
            elif tk_x > 0:
                cx, cy = tk_x, tk_y
            else:
                return None

            # 找到该点所在显示器的工作区（排除任务栏）
            monitor = win32api.MonitorFromPoint((int(cx), int(cy)), _nearest)
            work = win32api.GetMonitorInfo(monitor)['Work']
            right, bottom = work[2], work[3]
            return (right - w - 24, bottom - h - 16)
        except Exception:
            return None

    def _show_batch_toast(self, stats: dict):
        """
        显示批次完成通知卡片（右下角置顶，淡入淡出，约6秒自动关闭，
        点击任意处恢复主窗口）。新的批次通知会替换旧的。
        """
        # 关闭上一个 toast（连续批次只显示最新）
        if self._batch_toast is not None:
            try:
                self._batch_toast.destroy()
            except Exception:
                pass
            self._batch_toast = None

        total = stats.get('total', 0)
        success = stats.get('direct_success', 0)
        agent_recovered = stats.get('agent_recovered', 0)
        manual = stats.get('manual', 0)
        retrying = stats.get('retrying', 0)

        # 用原生 Toplevel:CTkToplevel 的 geometry() 会被 customtkinter 按 DPI
        # 缩放改写(尺寸缩小、查询值漂移),无法精确贴右下角;原生 Toplevel 的
        # geometry 与 win32 物理像素 1:1 对应,定位精确,内部放 CTk 控件保持主题
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)          # 无系统边框
        toast.attributes("-topmost", True)    # 始终置顶
        toast.configure(bg=theme.CARD)        # 圆角外露背景与卡片同色
        self._batch_toast = toast

        body = ctk.CTkFrame(toast, fg_color=theme.CARD_INNER, corner_radius=10)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # 标题行 + 关闭按钮
        title_row = ctk.CTkFrame(body, fg_color="transparent")
        title_row.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(title_row, text="作业上传完成",
                     font=theme.font(16, "bold"), text_color=theme.PRIMARY).pack(side="left")
        close_btn = ctk.CTkLabel(title_row, text="×",
                                 font=theme.font(17, "bold"), text_color=theme.TEXT_MUTED,
                                 cursor="hand2")
        close_btn.pack(side="right")

        # 统计行（四项）
        rows = [
            ("作业总数", total, theme.TEXT),
            ("上传成功", success, theme.SUCCESS),
            ("智能体修复", agent_recovered, theme.PRIMARY),
            ("需手动处理", manual, theme.DANGER if manual > 0 else theme.TEXT_MUTED),
        ]
        for label, value, color in rows:
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=2)
            ctk.CTkLabel(row, text=label, font=theme.font(14),
                         text_color=theme.TEXT_MUTED).pack(side="left")
            ctk.CTkLabel(row, text=str(value), font=theme.font(14, "bold"),
                         text_color=color).pack(side="right")
        if retrying > 0:
            ctk.CTkLabel(body, text=f"另有 {retrying} 个正在重试中",
                         font=theme.font(13), text_color=theme.WARNING).pack(
                anchor="w", padx=16, pady=(4, 12))

        # 定位到屏幕右下角（避开任务栏）：
        # 优先主窗口所在显示器，其次鼠标所在显示器（支持多屏），
        # winfo_screenwidth 只覆盖主屏，窗口/鼠标在副屏时位置会跑偏。
        # 坐标直接用物理像素（与 Tk geometry 1:1 对应，实测无需 DPI 换算）。
        toast.update_idletasks()
        h = toast.winfo_reqheight()
        width = 420
        pos = self._get_toast_position(width, h)
        if pos is None:
            try:
                import win32api
                import win32con
                screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
                screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            except Exception:
                screen_w = self.root.winfo_screenwidth()
                screen_h = self.root.winfo_screenheight()
            pos = (max(0, screen_w - width - 24), max(0, screen_h - h - 16))
        toast.geometry(f"{width}x{h}+{pos[0]}+{pos[1]}")

        # 点击任意处（除关闭按钮）恢复主窗口
        for widget in (toast, body, title_row):
            widget.bind("<Button-1>", lambda e: self._toast_clicked())

        def _close():
            """直接销毁（关闭按钮 / 6秒自动关闭共用）"""
            if self._batch_toast is not toast:
                return
            try:
                toast.destroy()
            except Exception:
                pass
            if self._batch_toast is toast:
                self._batch_toast = None

        close_btn.bind("<Button-1>", lambda e: _close())
        self.root.after(8000, _close)  # 8秒后自动关闭

    def _toast_clicked(self):
        """点击通知卡片：关闭 toast 并恢复主窗口"""
        if self._batch_toast is not None:
            try:
                self._batch_toast.destroy()
            except Exception:
                pass
            self._batch_toast = None
        self._restore_window_impl()

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

            # 生成托盘图标（主题蓝底白色上传箭头）
            def _make_icon():
                img = Image.new('RGB', (64, 64), color=theme.PRIMARY)
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

            # 自定义 setup：图标 NIM_ADD 完成后置位就绪事件。
            # notify() 底层是同步 Shell_NotifyIcon(NIM_MODIFY)，图标未创建前调用会
            # 静默失败（气泡不显示），必须先等就绪再通知
            self._tray_ready.clear()

            def _tray_setup_cb(icon):
                icon.visible = True
                self._tray_ready.set()

            self.tray_thread = Thread(
                target=lambda: self.tray_icon.run(setup=_tray_setup_cb), daemon=True)
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
        self.root.after(0, self._restore_window_impl)

    def _restore_window_impl(self):
        """在主线程中恢复窗口（从托盘恢复）"""
        self._restoring_from_tray = True  # 抑制 deiconify 触发的 <Map> 重复处理
        try:
            # 1. 先恢复任务栏样式（顶层窗口，64 位 API）
            self._show_on_taskbar()
            # 2. 透明重绘恢复: 最小化恢复时 Windows 需全量重绘, 重绘完成前
            #    窗口显示背景色(黑屏)。先置透明→强制完成首帧绘制→恢复可见,
            #    用户看到的始终是完整内容
            self._reveal_window_after_redraw(do_deiconify=True)
            self.root.lift()
            self.root.focus_force()
            # 统一走 win32_helpers 封装(已配置 argtypes), 替代裸 ctypes 调用
            win32_helpers.set_foreground_window(self._top_hwnd)
        finally:
            self._restoring_from_tray = False

    def _reveal_window_after_redraw(self, do_deiconify: bool = False):
        """
        透明重绘恢复窗口: alpha 0 → (deiconify) → update() 完成首帧绘制 → alpha 1。
        避免恢复最小化窗口时的"先黑屏再加载"。
        窗口已处于 normal 状态时直接跳过(对已可见窗口闪一帧全透明毫无意义,
        且 update() 会排空整个事件队列, 积压的日志链会让窗口长时间保持不可见)。
        任一步失败都保证 alpha 最终恢复 1.0（finally 兜底）。
        """
        if self.root.state() == "normal":
            return
        try:
            self.root.attributes("-alpha", 0.0)
        except Exception:
            # 不支持透明(个别环境): 退化为普通恢复, 不阻塞
            try:
                if do_deiconify:
                    self.root.deiconify()
                self.root.update_idletasks()
            except Exception:
                pass
            return
        try:
            if do_deiconify:
                self.root.deiconify()
            self.root.update()  # 处理事件直到队列空, 强制完成 WM_PAINT 重绘
        except Exception:
            pass
        finally:
            try:
                self.root.attributes("-alpha", 1.0)
            except Exception:
                pass

    def _on_window_unmap(self, event):
        """窗口被隐藏/最小化（含托盘 iconify）时记录, 供 <Map> 区分首次显示"""
        self._ever_unmapped = True

    def _on_window_map(self, event):
        """
        窗口恢复可见时的统一状态同步点:
        - 托盘路径由 _restore_window_impl 主动处理(_restoring_from_tray 抑制)
        - 外部路径(任务视图/Alt-Tab/Win+D/会话解锁)把"托盘隐藏中"的窗口恢复
          可见时, 这里同步恢复任务栏样式, 避免"可见但无任务栏按钮"的僵尸态
        - 首次启动显示由 _ever_unmapped 排除
        - 普通最小化还原时做透明重绘避免黑屏首帧
        """
        if not self._ever_unmapped:
            return
        if self._restoring_from_tray:
            return  # 托盘路径: _restore_window_impl 已完成任务栏还原+透明重绘
        if self._taskbar_hidden:
            # 窗口已被外部方式恢复可见: 状态与现实已不一致, 同步还原任务栏按钮
            self._show_on_taskbar()
        self._reveal_window_after_redraw(do_deiconify=False)

    def _hide_from_taskbar(self):
        """隐藏任务栏条目（顶层窗口移除 WS_EX_APPWINDOW、加 WS_EX_TOOLWINDOW）。
        0 是合法的扩展样式前值, 不是失败标志(set_ex_style 内部用 GetLastError
        判断真实失败)。失败时记录日志并保持 _taskbar_hidden=False, 由
        _on_closing 据实回退, 不再静默吞掉。"""
        if self._taskbar_hidden:
            return
        try:
            style = win32_helpers.get_ex_style(self._top_hwnd)
            # _original_exstyle 已在 _create_widgets 记录, 无需惰性兜底
            new_style = ((style & ~win32_helpers.WS_EX_APPWINDOW)
                         | win32_helpers.WS_EX_TOOLWINDOW)
            if win32_helpers.set_ex_style(self._top_hwnd, new_style):
                self._taskbar_hidden = True     # 仅成功时置位
            else:
                self._log_to_gui("警告: 隐藏任务栏按钮失败(扩展样式设置失败)", "error")
        except Exception as e:
            self._log_to_gui(f"警告: 隐藏任务栏按钮失败: {e}", "error")

    def _show_on_taskbar(self):
        """恢复任务栏条目（还原原始窗口样式）。失败时如实记录"""
        if not self._taskbar_hidden:
            return
        try:
            if self._original_exstyle is not None:
                if win32_helpers.set_ex_style(self._top_hwnd, self._original_exstyle):
                    self._taskbar_hidden = False
                else:
                    self._log_to_gui("警告: 恢复任务栏按钮失败(扩展样式设置失败)", "error")
        except Exception as e:
            self._log_to_gui(f"警告: 恢复任务栏按钮失败: {e}", "error")

    def _hide_window_to_tray(self):
        """隐藏窗口到托盘: 用 iconify 而非 ShowWindow(SW_HIDE)

        iconify 最小化窗口,Windows 保留显示表面,恢复时直接显示缓存,
        无黑屏/全量重绘;配合 _hide_from_taskbar 移除任务栏按钮后最小化
        窗口完全不可见,等效"隐藏"。且 state() 变 "iconic" 使 customtkinter
        的 DPI 轮询跳过隐藏窗口。
        旧实现把 SW_HIDE 发给 winfo_id 的客户区子窗口,只隐藏内容区,
        残留带标题栏的黑框窗口。
        """
        try:
            self.root.iconify()
        except Exception as e:
            self._log_to_gui(f"警告: 最小化窗口失败: {e}", "error")

    def _quit_app(self):
        """从托盘菜单完全退出程序"""
        self._perform_exit()

    def _perform_exit(self):
        """统一的退出流程：设停止信号、等队列清空、在主线程中销毁窗口"""
        self.stop_event.set()
        # 清理统计面板资源（线程安全：在事件循环线程中执行）
        if hasattr(self, 'stats_panel'):
            try:
                self.stats_panel.destroy()
            except Exception:
                pass
        # 还原设置页滚动修复的子类化窗口过程与全局滚轮绑定
        # (否则退出时子类化过程被遗留在已销毁/复用的 HWND 上)
        settings_fix = getattr(self, "_settings_ghost_fix", None)
        if settings_fix is not None:
            try:
                settings_fix.uninstall()
            except Exception:
                pass
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

        if not self.task_queue.empty():
            messagebox.showinfo("提示", "等待当前任务完成...")

        # 用 root.after 在主线程中延迟销毁窗口，给后台线程少量时间完成当前任务
        # 避免 daemon 线程调用 tkinter destroy 导致的线程安全问题
        def _safe_destroy():
            try:
                self.root.destroy()
            except Exception:
                pass

        self.root.after(2000, _safe_destroy)  # 2秒缓冲，后台线程在 stop_event 后最快退出

    def _log_to_gui(self, message: str, tag: str = "info"):
        """向GUI日志区域写入消息（线程安全）"""
        try:
            self.root.after(0, lambda: self._insert_log(message, tag))
        except Exception:
            pass

    def _insert_log(self, message: str, tag: str):
        """实际执行日志插入（必须在主线程调用）"""
        self._append_log_line(message, tag)

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
                    # 先隐藏任务栏条目，再隐藏窗口（避免黑屏）
                    self._hide_from_taskbar()
                    self._hide_window_to_tray()
                    # 验证隐藏是否完整生效: 样式设置或 iconify 任一环节失败时
                    # 窗口仍留在屏幕/任务栏, 如实告知, 避免"以为已退出"的误解
                    if not (self._taskbar_hidden
                            and self.root.state() == "iconic"):
                        self._log_to_gui(
                            "警告: 隐藏到托盘未完全生效，窗口仍保留在屏幕/任务栏，"
                            "可点击任务栏窗口或托盘图标恢复", "error")
                    if self.tray_icon and hasattr(self.tray_icon, 'notify'):
                        try:
                            # 等待图标 NIM_ADD 完成再通知，否则气泡静默丢失
                            if self._tray_ready.wait(timeout=2):
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

    # ==================== 设置页 ====================

    def _create_settings_page(self):
        """创建设置页面: 顶部保存按钮 + 分组配置卡片"""
        page = self.page_settings
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        # 顶部保存按钮栏
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._settings_save_btn = theme.primary_button(
            header, "保存设置", self._save_settings, width=120)
        self._settings_save_btn.pack(side="right")
        self._settings_status_label = ctk.CTkLabel(
            header, text="", font=theme.font(11), text_color=theme.SUCCESS)
        self._settings_status_label.pack(side="right", padx=12)

        # 可滚动配置区
        scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        self._settings_scroll = scroll
        # 滚动残影修复(与统计面板共用同一实现; 页面被盖住时 active_check 跳过重绘;
        # _perform_exit 中 uninstall 还原)
        self._settings_ghost_fix = win32_helpers.ScrollGhostFix(
            scroll, self.root,
            active_check=lambda: self._current_page == "settings")

        # 存储所有配置控件的引用: key -> {"var": ..., "type": ..., "required": ...}
        self._settings_widgets = {}
        editable = self.config.get_all_editable()

        for group_idx, (group_name, items) in enumerate(editable.items()):
            card = theme.card(scroll)
            card.grid(row=group_idx, column=0, sticky="ew", pady=(0, 12))
            theme.card_title(card, group_name).pack(fill="x", padx=20, pady=(16, 12))

            for item in items:
                row = ctk.CTkFrame(card, fg_color="transparent")
                row.pack(fill="x", padx=20, pady=(0, 10))
                row.grid_columnconfigure(1, weight=1)

                # 标签 + 帮助文字
                label_col = ctk.CTkFrame(row, fg_color="transparent")
                label_col.grid(row=0, column=0, sticky="w", padx=(0, 16))
                theme.settings_label(label_col, item["label"]).pack(anchor="w")
                if item.get("help"):
                    theme.settings_help(label_col, item["help"]).pack(anchor="w")

                # 根据类型创建控件
                key = item["key"]
                current_val = self.config.get(key, item["default"])
                cfg_type = item["type"]

                if cfg_type == "bool":
                    var = ctk.BooleanVar(value=bool(current_val))
                    widget = theme.settings_switch(row, var)
                    widget.grid(row=0, column=1, sticky="w")
                    self._settings_widgets[key] = {
                        "var": var, "type": "bool",
                        "required": bool(item.get("required"))}
                elif cfg_type == "combo":
                    var = ctk.StringVar(value=str(current_val))
                    widget = theme.settings_combo(row, var, values=item.get("options", []))
                    widget.grid(row=0, column=1, sticky="w")
                    self._settings_widgets[key] = {
                        "var": var, "type": "combo",
                        "required": bool(item.get("required"))}
                elif cfg_type == "int":
                    var = ctk.StringVar(value=str(current_val))
                    widget = theme.settings_entry(row, var)
                    widget.grid(row=0, column=1, sticky="w")
                    self._settings_widgets[key] = {
                        "var": var, "type": "int",
                        "required": bool(item.get("required"))}
                elif cfg_type == "float":
                    var = ctk.StringVar(value=str(current_val))
                    widget = theme.settings_entry(row, var)
                    widget.grid(row=0, column=1, sticky="w")
                    self._settings_widgets[key] = {
                        "var": var, "type": "float",
                        "required": bool(item.get("required"))}
                else:  # str
                    var = ctk.StringVar(value=str(current_val))
                    widget = theme.settings_entry(row, var)
                    widget.grid(row=0, column=1, sticky="w")
                    self._settings_widgets[key] = {
                        "var": var, "type": "str",
                        "required": bool(item.get("required"))}

    def _save_settings(self):
        """
        保存所有设置到 config.json: 先全量校验再一次性原子落盘(set_many)。
        任何字段格式非法或必填字段为空都不落盘, 避免旧的逐键 set() 在循环
        中途失败时留下"已保存一半却报失败"的混合状态(且 20+ 次磁盘写并
        非原子, 崩溃会截断 config.json)。
        """
        type_map = {
            "int": int,
            "float": float,
            "bool": lambda v: bool(v),
            "str": str,
            "combo": str,
        }
        errors = []
        updates = {}
        for key, info in self._settings_widgets.items():
            raw = info["var"].get()
            converter = type_map[info["type"]]
            try:
                value = converter(raw)
            except (ValueError, TypeError):
                errors.append(f"{key}: 格式错误")
                continue
            # 必填字符串字段不允许清空(如 ROOT_DIR 清空会导致文件监控启动失败);
            # 非必填清空则保存空串(覆盖旧值, ConfigManager 属性层的回退链
            # 会把空串归一化为"未配置", 否则旧值残留会导致回退链永不生效)
            if info["type"] in ("str", "combo") and isinstance(value, str) \
                    and not value.strip():
                if info.get("required"):
                    errors.append(f"{key}: 不能为空")
                    continue
            updates[key] = value

        if errors:
            self._settings_status_label.configure(
                text=f"保存失败: {'; '.join(errors)}", text_color=theme.DANGER)
            return  # 有任何错误则完全不落盘

        try:
            self.config.set_many(updates)
        except Exception as e:
            self._settings_status_label.configure(
                text=f"保存失败: {e}", text_color=theme.DANGER)
            return
        self._settings_status_label.configure(
            text="✓ 已保存", text_color=theme.SUCCESS)
        # 3秒后清除状态文字
        self.root.after(3000, lambda: self._settings_status_label.configure(text=""))
