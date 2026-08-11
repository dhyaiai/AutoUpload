# GUI 设置面板 + 数据库路径修复 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在 GUI 中添加"设置"页面集中管理所有配置项，同时修复数据库文件在 exe 外部创建的问题

**架构：** 在 gui_manager.py 中新增第三个导航页面"设置"，按分组展示所有配置项（Entry/Switch/ComboBox），通过 ConfigManager.set() 持久化；在 db_manager.py 中添加 PyInstaller 路径适配逻辑，使 data.db 始终创建在 exe 所在目录

**技术栈：** customtkinter (CTkEntry/CTkSwitch/CTkComboBox/CTkTabview), ConfigManager 单例, SQLite

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `gui_manager.py` | 修改 | 新增"设置"页面，含分组配置 UI |
| `db_manager.py` | 修改 | 添加 PyInstaller 路径适配，确保 data.db 在 exe 目录 |
| `config_manager.py` | 修改 | 新增 `get_all_editable()` 方法返回可编辑配置元数据 |
| `ui_theme.py` | 修改 | 新增设置页专用控件工厂方法 |

---

## 任务 1：config_manager.py 新增可编辑配置元数据

**文件：**
- 修改：`config_manager.py:329`（文件末尾追加方法）

- [ ] **步骤 1：新增 `get_all_editable()` 方法**

在 `ConfigManager` 类末尾（第 329 行前）添加方法，返回所有可在 GUI 中编辑的配置项及其元数据：

```python
def get_all_editable(self) -> dict:
    """
    返回所有可在 GUI 中编辑的配置项及其元数据。
    结构: {分组名: [{"key": ..., "label": ..., "type": ..., "default": ..., "help": ..., "options": ...}]}
    type: "str" | "int" | "float" | "bool" | "combo"
    """
    return {
        "网站账号": [
            {"key": "WEBSITE_URL", "label": "目标网站URL", "type": "str",
             "default": "https://zuoye.7net.cc", "help": "七天网络作业平台地址"},
            {"key": "USERNAME", "label": "登录用户名", "type": "str",
             "default": "", "help": "平台登录账号"},
            {"key": "PASSWORD", "label": "登录密码", "type": "str",
             "default": "", "help": "平台登录密码"},
            {"key": "ROLE", "label": "用户角色", "type": "combo",
             "default": "超级管理员", "options": ["超级管理员", "老师"],
             "help": "影响平台操作权限"},
        ],
        "AI 模型配置": [
            {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API Key", "type": "str",
             "default": "", "help": "用于科目识别的 DeepSeek API 密钥"},
            {"key": "QWEN_API_KEY", "label": "通义千问 API Key", "type": "str",
             "default": "", "help": "Agent 使用的通义千问密钥"},
            {"key": "QWEN_MODEL", "label": "通义千问模型", "type": "combo",
             "default": "qwen3.7-plus",
             "options": ["qwen3.7-plus", "qwen3.5-plus", "qwen-max", "qwen-plus"],
             "help": "Agent 对话使用的模型"},
            {"key": "QWEN_API_URL", "label": "通义千问 API URL", "type": "str",
             "default": "https://llm-nwnb3n9ni4k5ebc2.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions",
             "help": "MaaS 专属实例端点"},
            {"key": "QWEN_VL_MODEL", "label": "多模态视觉模型", "type": "combo",
             "default": "qwen3.7-plus",
             "options": ["qwen3.7-plus", "qwen3.5-plus", "qwen-vl-max"],
             "help": "截图理解专用模型"},
        ],
        "浏览器设置": [
            {"key": "CHROME_PROFILE_DIR", "label": "Chrome 用户目录", "type": "str",
             "default": "", "help": "留空=临时目录,填写则复用登录态"},
            {"key": "BROWSER_IDLE_TIMEOUT", "label": "浏览器空闲超时(秒)", "type": "int",
             "default": 1800, "help": "空闲多久后自动关闭浏览器"},
            {"key": "UPLOAD_IDLE_TIMEOUT", "label": "上传后空闲超时(秒)", "type": "int",
             "default": 1800, "help": "上传完成后无操作关闭超时"},
            {"key": "BROWSER_RESTART_INTERVAL", "label": "浏览器重启间隔(次)", "type": "int",
             "default": 50, "help": "每N次上传后重启浏览器,0=不自动重启"},
        ],
        "Agent 自动重试": [
            {"key": "AUTO_RETRY_ENABLE", "label": "启用自动重试", "type": "bool",
             "default": True, "help": "是否启用 Agent 自动接管失败任务"},
            {"key": "AI_RETRY_AGENT_ENABLE", "label": "启用 AI 决策", "type": "bool",
             "default": True, "help": "是否启用 AI 驱动的重试决策"},
            {"key": "AI_AGENT_MAX_STEPS", "label": "ReAct 最大步数", "type": "int",
             "default": 10, "help": "Agent 单次决策最大循环次数"},
            {"key": "AUTO_RETRY_SCAN_INTERVAL", "label": "扫描间隔(秒)", "type": "int",
             "default": 60, "help": "多久扫描一次失败记录"},
            {"key": "AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD", "label": "熔断阈值(次)", "type": "int",
             "default": 10, "help": "同类错误多少次后触发熔断"},
        ],
        "API 服务": [
            {"key": "API_SERVER_HOST", "label": "监听地址", "type": "str",
             "default": "0.0.0.0", "help": "API 服务监听地址"},
            {"key": "API_SERVER_PORT", "label": "监听端口", "type": "int",
             "default": 8000, "help": "API 服务监听端口"},
        ],
    }
```

- [ ] **步骤 2：Commit**

```bash
git add config_manager.py
git commit -m "feat: add get_all_editable() for GUI settings panel"
```

---

## 任务 2：ui_theme.py 新增设置页控件工厂

**文件：**
- 修改：`ui_theme.py:179`（文件末尾追加函数）

- [ ] **步骤 1：新增设置页专用控件工厂函数**

```python
def settings_entry(parent, variable: ctk.StringVar, **kwargs) -> ctk.CTkEntry:
    """设置页文本输入框"""
    defaults = dict(width=360, height=34, corner_radius=8,
                    font=font(12), fg_color=CARD_INNER,
                    border_color=BORDER, text_color=TEXT,
                    textvariable=variable)
    defaults.update(kwargs)
    return ctk.CTkEntry(parent, **defaults)


def settings_switch(parent, variable: ctk.BooleanVar, command=None, **kwargs) -> ctk.CTkSwitch:
    """设置页开关"""
    defaults = dict(font=font(12), text_color=TEXT,
                    variable=variable, command=command,
                    progress_color=SUCCESS,
                    button_color=BORDER, button_hover_color=PRIMARY)
    defaults.update(kwargs)
    return ctk.CTkSwitch(parent, **defaults)


def settings_combo(parent, variable: ctk.StringVar, values: list, **kwargs) -> ctk.CTkComboBox:
    """设置页下拉框"""
    defaults = dict(width=360, height=34, corner_radius=8,
                    font=font(12), dropdown_font=font(12),
                    fg_color=CARD_INNER, border_color=BORDER,
                    button_color=CARD_INNER, button_hover_color=PRIMARY_SOFT,
                    text_color=TEXT, dropdown_fg_color=CARD,
                    dropdown_hover_color=PRIMARY_SOFT,
                    dropdown_text_color=TEXT, variable=variable,
                    values=values, state="readonly")
    defaults.update(kwargs)
    return ctk.CTkComboBox(parent, **defaults)


def settings_label(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """设置页标签"""
    defaults = dict(text=text, font=font(12), text_color=TEXT_MUTED, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, **defaults)


def settings_help(parent, text: str, **kwargs) -> ctk.CTkLabel:
    """设置页帮助文字"""
    defaults = dict(text=text, font=font(10), text_color=TEXT_FAINT, anchor="w")
    defaults.update(kwargs)
    return ctk.CTkLabel(parent, **defaults)
```

- [ ] **步骤 2：Commit**

```bash
git add ui_theme.py
git commit -m "feat: add settings page widget factories to ui_theme"
```

---

## 任务 3：gui_manager.py 新增"设置"页面

**文件：**
- 修改：`gui_manager.py:148-193`（导航栏添加设置按钮）
- 修改：`gui_manager.py:99-120`（页面容器添加设置页）
- 修改：`gui_manager.py` 末尾（新增 `_create_settings_page` 及辅助方法）

- [ ] **步骤 1：导航栏添加"设置"按钮**

将 `gui_manager.py:149` 的导航按钮列表从：
```python
for key, label in [("upload", "上传管理"), ("stats", "数据统计")]:
```
改为：
```python
for key, label in [("upload", "上传管理"), ("stats", "数据统计"), ("settings", "设置")]:
```

- [ ] **步骤 2：页面容器添加设置页**

在 `_create_widgets()` 方法中（约第 116 行后）添加：
```python
# 页面3: 设置
self.page_settings = ctk.CTkFrame(self.content, fg_color="transparent")
self._create_settings_page()
```

- [ ] **步骤 3：`_show_page()` 支持 settings 页面**

修改 `_show_page()` 方法（约第 179-193 行），添加 settings 页面切换逻辑：
```python
def _show_page(self, name: str):
    """切换页面并更新导航高亮"""
    self._current_page = name
    self.page_upload.pack_forget()
    self.page_stats.pack_forget()
    self.page_settings.pack_forget()
    if name == "upload":
        page = self.page_upload
    elif name == "stats":
        page = self.page_stats
    else:
        page = self.page_settings
    page.pack(fill="both", expand=True)

    for key, btn in self._nav_buttons.items():
        if key == name:
            btn.configure(fg_color=theme.PRIMARY_SOFT, text_color=theme.PRIMARY)
        else:
            btn.configure(fg_color="transparent", text_color=theme.TEXT_MUTED)
```

- [ ] **步骤 4：实现 `_create_settings_page()` 方法**

在 `MainApplication` 类末尾添加设置页构建方法：

```python
def _create_settings_page(self):
    """创建设置页面: 顶部保存按钮 + 分组配置卡片"""
    page = self.page_settings
    page.grid_columnconfigure(0, weight=1)
    page.grid_rowconfigure(1, weight=1)

    # 顶部保存按钮栏
    header = ctk.CTkFrame(page, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
    self._settings_save_btn = theme.primary_button(
        header, "保存设置", self._save_settings, width=120)
    self._settings_save_btn.pack(side="right")
    self._settings_status_label = ctk.CTkLabel(
        header, text="", font=theme.font(11), text_color=theme.SUCCESS)
    self._settings_status_label.pack(side="right", padx=12)

    # 可滚动配置区
    scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
    scroll.grid(row=1, column=0, sticky="nsew")
    scroll.grid_columnconfigure(0, weight=1)

    # 存储所有配置控件的引用: key -> {"var": ..., "widget": ..., "type": ...}
    self._settings_widgets = {}
    editable = self.config.get_all_editable()

    for group_idx, (group_name, items) in enumerate(editable.items()):
        card = theme.card(scroll)
        card.grid(row=group_idx, column=0, sticky="ew", pady=(0, 12))
        theme.card_title(card, group_name).pack(fill="x", padx=20, pady=(16, 12))

        for item in items:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(0, 10))
            row.grid_columnconfigure(1, weight=1)

            # 标签 + 帮助文字
            label_col = ctk.CTkFrame(row, fg_color="transparent")
            label_col.grid(row=0, column=0, sticky="w", padx=(0, 16))
            theme.settings_label(label_col, item["label"]).pack(anchor="w")
            if item.get("help"):
                theme.settings_help(label_col, item["help"]).pack(anchor="w")

            # 根据类型创建控件
            key = item["key"]
            current_val = self.config.get(key, item["default"])
            cfg_type = item["type"]

            if cfg_type == "bool":
                var = ctk.BooleanVar(value=bool(current_val))
                widget = theme.settings_switch(row, var)
                widget.grid(row=0, column=1, sticky="w")
                self._settings_widgets[key] = {"var": var, "type": "bool"}
            elif cfg_type == "combo":
                var = ctk.StringVar(value=str(current_val))
                widget = theme.settings_combo(row, var, values=item.get("options", []))
                widget.grid(row=0, column=1, sticky="w")
                self._settings_widgets[key] = {"var": var, "type": "combo"}
            elif cfg_type == "int":
                var = ctk.StringVar(value=str(current_val))
                widget = theme.settings_entry(row, var)
                widget.grid(row=0, column=1, sticky="w")
                self._settings_widgets[key] = {"var": var, "type": "int"}
            elif cfg_type == "float":
                var = ctk.StringVar(value=str(current_val))
                widget = theme.settings_entry(row, var)
                widget.grid(row=0, column=1, sticky="w")
                self._settings_widgets[key] = {"var": var, "type": "float"}
            else:  # str
                var = ctk.StringVar(value=str(current_val))
                widget = theme.settings_entry(row, var)
                widget.grid(row=0, column=1, sticky="w")
                self._settings_widgets[key] = {"var": var, "type": "str"}
```

- [ ] **步骤 5：实现 `_save_settings()` 方法**

```python
def _save_settings(self):
    """保存所有设置到 config.json"""
    type_map = {
        "int": int,
        "float": float,
        "bool": lambda v: bool(v),
        "str": str,
        "combo": str,
    }
    errors = []
    for key, info in self._settings_widgets.items():
        raw = info["var"].get()
        converter = type_map[info["type"]]
        try:
            value = converter(raw)
        except (ValueError, TypeError):
            errors.append(f"{key}: 格式错误")
            continue
        self.config.set(key, value)

    if errors:
        self._settings_status_label.configure(
            text=f"保存失败: {'; '.join(errors)}", text_color=theme.DANGER)
    else:
        self._settings_status_label.configure(
            text="✓ 已保存", text_color=theme.SUCCESS)
        # 3秒后清除状态文字
        self.root.after(3000, lambda: self._settings_status_label.configure(text=""))
```

- [ ] **步骤 6：Commit**

```bash
git add gui_manager.py
git commit -m "feat: add settings page with grouped config controls"
```

---

## 任务 4：db_manager.py 添加 PyInstaller 路径适配

**文件：**
- 修改：`db_manager.py:22-35`（`__new__` 方法中的路径逻辑）

- [ ] **步骤 1：修改 `__new__` 方法**

将 `db_manager.py:22-35` 的 `__new__` 方法从：
```python
def __new__(cls, db_path: str = "data.db"):
    if cls._instance is None:
        cls._instance = super().__new__(cls)
        cls._instance.db_path = db_path
        cls._instance._init_db()
    return cls._instance
```
改为：
```python
def __new__(cls, db_path: str = None):
    if cls._instance is None:
        cls._instance = super().__new__(cls)
        if db_path is None:
            # 未指定路径时,智能选择位置
            import sys
            if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
                # PyInstaller 打包环境: 数据库放在 exe 所在目录
                exe_dir = os.path.dirname(sys.executable)
                cls._instance.db_path = os.path.join(exe_dir, "data.db")
            else:
                # 开发环境: 使用当前工作目录
                cls._instance.db_path = "data.db"
        else:
            cls._instance.db_path = db_path
        cls._instance._init_db()
    return cls._instance
```

- [ ] **步骤 2：验证逻辑**

确认修改后：
- 开发环境 `python main.py` → `data.db` 在项目目录
- 打包后 exe 在桌面运行 → `data.db` 在桌面（即 exe 同目录）
- 打包后 exe 在 `D:\Apps\` 运行 → `data.db` 在 `D:\Apps\`
- 单元测试传入临时路径 → 使用临时路径（不受影响）

- [ ] **步骤 3：Commit**

```bash
git add db_manager.py
git commit -m "fix: database path resolves to exe directory in PyInstaller build"
```

---

## 任务 5：验证与测试

- [ ] **步骤 1：启动 GUI 验证设置页面**

运行 `python main.py`，检查：
- 左侧导航栏出现"设置"按钮
- 点击切换到设置页面，显示 6 个分组卡片
- 每个配置项有标签、帮助文字、对应控件
- 修改几个值后点击"保存设置"，状态文字显示"✓ 已保存"
- 重启程序，确认设置值已持久化

- [ ] **步骤 2：验证数据库路径**

在代码中临时添加 `print(f"Database path: {db.db_path}")` 确认：
- 开发环境输出项目目录的 `data.db`
- 打包后输出 exe 目录的 `data.db`

- [ ] **步骤 3：运行 L1 测试确认无回归**

```bash
.venv\Scripts\python -m pytest tests -q
```

预期：全部通过（76 个用例）

---

## 自检

**规格覆盖度：**
- 需求 1（GUI 配置面板）→ 任务 1 + 2 + 3 ✅
- 需求 2（数据库不在桌面创建）→ 任务 4 ✅
- 换大模型在 GUI 配置 → 任务 1 的 "AI 模型配置" 分组包含 QWEN_MODEL 下拉框 ✅

**占位符扫描：** 无"待定"、"TODO"、"后续实现"等占位符 ✅

**类型一致性：**
- `get_all_editable()` 返回的 dict 结构在任务 1 定义，任务 3 使用，字段一致 ✅
- `_settings_widgets` 存储格式在任务 3 定义，任务 5 使用，一致 ✅

---

## 执行交接

计划已完成并保存到 `docs/superpowers/plans/2026-08-11-gui-settings-and-db-path.md`。两种执行方式：

**1. 子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代

**2. 内联执行** - 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？
