# 打包为EXE文件指南

## 📦 快速开始

### 方法一: 使用自动打包脚本(推荐) ⭐

1. **双击运行** `build.bat` 文件
2. 等待打包完成(可能需要5-10分钟)
3. 在 `dist` 文件夹中找到 `作业自动上传工具.exe`

```
D:\python-project\AutoUpload\
└─ dist\
    └─ 作业自动上传工具.exe  ← 这就是生成的exe文件
```

### 方法二: 手动打包

如果自动脚本有问题,可以手动执行:

```bash
# 1. 安装PyInstaller
pip install pyinstaller

# 2. 安装所有依赖
pip install -r requirements.txt

# 3. 执行打包
pyinstaller build.spec --clean

# 4. 清理临时文件
rmdir /s /q build
del *.spec
```

## 🔧 打包配置说明

### build.spec 配置文件

这个文件告诉 PyInstaller 如何打包程序:

```python
a = Analysis(
    ['main.py'],              # 主入口文件
    datas=[                   # 需要包含的数据文件
        ('config.json', '.'), # 配置文件
    ],
    hiddenimports=[           # 隐式导入的模块
        'selenium',           # Selenium浏览器自动化
        'watchdog',           # 文件监控
        'docx',               # Word文档解析
        'pdfplumber',         # PDF文档解析
        'requests',           # HTTP请求
        'tkinter',            # GUI界面
        # ... 其他依赖
    ],
)

exe = EXE(
    name='作业自动上传工具',   # exe文件名
    console=False,            # False=不显示控制台窗口(GUI模式)
    upx=True,                 # 压缩exe文件大小
)
```

### 关键参数说明

| 参数 | 值 | 说明 |
|------|-----|------|
| `console` | `False` | 不显示黑色控制台窗口,只显示GUI界面 |
| `upx` | `True` | 压缩exe文件,减小体积(约减少50%) |
| `onefile` | 隐含 | 打包成单个exe文件(通过COLLECT实现) |
| `datas` | `[('config.json', '.')]` | 将config.json打包进exe |

## 📁 打包后的文件结构

### 打包前(开发环境)

```
D:\python-project\AutoUpload\
├─ main.py
├─ browser_automation.py
├─ upload_processor.py
├─ gui_manager.py
├─ db_manager.py
├─ config_manager.py
├─ info_extractor.py
├─ subject_classifier.py
├─ file_monitor.py
├─ config.json
├─ requirements.txt
├─ build.spec
└─ build.bat
```

### 打包后(分发环境)

```
D:\python-project\AutoUpload\dist\
─ 作业自动上传工具.exe  (约80-120MB)
```

**注意**: 
- ✅ 所有Python代码都已编译进exe
- ✅ 所有依赖库都已打包进exe
- ✅ config.json也已打包进exe(但建议外部保留一份)

## 🚀 首次运行准备

### 1. 确保Chrome浏览器已安装

程序使用 Chrome 浏览器进行自动化操作,请确保:
- ✅ Chrome浏览器已安装(版本 90+)
- ✅ ChromeDriver可用(新版Selenium会自动管理)

### 2. 配置config.json

虽然config.json已打包进exe,但建议**在exe同目录保留一份可编辑的config.json**:

```json
{
  "WEBSITE_URL": "https://school.7net.cn",
  "USERNAME": "your_username",
  "PASSWORD": "your_password",
  "ROLE": "admin",
  "WATCH_FOLDER": "D:/upload",
  "SLEEP_INTERVAL": 1,
  "BROWSER_IDLE_TIMEOUT": 1800,
  "MAX_RETRY_COUNT": 3,
  "DEEPSEEK_API_KEY": "sk-xxxxxxxxxxxx"
}
```

**重要**: 
- 修改密码、API密钥等敏感信息
- 设置正确的监听文件夹路径

### 3. 创建监听文件夹

根据 `config.json` 中的 `WATCH_FOLDER` 路径创建文件夹:

```
D:/upload/
─ 蚌埠第二中学高二/
│   ├─ 语文作业.docx
│   └─ 数学作业.docx
├─ 蚌埠第二中学高一/
│   └─ 英语作业.docx
└─ ...
```

## 🎯 运行exe文件

### 方式1: 双击运行

直接双击 `作业自动上传工具.exe`,会打开GUI界面。

### 方式2: 命令行运行(查看日志)

如果需要查看详细日志,可以在命令行中运行:

```bash
cd D:\python-project\AutoUpload\dist
作业自动上传工具.exe
```

或者修改 `build.spec`:
```python
exe = EXE(
    ...
    console=True,  # 改为True,显示控制台窗口
    ...
)
```

重新打包后即可看到详细日志输出。

## ❓ 常见问题

### Q1: 打包后exe文件很大怎么办?

**A:** 这是正常现象。Python程序打包后会包含:
- Python解释器(~30MB)
- 所有依赖库(~50-80MB)
- 你的代码(~几百KB)

总计约 **80-120MB**,属于正常范围。

**优化方法**:
- 启用UPX压缩(已启用):可减少约50%体积
- 移除未使用的依赖
- 使用虚拟环境只安装必要包

### Q2: 杀毒软件报毒怎么办?

**A:** PyInstaller打包的exe可能被误报为病毒,这是常见现象。

**解决方法**:
1. 将exe添加到杀毒软件白名单
2. 使用数字签名(需要购买证书)
3. 向杀毒软件厂商提交误报

### Q3: 在其他电脑上运行报错怎么办?

**A:** 可能的原因:

1. **缺少Chrome浏览器**
   - 解决: 安装Chrome浏览器

2. **缺少Visual C++运行库**
   - 解决: 安装 [Microsoft Visual C++ Redistributable](https://aka.ms/vs/16/release/vc_redist.x64.exe)

3. **配置文件路径错误**
   - 解决: 在exe同目录放置config.json,并修改路径为绝对路径

4. **权限不足**
   - 解决: 右键exe → "以管理员身份运行"

### Q4: 如何更新程序?

**A:** 
1. 修改源代码(.py文件)
2. 重新运行 `build.bat` 打包
3. 替换旧的exe文件

**注意**: 每次修改代码后都需要重新打包!

### Q5: 如何让其他人也能使用?

**A:** 分发时提供以下文件:

```
作业自动上传工具_v1.0/
├─ 作业自动上传工具.exe      (主程序)
├─ config.json.example       (配置示例)
├─ README.md                 (使用说明)
└─ 快速开始指南.md           (可选)
```

用户需要:
1. 复制 `config.json.example` 为 `config.json`
2. 修改配置(用户名、密码等)
3. 创建监听文件夹
4. 运行exe

## 🔍 调试技巧

### 查看打包日志

如果打包失败,查看 `build.log` 文件(如果有)或控制台输出。

### 测试打包是否正确

1. **删除Python环境测试**:
   ```bash
   # 临时重命名Python安装目录
   ren C:\Users\YourName\AppData\Local\Programs\Python Python_backup
   
   # 运行exe,看是否能正常工作
   dist\作业自动上传工具.exe
   
   # 恢复Python环境
   ren C:\Users\YourName\AppData\Local\Programs\Python_backup Python
   ```

2. **在其他电脑测试**:
   - 找一台没有安装Python的电脑
   - 复制exe和config.json
   - 运行测试

### 添加调试输出

如果程序运行有问题,可以:

1. 修改 `build.spec`:
   ```python
   exe = EXE(
       ...
       debug=True,          # 启用调试模式
       console=True,        # 显示控制台
       ...
   )
   ```

2. 重新打包
3. 运行exe,查看控制台输出的详细错误信息

## 📊 打包时间参考

| 电脑配置 | 预计时间 |
|---------|---------|
| i5 + 8GB RAM + SSD | 3-5分钟 |
| i7 + 16GB RAM + SSD | 2-3分钟 |
| i3 + 4GB RAM + HDD | 8-12分钟 |

**首次打包较慢**,后续打包会利用缓存,速度更快。

## 🎓 进阶知识

### PyInstaller工作原理

1. **分析阶段(Analysis)**:
   - 扫描main.py的所有import语句
   - 递归查找所有依赖
   - 构建依赖树

2. **打包阶段(PYZ)**:
   - 将所有Python模块编译为.pyc
   - 打包成PYZ归档文件

3. **生成阶段(EXE)**:
   - 创建bootloader(启动器)
   - 嵌入Python解释器
   - 嵌入PYZ归档
   - 嵌入数据文件(config.json等)

4. **收集阶段(COLLECT)**:
   - 收集所有二进制文件(.dll, .so等)
   - 生成最终的exe文件

### 为什么需要hiddenimports?

有些模块是动态导入的,PyInstaller无法自动检测:

```python
# 显式导入 - PyInstaller能检测到
import selenium

# 隐式导入 - PyInstaller可能检测不到
module = __import__('selenium.webdriver.chrome.service')
```

所以需要在 `build.spec` 中手动声明这些隐式导入。

## 📝 检查清单

打包前确认:

- [ ] 所有代码已测试通过
- [ ] config.json已正确配置
- [ ] requirements.txt包含所有依赖
- [ ] build.spec配置正确
- [ ] 已安装PyInstaller (`pip install pyinstaller`)

打包后确认:

- [ ] exe文件在dist目录生成
- [ ] exe文件大小合理(80-120MB)
- [ ] 在本机测试运行正常
- [ ] 在其他电脑测试运行正常
- [ ] 配置文件可以正常读取

## 🚀 下一步

打包完成后,你可以:

1. **分发给同事使用**
2. **设置开机自启动**(将exe放入启动文件夹)
3. **创建快捷方式**(右键exe → 发送到 → 桌面快捷方式)
4. **编写使用手册**(参考README.md)

---

**最后更新**: 2026-06-15  
**作者**: AutoUpload Team
