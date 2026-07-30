"""
L2 决策层评估：真实 LLM(DeepSeek) + 伪造失败记录 + Mock 浏览器
评估 ReAct 循环的「决策质量」——LLM 在各种故障形态下是否做出正确决策、
是否遵守工具协议（先诊断→修复→验证→入队），以及硬闸门被迫兜底的频率。

与 L1 的区别：
  L1 用脚本化 LLM 测「闸门拦得住吗」（安全性）
  L2 用真实 LLM   测「LLM 本来就做得对吗」（决策质量）

运行（会产生真实 API 调用费用，不被 pytest 收集）:
  .venv\\Scripts\\python tests\\l2_decision_eval.py            # 全部场景
  .venv\\Scripts\\python tests\\l2_decision_eval.py --repeat 3 # 每场景重复3次测稳定性
  .venv\\Scripts\\python tests\\l2_decision_eval.py --only transient_timeout dialog_stuck

报告输出: reports/l2_decision_eval_<时间戳>.md / .json
"""
import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根

from conftest import (FakeBrowser, FakeConfig, make_agent,               # noqa: E402
                      insert_failed_record)
from db_manager import DatabaseManager                                   # noqa: E402
from deepseek_helper import DeepSeekHelper                               # noqa: E402
from error_types import RetryLevel                                       # noqa: E402

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "reports")


# ─── 真实 LLM 包装：记录请求/响应/耗时，接口与 DeepSeekHelper 一致 ───

class RecordingLLM:
    def __init__(self, inner: DeepSeekHelper):
        self.inner = inner
        self.api_key = inner.api_key
        self.requests = []       # 与 ScriptedLLM 对齐，供 observation 提取
        self.responses = []
        self.latencies = []

    def chat_messages_raw(self, messages, tools=None):
        self.requests.append({"messages": list(messages), "tools": tools})
        t0 = time.time()
        resp = self.inner.chat_messages_raw(messages, tools)
        self.latencies.append(time.time() - t0)
        self.responses.append(resp)
        return resp

    @property
    def tool_call_sequence(self):
        """LLM 全程按序发出的工具调用名列表"""
        seq = []
        for resp in self.responses:
            if not resp:
                continue
            for tc in resp.get("tool_calls") or []:
                seq.append(tc.get("function", {}).get("name", "?"))
        return seq


# ─── 场景定义 ───
# 每个场景 = 伪造的失败记录 + Mock 浏览器形态 + 期望决策/协议约束

REPAIR_TOOLS = {"close_dialog", "press_escape", "refresh_page", "navigate_home",
                "re_login", "restart_browser", "full_recovery"}

SCENARIOS = [
    {
        "name": "transient_timeout",
        "desc": "偶发提交超时，页面完好在首页 → 应轻量入队，不应动用重量级修复",
        "record": dict(error_type="upload_submit_timeout",
                       fail_stage="submit_upload", error_message="上传提交超时"),
        "browser": {},                                   # 默认: home、验证通过
        "expect": {"actions": {"enqueue"},
                   "levels": {"L1", "L2"},
                   "forbidden_tools": {"restart_browser", "re_login",
                                       "full_recovery"}},
    },
    {
        "name": "dialog_stuck",
        "desc": "上传弹窗残留卡在页面 → 应先关弹窗/复位并验证后再入队",
        "record": dict(error_type="upload_submit_timeout",
                       fail_stage="submit_upload",
                       error_message="上传提交超时，确认按钮无响应"),
        "browser": {"default_state": "upload_dialog",
                    "capture_result": {
                        "success": True, "has_error": False, "errors": [],
                        "combined_text": "", "is_permanent": False,
                        "page_state": "upload_dialog"},
                    # 第1次验证(修复前)失败，修复后验证通过
                    "verify_results": [
                        {"verified": False, "state": "upload_dialog",
                         "details": "上传对话框仍然打开"}]},
        "page_context": {"current_page_state": "upload_dialog",
                         "state_details": "检测到上传对话框未关闭"},
        "expect": {"actions": {"enqueue"},
                   "levels": {"L2", "L3"},
                   "require_repair": True,
                   "forbidden_tools": {"restart_browser", "re_login"}},
    },
    {
        "name": "session_lost_on_login_page",
        "desc": "上传中被挤下线，页面已回到登录页(状态错位) → 应完整恢复而非盲目重试",
        "record": dict(error_type="upload_submit_timeout",
                       fail_stage="submit_upload",
                       error_message="点击提交后无响应"),
        "browser": {"default_state": "login", "is_logged_in": False,
                    "session_lost_on_page": True,
                    "capture_result": {
                        "success": True, "has_error": False, "errors": [],
                        "combined_text": "请登录", "is_permanent": False,
                        "page_state": "login"}},
        "page_context": {"current_page_state": "login",
                         "state_mismatch": True,
                         "mismatch_detail": "⚠️ 页面状态已变化！原始错误发生在"
                         "「submit_upload」阶段，预期页面应在 ('home', "
                         "'upload_dialog')，但当前页面是「login」。请先通过 "
                         "full_recovery 恢复到正常状态，不要盲目按原错误阶段重试。"},
        "expect": {"actions": {"enqueue"},
                   "levels": {"L3", "L4"},
                   "require_repair": True,
                   "required_tools_any": {"full_recovery", "re_login"},
                   "forbidden_tools": set()},
    },
    {
        "name": "permanent_school_not_activated",
        "desc": "页面提示学校未开通服务(永久性业务错误) → 必须转人工，严禁入队",
        "record": dict(error_type="upload_submit_timeout",
                       fail_stage="submit_upload",
                       error_message="提交失败"),
        "browser": {"capture_result": {
            "success": True, "has_error": True,
            "errors": ["该校未开通数智作业服务"],
            "combined_text": "该校未开通数智作业服务，请联系管理员",
            "is_permanent": True,
            "suggested_error_type": "school_not_activated",
            "page_state": "home"}},
        "expect": {"actions": {"manual"},
                   "forbidden_tools": {"enqueue_retry", "restart_browser",
                                       "re_login", "full_recovery"}},
    },
    {
        "name": "school_switch_fail",
        "desc": "学校切换失败(当前校≠目标校) → 应环境复位后入队重试",
        "record": dict(error_type="school_switch_fail",
                       fail_stage="school_check",
                       error_message="切换到「测试中学」失败：下拉列表未找到目标学校项",
                       error_category="browser_error"),
        "browser": {"default_state": "home"},
        "expect": {"actions": {"enqueue"},
                   "levels": {"L1", "L2", "L3"},
                   "forbidden_tools": {"restart_browser", "re_login"}},
    },
    {
        "name": "browser_dead",
        "desc": "浏览器进程已死(no_browser) → 应重启浏览器并验证后入队",
        "record": dict(error_type="browser_start_fail",
                       fail_stage="browser_init",
                       error_message="浏览器初始化失败: chrome not reachable",
                       error_category="browser_error"),
        "browser": {"driver": None, "is_logged_in": False,
                    "is_initialized": False, "default_state": "no_browser",
                    "capture_result": {
                        "success": False, "has_error": False, "errors": [],
                        "combined_text": "", "is_permanent": False,
                        "page_state": "no_browser"}},
        "page_context": {"current_page_state": "no_browser",
                         "state_details": "浏览器进程不存在"},
        "expect": {"actions": {"enqueue"},
                   "levels": {"L3", "L4"},
                   "require_repair": True,
                   "required_tools_any": {"restart_browser", "full_recovery"},
                   "forbidden_tools": set()},
    },
    {
        "name": "retry_exhausted",
        "desc": "重试次数已用尽(3/3) → 入队会被工具拒绝，LLM 应转人工/跳过",
        "record": dict(error_type="upload_submit_timeout",
                       fail_stage="submit_upload",
                       error_message="上传提交超时", retry_count=3),
        "browser": {},
        "expect": {"actions": {"manual", "skip"},
                   "forbidden_tools": {"restart_browser", "re_login",
                                       "full_recovery"}},
    },
    {
        "name": "tripped_error_type",
        "desc": "该错误类型已熔断 → LLM 应识别熔断状态并跳过等待",
        "record": dict(error_type="upload_submit_timeout",
                       fail_stage="submit_upload",
                       error_message="上传提交超时"),
        "browser": {},
        "tripped": {"upload_submit_timeout"},
        "expect": {"actions": {"skip", "manual"},
                   "forbidden_tools": {"restart_browser", "re_login",
                                       "full_recovery"}},
    },
    {
        "name": "form_validate_fail",
        "desc": "表单校验失败(临时性toast) → 类型上限1次，可轻量入队或转人工",
        "record": dict(error_type="form_validate_fail",
                       fail_stage="submit_upload",
                       error_message="表单校验失败：请选择作业时间",
                       error_category="biz_error"),
        "browser": {"capture_result": {
            "success": True, "has_error": True,
            "errors": ["请选择作业时间"],
            "combined_text": "请选择作业时间",
            "is_permanent": False,
            "page_state": "home"}},
        "expect": {"actions": {"enqueue", "manual"},
                   "levels": {"L1", "L2"},
                   "forbidden_tools": {"restart_browser", "re_login",
                                       "full_recovery"}},
    },
    {
        "name": "page_error_network",
        "desc": "网络错误页面(error状态) → 应刷新/回首页修复并验证后入队",
        "record": dict(error_type="network_error",
                       fail_stage="submit_upload",
                       error_message="网络连接中断: ERR_CONNECTION_RESET",
                       error_category="system_error"),
        "browser": {"default_state": "error",
                    "capture_result": {
                        "success": True, "has_error": True,
                        "errors": ["网页无法访问"],
                        "combined_text": "ERR_CONNECTION_RESET 网页无法访问",
                        "is_permanent": False,
                        "page_state": "error"},
                    "verify_results": [
                        {"verified": False, "state": "error",
                         "details": "页面仍是错误页"}]},
        "page_context": {"current_page_state": "error",
                         "state_details": "页面显示网络错误"},
        "expect": {"actions": {"enqueue", "skip"},
                   "levels": {"L2", "L3"},
                   "require_repair": True,
                   "forbidden_tools": {"re_login"}},
    },
]


# ─── 单场景执行与评分 ───

def build_browser(spec):
    b = FakeBrowser()
    for key, value in spec.items():
        setattr(b, key, value)
    return b


def extract_observations(llm):
    """从最后一次请求的完整对话里提取 (tool_name, observation) 序列"""
    if not llm.requests:
        return []
    msgs = llm.requests[-1]["messages"]
    return [(m.get("name", "?"), m.get("content", ""))
            for m in msgs if m.get("role") == "tool"]


def run_scenario(scenario, llm_factory, run_idx=0):
    # 每个场景独立临时库（重置单例）
    DatabaseManager._instance = None
    DatabaseManager._connection = None
    db_dir = tempfile.mkdtemp(prefix="l2eval_")
    db = DatabaseManager(os.path.join(db_dir, f"{uuid.uuid4().hex[:8]}.db"))
    try:
        # 伪造失败记录（文件真实存在，避免文件预检查干扰决策评估）
        f = os.path.join(db_dir, "高二数学第3次周末作业.docx")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("fake")
        record = insert_failed_record(db, f, **scenario["record"])

        browser = build_browser(scenario.get("browser", {}))
        llm = RecordingLLM(llm_factory())
        agent = make_agent(db, browser, llm, config=FakeConfig())

        t0 = time.time()
        decision = agent._run_react_loop(
            record, set(scenario.get("tripped", set())),
            scenario.get("page_context"), "")
        duration = time.time() - t0

        return grade(scenario, decision, llm, browser, duration, run_idx)
    finally:
        try:
            db.close()
        except Exception:
            pass
        DatabaseManager._instance = None
        DatabaseManager._connection = None


def grade(scenario, decision, llm, browser, duration, run_idx):
    expect = scenario["expect"]
    seq = llm.tool_call_sequence
    used_repairs = [t for t in seq if t in REPAIR_TOOLS]
    observations = extract_observations(llm)

    result = {
        "scenario": scenario["name"],
        "run": run_idx,
        "desc": scenario["desc"],
        "decision": None,
        "tool_sequence": seq,
        "llm_calls": len(llm.responses),
        "duration_s": round(duration, 1),
        "avg_latency_s": round(sum(llm.latencies) / len(llm.latencies), 1)
        if llm.latencies else 0,
        "checks": {},
        "violations": [],
        "gate_interventions": [],
    }

    # 决策提取
    if decision is None:
        result["decision"] = {"action": None}
        result["violations"].append("ReAct 循环返回 None（LLM 决策失败）")
        result["grade"] = "FAIL"
        return result

    action = decision.get("action")
    level = decision.get("retry_level")
    level_str = level.value if isinstance(level, RetryLevel) else level
    result["decision"] = {"action": action, "retry_level": level_str,
                          "reason": decision.get("reason", "")}

    checks = result["checks"]

    # 1. 动作正确性（核心，权重最高）
    checks["action_ok"] = action in expect["actions"]
    if not checks["action_ok"]:
        result["violations"].append(
            f"动作错误: 期望 {sorted(expect['actions'])}, 实际 {action}")

    # 2. 重试级别合理性（仅入队时评）
    if action == "enqueue" and "levels" in expect:
        checks["level_ok"] = level_str in expect["levels"]
        if not checks["level_ok"]:
            result["violations"].append(
                f"级别不当: 期望 {sorted(expect['levels'])}, 实际 {level_str}")

    # 3. 协议: capture_page_error 必须是第一个工具调用
    checks["capture_first"] = bool(seq) and seq[0] == "capture_page_error"
    if not checks["capture_first"]:
        result["violations"].append(
            f"未先诊断: 第一个工具是 {seq[0] if seq else '(无)'}")

    # 4. 协议: 禁用工具（不必要的重量级动作 = 决策不精准）
    forbidden_used = [t for t in seq if t in expect.get("forbidden_tools", set())]
    checks["no_forbidden_tools"] = not forbidden_used
    if forbidden_used:
        result["violations"].append(f"动用了不必要的重量级工具: {forbidden_used}")

    # 5. 协议: 场景要求修复动作时确实修了
    if expect.get("require_repair"):
        checks["repair_performed"] = bool(used_repairs)
        if not used_repairs:
            result["violations"].append("页面已损坏但未执行任何修复动作")
        required_any = expect.get("required_tools_any")
        if required_any:
            checks["right_repair_tool"] = bool(set(seq) & required_any)
            if not checks["right_repair_tool"]:
                result["violations"].append(
                    f"修复工具选择不当: 期望其中之一 {sorted(required_any)}, "
                    f"实际 {used_repairs}")

    # 6. 协议: 修复后主动验证（LLM 自己调 verify_recovery，而非闸门代劳）
    #    full_recovery 内部自带验证，视为已验证
    if used_repairs:
        explicit_verify = "verify_recovery" in seq or "full_recovery" in seq
        checks["explicit_verify"] = explicit_verify
        if not explicit_verify and browser.verify_calls > 0:
            result["gate_interventions"].append(
                "LLM 修复后未主动验证，由 enqueue 闸门强制补验证")

    # 7. 闸门兜底痕迹（工具层拒绝/最终决策被降级）
    for tool_name, obs in observations:
        if tool_name == "enqueue_retry" and any(
                kw in obs for kw in ("拒绝入队", "已熔断", "最大重试次数",
                                     "已在重试队列")):
            result["gate_interventions"].append(f"enqueue_retry 被拒: {obs[:60]}")
    if decision.get("action") == "skip" and "验证未通过" in decision.get("reason", ""):
        result["gate_interventions"].append("最终 enqueue 决策被回溯检查降级为 skip")

    # 综合评级
    if not checks["action_ok"]:
        result["grade"] = "FAIL"          # 决策方向就错了
    elif result["violations"]:
        result["grade"] = "PARTIAL"       # 方向对但协议/精准度有瑕疵
    else:
        result["grade"] = "PASS"
    return result


# ─── 报告生成 ───

def render_report(results, model, started_at):
    lines = ["# L2 决策层评估报告",
             "",
             f"- 模型: `{model}`",
             f"- 时间: {started_at}",
             f"- 场景数: {len(set(r['scenario'] for r in results))}，"
             f"总运行: {len(results)}",
             ""]
    n_pass = sum(1 for r in results if r["grade"] == "PASS")
    n_partial = sum(1 for r in results if r["grade"] == "PARTIAL")
    n_fail = sum(1 for r in results if r["grade"] == "FAIL")
    total_calls = sum(r["llm_calls"] for r in results)
    total_gates = sum(len(r["gate_interventions"]) for r in results)
    lines += [f"## 总览",
              "",
              f"| PASS | PARTIAL | FAIL | 决策正确率 | LLM总调用 | 闸门兜底次数 |",
              f"|---|---|---|---|---|---|",
              f"| {n_pass} | {n_partial} | {n_fail} | "
              f"{(n_pass + n_partial) / len(results):.0%} | "
              f"{total_calls} | {total_gates} |",
              "",
              "> 决策正确率 = 动作方向正确(PASS+PARTIAL)占比；"
              "闸门兜底次数越多说明 LLM 自律性越差（但系统仍安全）",
              "",
              "## 场景明细", ""]
    lines += ["| 场景 | 评级 | 决策 | 级别 | 工具链 | 调用/耗时 | 问题 |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        d = r["decision"] or {}
        issues = "; ".join(r["violations"] + r["gate_interventions"]) or "—"
        seq = " → ".join(r["tool_sequence"]) or "(未调用工具)"
        lines.append(
            f"| {r['scenario']}#{r['run']} | **{r['grade']}** "
            f"| {d.get('action')} | {d.get('retry_level') or '—'} "
            f"| {seq} | {r['llm_calls']}次/{r['duration_s']}s | {issues} |")
    lines += ["", "## 各场景说明", ""]
    seen = set()
    for r in results:
        if r["scenario"] in seen:
            continue
        seen.add(r["scenario"])
        lines.append(f"- **{r['scenario']}**: {r['desc']}")
        d = r["decision"] or {}
        if d.get("reason"):
            lines.append(f"  - LLM 决策理由: {d['reason']}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="L2 决策层评估（真实 LLM）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每场景重复次数（测决策稳定性）")
    parser.add_argument("--only", nargs="*", default=None,
                        help="只跑指定场景名")
    args = parser.parse_args()

    probe = DeepSeekHelper()
    if not probe.api_key:
        print("未配置 DEEPSEEK_API_KEY，无法运行 L2 评估")
        sys.exit(1)

    scenarios = [s for s in SCENARIOS
                 if not args.only or s["name"] in args.only]
    if not scenarios:
        print(f"没有匹配的场景，可选: {[s['name'] for s in SCENARIOS]}")
        sys.exit(1)

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"L2 决策层评估开始: {len(scenarios)} 场景 x {args.repeat} 次, "
          f"模型={probe.model}")
    results = []
    for scenario in scenarios:
        for i in range(args.repeat):
            tag = f"{scenario['name']}#{i}"
            print(f"\n▶ {tag}: {scenario['desc']}")
            try:
                r = run_scenario(scenario, DeepSeekHelper, i)
            except Exception as e:
                import traceback
                traceback.print_exc()
                r = {"scenario": scenario["name"], "run": i,
                     "desc": scenario["desc"], "decision": None,
                     "tool_sequence": [], "llm_calls": 0, "duration_s": 0,
                     "avg_latency_s": 0, "checks": {},
                     "violations": [f"执行异常: {e}"],
                     "gate_interventions": [], "grade": "FAIL"}
            results.append(r)
            d = r["decision"] or {}
            print(f"  评级={r['grade']}  决策={d.get('action')}"
                  f"/{d.get('retry_level') or '—'}  "
                  f"工具链={' → '.join(r['tool_sequence']) or '(无)'}")
            for v in r["violations"]:
                print(f"  ✗ {v}")
            for g in r["gate_interventions"]:
                print(f"  ⚠ 闸门兜底: {g}")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(REPORTS_DIR, f"l2_decision_eval_{stamp}.md")
    json_path = os.path.join(REPORTS_DIR, f"l2_decision_eval_{stamp}.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_report(results, probe.model, started_at))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=str)

    n_pass = sum(1 for r in results if r["grade"] == "PASS")
    n_partial = sum(1 for r in results if r["grade"] == "PARTIAL")
    n_fail = sum(1 for r in results if r["grade"] == "FAIL")
    print(f"\n{'=' * 60}")
    print(f"完成: PASS={n_pass}  PARTIAL={n_partial}  FAIL={n_fail}  "
          f"(共 {len(results)} 次运行)")
    print(f"报告: {md_path}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
