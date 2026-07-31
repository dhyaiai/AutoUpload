# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

桌面自动化工具：监控文件夹中的学生作业文件 → DeepSeek AI 识别科目 → Selenium 浏览器自动上传到七天网络数智作业平台（zuoye.7net.cc），带 customtkinter 现代化 GUI 管理界面。支持试题+答案文件合并、拖拽上传、系统托盘最小化、数据统计分析、AI Agent 自动诊断自愈失败任务、流水线卡死看门狗、经验记忆（越用越聪明）、FastAPI 后端对接微信小程序。

## 常用命令

```bash
# 运行程序（GUI 桌面模式）
python main.py

# 纯 API 服务模式（无 GUI，微信小程序对接）
python main.py --api-only
# 或 uvicorn api_server:app --host 0.0.0.0 --port 8000

# 安装依赖
pip install -r requirements.txt

# 打包为单文件 exe
pyinstaller build.spec --clean

# 浏览器自动化独立测试（无需启动完整GUI）
python browser_automation.py --file "C:\path\to\file.docx" --school "学校名" --grade "高一" --subject "生物"
python browser_automation.py --file "C:\path\to\file.docx"                # 自动从文件夹名解析学校/年级，AI识别科目
python browser_automation.py --file "C:\path\to\file.docx" --skip-login   # 复用已登录的浏览器会话
```

## 测试体系（三层）

```bash
# L1 单元层：Mock LLM + FakeBrowser，验证闸门/熔断等硬约束逻辑（76 个用例，~8 秒，零成本）
.venv\Scripts\python -m pytest tests -q

# L2 决策层：真实 DeepSeek LLM + 伪造失败记录 + Mock 浏览器，评估 LLM 决策质量（10 场景，~6 分钟，产生 API 费用，不被 pytest 收集）
.venv\Scripts\python tests\l2_decision_eval.py                 # 全部场景
.venv\Scripts\python tests\l2_decision_eval.py --repeat 3      # 每场景重复测稳定性
.venv\Scripts\python tests\l2_decision_eval.py --only transient_timeout dialog_stuck

# L3 端到端层：真实浏览器 + 真实平台 + 主动破坏页面状态，验证修复动作真实有效（6 场景，~7 分钟，需网络+平台账号）
.venv\Scripts\python tests\l3_e2e_faultinject.py               # 全部（Part A 原子修复 + Part B 智能体全链路）
.venv\Scripts\python tests\l3_e2e_faultinject.py --skip-agent  # 只跑 Part A（无 LLM）
```

- L1 测「闸门拦得住吗」（安全性），L2 测「LLM 本来就做得对吗」（决策质量），L3 测「修复动作在真实 DOM/会话上真的生效吗」
- `tests/conftest.py` 提供 L1 基座：`ScriptedLLM`（脚本化 LLM，可模拟"不听话的 LLM"）、`FakeBrowser`、`fresh_db`（临时 SQLite）、`make_agent`（绕过 `__init__` 注入依赖的 Agent 工厂），L2/L3 复用这些组件
- L2/L3 报告输出到 `reports/` 目录（.md + .json）
- L3 安全约束：只打开上传对话框绝不提交、不切换学校、用临时数据库不碰生产 data.db
- 项目没有 linter 或 formatter 配置

## 架构

**分层模块化 + 生产者-消费者模式 + AI Agent 自愈系统**，20 个模块全部在项目根目录（无 `src/` 子目录）。

### 线程模型

```
主线程: tkinter/customtkinter GUI 主循环 (gui_manager.py)
  └─ 后台线程 backend_worker() (main.py)
       ├─ 文件监控线程 watchdog (file_monitor.py)
       ├─ 上传处理线程 (upload_processor.py)
       ├─ Agent 线程 (auto_retry_agent.py)
       ├─ 浏览器空闲监控线程 (BrowserAutomation.start_idle_monitor)
       └─ 流水线看门狗线程 (pipeline_watchdog.py)
```

API-only 模式下由 `api_server.py` 的 FastAPI lifespan 启动同一套组件（共享全部单例），无 GUI 和文件监控。

线程间通过 `Queue` 安全通信：
- `task_queue`：文件路径 → 上传处理器
- `log_queue`：日志消息 → GUI（含特殊指令 `REFRESH_FAILED_LIST`、`BROWSER_STATUS:xxx`）

### 三个单例

`BrowserAutomation`、`DatabaseManager`、`ConfigManager` 均用 `__new__` 实现单例，确保全局只有一个浏览器实例、一个数据库连接、一份配置。

### 核心流程

```
watchdog 检测新文件 → task_queue → UploadProcessor.run()
  → InfoExtractor.parse_folder_name()（正则解析"学校名+年级"）
  → InfoExtractor.read_file_content()（前200字）
  → SubjectClassifier.classify()（DeepSeek API）
  → BrowserAutomation（ensure_initialized → check_and_switch_school → upload_file）
  → DatabaseManager.add_record()
  → AutoRetryAgent.on_upload_result()  ← 通知 Agent 上传结果
```

UploadProcessor 每个阶段调用 `heartbeat.beat(stage, file)` 上报心跳，PipelineWatchdog 按 `WATCHDOG_STAGE_TIMEOUTS` 检测卡死，超时强制打断浏览器并唤醒 Agent。

### AI Agent 自愈系统

程序启动时自动拉起 `AutoRetryAgent` 后台线程，持续扫描失败记录，通过 **ReAct 循环**（Thought → Action → Observation）驱动 LLM 自主诊断并执行自愈策略。

**核心模块：**

| 模块 | 职责 |
|------|------|
| `error_types.py` | 结构化错误体系：`UploadStage`（8个阶段）、`ErrorCategory`（5大类）、`ErrorType`（19种具体错误）、`RetryLevel`（L1-L5 自愈级别）、`STRATEGY_MAP`（阶段×错误→策略映射表）、错误分类推断规则、根因描述与建议 |
| `deepseek_helper.py` | DeepSeek API 通用封装，提供 `chat()` 和 `chat_json()`，供 Agent 模块共用。支持多提供商（DeepSeek/Qwen MaaS），内置重试和超时 |
| `react_loop.py` | 通用 ReAct 循环引擎，零项目依赖。`@tool` 装饰器自动生成工具 Schema。`max_steps` 硬上限防止死循环 |
| `auto_retry_agent.py` | 最大的 Agent 模块（~2100 行）。失败自动接管，后台常驻。含安全守护：熔断器（CircuitBreaker）、全局重试上限、`_validate_react_decision` 硬门禁（LLM 无法绕过）。ReAct 前预检测页面状态并注入 prompt（状态不匹配时提示按当前状态决策）。集成经验记忆。失败列表在 GUI 显示"Agent接管"状态 |
| `experience_memory.py` | 经验记忆：错误指纹（error_type+fail_stage+page_state）→动作序列→是否成功，写入 `repair_experiences` 表。ReAct 前注入同指纹历史成功方案，上传结果回调回填成败。用成功率统计动态修正 `STRATEGY_MAP`（样本≥5且成功率≥0.6覆盖静态映射；静态级别成功率<0.2升一级；L5 永不修正）。底部有 CLI 测试入口 |
| `failure_analysis_agent.py` | 按需触发的失败原因分析 Agent，ReAct 驱动 LLM 自主探索数据库、深度归因、生成 Markdown 分析报告。AI 禁用/失败时回退到模板报告 |
| `pipeline_watchdog.py` | `PipelineHeartbeat`（阶段心跳）+ `RecentLogBuffer`（日志环形缓冲，供 Agent 的 read_recent_logs 工具回看）+ `PipelineWatchdog`（卡死检测线程）。⚠️ 不能改名为 watchdog.py，会遮蔽 PyPI watchdog 包 |

**Agent 工具集（20个）分为四类：**

1. **诊断**（只读）：`capture_page_error`（【必须第一步调用】抓取页面错误并判断是否永久性业务错误）、`detect_page_state`、`check_current_school`、`query_error_history`、`check_circuit_breaker`、`check_file_exists`、`view_page_screenshot`（多模态视觉模型分析截图，用 `QWEN_VL_MODEL`）、`get_page_elements`、`read_recent_logs`
2. **原子修复**：`close_dialog`、`press_escape`、`refresh_page`、`navigate_home`、`re_login`（仅 login/role_select 页可执行）、`restart_browser`（受熔断器硬约束）、`full_recovery`（一键完整恢复流程）
3. **验证**：`verify_recovery`（【修复后必须调用】确认页面回到 home 且关键元素可交互）
4. **终态动作**：`enqueue_retry`（硬闸门：修复后未通过验证自动执行验证，不通过拒绝入队）、`mark_manual_review`、`skip_and_wait`

**自愈策略分级：**
- **L1 轻量重试**：原地重试当前步骤（网络抖动、API超时）
- **L2 页面复位**：关闭弹窗、返回首页（表单校验失败、元素定位超时）
- **L3 环境重置**：刷新页面、重新校验学校（学校切换失败）
- **L4 服务重启**：重启浏览器、重新登录（浏览器崩溃、登录态失效）
- **L5 人工兜底**：标记为待人工处理（科目不存在、权限不足、文件损坏）

**熔断机制：** `CircuitBreaker` 按错误类型追踪失败时间戳，同类型错误超过阈值（默认10次/30分钟）自动熔断，防止大面积故障时无效重试。

### 浏览器延迟初始化

程序启动时只开启文件监控，浏览器不启动。`UploadProcessor` 首次从队列取到文件时才调用 `browser.ensure_initialized()` 启动浏览器。空闲超时自动关闭浏览器（`UPLOAD_IDLE_TIMEOUT`/`BROWSER_IDLE_TIMEOUT`）。`upload_processor.processing` 标记阻止后台在上传进行中误关浏览器。每 `BROWSER_RESTART_INTERVAL` 次上传后定时重启浏览器防内存泄漏。

### 各模块要点

| 模块 | 关键点 |
|------|--------|
| `browser_automation.py` | 最大的模块（2500+ 行），已针对七天网络（Element UI + Vue）完整适配。所有选择器硬编码到该网站，包含多方案降级点击策略（Vue API → JS click → ActionChains → MouseEvent）。提供 Agent 修复动作的底层实现（detect_page_state、verify_home_ready、close_all_dialogs 等）。底部有 CLI 测试入口 |
| `gui_manager.py` | customtkinter 现代化界面（圆角卡片 + 侧边栏导航 + 低饱和配色，样式统一定义在 `ui_theme.py`）。上传管理含文件夹管理、试题答案合并+拖拽、失败列表（含Agent接管状态列）。关闭时最小化到系统托盘（pystray） |
| `ui_theme.py` | UI 设计系统：配色（4主色+中性色）、统一字体（Microsoft YaHei UI）、`create_root()` 创建支持拖拽的 CTk 根窗口 |
| `upload_processor.py` | 单线程顺序消费，`processing` 标记防止后台误关浏览器，每次上传前都校验学校。各阶段上报心跳给看门狗。上传完成后回调 `auto_retry_agent.on_upload_result()` 通知结果 |
| `api_server.py` | FastAPI 后端（微信小程序对接）。lifespan 中初始化全部组件（DB/浏览器/上传处理器/Agent/看门狗）。接口：`/api/upload/submit`（multipart 上传到 `upload_temp/` 后入队）、`/api/upload/status/{id}`、`/api/failed/list`、`/api/upload/retry/{id}`、`/api/report/generate`、`/api/stats/overview`、`/api/health`。统一响应格式 `{code, msg, data}` |
| `info_extractor.py` | 年级正则：`^(.+?)(高一\|高二\|...\|小六)$`，支持 txt/docx/doc/pdf（.doc 通过 olefile 解析 OLE2 二进制格式） |
| `subject_classifier.py` | 调用 `deepseek-chat`，temperature=0，限制 9 个科目，最多重试 3 次间隔 2 秒 |
| `file_monitor.py` | watchdog `on_created` 事件，文件稳定等待 2 秒后入队，过滤根目录下的直接文件 |
| `db_manager.py` | SQLite，`upload_records` + `analysis_records` + `repair_experiences` 三表，`check_same_thread=False`。分析表支持按科目/学校年级/日期聚合查询。Agent 通过工具函数查询/更新失败记录 |
| `config_manager.py` | 处理 PyInstaller 打包路径（`sys._MEIPASS`），`@property` 暴露配置项，自动清理 Unicode 控制字符 |
| `file_merger.py` | 试题+答案合并（试题在前，分页符分隔，答案在后）。.doc/.docx 用 Word COM（支持 MS Word / WPS），.pdf 用 pypdf |
| `stats_panel.py` | 数据统计页：matplotlib 柱状图（按科目/学校年级切换）+ 折线图（按日/周/月聚合）+ 上传/失败记录表 + openpyxl Excel 导出 + 失败分析报告入口 |
| `main.py` | 入口，协调线程启停。`--api-only` 参数切换到纯 API 模式。优雅退出：等队列清空（最多30秒）→ 停监控 → 关浏览器 → 关数据库 |

### Element UI 适配策略（browser_automation.py）

目标网站使用 Element UI（Vue 2），核心难点是自定义组件（el-select、el-dropdown、el-date-picker）无法用标准 Selenium Select 操作。代码采用多方案降级：

1. **打开下拉菜单**：Vue 组件 API（`__vue__.visible = true`）→ JS click() → ActionChains → MouseEvent 序列
2. **选择下拉项**：XPath 定位 `el-select-dropdown__item` → 遍历匹配文本
3. **读取当前选中值**：JS 读取 `__vue__.selected.currentLabel` → 回退读 `el-input__inner.value`
4. **上传结果检测**：轮询提交按钮是否消失（StaleElementReferenceException）→ 检测 toast 消息 → 检测表单校验错误。轮询期间临时禁用 implicit_wait 避免卡顿

## 配置

`config.json` 包含敏感信息（API key、密码），已被 `.gitignore` 排除。首次运行程序会自动生成。PyInstaller 打包时 exe 从同目录读取 config.json 而非从 `_MEIPASS` 读取。

关键配置项：
- `ROOT_DIR`：监控根目录，默认 `C:\Users\Administrator\Desktop\upload`
- `WEBSITE_URL`：`https://zuoye.7net.cc`
- `ROLE`：`"超级管理员"` 或 `"老师"`
- `CHROME_PROFILE_DIR`：Chrome 用户数据目录（留空=临时目录，填写则复用登录态）
- `DEEPSEEK_API_KEY`：DeepSeek API 密钥
- `QWEN_API_KEY` / `QWEN_MODEL` / `QWEN_API_URL`：通义千问 MaaS 专属实例（deepseek_helper 支持多提供商切换）
- `QWEN_VL_MODEL`：截图理解用多模态模型（view_page_screenshot 工具）
- `BROWSER_IDLE_TIMEOUT` / `UPLOAD_IDLE_TIMEOUT`：浏览器空闲关闭超时（各 1800 秒）
- `BROWSER_RESTART_INTERVAL`：每 N 次上传定时重启浏览器（默认 50，0=不重启）
- `MINIMIZE_TO_TRAY`：关闭窗口时最小化到系统托盘（默认 true）
- `UPLOAD_TIMEOUT`：上传轮询超时秒数（browser_automation 读取，默认 120）
- `AUTO_RETRY_ENABLE` / `AUTO_RETRY_SCAN_INTERVAL` / `AUTO_RETRY_BACKOFF_SECONDS`：Agent 开关/扫描间隔（60秒）/指数退避（[30,120,600]）
- `AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD` / `AUTO_RETRY_CIRCUIT_BREAKER_DURATION`：熔断阈值（10次）/熔断时长（1800秒）
- `AI_RETRY_AGENT_ENABLE` / `AI_ANALYSIS_AGENT_ENABLE`：AI 驱动重试/分析开关
- `AI_AGENT_MAX_STEPS`：ReAct 循环最大步数（默认 10）
- `WATCHDOG_ENABLE` / `WATCHDOG_CHECK_INTERVAL` / `WATCHDOG_STAGE_TIMEOUTS`：看门狗开关/检查间隔（10秒）/各阶段卡死阈值（submit_upload 需大于 UPLOAD_TIMEOUT）
- `API_SERVER_HOST` / `API_SERVER_PORT` / `UPLOAD_TEMP_DIR`：API 服务监听地址（0.0.0.0:8000）/小程序上传临时目录

## 数据库

SQLite 文件 `data.db`，三张表：

- `upload_records`：所有上传记录（success/failed/pending_retry）。索引：file_name、status、folder_name。字段含 fail_stage、error_category、error_type、retry_count、agent_retry_count、agent_status 等 Agent 相关列
- `analysis_records`：用户手动点击"复制数据到分析表"后从 upload_records 快照而来，用于统计图表。索引：subject、school、grade、upload_time
- `repair_experiences`：经验记忆表，以 `error_type|fail_stage|page_state` 为指纹记录每次处置的动作序列与结果，用于历史成功方案注入和 STRATEGY_MAP 动态修正

## 文件合并功能

GUI 的"合并文件"区域支持将试题文件和答案文件合并为一个文件（试题在前，分页符分隔，答案在后），合并后的文件自动加入上传队列。要求试题和答案格式一致（同为 .doc/.docx 或同为 .pdf）。Word 合并通过 win32com 调用本机 MS Word 或 WPS，PDF 合并通过 pypdf。

## 打包注意事项

- `build.spec` 是当前唯一使用的 PyInstaller 配置，`console=False`（窗口模式）。旧版遗留的 `HomeworkAutoUpload.spec` 已删除
- `selenium-manager.exe` 作为 binary 打包：已改为通过 `os.path.dirname(selenium.__file__)` 动态定位，无需再随 Python 版本手改硬编码路径
- datas 用 `collect_data_files('customtkinter')` 打包 customtkinter 主题资源，`collect_data_files('tkinterdnd2')` 打包文件拖拽所需的 tkdnd 平台二进制
- hiddenimports 已补齐 Agent 及新增模块（`auto_retry_agent`、`failure_analysis_agent`、`error_types`、`deepseek_helper`、`react_loop`、`experience_memory`、`pipeline_watchdog`、`api_server` 及 fastapi/uvicorn/python_multipart、`tkinterdnd2`），并移除了不存在的 `dnd_handler`。**新增动态导入的模块时（尤其是函数内 import）需同步更新 build.spec 的 hiddenimports**
- 打包目标平台：Windows 10/11 x64，目标 Python 版本：3.9+
- exe 名称：`HomeworkAutoUpload.exe`
- `build.bat` 已不再删除 `*.spec` 文件（旧版的 `del /q *.spec` 已移除），打包后 `build.spec` 会保留
