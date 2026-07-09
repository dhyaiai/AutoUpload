"""
通用 ReAct (Reasoning + Acting) 循环引擎
提供 LLM 驱动的 Thought → Action → Observation 自主决策循环
不依赖任何项目模块，纯通用 AI Agent 执行引擎
"""
import json
import re
import traceback
from typing import Any, Callable, Dict, List, Optional


class ReActLoop:
    """
    ReAct 循环引擎

    工作流程：
    1. LLM 收到任务 + 可用工具列表
    2. LLM 输出 Thought（推理）+ Action（工具调用）
    3. 引擎解析 Action → 执行工具 → 返回 Observation（观察结果）
    4. 重复 2-3 直到 LLM 输出 Final（最终结果）
    """

    # LLM 输出格式的正则
    # ── Action 匹配 ──
    # 主模式：Action: tool_name(key=value, ...)  —— 末尾不锚定 $，允许行尾有注释
    ACTION_RE = re.compile(
        r'(?:^|\n)\s*Action\s*[:：]\s*'             # Action: 或 Action：（中英文冒号）
        r'([a-zA-Z_][a-zA-Z0-9_]*)'                  # 工具名
        r'\s*\(\s*(.*?)\s*\)'                         # 参数（允许括号内空白）
        r'(?:\s*(?:#.*)?)$',                          # 行尾（可选注释）
        re.MULTILINE
    )
    # 降级模式1：无括号调用（无参工具省略了括号）
    ACTION_NOARGS_RE = re.compile(
        r'(?:^|\n)\s*Action\s*[:：]\s*'
        r'([a-zA-Z_][a-zA-Z0-9_]*)'
        r'(?:\s*\(\))?'                                # 可选空括号
        r'\s*$',
        re.MULTILINE
    )
    # 降级模式2：代码块包裹的 Action（LLM 习惯用 ``` 包裹）
    ACTION_FENCED_RE = re.compile(
        r'```(?:json|text|plain)?\s*\n?'
        r'\s*Action\s*[:：]\s*'
        r'([a-zA-Z_][a-zA-Z0-9_]*)'
        r'\s*\(\s*(.*?)\s*\)'
        r'\s*\n?```',
        re.MULTILINE | re.DOTALL
    )
    # ── Final 匹配 ──
    # 主模式：不要求 Final 在字符串绝对末尾，允许后面有空白或代码块结束符
    FINAL_RE = re.compile(
        r'(?:^|\n)\s*Final\s*[:：]\s*'
        r'(\{.+?\}|\[.+?\]|.+?)'                       # JSON 对象/数组 或 自由文本
        r'\s*$',
        re.MULTILINE | re.DOTALL
    )
    # 降级：代码块包裹的 Final
    FINAL_FENCED_RE = re.compile(
        r'```(?:json)?\s*\n?'
        r'(?:Final\s*[:：]\s*)?'                       # 代码块内可选的 Final: 前缀
        r'(\{[\s\S]+?\}|\[[\s\S]+?\])'                 # JSON
        r'\s*\n?```',
        re.MULTILINE
    )
    THOUGHT_RE = re.compile(r'Thought\s*[:：]\s*(.+?)(?:\n\n|\n(?=Action|\n*Final)|\Z)', re.MULTILINE | re.DOTALL)

    def __init__(self,
                 llm,
                 system_prompt: str,
                 tools: Dict[str, Callable],
                 tool_descriptions: Dict[str, str],
                 max_steps: int = 10,
                 log_fn: Optional[Callable] = None):
        """
        Args:
            llm: LLM 客户端，必须有 chat_messages(messages, temperature) 方法
            system_prompt: 系统提示词（应包含工具使用说明）
            tools: 工具名 → 可调用函数的映射
            tool_descriptions: 工具名 → 功能描述的映射（嵌入 system_prompt）
            max_steps: 最大循环步数
            log_fn: 可选的日志回调，用于输出推理过程
        """
        self.llm = llm
        self.tools = tools
        self.tool_descriptions = tool_descriptions
        self.max_steps = max_steps
        self.log = log_fn or (lambda _: None)

        # 构建完整的系统提示词
        self.system_prompt = self._build_system_prompt(system_prompt)

    def _build_system_prompt(self, base_prompt: str) -> str:
        """将工具列表嵌入系统提示词"""
        tool_list = "\n".join(
            f"- {name}: {desc}"
            for name, desc in self.tool_descriptions.items()
        )
        return f"""{base_prompt}

## 可用工具

你可以调用以下工具来完成任务：
{tool_list}

## 输出格式

每轮你必须按以下格式输出：

Thought: [你对当前状态的分析推理]
Action: tool_name(param1=value1, param2=value2)

当你认为任务已完成时，输出：

Thought: [总结性推理]
Final: [JSON格式的最终结果]

注意：
- 思考和行动放在同一轮输出中
- Action 的参数用 key=value 格式，字符串值不需要引号
- Final 必须是合法的 JSON 对象
- 如果工具返回 Error，分析原因并尝试其他方案"""

    def run(self, task: str, context: Optional[Dict] = None) -> Dict:
        """
        执行 ReAct 循环

        Args:
            task: 任务描述文本
            context: 可选的上下文字典，会作为第一条用户消息的附加信息

        Returns:
            {
                "success": bool,
                "result": Any,       # Final 中解析的 JSON 结果
                "steps": int,        # 实际执行的步数
                "reasoning": str,    # 最后一步的 Thought
                "history": List[Dict]  # 完整对话历史
            }
        """
        messages = [{"role": "system", "content": self.system_prompt}]

        # 构建任务消息
        task_content = task
        if context:
            task_content += f"\n\n## 上下文信息\n```json\n{json.dumps(context, ensure_ascii=False, indent=2, default=str)}\n```"

        messages.append({"role": "user", "content": task_content})

        last_thought = ""

        for step in range(1, self.max_steps + 1):
            self.log(f"[ReAct Step {step}/{self.max_steps}]")

            # 调用 LLM
            response = self.llm.chat_messages(messages)
            if not response:
                self.log("[ReAct] LLM 调用失败，终止循环")
                return {
                    "success": False,
                    "result": None,
                    "steps": step,
                    "reasoning": last_thought,
                    "error": "LLM 调用失败",
                    "history": messages
                }

            messages.append({"role": "assistant", "content": response})

            # 提取 Thought
            thought_match = self.THOUGHT_RE.search(response)
            if thought_match:
                last_thought = thought_match.group(1).strip()
                self.log(f"  Thought: {last_thought[:100]}...")

            # ── 检测 Final（含降级模式）──
            final_match = self.FINAL_RE.search(response)
            if not final_match:
                final_match = self.FINAL_FENCED_RE.search(response)  # 代码块降级
            if final_match:
                final_text = final_match.group(1).strip()
                self.log(f"  Final: {final_text[:200]}")

                # 尝试解析 JSON
                try:
                    result = json.loads(final_text)
                except json.JSONDecodeError:
                    # 尝试从代码块提取
                    code_match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', final_text)
                    if code_match:
                        try:
                            result = json.loads(code_match.group(1).strip())
                        except json.JSONDecodeError:
                            result = {"raw": final_text}
                    else:
                        result = {"raw": final_text}

                return {
                    "success": True,
                    "result": result,
                    "steps": step,
                    "reasoning": last_thought,
                    "history": messages
                }

            # ── 检测 Action（含降级模式）──
            action_match = self.ACTION_RE.search(response)
            if not action_match:
                action_match = self.ACTION_FENCED_RE.search(response)  # 代码块降级
            if not action_match:
                # 无参降级：匹配 "Action: tool_name" 无括号形式
                action_match = self.ACTION_NOARGS_RE.search(response)
                # 过滤：排除 "Action: skip" / "Action: manual" 这类非工具调用词
                if action_match and action_match.group(1) in self.tools:
                    pass  # 有效无参调用
                elif action_match:
                    action_match = None  # 不匹配任何已知工具，继续找

            if not action_match:
                # LLM 没有输出可解析的 Action 或 Final，给予反馈
                # 记录原始响应（截断）便于调试
                self.log(f"  [ReAct] 未检测到 Action/Final，LLM原始响应(前200字): {response[:200]}")
                self.log("  [ReAct] 未检测到 Action 或 Final，提示 LLM")

                # 动态反馈：第一第二次宽容提醒，第三次起给出强制格式示例
                consecutive_failures = getattr(self, '_format_fail_count', 0) + 1
                self._format_fail_count = consecutive_failures

                # ── 逃生舱口：连续3次格式失败后，尝试直接从原始响应中提取 Final JSON ──
                # 某些模型（如 Qwen）不遵循 ReAct 格式，但会在文本中输出 JSON 决策
                if consecutive_failures >= 3:
                    extracted = self._try_extract_final_from_text(response)
                    if extracted:
                        self.log(f"  [ReAct] 逃生舱口: 从原始响应中提取到 Final JSON (连续{consecutive_failures}次格式失败)")
                        self._format_fail_count = 0
                        try:
                            result = json.loads(extracted) if isinstance(extracted, str) else extracted
                        except json.JSONDecodeError:
                            result = {"raw": str(extracted)}
                        return {
                            "success": True,
                            "result": result,
                            "steps": step,
                            "reasoning": last_thought,
                            "history": messages
                        }

                if consecutive_failures <= 2:
                    hint = (
                        "你的上一条回复没有包含 Action 或 Final 指令。\n\n"
                        "请严格按以下格式回复（任选一种）：\n\n"
                        "格式1 - 调用工具:\n"
                        "Thought: 你的分析推理\n"
                        "Action: tool_name(key1=value1)\n\n"
                        "格式2 - 完成:\n"
                        "Thought: 你的总结\n"
                        "Final: {\"action\": \"enqueue\", \"retry_level\": \"L2\", \"reason\": \"...\"}\n\n"
                        "注意：Action 必须在单独一行以 \"Action: \" 开头，"
                        "Final 必须在单独一行以 \"Final: \" 开头。"
                    )
                else:
                    # 第三次及以后：给出具体强制示例
                    hint = (
                        f"你已经连续 {consecutive_failures} 次没有输出正确的格式。\n\n"
                        "现在你必须严格、一字不差地按以下格式回复。请选择一个行动：\n\n"
                        "如果要调用工具，请输出：\n"
                        "Thought: [一句话分析]\n"
                        "Action: capture_page_error()\n\n"
                        "如果要完成任务，请输出：\n"
                        "Thought: 任务完成\n"
                        "Final: {\"action\": \"enqueue\", \"retry_level\": \"L1\", \"reason\": \"页面干净可重试\"}\n\n"
                        "现在就输出，不要输出其他额外内容，不要用中文冒号。"
                    )

                messages.append({"role": "user", "content": hint})
                continue
            else:
                # 格式匹配成功，重置失败计数
                self._format_fail_count = 0

            tool_name = action_match.group(1)
            args_str = action_match.group(2).strip()

            self.log(f"  Action: {tool_name}({args_str[:100]})")

            # 验证工具存在
            if tool_name not in self.tools:
                observation = f"Error: 工具 '{tool_name}' 不存在。可用工具: {', '.join(self.tools.keys())}"
                self.log(f"  Observation: {observation}")
                messages.append({"role": "user", "content": f"Observation: {observation}"})
                continue

            # 解析参数
            kwargs = self._parse_args(args_str)

            # 执行工具
            try:
                result = self.tools[tool_name](**kwargs)
                observation = json.dumps(result, ensure_ascii=False, default=str)
            except TypeError as e:
                observation = f"Error: 参数错误 - {e}"
            except Exception as e:
                observation = f"Error: 工具执行异常 - {e}"
                traceback.print_exc()

            self.log(f"  Observation: {observation[:200]}")

            # 将观察结果反馈给 LLM
            messages.append({"role": "user", "content": f"Observation: {observation}"})

        # 达到最大步数
        self.log(f"[ReAct] 达到最大步数 {self.max_steps}，强制终止")
        return {
            "success": False,
            "result": None,
            "steps": self.max_steps,
            "reasoning": last_thought,
            "error": "达到最大步数限制",
            "history": messages
        }

    @staticmethod
    def _parse_args(args_str: str) -> Dict[str, str]:
        """
        解析 key=value 格式的参数列表
        支持 value 中包含空格（引号包裹时）
        例如: 'file_path=C:\\test.docx, retry_level=L2'
        """
        if not args_str.strip():
            return {}

        kwargs = {}
        # 用逗号分割，.*? 允许空值（避免空值吞噬后续参数）
        pattern = re.compile(r"""(\w+)\s*=\s*(.*?)(?=\s*,\s*\w+\s*=|\s*$)""")
        for match in pattern.finditer(args_str):
            key = match.group(1)
            value = match.group(2).strip()
            # 去除首尾引号
            if len(value) >= 2:
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
            kwargs[key] = value
        return kwargs

    @staticmethod
    def _try_extract_final_from_text(text: str) -> Optional[str]:
        """
        逃生舱口：从 LLM 的任意文本响应中尝试提取 Final 决策 JSON。

        当 LLM 连续多次不遵循 ReAct 格式时调用，作为最后的兜底尝试。
        查找包含 action 字段的 JSON 对象（enqueue/manual/skip）。

        Returns:
            提取到的 JSON 字符串，或 None
        """
        # 策略1：匹配 {"action": "enqueue"|"manual"|"skip", ...} 的 JSON 对象
        action_json_re = re.compile(
            r'\{[^{}]*"action"\s*:\s*"(?:enqueue|manual|skip)"[^{}]*\}',
            re.DOTALL
        )
        match = action_json_re.search(text)
        if match:
            return match.group(0)

        # 策略2：从代码块中提取 JSON
        fenced_match = re.search(r'```(?:json)?\s*\n?([\s\S]+?)\n?```', text)
        if fenced_match:
            inner = fenced_match.group(1).strip()
            if inner.startswith('{'):
                return inner

        # 策略3：匹配任意 JSON 对象（最宽松）
        json_obj_re = re.compile(r'\{[^{}]*\}')
        for m in json_obj_re.finditer(text):
            candidate = m.group(0)
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and 'action' in obj:
                    return candidate
            except json.JSONDecodeError:
                continue

        return None


# ─── CLI 独立测试入口 ───
if __name__ == "__main__":
    """用模拟工具测试 ReAct 循环"""
    import sys
    sys.path.insert(0, '.')
    from deepseek_helper import DeepSeekHelper

    # 模拟工具
    def get_weather(city):
        """模拟天气查询"""
        weather_data = {"北京": "晴天 25°C", "上海": "多云 28°C", "深圳": "阵雨 30°C"}
        return {"city": city, "weather": weather_data.get(city, "未知")}

    def calculate(expression):
        """模拟计算器"""
        try:
            return {"expression": expression, "result": eval(expression)}
        except Exception as e:
            return {"error": str(e)}

    tools = {"get_weather": get_weather, "calculate": calculate}
    tool_descs = {
        "get_weather": "查询城市天气。参数: city=城市名",
        "calculate": "计算数学表达式。参数: expression=数学表达式"
    }

    system_prompt = """你是一个智能助手，可以使用工具来回答用户问题。
遇到需要查询天气或计算的问题时，调用相应工具获取信息，然后给出最终答案。"""

    llm = DeepSeekHelper()
    if not llm.api_key:
        print("未配置 DEEPSEEK_API_KEY，跳过测试")
        sys.exit(1)

    agent = ReActLoop(
        llm=llm,
        system_prompt=system_prompt,
        tools=tools,
        tool_descriptions=tool_descs,
        max_steps=5,
        log_fn=lambda msg: print(f"  {msg}")
    )

    print("=" * 60)
    print("测试: 北京今天天气怎么样？")
    result = agent.run("查询北京的天气，然后计算 25 + 17 的结果，最后用一句话总结。")
    print(f"\n结果: {json.dumps(result['result'], ensure_ascii=False)}")
    print(f"步数: {result['steps']}, 成功: {result['success']}")
