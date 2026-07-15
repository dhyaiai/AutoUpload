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
    log_queue = Queue()

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

    # --- 浏览器空闲监控线程 ---
    # 在 API 模式下没有 GUI 主循环管理浏览器生命周期，
    # 需要独立的后台线程检测空闲并自动关闭浏览器，避免资源浪费
    def _browser_idle_monitor():
        browser_error_logged = False
        _pending_retry_logged = False       # 防日志刷屏：pending_retry 提示只输出一次
        _idle_timeout_pending_logged = False
        while not stop_event.is_set():
            stop_event.wait(5)
            if stop_event.is_set():
                break

            if not browser.is_initialized:
                browser_error_logged = False
                _pending_retry_logged = False
                _idle_timeout_pending_logged = False
                continue

            # 队列为空 + 无正在处理的任务 + 空闲超过阈值 → 关闭浏览器
            # 阈值由 config.json 的 UPLOAD_IDLE_TIMEOUT 控制（默认1800秒=30分钟）
            if (task_queue.empty() and not upload_processor.processing
                    and browser.is_idle_for(config.upload_idle_timeout)):
                pending_retry = db.count_pending_retry_records()
                if pending_retry > 0:
                    if not _pending_retry_logged:
                        log_queue.put(f"队列空闲但有{pending_retry}条待重试记录,"
                                      f"保持浏览器运行等待Agent处理")
                        _pending_retry_logged = True
                    # 不重置 last_active_time —— Agent 自己干活时会更新；
                    # Agent 不干活说明无法处理，交给 is_idle_timeout 兜底关闭
                else:
                    log_queue.put("上传完成,队列为空,正在关闭浏览器...")
                    browser.close()
                    browser_error_logged = False
                    _pending_retry_logged = False
                continue
            else:
                _pending_retry_logged = False

            # 浏览器空闲兜底超时（默认30分钟，由 BROWSER_IDLE_TIMEOUT 控制）
            # 有待重试记录时不关闭——Agent 还在工作中，关闭后反而要重新初始化
            if not upload_processor.processing and browser.is_idle_timeout():
                pending_retry = db.count_pending_retry_records()
                if pending_retry > 0:
                    if not _idle_timeout_pending_logged:
                        log_queue.put(f"浏览器已空闲{browser.config.browser_idle_timeout}秒,"
                                      f"但仍有{pending_retry}条待重试记录,继续等待Agent处理")
                        _idle_timeout_pending_logged = True
                else:
                    log_queue.put("检测到浏览器空闲超时,正在关闭...")
                    browser.close()
                    browser_error_logged = False
                    _pending_retry_logged = False
                    _idle_timeout_pending_logged = False

            # 浏览器崩溃检测
            elif not browser.check_browser_status():
                if upload_processor.processing:
                    if not browser_error_logged:
                        log_queue.put("浏览器已关闭，但上传正在处理中，等待处理完成...")
                        browser_error_logged = True
                    browser.driver = None
                    browser.is_logged_in = False
                elif task_queue.empty():
                    if not browser_error_logged:
                        log_queue.put("浏览器已关闭,队列为空,不重启")
                        browser_error_logged = True
                    browser.driver = None
                    browser.is_logged_in = False
                else:
                    if not browser_error_logged:
                        log_queue.put("警告: 浏览器异常关闭,尝试重启...")
                        browser_error_logged = True
                    if browser.restart_browser():
                        browser_error_logged = False

            # 登录状态检查
            elif not browser.check_login_status():
                if not browser_error_logged:
                    log_queue.put("警告: 登录态失效,尝试重新登录...")
                    browser_error_logged = True
                if browser.restart_browser():
                    browser_error_logged = False

            else:
                browser_error_logged = False

    idle_monitor_thread = threading.Thread(
        target=_browser_idle_monitor, daemon=True, name="BrowserIdleMonitor"
    )
    idle_monitor_thread.start()
    print("浏览器空闲监控已启动")

    print(f"API Server 已启动: http://{config.api_server_host}:{config.api_server_port}")
    print(f"Swagger 文档: http://{config.api_server_host}:{config.api_server_port}/docs")
    print("=" * 50)

    yield  # 服务运行中

    # --- 优雅关闭 ---
    print("API Server 正在关闭...")
    stop_event.set()
    time.sleep(2)

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
    file: UploadFile = File(..., max_size=50 * 1024 * 1024),  # 50MB 上限
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
    original_name = filename or file.filename

    # 保存文件：用 UUID 子目录防碰撞，文件名保持原始名称不变
    file_dir = os.path.join(upload_temp, uuid.uuid4().hex)
    os.makedirs(file_dir, exist_ok=True)
    file_path = os.path.join(file_dir, original_name)
    try:
        content = await file.read()
        with open(file_path, 'wb') as f:
            f.write(content)
    except Exception as e:
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
    task_queue = _app_state.get("task_queue")
    if task_queue is not None:
        task_queue.put(task)
    else:
        return api_response(code=1, msg="服务未就绪，任务队列不可用")

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
    task_queue = _app_state.get("task_queue")
    if task_queue is not None:
        task_queue.put(task)
    else:
        return api_response(code=1, msg="服务未就绪，任务队列不可用")

    return api_response(msg="重试任务已提交", data={"task_id": task_id})


@app.post("/api/report/generate")
async def generate_report(start_date: str, end_date: str):
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
        return api_response(code=1, msg=f"报告生成异常: {e}")


@app.get("/api/stats/overview")
async def get_stats_overview():
    """获取统计概览（总上传量、成功率、今日数据、待重试数等）"""
    db = DatabaseManager()
    try:
        stats = db.get_stats_overview()
        return api_response(data=stats)
    except Exception as e:
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
