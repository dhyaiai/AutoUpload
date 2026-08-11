"""
win32_helpers 单元测试
覆盖:
  - ctypes 声明冒烟: 64 位下 LRESULT 必须为 c_ssize_t、GetWindowLongPtrW.restype 已配置
  - 无 Tk 端到端子类化: 真实 HWND 上 安装→拦截 WM_ERASEBKGND→透传→还原 全链路
  - get_top_level_hwnd: GetParent 失败时回退 winfo_id
  - _toast_clicked 回归: 调用 _restore_window_impl(旧代码调用不存在的方法抛 AttributeError)
  - 真实 Tk 冒烟: winfo_id 是 TkChild, 父级 TkTopLevel 才是顶层窗口(有显示环境才跑)
"""
import sys
import ctypes
from ctypes import wintypes

import pytest

import win32_helpers
from win32_helpers import (
    GWL_WNDPROC, GWL_EXSTYLE, WM_ERASEBKGND, LRESULT)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Win32 API 仅适用于 Windows")


def _user32():
    """已配置 argtypes/restype 的 user32(测试用声明)"""
    user32 = win32_helpers._user32()
    # 测试专用声明(win32_helpers 只配置了自己用到的函数)
    user32.SendMessageW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.SendMessageW.restype = LRESULT
    user32.GetClassNameW.argtypes = (
        wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p)
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = (wintypes.HWND,)
    user32.DestroyWindow.restype = wintypes.BOOL
    return user32


class TestCtypesDeclarations:
    def test_lresult_is_pointer_width(self):
        """LRESULT 必须是指针宽度: 64 位下 c_ssize_t, 防止截断窗口过程地址"""
        win32_helpers._configure_user32()
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            assert LRESULT is ctypes.c_ssize_t
        else:
            assert LRESULT is ctypes.c_long

    def test_get_window_long_ptr_restype_configured(self):
        """GetWindowLongPtrW.restype 已配置为 LRESULT(默认 c_int 会截断 64 位地址)"""
        win32_helpers._configure_user32()
        user32 = win32_helpers._user32()
        assert user32.GetWindowLongPtrW.restype is LRESULT
        assert user32.SetWindowLongPtrW.restype is LRESULT

    def test_user32_uses_last_error(self):
        """
        行为验证: _user32() 必须是 use_last_error=True 的 WinDLL(set_ex_style
        依赖 ctypes.get_last_error() 区分"返回0=合法旧值"与"调用真的失败")。
        无效句柄调用 SetWindowLongPtrW 失败后, get_last_error() 应返回非 0
        (ctypes.windll.user32 是 use_last_error=False, 读不到真实错误码)。
        """
        user32 = win32_helpers._user32()
        ctypes.set_last_error(0)
        user32.SetWindowLongPtrW(0xDEAD, win32_helpers.GWL_EXSTYLE, 0)
        assert ctypes.get_last_error() != 0


class TestEraseSuppressor:
    def test_install_intercept_forward_uninstall(self):
        """
        真实 HWND 上的完整子类化链路:
        安装 → 窗口过程已变更 → WM_ERASEBKGND 返回 1 → WM_NULL 透传不抛异常
        → uninstall 还原原窗口过程
        """
        user32 = _user32()
        WS_POPUP = 0x80000000
        hwnd = user32.CreateWindowExW(0, "STATIC", "", WS_POPUP,
                                      0, 0, 100, 100, None, None, None, None)
        assert hwnd, "创建隐藏测试窗口失败"
        try:
            original = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
            assert original != 0

            suppressor = win32_helpers.install_erase_suppressor(hwnd)
            assert suppressor is not None
            try:
                assert user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC) != original
                # WM_ERASEBKGND 被拦截, 返回 1 表示"已擦除"
                assert user32.SendMessageW(hwnd, WM_ERASEBKGND, 0, 0) == 1
                # 其余消息透传给原始窗口过程, 不抛异常
                user32.SendMessageW(hwnd, 0x0000, 0, 0)  # WM_NULL
            finally:
                suppressor.uninstall()
            assert user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC) == original
        finally:
            user32.DestroyWindow(hwnd)

    def test_install_returns_none_on_null_hwnd(self):
        """hwnd 为 0 时静默返回 None, 不抛异常"""
        assert win32_helpers.install_erase_suppressor(0) is None

    def test_uninstall_is_idempotent(self):
        """重复 uninstall 无副作用(第二次为空操作)"""
        user32 = _user32()
        WS_POPUP = 0x80000000
        hwnd = user32.CreateWindowExW(0, "STATIC", "", WS_POPUP,
                                      0, 0, 100, 100, None, None, None, None)
        assert hwnd
        try:
            original = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
            suppressor = win32_helpers.install_erase_suppressor(hwnd)
            assert suppressor is not None
            suppressor.uninstall()
            suppressor.uninstall()  # 第二次调用应无副作用
            assert user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC) == original
        finally:
            user32.DestroyWindow(hwnd)


class TestSetExStyle:
    def test_zero_previous_style_is_success(self):
        """
        回归: 前值扩展样式为 0 是合法值, 不是失败标志。
        旧代码把 SetWindowLongPtrW 返回 0 一律当失败, 任务栏隐藏
        被提前中止, 窗口 iconify 后仍留在任务栏。
        """
        user32 = _user32()
        WS_POPUP = 0x80000000
        hwnd = user32.CreateWindowExW(0, "STATIC", "", WS_POPUP,
                                      0, 0, 100, 100, None, None, None, None)
        assert hwnd
        try:
            assert user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) == 0
            assert win32_helpers.set_ex_style(
                hwnd, win32_helpers.WS_EX_TOOLWINDOW) is True
            assert user32.GetWindowLongPtrW(
                hwnd, GWL_EXSTYLE) == win32_helpers.WS_EX_TOOLWINDOW
        finally:
            user32.DestroyWindow(hwnd)

    def test_invalid_hwnd_returns_false(self):
        """无效句柄(真实失败, GetLastError 非 0)返回 False"""
        assert win32_helpers.set_ex_style(0xDEAD, 0) is False


class TestEraseSuppressorSlot:
    def test_uninstall_respects_slot_ownership(self):
        """
        回归: 槽位已被他人替换时 uninstall 不写回旧地址。
        窗口销毁后 HWND 句柄值可能被 OS 复用, 无校验写回会把
        已失效的窗口过程地址塞进陌生窗口。
        """
        user32 = _user32()
        WS_POPUP = 0x80000000
        hwnd = user32.CreateWindowExW(0, "STATIC", "", WS_POPUP,
                                      0, 0, 100, 100, None, None, None, None)
        assert hwnd
        try:
            original = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
            suppressor = win32_helpers.install_erase_suppressor(hwnd)
            assert suppressor is not None
            # 模拟其他代码替换了窗口过程
            other = win32_helpers.WNDPROC(lambda h, m, w, l: 0)
            other_addr = ctypes.cast(other, ctypes.c_void_p).value
            user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, other_addr)
            suppressor.uninstall()
            # 槽位仍是别人的回调, 既未写回旧地址也未清空
            assert user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC) == other_addr
        finally:
            user32.DestroyWindow(hwnd)


class TestGetTopLevelHwnd:
    def test_fallback_to_winfo_id_when_ancestors_fail(self, monkeypatch):
        """GetAncestor/GetParent 均返回 0(失败)时回退到 winfo_id"""
        class FakeUser32:
            def GetAncestor(self, hwnd, flag):
                return 0

            def GetParent(self, hwnd):
                return 0

        class FakeRoot:
            def winfo_id(self):
                return 12345

        monkeypatch.setattr(win32_helpers, "_user32", lambda: FakeUser32())
        assert win32_helpers.get_top_level_hwnd(FakeRoot()) == 12345

    def test_uses_ancestor_result(self, monkeypatch):
        """GetAncestor(GA_ROOT) 成功时返回根窗口 HWND"""
        class FakeUser32:
            def GetAncestor(self, hwnd, flag):
                assert flag == win32_helpers.GA_ROOT
                return 99999

            def GetParent(self, hwnd):
                return 88888

        class FakeRoot:
            def winfo_id(self):
                return 12345

        monkeypatch.setattr(win32_helpers, "_user32", lambda: FakeUser32())
        assert win32_helpers.get_top_level_hwnd(FakeRoot()) == 99999

    def test_falls_back_to_getparent_when_ancestor_missing(self, monkeypatch):
        """GetAncestor 返回 0 时回退 GetParent(兼容两层 TkChild/TkTopLevel 结构)"""
        class FakeUser32:
            def GetAncestor(self, hwnd, flag):
                return 0

            def GetParent(self, hwnd):
                return 77777

        class FakeRoot:
            def winfo_id(self):
                return 12345

        monkeypatch.setattr(win32_helpers, "_user32", lambda: FakeUser32())
        assert win32_helpers.get_top_level_hwnd(FakeRoot()) == 77777


class TestToastClickedRegression:
    def test_toast_clicked_calls_restore_window_impl(self, monkeypatch):
        """
        回归: _toast_clicked 必须调用 _restore_window_impl
        (旧代码调用不存在的 _restore_window 会抛 AttributeError)
        """
        from unittest.mock import MagicMock
        from gui_manager import MainApplication

        app = MainApplication.__new__(MainApplication)
        app._batch_toast = None
        mock = MagicMock()
        monkeypatch.setattr(app, "_restore_window_impl", mock)
        app._toast_clicked()
        mock.assert_called_once()


class TestRealTkTopLevel:
    def test_plain_tk_root_returns_valid_handle(self):
        """
        纯 tk.Tk() 是单层结构: winfo_id 本身即顶层, 返回非 0 有效句柄
        (无显示环境自动跳过)
        """
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError:
            pytest.skip("无显示环境, 跳过真实 Tk 冒烟测试")
        try:
            root.withdraw()
            top = win32_helpers.get_top_level_hwnd(root)
            assert top != 0
            assert top == root.winfo_id()  # 无父级时回退自身
        finally:
            root.destroy()

    def test_app_root_top_is_tk_toplevel(self):
        """
        应用真实窗口(tkdnd 集成)是两层结构: winfo_id 返回 TkChild 客户区,
        父级 TkTopLevel 才是带标题栏/任务栏按钮的顶层窗口
        (依赖 tkinterdnd2 真实窗口, 环境不支持自动跳过)
        """
        try:
            import ui_theme
            root = ui_theme.create_root()
        except Exception as e:
            pytest.skip(f"无法创建应用真实窗口: {e}")
        try:
            root.update_idletasks()
            top = win32_helpers.get_top_level_hwnd(root)
            assert top != root.winfo_id()
            buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(top, buf, 64)
            assert buf.value == "TkTopLevel"
        finally:
            root.destroy()
