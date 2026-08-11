"""
win32_helpers.ScrollGhostFix 单元测试（纯逻辑, 不依赖 Tk/Windows）
覆盖: 事件合并调度(同一批次只 update 一次) / 非活动页跳过 /
      active_check 跳过 / uninstall 精确移除绑定
"""
from unittest.mock import MagicMock

import win32_helpers


def make_fix():
    """绕过 __init__ 手工构造(不装窗口子类化/绑定), 只测调度与卸载逻辑"""
    frame = MagicMock()
    root = MagicMock()
    fix = win32_helpers.ScrollGhostFix.__new__(win32_helpers.ScrollGhostFix)
    fix.scroll_frame = frame
    fix.root = root
    fix.active_check = None
    fix.active = True
    fix._pending = False
    fix.installed = True
    return fix, frame, root


def test_events_coalesced_to_one_update():
    """同一批次 N 个事件只调度一次 update(防止嵌套 update 递归重入)"""
    fix, frame, root = make_fix()
    fix._schedule_redraw()
    fix._schedule_redraw()
    fix._schedule_redraw()
    root.after_idle.assert_called_once()
    # 执行延迟回调: 恢复 _pending, 且只触发一次 update
    cb = root.after_idle.call_args[0][0]
    cb()
    frame.update.assert_called_once()
    # 下一批次重新调度
    fix._schedule_redraw()
    assert root.after_idle.call_count == 2


def test_inactive_page_skips_schedule():
    """页面被 tkraise 盖住(active=False)时跳过调度"""
    fix, frame, root = make_fix()
    fix.active = False
    fix._schedule_redraw()
    root.after_idle.assert_not_called()


def test_active_check_skips_when_hidden():
    """active_check 返回 False(非活动页)时跳过调度"""
    fix, frame, root = make_fix()
    fix.active_check = lambda: False
    fix._schedule_redraw()
    root.after_idle.assert_not_called()


def test_active_check_passes_when_visible():
    fix, frame, root = make_fix()
    fix.active_check = lambda: True
    fix._schedule_redraw()
    root.after_idle.assert_called_once()


def test_uninstall_removes_wheel_binding_precisely():
    """
    uninstall 用 tk.call('bind','all',seq,funcid,'') 精确移除单个处理器,
    而不是 unbind_all(seq, funcid)(其签名只有 sequence 会抛 TypeError)
    或 unbind('all', seq, funcid)(第一句会删光全部 'all' 处理器)。
    """
    root = MagicMock()
    sbar = MagicMock()
    fix = win32_helpers.ScrollGhostFix.__new__(win32_helpers.ScrollGhostFix)
    fix.scroll_frame = MagicMock()
    fix.scroll_frame._scrollbar = sbar
    fix.root = root
    fix.suppressors = []
    fix._sbar_binds = [("<B1-Motion>", "b1-funcid")]
    fix._wheel_bind_id = "wheel-funcid"
    fix.installed = True
    fix.uninstall()
    # 全局滚轮: 精确移除
    root.tk.call.assert_any_call(
        "bind", "all", "<MouseWheel>", "wheel-funcid", "")
    # 滚动条: 同样精确移除
    sbar.tk.call.assert_any_call(
        "bind", sbar._w, "<B1-Motion>", "b1-funcid", "")


def test_uninstall_idempotent():
    fix, frame, root = make_fix()
    fix.suppressors = []
    fix._sbar_binds = []
    fix._wheel_bind_id = "wheel-funcid"
    fix.uninstall()
    fix.uninstall()  # 第二次为空操作, 不重复调用
    assert root.tk.call.call_count == 1
