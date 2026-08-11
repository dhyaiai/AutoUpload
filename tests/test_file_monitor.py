"""
file_monitor 单元测试
覆盖: 新文件入队 / 编辑器保存重建跳过(墓碑窗口) / 删除后同路径重建上传 /
      稳定等待期被删不污染状态 / 过滤规则
"""
import threading
from queue import Queue

import pytest

import file_monitor
from file_monitor import FileMonitorHandler, RECREATE_SUPPRESS_SECONDS


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """构造 handler:监控根目录指向临时目录,禁用文件稳定等待"""
    monkeypatch.setattr(file_monitor.time, "sleep", lambda s: None)
    root = tmp_path / "upload"
    monkeypatch.setattr(
        file_monitor.ConfigManager, "root_dir",
        property(lambda self: str(root))
    )
    q = Queue()
    h = FileMonitorHandler(q, threading.Event())
    return h, q


def make_event(path):
    return type("Event", (), {"src_path": str(path), "is_directory": False})


def test_new_file_enqueued(tmp_path, handler):
    """全新文件 on_created → 入队"""
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "作业.pdf"
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    assert q.get_nowait() == str(f)


def test_recreate_within_window_skipped(tmp_path, handler):
    """删除后短窗口内同路径重建(编辑器保存=删旧写新)→ 跳过不入队"""
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "作业.pdf"
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    assert not q.empty()
    q.get()
    # 编辑器保存: on_deleted 后紧接着 on_created
    h.on_deleted(make_event(f))
    f.write_bytes(b"y")
    h.on_created(make_event(f))
    assert q.empty()


def test_recreate_after_window_enqueued(tmp_path, handler, monkeypatch):
    """删除超过抑制窗口后同路径重建(真正的新文件)→ 照常入队, 不被永久压制。

    回归旧 _known_paths 缺陷: 集合只增不减, 删除后同路径重建会被
    永久跳过, 教师"删上周作业、放同名新作业"的流程直接静默失效。
    """
    base = 1000000.0
    clock = {"t": base}
    monkeypatch.setattr(file_monitor.time, "time", lambda: clock["t"])
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "作业.pdf"
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    q.get()
    h.on_deleted(make_event(f))          # 墓碑记录于 base
    # 时间推进超过抑制窗口
    clock["t"] = base + RECREATE_SUPPRESS_SECONDS + 1
    f.write_bytes(b"new content")
    h.on_created(make_event(f))
    assert q.get_nowait() == str(f)


def test_unknown_time_before_deleted_enqueued(tmp_path, handler):
    """无墓碑时直接 created(程序启动后首次放置)→ 照常入队"""
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "作业.pdf"
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    assert q.get_nowait() == str(f)


def test_deleted_during_wait_not_poisoned(tmp_path, handler):
    """稳定等待期间文件被删: 跳过且不污染状态, 之后同路径真新文件照常入队。

    旧 _known_paths 在等待前就把路径加进集合, 等待期被删的文件路径
    被永久记入"已见", 同路径重建永远无法上传。
    """
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "作业.pdf"
    # on_created 进入 2 秒稳定等待, 但文件从未存在 → 跳过(不写墓碑)
    h.on_created(make_event(f))
    assert q.empty()
    # 同路径创建真新文件: 必须入队
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    assert q.get_nowait() == str(f)


def test_tombstone_expires_naturally(tmp_path, handler, monkeypatch):
    """墓碑过期后 on_created 会清理旧墓碑, 再次删除+重建行为正确"""
    base = 1000000.0
    clock = {"t": base}
    monkeypatch.setattr(file_monitor.time, "time", lambda: clock["t"])
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "作业.pdf"
    h.on_deleted(make_event(f))          # 墓碑记录于 base
    clock["t"] = base + RECREATE_SUPPRESS_SECONDS + 1
    f.write_bytes(b"x")
    h.on_created(make_event(f))   # 超窗: 入队且清理墓碑
    assert q.get_nowait() == str(f)
    assert h._normalize(f) not in h._tombstones


def test_on_deleted_ignores_directories(tmp_path, handler):
    """目录删除事件不写墓碑"""
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    h.on_deleted(type("Event", (), {"src_path": str(sub), "is_directory": True}))
    assert h._tombstones == {}


def test_suppress_window_constant_sane():
    """抑制窗口必须为正值(窗口=0 会退化成永久压制)"""
    assert RECREATE_SUPPRESS_SECONDS > 0


def test_root_file_still_filtered(tmp_path, handler):
    """根目录直接文件仍被过滤"""
    h, q = handler
    root = tmp_path / "upload"
    root.mkdir(parents=True)
    f = root / "直接文件.pdf"
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    assert q.empty()


def test_unsupported_ext_filtered(tmp_path, handler):
    """不支持的扩展名不入队"""
    h, q = handler
    sub = tmp_path / "upload" / "学校"
    sub.mkdir(parents=True)
    f = sub / "图片.png"
    f.write_bytes(b"x")
    h.on_created(make_event(f))
    assert q.empty()
