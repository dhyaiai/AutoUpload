# 配置文件说明

## config.json 配置项详解

### 必填配置

#### 1. WEBSITE_URL (目标网站URL)
- **说明**: 七天网络数智作业系统的登录地址
- **示例**: `"https://school.7net.cn"` 或 `"https://homework.7net.cn"`
- **如何获取**: 用浏览器打开教学平台,复制地址栏的URL

#### 2. USERNAME (用户名)
- **说明**: 登录账号
- **示例**: `"teacher001"` 或 `"admin@school.com"`
- **注意**: 请确保该账号有上传作业的权限

#### 3. PASSWORD (密码)
- **说明**: 登录密码
- **示例**: `"YourPassword123"`
- **安全提示**: 不要将包含真实密码的文件分享给他人

#### 4. ROLE (角色类型) ⭐ 新增
- **说明**: 登录后需要选择的角色
- **可选值**:
  - `"admin"` 或 `"administrator"` 或 `"超级管理员"` → 选择超级管理员角色
  - `"teacher"` 或 `"老师"` → 选择老师角色(默认)
- **默认值**: `"admin"` (超级管理员)
- **建议**: 根据实际账号权限选择,通常使用超级管理员权限更大

#### 5. DEEPSEEK_API_KEY (DeepSeek API密钥)
- **说明**: AI科目识别所需的API密钥
- **获取方式**:
  1. 访问 https://platform.deepseek.com
  2. 注册并登录账号
  3. 进入"API密钥"页面
  4. 点击"创建新密钥",复制生成的密钥
- **示例**: `"sk-abc123def456ghi789..."`
- **费用**: DeepSeek提供免费额度,一般够用

### 可选配置

#### 6. ROOT_DIR (监控根目录)
- **说明**: 存放作业文件的根文件夹路径
- **默认值**: `"./作业文件夹"` (程序同目录下的"作业文件夹")
- **自定义**: 可以改为绝对路径,如 `"D:\\Homework\\Files"`

#### 7. CHROME_DRIVER_PATH (Chrome驱动路径)
- **说明**: ChromeDriver的完整路径
- **默认值**: `""` (空字符串,让Selenium自动管理驱动)
- **建议**: 保持为空,使用Selenium 4.x的自动驱动管理功能
- **手动指定**: 如果需要,可填写如 `"C:\\drivers\\chromedriver.exe"`

#### 8. FILE_STABLE_DELAY (文件稳定等待时间)
- **说明**: 检测到新文件后等待的时间(秒),确保文件写入完成
- **默认值**: `2`
- **调整建议**: 
  - 如果文件较大,可增加至 `3-5` 秒
  - 如果都是小文件,可减少至 `1` 秒

#### 9. BROWSER_IDLE_TIMEOUT (浏览器空闲超时)
- **说明**: 浏览器无操作多长时间后自动关闭(秒)
- **默认值**: `1800` (30分钟)
- **调整建议**: 
  - 长时间运行可设为 `3600` (1小时)
  - 短时间测试可设为 `300` (5分钟)

#### 10. MAX_RETRY_COUNT (最大重试次数)
- **说明**: 上传失败后最多重试几次
- **默认值**: `3`
- **范围**: `1-5`,建议不超过5次

#### 11. SLEEP_INTERVAL (操作间隔时间)
- **说明**: 每次浏览器操作之间的等待时间(秒)
- **默认值**: `0.5`
- **调整建议**:
  - 如果网站响应慢,可增加至 `1-2` 秒
  - 如果追求速度,可减少至 `0.3` 秒(可能不稳定)

---

## 配置示例

### 最小化配置(仅修改必要项)

```json
{
    "WEBSITE_URL": "https://your-school.7net.cn",
    "USERNAME": "your_username",
    "PASSWORD": "your_password",
    "ROLE": "admin",
    "DEEPSEEK_API_KEY": "sk-your_api_key_here"
}
```

### 完整配置(包含所有选项)

```json
{
    "ROOT_DIR": "./作业文件夹",
    "WEBSITE_URL": "https://your-school.7net.cn",
    "USERNAME": "teacher001",
    "PASSWORD": "SecurePass123!",
    "ROLE": "admin",
    "DEEPSEEK_API_KEY": "sk-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz",
    "CHROME_DRIVER_PATH": "",
    "FILE_STABLE_DELAY": 2,
    "BROWSER_IDLE_TIMEOUT": 1800,
    "MAX_RETRY_COUNT": 3,
    "SLEEP_INTERVAL": 0.5
}
```

---

## 常见问题

### Q1: 如何知道应该选"超级管理员"还是"老师"?
**A**: 
- 如果你的账号是管理员账号,选择 `"admin"`
- 如果是普通教师账号,选择 `"teacher"`
- 不确定时,先试 `"admin"`,如果报错再改 `"teacher"`

### Q2: 配置保存后需要重启程序吗?
**A**: 是的,修改 `config.json` 后需要重新启动程序才能生效。

### Q3: API密钥泄露了怎么办?
**A**: 
1. 立即在 DeepSeek 平台删除该密钥
2. 重新生成新密钥
3. 更新 `config.json`
4. **重要**: 不要将 `config.json` 提交到Git或其他公开平台

### Q4: 可以使用多个账号吗?
**A**: 当前版本只支持单账号。如需多账号,可以:
- 复制多份程序,每份配置不同账号
- 或等待后续版本支持多账号切换

### Q5: 配置文件放哪里?
**A**: `config.json` 必须和 `main.py` (或打包后的exe)放在同一目录下。

---

## 安全建议

️ **重要安全提醒**:

1. **保护配置文件**:
   - 不要将 `config.json` 上传到GitHub等公开平台
   - 已添加 `.gitignore` 规则自动忽略此文件

2. **定期更换密码**:
   - 建议每3-6个月更换一次登录密码
   - 同时更新 `config.json` 中的密码

3. **API密钥管理**:
   - 定期检查DeepSeek API使用情况
   - 设置使用限额防止超额消费

4. **文件权限**:
   - Windows: 右键 `config.json` → 属性 → 安全,限制访问用户
   - Linux/Mac: `chmod 600 config.json`

---

## 下一步

配置完成后,请运行:

```bash
python main.py
```

或在Windows上双击打包后的exe文件。

如有问题,请查看 [QUICKSTART.md](QUICKSTART.md) 快速开始指南。
