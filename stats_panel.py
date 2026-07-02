"""
作业上传数据统计面板模块
功能: 提供数据复制、柱状图、折线图、上传记录表、失败记录表及Excel导出
技术: tkinter + matplotlib + openpyxl
"""
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import MultipleLocator
import matplotlib.pyplot as plt


class StatsPanel:
    """
    数据统计面板
    包含图表可视化、数据表格和Excel导出功能
    """

    def __init__(self, parent: ttk.Frame, db, root: tk.Tk):
        self.parent = parent
        self.db = db
        self.root = root

        # 图表状态
        self._bar_mode = "subject"       # "subject" | "school_grade"
        self._line_mode = "daily"        # "daily" | "weekly" | "monthly"

        # matplotlib figure 引用(用于内存清理)
        self._bar_figure = None
        self._line_figure = None
        self._bar_canvas = None
        self._line_canvas = None

        # 数据是否已复制到分析表
        self._data_copied = False

        # 中文字体
        self._setup_chinese_font()

        # 构建界面
        self._create_widgets()

    @staticmethod
    def _setup_chinese_font():
        """配置matplotlib中文字体支持"""
        plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'WenQuanYi Micro Hei', 'Arial Unicode MS']
        plt.rcParams['axes.unicode_minus'] = False

    # ==================== 界面构建 ====================

    def _create_widgets(self):
        """创建统计面板所有界面组件"""
        # 可滚动画布
        self._canvas = tk.Canvas(self.parent, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self.parent, orient="vertical", command=self._canvas.yview)
        self._inner_frame = ttk.Frame(self._canvas)

        self._inner_frame.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_window = self._canvas.create_window(
            (0, 0), window=self._inner_frame, anchor="nw")

        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._canvas.bind("<Enter>", self._bind_mousewheel)
        self._canvas.bind("<Leave>", self._unbind_mousewheel)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        # === 1. 数据管理区 ===
        self._create_action_bar()

        # === 2. 柱状图: 作业上传数量 ===
        self._create_bar_chart_section()

        # === 3. 折线图: 作业上传趋势 ===
        self._create_line_chart_section()

        # === 4. 上传记录表 ===
        self._create_upload_table_section()

        # === 5. 失败记录表 ===
        self._create_failed_table_section()

    def _create_action_bar(self):
        """数据管理区: 复制数据按钮"""
        frame = ttk.LabelFrame(self._inner_frame, text="【数据管理】", padding=10)
        frame.pack(fill="x", padx=10, pady=5)

        self.copy_btn = ttk.Button(frame, text="📋 复制数据到分析表",
                                    command=self._on_copy_data)
        self.copy_btn.pack(side="left", padx=5)

        self._copy_status_label = ttk.Label(frame, text="尚未复制数据", foreground="gray")
        self._copy_status_label.pack(side="left", padx=10)

    def _create_bar_chart_section(self):
        """柱状图区: 作业上传数量图"""
        frame = ttk.LabelFrame(self._inner_frame, text="【作业上传数量图】", padding=10)
        frame.pack(fill="x", padx=10, pady=5)

        toggle_frame = ttk.Frame(frame)
        toggle_frame.pack(fill="x", pady=(0, 5))

        self._bar_toggle_btn = ttk.Button(toggle_frame, text="切换为: 按学校+年级",
                                           command=self._toggle_bar_chart)
        self._bar_toggle_btn.pack(side="left", padx=5)

        self._bar_figure = Figure(figsize=(8, 3.5), dpi=100)
        self._bar_canvas = FigureCanvasTkAgg(self._bar_figure, master=frame)
        self._bar_canvas.get_tk_widget().pack(fill="x", padx=5, pady=5)

        # 初始显示提示
        self._show_empty_chart(self._bar_figure, "请先点击「复制数据」按钮")
        self._bar_canvas.draw()

    def _create_line_chart_section(self):
        """折线图区: 作业上传趋势图"""
        frame = ttk.LabelFrame(self._inner_frame, text="【作业上传趋势图】", padding=10)
        frame.pack(fill="x", padx=10, pady=5)

        toggle_frame = ttk.Frame(frame)
        toggle_frame.pack(fill="x", pady=(0, 5))

        self._line_buttons = {}
        for mode, label in [("daily", "按日"), ("weekly", "按周"), ("monthly", "按月")]:
            btn = ttk.Button(toggle_frame, text=label,
                             command=lambda m=mode: self._switch_line_mode(m))
            btn.pack(side="left", padx=3)
            self._line_buttons[mode] = btn

        self._line_figure = Figure(figsize=(8, 3.5), dpi=100)
        self._line_canvas = FigureCanvasTkAgg(self._line_figure, master=frame)
        self._line_canvas.get_tk_widget().pack(fill="x", padx=5, pady=5)

        self._show_empty_chart(self._line_figure, "请先点击「复制数据」按钮")
        self._line_canvas.draw()

    def _create_upload_table_section(self):
        """上传记录表"""
        frame = ttk.LabelFrame(self._inner_frame, text="【上传记录表】", padding=10)
        frame.pack(fill="both", expand=True, padx=10, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(0, 5))

        ttk.Button(btn_frame, text="📥 导出到Excel",
                   command=self._export_upload_table).pack(side="right", padx=5)

        columns = ("file_name", "school", "grade", "subject", "upload_time")
        headers = ("作业名称", "学校", "年级", "科目", "上传时间")
        upload_frame, self._upload_tree = self._create_treeview(frame, columns, headers, height=8)
        upload_frame.pack(fill="both", expand=True)

    def _create_failed_table_section(self):
        """失败记录表"""
        frame = ttk.LabelFrame(self._inner_frame, text="【失败上传记录表】", padding=10)
        frame.pack(fill="both", expand=True, padx=10, pady=5)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(0, 5))

        ttk.Button(btn_frame, text="📥 导出到Excel",
                   command=self._export_failed_table).pack(side="right", padx=5)

        columns = ("file_name", "school", "grade", "subject", "upload_time", "error_message")
        headers = ("作业名称", "学校", "年级", "科目", "上传时间", "失败原因")
        failed_frame, self._failed_tree = self._create_treeview(frame, columns, headers, height=8)
        failed_frame.pack(fill="both", expand=True)

    @staticmethod
    def _create_treeview(parent, columns, headers, height=10):
        """创建带滚动条的Treeview，返回 (容器Frame, Treeview) 元组"""
        tree_frame = ttk.Frame(parent)
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=height)

        for col, header in zip(columns, headers):
            tree.heading(col, text=header)
            if col == "error_message":
                width = 200
                anchor = "w"
            elif col == "file_name":
                width = 200
                anchor = "w"
            elif col == "upload_time":
                width = 150
                anchor = "center"
            else:
                width = 100
                anchor = "center"
            tree.column(col, width=width, anchor=anchor, minwidth=60)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)

        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return tree_frame, tree

    # ==================== 数据操作 ====================

    def _on_copy_data(self):
        """复制成功记录到分析表,并刷新所有图表和表格"""
        try:
            count = self.db.copy_success_to_analysis()
            self._data_copied = True
            self._copy_status_label.config(
                text=f"✓ 已复制 {count} 条记录到分析表", foreground="green")
            self._refresh_bar_chart()
            self._refresh_line_chart()
            self._refresh_upload_table()
            self._refresh_failed_table()
        except Exception as e:
            messagebox.showerror("错误", f"复制数据失败: {e}")

    # ==================== 图表刷新 ====================

    def _toggle_bar_chart(self):
        """切换柱状图视图: 科目 ↔ 学校+年级"""
        self._bar_mode = "school_grade" if self._bar_mode == "subject" else "subject"
        next_label = "按科目" if self._bar_mode == "subject" else "按学校+年级"
        self._bar_toggle_btn.config(text=f"切换为: {next_label}")
        self._refresh_bar_chart()

    def _switch_line_mode(self, mode):
        """切换折线图聚合方式: daily / weekly / monthly"""
        self._line_mode = mode
        self._refresh_line_chart()

    def _refresh_bar_chart(self):
        """重绘柱状图"""
        if not self._data_copied:
            self._show_empty_chart(self._bar_figure, "请先点击「复制数据」按钮")
            self._bar_canvas.draw()
            return

        if self._bar_mode == "subject":
            data = self.db.get_upload_count_by_subject()
            x_label = "科目"
            title = "各科目上传数量统计"
        else:
            data = self.db.get_upload_count_by_school_grade()
            x_label = "学校 + 年级"
            title = "各学校年级上传数量统计"

        self._bar_figure.clear()
        ax = self._bar_figure.add_subplot(111)

        if not data:
            ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                    fontsize=14, transform=ax.transAxes)
            ax.set_title(title)
            self._bar_figure.tight_layout()
            self._bar_canvas.draw()
            return

        if self._bar_mode == "subject":
            labels = [d["subject"] for d in data]
        else:
            labels = [f"{d['school']}{d['grade']}" for d in data]
        counts = [d["count"] for d in data]

        # 颜色: 用渐变蓝色
        colors = [f"#{(60 + i * 30 % 200):02X}{(120 + i * 20 % 100):02X}{(200 - i * 15 % 50):02X}"
                  for i in range(len(labels))]

        bars = ax.bar(range(len(labels)), counts, color="#4A90D9")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
        ax.set_xlabel(x_label)
        ax.set_ylabel("上传数量")
        ax.yaxis.set_major_locator(MultipleLocator(1))
        ax.set_title(title)

        # 柱顶数值标签
        for bar, count in zip(bars, counts):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    str(count), ha="center", va="bottom", fontsize=8)
        for bar, count in zip(bars, counts):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    str(count), ha="center", va="bottom", fontsize=8)

        self._bar_figure.tight_layout()
        self._bar_canvas.draw()

    def _refresh_line_chart(self):
        """重绘折线图"""
        if not self._data_copied:
            self._show_empty_chart(self._line_figure, "请先点击「复制数据」按钮")
            self._line_canvas.draw()
            return

        data = self.db.get_upload_count_by_date(self._line_mode)

        self._line_figure.clear()
        ax = self._line_figure.add_subplot(111)

        if not data:
            ax.text(0.5, 0.5, "暂无数据", ha="center", va="center",
                    fontsize=14, transform=ax.transAxes)
            ax.set_title("作业上传趋势图")
            self._line_figure.tight_layout()
            self._line_canvas.draw()
            return

        labels = [d["date_label"] for d in data]
        counts = [d["count"] for d in data]

        mode_title = {"daily": "每日", "weekly": "每周", "monthly": "每月"}

        ax.plot(range(len(labels)), counts, marker="o", linestyle="-",
                color="#E67E22", linewidth=1.5, markersize=4)
        ax.fill_between(range(len(labels)), counts, alpha=0.12, color="#E67E22")
        ax.set_xticks(range(len(labels)))

        # 标签过多时每隔N个显示一个
        n = max(1, len(labels) // 20)
        visible_labels = [label if i % n == 0 else "" for i, label in enumerate(labels)]
        ax.set_xticklabels(visible_labels, rotation=45, ha="right", fontsize=8)

        ax.set_xlabel("日期")
        ax.set_ylabel("上传数量")
        ax.yaxis.set_major_locator(MultipleLocator(1))
        ax.set_title(f"作业上传趋势图 ({mode_title[self._line_mode]})")

        # 数值标注
        for i, (x, y) in enumerate(zip(range(len(labels)), counts)):
            ax.text(x, y + 0.3, str(y), ha="center", va="bottom", fontsize=7)

        self._line_figure.tight_layout()
        self._line_canvas.draw()

    @staticmethod
    def _show_empty_chart(figure, message):
        """在图表中显示提示信息"""
        figure.clear()
        ax = figure.add_subplot(111)
        ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=14,
                color="gray", transform=ax.transAxes)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

    # ==================== 表格刷新 ====================

    def _refresh_upload_table(self):
        """刷新上传记录表"""
        for item in self._upload_tree.get_children():
            self._upload_tree.delete(item)
        records = self.db.get_all_successful_records()
        for r in records:
            self._upload_tree.insert("", "end", values=(
                r["file_name"], r["school"], r["grade"],
                r["subject"], r["upload_time"]
            ))

    def _refresh_failed_table(self):
        """刷新失败记录表"""
        for item in self._failed_tree.get_children():
            self._failed_tree.delete(item)
        records = self.db.get_failed_records_for_stats()
        for r in records:
            self._failed_tree.insert("", "end", values=(
                r["file_name"], r["school"], r["grade"],
                r["subject"], r["upload_time"],
                r["error_message"] or ""
            ))

    # ==================== Excel导出 ====================

    def _export_upload_table(self):
        """导出上传记录表到Excel"""
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel文件", "*.xlsx")],
            title="导出上传记录表"
        )
        if not path:
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, PatternFill

            wb = Workbook()
            ws = wb.active
            ws.title = "上传记录"

            # 表头
            headers = ["作业名称", "学校", "年级", "科目", "上传时间"]
            header_font = Font(bold=True, size=11)
            header_fill = PatternFill(start_color="4A90D9", end_color="4A90D9", fill_type="solid")
            header_font_white = Font(bold=True, size=11, color="FFFFFF")

            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_idx, value=header)
                cell.font = header_font_white
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")

            # 数据行
            for row_idx, item in enumerate(self._upload_tree.get_children(), 2):
                values = self._upload_tree.item(item, "values")
                for col_idx, val in enumerate(values, 1):
                    ws.cell(row=row_idx, column=col_idx, value=val)

            # 调整列宽
            ws.column_dimensions['A'].width = 40  # 作业名称
            ws.column_dimensions['B'].width = 20  # 学校
            ws.column_dimensions['C'].width = 10  # 年级
            ws.column_dimensions['D'].width = 10  # 科目
            ws.column_dimensions['E'].width = 22  # 上传时间

            wb.save(path)
            count = len(self._upload_tree.get_children())
            messagebox.showinfo("导出成功", f"已导出 {count} 条上传记录到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _export_failed_table(self):
        """导出失败记录表到Excel"""
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel文件", "*.xlsx")],
            title="导出失败记录表"
        )
        if not path:
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, PatternFill

            wb = Workbook()
            ws = wb.active
            ws.title = "失败记录"

            headers = ["作业名称", "学校", "年级", "科目", "上传时间", "失败原因"]
            header_fill = PatternFill(start_color="E74C3C", end_color="E74C3C", fill_type="solid")
            header_font = Font(bold=True, size=11, color="FFFFFF")

            for col_idx, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col_idx, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center")

            for row_idx, item in enumerate(self._failed_tree.get_children(), 2):
                values = self._failed_tree.item(item, "values")
                for col_idx, val in enumerate(values, 1):
                    ws.cell(row=row_idx, column=col_idx, value=val)

            ws.column_dimensions['A'].width = 40
            ws.column_dimensions['B'].width = 20
            ws.column_dimensions['C'].width = 10
            ws.column_dimensions['D'].width = 10
            ws.column_dimensions['E'].width = 22
            ws.column_dimensions['F'].width = 40

            wb.save(path)
            count = len(self._failed_tree.get_children())
            messagebox.showinfo("导出成功", f"已导出 {count} 条失败记录到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    # ==================== 滚动支持 ====================

    def _on_canvas_configure(self, event):
        self._canvas.itemconfig(self._canvas_window, width=event.width)

    def _bind_mousewheel(self, event):
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, event):
        self._canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ==================== 资源清理 ====================

    def destroy(self):
        """释放matplotlib figure资源"""
        if self._bar_figure is not None:
            plt.close(self._bar_figure)
            self._bar_figure = None
        if self._line_figure is not None:
            plt.close(self._line_figure)
            self._line_figure = None
