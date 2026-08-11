# GUI 滚动拖影 + 最小化恢复加载问题 修复计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 修复两个 GUI 渲染性能问题：(1) 拖动滚动条时内容出现拖影/残影；(2) 从系统托盘恢复窗口时有明显加载过程

**架构：** 
- 问题1根因：customtkinter 的 `CTkScrollableFrame` 内部用 Canvas 嵌入真实 HWND Frame，滚动时 Windows 走 erase→paint 路径，无双重缓冲，产生拖影。现有 stats_panel 已有部分修复但 settings 页缺失，且修复方案可优化为更彻底的 `update_idletasks()` 调用。
- 问题2根因：`withdraw()` + `deiconify()` 路径下 Windows 丢弃窗口位图缓存，恢复时 customtkinter 需完整重绘所有控件（包括 Canvas 绘制的图表），造成明显延迟。解决方案：用 `iconify()` 替代 `withdraw()` 保持窗口表面缓存，同时隐藏任务栏条目。

**技术栈：** customtkinter 5.2+, tkinter, pystray, Windows API

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `gui_manager.py` | 主窗口管理，含页面切换、托盘最小化/恢复逻辑 |
| `stats_panel.py` | 统计面板，已有滚动残影修复（需优化） |
| `ui_theme.py` | UI 控件工厂（不改） |

---

## 任务 1：优化 settings 页滚动拖影修复

**文件：**
- 修改：`gui_manager.py:1435-1437`（settings 页 CTkScrollableFrame 创建处）

- [ ] **步骤 1：在 settings 页创建滚动容器后添加残影修复绑定**

在 `_create_settings_page` 方法中，`scroll.grid(...)` 之后添加一行调用修复方法：

```python
# 可滚动配置区
scroll = ctk.CTkScrollableFrame(page, fg_color="transparent")
scroll.grid(row=1, column=0, sticky="nsew")
scroll.grid_columnconfigure(0, weight=1)
self._settings_scroll = scroll  # 保存引用供修复方法使用
self._bind_settings_scroll_fix()  # 新增：修复滚动拖影
```

- [ ] **步骤 2：添加 `_bind_settings_scroll_fix` 方法**

在 `_create_settings_page` 方法之后（约第 1489 行 `self._settings_widgets[key] = ...` 的 for 循环结束后），添加新方法：

```python
def _bind_settings_scroll_fix(self):
    """
    修复 settings 页 CTkScrollableFrame 拖动滚动条时的拖影问题。
    根因：customtkinter 的 ScrollableFrame 用 Canvas 内嵌真实 HWND Frame，
    滚动时 Windows 走 erase→paint 路径无双重缓冲，产生残影。
    修复：在滚动事件后强制 update_idletasks() 完成重绘。
    """
    def _force_redraw(_event=None):
        try:
            self._settings_scroll.update_idletasks()
        except Exception:
            pass

    # 滚轮事件
    try:
        self.root.bind_all("<MouseWheel>", _force_redraw, add="+")
    except Exception:
        pass

    # 滚动条拖动事件
    sbar = getattr(self._settings_scroll, "_scrollbar", None)
    if sbar is not None:
        for seq in ("<B1-Motion>", "<Button-1>", "<ButtonRelease-1>"):
            try:
                sbar.bind(seq, _force_redraw, add=True)
            except Exception:
                pass
```

- [ ] **步骤 3：运行程序验证 settings 页滚动无拖影**

运行：`python main.py`
预期：切换到设置页，拖动滚动条时内容无残影/拖影

---

## 任务 2：优化 stats_panel 滚动修复（去除重复绑定）

**文件：**
- 修改：`stats_panel.py:313-343`（`_bind_scroll_ghost_fix` 方法）

- [ ] **步骤 1：修复 MouseWheel 重复绑定问题**

当前 `_bind_scroll_ghost_fix` 在 stats_panel 创建时调用，但 `bind_all` 是全局的，如果多个 StatsPanel 实例存在会重复绑定。改为在绑定前解绑旧处理器：

```python
def _bind_scroll_ghost_fix(self):
    """
    修复 Windows 下滚动统计面板时表格区域出现残影的问题
    """
    trees = (self._upload_tree, self._failed_tree)

    def _redraw(_event=None):
        for t in trees:
            try:
                t.update_idletasks()
            except Exception:
                pass

    # 滚轮：先解绑旧的再绑定新的，避免重复
    try:
        self.root.unbind_all("<MouseWheel>", self._wheel_bind_id)  # 保存绑定 ID
    except Exception:
        pass
    try:
        self._wheel_bind_id = self.root.bind_all("<MouseWheel>", _redraw, add="+")
    except Exception:
        pass

    # 拖动滚动条
    sbar = getattr(self._scroll, "_scrollbar", None)
    if sbar is not None:
        for seq in ("<B1-Motion>", "<Button-1>", "<ButtonRelease-1>"):
            try:
                sbar.bind(seq, _redraw, add=True)
            except Exception:
                pass
```

- [ ] **步骤 2：在 destroy 方法中清理绑定**

在 `destroy` 方法中添加清理：

```python
def destroy(self):
    try:
        self.root.unbind_all("<MouseWheel>", self._wheel_bind_id)
    except Exception:
        pass
    # ... 其余清理代码 ...
```

- [ ] **步骤 3：运行程序验证统计页滚动正常**

运行：`python main.py`
预期：切换到统计页，滚动时表格无残影

---

## 任务 3：修复最小化到托盘后恢复有明显加载过程的问题

**文件：**
- 修改：`gui_manager.py:1316-1324`（`_restore_window` 方法）
- 修改：`gui_manager.py:1384-1388`（`_on_closing` 中的最小化逻辑）

- [ ] **步骤 1：用 `iconify()` 替代 `withdraw()` 保持窗口位图缓存**

修改 `_on_closing` 方法中的最小化逻辑：

```python
if use_tray:
    try:
        if self._setup_tray():
            # 使用 iconify 而非 withdraw，保持窗口表面位图缓存
            # 恢复时 Windows 直接显示缓存，无需完整重绘
            self.root.iconify()
            # 隐藏任务栏条目（通过 withdraw 的窗口样式效果）
            self._hide_from_taskbar()
            if self.tray_icon and hasattr(self.tray_icon, 'notify'):
                try:
                    if self._tray_ready.wait(timeout=2):
                        self.tray_icon.notify(
                            "作业自动上传工具仍在后台运行\n双击托盘图标可恢复窗口",
                            title="作业自动上传"
                        )
                except Exception:
                    pass
            return
        else:
            self._log_to_gui("托盘启动失败，回退到退出确认模式", "error")
    except Exception:
        import traceback
        self._log_to_gui(f"托盘异常: {traceback.format_exc()}", "error")
```

- [ ] **步骤 2：添加 `_hide_from_taskbar` 和 `_show_on_taskbar` 方法**

在 `_setup_tray` 方法之后添加：

```python
def _hide_from_taskbar(self):
    """隐藏任务栏条目（窗口已 iconify 后移除 WS_EX_APPWINDOW）"""
    try:
        import ctypes
        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080

        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # 移除 APPWINDOW，添加 TOOLWINDOW（不在任务栏显示）
        new_style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
    except Exception:
        pass

def _show_on_taskbar(self):
    """恢复任务栏条目"""
    try:
        import ctypes
        GWL_EXSTYLE = -20
        WS_EX_APPWINDOW = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080

        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # 恢复 APPWINDOW，移除 TOOLWINDOW
        new_style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
    except Exception:
        pass
```

- [ ] **步骤 3：修改 `_restore_window` 方法恢复任务栏条目**

```python
def _restore_window(self):
    """在主线程中恢复窗口"""
    self._show_on_taskbar()  # 先恢复任务栏样式
    self.root.deiconify()
    self.root.lift()
    self.root.focus_force()
```

- [ ] **步骤 4：运行程序验证最小化/恢复无明显加载**

运行：`python main.py`
预期：
1. 点击关闭按钮 → 窗口最小化到托盘，任务栏条目消失
2. 双击托盘图标 → 窗口立即恢复，无可见重绘/加载过程

---

## 任务 4：综合验证

- [ ] **步骤 1：验证所有页面滚动正常**

运行：`python main.py`
测试步骤：
1. 切换到设置页，快速拖动滚动条 → 无拖影
2. 切换到统计页，快速拖动滚动条 → 无拖影
3. 在统计页滚动表格区域 → 无拖影

- [ ] **步骤 2：验证托盘最小化/恢复流畅**

测试步骤：
1. 点击窗口关闭按钮 → 窗口隐藏，托盘图标出现
2. 双击托盘图标 → 窗口立即恢复（<100ms 视觉感知）
3. 从托盘菜单退出 → 程序正常关闭

- [ ] **步骤 3：验证功能完整性**

测试步骤：
1. 创建文件夹功能正常
2. 合并文件功能正常
3. 日志实时更新正常
4. 统计面板数据刷新正常

---

## 自检

**规格覆盖度：**
- 问题1（滚动拖影）：任务1（settings页修复）+ 任务2（stats页优化）✓
- 问题2（最小化恢复加载）：任务3（iconify替代withdraw）✓

**占位符扫描：** 无占位符，所有步骤有完整代码。

**类型一致性：**
- `self._settings_scroll` 在任务1步骤1定义，在任务1步骤2使用 ✓
- `self._wheel_bind_id` 在任务2步骤1定义，在任务2步骤2使用 ✓
- `_hide_from_taskbar` / `_show_on_taskbar` 在任务3步骤2定义，在任务3步骤1/3调用 ✓
