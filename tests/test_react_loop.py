"""
ReActLoop 引擎健壮性测试（模拟"不听话/异常的 LLM"）
覆盖: 未知工具 / 畸形参数 / 工具抛异常 / 参数错误 / 步数上限 /
      LLM调用失败 / 空回复 / 最终JSON解析降级 / Schema 自动生成
"""
import json

import pytest

from react_loop import ReActLoop, tool
from conftest import ScriptedLLM, call, calls, final, tool_observations


# ─── 测试用工具 ───

@tool(description="回显文本", params={"text": "要回显的文本"})
def tool_echo(text=""):
    return {"echo": text}


@tool(description="必崩工具")
def tool_boom():
    raise RuntimeError("boom!")


@tool(description="带类型参数", params={"x": "必填", "count": "整数", "flag": "布尔"})
def tool_typed(x, count=1, flag=False):
    return {"x": x, "count": count, "flag": flag}


def build_loop(llm, max_steps=5):
    return ReActLoop(llm=llm,
                     system_prompt="测试系统提示词",
                     tools=[tool_echo, tool_boom, tool_typed],
                     max_steps=max_steps)


class TestToolCallRobustness:
    def test_unknown_tool_returns_error_and_loop_continues(self):
        llm = ScriptedLLM([
            call("no_such_tool"),
            final({"done": True}),
        ])
        result = build_loop(llm).run("测试")
        assert result["success"]
        obs = tool_observations(result["history"], "no_such_tool")
        assert obs and "不存在" in obs[0]
        # 错误提示中应列出可用工具，帮助 LLM 自我纠正
        assert "echo" in obs[0]

    def test_malformed_arguments_fallback_to_empty(self):
        """arguments 不是合法 JSON → 按空参数调用（有默认值的工具不报错）"""
        llm = ScriptedLLM([
            ("raw", {"content": "", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "echo", "arguments": "{{{ not json"},
            }]}),
            final({"done": True}),
        ])
        result = build_loop(llm).run("测试")
        assert result["success"]
        obs = tool_observations(result["history"], "echo")
        assert json.loads(obs[0]) == {"echo": ""}

    def test_wrong_param_name_reports_type_error(self):
        llm = ScriptedLLM([
            call("echo", nonexist_param="x"),
            final({"done": True}),
        ])
        result = build_loop(llm).run("测试")
        assert result["success"]
        obs = tool_observations(result["history"], "echo")
        assert "参数错误" in obs[0]

    def test_missing_required_param_reports_type_error(self):
        llm = ScriptedLLM([
            call("typed"),  # 缺少必填参数 x
            final({"done": True}),
        ])
        result = build_loop(llm).run("测试")
        obs = tool_observations(result["history"], "typed")
        assert "参数错误" in obs[0]

    def test_tool_exception_captured_as_observation(self):
        """工具抛异常不能炸掉循环，应作为 observation 回传"""
        llm = ScriptedLLM([
            call("boom"),
            final({"done": True}),
        ])
        result = build_loop(llm).run("测试")
        assert result["success"]
        obs = tool_observations(result["history"], "boom")
        assert "工具执行异常" in obs[0] and "boom!" in obs[0]

    def test_multiple_tool_calls_in_one_round(self):
        llm = ScriptedLLM([
            calls(("echo", {"text": "a"}), ("echo", {"text": "b"})),
            final({"done": True}),
        ])
        result = build_loop(llm).run("测试")
        obs = tool_observations(result["history"], "echo")
        assert len(obs) == 2
        assert json.loads(obs[0]) == {"echo": "a"}
        assert json.loads(obs[1]) == {"echo": "b"}


class TestLoopTermination:
    def test_max_steps_forced_stop(self):
        """LLM 无限调用工具 → 达到 max_steps 强制终止且 success=False"""
        llm = ScriptedLLM([call("echo", text=str(i)) for i in range(10)])
        result = build_loop(llm, max_steps=3).run("测试")
        assert not result["success"]
        assert result["steps"] == 3
        assert "最大步数" in result["error"]

    def test_llm_returns_none_terminates(self):
        llm = ScriptedLLM([("none", None)])
        result = build_loop(llm).run("测试")
        assert not result["success"]
        assert "LLM 调用失败" in result["error"]

    def test_empty_reply_without_tool_calls_terminates(self):
        llm = ScriptedLLM([("raw", {"content": "", "tool_calls": []})])
        result = build_loop(llm).run("测试")
        assert not result["success"]
        assert "空回复" in result["error"]


class TestFinalParsing:
    def test_plain_json(self):
        assert ReActLoop._parse_final('{"a": 1}') == {"a": 1}

    def test_fenced_code_block(self):
        text = '决策如下：\n```json\n{"action": "enqueue"}\n```'
        assert ReActLoop._parse_final(text) == {"action": "enqueue"}

    def test_embedded_json_with_prose(self):
        text = '我认为应该重试。{"action": "enqueue", "retry_level": "L1"} 以上。'
        assert ReActLoop._parse_final(text) == {"action": "enqueue",
                                                "retry_level": "L1"}

    def test_garbage_wrapped_as_raw(self):
        assert ReActLoop._parse_final("完全不是JSON") == {"raw": "完全不是JSON"}

    def test_parse_tool_args_variants(self):
        assert ReActLoop._parse_tool_args('{"a": 1}') == {"a": 1}
        assert ReActLoop._parse_tool_args({"a": 1}) == {"a": 1}
        assert ReActLoop._parse_tool_args("") == {}
        assert ReActLoop._parse_tool_args(None) == {}
        assert ReActLoop._parse_tool_args("[1,2]") == {}  # 非 dict → 空
        assert ReActLoop._parse_tool_args("{{bad") == {}


class TestSchemaGeneration:
    def test_tool_prefix_stripped_and_names_registered(self):
        loop = build_loop(ScriptedLLM())
        assert set(loop.tools.keys()) == {"echo", "boom", "typed"}

    def test_schema_types_and_required_from_signature(self):
        loop = build_loop(ScriptedLLM())
        schema = next(s for s in loop.tool_schemas
                      if s["function"]["name"] == "typed")
        params = schema["function"]["parameters"]
        assert params["required"] == ["x"]
        assert params["properties"]["x"]["type"] == "string"
        assert params["properties"]["count"]["type"] == "integer"
        assert params["properties"]["flag"]["type"] == "boolean"

    def test_param_descriptions_injected(self):
        loop = build_loop(ScriptedLLM())
        schema = next(s for s in loop.tool_schemas
                      if s["function"]["name"] == "echo")
        assert schema["function"]["parameters"]["properties"]["text"][
            "description"] == "要回显的文本"
