"""
Windows Win32 API 辅助工具(纯 ctypes,不依赖 tkinter)

背景: 项目运行在 64 位 Python + Tk 8.6,凡读写窗口指针槽位
(GWL_WNDPROC / GWL_EXSTYLE)必须用 64 位 API(GetWindowLongPtrW /
SetWindowLongPtrW)。旧的 GetWindowLongW/SetWindowLongW 在 64 位下
静默失败(Set 返回 0),此前两处"拖影修复"从未生效的根因。

功能:
  1. get_top_level_hwnd      : Tk 根窗口真正的顶层 HWND(winfo_id 返回
                                TkChild 客户区,父级 TkTopLevel 才是带
                                标题栏/任务栏按钮的窗口,与 customtkinter
                                ctk_tk.py 中 GetParent(winfo_id()) 一致)
  2. install_erase_suppressor: 子类化窗口过程抑制 WM_ERASEBKGND(64 位
                                安全实现,失败静默返回 None 不破坏窗口)
  3. 扩展样式读写 + SetWindowPos(SWP_FRAMECHANGED) 强制样式生效
"""
import sys
import ctypes
from ctypes import wintypes

GWL_WNDPROC = -4
GWL_EXSTYLE = -20
WM_ERASEBKGND = 0x0014
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
GA_ROOT = 2  # GetAncestor: 沿父链回到根窗口
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

# LRESULT 在 64 位下是指针宽度整数。ctypes 默认 restype=c_int 会截断
# 64 位指针地址,必须显式声明为 c_ssize_t —— 这是本模块的核心修正。
LRESULT = ctypes.c_ssize_t if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long

# 窗口过程回调类型:(LRESULT)(HWND, UINT, WPARAM, LPARAM)
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_user32_module = None


def _configure_user32():
    """
    一次性配置 user32 函数指针的 argtypes/restype。
    使用 WinDLL(use_last_error=True): 调用包装器会保存调用后的 GetLastError,
    供 ctypes.get_last_error() 读取——set_ex_style 需要它区分
    "返回 0 是合法旧值" 与 "调用真的失败"(ctypes.windll.user32 是
    use_last_error=False, get_last_error 读不到真实错误码)。
    """
    user32 = _user32()
    user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongPtrW.restype = LRESULT
    # 第三参数是地址槽位(GWL_WNDPROC/GWL_EXSTYLE 存 64 位地址),声明 c_void_p;
    # 传入时须是整数地址(WINFUNCTYPE 实例需先 cast 取地址)
    user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
    user32.SetWindowLongPtrW.restype = LRESULT
    user32.GetParent.argtypes = (wintypes.HWND,)
    user32.GetParent.restype = wintypes.HWND
    user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetAncestor.restype = wintypes.HWND
    # 透传参数用 c_void_p:可直接传 Python int 形式的原窗口过程地址
    user32.CallWindowProcW.argtypes = (
        ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM)
    user32.CallWindowProcW.restype = LRESULT
    user32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, ctypes.c_uint)
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL


def _user32():
    if sys.platform != "win32":
        raise OSError("仅支持 Windows")
    global _user32_module
    if _user32_module is None:
        _user32_module = ctypes.WinDLL("user32", use_last_error=True)
        _configure_user32()
    return _user32_module


def get_top_level_hwnd(root) -> int:
    """
    返回 Tk 根窗口真正的顶层 HWND。

    应用窗口(tkdnd 集成)是两层结构: winfo_id 返回 TkChild 客户区,其父级
    TkTopLevel 才是带标题栏/任务栏按钮的窗口(与 customtkinter ctk_tk.py
    中 GetParent(winfo_id()) 一致)。纯 tk.Tk() 是单层结构,winfo_id 本身
    即顶层。GetAncestor(GA_ROOT) 沿父链取根,两种结构都返回正确顶层;
    失败时回退 winfo_id。
    """
    try:
        wid = root.winfo_id()
        top = _user32().GetAncestor(wid, GA_ROOT) or _user32().GetParent(wid)
        return top or wid
    except Exception:
        try:
            return root.winfo_id()
        except Exception:
            return 0


class EraseSuppressor:
    """窗口过程子类化句柄:持有回调引用防 GC,提供 uninstall 还原"""

    def __init__(self, hwnd, original, callback, callback_addr):
        self.hwnd = hwnd
        self.original = original      # 原始窗口过程地址(还原用)
        self.callback = callback      # 新窗口过程(WINFUNCTYPE 实例,必须持有防 GC)
        self.callback_addr = callback_addr
        self.installed = True

    def uninstall(self):
        """
        还原原始窗口过程(窗口销毁前调用)。
        先校验槽位仍持有本回调再写回: HWND 销毁后句柄值可能被 OS 复用于
        新窗口, 无校验地写回会把旧地址塞进陌生窗口。
        """
        if not self.installed:
            return
        try:
            user32 = _user32()
            current = user32.GetWindowLongPtrW(self.hwnd, GWL_WNDPROC)
            if current == self.callback_addr:
                user32.SetWindowLongPtrW(self.hwnd, GWL_WNDPROC, self.original)
            # 槽位已被替换/窗口已销毁: 不写, 避免污染复用句柄的窗口
        except Exception:
            pass
        self.installed = False


def install_erase_suppressor(hwnd):
    """
    为窗口安装 WM_ERASEBKGND 抑制子类化,返回 EraseSuppressor 或 None。
    hwnd 可以是整数 HWND,或带 winfo_id() 的 tkinter 控件。
    失败(读取原过程为 0 / 设置返回 0)静默返回 None,不崩溃不破坏窗口。
    """
    try:
        if not isinstance(hwnd, int):
            hwnd = hwnd.winfo_id()
        if not hwnd:
            return None
        user32 = _user32()
        original = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
        if original == 0:              # 正常窗口的窗口过程不可能为 NULL
            return None

        def _wndproc(h, msg, wparam, lparam):
            if msg == WM_ERASEBKGND:
                return 1               # 返回 1 表示"已擦除",阻止默认擦除
            try:
                return user32.CallWindowProcW(
                    original, h, msg, wparam, lparam)
            except Exception as e:
                # 回调内兜底,绝不向上抛出(WINFUNCTYPE 回调抛异常会终止进程);
                # 但必须打印, 避免伪造 LRESULT 静默掩盖真实 bug
                print(f"[win32_helpers] CallWindowProcW 转发异常 (msg=0x{msg:x}): {e!r}",
                      file=sys.stderr)
                return 0

        callback = WNDPROC(_wndproc)
        # WINFUNCTYPE 实例不能直接作为整数参数传入,需显式取地址
        callback_addr = ctypes.cast(callback, ctypes.c_void_p).value
        if user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, callback_addr) == 0:
            return None                # 旧代码用 SetWindowLongW 就死在这里(返回 0 被忽略)
        return EraseSuppressor(hwnd, original, callback, callback_addr)
    except Exception:
        return None


def get_ex_style(hwnd) -> int:
    """读取窗口扩展样式(失败返回 0)"""
    try:
        return _user32().GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    except Exception:
        return 0


def set_ex_style(hwnd, style) -> bool:
    """
    设置扩展样式并 SetWindowPos(SWP_FRAMECHANGED) 强制窗口重读样式。

    返回值是"旧扩展样式", 0 是完全合法的前值(窗口本来就没有任何扩展样式),
    不能把 0 当失败——失败只能靠 GetLastError 判断(需要 use_last_error=True 的
    WinDLL, 见 _configure_user32 说明)。此前 0 值误判会导致: 样式其实
    设置成功却被当作失败, 任务栏隐藏提前中止, 窗口 iconify 后仍留在任务栏。
    """
    try:
        user32 = _user32()
        ctypes.set_last_error(0)
        prev = user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
        if prev == 0 and ctypes.get_last_error() != 0:
            return False
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


def set_foreground_window(hwnd) -> None:
    """把窗口带到前台(已配置 argtypes 的 user32 封装, 替代裸 ctypes 调用)"""
    try:
        _user32().SetForegroundWindow(hwnd)
    except Exception:
        pass


class ScrollGhostFix:
    """
    滚动残影修复句柄(统计面板/设置页共用):

    1. 子类化画布与内容帧抑制 WM_ERASEBKGND(64 位安全);
    2. 滚动条拖动(B1-Motion/Button-1/ButtonRelease-1)与全局滚轮事件后
       经 after_idle 合并调度一次 update(), 强制内容跟上滑块。

    设计要点(对应评审发现的旧实现缺陷):
    - update() 绝不在事件处理器内直接调用(会嵌套排空事件队列导致递归重入),
      统一经 after_idle 调度 + _pending 标志合并, 同一批次事件只重绘一次;
    - 滚轮用 bind_all('all') 且记录 funcid, uninstall 时用
      tk.call('bind', 'all', seq, funcid, '') 精确移除——
      root.unbind_all(seq, funcid) 只接受一个参数(Python 3.14 签名),
      root.unbind('all', seq, funcid) 会误删其他页面的全部 'all' 处理器;
    - active_check 谓词: 页面被 tkraise 盖住时跳过重绘, 避免对隐藏页无谓 update。
    """

    def __init__(self, scroll_frame, root, active_check=None):
        """
        Args:
            scroll_frame: CTkScrollableFrame 实例(需暴露 _parent_canvas 与 _scrollbar)
            root: tkinter 根窗口(bind_all/unbind 目标)
            active_check: 可选 callable → bool, 返回 False 时跳过调度(页面隐藏)
        """
        self.scroll_frame = scroll_frame
        self.root = root
        self.active_check = active_check
        self.active = True
        self.suppressors = []
        self._sbar_binds = []      # [(sequence, funcid)]
        self._wheel_bind_id = None
        self._pending = False
        self.installed = False
        self._install()

    def _install(self):
        target_canvas = getattr(self.scroll_frame, "_parent_canvas", None)
        if target_canvas is None:
            return
        try:
            self.suppressors = [
                install_erase_suppressor(target_canvas),
                install_erase_suppressor(self.scroll_frame),
            ]
        except Exception:
            self.suppressors = []
        sbar = getattr(self.scroll_frame, "_scrollbar", None)
        if sbar is not None:
            try:
                for seq in ("<B1-Motion>", "<Button-1>", "<ButtonRelease-1>"):
                    self._sbar_binds.append(
                        (seq, sbar.bind(seq, self._schedule_redraw, add=True)))
            except Exception:
                self._sbar_binds = []
        try:
            self._wheel_bind_id = self.root.bind_all(
                "<MouseWheel>", self._schedule_redraw, add=True)
        except Exception:
            self._wheel_bind_id = None
        self.installed = True

    def _schedule_redraw(self, _event=None):
        """合并调度重绘: 同一批次事件只产生一次 update()"""
        if not self.installed or not self.active:
            return
        if self.active_check is not None:
            try:
                if not self.active_check():
                    return
            except Exception:
                pass
        if self._pending:
            return
        self._pending = True

        def _do():
            self._pending = False
            try:
                self.scroll_frame.update()
            except Exception:
                pass

        try:
            self.root.after_idle(_do)
        except Exception:
            self._pending = False

    def uninstall(self):
        """还原子类化窗口过程、滚动条绑定与应用级滚轮绑定(页面销毁/程序退出时调用)"""
        if not self.installed:
            return
        self.installed = False
        for suppressor in self.suppressors:
            if suppressor is not None:
                try:
                    suppressor.uninstall()
                except Exception:
                    pass
        sbar = getattr(self.scroll_frame, "_scrollbar", None)
        if sbar is not None:
            for seq, funcid in self._sbar_binds:
                self._unbind_specific(sbar, seq, funcid)
        if self._wheel_bind_id:
            self._unbind_specific(self.root, "<MouseWheel>", self._wheel_bind_id,
                                  target="all")

    @staticmethod
    def _unbind_specific(widget, sequence, funcid, target=None):
        """
        精确移除单个绑定: tk.call('bind', target, seq, funcid, '')。
        不能用 widget.unbind_all(seq, funcid)(3.14 只接受 sequence)或
        widget.unbind('all', seq, funcid)(第一句 bind 'all' seq '' 会删光
        所有 'all' 处理器, 另一个页面的滚轮绑定会被误删)。
        """
        if not funcid:
            return
        try:
            widget.tk.call("bind", target or widget._w, sequence, funcid, "")
        except Exception:
            pass
