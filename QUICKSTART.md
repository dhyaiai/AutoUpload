# 快速开始指南

## 🚀 5分钟快速上手

### 第一步: 安装依赖

打开命令行,进入项目目录:

```bash
cd D:\python-project\AutoUpload
pip install -r requirements.txt
```

### 第二步: 配置程序

复制 `config.json.example` 为 `config.json`:

```bash
copy config.json config.json
```

然后编辑 `config.json`,填写以下关键信息:

```json
{
    "WEBSITE_URL": "你的教学平台网址",
    "USERNAME": "你的账号",
    "PASSWORD": "你的密码",
    "DEEPSEEK_API_KEY": "你的DeepSeek API密钥"
}
```

**如何获取DeepSeek API密钥:**
1. 访问 https://platform.deepseek.com
2. 注册账号并登录
3. 在"API密钥"页面创建新密钥
4. 复制密钥到配置文件

### 第三步: 定制浏览器选择器(重要!)

由于每个网站的HTML结构不同,你需要修改 `browser_automation.py` 中的元素选择器。

**如何找到正确的选择器:**

1. 用Chrome打开你的教学平台
2. 按 `F12` 打开开发者工具
3. 点击左上角的箭头图标(或按Ctrl+Shift+C)
4. 点击页面上的元素(如用户名输入框)
5. 在开发者工具中查看元素的属性(name、id、class等)
6. 修改代码中对应的选择器

**需要修改的位置:**

```python
# browser_automation.py 第80-110行左右

# 登录相关
username_input = self.driver.find_element(By.NAME, "username")  # 改成实际的name
password_input = self.driver.find_element(By.NAME, "password")  # 改成实际的name
login_button = self.driver.find_element(By.ID, "login-btn")     # 改成实际的id

# 上传相关
upload_btn = self.driver.find_element(By.ID, "upload-homework-btn")
file_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
grade_select = self.driver.find_element(By.ID, "grade-select")
subject_select = self.driver.find_element(By.ID, "subject-select")
```

### 第四步: 运行程序

```bash
python main.py
```

程序启动后会显示GUI界面。

### 第五步: 测试上传

1. **创建测试文件夹**:
   - 在GUI中输入学校名称(如"测试中学")
   - 选择年级(如"高一")
   - 点击"创建"按钮

2. **放入测试文件**:
   - 在 `作业文件夹/测试中学高一/` 目录下放入一个txt文件
   - 文件内容包含明显的科目关键词(如"数学题"、"方程式"等)

3. **观察日志**:
   - GUI日志区域会显示检测和上传过程
   - 如果一切正常,会显示"✓ 上传成功"

---

## 🔧 常见问题排查

### 问题1: 程序启动后立即关闭

**原因**: 缺少依赖包  
**解决**: 
```bash
pip install -r requirements.txt
```

### 问题2: 浏览器无法启动

**原因**: Chrome未安装或版本不匹配  
**解决**:
1. 安装最新版Google Chrome
2. 删除配置中的 `CHROME_DRIVER_PATH` 让Selenium自动管理

### 问题3: 登录失败

**原因**: 选择器不正确  
**解决**: 
1. 使用开发者工具检查登录表单的元素
2. 修改 `browser_automation.py` 中的选择器
3. 重新运行程序

### 问题4: API调用失败

**原因**: API密钥错误或网络问题  
**解决**:
1. 检查 `DEEPSEEK_API_KEY` 是否正确
2. 确认能访问 https://api.deepseek.com
3. 查看日志中的具体错误信息

### 问题5: 找不到上传按钮

**原因**: 网站结构与预期不同  
**解决**:
1. 手动登录网站,找到上传作业的页面
2. 使用开发者工具检查上传按钮的元素
3. 修改对应的选择器

---

## 💡 使用技巧

### 1. 批量处理文件

将多个文件依次放入不同的"学校+年级"文件夹,程序会自动排队处理。

### 2. 查看失败原因

在GUI的"上传失败文件"列表中可以看到详细的失败原因,方便排查问题。

### 3. 重新上传

对于失败的文件,点击"🔄重新上传"按钮即可重试,最多重试3次。

### 4. 清空测试数据

测试完成后,在文件夹列表中点击"🧹清空"可以删除所有文件和数据库记录。

### 5. 日志保存

日志会自动保存到 `app.log` 文件,方便后续查看。

---

## 📝 下一步

- 阅读 [README.md](README.md) 了解完整功能
- 阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 了解架构设计
- 根据实际网站调整浏览器选择器
- 打包为exe文件分发给其他用户

---

## 🆘 需要帮助?

如果遇到无法解决的问题:

1. 查看日志文件 `app.log` 中的详细错误信息
2. 检查控制台输出的错误消息
3. 确认所有配置项都已正确填写
4. 尝试简化测试场景(如先用txt文件测试)

---

**祝你使用愉快! 🎉**
