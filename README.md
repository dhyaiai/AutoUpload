# 作业自动上传工具

## 📋 项目简介

这是一个桌面自动化程序,用于自动监听文件夹中的学生作业文件,通过AI识别科目后自动上传到在线教学平台。

### 核心功能

- ✅ **自动监控**: 实时监控"学校+年级"子文件夹中的新增文件
- ✅ **智能识别**: 调用DeepSeek AI自动识别作业科目
- ✅ **自动上传**: Selenium控制Chrome浏览器完成登录、学校切换、文件上传
- ✅ **防重复上传**: SQLite数据库记录已上传文件,避免重复
- ✅ **图形界面**: 类似微信的GUI管理窗口,直观易用
- ✅ **失败重试**: 上传失败的文件醒目提醒,支持一键重新上传
- ✅ **单文件部署**: PyInstaller打包为单个exe,双击即用

---

## 🏗️ 项目架构

```
AutoUpload/
├── main.py                  # 主控制模块(程序入口)
├── config_manager.py        # 配置管理模块
├── db_manager.py           # 数据库管理模块
├── info_extractor.py       # 信息提取模块(解析学校年级、读取文件)
├── subject_classifier.py   # AI科目识别模块
├── browser_automation.py   # 浏览器自动化模块
├── file_monitor.py         # 文件监控模块
├── upload_processor.py     # 上传处理器模块
├── gui_manager.py          # GUI管理界面模块
├── config.json             # 配置文件
├── requirements.txt        # Python依赖包
├── data.db                 # SQLite数据库(自动生成)
└── 作业文件夹/              # 监控根目录(自动创建)
    ├── 合肥卓越中学高一/
    ├── 合肥一中高二/
    └── ...
```

---

## 🚀 快速开始

### 1. 环境要求

- **操作系统**: Windows 10/11 (64位)
- **Python版本**: Python 3.9+
- **浏览器**: Google Chrome (最新版本)
- **网络**: 能访问目标网站和DeepSeek API

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置程序

编辑 `config.json` 文件,填写必要信息:

```json
{
    "ROOT_DIR": "./作业文件夹",
    "WEBSITE_URL": "https://your-school-platform.com",
    "USERNAME": "你的用户名",
    "PASSWORD": "你的密码",
    "ROLE": "teacher",
    "DEEPSEEK_API_KEY": "sk-your-api-key-here",
    "CHROME_DRIVER_PATH": "./chromedriver.exe",
    "FILE_STABLE_DELAY": 2,
    "BROWSER_IDLE_TIMEOUT": 1800,
    "MAX_RETRY_COUNT": 3,
    "SLEEP_INTERVAL": 0.5
}
```

**重要配置项说明:**
- `WEBSITE_URL`: 目标教学平台的网址
- `USERNAME/PASSWORD`: 登录账号密码
- `DEEPSEEK_API_KEY`: DeepSeek API密钥(需在 https://platform.deepseek.com 申请)
- `CHROME_DRIVER_PATH`: Chrome驱动路径(可留空让Selenium自动管理)

### 4. 运行程序

```bash
python main.py
```

---

## 📖 使用说明

### 基本流程

1. **启动程序**: 双击运行 `main.py` 或打包后的exe文件
2. **创建文件夹**: 在GUI中输入学校名称和年级,点击"创建"按钮
3. **放入作业文件**: 将作业文件复制到对应的"学校+年级"文件夹中
4. **自动上传**: 程序会自动检测新文件,识别科目并上传
5. **查看日志**: 在GUI日志区域查看上传进度和结果
6. **处理失败**: 如有失败文件,在失败列表中点击"重新上传"

### 文件夹命名规则

文件夹必须按照 **"学校名称+年级"** 格式命名,例如:
- ✅ 合肥卓越中学高一
- ✅ 北京四中初二
- ✅ 上海中学高三

支持的年级: 高一、高二、高三、初一、初二、初三、小一~小六

### 支持的文件格式

- `.txt` - 文本文件
- `.docx` - Word文档
- `.pdf` - PDF文档

其他格式会被标记为"未知科目",需要手动处理。

---

## ⚙️ 高级配置

### 浏览器选择器定制

由于不同网站的HTML结构不同,需要修改 `browser_automation.py` 中的元素选择器:

```python
# 登录相关选择器
username_input = self.driver.find_element(By.NAME, "username")  # 改为实际的选择器
password_input = self.driver.find_element(By.NAME, "password")
login_button = self.driver.find_element(By.ID, "login-btn")

# 上传相关选择器
upload_btn = self.driver.find_element(By.ID, "upload-homework-btn")
file_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
grade_select = self.driver.find_element(By.ID, "grade-select")
subject_select = self.driver.find_element(By.ID, "subject-select")
```

**建议**: 使用Chrome开发者工具(F12)查看网页元素,找到正确的选择器。

### 打包为exe

```bash
pyinstaller --onefile --windowed --name "作业自动上传工具" main.py
```

生成的exe文件在 `dist/` 目录下。

---

## 🔧 技术栈

| 模块 | 技术选型 |
|------|---------|
| GUI界面 | Python tkinter + ttk |
| 文件监控 | watchdog |
| 文档读取 | python-docx, pdfplumber |
| AI识别 | DeepSeek Chat API |
| 浏览器自动化 | Selenium WebDriver + Chrome |
| 数据库 | SQLite3 |
| 打包工具 | PyInstaller |

---

## 🐛 常见问题

### 1. 浏览器启动失败

**原因**: Chrome浏览器未安装或版本不匹配  
**解决**: 
- 安装最新版Google Chrome
- 确保ChromeDriver版本与Chrome版本匹配
- 或使用Selenium自动管理驱动(配置中留空CHROME_DRIVER_PATH)

### 2. API调用失败

**原因**: DeepSeek API密钥错误或网络问题  
**解决**:
- 检查 `config.json` 中的 `DEEPSEEK_API_KEY` 是否正确
- 确认网络能访问 `https://api.deepseek.com`
- 查看日志中的具体错误信息

### 3. 元素定位超时

**原因**: 网站结构与代码中的选择器不匹配  
**解决**:
- 使用浏览器开发者工具检查元素
- 修改 `browser_automation.py` 中的选择器
- 增加等待时间(`SLEEP_INTERVAL`)

### 4. 文件重复上传

**原因**: 数据库记录异常  
**解决**:
- 删除 `data.db` 文件重新开始
- 或在GUI中使用"清空文件夹"功能清理记录

---

## 📝 开发说明

### 模块职责

1. **config_manager.py**: 读取和管理配置
2. **db_manager.py**: 数据库增删查改操作
3. **info_extractor.py**: 解析文件夹名、读取文件内容
4. **subject_classifier.py**: 调用AI识别科目
5. **browser_automation.py**: 控制浏览器自动化操作
6. **file_monitor.py**: 监控文件系统变化
7. **upload_processor.py**: 协调整个上传流程
8. **gui_manager.py**: 提供图形用户界面
9. **main.py**: 程序入口,线程协调

### 线程架构

```
主线程: GUI界面(tkinter主循环)
  ↓
后台线程: backend_worker()
  ├─ 浏览器管理线程
  ├─ 文件监控线程(watchdog)
  └─ 上传处理线程(单线程队列消费)
```

线程间通过 `Queue` 进行安全通信。

---

## 📄 许可证

本项目仅供学习交流使用。

---

## 👨‍💻 作者

如有问题或建议,欢迎反馈!

---

**祝使用愉快! 🎉**
