"""
配置管理模块
功能:读取和管理config.json配置文件,提供全局配置访问接口
特点:使用单例模式,支持默认值,避免硬编码
"""
import json
import os
import re
import unicodedata


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
        # 定义默认配置（敏感信息留空，需用户自行填写）
        default_config = {
            "ROOT_DIR": r"C:\Users\Administrator\Desktop\upload",  # 监控的根目录
            "WEBSITE_URL": "https://zuoye.7net.cc",                # 目标网站URL
            "USERNAME": "",                                        # 登录用户名（需填写）
            "PASSWORD": "",                                        # 登录密码（需填写）
            "ROLE": "超级管理员",                                   # 用户角色
            "DEEPSEEK_API_KEY": "",                                # DeepSeek API密钥（需填写）
            "CHROME_DRIVER_PATH": "",                              # Chrome驱动路径（留空让Selenium自动管理）
            "FILE_STABLE_DELAY": 2,                                # 文件稳定等待时间(秒)
            "BROWSER_IDLE_TIMEOUT": 1800,                          # 浏览器空闲超时时间(秒)
            "MAX_RETRY_COUNT": 3,                                  # 最大重试次数
            "SLEEP_INTERVAL": 0.5,                                # 操作间隔时间(秒)
            "MINIMIZE_TO_TRAY": True,                              # 关闭窗口时最小化到托盘(False=直接退出)

            # AutoRetryAgent 配置
            "AUTO_RETRY_ENABLE": True,                              # 是否启用自动重试Agent
            "AUTO_RETRY_SCAN_INTERVAL": 60,                         # 扫描间隔(秒)
            "AUTO_RETRY_BACKOFF_SECONDS": [30, 120, 600],           # 指数退避间隔(秒)
            "AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD": 10,             # 熔断阈值(5分钟内同类错误次数)
            "AUTO_RETRY_CIRCUIT_BREAKER_DURATION": 1800,            # 熔断持续时间(秒，默认30分钟)
        }

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
                self._config = default_config
                self._save_config()  # 保存默认配置到文件
        else:
            # 配置文件不存在,使用默认配置并创建文件
            print(f"配置文件不存在,创建默认配置: {self.config_path}")
            self._config = default_config
            self._save_config()

    def _save_config(self):
        """
        将当前配置保存到JSON文件
        """
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"错误: 无法保存配置文件 ({e})")

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
        设置配置项的值并保存到文件

        Args:
            key: 配置项的键名
            value: 要设置的值
        """
        self._config[key] = self._sanitize_strings(value)
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
    def file_stable_delay(self) -> int:
        """获取文件稳定等待时间(秒)"""
        return self.get("FILE_STABLE_DELAY", 2)

    @property
    def browser_idle_timeout(self) -> int:
        """获取浏览器空闲超时时间(秒)"""
        return self.get("BROWSER_IDLE_TIMEOUT", 1800)

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

    def reload(self):
        """
        重新加载配置文件
        用于用户修改配置文件后刷新配置
        """
        self._load_config()

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
