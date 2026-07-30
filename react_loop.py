"""
通用 Agent 循环引擎 (Function Calling 版)
基于 OpenAI 兼容的原生 tools 协议 (DeepSeek 支持) 驱动 LLM 自主决策循环
不依赖任何项目模块，纯通用 AI Agent 执行引擎

相比旧版文本 ReAct (正则解析 Thought/Action/Final):
  - 工具调用由 API 返回结构化 tool_calls 字段，无需正则解析
  - 工具参数为 JSON Schema 约束的结构化数据，支持类型
  - 格式合法性由模型服务端保证，删除了全部降级正则/格式训话/逃生舱口
"""
import inspect
import json
import re
import traceback
from typing import Any, Callable, Dict, List, Optional


def tool(name: Optional[str] = None,
         description: str = "",
         params: Optional[Dict[str, str]] = None) -> Callable:
    """
    工具装饰器：将普通函数标记为 Agent 工具，并附加 Schema 元数据。
    Schema 的参数名/必填性/类型仍由 inspect.signature 从函数签名自动生成，
    装饰器只补充无法从签名推断的信息（工具名、功能描述、参数描述）。

    Args:
        name: 工具名（缺省用函数名，并自动剥离 'tool_' 前缀）
        description: 工具功能描述（缺省取函数 docstring）
        params: 参数名 → 参数描述 的映射（写入 Schema 参数的 description）

    用法:
        @tool(description="查询城市天气", params={"city": "城市名"})
        def get_weather(city=""):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        tool_name = name or fn.__name__
        if tool_name.startswith("tool_"):
            tool_name = tool_name[len("tool_"):]
        fn.__tool__ = {
            "name": tool_name,
            "description": description or inspect.getdoc(fn) or "",
            "params": params or {},
        }
        return fn
    return decorator


def _tool_meta(fn: Callable) -> Dict:
    """读取工具元数据；未装饰的函数回退到函数名 + docstring"""
    meta = getattr(fn, "__tool__", None)
    if meta is not None:
        return meta
    return {
        "name": getattr(fn, "__name__", str(fn)),
        "description": inspect.getdoc(fn) or "",
        "params": {},
    }


class ReActLoop:
    """
    Function Calling 循环引擎（保留 ReActLoop 类名，兼容既有调用方）

    工作流程：
    1. 将 @tool 装饰的工具函数签名转换为 JSON Schema，随请求发送给 LLM
    2. LLM 返回结构化 tool_calls → 引擎执行工具 → 以 tool 消息回传结果
    3. 重复 2 直到 LLM 不再调用工具，其最终文本回复解析为 JSON 结果
    """

    def __init__(self,
                 llm,
                 system_prompt: str,
                 tools: List[Callable],
                 max_steps: int = 10,
                 log_fn: Optional[Callable] = None):
        """
        Args:
            llm: LLM 客户端，必须有 chat_messages_raw(messages, tools) 方法
            system_prompt: 系统提示词（任务角色与决策规则说明）
            tools: @tool 装饰的工具函数列表（名称/描述/参数描述取自装饰器元数据）
            max_steps: 最大循环步数
            log_fn: 可选的日志回调，用于输出推理过程
        """
        self.llm = llm
        # 工具名 → 可调用函数（名称来自 @tool 元数据，未装饰的回退函数名）
        self.tools: Dict[str, Callable] = {
            _tool_meta(fn)["name"]: fn for fn in tools
        }
        self.max_steps = max_steps
        self.log = log_fn or (lambda _: None)

        # 从函数签名自动生成 OpenAI 兼容的工具 Schema
        self.tool_schemas = self._build_tool_schemas()
        self.system_prompt = self._build_system_prompt(system_prompt)

    # ─── Schema 构建 ───

    def _build_tool_schemas(self) -> List[Dict]:
        """
        为每个工具生成 OpenAI 兼容的 JSON Schema：
        - 参数名/必填性来自函数签名（inspect）
        - 参数类型根据默认值推断（bool/int/float/其他→string）
        - 工具描述/参数描述来自 @tool 装饰器元数据
        """
        schemas = []
        for name, fn in self.tools.items():
            meta = _tool_meta(fn)
            param_descs: Dict[str, str] = meta.get("params", {})
            properties: Dict[str, Dict] = {}
            required: List[str] = []
            try:
                sig = inspect.signature(fn)
                for pname, param in sig.parameters.items():
                    if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                                      inspect.Parameter.VAR_KEYWORD):
                        continue
                    default = param.default
                    if default is inspect.Parameter.empty:
                        ptype = "string"
                        required.append(pname)
                    elif isinstance(default, bool):
                        ptype = "boolean"
                    elif isinstance(default, int):
                        ptype = "integer"
                    elif isinstance(default, float):
                        ptype = "number"
                    else:
                        ptype = "string"
                    prop: Dict[str, Any] = {"type": ptype}
                    if param_descs.get(pname):
                        prop["description"] = param_descs[pname]
                    properties[pname] = prop
            except (TypeError, ValueError):
                pass  # 无法内省签名的可调用对象 → 空参数 Schema

            schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": meta.get("description", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return schemas

    def _build_system_prompt(self, base_prompt: str) -> str:
        """追加工具调用与结束方式说明（工具清单由 Schema 携带，无需重复罗列）"""
        return f"""{base_prompt}

## 工具调用与结束方式

- 通过原生函数调用(tool calls)使用工具，参数按工具 Schema 提供
- 如果工具返回 Error，分析原因并尝试其他方案
- 当任务完成、无需再调用工具时，直接回复最终结果：内容必须是一个合法的 JSON 对象，不要附加任何其他文字"""

    # ─── 主循环 ───

    def run(self, task: str, context: Optional[Dict] = None) -> Dict:
        """
        执行 Function Calling 循环

        Args:
            task: 任务描述文本
            context: 可选的上下文字典，会作为第一条用户消息的附加信息

        Returns:
            {
                "success": bool,
                "result": Any,       # 最终回复解析出的 JSON 结果
                "steps": int,        # 实际执行的步数
                "reasoning": str,    # 最后一次的助手文本内容
                "history": List[Dict]  # 完整对话历史（含 tool 消息）
            }
        """
        messages: List[Dict] = [{"role": "system", "content": self.system_prompt}]

        task_content = task
        if context:
            task_content += f"\n\n## 上下文信息\n```json\n{json.dumps(context, ensure_ascii=False, indent=2, default=str)}\n```"
        messages.append({"role": "user", "content": task_content})

        last_thought = ""

        for step in range(1, self.max_steps + 1):
            self.log(f"[Agent Step {step}/{self.max_steps}]")

            msg = self.llm.chat_messages_raw(messages, tools=self.tool_schemas)
            if msg is None:
                self.log("[Agent] LLM 调用失败，终止循环")
                return {
                    "success": False,
                    "result": None,
                    "steps": step,
                    "reasoning": last_thought,
                    "error": "LLM 调用失败",
                    "history": messages,
                }

            content = (msg.get("content") or "").strip()
            tool_calls = msg.get("tool_calls") or []

            # 助手消息原样入历史（含 tool_calls，API 要求成对出现）
            assistant_msg: Dict[str, Any] = {"role": "assistant",
                                             "content": msg.get("content") or ""}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            if content:
                last_thought = content
                self.log(f"  Thought: {content[:100]}")

            # ── 无工具调用 → 最终结果 ──
            if not tool_calls:
                if not content:
                    self.log("[Agent] LLM 返回空回复且无工具调用，终止")
                    return {
                        "success": False,
                        "result": None,
                        "steps": step,
                        "reasoning": last_thought,
                        "error": "LLM 返回空回复",
                        "history": messages,
                    }
                result = self._parse_final(content)
                self.log(f"  Final: {json.dumps(result, ensure_ascii=False, default=str)[:200]}")
                return {
                    "success": True,
                    "result": result,
                    "steps": step,
                    "reasoning": last_thought,
                    "history": messages,
                }

            # ── 执行工具调用（同一轮可能有多个）──
            for tc in tool_calls:
                fn_info = tc.get("function", {})
                tool_name = fn_info.get("name", "")
                kwargs = self._parse_tool_args(fn_info.get("arguments"))

                self.log(f"  Action: {tool_name}({json.dumps(kwargs, ensure_ascii=False, default=str)[:100]})")

                if tool_name not in self.tools:
                    observation = (f"Error: 工具 '{tool_name}' 不存在。"
                                   f"可用工具: {', '.join(self.tools.keys())}")
                else:
                    try:
                        result = self.tools[tool_name](**kwargs)
                        observation = json.dumps(result, ensure_ascii=False, default=str)
                    except TypeError as e:
                        observation = f"Error: 参数错误 - {e}"
                    except Exception as e:
                        observation = f"Error: 工具执行异常 - {e}"
                        traceback.print_exc()

                self.log(f"  Observation: {observation[:200]}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tool_name,
                    "content": observation,
                })

        # 达到最大步数
        self.log(f"[Agent] 达到最大步数 {self.max_steps}，强制终止")
        return {
            "success": False,
            "result": None,
            "steps": self.max_steps,
            "reasoning": last_thought,
            "error": "达到最大步数限制",
            "history": messages,
        }

    # ─── 解析辅助 ───

    @staticmethod
    def _parse_tool_args(arguments: Any) -> Dict:
        """解析 tool_calls 中的 arguments 字段（标准为 JSON 字符串）"""
        if isinstance(arguments, dict):
            return arguments
        if not arguments:
            return {}
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _parse_final(text: str) -> Any:
        """
        将最终文本回复解析为 JSON。
        模型偶尔会包一层代码块或附加说明文字，做轻量提取；
        完全无法解析时包装为 {"raw": 原文}。
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 代码块内的 JSON
        code_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
        if code_match:
            try:
                return json.loads(code_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 第一个 { 到最后一个 } 的片段
        try:
            start = text.index('{')
            end = text.rindex('}') + 1
            return json.loads(text[start:end])
        except (ValueError, json.JSONDecodeError):
            pass

        return {"raw": text}


# ─── CLI 独立测试入口 ───
if __name__ == "__main__":
    """用模拟工具测试 Function Calling 循环"""
    import sys
    sys.path.insert(0, '.')
    from deepseek_helper import DeepSeekHelper

    # 模拟工具
    @tool(description="查询城市天气", params={"city": "城市名"})
    def get_weather(city=""):
        """模拟天气查询"""
        weather_data = {"北京": "晴天 25°C", "上海": "多云 28°C", "深圳": "阵雨 30°C"}
        return {"city": city, "weather": weather_data.get(city, "未知")}

    @tool(description="计算数学表达式", params={"expression": "数学表达式"})
    def calculate(expression=""):
        """模拟计算器（仅允许纯算术字符，防止任意代码执行）"""
        if not re.fullmatch(r'[\d\s+\-*/().%]+', expression or ''):
            return {"error": "表达式含非法字符，仅支持纯算术运算"}
        try:
            return {"expression": expression, "result": eval(expression)}  # noqa: S307 — 已白名单过滤
        except Exception as e:
            return {"error": str(e)}

    tools = [get_weather, calculate]

    system_prompt = """你是一个智能助手，可以使用工具来回答用户问题。
遇到需要查询天气或计算的问题时，调用相应工具获取信息，然后给出最终答案。
最终答案输出 JSON: {"summary": "一句话总结"}"""

    llm = DeepSeekHelper()
    if not llm.api_key:
        print("未配置 DEEPSEEK_API_KEY，跳过测试")
        sys.exit(1)

    agent = ReActLoop(
        llm=llm,
        system_prompt=system_prompt,
        tools=tools,
        max_steps=5,
        log_fn=lambda msg: print(f"  {msg}")
    )

    print("=" * 60)
    print("测试: 北京今天天气怎么样？")
    result = agent.run("查询北京的天气，然后计算 25 + 17 的结果，最后用一句话总结。")
    print(f"\n结果: {json.dumps(result['result'], ensure_ascii=False)}")
    print(f"步数: {result['steps']}, 成功: {result['success']}")
