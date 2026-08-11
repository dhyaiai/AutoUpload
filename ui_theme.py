"""
UI设计系统模块
功能: 统一定义配色、字体、控件样式，供 gui_manager 与 stats_panel 使用
设计原则: 低饱和配色(主色不超过4种)、圆角卡片、单一字体、充足留白、无原生默认控件
"""
import tkinter as tk
from tkinter import ttk
import customtkinter as ctk

# ==================== 配色系统（4种主色 + 中性色） ====================
# 主色: 柔和蓝（品牌色/交互色）
PRIMARY = "#5B7CFA"
PRIMARY_HOVER = "#4A69E0"
PRIMARY_SOFT = "#EEF2FE"      # 主色浅底（选中态/悬浮底色）

# 辅助色: 柔和绿（成功）
SUCCESS = "#3BA272"
SUCCESS_SOFT = "#E9F6F0"

# 辅助色: 柔和红（失败/危险）
DANGER = "#D9695F"
DANGER_HOVER = "#C75A50"
DANGER_SOFT = "#FBF0EE"

# 辅助色: 柔和琥珀（进行中/警告）
WARNING = "#D99A3D"

# 中性色
BG = "#F4F6FB"                # 窗口背景（浅灰蓝）
CARD = "#FFFFFF"              # 卡片表面
CARD_INNER = "#F7F8FC"        # 卡片内嵌区域（拖拽区/表头）
BORDER = "#E4E8F1"            # 描边
TEXT = "#2B3245"              # 主文字
TEXT_MUTED = "#8A92A6"        # 次要文字
TEXT_FAINT = "#B4BBCC"        # 占位文字

# ==================== 字体（全局单一字体族） ====================
FONT_FAMILY = "Microsoft YaHei UI"


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    """创建统一字体（必须在root创建后调用）"""
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


def init_appearance():
    """初始化 customtkinter 全局外观（浅色模式）"""
    ctk.set_appearance_mode("light")


def create_root():
    """
    创建应用根窗口。
    优先创建同时支持圆角控件(CTk)与文件拖拽(TkinterDnD)的根窗口，
    拖拽库不可用时回退到纯 CTk 根窗口。
    """
    init_appearance()
    try:
        from tkinterdnd2 import TkinterDnD

        class _DnDCTk(ctk.CTk, TkinterDnD.DnDWrapper):
            """支持拖拽的 CTk 根窗口"""
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.TkdndVersion = TkinterDnD._require(self)

        return _DnDCTk()
    except Exception:
        return ctk.CTk()


def setup_treeview_style(root):
    """
    配置 ttk.Treeview 扁平化样式（Card.Treeview），
    使表格融入白色卡片：无边框、加大行高、浅灰表头、浅蓝选中。
    """
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(
        "Card.Treeview",
        background=CARD,
        foreground=TEXT,
        fieldbackground=CARD,
        borderwidth=0,
        relief="flat",
        rowheight=34,
        font=(FONT_FAMILY, 10),
    )
    style.configure(
        "Card.Treeview.Heading",
        background=CARD_INNER,
        foreground=TEXT_MUTED,
        borderwidth=0,
        relief="flat",
        font=(FONT_FAMILY, 10),
        padding=(8, 8),
    )
    style.map(
        "Card.Treeview",
        background=[("selected", PRIMARY_SOFT)],
        foreground=[("selected", TEXT)],
    )
    style.map(
        "Card.Treeview.Heading",
        background=[("active", CARD_INNER)],
    )


# ==================== 通用控件工厂 ====================

def card(parent, **kwargs) -> ctk.CTkFrame:
    """圆角白色卡片容器"""
    defaults = dict(fg_color=CARD, corner_radius=12,
                    border_width=1, border_color=BORDER)
    defaults.update(kwargs)
    return ctk.CTkFrame(parent, **defaults)


def card_title(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """卡片标题标签"""
    defaults = dict(text=text, font=font(13, "bold"),
                    text_color=TEXT, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, **defaults)


def primary_button(parent, text: str, command=None, **kwargs) -> ctk.CTkButton:
    """主操作按钮（实心蓝）"""
    defaults = dict(text=text, command=command, font=font(12),
                    fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                    text_color="#FFFFFF", corner_radius=8, height=34)
    defaults.update(kwargs)
    return ctk.CTkButton(parent, **defaults)


def ghost_button(parent, text: str, command=None, **kwargs) -> ctk.CTkButton:
    """次级按钮（描边浅底）"""
    defaults = dict(text=text, command=command, font=font(12),
                    fg_color=CARD, hover_color=PRIMARY_SOFT,
                    text_color=PRIMARY, border_width=1,
                    border_color=BORDER, corner_radius=8, height=34)
    defaults.update(kwargs)
    return ctk.CTkButton(parent, **defaults)


def attach_tree_scrollbar(container, tree) -> ctk.CTkScrollbar:
    """为 Treeview 附加圆角滚动条"""
    sb = ctk.CTkScrollbar(container, command=tree.yview,
                          button_color=TEXT_FAINT,
                          button_hover_color=TEXT_MUTED)
    tree.configure(yscrollcommand=sb.set)
    return sb


def register_drop_target_recursive(widget, callback):
    """
    为控件及其全部子控件注册拖拽目标。
    CTk 控件由多个内部子件组成，需要递归注册才能保证任意落点都能触发。
    DnD 库不可用时静默忽略（回退到点击上传）。
    """
    try:
        widget.drop_target_register('*')
        widget.dnd_bind('<<Drop>>', callback)
    except Exception:
        pass
    for child in widget.winfo_children():
        register_drop_target_recursive(child, callback)


def bind_click_recursive(widget, callback):
    """为控件及其全部子控件绑定左键点击（配合手型光标）"""
    try:
        widget.bind('<Button-1>', callback)
        widget.configure(cursor="hand2")
    except Exception:
        pass
    for child in widget.winfo_children():
        bind_click_recursive(child, callback)


# ==================== 设置页专用控件工厂 ====================

def settings_entry(parent, variable: ctk.StringVar, **kwargs) -> ctk.CTkEntry:
    """设置页文本输入框"""
    defaults = dict(width=360, height=34, corner_radius=8,
                    font=font(12), fg_color=CARD_INNER,
                    border_color=BORDER, text_color=TEXT,
                    textvariable=variable)
    defaults.update(kwargs)
    return ctk.CTkEntry(parent, **defaults)


def settings_switch(parent, variable: ctk.BooleanVar, **kwargs) -> ctk.CTkSwitch:
    """设置页开关（不需要 command 参数，**kwargs 会原样转发给 CTkSwitch）"""
    defaults = dict(font=font(12), text_color=TEXT,
                    variable=variable,
                    progress_color=SUCCESS,
                    button_color=BORDER, button_hover_color=PRIMARY)
    defaults.update(kwargs)
    return ctk.CTkSwitch(parent, **defaults)


def settings_combo(parent, variable: ctk.StringVar, values: list, **kwargs) -> ctk.CTkComboBox:
    """设置页下拉框"""
    defaults = dict(width=360, height=34, corner_radius=8,
                    font=font(12), dropdown_font=font(12),
                    fg_color=CARD_INNER, border_color=BORDER,
                    button_color=CARD_INNER, button_hover_color=PRIMARY_SOFT,
                    text_color=TEXT, dropdown_fg_color=CARD,
                    dropdown_hover_color=PRIMARY_SOFT,
                    dropdown_text_color=TEXT, variable=variable,
                    values=values, state="readonly")
    defaults.update(kwargs)
    return ctk.CTkComboBox(parent, **defaults)


def settings_label(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """设置页标签"""
    defaults = dict(text=text, font=font(12), text_color=TEXT_MUTED, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, **defaults)


def settings_help(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """设置页帮助文字"""
    defaults = dict(text=text, font=font(10), text_color=TEXT_FAINT, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, **defaults)
