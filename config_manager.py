"""
配置管理模块
功能:读取和管理config.json配置文件,提供全局配置访问接口
特点:使用单例模式,支持默认值,避免硬编码
"""
import json
import os
import re
import unicodedata


# 默认配置（敏感信息留空，需用户自行填写）。
# 唯一真源: _load_config 合并、get_all_editable(设置页) 的 "default" 都从这读取,
# 避免两处默认值漂移(改一处忘了另一处, 设置页会预填旧默认值覆盖新默认)。
DEFAULT_CONFIG = {
    "ROOT_DIR": r"C:\Users\Administrator\Desktop\upload",  # 监控的根目录
    "WEBSITE_URL": "https://zuoye.7net.cc",                # 目标网站URL
    "USERNAME": "",                                        # 登录用户名（需填写）
    "PASSWORD": "",                                        # 登录密码（需填写）
    "ROLE": "超级管理员",                                   # 用户角色
    "DEEPSEEK_API_KEY": "",                                # DeepSeek API密钥（兼容旧配置,新代码使用 LLM_API_KEY）
    "LLM_API_URL": "https://api.deepseek.com/v1/chat/completions",  # 默认大模型 API 端点
    "LLM_MODEL": "deepseek-chat",                           # 默认大模型名称
    "LLM_API_KEY": "",                                      # 默认大模型 API 密钥（需填写）
    "LLM_VL_API_URL": "",                                   # 多模态 API 地址（留空=禁用截图识别）
    "LLM_VL_MODEL": "",                                     # 多模态模型名称（留空=禁用截图识别）
    "LLM_VL_API_KEY": "",                                   # 多模态 API Key（留空=禁用截图识别）
    "CHROME_DRIVER_PATH": "",                              # Chrome驱动路径（留空让Selenium自动管理）
    "CHROME_PROFILE_DIR": "",                              # Chrome用户数据目录（留空=默认临时目录，填写则复用登录态）
    "FILE_STABLE_DELAY": 2,                                # 文件稳定等待时间(秒)
    "BROWSER_IDLE_TIMEOUT": 1800,                          # 浏览器空闲兜底超时(秒)，即使有待重试记录也强制关闭
    "UPLOAD_IDLE_TIMEOUT": 1800,                           # 上传完成后无操作关闭超时(秒)，队列空+无处理时触发
    "MAX_RETRY_COUNT": 3,                                  # 最大重试次数
    "SLEEP_INTERVAL": 0.5,                                # 操作间隔时间(秒)
    "MINIMIZE_TO_TRAY": True,                              # 关闭窗口时最小化到托盘(False=直接退出)

    # AutoRetryAgent 配置
    "AUTO_RETRY_ENABLE": True,                              # 是否启用自动重试Agent
    "AUTO_RETRY_SCAN_INTERVAL": 60,                         # 扫描间隔(秒)
    "AUTO_RETRY_BACKOFF_SECONDS": [30, 120, 600],           # 指数退避间隔(秒)
    "AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD": 10,             # 熔断阈值(5分钟内同类错误次数)
    "AUTO_RETRY_CIRCUIT_BREAKER_DURATION": 1800,            # 熔断持续时间(秒，默认30分钟)

    # AI Agent 配置
    "AI_RETRY_AGENT_ENABLE": True,                           # 启用AI驱动的重试决策
    "AI_ANALYSIS_AGENT_ENABLE": True,                        # 启用AI驱动的分析报告生成
    "AI_AGENT_MAX_STEPS": 10,                                # ReAct循环最大步数
    "QWEN_API_KEY": "",                                      # 通义千问 API Key（兼容旧配置）
    "QWEN_MODEL": "qwen3.7-plus",                           # 通义千问模型名（兼容旧配置）
    "QWEN_API_URL": "https://llm-nwnb3n9ni4k5ebc2.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",  # 通义千问 API 端点（兼容旧配置）
    "QWEN_VL_MODEL": "qwen3.7-plus",                        # 截图理解多模态模型（兼容旧配置）

    # 流水线看门狗配置（卡死检测）
    "WATCHDOG_ENABLE": True,                                 # 启用看门狗卡死检测
    "WATCHDOG_CHECK_INTERVAL": 10,                           # 检查间隔(秒)
    "WATCHDOG_STAGE_TIMEOUTS": {                             # 各阶段卡死判定阈值(秒)
        "read_file": 60,
        "ai_classify": 180,
        "browser_init": 240,
        "school_check": 240,
        "submit_upload": 300                                 # 需大于 UPLOAD_TIMEOUT(120s)
    },

    # API 服务配置（微信小程序对接）
    "API_SERVER_HOST": "0.0.0.0",                           # API服务监听地址
    "API_SERVER_PORT": 8000,                                  # API服务监听端口
    "UPLOAD_TEMP_DIR": "./upload_temp",                       # 小程序上传文件临时目录
    "BROWSER_RESTART_INTERVAL": 50,                           # 浏览器定时重启间隔(每N次上传,0=不自动重启)
}


class ConfigManager:
    """
    配置管理器(单例模式)
    负责加载、存储和提供程序配置信息
    """

    _instance = None
    _config = {}

    def __new__(cls, config_path: str = None):
        """
        重写__new__方法实现单例模式

        Args:
            config_path: 配置文件路径,默认为当前目录下的config.json
                        如果是打包后的exe,则优先使用exe所在目录的config.json
        """
        if cls._instance is None:
            cls._instance = super().__new__(cls)

            # 处理打包后的exe文件路径问题
            import sys
            if config_path is None:
                # 检查是否在PyInstaller打包环境中
                if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
                    # 打包环境:优先使用exe所在目录的config.json
                    exe_dir = os.path.dirname(sys.executable)
                    cls._instance.config_path = os.path.join(exe_dir, "config.json")
                else:
                    # 开发环境:使用当前工作目录的config.json
                    cls._instance.config_path = "config.json"
            else:
                cls._instance.config_path = config_path

            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        """
        从JSON文件加载配置
        如果文件不存在,则创建默认配置
        """
        default_config = DEFAULT_CONFIG  # 默认值统一来自模块级 DEFAULT_CONFIG（唯一真源）

        # 尝试加载配置文件
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                    # 清理字符串值中的不可见Unicode控制字符（如从网页/聊天软件复制路径时带入的LRE/RLE等）
                    loaded_config = self._sanitize_strings(loaded_config)
                    # 合并默认配置和加载的配置(加载的配置覆盖默认值)
                    self._config = {**default_config, **loaded_config}
                    print(f"已加载配置文件: {self.config_path}")
            except Exception as e:
                print(f"警告: 配置文件读取失败 ({e}), 使用默认配置")
                # 必须拷贝: 直接赋引用会让后续 set/set_many 原地修改模块级
                # DEFAULT_CONFIG, 污染单例的其他实例/测试
                self._config = dict(default_config)
                self._save_config()  # 保存默认配置到文件
        else:
            # 配置文件不存在,使用默认配置并创建文件
            print(f"配置文件不存在,创建默认配置: {self.config_path}")
            self._config = dict(default_config)  # 同上: 拷贝而非引用
            self._save_config()

    def _save_config(self):
        """
        将当前配置原子写入 JSON 文件: 先写 .tmp 再 os.replace。
        直接 open('w') 在写盘中途崩溃/断电会留下截断的 config.json,
        下次启动读不到 API Key/密码会静默回退默认值。
        """
        tmp_path = f"{self.config_path}.tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, self.config_path)
        except Exception as e:
            print(f"错误: 无法保存配置文件 ({e})")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

    def get(self, key: str, default=None):
        """
        获取配置项的值

        Args:
            key: 配置项的键名
            default: 如果键不存在时的默认返回值

        Returns:
            配置项的值,如果键不存在则返回default
        """
        return self._config.get(key, default)

    def set(self, key: str, value):
        """
        设置单个配置项并保存（等价于 set_many({key: value})，保留给单键调用方）
        """
        self.set_many({key: value})

    def set_many(self, updates: dict):
        """
        批量设置配置项并只落盘一次（原子写）。
        设置页 20+ 个字段一次保存 = 1 次磁盘写; 避免逐键 set()
        在循环中途失败时留下"部分保存"的混合状态。
        """
        self._config.update(self._sanitize_strings(updates))
        self._save_config()

    @property
    def root_dir(self) -> str:
        """获取监控根目录"""
        return self.get("ROOT_DIR")

    @property
    def website_url(self) -> str:
        """获取目标网站URL"""
        return self.get("WEBSITE_URL")

    @property
    def username(self) -> str:
        """获取登录用户名"""
        return self.get("USERNAME")

    @property
    def password(self) -> str:
        """获取登录密码"""
        return self.get("PASSWORD")

    @property
    def role(self) -> str:
        """获取用户角色"""
        return self.get("ROLE")

    @property
    def deepseek_api_key(self) -> str:
        """获取DeepSeek API密钥"""
        return self.get("DEEPSEEK_API_KEY")

    @property
    def chrome_driver_path(self) -> str:
        """获取Chrome驱动路径"""
        return self.get("CHROME_DRIVER_PATH")

    @property
    def chrome_profile_dir(self) -> str:
        """获取Chrome用户数据目录路径（持久化登录Cookie，重启后免登录）"""
        return self.get("CHROME_PROFILE_DIR")

    @property
    def file_stable_delay(self) -> int:
        """获取文件稳定等待时间(秒)"""
        return self.get("FILE_STABLE_DELAY", 2)

    @property
    def browser_idle_timeout(self) -> int:
        """获取浏览器空闲兜底超时(秒)，即使有待重试记录也强制关闭"""
        return self.get("BROWSER_IDLE_TIMEOUT", 1800)

    @property
    def upload_idle_timeout(self) -> int:
        """获取上传完成后无操作关闭超时(秒)，队列空+无处理时触发"""
        return self.get("UPLOAD_IDLE_TIMEOUT", 1800)

    @property
    def max_retry_count(self) -> int:
        """获取最大重试次数"""
        return self.get("MAX_RETRY_COUNT", 3)

    @property
    def sleep_interval(self) -> float:
        """获取操作间隔时间(秒)"""
        return self.get("SLEEP_INTERVAL", 0.5)

    @property
    def auto_retry_enable(self) -> bool:
        """是否启用自动重试Agent"""
        return self.get("AUTO_RETRY_ENABLE", True)

    @property
    def auto_retry_scan_interval(self) -> int:
        """自动重试Agent扫描间隔(秒)"""
        return self.get("AUTO_RETRY_SCAN_INTERVAL", 60)

    @property
    def auto_retry_backoff_seconds(self) -> list:
        """自动重试指数退避间隔列表(秒)"""
        return self.get("AUTO_RETRY_BACKOFF_SECONDS", [30, 120, 600])

    @property
    def auto_retry_circuit_breaker_threshold(self) -> int:
        """熔断阈值(5分钟内同类错误次数)"""
        return self.get("AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD", 10)

    @property
    def auto_retry_circuit_breaker_duration(self) -> int:
        """熔断持续时间(秒)"""
        return self.get("AUTO_RETRY_CIRCUIT_BREAKER_DURATION", 1800)

    @property
    def ai_retry_agent_enable(self) -> bool:
        """是否启用AI驱动的重试决策"""
        return self.get("AI_RETRY_AGENT_ENABLE", True)

    @property
    def ai_analysis_agent_enable(self) -> bool:
        """是否启用AI驱动的分析报告生成"""
        return self.get("AI_ANALYSIS_AGENT_ENABLE", True)

    # ==================== 统一 LLM 配置 ====================
    # 单真源回退链: LLM_* → DEEPSEEK/QWEN 旧键（兼容旧配置迁移）。
    # 所有 LLM 消费方（deepseek_helper / subject_classifier /
    # auto_retry_agent / failure_analysis_agent）一律通过这些属性取配置,
    # 禁止各自内联回退逻辑(此前三处内联已互相漂移)。
    # 空串经 (get(k) or '').strip() 归一化为"未配置", 避免设置页保存空值后
    # requests.post('') 触发 MissingSchema。

    @property
    def llm_api_url(self) -> str:
        """默认大模型 API 端点（空串/缺失回退默认 DeepSeek 端点，永不为空）"""
        return (self.get("LLM_API_URL") or "").strip() \
            or DEFAULT_CONFIG["LLM_API_URL"]

    @property
    def llm_model(self) -> str:
        """默认大模型名称（空串/缺失回退 deepseek-chat，永不为空）"""
        return (self.get("LLM_MODEL") or "").strip() \
            or DEFAULT_CONFIG["LLM_MODEL"]

    @property
    def llm_api_key(self) -> str:
        """默认大模型 API 密钥: LLM_API_KEY → DEEPSEEK_API_KEY → QWEN_API_KEY（兼容旧配置迁移）"""
        return (self.get("LLM_API_KEY") or "").strip() \
            or (self.get("DEEPSEEK_API_KEY") or "").strip() \
            or (self.get("QWEN_API_KEY") or "").strip()

    @property
    def llm_vl_api_url(self) -> str:
        """多模态端点: LLM_VL_API_URL → QWEN_API_URL(含其默认 MaaS 端点, 兼容
        只配了 QWEN_API_KEY+QWEN_VL_MODEL 的旧配置)。绝不回退到文本模型端点
        (LLM_API_URL)——多模态请求发往文本端点必失败。截图识别是否启用由
        auto_retry_agent 的三键闸门(model/url/key 齐全)判定, 不依赖本键"""
        return (self.get("LLM_VL_API_URL") or "").strip() \
            or (self.get("QWEN_API_URL") or "").strip()

    @property
    def llm_vl_model(self) -> str:
        """多模态模型名: LLM_VL_MODEL → QWEN_VL_MODEL（兼容旧配置）。留空=禁用截图识别"""
        return (self.get("LLM_VL_MODEL") or "").strip() \
            or (self.get("QWEN_VL_MODEL") or "").strip()

    @property
    def llm_vl_api_key(self) -> str:
        """多模态 API 密钥: LLM_VL_API_KEY → QWEN_API_KEY（兼容旧配置）。
        留空则截图识别禁用（auto_retry_agent 三键闸门）"""
        return (self.get("LLM_VL_API_KEY") or "").strip() \
            or (self.get("QWEN_API_KEY") or "").strip()

    # ==================== API 服务配置 ====================

    @property
    def api_server_host(self) -> str:
        """API服务监听地址"""
        return self.get("API_SERVER_HOST", "0.0.0.0")

    @property
    def api_server_port(self) -> int:
        """API服务监听端口"""
        return self.get("API_SERVER_PORT", 8000)

    @property
    def upload_temp_dir(self) -> str:
        """小程序上传文件临时目录"""
        return self.get("UPLOAD_TEMP_DIR", "./upload_temp")

    @property
    def browser_restart_interval(self) -> int:
        """浏览器定时重启间隔(每N次上传后重启,0=不自动重启)"""
        return self.get("BROWSER_RESTART_INTERVAL", 50)

    def reload(self):
        """
        重新加载配置文件
        用于用户修改配置文件后刷新配置
        """
        self._load_config()

    def get_all_editable(self) -> dict:
        """
        返回所有可在 GUI 中编辑的配置项及其元数据。
        结构: {分组名: [{"key": ..., "label": ..., "type": ..., "default": ...,
                         "help": ..., "options": ..., "required": ...}]}
        type: "str" | "int" | "float" | "bool" | "combo"
        "default" 一律取模块级 DEFAULT_CONFIG（唯一真源），不再内联第二份默认值。
        "required": True 的字符串字段在设置页保存时不允许为空
        （如 ROOT_DIR 清空会导致文件监控启动失败）。
        """
        d = DEFAULT_CONFIG
        return {
            "基本设置": [
                {"key": "ROOT_DIR", "label": "监控文件夹", "type": "str", "required": True,
                 "default": d["ROOT_DIR"],
                 "help": "监控的根目录，新文件放入其子文件夹后自动上传"},
                {"key": "CHROME_DRIVER_PATH", "label": "ChromeDriver 路径", "type": "str",
                 "default": d["CHROME_DRIVER_PATH"],
                 "help": "留空让 Selenium 自动管理，或填写 chromedriver.exe 的完整路径"},
            ],
            "网站账号": [
                {"key": "WEBSITE_URL", "label": "目标网站URL", "type": "str", "required": True,
                 "default": d["WEBSITE_URL"], "help": "七天网络作业平台地址"},
                {"key": "USERNAME", "label": "登录用户名", "type": "str",
                 "default": d["USERNAME"], "help": "平台登录账号"},
                {"key": "PASSWORD", "label": "登录密码", "type": "str",
                 "default": d["PASSWORD"], "help": "平台登录密码"},
                {"key": "ROLE", "label": "用户角色", "type": "combo",
                 "default": d["ROLE"], "options": ["超级管理员", "老师"],
                 "help": "影响平台操作权限"},
            ],
            "AI 模型配置": [
                {"key": "LLM_API_URL", "label": "API 地址", "type": "str",
                 "default": d["LLM_API_URL"],
                 "help": "OpenAI 兼容的 API 端点（留空=使用默认 DeepSeek 端点）"},
                {"key": "LLM_MODEL", "label": "模型名称", "type": "str",
                 "default": d["LLM_MODEL"],
                 "help": "科目识别和智能体默认使用的模型（留空=deepseek-chat）"},
                {"key": "LLM_API_KEY", "label": "API Key", "type": "str",
                 "default": d["LLM_API_KEY"],
                 "help": "调用大模型的 API 密钥（留空则回退 DEEPSEEK_API_KEY/QWEN_API_KEY）"},
                {"key": "LLM_VL_API_URL", "label": "多模态 API 地址", "type": "str",
                 "default": d["LLM_VL_API_URL"],
                 "help": "截图识别专用。需与模型名/Key 一起填写才启用，留空=禁用"},
                {"key": "LLM_VL_MODEL", "label": "多模态模型名称", "type": "str",
                 "default": d["LLM_VL_MODEL"],
                 "help": "截图识别专用。需与地址/Key 一起填写才启用，留空=禁用"},
                {"key": "LLM_VL_API_KEY", "label": "多模态 API Key", "type": "str",
                 "default": d["LLM_VL_API_KEY"],
                 "help": "截图识别专用。需与地址/模型名一起填写才启用，留空=禁用"},
            ],
            "浏览器设置": [
                {"key": "CHROME_PROFILE_DIR", "label": "Chrome 用户目录", "type": "str",
                 "default": d["CHROME_PROFILE_DIR"],
                 "help": "留空=临时目录,填写则复用登录态"},
                {"key": "BROWSER_IDLE_TIMEOUT", "label": "浏览器空闲超时(秒)", "type": "int",
                 "default": d["BROWSER_IDLE_TIMEOUT"], "help": "空闲多久后自动关闭浏览器"},
                {"key": "UPLOAD_IDLE_TIMEOUT", "label": "上传后空闲超时(秒)", "type": "int",
                 "default": d["UPLOAD_IDLE_TIMEOUT"], "help": "上传完成后无操作关闭超时"},
                {"key": "BROWSER_RESTART_INTERVAL", "label": "浏览器重启间隔(次)", "type": "int",
                 "default": d["BROWSER_RESTART_INTERVAL"], "help": "每N次上传后重启浏览器,0=不自动重启"},
            ],
            "Agent 自动重试": [
                {"key": "AUTO_RETRY_ENABLE", "label": "启用自动重试", "type": "bool",
                 "default": d["AUTO_RETRY_ENABLE"], "help": "是否启用 Agent 自动接管失败任务"},
                {"key": "AI_RETRY_AGENT_ENABLE", "label": "启用 AI 决策", "type": "bool",
                 "default": d["AI_RETRY_AGENT_ENABLE"], "help": "是否启用 AI 驱动的重试决策"},
                {"key": "AI_AGENT_MAX_STEPS", "label": "ReAct 最大步数", "type": "int",
                 "default": d["AI_AGENT_MAX_STEPS"], "help": "Agent 单次决策最大循环次数"},
                {"key": "AUTO_RETRY_SCAN_INTERVAL", "label": "扫描间隔(秒)", "type": "int",
                 "default": d["AUTO_RETRY_SCAN_INTERVAL"], "help": "多久扫描一次失败记录"},
                {"key": "AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD", "label": "熔断阈值(次)", "type": "int",
                 "default": d["AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD"], "help": "同类错误多少次后触发熔断"},
            ],
            "API 服务": [
                {"key": "API_SERVER_HOST", "label": "监听地址", "type": "str",
                 "default": d["API_SERVER_HOST"], "help": "API 服务监听地址"},
                {"key": "API_SERVER_PORT", "label": "监听端口", "type": "int",
                 "default": d["API_SERVER_PORT"], "help": "API 服务监听端口"},
            ],
        }

    @staticmethod
    def _sanitize_strings(obj):
        """
        递归清理字典/列表中的字符串值，移除不可见Unicode控制字符。
        防止从网页、PDF、聊天软件复制粘贴路径时带入的
        LRE/RLE/LRO/RLO/PDF/ZWSP/BOM等字符导致Windows路径解析失败。

        Args:
            obj: 待清理的任意对象（dict/list/str/其他）

        Returns:
            清理后的对象
        """
        def clean_str(s):
            return ''.join(
                ch for ch in s
                if unicodedata.category(ch) not in ('Cf', 'Cc')
                or ch in ('\n', '\r', '\t')
            )

        if isinstance(obj, dict):
            return {k: ConfigManager._sanitize_strings(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [ConfigManager._sanitize_strings(v) for v in obj]
        if isinstance(obj, str):
            return clean_str(obj)
        return obj
