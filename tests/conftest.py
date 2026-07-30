"""
L1 单元测试基座
提供三大 Mock 组件 + Agent 工厂，全程不启动真实浏览器、不调用真实 LLM：
  - ScriptedLLM   : 脚本化 LLM，按预设顺序返回 tool_calls，可模拟"不听话的 LLM"
  - FakeBrowser   : 脚本化浏览器，detect_page_state/verify_home_ready 可按队列返回预设值
  - fresh_db      : 每个测试独立的临时 SQLite 库（重置 DatabaseManager 单例）
  - make_agent    : 绕过 AutoRetryAgent.__init__ 的工厂，手工注入全部依赖
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime
from queue import Queue

import pytest

# 项目根目录加入 sys.path（tests/ 与项目根同级导入）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from db_manager import DatabaseManager  # noqa: E402


# ─── ScriptedLLM：脚本化 LLM ───

def call(name, **kwargs):
    """构造一条 tool_call 脚本项"""
    return ("call", [(name, kwargs)])


def calls(*items):
    """构造同一轮多个 tool_calls 的脚本项，items 为 (name, kwargs) 元组"""
    return ("call", list(items))


def final(obj):
    """构造最终回复脚本项（dict 自动转 JSON 字符串）"""
    return ("final", obj)


class ScriptedLLM:
    """
    按脚本顺序返回消息的假 LLM，接口与 DeepSeekHelper.chat_messages_raw 一致。
    脚本项:
      ("call", [(tool_name, kwargs), ...])  → 返回带 tool_calls 的助手消息
      ("final", dict|str)                   → 返回纯文本最终回复
      ("none", None)                        → 返回 None 模拟 API 调用失败
      ("raw", message_dict)                 → 原样返回自定义消息（测畸形数据）
    脚本耗尽后默认返回 skip 决策，避免测试死循环。
    """

    def __init__(self, script=None, api_key="test-key"):
        self.script = list(script or [])
        self.api_key = api_key
        self.requests = []          # 每次调用的入参快照（供断言 prompt 内容）
        self._call_seq = 0

    def chat_messages_raw(self, messages, tools=None):
        self.requests.append({"messages": list(messages), "tools": tools})
        if not self.script:
            return {"content": json.dumps(
                {"action": "skip", "reason": "script exhausted"}),
                "tool_calls": []}
        kind, payload = self.script.pop(0)
        if kind == "none":
            return None
        if kind == "raw":
            return payload
        if kind == "final":
            content = payload if isinstance(payload, str) \
                else json.dumps(payload, ensure_ascii=False)
            return {"content": content, "tool_calls": []}
        # kind == "call"
        tool_calls = []
        for name, kwargs in payload:
            self._call_seq += 1
            tool_calls.append({
                "id": f"call_{self._call_seq}",
                "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(kwargs, ensure_ascii=False)},
            })
        return {"content": "", "tool_calls": tool_calls}


# ─── FakeBrowser：脚本化浏览器 ───

class FakeBrowser:
    """
    模拟 BrowserAutomation 中被 AutoRetryAgent 使用的全部接口。
    detect_page_state / verify_home_ready 支持队列脚本（依次弹出，耗尽后用默认值）。
    所有方法调用记入 self.calls 供断言。
    """

    def __init__(self):
        self.driver = object()              # 非 None = 浏览器进程存在
        self.is_logged_in = True
        self.is_initialized = True
        self.current_url = "https://platform.example/jobManager"

        self.page_states = []               # detect_page_state 脚本队列
        self.default_state = "home"
        self.verify_results = []            # verify_home_ready 脚本队列
        self.default_verify = {"verified": True, "state": "home", "details": ""}
        self.capture_result = {"success": True, "has_error": False, "errors": [],
                               "combined_text": "", "is_permanent": False,
                               "page_state": "home"}
        self.ensure_init_ok = True          # ensure_initialized 是否成功
        self.login_ok = True
        self.school_switch_ok = True
        self.session_lost_on_page = False

        self.calls = []                     # 方法调用轨迹
        self.verify_calls = 0
        self.restart_cycles = 0             # close + ensure_initialized 完整周期数

    # — 只读诊断 —

    def detect_page_state(self):
        self.calls.append("detect_page_state")
        state = self.page_states.pop(0) if self.page_states else self.default_state
        return {"success": True, "state": state, "details": f"fake:{state}"}

    def capture_page_error(self):
        self.calls.append("capture_page_error")
        return dict(self.capture_result)

    def verify_home_ready(self):
        self.calls.append("verify_home_ready")
        self.verify_calls += 1
        result = self.verify_results.pop(0) if self.verify_results \
            else dict(self.default_verify)
        return dict(result)

    def get_current_school(self):
        self.calls.append("get_current_school")
        return {"success": True, "school": "测试中学"}

    def get_interactable_elements(self):
        self.calls.append("get_interactable_elements")
        return {"success": True, "buttons": [], "dialogs": [], "masks": []}

    def get_screenshot_base64(self):
        self.calls.append("get_screenshot_base64")
        return {"success": True, "base64": "", "mime": "image/jpeg", "path": ""}

    def _detect_session_lost_on_page(self):
        self.calls.append("_detect_session_lost_on_page")
        return self.session_lost_on_page

    # — 原子修复动作 —

    def close_dialogs(self):
        self.calls.append("close_dialogs")
        return {"success": True, "closed": 1}

    def press_escape(self, times=1):
        self.calls.append(f"press_escape({times})")
        return {"success": True, "times": times}

    def refresh_page(self):
        self.calls.append("refresh_page")
        return {"success": True}

    def navigate_home(self):
        self.calls.append("navigate_home")
        return {"success": True}

    def reset_to_home(self):
        self.calls.append("reset_to_home")
        return True

    def recover_session(self):
        self.calls.append("recover_session")
        return {"success": True, "action_taken": "noop"}

    # — 登录/重启 —

    def _login(self):
        self.calls.append("_login")
        return self.login_ok

    def _handle_role_selection(self):
        self.calls.append("_handle_role_selection")
        return self.login_ok

    def close(self):
        self.calls.append("close")
        self.driver = None
        self.is_logged_in = False

    def ensure_initialized(self):
        self.calls.append("ensure_initialized")
        if self.ensure_init_ok:
            self.driver = object()
            self.is_logged_in = True
            self.is_initialized = True
            self.restart_cycles += 1
            return True
        return False

    def check_and_switch_school(self, school):
        self.calls.append(f"check_and_switch_school({school})")
        return self.school_switch_ok

    def update_activity_time(self):
        self.calls.append("update_activity_time")


# ─── FakeConfig ───

class FakeConfig:
    """模拟 ConfigManager 被 Agent 用到的属性/方法"""

    def __init__(self, max_retry_count=3, ai_enable=True, **values):
        self.max_retry_count = max_retry_count
        self.ai_retry_agent_enable = ai_enable
        self.values = {"AI_AGENT_MAX_STEPS": 10}
        self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


# ─── 数据库 fixture ───

@pytest.fixture
def fresh_db(tmp_path):
    """每个测试独立的临时数据库（重置单例，测试后关闭连接）"""
    DatabaseManager._instance = None
    DatabaseManager._connection = None
    db = DatabaseManager(str(tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"))
    yield db
    try:
        db.close()
    except Exception:
        pass
    DatabaseManager._instance = None
    DatabaseManager._connection = None


def insert_failed_record(db, file_path, file_name=None,
                         fail_stage="submit_upload",
                         error_type="upload_submit_timeout",
                         error_category="browser_error",
                         retry_count=0,
                         error_message="上传提交超时",
                         school="测试中学", grade="高二", subject="数学"):
    """插入一条失败记录并返回与 get_pending_failed_records 同构的字典"""
    cursor = db._connection.cursor()
    cursor.execute('''
        INSERT INTO upload_records
        (file_name, file_path, folder_name, school, grade, subject, status,
         error_message, retry_count, fail_stage, error_category, error_type,
         retry_status, upload_time)
        VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, ?, 'pending', ?)
    ''', (file_name or os.path.basename(file_path), file_path,
          f"{school}{grade}", school, grade, subject,
          error_message, retry_count, fail_stage, error_category, error_type,
          datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    db._connection.commit()
    record_id = cursor.lastrowid
    cursor.execute('SELECT * FROM upload_records WHERE id = ?', (record_id,))
    return dict(cursor.fetchone())


def get_record(db, record_id):
    """读取记录当前状态"""
    cursor = db._connection.cursor()
    cursor.execute('SELECT * FROM upload_records WHERE id = ?', (record_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def get_experiences(db):
    """读取全部经验记录"""
    cursor = db._connection.cursor()
    cursor.execute('SELECT * FROM repair_experiences ORDER BY id')
    return [dict(row) for row in cursor.fetchall()]


# ─── Agent 工厂 ───

def make_agent(db, browser=None, llm=None, config=None):
    """
    绕过 __init__ 构造 AutoRetryAgent（避免真实 ConfigManager/DeepSeek/浏览器单例），
    手工注入全部运行属性，与 __init__ 的字段一一对应。
    """
    from auto_retry_agent import AutoRetryAgent, CircuitBreaker
    from experience_memory import ExperienceMemory
    from error_types import ErrorType

    agent = AutoRetryAgent.__new__(AutoRetryAgent)
    agent.task_queue = Queue()
    agent.stop_event = threading.Event()
    agent.log_queue = Queue()
    agent.db = db
    agent.config = config or FakeConfig()
    agent.deepseek = llm or ScriptedLLM()
    agent.vision_llm = None
    agent.browser = browser or FakeBrowser()
    agent.enabled = True
    agent.scan_interval = 1
    agent.backoff_seconds = [0, 0, 0]       # 测试中不等待退避
    agent.circuit_breaker = CircuitBreaker()
    agent._in_retry = set()
    agent._in_retry_lock = threading.Lock()
    agent._retry_error_types = {}
    agent._retry_error_lock = threading.Lock()
    agent.experience = ExperienceMemory(db)
    agent._retry_experience_ids = {}
    agent.agent_busy = threading.Event()
    agent.wake_event = threading.Event()
    agent.upload_processor = None
    agent._TRANSIENT_ERROR_TYPES = {
        ErrorType.UPLOAD_SUBMIT_TIMEOUT.value,
        ErrorType.FORM_VALIDATE_FAIL.value,
        ErrorType.ELEMENT_TIMEOUT.value,
        ErrorType.PAGE_LOAD_TIMEOUT.value,
        ErrorType.SCHOOL_SWITCH_FAIL.value,
        ErrorType.LOGIN_EXPIRED.value,
        ErrorType.NETWORK_ERROR.value,
        ErrorType.BROWSER_START_FAIL.value,
    }
    return agent


def drain_logs(agent):
    """取出 Agent 全部日志文本（供断言日志内容）"""
    logs = []
    while not agent.log_queue.empty():
        logs.append(agent.log_queue.get_nowait())
    return logs


def tool_observations(history, tool_name):
    """从 ReAct 对话历史中提取某工具的全部 observation 文本"""
    return [msg.get("content", "") for msg in history
            if msg.get("role") == "tool" and msg.get("name") == tool_name]
