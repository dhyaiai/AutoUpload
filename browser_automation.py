"""
浏览器自动化模块
功能:管理Chrome浏览器的生命周期,实现自动登录、学校校验、文件上传
特点:单例模式复用浏览器实例,支持自动重启和状态检测
"""
import os
import re
import sys
import time
import threading
from datetime import datetime, timedelta
from typing import Optional
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.common.keys import Keys
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
            cls._instance.last_upload_error = ""  # 最近一次 upload_file 的 error_text
            cls._instance._lock = threading.RLock()  # 保护浏览器生命周期操作的线程安全
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
        线程安全：加锁防止多线程同时初始化浏览器

        Returns:
            True表示浏览器可用,False表示启动失败
        """
        with self._lock:
            if self.is_initialized:
                # 验证浏览器是否真的还活着（用户可能手动关闭了浏览器）
                if self.check_browser_status():
                    self.update_activity_time()
                    return True
                else:
                    self._log("检测到浏览器已被手动关闭，将重新初始化...")
                    self.is_logged_in = False
                    self.driver = None
            self._log("检测到新文件,正在启动浏览器...")
            return self.initialize()

    def initialize(self):
        """
        初始化并启动Chrome浏览器
        配置浏览器选项,加载驱动,打开目标网站
        线程安全：加锁防止多线程同时初始化

        优化：如果配置了 CHROME_PROFILE_DIR，使用持久化用户目录，
        浏览器重启后 cookie/session 保留，自动恢复登录态，
        初始化时间从 ~60s 降至 ~10s。

        Returns:
            True表示启动成功,False表示失败
        """
        # 注意：调用者（ensure_initialized/restart_browser）可能已持有 _lock，
        # 此处用 RLock 允许同一线程重入。
        with self._lock:
            try:
                # 防御：初始化前先关闭已存在的残留 driver（如上次异常未清理的）
                if self.driver is not None:
                    try:
                        self.driver.quit()
                    except Exception:
                        pass
                    self.driver = None
                    self.is_logged_in = False

                self._log("正在启动Chrome浏览器...")

                # 创建Chrome选项
                options = ChromeOptions()
                options.page_load_strategy = "eager"  # DOM就绪即返回，不等图片加载
                options.add_argument('--start-maximized')  # 最大化窗口

                # 持久化用户数据目录：浏览器重启后保留登录 Cookie，跳过重新登录
                profile_dir = self.config.chrome_profile_dir
                if profile_dir:
                    os.makedirs(profile_dir, exist_ok=True)
                    options.add_argument(f'--user-data-dir={profile_dir}')
                    self._log(f"使用Chrome用户数据目录: {profile_dir}")
                else:
                    self._log("未配置CHROME_PROFILE_DIR，使用临时会话（每次重启需重新登录）")

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

                # 低隐式等待(2s)：Vue 组件异步渲染需要短暂余量，但不能设为10s
                # 否则每次 find_element 最多等10s，与 WebDriverWait 叠加后
                # 登录→角色选择→学校校验全链路累计 60~90s 卡顿
                self.driver.implicitly_wait(2)
                # 限制异步脚本执行超时（防止 execute_async_script 无限卡死）
                self.driver.timeouts.script = 5

                # 打开目标网站
                self._log(f"正在访问网站: {self.config.website_url}")
                self.driver.get(self.config.website_url)

                # ── 免登录检测：持久化 profile 恢复后可能已有有效 session ──
                if self._detect_existing_session():
                    self.is_logged_in = True
                    self.last_active_time = time.time()
                    self._log("检测到已有登录会话，跳过登录流程")
                    self._log("BROWSER_STATUS:CONNECTED")
                    return True

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

                # 清理残留 driver（初始化中途失败时 driver 可能已部分创建）
                self.close()
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

            # 等待登录表单渲染完成（Vue/ElementUI 在 DOM ready 后异步渲染，
            # 仅靠 driver.get() 的 eager 策略不够，需显式等待表单元素就位）
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "input[placeholder='请输入您的账户']")
                    )
                )
            except TimeoutException:
                self._log("错误: 登录表单未能在15秒内加载")
                return False

            # 查找账号输入框(通过placeholder定位)
            username_input = self.driver.find_element(By.CSS_SELECTOR, "input[placeholder='请输入您的账户']")
            username_input.clear()
            # 用 JS 直接设值 + dispatch input 事件，绕过 send_keys 逐字符开销
            # Element UI 的 v-model 监听 input 事件，手动 dispatch 即可触发响应
            # JS dispatchEvent 是同步的，无需 sleep
            self.driver.execute_script("""
                var el = arguments[0];
                el.value = arguments[1];
                el.dispatchEvent(new InputEvent('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            """, username_input, self.config.username)
            self._log("已输入账号")

            # 查找密码输入框(通过placeholder定位)
            password_input = self.driver.find_element(By.CSS_SELECTOR, "input[placeholder='请输入您的密码']")
            password_input.clear()
            self.driver.execute_script("""
                var el = arguments[0];
                el.value = arguments[1];
                el.dispatchEvent(new InputEvent('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            """, password_input, self.config.password)
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

            # 等待登录完成：URL跳转离开login页，或出现角色选择页，或出现登录错误
            # 使用 innerText 替代 page_source 避免每次轮询下载完整HTML（~30次/15s）
            try:
                WebDriverWait(self.driver, 15).until(
                    lambda d: (
                        "login" not in d.current_url.lower()
                        or "选择角色" in d.execute_script(
                            "return document.body ? document.body.innerText : ''")
                        or d.find_elements(By.CSS_SELECTOR, ".el-message--error, .el-form-item__error")
                    )
                )
            except TimeoutException:
                self._log("警告: 登录等待超时(15s)，继续检查状态...")

            # 检查是否需要选择角色
            if self._handle_role_selection():
                self._log("角色选择完成")
            else:
                self._log("警告: 角色选择可能失败,继续执行")

            # 等待页面就绪（最多10秒）
            try:
                WebDriverWait(self.driver, 10).until(
                    lambda d: d.execute_script("return document.readyState;") == "complete"
                )
            except TimeoutException:
                pass
            self._log(f"登录后页面状态: URL={self.driver.current_url}")

            # 验证是否登录成功(检查URL是否变化或出现主页元素)
            try:
                # 方法1: 检查URL是否跳转到主页
                current_url = self.driver.current_url
                if "login" not in current_url.lower():
                    self._log(f"登录成功,当前URL: {current_url}")
                    return True

                # 方法2: 检查是否有用户信息或主页元素
                try:
                    user_elements = [
                        (By.CLASS_NAME, "user-info"),
                        (By.CLASS_NAME, "username"),
                        (By.ID, "user-name"),
                        (By.CSS_SELECTOR, ".header .user"),
                    ]

                    for locator_type, locator_value in user_elements:
                        try:
                            self.driver.find_element(locator_type, locator_value)
                            self._log("检测到用户信息元素,登录成功")
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
                    self._log("未检测到错误信息,假设登录成功")
                    return True
                else:
                    self._log("警告: 检测到错误信息,登录可能失败")
                    return False

            except Exception as e:
                self._log(f"警告: 登录验证异常 - {e}")
                # 即使验证失败,也假设登录成功(避免误判)
                return True

        except Exception as e:
            self._log(f"错误: 登录过程异常 - {e}")
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
                self._log("检测到角色选择界面")

                # 等待角色卡片可点击（替代 time.sleep(1)）
                try:
                    WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, ".role-item, [id^='changeRole_']"))
                    )
                except TimeoutException:
                    self._log("警告: 等待角色卡片超时，继续尝试...")

                # 获取配置的角色,默认为"teacher"
                role = self.config.get("ROLE", "teacher")
                self._log(f"配置的角色: {role}")

                # 根据角色点击对应的卡片
                if role == "admin" or role == "administrator" or role == "超级管理员":
                    # 选择超级管理员
                    self._log("正在选择: 超级管理员")
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
                    self._log("已选择超级管理员角色")
                else:
                    # 选择老师
                    self._log("正在选择: 老师")
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
                    self._log("已选择老师角色")

                # 等待确定按钮可点击（替代 time.sleep(self.config.sleep_interval)）
                try:
                    WebDriverWait(self.driver, 3).until(
                        EC.element_to_be_clickable((By.ID, "changeRole_2"))
                    )
                except TimeoutException:
                    try:
                        WebDriverWait(self.driver, 3).until(
                            EC.element_to_be_clickable(
                                (By.XPATH, "//span[text()='确定']/parent::button")
                            )
                        )
                    except TimeoutException:
                        pass

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
                self._log("已点击确定按钮")

                # 等待角色选择界面消失（替代 time.sleep(5)）
                try:
                    WebDriverWait(self.driver, 10).until(
                        lambda d: "选择角色" not in d.page_source
                    )
                    self._log("角色选择界面已关闭")
                except TimeoutException:
                    self._log("警告: 等待角色选择界面关闭超时(10s)，继续执行")

                return True
            else:
                # 没有角色选择界面,直接返回成功
                self._log("未检测到角色选择界面,跳过")
                return True

        except NoSuchElementException as e:
            self._log(f"警告: 未找到角色选择元素 - {e}")
            return False
        except Exception as e:
            self._log(f"错误: 角色选择过程异常 - {e}")
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

            # 0. 前置检查：如果当前在登录页，直接判定会话丢失
            current_url = self.driver.current_url
            if "login" in current_url.lower() or "/login" in current_url:
                self._log(f"前置检测: 当前页面为登录页({current_url})，会话已丢失")
                self.is_logged_in = False
                return False

            # 0.5 扫描页面文本中的会话丢失关键词（页面可能未跳转但显示了踢下线通知）
            if self._detect_session_lost_on_page():
                self._log("前置检测: 页面文本含会话丢失信号，中止学校校验")
                return False

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
            # 关键：后续方案使用 JS click 会 toggle 下拉状态，所以每步先检查是否已经打开了
            dropdown_opened = False
            menu_selector = "li.el-dropdown-menu__item.info-dropdown-item.info-school"

            def _is_dropdown_visible():
                """检查下拉菜单是否已可见（避免重复点击导致toggle关闭）"""
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, menu_selector)
                    return el.is_displayed()
                except Exception:
                    return False

            # 先检查下拉是否已经处于打开状态（上一轮操作可能残留）
            if _is_dropdown_visible():
                dropdown_opened = True
                self._log("下拉框已处于打开状态，跳过点击")

            # 方案1：Vue 组件 API（最直接，绕过事件系统，不会toggle）
            if not dropdown_opened:
                try:
                    self.driver.execute_script("""
                        const dropdown = document.querySelector('.info-user > .el-dropdown');
                        if (dropdown && dropdown.__vue__) {
                            if (dropdown.__vue__.show) {
                                dropdown.__vue__.show();
                            } else {
                                dropdown.__vue__.visible = true;
                            }
                        }
                    """)
                    self._log("方案1: Vue API 展开")
                    WebDriverWait(self.driver, 3).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                    dropdown_opened = True
                    self._log("下拉框已确认打开(方案1: Vue API)")
                except TimeoutException:
                    self._log("方案1 未打开下拉框")
                except Exception as e:
                    self._log(f"方案1 异常: {type(e).__name__}: {e}")

            # 方案2：JS 原生 click()（注意：这是toggle操作！使用前先确保下拉未打开）
            if not dropdown_opened:
                try:
                    self.driver.execute_script("arguments[0].click();", teacher_dropdown)
                    self._log("方案2: JS click()")
                    WebDriverWait(self.driver, 3).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                    dropdown_opened = True
                    self._log("下拉框已确认打开(方案2: JS click)")
                except TimeoutException:
                    self._log("方案2 未打开下拉框")
                    # 方案2可能toggle关闭了方案1打开的下拉，用Vue API重新打开
                    try:
                        self.driver.execute_script("""
                            const dropdown = document.querySelector('.info-user > .el-dropdown');
                            if (dropdown && dropdown.__vue__) {
                                dropdown.__vue__.visible = true;
                            }
                        """)
                        if _is_dropdown_visible():
                            dropdown_opened = True
                            self._log("方案2回退: Vue API重新打开成功")
                    except Exception:
                        pass
                except Exception as e:
                    self._log(f"方案2 异常: {type(e).__name__}: {e}")

            # 方案3：Selenium 原生 click + ActionChains（同样会toggle）
            if not dropdown_opened:
                try:
                    actions = ActionChains(self.driver)
                    actions.move_to_element(teacher_dropdown).pause(0.3).click().perform()
                    self._log("方案3: ActionChains")
                    WebDriverWait(self.driver, 3).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                    dropdown_opened = True
                    self._log("下拉框已确认打开(方案3: ActionChains)")
                except TimeoutException:
                    self._log("方案3 未打开下拉框")
                except Exception as e:
                    self._log(f"方案3 异常: {type(e).__name__}: {e}")

            # 方案4：MouseEvent 序列（最后兜底）
            if not dropdown_opened:
                try:
                    self.driver.execute_script("""
                        const el = arguments[0];
                        ['mouseover', 'mousedown', 'mouseup', 'click'].forEach(t => {
                            el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
                        });
                    """, teacher_dropdown)
                    self._log("方案4: MouseEvent 序列")
                    WebDriverWait(self.driver, 3).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, menu_selector))
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
            if current_school == target_school:
                self._log("[OK] 学校一致，无需切换")
                self._close_teacher_dropdown()
                return True

            # 4. 不一致：点击学校元素弹出切换对话框
            self._log("学校不一致，正在切换...")
            self.driver.execute_script("arguments[0].click();", school_li)
            time.sleep(1)  # 等待对话框加载
            
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
            time.sleep(1)
            
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
                    self._close_teacher_dropdown()
                    return True
                else:
                    self._log(f"[FAIL] 学校切换失败,当前学校仍为: {new_school}")
                    self._close_teacher_dropdown()
                    return False
            except Exception as e:
                self._log(f"警告: 验证学校切换时出错 - {e}")
                # 连接断开类异常说明浏览器已不可用
                if any(kw in str(e).lower() for kw in ('connection', 'disconnected', 'timeout', 'closed')):
                    self.is_logged_in = False
                    return False
                # 非关键异常（如元素查找失败）也视为切换未验证成功
                self._log(f"学校切换验证失败，无法确认当前学校: {e}")
                return False
        
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

    def get_current_school(self) -> dict:
        """
        轻量级只读方法：读取右上角教师下拉框中显示的当前学校名称。
        不触发学校切换，读完即关闭下拉框。

        Returns:
            {"success": True, "school": str} 或 {"success": False, "error": str}
        """
        try:
            self.update_activity_time()

            # 步骤1: 查找教师下拉触发元素（与 check_and_switch_school 共用选择器级联）
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
                    break
                except TimeoutException:
                    continue

            if not teacher_dropdown:
                return {"success": False, "error": "未找到教师下拉触发元素，可能未登录"}

            # 步骤2: 打开下拉菜单（优先使用 Vue API，与 check_and_switch_school 一致）
            menu_selector = "li.el-dropdown-menu__item.info-dropdown-item.info-school"

            def _dropdown_visible():
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, menu_selector)
                    return el.is_displayed()
                except Exception:
                    return False

            if not _dropdown_visible():
                # 方案1: Vue 组件 API 展开
                try:
                    self.driver.execute_script("""
                        var dropdown = document.querySelector('.info-user > .el-dropdown');
                        if (dropdown && dropdown.__vue__) {
                            if (dropdown.__vue__.show) {
                                dropdown.__vue__.show();
                            } else if (dropdown.__vue__.visible !== undefined) {
                                dropdown.__vue__.visible = true;
                            }
                        }
                    """)
                    WebDriverWait(self.driver, 2).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                except Exception:
                    pass

            if not _dropdown_visible():
                # 方案2: JS click 降级
                try:
                    self.driver.execute_script("arguments[0].click();", teacher_dropdown)
                    WebDriverWait(self.driver, 2).until(
                        EC.visibility_of_element_located((By.CSS_SELECTOR, menu_selector))
                    )
                except Exception:
                    pass

            if not _dropdown_visible():
                return {"success": False, "error": "无法打开教师下拉菜单"}

            # 步骤3: 读取学校名称
            school_li = self.driver.find_element(By.CSS_SELECTOR, menu_selector)
            current_school = school_li.text.strip()

            # 步骤4: 关闭下拉菜单
            self._close_teacher_dropdown()

            self._log(f"[get_current_school] 当前学校: {current_school}")
            return {"success": True, "school": current_school}

        except Exception as e:
            self._log(f"[get_current_school] 异常: {e}")
            return {"success": False, "error": str(e)[:200]}

    def detect_page_state(self) -> dict:
        """
        检测浏览器当前所在的页面状态，用于 Agent 智能决策恢复策略。

        通过分析页面 URL 和关键元素来判断当前处于哪个页面。

        Returns:
            {"success": True, "state": str, "details": str}
            state 取值:
              - "login"       : 登录页面
              - "role_select" : 角色选择页面
              - "home"        : 平台首页（已登录）
              - "upload_dialog": 上传对话框已打开
              - "school_dialog": 学校切换对话框已打开
              - "error"       : 页面显示错误信息
              - "unknown"     : 无法判断
        """
        try:
            if not self.driver:
                return {"success": False, "state": "unknown",
                        "details": "浏览器未启动"}

            current_url = self.driver.current_url

            # 1. 检测登录页面
            if "login" in current_url.lower():
                return {"success": True, "state": "login",
                        "details": "当前在登录页面，URL包含login"}

            # 通过页面元素判断
            page_source = self.driver.page_source

            # 2. 检测登录表单（即使URL不含login，也可能被踢到登录页）
            try:
                login_inputs = self.driver.find_elements(
                    By.CSS_SELECTOR, "input[placeholder='请输入您的账户']"
                )
                if login_inputs and any(el.is_displayed() for el in login_inputs):
                    return {"success": True, "state": "login",
                            "details": "检测到登录表单（账号输入框可见）"}
            except Exception:
                pass

            # 3. 检测角色选择页面
            if "选择角色" in page_source:
                return {"success": True, "state": "role_select",
                        "details": "检测到角色选择界面"}

            # 4. 检测上传对话框
            try:
                upload_dialog = self.driver.find_elements(
                    By.CSS_SELECTOR, ".el-dialog__wrapper:not([style*='display: none'])"
                )
                if upload_dialog:
                    dialog_text = " ".join(
                        el.text[:200] for el in upload_dialog if el.is_displayed()
                    )
                    if "上传" in dialog_text or "作业" in dialog_text:
                        return {"success": True, "state": "upload_dialog",
                                "details": f"上传对话框已打开: {dialog_text[:100]}"}
            except Exception:
                pass

            # 5. 检测学校切换对话框
            if "切换学校" in page_source:
                try:
                    school_dialog = self.driver.find_elements(
                        By.CSS_SELECTOR, ".el-dialog__body"
                    )
                    for d in school_dialog:
                        if d.is_displayed() and "学校" in d.text:
                            return {"success": True, "state": "school_dialog",
                                    "details": "学校切换对话框已打开"}
                except Exception:
                    pass

            # 6. 检测错误提示
            try:
                error_elements = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    ".el-message--error, .el-notification--error, .el-alert--error"
                )
                visible_errors = [e for e in error_elements
                                  if e.is_displayed() and e.text.strip()]
                if visible_errors:
                    error_text = "; ".join(e.text.strip()[:100] for e in visible_errors)
                    return {"success": True, "state": "error",
                            "details": f"页面显示错误: {error_text}"}
            except Exception:
                pass

            # 7. 检测首页（已登录状态）
            if "index" in current_url or "jobManager" in current_url or "home" in current_url:
                return {"success": True, "state": "home",
                        "details": f"已登录，URL: {current_url}"}

            # 8. 尝试通过用户信息元素判断
            try:
                user_indicators = self.driver.find_elements(
                    By.CSS_SELECTOR, ".info-user, .el-dropdown-link, .user-info"
                )
                if user_indicators and any(el.is_displayed() for el in user_indicators):
                    return {"success": True, "state": "home",
                            "details": "检测到用户信息元素，已登录"}
            except Exception:
                pass

            return {"success": True, "state": "unknown",
                    "details": f"无法判断页面状态, URL: {current_url}"}

        except Exception as e:
            return {"success": False, "state": "unknown",
                    "details": f"检测异常: {str(e)[:200]}"}

    def capture_page_error(self) -> dict:
        """
        全面抓取页面当前显示的所有错误信息，供 Agent 在上传失败后分析根因。

        与 detect_page_state 不同，本方法专注于提取错误文本内容，
        并判断是否为不可恢复的永久性业务错误。

        检测范围：
          - Element UI 错误 toast (.el-message--error)
          - Element UI 错误通知 (.el-notification--error)
          - Element UI 错误警告 (.el-alert--error)
          - 表单校验错误 (.el-form-item__error)
          - 对话框内的错误文本 (.el-dialog__body 中的 error/警告文本)
          - 页面正文中的错误提示（通用）

        Returns:
            {
                "success": bool,
                "has_error": bool,           # 页面是否显示任何错误
                "errors": [                  # 所有捕获到的错误文本列表
                    {"text": str, "source": str, "selector": str}
                ],
                "combined_text": str,        # 合并后的完整错误文本
                "is_permanent": bool,        # 是否包含不可恢复的业务错误
                "permanent_reason": str,     # 判定为永久错误的依据
                "suggested_error_type": str, # 推断的 ErrorType
                "page_state": str,           # 当前页面状态（辅助信息）
            }
        """
        result = {
            "success": False,
            "has_error": False,
            "errors": [],
            "combined_text": "",
            "is_permanent": False,
            "permanent_reason": "",
            "suggested_error_type": "",
            "page_state": "unknown",
        }

        try:
            if not self.driver:
                result["page_state"] = "no_browser"
                return result

            # 先获取页面状态作为上下文
            page_state_info = self.detect_page_state()
            result["page_state"] = page_state_info.get("state", "unknown") if page_state_info.get("success") else "unknown"

            # ── 按优先级检测各类错误元素 ──
            error_selectors = [
                # (CSS选择器, 来源标签, 是否需检查可见性)
                (".el-message--error", "error_toast", True),
                (".el-notification--error", "error_notification", True),
                (".el-notification--warning", "warning_notification", True),
                (".el-alert--error", "error_alert", True),
                (".el-alert--warning", "warning_alert", True),
                (".el-form-item__error", "form_validation", True),
                # 对话框内的错误文本
                (".el-dialog__body .el-alert--error", "dialog_alert", True),
                (".el-dialog__body", "dialog_body", False),  # 需要额外过滤
            ]

            for selector, source, check_visible in error_selectors:
                try:
                    elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    for el in elements:
                        try:
                            if check_visible and not el.is_displayed():
                                continue
                            text = el.text.strip()
                            if not text:
                                continue
                            # 对话框body需要过滤：只保留包含错误/警告关键词的文本
                            if source == "dialog_body":
                                error_keywords = ["错误", "失败", "异常", "error", "fail",
                                                   "未开通", "权限", "不支持", "超限",
                                                   "已存在", "重复", "禁用", "过期"]
                                if not any(kw in text for kw in error_keywords):
                                    continue
                                # 截取关键部分（对话框可能包含大量无关文本）
                                text_lines = text.split("\n")
                                text = "\n".join(line for line in text_lines
                                                  if any(kw in line for kw in error_keywords))
                                if not text:
                                    continue

                            result["errors"].append({
                                "text": text[:300],
                                "source": source,
                                "selector": selector,
                            })
                        except Exception:
                            continue
                except Exception:
                    continue

            # ── 额外检测：页面全局错误（非 Element UI 组件）──
            try:
                # 检测通用错误提示区域
                generic_error_selectors = [
                    ".error-message", ".error-text", ".err-msg",
                    ".msg-error", ".tip-error",
                ]
                for gs in generic_error_selectors:
                    try:
                        els = self.driver.find_elements(By.CSS_SELECTOR, gs)
                        for el in els:
                            if el.is_displayed() and el.text.strip():
                                text = el.text.strip()
                                if len(text) > 3:  # 过滤太短的无意义文本
                                    result["errors"].append({
                                        "text": text[:200],
                                        "source": "generic_error",
                                        "selector": gs,
                                    })
                    except Exception:
                        continue
            except Exception:
                pass

            # ── 汇总 ──
            if result["errors"]:
                result["has_error"] = True
                result["combined_text"] = " | ".join(
                    e["text"] for e in result["errors"]
                )

            # ── 分类：判断是否永久性错误 ──
            if result["has_error"]:
                from error_types import PAGE_ERROR_PATTERNS, ErrorType
                combined_lower = result["combined_text"].lower()
                for keywords, error_type, is_permanent in PAGE_ERROR_PATTERNS:
                    for kw in keywords:
                        if kw.lower() in combined_lower:
                            result["suggested_error_type"] = error_type.value
                            result["is_permanent"] = is_permanent
                            result["permanent_reason"] = (
                                f"页面错误匹配关键词'{kw}' → "
                                f"{'不可恢复的业务错误' if is_permanent else '可恢复错误'}"
                            )
                            break
                    if result["suggested_error_type"]:
                        break

                # 未被规则匹配的默认处理：标记为未分类但可恢复
                # 由上层 ReAct 循环和规则引擎根据上下文做最终决策
                if not result["suggested_error_type"]:
                    result["suggested_error_type"] = ErrorType.UNKNOWN.value
                    result["is_permanent"] = False
                    result["permanent_reason"] = "页面显示未分类的错误信息，交由 Agent 进一步判断"

            result["success"] = True
            return result

        except Exception as e:
            result["combined_text"] = f"捕获页面错误时异常: {str(e)[:200]}"
            return result

    def recover_session(self) -> dict:
        """
        智能会话恢复：检测当前页面状态并执行对应的恢复操作。
        优先使用轻量恢复（登录/角色选择），失败才回退到浏览器重启。

        恢复策略:
          - 登录页面 → 重新登录
          - 角色选择页面 → 重新选择角色
          - 首页但登录失效 → 导航回首页后重试
          - 其他 → 返回失败，由调用方决定是否重启浏览器

        Returns:
            {"success": bool, "state_before": str, "action_taken": str, "error": str}
        """
        MAX_ITERATIONS = 5  # 防止无限循环

        try:
            if not self.driver:
                return {"success": False, "state_before": "no_driver",
                        "action_taken": "none", "error": "浏览器未启动"}

            iteration = 0
            while iteration < MAX_ITERATIONS:
                iteration += 1

                # 先检测当前页面状态（不提前刷新，避免破坏对话框等可恢复状态）
                page_state = self.detect_page_state()
                state = page_state.get("state", "unknown")
                self._log(f"[recover_session] 检测到页面状态: {state} (第{iteration}次)")

                if not page_state.get("success"):
                    return {"success": False, "state_before": state,
                            "action_taken": "none",
                            "error": page_state.get("details", "页面状态检测失败")}

                # ── 按状态执行恢复 ──

                if state == "login":
                    self._log("[recover_session] 检测到登录页面，执行自动登录...")
                    try:
                        if self._login():
                            self._log("[recover_session] 自动登录成功")
                            return {"success": True, "state_before": "login",
                                    "action_taken": "auto_login", "error": ""}
                        else:
                            self._log("[recover_session] 自动登录失败")
                            return {"success": False, "state_before": "login",
                                    "action_taken": "auto_login_failed",
                                    "error": "自动登录失败，账号密码可能不正确"}
                    except Exception as e:
                        return {"success": False, "state_before": "login",
                                "action_taken": "auto_login_error",
                                "error": f"登录过程异常: {str(e)[:200]}"}

                elif state == "role_select":
                    self._log("[recover_session] 检测到角色选择页面，执行自动选择...")
                    try:
                        if self._handle_role_selection():
                            self._log("[recover_session] 角色选择完成")
                            time.sleep(1)
                            return {"success": True, "state_before": "role_select",
                                    "action_taken": "auto_role_select", "error": ""}
                        else:
                            self._log("[recover_session] 角色选择失败")
                            return {"success": False, "state_before": "role_select",
                                    "action_taken": "role_select_failed",
                                    "error": "角色选择失败"}
                    except Exception as e:
                        return {"success": False, "state_before": "role_select",
                                "action_taken": "role_select_error",
                                "error": f"角色选择异常: {str(e)[:200]}"}

                elif state in ("upload_dialog", "school_dialog"):
                    # 有对话框打开，尝试关闭
                    self._log(f"[recover_session] 检测到{state}，尝试关闭对话框...")
                    try:
                        close_btns = self.driver.find_elements(
                            By.CSS_SELECTOR,
                            ".el-dialog__close, .el-dialog__headerbtn, .el-icon-close"
                        )
                        for btn in close_btns:
                            try:
                                if btn.is_displayed():
                                    btn.click()
                                    time.sleep(1)
                            except Exception:
                                pass
                        # 也尝试ESC
                        webdriver.ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                        time.sleep(1)
                        return {"success": True, "state_before": state,
                                "action_taken": "close_dialog", "error": ""}
                    except Exception:
                        pass
                    return {"success": True, "state_before": state,
                            "action_taken": "close_dialog_attempted", "error": ""}

                elif state == "home":
                    self._log("[recover_session] 已在首页，无需恢复")
                    return {"success": True, "state_before": "home",
                            "action_taken": "none", "error": ""}

                elif state == "error":
                    # 页面有错误提示，尝试关闭弹窗并刷新
                    self._log("[recover_session] 检测到错误提示，尝试关闭弹窗刷新...")
                    try:
                        webdriver.ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                        time.sleep(1)
                        self.driver.refresh()
                        time.sleep(1)
                        # 刷新后继续循环重新检测
                        continue
                    except Exception:
                        pass
                    return {"success": False, "state_before": "error",
                            "action_taken": "close_error_dialog_failed",
                            "error": page_state.get("details", "页面错误无法自动恢复")}

                else:  # unknown
                    self._log("[recover_session] 未知页面状态，尝试刷新后重新检测...")
                    try:
                        self.driver.refresh()
                        time.sleep(1)
                        # 刷新后继续循环重新检测
                        continue
                    except Exception:
                        pass
                    return {"success": False, "state_before": "unknown",
                            "action_taken": "refresh",
                            "error": "未知页面状态，刷新后仍无法识别"}

            # 超过最大迭代次数
            return {"success": False, "state_before": "unknown",
                    "action_taken": "max_iterations_exceeded",
                    "error": f"恢复尝试超过{MAX_ITERATIONS}次，状态仍未稳定"}

        except Exception as e:
            self._log(f"[recover_session] 异常: {e}")
            return {"success": False, "state_before": "unknown",
                    "action_taken": "exception",
                    "error": f"会话恢复异常: {str(e)[:200]}"}

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

                // 限定在上传对话框内搜索，避免读到主页面上的筛选条件
                const dialog = document.querySelector('.el-dialog__wrapper:not([style*="display: none"])');
                const scope = dialog || document;

                const labels = scope.querySelectorAll('label');
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

    def _close_teacher_dropdown(self):
        """关闭教师下拉菜单 — Vue API 优先，JS body.click() 兜底触发 click-outside 关闭"""
        try:
            self.driver.execute_script("""
                var dropdown = document.querySelector('.info-user > .el-dropdown');
                if (dropdown && dropdown.__vue__) {
                    dropdown.__vue__.visible = false;
                }
            """)
        except Exception:
            pass
        # 兜底：JS 直接触发 body click，不会像 Selenium click 那样受坐标影响
        try:
            self.driver.execute_script("document.body.click();")
        except Exception:
            pass
        time.sleep(0.2)

    def _is_on_login_page(self) -> bool:
        """
        快速检测当前页面是否为登录页（仅检查URL，不等待任何元素）。
        用于在各操作步骤间快速发现会话丢失，避免等待元素超时。

        Returns:
            True=当前在登录页面, False=不在登录页或无法判断
        """
        try:
            if not self.driver:
                return False
            url = self.driver.current_url
            return "login" in url.lower() or "/login" in url
        except Exception:
            return False

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
            self.last_upload_error = ""  # 每次上传前重置错误记录
            display_name = re.sub(r'^[0-9a-f]{32}_', '', os.path.basename(file_path))
            self._log(f"开始上传: {display_name} (学校={school}, 年级={grade}, 科目={subject})")

            # 0) 快速前置检查：是否已经在登录页（账号在上一步操作中被踢下线）
            if self._is_on_login_page():
                error_text = "会话丢失(上传前检测到登录页)，账号可能被异地登录踢下线"
                self._log(f"[FAIL] {error_text}")
                self.is_logged_in = False
                self.last_upload_error = error_text
                return False

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
            self._log("已打开上传作业对话框")

            # 快速检测：点击上传按钮后页面是否跳转到登录页
            if self._is_on_login_page():
                error_text = "会话丢失(打开上传对话框后检测到登录页)，账号可能被异地登录踢下线"
                self._log(f"[FAIL] {error_text}")
                self.is_logged_in = False
                self.last_upload_error = error_text
                return False

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
                self._log(f"页面源码(前500字符): {self.driver.page_source[:500]}")
                raise Exception("找不到文件上传input,请检查上传对话框是否正确打开")
            file_input.send_keys(file_path)
            time.sleep(self.config.sleep_interval)
            self._log(f"[OK] 已选择文件: {os.path.basename(file_path)}")

            # 快速检测：选择文件后页面是否跳转到登录页
            if self._is_on_login_page():
                error_text = "会话丢失(选择文件后检测到登录页)，账号可能被异地登录踢下线"
                self._log(f"[FAIL] {error_text}")
                self.is_logged_in = False
                self.last_upload_error = error_text
                return False

            # 勾选年级+科目
            wait = WebDriverWait(self.driver, 10)

            # 先读取页面当前已选中的年级和科目，若与目标一致则跳过选择
            current_grade = self._read_select_value("年级")
            current_subject = self._read_select_value("科目")
            self._log(f"页面当前选中: 年级={current_grade}, 科目={current_subject}")

            # -------------------- 4. 选择年级（自定义下拉） --------------------
            if current_grade and current_grade == grade:
                self._log(f"⏭️ 年级已匹配({grade})，跳过选择")
            else:
                self._log(f"选择年级: {grade}（当前: {current_grade}）")
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
                # 验证年级选择是否生效
                time.sleep(0.5)
                verify_grade = self._read_select_value("年级")
                if verify_grade and verify_grade != grade:
                    self._log(f"⚠ 年级选择可能未生效: 期望={grade}, 实际={verify_grade}")
                else:
                    self._log(f"✅ 年级选择完成: {grade}")

            # -------------------- 5. 选择科目（自定义下拉） --------------------
            # 重要：年级切换后科目可能被页面重置，必须重新读取
            if current_grade != grade:
                time.sleep(0.5)
                current_subject = self._read_select_value("科目")
                self._log(f"年级已变更，重新读取科目: {current_subject}")
            if current_subject and current_subject == subject:
                self._log(f"⏭️ 科目已匹配({subject})，跳过选择")
            else:
                self._log(f"选择科目: {subject}（当前: {current_subject}）")
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
                # 验证科目选择是否生效
                time.sleep(0.5)
                verify_subject = self._read_select_value("科目")
                if verify_subject and verify_subject != subject:
                    self._log(f"⚠ 科目选择可能未生效: 期望={subject}, 实际={verify_subject}")
                else:
                    self._log(f"✅ 科目选择完成: {subject}")

            # 步骤5: 设置预计使用时间(明天) — 直接输入模式，跳过不稳定的日历选择器
            self._log("正在设置预计使用时间...")
            try:
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                tomorrow_time = tomorrow + " 09:00"
                self._log(f"目标日期时间: {tomorrow_time}")

                date_input = self.driver.find_element(
                    By.XPATH,
                    "//input[@placeholder='选择日期' and @class='el-input__inner']"
                )
                date_input.clear()
                date_input.send_keys(tomorrow_time)
                date_input.send_keys(Keys.RETURN)
                time.sleep(0.3)
                self._log("[OK] 使用时间设置完成!")

            except Exception as e:
                self._log(f"警告: 设置预计使用时间失败 - {e}")

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
            self._log("已点击提交按钮，等待上传结果...")

            # 步骤7: 等待上传完成并验证
            # 核心策略：监控提交按钮是否消失。点击提交后，上传对话框关闭 →
            # 提交按钮要么从DOM移除(v-if)要么隐藏(v-show)，这是最可靠的完成信号。
            # Element UI 的 toast 消息（.el-message--success）~3秒自动消失，
            # 不能作为主要判断依据。
            #
            # 关键：轮询期间必须临时禁用 implicit_wait，否则每次 find_elements()
            # 在找不到元素时会等待10秒才返回空列表，导致每轮循环耗时30秒以上。
            upload_timeout = self.config.get("UPLOAD_TIMEOUT", 120)
            deadline = time.time() + upload_timeout
            result = None  # None=等待中, True=成功, False=失败
            error_text = ""

            self.driver.implicitly_wait(0)  # 轮询阶段确保隐式等待关闭（全局默认已是0）
            poll_count = 0  # 轮询计数器，用于降低部分检查频率
            try:
                while time.time() < deadline:
                    poll_count += 1
                    # 0) 前置检测：页面是否跳转到登录页（会话丢失）
                    try:
                        url = self.driver.current_url
                        if "login" in url.lower() or "/login" in url:
                            error_text = "会话丢失(页面跳转到登录页)，账号可能被异地登录踢下线"
                            self._log(f"[FAIL] {error_text}")
                            self.is_logged_in = False
                            self.last_upload_error = error_text
                            result = False
                            break
                    except Exception:
                        pass  # 读取URL失败则跳过本次检查

                    # 1) 检查提交按钮是否已消失（对话框关闭的最可靠信号）
                    try:
                        submit_btn.is_enabled()  # 触发 StaleElementReferenceException 如果按钮已脱离DOM
                        if not submit_btn.is_displayed():
                            result = True
                            break
                    except StaleElementReferenceException:
                        result = True
                        break

                    # 2) 检查表单校验错误（如"请选择年级"等）
                    try:
                        form_errors = self.driver.find_elements(
                            By.CSS_SELECTOR, ".el-form-item__error"
                        )
                        visible_errors = [e for e in form_errors if e.is_displayed() and e.text.strip()]
                        if visible_errors:
                            error_text = "; ".join(e.text.strip() for e in visible_errors)
                            self._log(f"[FAIL] 表单校验错误: {error_text}")
                            self.last_upload_error = error_text
                            result = False
                            break
                    except Exception:
                        pass

                    # 3) 检查成功/错误 toast
                    try:
                        success_toasts = self.driver.find_elements(
                            By.CSS_SELECTOR, ".el-message--success, .el-notification--success"
                        )
                        if any(el.is_displayed() for el in success_toasts):
                            result = True
                            break
                    except Exception:
                        pass

                    try:
                        error_toasts = self.driver.find_elements(
                            By.CSS_SELECTOR, ".el-message--error, .el-notification--error"
                        )
                        visible = [e for e in error_toasts if e.is_displayed() and e.text.strip()]
                        if visible:
                            error_text = "; ".join(e.text.strip() for e in visible)
                            # 标记特定错误类型，供 upload_processor 细分 ErrorType
                            if "该校未开通数智作业服务" in error_text or "未开通数智作业" in error_text:
                                error_text = "[SCHOOL_NOT_ACTIVATED] " + error_text
                                self._log(f"[FAIL] 学校未开通数智作业服务: {error_text}")
                            else:
                                self._log(f"[FAIL] 检测到错误提示: {error_text}")
                            # 账号被踢下线 → 标记会话失效，阻止后续文件继续上传
                            if ("另一个地点登录" in error_text
                                    or "被迫下线" in error_text
                                    or "异地登录" in error_text):
                                self.is_logged_in = False
                                self._log("[WARN] 会话已失效(账号被踢下线)，后续文件将暂停处理")
                            self.last_upload_error = error_text
                            result = False
                            break
                    except Exception:
                        pass

                    # 3.5) 每3秒扫描一次页面文本中的会话丢失信号（降低开销）
                    if poll_count % 3 == 0 and self._detect_session_lost_on_page():
                        error_text = "会话丢失(页面文本检测到踢下线信号)"
                        self._log(f"[FAIL] {error_text}")
                        self.last_upload_error = error_text
                        result = False
                        break

                    time.sleep(1)
            finally:
                self.driver.implicitly_wait(2)  # 恢复隐式等待至全局默认值

            self.last_active_time = time.time()

            if result is True:
                # 对话框已关闭或检测到成功toast → 最后确认无残留错误
                time.sleep(0.5)
                lingering = self.driver.find_elements(
                    By.CSS_SELECTOR, ".el-message--error, .el-form-item__error"
                )
                if lingering:
                    err_text = " ".join(e.text.strip() for e in lingering if e.text.strip())
                    if err_text:
                        self._log(f"[FAIL] 上传失败: {err_text}")
                        self.last_upload_error = err_text
                        return False
                self._log("[OK] 文件上传成功")
                return True
            elif result is False:
                self._log(f"[FAIL] 文件上传失败: {error_text}")
                self.last_upload_error = error_text
                return False
            else:
                # 超时：提交按钮始终可见 → 可能上传大文件耗时较长，或页面卡住
                self._log(f"[FAIL] 上传超时({upload_timeout}秒)，提交按钮未消失")
                self.last_upload_error = f"上传超时({upload_timeout}秒)"
                return False
        
        except Exception as e:
            self._log(f"错误: 文件上传失败 - {e}")
            self.last_upload_error = str(e)[:500]
            return False
    
    def reset_to_home(self) -> bool:
        """
        环境复位：将浏览器恢复到干净的首页状态
        用于 AutoRetryAgent 重试前清理中间状态，避免二次失败

        执行流程：
        1. 发送 ESC 键关闭所有下拉/浮层
        2. 关闭所有残留对话框（上传框、学校切换框、错误提示）
        3. 导航回平台首页
        4. 校验登录状态，异常则触发重启

        Returns:
            True表示复位成功, False表示浏览器已不可用
        """
        try:
            if not self.driver:
                self._log("reset_to_home: 浏览器未启动，尝试重新初始化...")
                if not self.ensure_initialized():
                    self._log("reset_to_home: 浏览器重新初始化失败")
                    return False
                # 浏览器已恢复，继续执行复位流程

            self._log("开始环境复位...")

            # 1. 发送 ESC 关闭所有下拉/浮层/弹窗
            for _ in range(3):
                try:
                    webdriver.ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                    time.sleep(0.3)
                except Exception:
                    break

            # 2. 关闭残留对话框
            close_selectors = [
                # Element UI dialog 关闭按钮
                (By.CSS_SELECTOR, ".el-dialog__close"),
                (By.CSS_SELECTOR, ".el-dialog__headerbtn"),
                # 上传对话框关闭
                (By.CSS_SELECTOR, ".el-dialog .el-icon-close"),
                # 通用关闭按钮
                (By.XPATH, "//button[contains(@class, 'el-dialog__close')]"),
                # 取消按钮（关闭对话框）
                (By.XPATH, "//button[contains(., '取 消') or contains(., '取消')]"),
                # 遮罩层点击关闭
                (By.CSS_SELECTOR, ".v-modal"),
            ]
            for by, selector in close_selectors:
                try:
                    elements = self.driver.find_elements(by, selector)
                    for el in elements:
                        try:
                            if el.is_displayed():
                                el.click()
                                time.sleep(0.3)
                        except Exception:
                            pass
                except Exception:
                    continue

            # 再发送一次 ESC 确保关闭残留
            try:
                webdriver.ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                time.sleep(0.3)
            except Exception:
                pass

            # 3. 导航回平台首页
            try:
                current_url = self.driver.current_url
                # 如果不在首页，导航回去
                if "login" not in current_url.lower():
                    base_url = self.config.website_url.rstrip('/')
                    # 尝试直接跳转到首页
                    self.driver.get(base_url)
                    time.sleep(1)

                    # 等待页面加载
                    WebDriverWait(self.driver, 10).until(
                        lambda d: d.execute_script("return document.readyState;") == "complete"
                    )

                    # 强制刷新清除卡住的JS状态
                    try:
                        self.driver.refresh()
                        time.sleep(1)
                    except Exception:
                        pass
            except Exception as e:
                self._log(f"reset_to_home: 导航回首页失败 - {e}")
                # 导航失败时尝试浏览器重启
                try:
                    self._log("reset_to_home: 尝试重启浏览器恢复...")
                    if self.restart_browser():
                        self._log("reset_to_home: 浏览器重启成功")
                    else:
                        self._log("reset_to_home: 浏览器重启也失败")
                        return False
                except Exception as re:
                    self._log(f"reset_to_home: 浏览器重启异常 - {re}")
                    return False

            # 4. 校验登录状态
            if not self.check_login_status():
                self._log("reset_to_home: 检测到登录失效，尝试重启浏览器")
                if not self.restart_browser():
                    self._log("reset_to_home: 浏览器重启失败")
                    return False

            self._log("环境复位完成")
            self.update_activity_time()
            return True

        except Exception as e:
            self._log(f"reset_to_home: 环境复位异常 - {e}")
            import traceback
            traceback.print_exc()
            return False


    # ─── 会话丢失检测 ───

    def _detect_session_lost_on_page(self) -> bool:
        """
        扫描当前页面可见文本，检测会话丢失信号（如"已被迫下线"等toast/通知）。
        仅做检测，不触发页面刷新——由调用方决定后续操作。

        Returns:
            True=检测到会话已丢失, False=未检测到会话丢失信号
        """
        if not self.driver:
            return False
        try:
            from error_types import SESSION_LOST_KEYWORDS
            body_text = self.driver.execute_script(
                "return document.body ? document.body.innerText.substring(0, 1000) : ''"
            )
            for kw in SESSION_LOST_KEYWORDS:
                if kw in body_text:
                    self._log(f"检测到会话丢失关键词: {kw}")
                    self.is_logged_in = False
                    return True
        except Exception:
            pass
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
    
    def _detect_existing_session(self) -> bool:
        """
        检测浏览器是否已有有效登录会话（持久化 profile 恢复场景）。
        与 check_login_status 不同：此方法不依赖 self.is_logged_in 标志，
        直接检查页面状态，用于 initialize() 中跳过登录流程。

        Returns:
            True表示已有有效会话无需重新登录, False表示需要走登录流程
        """
        try:
            current_url = self.driver.current_url

            # 不在登录页 → 大概率已有 session
            if "login" not in current_url.lower():
                # 进一步确认：检查是否有用户信息元素
                status_indicators = [
                    (By.CSS_SELECTOR, ".info-user"),
                    (By.CSS_SELECTOR, ".el-dropdown-link"),
                ]
                for by, selector in status_indicators:
                    try:
                        self.driver.find_element(by, selector)
                        self._log(f"_detect_existing_session: 找到元素 {selector}，会话有效")
                        return True
                    except NoSuchElementException:
                        continue

                # URL不在login且页面有内容 → 可能已登录（有些页面info-user渲染慢）
                body_text = self.driver.execute_script(
                    "return document.body ? document.body.innerText : ''")
                if len(body_text) > 100 and "登录" not in body_text[:500]:
                    self._log("_detect_existing_session: URL非登录页+页面无登录提示，判定会话有效")
                    return True

            # 在登录页 → 但可能表单还没渲染，稍等片刻再判断
            if "login" in current_url.lower():
                # 快速检查登录表单是否已出现
                try:
                    self.driver.find_element(
                        By.CSS_SELECTOR, "input[placeholder='请输入您的账户']")
                    self._log("_detect_existing_session: 在登录页检测到登录表单，需要重新登录")
                    return False
                except NoSuchElementException:
                    # 可能在登录页但页面还在加载，短等后重试
                    time.sleep(1)
                    try:
                        self.driver.find_element(
                            By.CSS_SELECTOR, "input[placeholder='请输入您的账户']")
                        self._log("_detect_existing_session: 登录表单已出现，需要重新登录")
                        return False
                    except NoSuchElementException:
                        pass

            self._log("_detect_existing_session: 无法确定会话状态，走正常登录流程")
            return False

        except Exception as e:
            self._log(f"_detect_existing_session: 检测异常 - {e}，走正常登录流程")
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

            # 方式2.5: 扫描页面文本中的会话丢失关键词（使用共享检测方法）
            if self._detect_session_lost_on_page():
                return False

            # 方式3: URL 包含 login 说明已跳转到登录页，判定为未登录
            if "login" in current_url.lower():
                self._log(f"检测到登录页URL: {current_url}")
                self.is_logged_in = False
                return False

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
        线程安全：加锁防止多线程同时重启

        Returns:
            True表示重启成功,False表示失败
        """
        with self._lock:
            print("正在重启浏览器...")
            self.close()
            time.sleep(1)
            return self.initialize()
    
    # ─── 页面检查工具（供 AI Agent ReAct 循环调用）───

    def get_page_text(self) -> dict:
        """
        获取当前页面可见文本内容

        Returns:
            {"success": bool, "text": str} 或 {"success": False, "error": str}
        """
        try:
            if not self.driver:
                return {"success": False, "error": "浏览器未初始化"}
            body = self.driver.find_element(By.TAG_NAME, "body")
            text = body.text[:3000]
            return {"success": True, "text": text, "length": len(text)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_page_screenshot(self) -> dict:
        """
        截取当前页面并保存到 screenshots/ 目录

        Returns:
            {"success": bool, "path": str} 或 {"success": False, "error": str}
        """
        try:
            if not self.driver:
                return {"success": False, "error": "浏览器未初始化"}
            os.makedirs("screenshots", exist_ok=True)
            from datetime import datetime as dt
            filename = f"page_{dt.now().strftime('%Y%m%d_%H%M%S')}.png"
            path = os.path.join("screenshots", filename)
            self.driver.save_screenshot(path)
            self._log(f"截图已保存: {path}")
            return {"success": True, "path": os.path.abspath(path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def check_element(self, selector: str, selector_type: str = "xpath") -> dict:
        """
        检查页面元素是否存在、可见、可用

        Args:
            selector: 元素选择器
            selector_type: "xpath" 或 "css"

        Returns:
            {"exists": bool, "visible": bool, "enabled": bool, "text": str, "selected": bool}
        """
        try:
            if not self.driver:
                return {"exists": False, "visible": False, "error": "浏览器未初始化"}
            by = By.XPATH if selector_type == "xpath" else By.CSS_SELECTOR
            el = self.driver.find_element(by, selector)
            result = {
                "exists": True,
                "visible": el.is_displayed(),
                "enabled": el.is_enabled(),
                "text": (el.text or "")[:200],
            }
            if el.tag_name in ("option", "input"):
                result["selected"] = el.is_selected()
            return result
        except Exception as e:
            return {"success": False, "exists": False, "visible": False,
                    "enabled": False, "error": f"元素检查失败: {str(e)[:200]}"}

    def execute_browser_action(self, action: str, selector: str = "",
                               value: str = "", selector_type: str = "xpath") -> dict:
        """
        在浏览器中执行操作

        Args:
            action: "click" | "type" | "select" | "scroll_down" | "refresh"
            selector: 目标元素选择器
            value: 输入文本（action=type 时使用）
            selector_type: "xpath" 或 "css"

        Returns:
            {"success": bool, "action": str}
        """
        try:
            if not self.driver:
                return {"success": False, "error": "浏览器未初始化"}

            if action == "refresh":
                self.driver.refresh()
                return {"success": True, "action": "refresh"}

            if action == "scroll_down":
                self.driver.execute_script("window.scrollBy(0, 300)")
                return {"success": True, "action": "scroll_down"}

            if not selector:
                return {"success": False, "error": "缺少 selector 参数"}

            by = By.XPATH if selector_type == "xpath" else By.CSS_SELECTOR
            el = self.driver.find_element(by, selector)

            if action == "click":
                el.click()
            elif action == "type":
                el.clear()
                el.send_keys(value)
            elif action == "select":
                # 对 el-select 等 Vue 组件，用 JS click 触发
                self.driver.execute_script("arguments[0].click();", el)
            else:
                return {"success": False, "error": f"未知操作: {action}"}

            self._log(f"浏览器操作: {action} {selector[:60]}")
            return {"success": True, "action": action}
        except Exception as e:
            return {"success": False, "error": str(e)[:300]}

    def close(self):
        """
        关闭浏览器,释放资源
        线程安全：加锁防止多线程同时关闭/初始化
        """
        with self._lock:
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
