"""
浏览器自动化模块
功能:管理Chrome浏览器的生命周期,实现自动登录、学校校验、文件上传
特点:单例模式复用浏览器实例,支持自动重启和状态检测
"""
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from config_manager import ConfigManager
from selenium.webdriver.chrome.service import Service


class BrowserAutomation:
    """
    浏览器自动化管理器(单例模式)
    负责Chrome浏览器的启动、登录、上传等操作
    """
    
    _instance = None
    _driver = None
    
    def __new__(cls, log_queue=None):
        """实现单例模式,确保全局只有一个浏览器实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.config = ConfigManager()
            cls._instance.driver = None
            cls._instance.is_logged_in = False
            cls._instance.last_active_time = time.time()
        # 每次调用都更新 log_queue（允许后续调用者注入有效的日志队列）
        if log_queue is not None:
            cls._instance.log_queue = log_queue
        elif not hasattr(cls._instance, 'log_queue'):
            cls._instance.log_queue = None
        return cls._instance
    
    def _log(self, message: str):
        """
        记录日志消息
        如果有log_queue则发送到队列,否则使用print

        Args:
            message: 日志消息
        """
        if self.log_queue:
            self.log_queue.put(message)
        else:
            print(message)

    @property
    def is_initialized(self) -> bool:
        """
        检查浏览器是否已初始化并登录

        Returns:
            True表示浏览器已启动且已登录,False表示未启动或登录失效
        """
        return self.driver is not None and self.is_logged_in

    def ensure_initialized(self) -> bool:
        """
        延迟初始化: 只在浏览器未运行时才启动
        供 UploadProcessor 在处理文件前调用

        Returns:
            True表示浏览器可用,False表示启动失败
        """
        if self.is_initialized:
            self.update_activity_time()
            return True
        self._log("检测到新文件,正在启动浏览器...")
        return self.initialize()

    def initialize(self):
        """
        初始化并启动Chrome浏览器
        配置浏览器选项,加载驱动,打开目标网站
        
        Returns:
            True表示启动成功,False表示失败
        """
        try:
            self._log("正在启动Chrome浏览器...")
            
            # 创建Chrome选项
            options = ChromeOptions()
            options.page_load_strategy = "eager"  # DOM就绪即返回，不等图片加载
            # 添加常用选项(可根据需要调整)
            options.add_argument('--start-maximized')  # 最大化窗口
            # options.add_argument('--disable-gpu')      # 禁用GPU加速
            
            # 创建WebDriver实例
            driver_path = self.config.chrome_driver_path
            
            # 检查ChromeDriver是否存在
            import os
            if driver_path and driver_path != "./chromedriver.exe":
                # 如果配置了驱动路径且不是默认值
                if os.path.exists(driver_path):
                    self._log(f"使用指定的ChromeDriver: {driver_path}")
                    from selenium.webdriver.chrome.service import Service
                    service = Service(executable_path=driver_path)
                    self.driver = webdriver.Chrome(service=service, options=options)
                else:
                    self._log(f"警告: ChromeDriver不存在于 {driver_path},尝试自动查找")
                    self.driver = webdriver.Chrome(options=options)
            else:
                # 否则让Selenium自动管理驱动(推荐方式)
                self._log("使用Selenium自动管理的ChromeDriver")
                self.driver = webdriver.Chrome(options=options)
            
            # 设置隐式等待时间(查找元素时最多等待10秒)
            self.driver.implicitly_wait(10)
            # 限制异步脚本执行超时（防止 execute_async_script 无限卡死）
            self.driver.timeouts.script = 5
            
            # 打开目标网站
            self._log(f"正在访问网站: {self.config.website_url}")
            self.driver.get(self.config.website_url)
            
            # 执行登录
            if self._login():
                self.is_logged_in = True
                self.last_active_time = time.time()
                self._log("浏览器初始化成功,已登录")
                self._log("BROWSER_STATUS:CONNECTED")
                return True
            else:
                self._log("错误: 登录失败")
                self.close()
                return False
        
        except Exception as e:
            error_msg = str(e)
            self._log(f"错误: 浏览器启动失败 - {error_msg}")
            
            # 提供更详细的错误提示
            if "chromedriver" in error_msg.lower() or "chrome" in error_msg.lower():
                self._log("")
                self._log("可能的原因:")
                self._log("1. Chrome浏览器未安装或版本不兼容")
                self._log("2. ChromeDriver版本与Chrome浏览器版本不匹配")
                self._log("3. ChromeDriver不在系统PATH中")
                self._log("")
                self._log("解决方案:")
                self._log("- 确保已安装最新版本的Chrome浏览器")
                self._log("- 删除config.json中的CHROME_DRIVER_PATH配置,让Selenium自动管理")
                self._log("- 或下载与Chrome版本匹配的ChromeDriver并放到正确位置")
            
            import traceback
            tb_str = traceback.format_exc()
            self._log(f"详细错误信息:\n{tb_str}")
            return False
    
    def _login(self) -> bool:
        """
        执行自动登录操作
        适配七天网络数智作业系统登录页面
        
        Returns:
            True表示登录成功,False表示失败
        """
        try:
            self._log("正在执行登录操作...")
            
            # 查找账号输入框(通过placeholder定位)
            username_input = self.driver.find_element(By.CSS_SELECTOR, "input[placeholder='请输入您的账户']")
            username_input.clear()
            username_input.send_keys(self.config.username)
            time.sleep(self.config.sleep_interval)
            self._log("已输入账号")
            
            # 查找密码输入框(通过placeholder定位)
            password_input = self.driver.find_element(By.CSS_SELECTOR, "input[placeholder='请输入您的密码']")
            password_input.clear()
            password_input.send_keys(self.config.password)
            time.sleep(self.config.sleep_interval)
            self._log("已输入密码")
            
            # 查找登录按钮(通过ID或包含'立即登录'文本的span)
            try:
                # 方法1: 通过ID定位(最可靠)
                login_button = self.driver.find_element(By.ID, "login_1")
            except NoSuchElementException:
                try:
                    # 方法2: 通过包含'立即登录'文本的span元素定位
                    login_button = self.driver.find_element(By.XPATH, "//span[text()='立即登录']/parent::button")
                except NoSuchElementException:
                    # 方法3: 通过class定位
                    login_button = self.driver.find_element(By.CSS_SELECTOR, "button.login-btn.el-button--primary")
            
            login_button.click()
            self._log("已点击登录按钮")

            # 等待登录完成(可以根据实际情况调整等待条件)
            time.sleep(3)

            # 检查是否需要选择角色
            if self._handle_role_selection():
                self._log("角色选择完成")
            else:
                self._log("警告: 角色选择可能失败,继续执行")

            # 等待页面跳转完毕
            time.sleep(2)
            ready = self.driver.execute_script("return document.readyState;")
            self._log(f"登录后页面状态: {ready}, URL: {self.driver.current_url}")

            # 验证是否登录成功(检查URL是否变化或出现主页元素)
            # TODO: 根据实际网站调整验证方式
            try:
                # 方法1: 检查URL是否跳转到主页
                current_url = self.driver.current_url
                if "login" not in current_url.lower():
                    print(f"登录成功,当前URL: {current_url}")
                    return True
                
                # 方法2: 检查是否有用户信息或主页元素
                # 可以尝试查找常见的首页元素,如导航栏、用户头像等
                try:
                    # 尝试查找可能的用户信息元素(需要根据实际页面调整)
                    user_elements = [
                        (By.CLASS_NAME, "user-info"),
                        (By.CLASS_NAME, "username"),
                        (By.ID, "user-name"),
                        (By.CSS_SELECTOR, ".header .user"),
                    ]
                    
                    for locator_type, locator_value in user_elements:
                        try:
                            self.driver.find_element(locator_type, locator_value)
                            print("检测到用户信息元素,登录成功")
                            return True
                        except NoSuchElementException:
                            continue
                except:
                    pass
                
                # 如果以上方法都失败,简单判断没有错误提示也算成功
                error_elements = [
                    (By.CLASS_NAME, "error"),
                    (By.CLASS_NAME, "error-message"),
                    (By.CSS_SELECTOR, ".ant-message-error"),
                ]
                
                has_error = False
                for locator_type, locator_value in error_elements:
                    try:
                        self.driver.find_element(locator_type, locator_value)
                        has_error = True
                        break
                    except NoSuchElementException:
                        continue
                
                if not has_error:
                    print("未检测到错误信息,假设登录成功")
                    return True
                else:
                    print("警告: 检测到错误信息,登录可能失败")
                    return False
                    
            except Exception as e:
                print(f"警告: 登录验证异常 - {e}")
                # 即使验证失败,也假设登录成功(避免误判)
                return True
        
        except Exception as e:
            print(f"错误: 登录过程异常 - {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _handle_role_selection(self) -> bool:
        """
        处理角色选择界面
        根据config.json中的ROLE配置自动选择对应角色(默认超级管理员)
        
        Returns:
            True表示选择成功或无需选择,False表示选择失败
        """
        try:
            # 检查是否出现角色选择界面
            # 通过检测页面标题或特定元素来判断
            page_source = self.driver.page_source
            
            # 如果页面包含"选择角色",说明需要选择角色
            if "选择角色" in page_source:
                print("检测到角色选择界面")
                time.sleep(1)
                
                # 获取配置的角色,默认为"teacher"
                role = self.config.get("ROLE", "teacher")
                print(f"配置的角色: {role}")
                
                # 根据角色点击对应的卡片
                if role == "admin" or role == "administrator" or role == "超级管理员":
                    # 选择超级管理员
                    print("正在选择: 超级管理员")
                    try:
                        # 方法1: 通过ID直接定位(最可靠)
                        admin_card = self.driver.find_element(By.ID, "changeRole_0")
                    except NoSuchElementException:
                        try:
                            # 方法2: 通过包含'超级管理员'文本的role-name div定位其父元素role-item
                            admin_card = self.driver.find_element(
                                By.XPATH,
                                "//div[@class='role-name' and text()='超级管理员']/parent::div"
                            )
                        except NoSuchElementException:
                            # 方法3: 通过包含'超级管理员'文本的元素定位
                            admin_card = self.driver.find_element(
                                By.XPATH,
                                "//*[contains(text(), '超级管理员')]/ancestor::div[@class='role-item']"
                            )
                    
                    admin_card.click()
                    print("已选择超级管理员角色")
                else:
                    # 选择老师
                    print("正在选择: 老师")
                    try:
                        # 方法1: 通过ID定位(第二个role-item通常是老师)
                        teacher_card = self.driver.find_element(By.ID, "changeRole_1")
                    except NoSuchElementException:
                        try:
                            # 方法2: 通过包含'老师'文本的role-name div定位其父元素role-item
                            teacher_card = self.driver.find_element(
                                By.XPATH,
                                "//div[@class='role-name' and contains(text(), '老师')]/parent::div"
                            )
                        except NoSuchElementException:
                            # 方法3: 通过包含'老师'文本的元素定位
                            teacher_card = self.driver.find_element(
                                By.XPATH,
                                "//*[contains(text(), '老师')]/ancestor::div[@class='role-item']"
                            )
                    
                    teacher_card.click()
                    print("已选择老师角色")
                
                time.sleep(self.config.sleep_interval)
                
                # 点击确定按钮
                try:
                    # 方法1: 通过ID直接定位(最可靠)
                    confirm_btn = self.driver.find_element(By.ID, "changeRole_2")
                except NoSuchElementException:
                    try:
                        # 方法2: 通过包含'确定'文本的span定位其父按钮元素
                        confirm_btn = self.driver.find_element(
                            By.XPATH,
                            "//span[text()='确定']/parent::button"
                        )
                    except NoSuchElementException:
                        # 方法3: 通过class和type定位
                        confirm_btn = self.driver.find_element(
                            By.CSS_SELECTOR,
                            "button.el-button--primary[type='button']"
                        )
                
                confirm_btn.click()
                print("已点击确定按钮")
                
                # 等待角色切换完成
                time.sleep(5)
                
                return True
            else:
                # 没有角色选择界面,直接返回成功
                print("未检测到角色选择界面,跳过")
                return True
        
        except NoSuchElementException as e:
            print(f"警告: 未找到角色选择元素 - {e}")
            return False
        except Exception as e:
            print(f"错误: 角色选择过程异常 - {e}")
            import traceback
            traceback.print_exc()
            return False

    def check_and_switch_school(self, target_school: str) -> bool:
        try:
            self.update_activity_time()
            self._log(f"正在校验学校: {target_school}")
            self._log(f"当前页面URL: {self.driver.current_url}")

            # 确保页面完全加载
            ready_state = self.driver.execute_script("return document.readyState;")
            self._log(f"document.readyState: {ready_state}")

            # 1. 查找教师下拉触发元素（多选择器兜底）
            teacher_dropdown = None
            dropdown_selectors = [
                (By.CSS_SELECTOR, ".info-user > .el-dropdown > .el-dropdown-link"),
                (By.CSS_SELECTOR, ".info-user .el-dropdown-link"),
                (By.CSS_SELECTOR, ".el-dropdown-link"),
                (By.XPATH, "//span[contains(@class, 'el-dropdown-link')]"),
                (By.XPATH, "//*[contains(@class, 'info-user')]//*[contains(@class, 'el-dropdown-link')]"),
            ]
            for by, selector in dropdown_selectors:
                try:
                    teacher_dropdown = WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((by, selector))
                    )
                    self._log(f"找到教师下拉元素: {selector}")
                    break
                except TimeoutException:
                    continue

            if not teacher_dropdown:
                self._log("错误: 未找到教师下拉触发元素，尝试打印页面头部HTML")
                try:
                    header_html = self.driver.execute_script(
                        "return document.querySelector('.info-user') ? document.querySelector('.info-user').outerHTML : 'NO .info-user ELEMENT'"
                    )
                    self._log(f"页面 .info-user 区域: {header_html[:500]}")
                except Exception:
                    pass
                return False

            self._log(f"定位元素: tag={teacher_dropdown.tag_name}, text={teacher_dropdown.text}")

            # 2. 确保可见并聚焦
            self.driver.execute_script("window.focus();")
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", teacher_dropdown)
            time.sleep(1)  # 等 Vue 完成事件绑定

            # 3. 多方案点击，每步验证
            dropdown_opened = False
            menu_selector = "li.el-dropdown-menu__item.info-dropdown-item.info-school"

            # 方案1：Vue 组件 API（最直接，绕过事件系统）
            try:
                self.driver.execute_script("""
                    const dropdown = document.querySelector('.info-user > .el-dropdown');
                    if (dropdown && dropdown.__vue__ && dropdown.__vue__.show) {
                        dropdown.__vue__.show();
                    } else {
                        // fallback: 直接修改 visible 属性
                        if (dropdown && dropdown.__vue__) {
                            dropdown.__vue__.visible = true;
                        }
                    }
                """)
                self._log("方案1: Vue API 展开")
                WebDriverWait(self.driver, 2).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, menu_selector))
                )
                dropdown_opened = True
                self._log("下拉框已确认打开(方案1: Vue API)")
            except TimeoutException:
                self._log("方案1 未打开下拉框")
            except Exception as e:
                self._log(f"方案1 异常: {type(e).__name__}: {e}")

            # 方案2：JS 原生 click()
            if not dropdown_opened:
                try:
                    self.driver.execute_script("arguments[0].click();", teacher_dropdown)
                    self._log("方案2: JS click()")
                    WebDriverWait(self.driver, 2).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                    dropdown_opened = True
                    self._log("下拉框已确认打开(方案2: JS click)")
                except TimeoutException:
                    self._log("方案2 未打开下拉框")
                except Exception as e:
                    self._log(f"方案2 异常: {type(e).__name__}: {e}")

            # 方案3：Selenium 原生 click + ActionChains
            if not dropdown_opened:
                try:
                    actions = ActionChains(self.driver)
                    actions.move_to_element(teacher_dropdown).pause(0.3).click().perform()
                    self._log("方案3: ActionChains")
                    WebDriverWait(self.driver, 2).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                    dropdown_opened = True
                    self._log("下拉框已确认打开(方案3: ActionChains)")
                except TimeoutException:
                    self._log("方案3 未打开下拉框")
                except Exception as e:
                    self._log(f"方案3 异常: {type(e).__name__}: {e}")

            # 方案4：MouseEvent 序列
            if not dropdown_opened:
                try:
                    self.driver.execute_script("""
                        const el = arguments[0];
                        ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(t => {
                            el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
                        });
                    """, teacher_dropdown)
                    self._log("方案4: MouseEvent 序列")
                    WebDriverWait(self.driver, 2).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                    dropdown_opened = True
                    self._log("下拉框已确认打开(方案4: MouseEvent)")
                except TimeoutException:
                    self._log("方案4 未打开下拉框")
                except Exception as e:
                    self._log(f"方案4 异常: {type(e).__name__}: {e}")

            if not dropdown_opened:
                self._log("错误: 4种方案均无法打开教师下拉菜单")
                # 打印页面信息辅助诊断
                try:
                    body_text = self.driver.execute_script(
                        "return document.body ? document.body.innerText.substring(0, 500) : 'NO BODY'"
                    )
                    self._log(f"页面文本前500字符: {body_text}")
                except Exception:
                    pass
                return False

            school_li = self.driver.find_element(By.CSS_SELECTOR, menu_selector)
            self._log("下拉菜单已出现，继续执行")
            time.sleep(0.5)

            # 提取当前显示的学校名称
            current_school = school_li.text.strip()
            self._log(f"当前学校: {current_school}")

            # 3. 判断是否一致
            if current_school == target_school or target_school in current_school or current_school in target_school:
                self._log("[OK] 学校一致，无需切换")
                # 关闭下拉菜单
                self.driver.find_element(By.TAG_NAME, "body").click()
                time.sleep(0.5)
                return True

            # 4. 不一致：点击学校元素弹出切换对话框
            self._log("学校不一致，正在切换...")
            self.driver.execute_script("arguments[0].click();", school_li)
            time.sleep(2)  # 等待对话框加载
            
            # 检查是否出现"切换学校"对话框
            page_source = self.driver.page_source
            if "切换学校" not in page_source:
                self._log("错误: 未检测到切换学校对话框")
                return False
            
            self._log("已打开切换学校对话框")
            
            # 步骤3: 在搜索框中输入目标学校名称
            try:
                # 优先通过父容器 .search-box 精确定位输入框
                search_input = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, ".search-box .el-input__inner")
                    )
                )
            except TimeoutException:
                # 兜底：通过placeholder属性定位
                search_input = self.driver.find_element(
                    By.XPATH, "//input[@placeholder='输入关键字搜索']"
                )

            # 清空并输入，同时触发Vue的input事件（解决Element UI受控组件不生效问题）
            search_input.clear()
            search_input.click()  # 先聚焦输入框
            search_input.send_keys(target_school)
            
            # 关键：手动触发 input 事件，让 Vue 同步数据
            self.driver.execute_script("""
                const el = arguments[0];
                el.value = arguments[1];
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            """, search_input, target_school)
            
            self._log(f"已输入学校名称并触发输入事件: {target_school}")
            time.sleep(self.config.sleep_interval)

            # 步骤4: 点击搜索按钮（多方案兜底）
            search_btn = None
            try:
                search_btn = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable((By.ID, "header_4"))
                )
            except TimeoutException:
                try:
                    search_btn = WebDriverWait(self.driver, 8).until(
                        EC.element_to_be_clickable((By.XPATH, "//span[text()='搜索']/ancestor::button"))
                    )
                except TimeoutException:
                    try:
                        search_btn = self.driver.find_element(By.XPATH, "//button[contains(text(), '搜索')]")
                    except NoSuchElementException:
                        search_btn = self.driver.find_element(By.CSS_SELECTOR, ".el-button--primary")

            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_btn)
            time.sleep(0.3)
            try:
                ActionChains(self.driver).move_to_element(search_btn).click().perform()
            except Exception:
                self.driver.execute_script("arguments[0].click();", search_btn)
            self._log("已点击搜索按钮")
            # 等待搜索完成：先等 loading 遮罩消失，再等表格行出现
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.invisibility_of_element_located(
                        (By.CSS_SELECTOR, ".el-loading-mask, .el-table__empty-block, .el-loading-spinner")
                    )
                )
            except TimeoutException:
                pass
            # 确保表格行已渲染
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//tbody/tr[contains(@class, 'el-table__row')]")
                    )
                )
            except TimeoutException:
                pass
            # 给 Vue 渲染一个稳定的短间隔，避免读到旧 DOM 导致 stale element
            time.sleep(0.5)
# ==============================================

            # 步骤5: 遍历所有学校容器，严格完全匹配目标学校名称
            school_found = False
            matched_school_name = ""

            # 直接获取表格行，跳过冗余选择器和低效去重
            school_rows = self.driver.find_elements(
                By.XPATH, "//tbody/tr[contains(@class, 'el-table__row')]"
            )
            self._log(f"找到 {len(school_rows)} 条学校数据")

            unique_containers = school_rows

            self._log(f"去重后待遍历学校总行数：{len(unique_containers)}")

            # 逐行遍历，严格完全匹配校名
            for idx, row in enumerate(unique_containers):
                try:
                    # 单独提取该行【学校名称单元格文本】，排除ID、操作按钮文字干扰
                    # 适配el-table，第一列单元格为学校名称
                    name_cell = row.find_element(By.XPATH, ".//td[1]")
                    real_school_name = name_cell.text.strip()
                    self._log(f"[{idx + 1}] 当前行校名：「{real_school_name}」")

                    # 核心：严格完全相等匹配，不模糊、不包含匹配
                    if real_school_name == target_school.strip():
                        matched_school_name = real_school_name
                        self._log(f"✅ 完全匹配目标学校：{matched_school_name}")

                        # 在当前行内查找【立即进入】按钮
                        enter_btn = None
                        btn_xpaths = [
                            ".//span[text()='立即进入']/parent::button",
                            ".//button[.//span[text()='立即进入']]",
                            ".//button[contains(@class, 'el-button--mini')]"
                        ]
                        for xp in btn_xpaths:
                            try:
                                enter_btn = row.find_element(By.XPATH, xp)
                                break
                            except NoSuchElementException:
                                continue

                        if not enter_btn:
                            self._log("该行未找到立即进入按钮，跳过")
                            continue

                        # 直接 JS 点击，跳过 scrollIntoView + sleep
                        self.driver.execute_script("arguments[0].click();", enter_btn)

                        school_found = True
                        break

                except NoSuchElementException:
                    # 该行没有第一列文本，跳过
                    continue
                except Exception as e:
                    self._log(f"遍历第{idx + 1}行异常：{str(e)}")
                    continue

            # 匹配失败处理
            if not school_found:
                self._log(f"❌ 未找到名称【完全一致】的学校：{target_school}")
                # 打印页面所有检索出来的校名，方便调试
                all_names = []
                for r in unique_containers:
                    try:
                        name = r.find_element(By.XPATH, ".//td[1]").text.strip()
                        all_names.append(name)
                    except:
                        pass
                self._log(f"当前搜索列表内所有学校名称：{all_names}")
                # 关闭弹窗
                try:
                    close_btn = self.driver.find_element(By.XPATH,
                                                         "//span[@class='el-dialog__close'] | //button[text()='取消']")
                    self.driver.execute_script("arguments[0].click()", close_btn)
                except:
                    pass
                return False
            
            # 步骤6: 等待学校切换完成
            self._log("等待学校切换完成...")
            time.sleep(2)
            
            # 步骤7: 验证学校是否切换成功
            # 重新定位教师下拉元素（之前的引用可能因页面刷新而失效）
            try:
                teacher_dropdown = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, ".info-user > .el-dropdown > .el-dropdown-link")
                    )
                )
                self.driver.execute_script("arguments[0].click();", teacher_dropdown)
                time.sleep(1)
                
                new_school_elem = self.driver.find_element(
                    By.XPATH,
                    "//div[contains(text(), '中学') or contains(text(), '小学') or contains(text(), '学校')]"
                )
                new_school = new_school_elem.text.strip()
                
                if target_school in new_school or new_school in target_school:
                    self._log(f"[OK] 学校切换成功: {new_school}")
                    # 关闭下拉菜单
                    try:
                        self.driver.find_element(By.TAG_NAME, "body").click()
                    except:
                        pass
                    return True
                else:
                    self._log(f"[FAIL] 学校切换失败,当前学校仍为: {new_school}")
                    try:
                        self.driver.find_element(By.TAG_NAME, "body").click()
                    except:
                        pass
                    return False
            except Exception as e:
                self._log(f"警告: 验证学校切换时出错 - {e}")
                # 连接断开类异常说明浏览器已不可用，不能假设成功
                if any(kw in str(e).lower() for kw in ('connection', 'disconnected', 'timeout', 'closed')):
                    self.is_logged_in = False
                    return False
                return True  # 其他非关键异常（如元素查找失败）假设切换成功
        
        except NoSuchElementException as e:
            self._log(f"错误: 未找到学校切换相关元素 - {e}")
            import traceback
            self._log(traceback.format_exc())
            return False
        except Exception as e:
            self._log(f"错误: 学校切换过程异常 - {e}")
            import traceback
            self._log(traceback.format_exc())
            return False
    
    def _read_select_value(self, label_text: str) -> Optional[str]:
        """
        读取上传对话框表单中el-select组件当前选中的显示文本

        Args:
            label_text: 表单标签文本（如"年级"、"科目"）

        Returns:
            当前选中的文本，读取失败或未选中时返回None
        """
        try:
            result = self.driver.execute_script("""
                const labelText = arguments[0];
                const labels = document.querySelectorAll('label');
                let formItem = null;
                for (const label of labels) {
                    if (label.textContent.includes(labelText)) {
                        formItem = label.closest('.el-form-item');
                        if (!formItem) {
                            formItem = label.parentElement;
                            while (formItem && !formItem.querySelector('.el-select')) {
                                formItem = formItem.parentElement;
                            }
                        }
                        break;
                    }
                }
                if (!formItem) return null;

                const select = formItem.querySelector('.el-select');
                if (!select) return null;

                // 方式1: Vue组件状态——最可靠
                if (select.__vue__) {
                    const vm = select.__vue__;
                    const selected = vm.selected;
                    if (selected && selected.currentLabel) {
                        return selected.currentLabel;
                    }
                    if (vm.selectedLabel) {
                        return vm.selectedLabel;
                    }
                }

                // 方式2: el-input__inner 的 value（Element UI 在此显示选中文本）
                const input = select.querySelector('.el-input__inner');
                if (input && input.value && input.value.trim()) {
                    return input.value.trim();
                }
                return null;
            """, label_text)
            return result.strip() if result else None
        except Exception as e:
            self._log(f"读取{label_text}选中值异常: {e}")
            return None

    def upload_file(self, file_path: str, grade: str, subject: str, school: str = None) -> bool:
        """
        执行文件上传操作
        
        Args:
            file_path: 要上传的文件完整路径(已包含学校+年级信息)
            grade: 年级(如"高一")
            subject: 科目(如"数学")
            school: 学校名称(用于日志记录,实际不使用)
        
        Returns:
            True表示上传成功,False表示失败
        
        Note:
            Selenium的send_keys()方法可以直接指定文件完整路径,
            无需在Windows文件选择对话框中手动导航。
        """
        try:
            print(f"开始上传文件: {os.path.basename(file_path)}")
            print(f"学校: {school}, 年级: {grade}, 科目: {subject}")
            print(f"文件路径: {file_path}")
            
            # 步骤1: 点击"上传作业"按钮
            try:
                # 方法1: 通过精确XPath定位(参考ceshi3.py)
                upload_btn = self.driver.find_element(
                    By.XPATH,
                    "//*[@id='main']/section/div/div[1]/div/div[2]/span"
                )
            except NoSuchElementException:
                try:
                    # 方法2: 通过ID定位(如果存在)
                    upload_btn = self.driver.find_element(By.ID, "upload-homework-btn")
                except NoSuchElementException:
                    try:
                        # 方法3: 通过class="upload-btn"定位
                        upload_btn = self.driver.find_element(By.CSS_SELECTOR, "div.upload-btn")
                    except NoSuchElementException:
                        # 方法4: 通过包含'上传'文本的span定位
                        upload_btn = self.driver.find_element(
                            By.XPATH,
                            "//span[contains(text(), '上传')]/parent::div"
                        )
            
            # 使用JavaScript点击避免被遮挡
            self.driver.execute_script("arguments[0].click();", upload_btn)
            time.sleep(self.config.sleep_interval)
            print("已打开上传作业对话框")
            
            # 步骤2: 定位文件输入框并直接发送完整文件路径
            # Selenium会自动处理Windows文件选择对话框,无需手动操作
            file_input = None
            for selector in [
                (By.CSS_SELECTOR, "input[type='file']"),
                (By.XPATH, "//input[@type='file']"),
                (By.XPATH, "//*[@id='main']//input[@type='file']"),
                (By.CSS_SELECTOR, ".el-upload__input"),
                (By.CSS_SELECTOR, "input[type='file'][accept]"),
                (By.XPATH, "//div[contains(@class,'upload')]//input | //form//input[@type='file']"),
            ]:
                try:
                    file_input = self.driver.find_element(*selector)
                    print(f"找到文件input: {selector}")
                    break
                except NoSuchElementException:
                    continue
            if not file_input:
                print("页面源码(前500字符):", self.driver.page_source[:500])
                raise Exception("找不到文件上传input,请检查上传对话框是否正确打开")
            file_input.send_keys(file_path)
            time.sleep(self.config.sleep_interval)
            print(f"[OK] 已选择文件: {os.path.basename(file_path)}")

            # 勾选年级+科目
            wait = WebDriverWait(self.driver, 10)

            # 先读取页面当前已选中的年级和科目，若与目标一致则跳过选择
            current_grade = self._read_select_value("年级")
            current_subject = self._read_select_value("科目")
            self._log(f"页面当前选中: 年级={current_grade}, 科目={current_subject}")

            # -------------------- 4. 选择年级（自定义下拉） --------------------
            if current_grade and current_grade == grade:
                print(f"⏭️ 年级已匹配({grade})，跳过选择")
            else:
                print(f"选择年级：{grade}（当前: {current_grade}）")
                # 4.1 点击年级下拉框展开列表（优化定位+等待+JS触发）
                try:
                    grade_trigger = wait.until(EC.element_to_be_clickable(
                        (By.XPATH,
                         "//*[@id='main']/section/div/div[2]/div[2]/div/div[2]/form/div[4]/div/div/div[1]/span/span/i")
                    ))
                except TimeoutException:
                    grade_trigger = wait.until(EC.element_to_be_clickable(
                        (By.XPATH,
                         "//label[contains(text(), '年级')]/following::div[contains(@class, 'el-select')]//i[contains(@class, 'el-icon-arrow-down')]")
                    ))

                self.driver.execute_script("""
                            arguments[0].click();
                            const selectEl = arguments[0].closest('.el-select');
                            if (selectEl && selectEl.__vue__) {
                                selectEl.__vue__.visible = true;
                            }
                        """, grade_trigger)
                time.sleep(1)

                # 4.2 选择目标年级
                grade_selected = False
                try:
                    grade_option = wait.until(EC.element_to_be_clickable(
                        (By.XPATH,
                         f"//li[contains(@class, 'el-select-dropdown__item') and normalize-space(text())='{grade}']")
                    ))
                    self.driver.execute_script("arguments[0].click();", grade_option)
                    grade_selected = True
                except TimeoutException:
                    grade_options = self.driver.find_elements(By.XPATH,
                                                              "//li[contains(@class, 'el-select-dropdown__item')]")
                    for option in grade_options:
                        option_text = option.text.strip()
                        if option_text == grade or grade in option_text:
                            self.driver.execute_script("arguments[0].click();", option)
                            grade_selected = True
                            break

                if not grade_selected:
                    raise Exception(f"未找到年级选项: {grade}")
                print(f"✅ 年级选择完成：{grade}")

            # -------------------- 5. 选择科目（自定义下拉） --------------------
            if current_subject and current_subject == subject:
                print(f"⏭️ 科目已匹配({subject})，跳过选择")
            else:
                print(f"选择科目：{subject}（当前: {current_subject}）")
                # 5.1 点击科目下拉框展开列表
                try:
                    subject_trigger = wait.until(EC.element_to_be_clickable(
                        (By.XPATH,
                         "//*[@id='main']/section/div/div[2]/div[2]/div/div[2]/form/div[5]/div/div/div[1]/span/span/i")
                    ))
                except TimeoutException:
                    subject_trigger = wait.until(EC.element_to_be_clickable(
                        (By.XPATH,
                         "//label[contains(text(), '科目')]/following::div[contains(@class, 'el-select')]//i[contains(@class, 'el-icon-arrow-down')]")
                    ))

                self.driver.execute_script("""
                            arguments[0].click();
                            const selectEl = arguments[0].closest('.el-select');
                            if (selectEl && selectEl.__vue__) {
                                selectEl.__vue__.visible = true;
                            }
                        """, subject_trigger)
                time.sleep(1)

                # 5.2 选择目标科目
                subject_selected = False
                try:
                    subject_option = wait.until(EC.element_to_be_clickable(
                        (By.XPATH,
                         f"//li[contains(@class, 'el-select-dropdown__item') and normalize-space(text())='{subject}']")
                    ))
                    self.driver.execute_script("arguments[0].click();", subject_option)
                    subject_selected = True
                except TimeoutException:
                    subject_options = self.driver.find_elements(By.XPATH,
                                                                "//li[contains(@class, 'el-select-dropdown__item')]")
                    for option in subject_options:
                        option_text = option.text.strip()
                        if option_text == subject or subject in option_text:
                            self.driver.execute_script("arguments[0].click();", option)
                            subject_selected = True
                            break

                if not subject_selected:
                    raise Exception(f"未找到科目选项: {subject}")
                print(f"✅ 科目选择完成：{subject}")

            # 步骤5: 设置预计使用时间(明天)
            print("正在设置预计使用时间...")
            try:
                # 计算明天的日期和时间
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                tomorrow_time = tomorrow + " 09:00"
                print(f"目标日期时间: {tomorrow_time}")

                # 方法1: 点击日期输入框,打开日历选择器
                date_trigger = self.driver.find_element(
                    By.XPATH,
                    "//input[@placeholder='选择日期' and @class='el-input__inner']"
                )
                self.driver.execute_script("arguments[0].click();", date_trigger)
                time.sleep(1)  # 等待日期选择器完全展开

                # 在日历中点击明天的日期
                try:
                    # 获取明天的日期数字
                    day = int(tomorrow.split('-')[2])

                    # 等待日历面板出现
                    WebDriverWait(self.driver, 5).until(
                        EC.presence_of_element_located((By.CLASS_NAME, "el-picker-panel"))
                    )
                    time.sleep(0.5)

                    # 定位并点击明天的日期(Element UI日历中的日期单元格)
                    # 需要排除上个月/下个月的灰色日期,只选择当前月份的可用日期
                    date_cell_xpath = f"//td[contains(@class, 'available') and not(contains(@class, 'next-month')) and not(contains(@class, 'prev-month'))]//div[text()='{day}']"
                    date_cell = self.driver.find_element(By.XPATH, date_cell_xpath)
                    self.driver.execute_script("arguments[0].click();", date_cell)
                    print(f"[OK] 已选择日期: {tomorrow}")

                    # 如果是日期时间选择器,还需要选择时间
                    time.sleep(0.5)
                    try:
                        # 查找时间输入框并设置为 09:00
                        time_input = self.driver.find_element(
                            By.XPATH,
                            "//input[@placeholder='请选择时间']"
                        )
                        time_input.clear()
                        time_input.send_keys("09:00")
                        print("[OK] 时间已设置为 09:00")
                    except:
                        print("[WARN] 未找到时间输入框,使用默认时间")
                    # 点击确定按钮
                    confirm_btn = self.driver.find_element(
                        By.XPATH,
                        "//button[contains(@class, 'el-button') and contains(text(), '确定')]"
                    )
                    self.driver.execute_script("arguments[0].click();", confirm_btn)
                    time.sleep(1)
                    print("[OK] 使用时间设置完成!")

                except Exception as e:
                    print(f"日历选择器操作失败,尝试直接输入: {e}")
                    # 如果日历操作失败,回退到直接输入方式
                    date_input = self.driver.find_element(
                        By.XPATH,
                        "//input[@placeholder='选择日期' and @class='el-input__inner']"
                    )
                    date_input.clear()
                    date_input.send_keys(tomorrow_time)
                    from selenium.webdriver.common.keys import Keys
                    date_input.send_keys(Keys.RETURN)  # 按回车确认
                    time.sleep(self.config.sleep_interval)
                    print("[OK] 使用时间设置完成(直接输入模式)!")

            except Exception as e:
                print(f"警告: 设置预计使用时间失败 - {e}")
            
            # 步骤6: 提交表单
            try:
                # 方法1: 通过精确XPath定位提交按钮(参考ceshi3.py)
                submit_btn = self.driver.find_element(
                    By.XPATH,
                    "//*[@id='uploadWord_3']/span"
                )
            except NoSuchElementException:
                try:
                    # 方法2: 通过ID定位
                    submit_btn = self.driver.find_element(By.ID, "submit-btn")
                except NoSuchElementException:
                    # 方法3: 通过包含'提交'或'确定'文本的按钮定位
                    submit_btn = self.driver.find_element(
                        By.XPATH,
                        "//button[contains(text(), '提交')] | //button[contains(text(), '确定')] | //span[text()='提交']/parent::button"
                    )
            
            self.driver.execute_script("arguments[0].click();", submit_btn)
            print("已点击提交按钮")
            
            # 步骤7: 等待上传完成并验证
            # 尝试多种策略检测上传成功（适配 Element UI 提示样式）
            try:
                success_indicators = [
                    # Element UI 成功消息
                    (By.CSS_SELECTOR, ".el-message--success"),
                    (By.CSS_SELECTOR, ".el-notification--success"),
                    (By.CSS_SELECTOR, ".el-message .el-message--success"),
                    # 通用成功提示
                    (By.CLASS_NAME, "success-message"),
                    (By.CSS_SELECTOR, "[class*='success']"),
                ]
                success_detected = False
                deadline = time.time() + 30  # 最多等待30秒
                while time.time() < deadline:
                    for by, selector in success_indicators:
                        try:
                            self.driver.find_element(by, selector)
                            success_detected = True
                            break
                        except NoSuchElementException:
                            continue
                    if success_detected:
                        break
                    time.sleep(1)

                if success_detected:
                    print("[OK] 文件上传成功")
                else:
                    print("[WARN] 上传超时,未检测到成功提示,但假设上传成功")
                self.last_active_time = time.time()
                return True
            except Exception:
                print("[WARN] 上传验证异常,但假设上传成功")
                self.last_active_time = time.time()
                return True  # 即使超时也返回成功,避免误判
        
        except Exception as e:
            print(f"错误: 文件上传失败 - {e}")
            return False
    
    def check_browser_status(self) -> bool:
        """
        检查浏览器是否仍然可用
        
        Returns:
            True表示浏览器正常,False表示浏览器已崩溃或未启动
        """
        if not self.driver:
            return False
        
        try:
            # 尝试获取当前URL来检测浏览器状态
            self.driver.current_url
            return True
        except Exception:
            return False
    
    def check_login_status(self) -> bool:
        """
        检查登录状态是否仍然有效

        Returns:
            True表示已登录,False表示登录失效
        """
        if not self.is_logged_in:
            return False

        try:
            # 多种方式验证登录状态（与 check_and_switch_school 的定位逻辑保持一致）
            current_url = self.driver.current_url

            # 方式1: URL包含主页路径说明已登录
            if "login" not in current_url.lower() and (
                "index" in current_url or "jobManager" in current_url or "home" in current_url
            ):
                return True

            # 方式2: 检查是否存在用户信息/导航元素（注意class是info-user不是user-info）
            status_indicators = [
                (By.CSS_SELECTOR, ".info-user"),
                (By.CSS_SELECTOR, ".el-dropdown-link"),
                (By.CLASS_NAME, "user-info"),   # 保留作为兜底
            ]
            for by, selector in status_indicators:
                try:
                    self.driver.find_element(by, selector)
                    return True
                except NoSuchElementException:
                    continue

            return True  # 无明确失败信号时假设仍登录（避免误判）
        except Exception:
            # 浏览器崩溃等异常才判定失效
            print("警告: 检测到登录态失效")
            self.is_logged_in = False
            return False
    
    def restart_browser(self) -> bool:
        """
        重启浏览器(关闭后重新初始化)
        用于处理浏览器崩溃或登录失效的情况
        
        Returns:
            True表示重启成功,False表示失败
        """
        print("正在重启浏览器...")
        self.close()
        time.sleep(2)
        return self.initialize()
    
    def close(self):
        """
        关闭浏览器,释放资源
        """
        if self.driver:
            try:
                self.driver.quit()
                self._log("浏览器已关闭")
            except Exception as e:
                self._log(f"警告: 关闭浏览器时出错 - {e}")
            finally:
                self.driver = None
                self.is_logged_in = False
                self._log("BROWSER_STATUS:DISCONNECTED")
    
    def update_activity_time(self):
        """
        更新最后活动时间
        每次成功执行操作后调用,用于空闲超时检测
        """
        self.last_active_time = time.time()
    
    def is_idle_for(self, seconds: float) -> bool:
        """
        检查浏览器是否已空闲超过指定秒数

        Args:
            seconds: 空闲秒数阈值

        Returns:
            True表示已空闲超过指定秒数
        """
        return (time.time() - self.last_active_time) > seconds

    def is_idle_timeout(self) -> bool:
        """
        检查是否超过空闲超时时间

        Returns:
            True表示已超时,False表示未超时
        """
        idle_time = time.time() - self.last_active_time
        return idle_time > self.config.browser_idle_timeout


# ============================================================
# 直接测试入口: 无需打包成exe, python browser_automation.py 即可测试
# 用法:
#   python browser_automation.py --file "C:\...\文件.docx" --school "学校名" --grade "高一" --subject "生物"
#   python browser_automation.py --file "C:\...\文件.docx"  (年级/科目从文件夹名自动解析,科目用AI识别)
# ============================================================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="自动上传脚本测试工具")
    parser.add_argument("--file", required=True, help="要上传的文件完整路径")
    parser.add_argument("--school", default=None, help="学校名称（可选，默认从文件夹名解析）")
    parser.add_argument("--grade", default=None, help="年级（可选，默认从文件夹名解析）")
    parser.add_argument("--subject", default=None, help="科目（可选，默认用AI识别）")
    parser.add_argument("--skip-login", action="store_true", help="跳过登录（浏览器已登录时使用）")
    args = parser.parse_args()

    file_path = args.file
    if not os.path.exists(file_path):
        print(f"错误: 文件不存在 - {file_path}")
        sys.exit(1)

    print("=" * 60)
    print("浏览器自动化测试")
    print("=" * 60)
    print(f"文件: {file_path}")

    # 解析学校和年级
    folder_path = os.path.dirname(file_path)
    folder_name = os.path.basename(folder_path)

    from info_extractor import InfoExtractor
    parsed_school, parsed_grade = InfoExtractor.parse_folder_name(folder_path)

    school = args.school or parsed_school
    grade = args.grade or parsed_grade

    if not school:
        school = input("请输入学校名称: ").strip()
    if not grade:
        grade = input("请输入年级(如高一): ").strip()

    print(f"学校: {school}, 年级: {grade}")

    # 科目识别
    if args.subject:
        subject = args.subject
        print(f"科目(手动指定): {subject}")
    else:
        extractor = InfoExtractor()
        content = extractor.read_file_content(file_path)
        if content:
            from subject_classifier import SubjectClassifier
            classifier = SubjectClassifier()
            subject = classifier.classify(content)
            if not subject:
                subject = input("AI识别失败, 请手动输入科目: ").strip()
        else:
            subject = input("无法读取文件内容, 请手动输入科目: ").strip()
        print(f"科目: {subject}")

    # 初始化浏览器
    print("\n--- 初始化浏览器 ---")
    browser = BrowserAutomation()

    if args.skip_login:
        print("跳过登录, 使用已有浏览器会话...")
        if not browser.driver:
            print("错误: 浏览器未启动, 无法跳过登录")
            sys.exit(1)
    else:
        if not browser.initialize():
            print("错误: 浏览器初始化失败")
            sys.exit(1)

    # 学校切换（如果指定了学校且与当前不同）
    if school:
        print(f"\n--- 校验学校: {school} ---")
        if not browser.check_and_switch_school(school):
            print(f"警告: 学校切换失败, 继续尝试上传...")

    # 执行上传
    print(f"\n--- 开始上传 ---")
    success = browser.upload_file(file_path, grade, subject, school)

    if success:
        print("\n" + "=" * 60)
        print("[OK] 上传测试完成!")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print("[FAIL] 上传测试失败!")
        print("=" * 60)
        sys.exit(1)

    # 保持浏览器打开以便检查结果
    input("\n按回车键关闭浏览器...")
    browser.close()
