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
    ACTION_RE = re.compile(
        r'Action:\s*([a-zA-Z_][a-zA-Z0-9_]*)'  # 工具名
        r'\s*\((.*?)\)'  # 参数括号
        r'(?:\s*$)',  # 行尾
        re.MULTILINE
    )
    # 使用 \Z 替代 $ 以支持多行 JSON（re.MULTILINE 下 $ 匹配行尾会导致截断）
    FINAL_RE = re.compile(r'Final:\s*(.+?)(?:\s*\Z)', re.MULTILINE | re.DOTALL)
    THOUGHT_RE = re.compile(r'Thought:\s*(.+?)(?:\n\n|\n(?=Action:|\n*Final:)|\Z)', re.MULTILINE | re.DOTALL)

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

            # 检查是否是 Final
            final_match = self.FINAL_RE.search(response)
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

            # 检查是否包含 Action
            action_match = self.ACTION_RE.search(response)
            if not action_match:
                # LLM 既没有给出 Action 也没有 Final，给予反馈
                self.log("  [ReAct] 未检测到 Action 或 Final，提示 LLM")
                messages.append({
                    "role": "user",
                    "content": "你没有输出 Action 或 Final。请输出 Action: tool_name(key=value) 来调用工具，或 Final: {...} 来结束任务。"
                })
                continue

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
