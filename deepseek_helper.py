"""
DeepSeek API 通用调用封装
提供 chat() 和 chat_json() 两个方法，供 AutoRetryAgent 和 FailureAnalysisAgent 共用
与 subject_classifier.py 使用相同的 API URL、模型、超时和重试策略
"""
import json
import re
import time
import traceback
from typing import Dict, List, Optional

import requests

from config_manager import ConfigManager


class DeepSeekHelper:
    """LLM API 通用调用工具，支持 DeepSeek / Qwen 等多提供商"""

    # 默认配置
    DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
    DEFAULT_MODEL = "deepseek-chat"
    TIMEOUT = 30
    MAX_RETRIES = 3
    RETRY_INTERVAL = 2  # 秒

    def __init__(self, api_url: str = None, api_key: str = None, model: str = None):
        """
        Args:
            api_url: API 端点 URL，默认 DeepSeek
            api_key: API Key，默认从 ConfigManager 读取 deepseek_api_key
            model: 模型名，默认 deepseek-chat
        """
        cfg = ConfigManager()
        self.api_url = api_url or self.DEFAULT_API_URL
        self.api_key = api_key or cfg.deepseek_api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(self, system_prompt: str, user_content: str, temperature: float = 0) -> Optional[str]:
        """
        调用 DeepSeek API，返回自由文本

        Args:
            system_prompt: 系统提示词
            user_content: 用户消息内容
            temperature: 温度参数，默认 0 保证输出稳定

        Returns:
            AI 回复文本，失败返回 None
        """
        if not self.api_key:
            print("DeepSeekHelper: 未配置 API Key")
            return None

        return self._retry_api_call(
            lambda: self._call_api(system_prompt, user_content, temperature),
            desc="API调用"
        )

    def chat_json(self, system_prompt: str, user_content: str, temperature: float = 0) -> Optional[Dict]:
        """
        调用 DeepSeek API，返回结构化 JSON

        Args:
            system_prompt: 系统提示词
            user_content: 用户消息内容
            temperature: 温度参数，默认 0

        Returns:
            解析后的 dict，失败返回 None
        """
        raw = self.chat(system_prompt, user_content, temperature)
        if not raw:
            return None

        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 尝试从 markdown code block 提取 JSON
        code_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', raw)
        if code_match:
            try:
                return json.loads(code_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 尝试找到第一个 { 到最后一个 } 的 JSON 对象
        try:
            start = raw.index('{')
            end = raw.rindex('}') + 1
            return json.loads(raw[start:end])
        except (ValueError, json.JSONDecodeError):
            pass

        print(f"DeepSeekHelper: 无法解析 AI 返回的 JSON: {raw[:200]}")
        return None

    def chat_messages(self, messages: List[Dict], temperature: float = 0) -> Optional[str]:
        """
        多轮对话调用，支持完整消息历史

        Args:
            messages: 消息列表，格式 [{"role": "system"|"user"|"assistant", "content": "..."}]
            temperature: 温度参数，默认 0

        Returns:
            AI 回复文本，失败返回 None
        """
        if not self.api_key:
            print("DeepSeekHelper: 未配置 API Key")
            return None

        def _call():
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"
            }
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature
            }
            response = requests.post(
                self.api_url, headers=headers, json=payload, timeout=self.TIMEOUT
            )
            response.raise_for_status()
            result = response.json()

            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0]["message"]["content"].strip()
                if content:
                    return content
            return None

        return self._retry_api_call(_call, desc="多轮对话")

    def _is_auth_error(self, status_code: int) -> bool:
        """判断是否为认证/授权错误（不应重试）"""
        return status_code in (401, 403)

    def _call_api(self, system_prompt: str, user_content: str, temperature: float) -> Optional[str]:
        """单次 API 调用"""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": temperature
        }

        response = requests.post(self.api_url, headers=headers, json=payload, timeout=self.TIMEOUT)
        response.raise_for_status()
        result = response.json()

        if "choices" in result and len(result["choices"]) > 0:
            content = result["choices"][0]["message"]["content"].strip()
            return content if content else None

        return None

    def _retry_api_call(self, api_call_fn: callable, desc: str = "API调用") -> Optional[str]:
        """
        带重试和错误分类的 API 调用包装器

        Args:
            api_call_fn: 执行实际 API 调用的无参可调用对象
            desc: 调用描述（用于日志）

        Returns:
            API 返回文本，失败返回 None
        """
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                return api_call_fn()
            except requests.exceptions.HTTPError as e:
                response = getattr(e, 'response', None)
                status = response.status_code if response is not None else 0
                if self._is_auth_error(status):
                    print(f"DeepSeekHelper: {desc} 认证失败(HTTP {status}), 不再重试")
                    return None
                print(f"DeepSeekHelper: 第{attempt}次{desc} HTTP错误({status}) - {e}")
            except requests.exceptions.Timeout:
                print(f"DeepSeekHelper: 第{attempt}次{desc}超时")
            except requests.exceptions.ConnectionError as e:
                print(f"DeepSeekHelper: 第{attempt}次{desc}连接失败 - {e}")
            except Exception as e:
                print(f"DeepSeekHelper: 第{attempt}次{desc}异常 - {e}")
                traceback.print_exc()

            if attempt < self.MAX_RETRIES:
                time.sleep(self.RETRY_INTERVAL)

        return None


# ─── CLI 独立测试入口 ───
if __name__ == "__main__":
    """测试 DeepSeekHelper 基本功能"""
    helper = DeepSeekHelper()

    if not helper.api_key:
        print("未配置 DEEPSEEK_API_KEY，跳过测试")
        exit(1)

    # 测试 chat
    print("=== 测试 chat ===")
    result = helper.chat("你是一个助手，请用 JSON 回复", '回复 {"test": true, "message": "你好"}')
    print(f"chat 结果: {result}")

    # 测试 chat_json
    print("\n=== 测试 chat_json ===")
    result = helper.chat_json(
        "你是一个助手，必须严格输出 JSON 格式",
        '请输出 {"status": "ok", "value": 42}'
    )
    print(f"chat_json 结果: {result}")
