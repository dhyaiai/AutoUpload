# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

桌面自动化工具：监控文件夹中的学生作业文件 → DeepSeek AI 识别科目 → Selenium 浏览器自动上传到七天网络数智作业平台（zuoye.7net.cc），带 tkinter GUI 管理界面。支持试题+答案文件合并、拖拽上传、系统托盘最小化、数据统计分析、AI Agent 自动诊断自愈失败任务。

## 常用命令

```bash
# 运行程序（开发模式）
python main.py

# 安装依赖
pip install -r requirements.txt

# 打包为单文件 exe
pyinstaller build.spec --clean

# 使用打包脚本
.\build.bat

# 浏览器自动化独立测试（无需启动完整GUI）
python browser_automation.py --file "C:\path\to\file.docx" --school "学校名" --grade "高一" --subject "生物"
python browser_automation.py --file "C:\path\to\file.docx"                # 自动从文件夹名解析学校/年级，AI识别科目
python browser_automation.py --file "C:\path\to\file.docx" --skip-login   # 复用已登录的浏览器会话
```

该项目没有测试套件、linter 或 formatter 配置。

## 架构

**分层模块化 + 生产者-消费者模式 + AI Agent 自愈系统**，16 个模块全部在项目根目录（无 `src/` 子目录）。

### 线程模型

```
主线程: tkinter GUI 主循环 (gui_manager.py)
  └─ 后台线程 backend_worker() (main.py)
       ├─ 文件监控线程 watchdog (file_monitor.py)
       ├─ 上传处理线程 (upload_processor.py)
       └─ Agent 线程 (auto_retry_agent.py)  ← 2.0 新增
```

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

### AI Agent 自愈系统（2.0 新增）

程序启动时自动拉起 `AutoRetryAgent` 后台线程，持续扫描失败记录，通过 **ReAct 循环**（Thought → Action → Observation）驱动 LLM 自主诊断并执行自愈策略。

**五个核心模块：**

| 模块 | 行数 | 职责 |
|------|------|------|
| `error_types.py` | 274 | 结构化错误体系：`UploadStage`（8个阶段）、`ErrorCategory`（5大类）、`ErrorType`（19种具体错误）、`RetryLevel`（L1-L5 自愈级别）、`STRATEGY_MAP`（阶段×错误→策略映射表）、错误分类推断规则、根因描述与建议 |
| `deepseek_helper.py` | 225 | DeepSeek API 通用封装，提供 `chat()` 和 `chat_json()` 两个方法，供 Agent 模块共用。支持多提供商（DeepSeek/Qwen），内置重试和超时 |
| `react_loop.py` | 294 | 通用 ReAct 循环引擎，零项目依赖。LLM 输出 Thought（推理）→ Action（工具调用）→ 引擎执行工具 → Observation（观察结果）→ 循环直到 Final（最终结论）。`max_steps` 硬上限防止死循环 |
| `auto_retry_agent.py` | 836 | 失败自动接管 Agent，后台常驻。含安全守护：熔断器（CircuitBreaker）、全局重试上限、LLM 无法绕过的硬编码安全约束。失败列表在 GUI 显示"Agent接管"状态。集成经验记忆：每次处置写入 `repair_experiences`，ReAct 前注入同指纹历史成功方案，上传结果回调回填成败 |
| `experience_memory.py` | — | 经验记忆模块：错误指纹（error_type+fail_stage+page_state）→动作序列→是否成功。提供历史成功方案的 prompt 提示构建，并用成功率统计动态修正 `STRATEGY_MAP`（L5 永不修正）。底部有 CLI 测试入口 |
| `failure_analysis_agent.py` | 698 | 按需触发的失败原因分析 Agent，ReAct 驱动 LLM 自主探索数据库、深度归因、生成 Markdown 分析报告。AI 禁用/失败时回退到模板报告 |

**自愈策略分级：**
- **L1 轻量重试**：原地重试当前步骤（网络抖动、API超时）
- **L2 页面复位**：关闭弹窗、返回首页（表单校验失败、元素定位超时）
- **L3 环境重置**：刷新页面、重新校验学校（学校切换失败）
- **L4 服务重启**：重启浏览器、重新登录（浏览器崩溃、登录态失效）
- **L5 人工兜底**：标记为待人工处理（科目不存在、权限不足、文件损坏）

**熔断机制：** `CircuitBreaker` 按错误类型追踪失败时间戳，同类型错误超过阈值（默认10次/30分钟）自动熔断，防止大面积故障时无效重试。

### 浏览器延迟初始化

程序启动时只开启文件监控，浏览器不启动。`UploadProcessor` 首次从队列取到文件时才调用 `browser.ensure_initialized()` 启动浏览器。上传队列清空 + 空闲 30 秒后自动关闭浏览器。`upload_processor.processing` 标记阻止后台在上传进行中误关浏览器。

### 各模块要点

| 模块 | 行数 | 关键点 |
|------|------|--------|
| `browser_automation.py` | 1622 | 最大的模块，已针对七天网络（Element UI + Vue）完整适配。所有选择器硬编码到该网站，包含多方案降级点击策略（Vue API → JS click → ActionChains → MouseEvent）。底部有 CLI 测试入口 |
| `gui_manager.py` | 1088 | tkinter Notebook 双标签页（上传管理 + 数据统计）。上传管理含文件夹管理、试题答案合并+拖拽、失败列表（含Agent接管状态列）。Treeview 点击操作列（清空/删除/重传/忽略）。关闭时最小化到系统托盘（pystray） |
| `upload_processor.py` | 548 | 单线程顺序消费，`processing` 标记防止后台误关浏览器，每次上传前都校验学校。上传完成后回调 `auto_retry_agent.on_upload_result()` 通知结果 |
| `info_extractor.py` | — | 年级正则：`^(.+?)(高一\|高二\|...\|小六)$`，支持 txt/docx/doc/pdf（.doc 通过 olefile 解析 OLE2 二进制格式） |
| `subject_classifier.py` | — | 调用 `deepseek-chat`，temperature=0，限制 9 个科目，最多重试 3 次间隔 2 秒 |
| `file_monitor.py` | — | watchdog `on_created` 事件，文件稳定等待 2 秒后入队，过滤根目录下的直接文件 |
| `db_manager.py` | 766 | SQLite，`upload_records` + `analysis_records` + `repair_experiences`（经验记忆）三表，`check_same_thread=False`。分析表支持按科目/学校年级/日期聚合查询。Agent 通过工具函数查询/更新失败记录 |
| `config_manager.py` | — | 处理 PyInstaller 打包路径（`sys._MEIPASS`），`@property` 暴露配置项，自动清理 Unicode 控制字符 |
| `file_merger.py` | — | 试题+答案合并（试题在前，分页符分隔，答案在后）。.doc/.docx 用 Word COM（支持 MS Word / WPS），.pdf 用 pypdf |
| `stats_panel.py` | 544 | 数据统计标签页：matplotlib 柱状图（按科目/学校年级切换）+ 折线图（按日/周/月聚合）+ 上传/失败记录表 + openpyxl Excel 导出 + 失败分析报告入口 |
| `main.py` | 228 | 入口，协调线程启停。支持 tkinterdnd2 拖拽（回退到标准 tk）。优雅退出：等 task_queue.join() → 停监控 → 关浏览器 → 关数据库。同时启动 AutoRetryAgent 线程 |

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
- `DEEPSEEK_API_KEY`：DeepSeek API 密钥
- `QWEN_API_KEY` / `QWEN_MODEL`：通义千问 API（deepseek_helper 支持多提供商切换）
- `BROWSER_IDLE_TIMEOUT`：1800 秒（30 分钟）
- `MINIMIZE_TO_TRAY`：关闭窗口时最小化到系统托盘（默认 true）
- `UPLOAD_TIMEOUT`：上传超时秒数（默认 120）
- `AUTO_RETRY_ENABLE`：是否启用自动重试 Agent（默认 true）
- `AUTO_RETRY_SCAN_INTERVAL`：Agent 扫描间隔秒数（默认 60）
- `AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD`：熔断阈值（默认 10 次/30 分钟）
- `AI_RETRY_AGENT_ENABLE` / `AI_ANALYSIS_AGENT_ENABLE`：AI 驱动重试/分析开关
- `AI_AGENT_MAX_STEPS`：ReAct 循环最大步数（默认 10）

## 数据库

SQLite 文件 `data.db`，两张表：

- `upload_records`：所有上传记录（success/failed/pending_retry）。索引：file_name、status、folder_name。字段含 fail_stage、error_category、error_type、retry_count、agent_retry_count、agent_status 等 Agent 相关列
- `analysis_records`：用户手动点击"复制数据到分析表"后从 upload_records 快照而来，用于统计图表。索引：subject、school、grade、upload_time

## 文件合并功能

GUI 的"合并文件"区域支持将试题文件和答案文件合并为一个文件（试题在前，分页符分隔，答案在后），合并后的文件自动加入上传队列。要求试题和答案格式一致（同为 .doc/.docx 或同为 .pdf）。Word 合并通过 win32com 调用本机 MS Word 或 WPS，PDF 合并通过 pypdf。

## 打包注意事项

- `build.spec` 是当前使用的 PyInstaller 配置，`console=False`（窗口模式）。项目根目录还有一个 `HomeworkAutoUpload.spec` 是旧版遗留文件，以 `build.spec` 为准
- 必须将 `selenium-manager.exe` 作为 binary 打包（路径含 Python 版本号，不同环境需调整）
- hiddenimports 包含：所有本地模块、selenium 子模块、pystray/PIL、matplotlib、openpyxl、pypdf、win32com、olefile、tkinterdnd2
- **⚠️ 已知问题**：`build.spec` 的 hiddenimports 缺少 Agent 相关模块（`auto_retry_agent`、`failure_analysis_agent`、`error_types`、`deepseek_helper`、`react_loop`），PyInstaller 静态分析可能检测不到这些动态导入的模块，打包后 exe 的 Agent 功能可能失效。新增 Agent 相关模块时需同步更新 build.spec
- 打包目标平台：Windows 10/11 x64，目标 Python 版本：3.9+
- exe 名称：`HomeworkAutoUpload.exe`
- `build.bat` 会删除 `*.spec` 文件（第37行 `if exist *.spec del /q *.spec`），打包后如需保留 spec 请注释该行
