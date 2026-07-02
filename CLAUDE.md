# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

桌面自动化工具：监控文件夹中的学生作业文件 → DeepSeek AI 识别科目 → Selenium 浏览器自动上传到教学平台，带 tkinter GUI 管理界面。

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
```

该项目没有测试套件、linter 或 formatter 配置。

## 架构

**分层模块化 + 生产者-消费者模式**，9 个模块全部在项目根目录（无 `src/` 子目录）。

### 线程模型

```
主线程: tkinter GUI 主循环 (gui_manager.py)
  └─ 后台线程 backend_worker() (main.py)
       ├─ 文件监控线程 watchdog (file_monitor.py)
       └─ 上传处理线程 (upload_processor.py)
```

线程间通过 `Queue` 安全通信：
- `task_queue`：文件路径 → 上传处理器
- `log_queue`：日志消息 → GUI

### 三个单例

`BrowserAutomation`、`DatabaseManager`、`ConfigManager` 均用 `__new__` 实现单例，确保全局只有一个浏览器实例、一个数据库连接、一份配置。

### 核心流程

```
watchdog 检测新文件 → task_queue → UploadProcessor.run()
  → InfoExtractor.parse_folder_name()（正则解析"学校名+年级"）
  → InfoExtractor.read_file_content()（前200字）
  → SubjectClassifier.classify()（DeepSeek API）
  → BrowserAutomation（确保已登录 → 校验/切换学校 → 执行上传）
  → DatabaseManager.add_record()
```

### 浏览器延迟初始化

程序启动时只开启文件监控，浏览器不启动。`UploadProcessor` 首次从队列取到文件时才调用 `browser.ensure_initialized()` 启动浏览器。上传队列清空 + 空闲 30 秒后自动关闭浏览器。

### 各模块要点

| 模块 | 关键点 |
|------|--------|
| `browser_automation.py` | 最大的模块（~64KB），所有 Selenium 选择器硬编码，更换目标网站需改这里 |
| `gui_manager.py` | tkinter Treeview 显示文件夹列表和失败列表，`after()` 定时轮询 log_queue，关闭时最小化到系统托盘 |
| `upload_processor.py` | 单线程顺序消费，`processing` 标记防止后台误关浏览器，按学校分组减少切换 |
| `info_extractor.py` | 年级正则：`^(.+?)(高一\|高二\|...\|小六)$`，支持 txt/docx/pdf |
| `subject_classifier.py` | 调用 `deepseek-chat`，temperature=0，限制 9 个科目 |
| `file_monitor.py` | watchdog `on_created` 事件，文件稳定等待 2 秒后入队 |
| `db_manager.py` | SQLite，`upload_records` 表，`check_same_thread=False` |
| `config_manager.py` | 处理 PyInstaller 打包路径（`sys._MEIPASS`），`@property` 暴露配置项 |

## 配置

`config.json` 包含敏感信息（API key、密码），已被 `.gitignore` 排除。首次运行程序会自动生成。PyInstaller 打包时 exe 会从同目录读取 config.json 而非从 `_MEIPASS` 读取。

## 打包注意事项

- `build.spec` 是当前使用的 PyInstaller 配置，`console=False`（窗口模式），显式声明所有 `hiddenimports`
- 需要将 `selenium-manager.exe` 作为 binary 打包进去
- 打包目标平台：Windows 10/11 x64，目标 Python 版本：3.9+
