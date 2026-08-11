"""
gui_manager._save_settings 单元测试
覆盖: 全量校验后一次性 set_many(任一字段失败完全不落盘) /
      必填字段校验 / 非必填空串放行 / 成功路径
"""
from unittest.mock import MagicMock

from gui_manager import MainApplication


class FakeVar:
    """模拟 ctk.StringVar/BooleanVar: 只提供 get()"""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


def make_app(widgets):
    app = MainApplication.__new__(MainApplication)
    app._settings_widgets = widgets
    app._settings_status_label = MagicMock()
    app.config = MagicMock()
    app.root = MagicMock()   # 成功路径会 root.after(3000, ...) 清除状态文字
    return app


def status_text(app) -> str:
    """取状态标签最近一次 configure 的 text 值"""
    return app._settings_status_label.configure.call_args.kwargs.get("text", "")


def test_success_calls_set_many_once():
    """全部合法 → 只调用一次 set_many(旧代码每个字段一次 set/落盘)"""
    app = make_app({
        "ROOT_DIR": {"var": FakeVar("C:\\upload"), "type": "str", "required": True},
        "API_SERVER_PORT": {"var": FakeVar("8000"), "type": "int", "required": False},
        "AUTO_RETRY_ENABLE": {"var": FakeVar("1"), "type": "bool", "required": False},
    })
    app._save_settings()
    app.config.set_many.assert_called_once()
    updates = app.config.set_many.call_args[0][0]
    assert updates == {"ROOT_DIR": "C:\\upload", "API_SERVER_PORT": 8000,
                       "AUTO_RETRY_ENABLE": True}
    app._settings_status_label.configure.assert_called_once()


def test_invalid_int_blocks_all_writes():
    """
    任一字段非法 → 完全不落盘(旧代码逐键 set, 前面的字段已写入磁盘,
    用户看到"保存失败"但一半配置已生效)
    """
    app = make_app({
        "ROOT_DIR": {"var": FakeVar("C:\\upload"), "type": "str", "required": True},
        "API_SERVER_PORT": {"var": FakeVar("abc"), "type": "int", "required": False},
    })
    app._save_settings()
    app.config.set_many.assert_not_called()
    assert "保存失败" in status_text(app)


def test_required_empty_blocks_all_writes():
    """必填字段清空(如 ROOT_DIR) → 完全不落盘"""
    app = make_app({
        "ROOT_DIR": {"var": FakeVar("   "), "type": "str", "required": True},
        "API_SERVER_PORT": {"var": FakeVar("8000"), "type": "int", "required": False},
    })
    app._save_settings()
    app.config.set_many.assert_not_called()
    status = status_text(app)
    assert "ROOT_DIR" in status and "不能为空" in status


def test_optional_empty_string_allowed():
    """非必填字符串可为空(如 LLM_API_KEY 留空走回退链, 不阻塞保存)"""
    app = make_app({
        "LLM_API_KEY": {"var": FakeVar(""), "type": "str", "required": False},
        "API_SERVER_PORT": {"var": FakeVar("8000"), "type": "int", "required": False},
    })
    app._save_settings()
    app.config.set_many.assert_called_once()
    assert app.config.set_many.call_args[0][0]["LLM_API_KEY"] == ""


def test_float_conversion():
    """float 类型字段正常转换"""
    app = make_app({
        "SLEEP_INTERVAL": {"var": FakeVar("0.5"), "type": "float", "required": False},
    })
    app._save_settings()
    app.config.set_many.assert_called_once()
    assert app.config.set_many.call_args[0][0]["SLEEP_INTERVAL"] == 0.5
