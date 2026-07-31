"""
运行日志模块 (RunLogger)
功能:
  1. 创建 logs/ 文件夹, 将软件运行时经过 log_queue 的所有日志按天写入文件 (run_YYYYMMDD.log)
  2. 对错误类日志进行初步收集: 识别错误消息 → 自动分类(ErrorCategory/ErrorType) → 写入结构化文件 (errors_YYYYMMDD.jsonl)
  3. 捕获未处理的线程异常/主线程异常/tkinter GUI回调异常, 记入错误收集
  4. 接管 stdout/stderr: 各模块仅 print 的错误也能落盘并被收集
  5. 提供按时间段读取与聚合错误的接口, 供 FailureAnalysisAgent 生成报告时由 LLM 归纳分析
特点: 零侵入接入 — 用 RunLogQueue 替换 main.py 中的 log_queue 即可, 各模块无需改动
"""
import os
import re
import sys
import json
import glob
import threading
import traceback
from queue import Queue
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from error_types import classify_error

# 日志目录（与脚本同级）
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# 日志文件保留天数
LOG_RETENTION_DAYS = 30

# 错误消息识别关键词（初步收集的判定依据）
_ERROR_MARKERS = (
    "错误", "失败", "异常", "超时", "✗", "❌", "⚠",
    "error", "exception", "traceback", "timeout",
)

# GUI 控制指令消息，不属于运行日志
_CONTROL_PREFIXES = ("REFRESH_FAILED_LIST", "BROWSER_STATUS:")

# 文件写入锁（log_queue 会被多线程并发 put）
_write_lock = threading.Lock()

# 钩子重入标记: 异常钩子已结构化记录后, 避免控制台回显的 traceback 被重复收集
_hook_local = threading.local()


# ─── 内部写入 ───

def _write_run_log(message: str):
    """追加一行到当天的运行日志文件"""
    now = datetime.now()
    filepath = os.path.join(LOGS_DIR, f"run_{now.strftime('%Y%m%d')}.log")
    line = f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with _write_lock:
        with open(filepath, 'a', encoding='utf-8') as f:
            f.write(line)


def _looks_like_error(message: str) -> bool:
    """判断日志消息是否为错误类消息"""
    msg_lower = message.lower()
    return any(marker in msg_lower for marker in _ERROR_MARKERS)


def record_error(message: str, source: str = "", tb: str = ""):
    """
    错误初步收集: 自动分类后写入当天的结构化错误文件 errors_YYYYMMDD.jsonl

    Args:
        message: 错误消息文本
        source: 错误来源（模块/线程名，可选）
        tb: 异常堆栈文本（可选）
    """
    try:
        now = datetime.now()
        category, error_type = classify_error(message)
        entry = {
            "time": now.strftime('%Y-%m-%d %H:%M:%S'),
            "source": source,
            "message": message[:500],
            "category": category.value if hasattr(category, 'value') else str(category),
            "error_type": error_type.value if hasattr(error_type, 'value') else str(error_type),
        }
        if tb:
            entry["traceback"] = tb[:2000]
        filepath = os.path.join(LOGS_DIR, f"errors_{now.strftime('%Y%m%d')}.jsonl")
        with _write_lock:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        # 不用 print: stdout 可能已被 Tee 接管, 避免递归
        try:
            if sys.__stderr__:
                sys.__stderr__.write(f"记录错误日志失败: {e}\n")
        except Exception:
            pass


# ─── 日志队列（替换 main.py 中的 Queue 实例） ───

class RunLogQueue(Queue):
    """
    带落盘能力的日志队列
    put 时先写运行日志文件 + 错误初步收集, 再入队供 GUI 消费
    """

    def put(self, item, block=True, timeout=None):
        try:
            message = str(item)
            # 控制指令不写文件
            if not message.startswith(_CONTROL_PREFIXES):
                _write_run_log(message)
                if _looks_like_error(message):
                    record_error(message, source=threading.current_thread().name)
        except Exception as e:
            print(f"写入运行日志失败: {e}")
        super().put(item, block=block, timeout=timeout)


# ─── 初始化 ───

def init_run_logger() -> RunLogQueue:
    """
    初始化运行日志系统:
      创建 logs/ 目录 → 清理过期日志 → 安装未捕获异常钩子 → 接管 stdout/stderr → 返回落盘日志队列
    """
    os.makedirs(LOGS_DIR, exist_ok=True)
    _cleanup_old_logs()
    _install_exception_hooks()
    _install_console_tee()

    queue = RunLogQueue()
    queue.put(f"运行日志已启用, 日志目录: {LOGS_DIR}")
    return queue


def _cleanup_old_logs():
    """删除超过保留期的日志文件"""
    cutoff = (datetime.now() - timedelta(days=LOG_RETENTION_DAYS)).strftime('%Y%m%d')
    for path in glob.glob(os.path.join(LOGS_DIR, "*_*.log")) + \
                glob.glob(os.path.join(LOGS_DIR, "*_*.jsonl")):
        match = re.search(r'_(\d{8})\.(log|jsonl)$', os.path.basename(path))
        if match and match.group(1) < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass


def _install_exception_hooks():
    """安装线程/主线程未捕获异常钩子, 异常统一进入错误收集"""
    original_thread_hook = threading.excepthook

    def _thread_hook(args):
        tb = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        thread_name = args.thread.name if args.thread else "unknown"
        record_error(f"线程未捕获异常: {args.exc_value}",
                     source=thread_name, tb=tb)
        _write_run_log(f"线程 {thread_name} 未捕获异常: {args.exc_value}")
        # 原钩子会向 stderr 回显 traceback, 标记期间 Tee 不重复收集
        _hook_local.suppress = True
        try:
            original_thread_hook(args)
        finally:
            _hook_local.suppress = False

    threading.excepthook = _thread_hook

    original_sys_hook = sys.excepthook

    def _sys_hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        record_error(f"主线程未捕获异常: {exc_value}", source="MainThread", tb=tb)
        _hook_local.suppress = True
        try:
            original_sys_hook(exc_type, exc_value, exc_tb)
        finally:
            _hook_local.suppress = False

    sys.excepthook = _sys_hook


def install_tk_exception_hook(root):
    """
    安装 tkinter GUI 回调异常钩子
    tkinter 会拦截事件回调中的异常(不触发 sys.excepthook), 需单独接管 report_callback_exception

    Args:
        root: tkinter 根窗口
    """
    def _tk_hook(exc_type, exc_value, exc_tb):
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        record_error(f"GUI回调异常: {exc_value}", source="Tk-GUI", tb=tb)
        _write_run_log(f"GUI回调异常: {exc_value}")
        # 回显到原始 stderr(绕过 Tee, 避免重复收集)
        try:
            if sys.__stderr__:
                sys.__stderr__.write(tb)
        except Exception:
            pass

    root.report_callback_exception = _tk_hook


class _StreamTee:
    """
    stdout/stderr 代理: 原样输出到控制台, 同时按行写入运行日志并收集错误行
    用于捕获各模块仅通过 print/traceback.print_exc 输出的错误
    """

    def __init__(self, stream, tag: str):
        self._stream = stream
        self._tag = tag
        self._buffer = ""

    def write(self, text):
        try:
            if self._stream:
                self._stream.write(text)
        except Exception:
            pass
        try:
            self._buffer += str(text)
            while '\n' in self._buffer:
                line, self._buffer = self._buffer.split('\n', 1)
                stripped = line.rstrip()
                if not stripped:
                    continue
                _write_run_log(f"[{self._tag}] {stripped}")
                # traceback 的头部/缩进行不单独收集, 只收末尾的异常行, 避免一个堆栈产生多条错误
                if line[:1].isspace() or stripped.startswith('Traceback ('):
                    continue
                if _looks_like_error(stripped) and not getattr(_hook_local, 'suppress', False):
                    record_error(stripped, source=self._tag)
        except Exception:
            pass

    def flush(self):
        try:
            if self._stream:
                self._stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _install_console_tee():
    """接管 stdout/stderr(幂等): 控制台输出同步落盘, 错误行进入错误收集"""
    # 打包成无控制台窗口的 exe 时 sys.stdout/stderr 可能为 None, Tee 内部已兼容
    if not isinstance(sys.stdout, _StreamTee):
        sys.stdout = _StreamTee(sys.stdout, "stdout")
    if not isinstance(sys.stderr, _StreamTee):
        sys.stderr = _StreamTee(sys.stderr, "stderr")


# ─── 错误读取与聚合（供分析报告使用） ───

def collect_errors(start_time: str, end_time: str) -> List[Dict]:
    """
    读取时间段内收集到的所有运行错误

    Args:
        start_time: 'YYYY-MM-DD HH:MM:SS'
        end_time: 'YYYY-MM-DD HH:MM:SS'

    Returns:
        错误条目列表（按时间升序）
    """
    try:
        start_dt = datetime.strptime(start_time[:10], '%Y-%m-%d')
        end_dt = datetime.strptime(end_time[:10], '%Y-%m-%d')
    except ValueError:
        return []

    entries = []
    day = start_dt
    while day <= end_dt:
        filepath = os.path.join(LOGS_DIR, f"errors_{day.strftime('%Y%m%d')}.jsonl")
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if start_time <= entry.get('time', '') <= end_time:
                            entries.append(entry)
            except OSError:
                pass
        day += timedelta(days=1)

    entries.sort(key=lambda e: e.get('time', ''))
    return entries


def _normalize_message(message: str) -> str:
    """归一化错误消息, 用于把同类错误聚成一组（去掉文件名/路径/数字等变量部分）"""
    text = message
    text = re.sub(r'[A-Za-z]:\\[^\s|]+', '<路径>', text)          # Windows 路径
    text = re.sub(r'\S+\.(docx|doc|pdf|xlsx|xls|png|jpg|jpeg|txt)\b',
                  '<文件>', text, flags=re.IGNORECASE)             # 文件名
    text = re.sub(r'[0-9a-f]{16,}', '<ID>', text, flags=re.IGNORECASE)  # 哈希/ID
    text = re.sub(r'\d+', '#', text)                               # 数字
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:120]


def aggregate_errors(entries: List[Dict], top_n: int = 15) -> List[Dict]:
    """
    对错误条目做初步聚合: 同类消息归为一组, 按出现次数降序

    Returns:
        [{pattern, count, category, error_type, first_time, last_time, sample}, ...]
    """
    groups: Dict[str, Dict] = {}
    for e in entries:
        key = _normalize_message(e.get('message', ''))
        g = groups.get(key)
        if g is None:
            groups[key] = {
                "pattern": key,
                "count": 1,
                "category": e.get('category', 'unknown'),
                "error_type": e.get('error_type', 'unknown'),
                "first_time": e.get('time', ''),
                "last_time": e.get('time', ''),
                "sample": e.get('message', '')[:200],
                "has_traceback": bool(e.get('traceback')),
            }
        else:
            g["count"] += 1
            g["last_time"] = e.get('time', '')
            g["has_traceback"] = g["has_traceback"] or bool(e.get('traceback'))

    result = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
    return result[:top_n]


def get_logs_dir() -> str:
    """返回日志目录绝对路径"""
    return LOGS_DIR
