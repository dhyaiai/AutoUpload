"""
API Server Module
FastAPI backend for WeChat Mini Program integration.
Shares all singletons (DB, browser, upload processor, agents) with the desktop GUI.

启动方式:
- python main.py --api-only          # 纯 API 模式（无 GUI）
- python api_server.py               # 直接运行
- uvicorn api_server:app --host 0.0.0.0 --port 8000
"""
import os
import time
import threading
import uuid
from datetime import datetime
from queue import Queue
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from db_manager import DatabaseManager
from config_manager import ConfigManager
from upload_processor import UploadProcessor
from browser_automation import BrowserAutomation
from auto_retry_agent import AutoRetryAgent
from failure_analysis_agent import FailureAnalysisAgent
from pipeline_watchdog import PipelineWatchdog


# ==================== 全局状态（供 lifespan 注入） ====================
# 这些变量在 lifespan 启动阶段赋值，在接口函数中通过模块级引用访问
_app_state = {}


# ==================== 辅助函数 ====================

def api_response(code: int = 0, msg: str = "success", data=None) -> dict:
    """统一 API 响应格式"""
    return {"code": code, "msg": msg, "data": data}


# ==================== FastAPI 生命周期 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化所有组件，关闭时优雅退出"""
    print("=" * 50)
    print("API Server 启动中...")
    print("=" * 50)

    # --- 初始化配置和数据库 ---
    config = ConfigManager()
    db = DatabaseManager()
    print(f"数据库已连接: {db.db_path}")

    # --- 创建基础设施 ---
    stop_event = threading.Event()
    task_queue = Queue()
    # 日志队列同时落盘到 logs/ 目录并自动收集错误
    from run_logger import init_run_logger
    log_queue = init_run_logger()

    # --- 创建 upload_temp 目录 ---
    upload_temp = os.path.abspath(config.upload_temp_dir)
    os.makedirs(upload_temp, exist_ok=True)
    print(f"临时文件目录: {upload_temp}")

    # --- 初始化浏览器（延迟：首次上传时才真正启动） ---
    browser = BrowserAutomation(log_queue=log_queue)

    # --- 启动上传处理器线程 ---
    upload_processor = UploadProcessor(task_queue, stop_event, log_queue)
    processor_thread = threading.Thread(
        target=upload_processor.run, daemon=True, name="UploadProcessor"
    )
    processor_thread.start()
    print("上传处理器已启动")

    # --- 启动 AutoRetryAgent 线程 ---
    auto_retry_agent = AutoRetryAgent(task_queue, stop_event, log_queue)
    auto_retry_agent.set_upload_processor(upload_processor)
    upload_processor.set_agent(auto_retry_agent)
    if config.auto_retry_enable:
        retry_thread = threading.Thread(
            target=auto_retry_agent.run, daemon=True, name="AutoRetryAgent"
        )
        retry_thread.start()
        print("自动重试Agent已启动")
    else:
        print("自动重试Agent已禁用")

    # --- 启动流水线看门狗线程（卡死检测） ---
    watchdog = PipelineWatchdog(
        upload_processor.heartbeat, browser, upload_processor,
        auto_retry_agent, log_queue, stop_event)
    watchdog_thread = threading.Thread(
        target=watchdog.run, daemon=True, name="PipelineWatchdog")
    watchdog_thread.start()
    print("流水线看门狗已启动")

    # --- 初始化分析Agent ---
    analysis_agent = FailureAnalysisAgent(log_queue)
    print("失败分析Agent已就绪")

    # --- 注入全局状态 ---
    _app_state["stop_event"] = stop_event
    _app_state["task_queue"] = task_queue
    _app_state["log_queue"] = log_queue
    _app_state["upload_processor"] = upload_processor
    _app_state["auto_retry_agent"] = auto_retry_agent
    _app_state["analysis_agent"] = analysis_agent
    _app_state["config"] = config
    _app_state["upload_temp"] = upload_temp
    _app_state["browser"] = browser
    _app_state["db"] = db

    # --- 浏览器空闲监控线程（使用 BrowserAutomation 共享方法） ---
    browser.start_idle_monitor(
        stop_event, task_queue, log_queue, upload_processor, db, browser=browser)
    print("浏览器空闲监控已启动")

    print(f"API Server 已启动: http://{config.api_server_host}:{config.api_server_port}")
    print(f"Swagger 文档: http://{config.api_server_host}:{config.api_server_port}/docs")
    print("=" * 50)

    yield  # 服务运行中

    # --- 优雅关闭 ---
    print("API Server 正在关闭...")
    _app_state["_shutting_down"] = True
    stop_event.set()

    # 等待任务队列排空（最多等待 upload_idle_timeout 秒）
    drain_timeout = config.upload_idle_timeout
    drain_deadline = time.time() + drain_timeout
    while not task_queue.empty() and time.time() < drain_deadline:
        time.sleep(0.5)
    if not task_queue.empty():
        print(f"警告: 等待超时({drain_timeout}s), 队列中仍有{task_queue.qsize()}个未完成任务")

    try:
        browser.close()
    except Exception:
        pass

    db.close()
    print("API Server 已停止")


# --- 创建 FastAPI 应用 ---
app = FastAPI(
    title="作业自动上传 API",
    description="对接微信小程序的作业上传服务，支持提交任务、查询状态、管理失败记录、统计概览",
    version="2.0",
    lifespan=lifespan,
)

# --- CORS 跨域 ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== API 接口 ====================

@app.post("/api/upload/submit")
async def submit_upload(
    file: UploadFile = File(...),
    school: str = Form(...),
    grade: str = Form(...),
    subject: Optional[str] = Form(None),
    filename: Optional[str] = Form(None)
):
    """
    提交上传任务（小程序端调用）

    - **file**: 上传的文件（doc/docx/pdf）
    - **school**: 学校名称
    - **grade**: 年级
    - **subject**: 科目（可选，不传则由 AI 自动识别）
    - **filename**: 原始文件名（可选，微信小程序 wx.uploadFile 不保留原名，
      需由客户端从 wx.chooseMessageFile 的 tempFiles[].name 获取后单独传入）
    """
    upload_temp = _app_state.get("upload_temp", "./upload_temp")
    os.makedirs(upload_temp, exist_ok=True)

    # 原始文件名：优先使用客户端传入的 filename，回退到 file.filename
    # 微信小程序 wx.uploadFile 会将文件名设为临时路径的 UUID，不含原始名称
    original_name = filename or file.filename or "unknown"

    # 检查任务队列可用性（在写DB之前，避免创建孤儿记录）
    task_queue = _app_state.get("task_queue")
    if task_queue is None:
        return api_response(code=1, msg="服务未就绪，任务队列不可用")

    # 读文件内容并限制大小为 15MB
    MAX_UPLOAD_SIZE = 15 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        return api_response(code=1, msg=f"文件大小超过限制(最大15MB)，当前大小: {len(content) // (1024*1024)}MB")

    # 保存文件：UUID 子目录防碰撞，文件名保持原始名称不变
    import asyncio
    file_dir = os.path.join(upload_temp, uuid.uuid4().hex)
    os.makedirs(file_dir, exist_ok=True)
    file_path = os.path.join(file_dir, original_name)
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: open(file_path, 'wb').write(content))
    except Exception as e:
        print(f"API错误: 小程序上传文件保存失败 {original_name} - {e}")
        return api_response(code=1, msg=f"文件保存失败: {e}")

    # 写入待处理记录
    db = DatabaseManager()
    try:
        record_id = db.add_pending_record(
            file_name=original_name,
            file_path=os.path.abspath(file_path),
            folder_name="miniprogram",
            school=school,
            grade=grade,
            subject=subject or "待识别",
            source='miniprogram'
        )
    except Exception as e:
        print(f"API错误: 小程序上传数据库写入失败 {original_name} - {e}")
        # 删除已保存的文件，避免磁盘残留
        try:
            os.remove(file_path)
        except Exception:
            pass
        return api_response(code=1, msg=f"数据库写入失败: {e}")

    # 入队
    task = {
        "file_path": os.path.abspath(file_path),
        "original_name": original_name,
        "school": school,
        "grade": grade,
        "subject": subject,  # 可能为 None，上传处理器会 AI 识别
        "record_id": record_id,
    }
    task_queue.put(task)

    return api_response(msg="任务已提交", data={"task_id": record_id})


@app.get("/api/upload/status/{task_id}")
async def get_upload_status(task_id: int):
    """
    查询任务状态（小程序轮询用）

    - **task_id**: 提交任务时返回的 task_id
    """
    db = DatabaseManager()
    record = db.get_record_by_id(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="任务不存在")

    return api_response(data={
        "task_id": record["id"],
        "file_name": record["file_name"],
        "status": record["status"],
        "school": record["school"],
        "grade": record["grade"],
        "subject": record["subject"],
        "error": record.get("error_message"),
        "retry_count": record.get("retry_count", 0),
        "fail_stage": record.get("fail_stage"),
        "source": record.get("source", "desktop"),
        "upload_time": record.get("upload_time"),
    })


@app.get("/api/failed/list")
async def get_failed_list(page: int = 1, size: int = 20):
    """
    分页获取失败记录列表

    - **page**: 页码（从1开始，默认1）
    - **size**: 每页条数（默认20）
    """
    db = DatabaseManager()
    records = db.get_failed_records(page=page, size=size)
    total = db.get_failed_count()

    # 格式化输出
    items = []
    for r in records:
        items.append({
            "id": r["id"],
            "file_name": r["file_name"],
            "school": r["school"],
            "grade": r["grade"],
            "subject": r["subject"],
            "status": r["status"],
            "error": r.get("error_message"),
            "retry_count": r.get("retry_count", 0),
            "fail_stage": r.get("fail_stage"),
            "error_type": r.get("error_type"),
            "retry_status": r.get("retry_status"),
            "source": r.get("source", "desktop"),
            "upload_time": r.get("upload_time"),
        })

    return api_response(data={
        "records": items,
        "total": total,
        "page": page,
        "size": size,
    })


@app.post("/api/upload/retry/{task_id}")
async def retry_upload(task_id: int):
    """
    手动重试失败任务

    - **task_id**: 要重试的失败记录ID

    重试任务通过任务队列排队执行，不直接操作浏览器，
    避免与正在处理的上传任务发生浏览器并发冲突。
    """
    db = DatabaseManager()
    record = db.get_record_by_id(task_id)
    if not record:
        raise HTTPException(status_code=404, detail="任务不存在")
    if record["status"] != "failed":
        return api_response(code=1, msg="只能重试状态为 'failed' 的任务")

    file_path = record["file_path"]
    if not os.path.exists(file_path):
        return api_response(code=1, msg=f"文件不存在: {file_path}")

    # 先检查任务队列可用性（在递增重试计数前，避免状态卡死）
    task_queue = _app_state.get("task_queue")
    if task_queue is None:
        return api_response(code=1, msg="服务未就绪，任务队列不可用")

    # 增加重试次数并检查上限
    retry_count = db.increment_retry(task_id)
    config = _app_state.get("config")
    max_retry = config.max_retry_count if config else 10
    if retry_count > max_retry:
        db.update_record_structured_error(
            task_id, error_message=f"已达到最大重试次数({max_retry})",
            fail_stage="submit_upload")
        db.update_retry_status(task_id, 'finished')
        return api_response(code=1, msg=f"已达到最大重试次数({max_retry})")

    # 标记为 processing 防止 Agent 并发重试
    db.update_retry_status(task_id, 'processing')

    # 通过任务队列排队，不直接操作浏览器（避免并发冲突）
    task = {
        "file_path": os.path.abspath(file_path),
        "original_name": record.get("file_name", os.path.basename(file_path)),
        "school": record["school"],
        "grade": record["grade"],
        "subject": record.get("subject"),
        "record_id": task_id,
        "is_retry": True,
    }
    task_queue.put(task)

    return api_response(msg="重试任务已提交", data={"task_id": task_id})


@app.post("/api/report/generate")
async def generate_report(
    start_date: str = Form(...),
    end_date: str = Form(...)
):
    """
    手动生成失败分析报告

    - **start_date**: 起始日期，格式 "YYYY-MM-DD"
    - **end_date**: 结束日期，格式 "YYYY-MM-DD"
    """
    analysis_agent = _app_state.get("analysis_agent")
    if analysis_agent is None:
        return api_response(code=1, msg="分析Agent未就绪")

    try:
        filepath = analysis_agent.generate_report(start_date, end_date, report_type="custom")
        if filepath:
            return api_response(data={"report_path": filepath})
        else:
            return api_response(code=1, msg="报告生成失败")
    except Exception as e:
        print(f"API错误: 报告生成异常 - {e}")
        return api_response(code=1, msg=f"报告生成异常: {e}")


@app.get("/api/stats/overview")
async def get_stats_overview():
    """获取统计概览（总上传量、成功率、今日数据、待重试数等）"""
    db = DatabaseManager()
    try:
        stats = db.get_stats_overview()
        return api_response(data=stats)
    except Exception as e:
        print(f"API错误: 统计查询失败 - {e}")
        return api_response(code=1, msg=f"统计查询失败: {e}")


@app.get("/api/health")
async def health_check():
    """健康检查接口"""
    return api_response(data={
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "queue_size": _app_state.get("task_queue", Queue()).qsize() if _app_state.get("task_queue") else 0,
    })


# ==================== 直接运行入口 ====================

def run_api_server():
    """独立启动 API 服务（由 main.py --api-only 调用）"""
    config = ConfigManager()
    host = config.api_server_host
    port = config.api_server_port

    import uvicorn
    uvicorn.run(
        "api_server:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    run_api_server()
