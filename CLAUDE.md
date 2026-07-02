# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

桌面自动化工具：监控文件夹中的学生作业文件 → DeepSeek AI 识别科目 → Selenium 浏览器自动上传到七天网络数智作业平台（zuoye.7net.cc），带 tkinter GUI 管理界面。支持试题+答案文件合并、拖拽上传、系统托盘最小化、数据统计分析。

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

**分层模块化 + 生产者-消费者模式**，11 个模块全部在项目根目录（无 `src/` 子目录）。

### 线程模型

```
主线程: tkinter GUI 主循环 (gui_manager.py)
  └─ 后台线程 backend_worker() (main.py)
       ├─ 文件监控线程 watchdog (file_monitor.py)
       └─ 上传处理线程 (upload_processor.py)
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
```

### 浏览器延迟初始化

程序启动时只开启文件监控，浏览器不启动。`UploadProcessor` 首次从队列取到文件时才调用 `browser.ensure_initialized()` 启动浏览器。上传队列清空 + 空闲 30 秒后自动关闭浏览器。`upload_processor.processing` 标记阻止后台在上传进行中误关浏览器。

### 各模块要点

| 模块 | 关键点 |
|------|--------|
| `browser_automation.py` | 最大的模块（~1400行），已针对七天网络（Element UI + Vue）完整适配。所有选择器硬编码到该网站，包含多方案降级点击策略（Vue API → JS click → ActionChains → MouseEvent）。底部有 CLI 测试入口 |
| `gui_manager.py` | tkinter Notebook 双标签页（上传管理 + 数据统计）。上传管理含文件夹管理、试题答案合并+拖拽、失败列表。Treeview 点击操作列（清空/删除/重传/忽略）。关闭时最小化到系统托盘（pystray） |
| `upload_processor.py` | 单线程顺序消费，`processing` 标记防止后台误关浏览器，每次上传前都校验学校 |
| `info_extractor.py` | 年级正则：`^(.+?)(高一\|高二\|...\|小六)$`，支持 txt/docx/doc/pdf（.doc 通过 olefile 解析 OLE2 二进制格式） |
| `subject_classifier.py` | 调用 `deepseek-chat`，temperature=0，限制 9 个科目，最多重试 3 次间隔 2 秒 |
| `file_monitor.py` | watchdog `on_created` 事件，文件稳定等待 2 秒后入队，过滤根目录下的直接文件 |
| `db_manager.py` | SQLite，`upload_records` + `analysis_records` 双表，`check_same_thread=False`。分析表支持按科目/学校年级/日期聚合查询 |
| `config_manager.py` | 处理 PyInstaller 打包路径（`sys._MEIPASS`），`@property` 暴露配置项，自动清理 Unicode 控制字符 |
| `file_merger.py` | 试题+答案合并（试题在前，分页符分隔，答案在后）。.doc/.docx 用 Word COM（支持 MS Word / WPS），.pdf 用 pypdf |
| `stats_panel.py` | 数据统计标签页：matplotlib 柱状图（按科目/学校年级切换）+ 折线图（按日/周/月聚合）+ 上传/失败记录表 + openpyxl Excel 导出 |
| `main.py` | 入口，协调线程启停。支持 tkinterdnd2 拖拽（回退到标准 tk）。优雅退出：等 task_queue.join() → 停监控 → 关浏览器 → 关数据库 |

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
- `ROLE`：`"超级管理员"` 或 `"teacher"`
- `DEEPSEEK_API_KEY`：DeepSeek API 密钥
- `BROWSER_IDLE_TIMEOUT`：1800 秒（30 分钟）
- `MINIMIZE_TO_TRAY`：关闭窗口时最小化到系统托盘（默认 true）
- `UPLOAD_TIMEOUT`：上传超时秒数（默认 120）

## 打包注意事项

- `build.spec` 是 PyInstaller 配置，`console=False`（窗口模式），显式声明所有 `hiddenimports`
- 必须将 `selenium-manager.exe` 作为 binary 打包
- hiddenimports 包含：所有本地模块、selenium 子模块、pystray/PIL、matplotlib、openpyxl、pypdf、win32com、olefile、tkinterdnd2
- 打包目标平台：Windows 10/11 x64，目标 Python 版本：3.9+
- exe 名称：`HomeworkAutoUpload.exe`

## 数据库

SQLite 文件 `data.db`，两张表：

- `upload_records`：所有上传记录（success/failed）。索引：file_name、status、folder_name
- `analysis_records`：用户手动点击"复制数据到分析表"后从 upload_records 快照而来，用于统计图表。索引：subject、school、grade、upload_time

## 文件合并功能

GUI 的"合并文件"区域支持将试题文件和答案文件合并为一个文件（试题在前，分页符分隔，答案在后），合并后的文件自动加入上传队列。要求试题和答案格式一致（同为 .doc/.docx 或同为 .pdf）。Word 合并通过 win32com 调用本机 MS Word 或 WPS，PDF 合并通过 pypdf。
