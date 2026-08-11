"""
config_manager 单元测试
覆盖: LLM 属性回退链(LLM_* → DEEPSEEK/QWEN) / 空串归一化 / 视觉不回退文本端点 /
      set_many 批量保存 / 原子写盘 / get_all_editable 默认值单一真源
"""
import json

import pytest

from config_manager import ConfigManager, DEFAULT_CONFIG


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """隔离单例: 每个测试重置 ConfigManager 状态并指向临时 config.json"""
    monkeypatch.setattr(ConfigManager, "_instance", None)
    path = tmp_path / "config.json"
    c = ConfigManager(config_path=str(path))
    return c


class TestLLMFallbackChains:
    def test_llm_api_key_chain(self, cfg):
        """LLM_API_KEY → DEEPSEEK_API_KEY → QWEN_API_KEY（旧配置迁移）"""
        cfg.set_many({"LLM_API_KEY": "", "DEEPSEEK_API_KEY": "",
                      "QWEN_API_KEY": "qwen-key"})
        assert cfg.llm_api_key == "qwen-key"
        cfg.set_many({"DEEPSEEK_API_KEY": "ds-key"})
        assert cfg.llm_api_key == "ds-key"
        cfg.set_many({"LLM_API_KEY": "llm-key"})
        assert cfg.llm_api_key == "llm-key"

    def test_empty_or_whitespace_key_is_unconfigured(self, cfg):
        """设置页可保存空串/空白串: 按未配置处理, 走回退链"""
        cfg.set_many({"LLM_API_KEY": "   ", "DEEPSEEK_API_KEY": "ds-key"})
        assert cfg.llm_api_key == "ds-key"

    def test_llm_api_url_never_empty(self, cfg):
        """URL 空串回退默认 DeepSeek 端点(绝不出现 requests.post(''))"""
        cfg.set_many({"LLM_API_URL": ""})
        assert cfg.llm_api_url == DEFAULT_CONFIG["LLM_API_URL"]

    def test_llm_model_never_empty(self, cfg):
        cfg.set_many({"LLM_MODEL": "   "})
        assert cfg.llm_model == DEFAULT_CONFIG["LLM_MODEL"]

    def test_vl_never_falls_back_to_text_endpoint(self, cfg):
        """
        视觉配置只从 LLM_VL_* 或旧 QWEN_* 取, 绝不混用文本端点和文本 Key
        (多模态请求发往文本端点必失败)。只配文本模型时 model/key 为空
        → auto_retry_agent 三键闸门判定禁用截图识别。
        """
        cfg.set_many({"LLM_API_URL": "https://text-endpoint",
                      "LLM_API_KEY": "text-key", "LLM_MODEL": "deepseek-chat"})
        # 只配了文本模型: 视觉 key 为空(三键闸门判禁用截图识别);
        # model/url 解析到 QWEN 旧默认值(旧配置兼容), 绝不使用文本端点/文本 Key
        assert cfg.llm_vl_api_url != "https://text-endpoint"
        assert cfg.llm_vl_api_key == ""
        assert cfg.llm_vl_model == DEFAULT_CONFIG["QWEN_VL_MODEL"]
        # 只配旧 QWEN 键: 视觉从 QWEN 完整迁移(端点/Key/模型自洽)
        cfg.set_many({"QWEN_API_URL": "https://qwen-endpoint",
                      "QWEN_API_KEY": "qwen-key", "QWEN_VL_MODEL": "qwen-vl"})
        assert cfg.llm_vl_api_url == "https://qwen-endpoint"
        assert cfg.llm_vl_api_key == "qwen-key"
        assert cfg.llm_vl_model == "qwen-vl"
        # 文本端点/Key 不受影响
        assert cfg.llm_api_url == "https://text-endpoint"
        assert cfg.llm_api_key == "text-key"

    def test_vl_partial_config_does_not_mix(self, cfg):
        """LLM_VL_MODEL 单独配置: model 有值但 key 为空 → 闸门判定视觉不可用"""
        cfg.set_many({"LLM_VL_MODEL": "qwen-vl"})
        assert cfg.llm_vl_model == "qwen-vl"
        assert cfg.llm_vl_api_key == ""     # Key 仍空 → 视觉不可用

    def test_vl_url_without_configured_endpoint_uses_qwen_default(self, cfg):
        """只配 QWEN_API_KEY + QWEN_VL_MODEL 的旧配置: url 落到 QWEN 默认
        MaaS 端点, 与 key/model 自洽(旧版 qwen_api_url 属性同样有此默认)"""
        cfg.set_many({"QWEN_API_KEY": "qwen-key", "QWEN_VL_MODEL": "qwen-vl"})
        assert cfg.llm_vl_api_url == DEFAULT_CONFIG["QWEN_API_URL"]
        assert cfg.llm_vl_api_key == "qwen-key"
        assert cfg.llm_vl_model == "qwen-vl"


class TestSave:
    def test_set_many_updates_all_and_writes_once(self, cfg, tmp_path):
        """set_many 一次更新多键且只写一次盘"""
        cfg.set_many({"A": 1, "B": 2})
        assert cfg.get("A") == 1 and cfg.get("B") == 2
        with open(tmp_path / "config.json", encoding="utf-8") as f:
            assert json.load(f) == cfg._config

    def test_atomic_save_no_tmp_left(self, cfg, tmp_path):
        """原子写盘后不留 .tmp 残留文件"""
        cfg.set_many({"ROOT_DIR": "C:/x"})
        assert not (tmp_path / "config.json.tmp").exists()

    def test_set_keeps_working_single_key(self, cfg, tmp_path):
        """set() 单键保存仍然可用(委托 set_many)"""
        cfg.set("ROOT_DIR", "D:/new")
        assert cfg.get("ROOT_DIR") == "D:/new"
        with open(tmp_path / "config.json", encoding="utf-8") as f:
            assert json.load(f)["ROOT_DIR"] == "D:/new"


class TestGetAllEditable:
    def test_defaults_single_source_of_truth(self, cfg):
        """设置页默认值必须与 DEFAULT_CONFIG 完全一致(防两处漂移)"""
        editable = cfg.get_all_editable()
        assert editable
        for group, items in editable.items():
            assert items, f"分组 {group} 为空"
            for item in items:
                assert item["key"] in DEFAULT_CONFIG, item["key"]
                assert item["default"] == DEFAULT_CONFIG[item["key"]], \
                    f"{item['key']} 默认值漂移"

    def test_required_flags(self, cfg):
        """ROOT_DIR/WEBSITE_URL 必填, API Key 允许为空(回退链兜底)"""
        flags = {item["key"]: item.get("required", False)
                 for group, items in cfg.get_all_editable().items()
                 for item in items}
        assert flags["ROOT_DIR"] is True
        assert flags["WEBSITE_URL"] is True
        assert flags["LLM_API_KEY"] is not True
