"""
L3 端到端故障注入：真实浏览器 + 主动破坏状态 → 验证「修复动作真的有效」

与 L1/L2 的区别:
  L1: Mock 一切，测闸门逻辑（安全性）
  L2: 真 LLM + Mock 浏览器，测决策质量
  L3: 真浏览器 + 真平台，测修复动作在真实 DOM/会话上是否真正生效

两部分:
  Part A 原子修复验证（确定性，无 LLM）:
    每个场景 = 主动破坏 → 确认破坏生效 → 只用智能体的原子修复工具修 →
    用生产同款 verify_home_ready 硬验收
  Part B 智能体全链路（真 LLM + 真浏览器 + 临时 DB）:
    破坏页面 + 伪造失败记录 → _process_one_record 完整跑 →
    验证智能体自己诊断/修复/验证/入队，且页面真的恢复

安全约束:
  - 只打开上传对话框，绝不填表、绝不点提交
  - 学校用当前真实学校，不触发切换
  - 临时数据库，不碰生产 data.db
  - 破坏性最强的场景(会话丢失/杀浏览器)放最后

运行（需要真实网络+平台账号，约5~8分钟）:
  .venv\\Scripts\\python tests\\l3_e2e_faultinject.py                 # 全部
  .venv\\Scripts\\python tests\\l3_e2e_faultinject.py --skip-agent    # 只跑 Part A
  .venv\\Scripts\\python tests\\l3_e2e_faultinject.py --only dialog_stuck overlay_mask
"""
import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conftest import FakeConfig, make_agent, insert_failed_record, get_record  # noqa: E402
from l2_decision_eval import RecordingLLM                                      # noqa: E402
from browser_automation import BrowserAutomation                               # noqa: E402
from db_manager import DatabaseManager                                         # noqa: E402
from deepseek_helper import DeepSeekHelper                                     # noqa: E402

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "reports")


# ─── 破坏动作（真实 DOM/会话级） ───

UPLOAD_BTN_SELECTORS = [
    ("xpath", "//*[@id='main']/section/div/div[1]/div/div[2]/span"),
    ("id", "upload-homework-btn"),
    ("css selector", "div.upload-btn"),
    ("xpath", "//span[contains(text(), '上传')]/parent::div"),
]


def open_upload_dialog(browser):
    """点击首页上传入口打开对话框（只打开，绝不提交）"""
    for by, sel in UPLOAD_BTN_SELECTORS:
        try:
            el = browser.driver.find_element(by, sel)
            if el.is_displayed():
                el.click()
                time.sleep(2)
                return True
        except Exception:
            continue
    return False


def inject_overlay(browser):
    """JS 注入一个 Element-UI 同款遮罩层（挡住整个页面）"""
    browser.driver.execute_script("""
        var mask = document.createElement('div');
        mask.className = 'v-modal';
        mask.id = 'l3-fault-mask';
        mask.style.cssText = 'position:fixed;top:0;left:0;width:100%;' +
            'height:100%;opacity:0.5;background:#000;z-index:2000;';
        document.body.appendChild(mask);
    """)
    time.sleep(0.5)
    return True


def navigate_away(browser):
    """把页面开到无关地址（模拟导航迷失/页面被外链劫持）"""
    browser.driver.get("about:blank")
    time.sleep(1)
    return True


def kill_session(browser):
    """清空 cookie + 本地存储并刷新（模拟会话过期/被挤下线）"""
    browser.driver.delete_all_cookies()
    try:
        browser.driver.execute_script(
            "window.localStorage.clear(); window.sessionStorage.clear();")
    except Exception:
        pass
    browser.driver.refresh()
    time.sleep(3)
    browser.is_logged_in = False
    return True


def kill_browser(browser):
    """直接杀掉浏览器进程（模拟浏览器崩溃）"""
    browser.close()
    time.sleep(1)
    return browser.driver is None


# ─── Part A 场景矩阵: 破坏 → 确认破坏生效 → 原子修复 → verify_home_ready 验收 ───
# repair_steps 只允许使用智能体暴露给 LLM 的原子工具对应的浏览器方法

SCENARIOS = [
    {
        "name": "dialog_stuck",
        "desc": "真实打开上传对话框不关闭 → close_dialogs 应能关掉并回到可用首页",
        "break_fn": open_upload_dialog,
        "broken_states": {"upload_dialog"},
        "repair_steps": [("close_dialogs", lambda b: b.close_dialogs())],
    },
    {
        "name": "overlay_mask",
        "desc": "JS注入全屏遮罩层 → refresh_page 应能清掉卡死的前端状态",
        "break_fn": inject_overlay,
        # 遮罩不改变 page_state，破坏生效以 verify_home_ready 不通过为准
        "broken_states": None,
        "repair_steps": [("refresh_page", lambda b: b.refresh_page())],
    },
    {
        "name": "navigate_lost",
        "desc": "页面被开到 about:blank → navigate_home 应能导回平台首页",
        "break_fn": navigate_away,
        "broken_states": {"unknown", "error"},
        "repair_steps": [("navigate_home", lambda b: b.navigate_home())],
    },
    {
        "name": "session_lost",
        "desc": "清cookie+存储+刷新(被挤下线) → recover_session 应能自动重登录",
        "break_fn": kill_session,
        "broken_states": {"login", "role_select"},
        "repair_steps": [("recover_session", lambda b: b.recover_session()),
                         ("navigate_home", lambda b: b.navigate_home())],
        "destructive": True,
    },
    {
        "name": "browser_dead",
        "desc": "杀掉浏览器进程 → ensure_initialized 应能重启并重新登录",
        "break_fn": kill_browser,
        "broken_states": {"no_browser"},
        "repair_steps": [("ensure_initialized",
                          lambda b: {"success": b.ensure_initialized()})],
        "destructive": True,
    },
]


def current_state(browser):
    if not browser.driver:
        return "no_browser"
    return browser.detect_page_state().get("state", "unknown")


def ensure_healthy(browser):
    """场景开始前把浏览器恢复到已验证的健康首页，失败返回 False"""
    if not browser.driver:
        if not browser.ensure_initialized():
            return False
    for _ in range(2):
        v = browser.verify_home_ready()
        if v.get("verified"):
            return True
        browser.close_dialogs()
        browser.navigate_home()
        r = browser.recover_session()
        if not r.get("success"):
            browser.navigate_home()
    return browser.verify_home_ready().get("verified", False)


def run_scenario_a(scenario, browser):
    result = {"scenario": scenario["name"], "part": "A",
              "desc": scenario["desc"], "steps": [], "grade": "FAIL"}
    t0 = time.time()

    # 0. 前置：健康基线
    if not ensure_healthy(browser):
        result["steps"].append("前置健康检查失败，场景跳过")
        result["grade"] = "SKIP"
        return result
    result["steps"].append("基线: verify_home_ready 通过")

    # 1. 破坏
    try:
        broke = scenario["break_fn"](browser)
    except Exception as e:
        broke = False
        result["steps"].append(f"破坏动作异常: {str(e)[:120]}")
    if not broke:
        result["steps"].append("破坏动作未生效，场景无法测试")
        result["grade"] = "SKIP"
        return result

    # 2. 确认破坏真的生效（不生效则本场景无意义）
    state = current_state(browser)
    if scenario["broken_states"] is not None:
        took_effect = state in scenario["broken_states"]
        result["steps"].append(
            f"破坏后 page_state={state} "
            f"(预期 {sorted(scenario['broken_states'])})")
    else:
        v = browser.verify_home_ready()
        took_effect = not v.get("verified")
        result["steps"].append(
            f"破坏后 verify_home_ready={v.get('verified')} "
            f"({v.get('details', '')[:60]})")
    if not took_effect:
        result["steps"].append("⚠ 破坏未生效（平台行为与预期不同），跳过")
        result["grade"] = "SKIP"
        # 尽力恢复现场
        ensure_healthy(browser)
        return result

    # 3. 修复（只用智能体的原子工具）
    for step_name, fn in scenario["repair_steps"]:
        try:
            r = fn(browser)
            ok = r.get("success") if isinstance(r, dict) else bool(r)
            result["steps"].append(f"修复 {step_name} → success={ok}")
        except Exception as e:
            result["steps"].append(f"修复 {step_name} 异常: {str(e)[:120]}")

    # 4. 生产同款硬验收
    v = browser.verify_home_ready()
    result["steps"].append(
        f"验收 verify_home_ready={v.get('verified')} "
        f"state={v.get('state')} ({v.get('details', '')[:60]})")
    result["grade"] = "PASS" if v.get("verified") else "FAIL"
    result["duration_s"] = round(time.time() - t0, 1)
    return result


# ─── Part B: 智能体全链路（真 LLM + 真浏览器 + 临时 DB） ───

def run_agent_e2e(browser):
    """
    破坏: 真实打开上传对话框
    然后伪造一条 upload_submit_timeout 失败记录，让完整的
    _process_one_record 跑真实 LLM 决策 + 真实浏览器修复。
    验收: 记录入队(processing) + 页面真的恢复到已验证首页
    """
    result = {"scenario": "agent_full_loop", "part": "B",
              "desc": "智能体全链路: 真LLM诊断→真浏览器修复→验证→入队",
              "steps": [], "grade": "FAIL"}
    t0 = time.time()

    if not ensure_healthy(browser):
        result["steps"].append("前置健康检查失败")
        result["grade"] = "SKIP"
        return result

    # 用真实当前学校伪造记录，避免触发学校切换
    school_info = browser.get_current_school()
    school = school_info.get("school") or "未知学校"
    result["steps"].append(f"当前学校: {school}")

    # 破坏: 打开上传对话框
    if not open_upload_dialog(browser):
        result["steps"].append("无法打开上传对话框，场景跳过")
        result["grade"] = "SKIP"
        return result
    state = current_state(browser)
    result["steps"].append(f"破坏后 page_state={state}")
    if state != "upload_dialog":
        result["steps"].append("⚠ 破坏未生效，跳过")
        result["grade"] = "SKIP"
        return result

    # 基准事实: 破坏后页面上真实存在的错误（决定智能体的"正确答案"）
    # 某些学校打开上传对话框就显示"该校未开通数智作业服务"等永久性业务错误，
    # 此时智能体拒绝重试、直接转人工才是正确决策
    ground_truth = browser.capture_page_error()
    truth_permanent = ground_truth.get("is_permanent", False)
    if ground_truth.get("has_error"):
        result["steps"].append(
            f"页面真实错误: {ground_truth.get('combined_text', '')[:80]} "
            f"(永久性={truth_permanent})")

    # 临时 DB + 伪造失败记录（文件真实存在）
    DatabaseManager._instance = None
    DatabaseManager._connection = None
    db_dir = tempfile.mkdtemp(prefix="l3agent_")
    db = DatabaseManager(os.path.join(db_dir, f"{uuid.uuid4().hex[:8]}.db"))
    try:
        f = os.path.join(db_dir, "高二数学第99次周末作业.docx")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("l3-test")
        record = insert_failed_record(
            db, f, error_type="upload_submit_timeout",
            fail_stage="submit_upload",
            error_message="上传提交超时：点击确定后60秒无结果反馈",
            school=school)

        llm = RecordingLLM(DeepSeekHelper())
        agent = make_agent(db, browser, llm, config=FakeConfig())
        agent._process_one_record(record, set())

        row = get_record(db, record["id"])
        seq = llm.tool_call_sequence
        result["steps"].append(f"LLM 工具链: {' → '.join(seq) or '(无)'}")
        result["steps"].append(
            f"最终记录状态: retry_status={row['retry_status']} "
            f"retry_count={row['retry_count']}")
        result["llm_calls"] = len(llm.responses)

        enqueued = not agent.task_queue.empty()
        result["steps"].append(f"任务队列入队: {enqueued}")

        v = browser.verify_home_ready()
        result["steps"].append(
            f"页面实际状态: verified={v.get('verified')} "
            f"state={v.get('state')}")

        # 验收（对照基准事实评分）:
        #   页面存在真实永久错误 → 正确答案是识别并转人工，不许入队
        #   页面无永久错误       → 正确答案是修复+验证+入队，且页面真恢复
        if truth_permanent:
            correct_type = row["error_type"] == ground_truth.get(
                "suggested_error_type")
            if (row["retry_status"] == "finished" and not enqueued
                    and correct_type):
                result["grade"] = "PASS"
                result["steps"].append(
                    "✓ 页面存在真实永久性业务错误，智能体正确识别并转人工"
                    "（拒绝无意义重试）")
            else:
                result["grade"] = "FAIL"
                result["steps"].append(
                    f"✗ 永久错误场景下决策不当: finished期望/实际 "
                    f"{row['retry_status']}, 入队={enqueued}, "
                    f"error_type={row['error_type']}")
        elif enqueued and row["retry_status"] == "processing" \
                and v.get("verified"):
            result["grade"] = "PASS"
        elif v.get("verified"):
            result["grade"] = "PARTIAL"
            result["steps"].append("页面已修复但未入队（决策保守）")
        else:
            result["grade"] = "FAIL"
    finally:
        try:
            db.close()
        except Exception:
            pass
        DatabaseManager._instance = None
        DatabaseManager._connection = None
        # 清理现场，避免残留对话框影响后续场景
        ensure_healthy(browser)

    result["duration_s"] = round(time.time() - t0, 1)
    return result


# ─── 报告 ───

def render_report(results, started_at):
    lines = ["# L3 端到端故障注入报告", "",
             f"- 时间: {started_at}",
             f"- 平台: 真实浏览器 + 真实平台（未提交任何上传）", ""]
    n = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        n[r["grade"]] = n.get(r["grade"], 0) + 1
    lines += ["| PASS | PARTIAL | FAIL | SKIP |", "|---|---|---|---|",
              f"| {n['PASS']} | {n['PARTIAL']} | {n['FAIL']} | {n['SKIP']} |",
              "", "## 场景明细", ""]
    for r in results:
        lines.append(f"### [{r['grade']}] {r['scenario']} (Part {r['part']})")
        lines.append(f"{r['desc']}")
        if r.get("duration_s"):
            lines.append(f"- 耗时: {r['duration_s']}s")
        if r.get("llm_calls"):
            lines.append(f"- LLM 调用: {r['llm_calls']} 次")
        for s in r["steps"]:
            lines.append(f"- {s}")
        lines.append("")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="L3 端到端故障注入")
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--skip-agent", action="store_true",
                        help="跳过 Part B 智能体全链路（不调 LLM）")
    parser.add_argument("--skip-destructive", action="store_true",
                        help="跳过会话丢失/杀浏览器等重破坏场景")
    args = parser.parse_args()

    scenarios = [s for s in SCENARIOS
                 if (not args.only or s["name"] in args.only)
                 and not (args.skip_destructive and s.get("destructive"))]

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"L3 故障注入开始: PartA {len(scenarios)} 场景"
          f"{'' if args.skip_agent else ' + PartB 智能体全链路'}")
    print("正在初始化真实浏览器（含登录，约1分钟）...")

    browser = BrowserAutomation()
    if not browser.ensure_initialized():
        print("浏览器初始化失败，无法运行 L3")
        sys.exit(1)

    results = []
    try:
        # Part B 放在破坏性场景之前跑（需要健康会话）
        non_destructive = [s for s in scenarios if not s.get("destructive")]
        destructive = [s for s in scenarios if s.get("destructive")]

        for s in non_destructive:
            print(f"\n▶ [A] {s['name']}: {s['desc']}")
            r = run_scenario_a(s, browser)
            results.append(r)
            print(f"  评级={r['grade']}")
            for step in r["steps"]:
                print(f"    {step}")

        if not args.skip_agent:
            print("\n▶ [B] agent_full_loop: 智能体全链路（真 LLM）")
            r = run_agent_e2e(browser)
            results.append(r)
            print(f"  评级={r['grade']}")
            for step in r["steps"]:
                print(f"    {step}")

        for s in destructive:
            print(f"\n▶ [A] {s['name']}: {s['desc']}")
            r = run_scenario_a(s, browser)
            results.append(r)
            print(f"  评级={r['grade']}")
            for step in r["steps"]:
                print(f"    {step}")
    finally:
        try:
            browser.close()
            print("\n浏览器已关闭")
        except Exception:
            pass

    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    md_path = os.path.join(REPORTS_DIR, f"l3_e2e_{stamp}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_report(results, started_at))
    with open(os.path.join(REPORTS_DIR, f"l3_e2e_{stamp}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=str)

    n_fail = sum(1 for r in results if r["grade"] == "FAIL")
    print(f"\n{'=' * 60}")
    print("  ".join(f"{g}={sum(1 for r in results if r['grade'] == g)}"
                    for g in ("PASS", "PARTIAL", "FAIL", "SKIP")))
    print(f"报告: {md_path}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
