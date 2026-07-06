"""
错误类型定义模块
定义上传全流程阶段枚举、错误分类枚举、自愈策略映射表
用于 AutoRetryAgent 和 FailureAnalysisAgent 的结构化错误处理
"""
from enum import Enum
from typing import Dict, List, Optional, Tuple


class UploadStage(str, Enum):
    """上传全流程阶段，用于定位失败发生位置"""
    PARSE_FOLDER = "parse_folder"       # 解析文件夹名（学校/年级）
    READ_FILE = "read_file"             # 读取文件内容
    AI_CLASSIFY = "ai_classify"         # AI 科目识别
    BROWSER_INIT = "browser_init"       # 浏览器初始化与登录
    SCHOOL_CHECK = "school_check"       # 学校校验与切换
    FORM_FILL = "form_fill"             # 上传表单填写（年级/科目/时间）
    SUBMIT_UPLOAD = "submit_upload"     # 提交上传与结果确认
    DB_RECORD = "db_record"             # 数据库写入


class ErrorCategory(str, Enum):
    """错误一级分类"""
    BROWSER_ERROR = "browser_error"         # 浏览器自动化类
    FILE_PROCESS_ERROR = "file_error"       # 文件处理类
    AI_SERVICE_ERROR = "ai_error"           # AI服务类
    PLATFORM_BIZ_ERROR = "biz_error"        # 平台业务类
    SYSTEM_ENV_ERROR = "system_error"       # 系统环境类
    UNKNOWN_ERROR = "unknown_error"         # 未知错误


class ErrorType(str, Enum):
    """错误二级类型，用于Agent决策"""
    # 浏览器自动化类
    BROWSER_START_FAIL = "browser_start_fail"
    LOGIN_EXPIRED = "login_expired"
    ELEMENT_TIMEOUT = "element_timeout"
    PAGE_LOAD_TIMEOUT = "page_load_timeout"
    SCHOOL_SWITCH_FAIL = "school_switch_fail"
    SCHOOL_NOT_FOUND = "school_not_found"
    UPLOAD_SUBMIT_TIMEOUT = "upload_submit_timeout"

    # 文件处理类
    FILE_UNREADABLE = "file_unreadable"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FILE_NOT_EXIST = "file_not_exist"
    FILE_CORRUPTED = "file_corrupted"

    # AI服务类
    API_KEY_INVALID = "api_key_invalid"
    API_TIMEOUT = "api_timeout"
    API_RATE_LIMIT = "api_rate_limit"
    SUBJECT_RECOGNIZE_EMPTY = "subject_empty"

    # 平台业务类
    FORM_VALIDATE_FAIL = "form_validate_fail"
    SUBJECT_NOT_OPTION = "subject_not_in_option"
    PERMISSION_DENIED = "permission_denied"
    SCHOOL_NOT_ACTIVATED = "school_not_activated"  # 学校未开通数智作业服务

    # 系统环境类
    NETWORK_ERROR = "network_error"
    DATABASE_ERROR = "database_error"

    # 兜底
    UNKNOWN = "unknown"


class RetryLevel(str, Enum):
    """自愈策略级别"""
    L1_LIGHT_RETRY = "L1"    # 轻量重试：原地重试当前步骤
    L2_PAGE_RESET = "L2"     # 页面复位：关闭弹窗、返回首页
    L3_ENV_RESET = "L3"      # 环境重置：刷新页面、重新校验学校
    L4_SERVICE_RESTART = "L4"  # 服务重启：重启浏览器、重新登录
    L5_MANUAL = "L5"         # 人工兜底：标记为待人工处理


# ─── 核心策略映射表 ───
# 格式: (UploadStage, ErrorType) → (RetryLevel, max_retries_for_this_error)
# 注意: max_retries_for_this_error 是 Agent 对该错误类型的最大重试次数，
# 不是全局 retry_count 上限（后者由 config.max_retry_count 控制）

STRATEGY_MAP: Dict[Tuple[UploadStage, ErrorType], Tuple[RetryLevel, int]] = {
    # READ_FILE 阶段
    (UploadStage.READ_FILE, ErrorType.FILE_NOT_EXIST):     (RetryLevel.L5_MANUAL, 0),
    (UploadStage.READ_FILE, ErrorType.FILE_UNREADABLE):    (RetryLevel.L1_LIGHT_RETRY, 1),
    (UploadStage.READ_FILE, ErrorType.UNSUPPORTED_FORMAT): (RetryLevel.L5_MANUAL, 0),
    (UploadStage.READ_FILE, ErrorType.FILE_CORRUPTED):     (RetryLevel.L5_MANUAL, 0),

    # AI_CLASSIFY 阶段
    (UploadStage.AI_CLASSIFY, ErrorType.API_TIMEOUT):           (RetryLevel.L1_LIGHT_RETRY, 2),
    (UploadStage.AI_CLASSIFY, ErrorType.API_RATE_LIMIT):        (RetryLevel.L1_LIGHT_RETRY, 2),
    (UploadStage.AI_CLASSIFY, ErrorType.API_KEY_INVALID):       (RetryLevel.L5_MANUAL, 0),
    (UploadStage.AI_CLASSIFY, ErrorType.SUBJECT_RECOGNIZE_EMPTY): (RetryLevel.L1_LIGHT_RETRY, 1),

    # BROWSER_INIT 阶段
    (UploadStage.BROWSER_INIT, ErrorType.BROWSER_START_FAIL): (RetryLevel.L4_SERVICE_RESTART, 2),
    (UploadStage.BROWSER_INIT, ErrorType.PAGE_LOAD_TIMEOUT):  (RetryLevel.L4_SERVICE_RESTART, 2),
    (UploadStage.BROWSER_INIT, ErrorType.NETWORK_ERROR):      (RetryLevel.L1_LIGHT_RETRY, 3),

    # SCHOOL_CHECK 阶段
    (UploadStage.SCHOOL_CHECK, ErrorType.ELEMENT_TIMEOUT):    (RetryLevel.L1_LIGHT_RETRY, 2),
    (UploadStage.SCHOOL_CHECK, ErrorType.SCHOOL_NOT_FOUND):   (RetryLevel.L5_MANUAL, 0),
    (UploadStage.SCHOOL_CHECK, ErrorType.SCHOOL_SWITCH_FAIL): (RetryLevel.L3_ENV_RESET, 2),
    (UploadStage.SCHOOL_CHECK, ErrorType.PAGE_LOAD_TIMEOUT):  (RetryLevel.L2_PAGE_RESET, 1),

    # FORM_FILL 阶段
    (UploadStage.FORM_FILL, ErrorType.ELEMENT_TIMEOUT):       (RetryLevel.L2_PAGE_RESET, 2),
    (UploadStage.FORM_FILL, ErrorType.SUBJECT_NOT_OPTION):    (RetryLevel.L5_MANUAL, 0),
    (UploadStage.FORM_FILL, ErrorType.FORM_VALIDATE_FAIL):    (RetryLevel.L2_PAGE_RESET, 1),

    # SCHOOL_CHECK 阶段 — 学校未开通（平台业务错误，不能通过重试修复）
    (UploadStage.SCHOOL_CHECK, ErrorType.SCHOOL_NOT_ACTIVATED): (RetryLevel.L5_MANUAL, 0),

    # SUBMIT_UPLOAD 阶段
    (UploadStage.SUBMIT_UPLOAD, ErrorType.LOGIN_EXPIRED):          (RetryLevel.L4_SERVICE_RESTART, 1),
    (UploadStage.SUBMIT_UPLOAD, ErrorType.UPLOAD_SUBMIT_TIMEOUT):  (RetryLevel.L2_PAGE_RESET, 2),
    (UploadStage.SUBMIT_UPLOAD, ErrorType.FORM_VALIDATE_FAIL):     (RetryLevel.L2_PAGE_RESET, 1),
    (UploadStage.SUBMIT_UPLOAD, ErrorType.PERMISSION_DENIED):      (RetryLevel.L5_MANUAL, 0),
    (UploadStage.SUBMIT_UPLOAD, ErrorType.SCHOOL_NOT_ACTIVATED):   (RetryLevel.L5_MANUAL, 0),

    # 全局网络错误（任意阶段）
    (None, ErrorType.NETWORK_ERROR): (RetryLevel.L1_LIGHT_RETRY, 3),

    # 未知错误兜底（任意阶段/任意错误类型 → 人工处理）
    (None, ErrorType.UNKNOWN): (RetryLevel.L5_MANUAL, 0),
}


# ─── 错误分类推断规则 ───
# 根据异常信息和阶段推断 ErrorCategory 和 ErrorType
# 用于兼容旧数据（无结构化字段的失败记录）

ERROR_CLASSIFICATION_RULES: List[tuple] = [
    # (关键词列表, ErrorCategory, ErrorType)
    # 浏览器相关
    (["浏览器启动失败", "browser_start", "chrome", "webdriver", "driver",
      "无法启动浏览器", "浏览器崩溃", "browser crash"],
     ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL),
    (["登录失效", "login expired", "未登录", "重新登录", "登录态",
      "login status", "请先登录"],
     ErrorCategory.BROWSER_ERROR, ErrorType.LOGIN_EXPIRED),
    (["元素", "element", "超时", "timeout", "等待超时", "找不到",
      "not found", "not clickable", "不可点击", "stale"],
     ErrorCategory.BROWSER_ERROR, ErrorType.ELEMENT_TIMEOUT),
    (["学校切换失败", "school_switch", "学校校验", "切换学校"],
     ErrorCategory.BROWSER_ERROR, ErrorType.SCHOOL_SWITCH_FAIL),
    (["学校不存在", "school not found", "未找到学校"],
     ErrorCategory.PLATFORM_BIZ_ERROR, ErrorType.SCHOOL_NOT_FOUND),
    (["该校未开通", "数智作业服务", "未开通数智作业", "school not activated"],
     ErrorCategory.PLATFORM_BIZ_ERROR, ErrorType.SCHOOL_NOT_ACTIVATED),
    (["上传", "upload", "提交", "submit", "确认"],
     ErrorCategory.BROWSER_ERROR, ErrorType.UPLOAD_SUBMIT_TIMEOUT),

    # 文件相关
    (["文件不存在", "file not exist", "文件损坏", "corrupted",
      "无法读取", "unreadable", "文件格式不支持", "unsupported"],
     ErrorCategory.FILE_PROCESS_ERROR, ErrorType.FILE_UNREADABLE),

    # AI相关
    (["API", "api", "deepseek", "DeepSeek", "识别失败", "科目",
      "classify", "密钥", "key", "apikey"],
     ErrorCategory.AI_SERVICE_ERROR, ErrorType.API_TIMEOUT),

    # 网络（避免使用 "timeout"/"超时" 以防与 ELEMENT_TIMEOUT 冲突）
    (["网络错误", "网络超时", "网络异常", "网络故障", "网络不可达", "网络连接失败",
      "network error", "network timeout", "network unreachable",
      "connection refused", "connection reset", "connection timeout",
      "DNS", "no internet", "ERR_", "无法访问", "连接超时",
      "proxy", "代理", "connectivity"],
     ErrorCategory.SYSTEM_ENV_ERROR, ErrorType.NETWORK_ERROR),
]


def classify_error(error_message: str, fail_stage: Optional[str] = None) -> tuple:
    """
    根据错误信息文本推断 ErrorCategory 和 ErrorType
    用于兼容旧数据中无结构化字段的失败记录

    Args:
        error_message: 错误信息文本
        fail_stage: 已知的失败阶段（可选，辅助推断）

    Returns:
        (ErrorCategory, ErrorType) 元组
    """
    if not error_message:
        return (ErrorCategory.UNKNOWN_ERROR, ErrorType.UNKNOWN)

    msg_lower = error_message.lower()

    for keywords, category, error_type in ERROR_CLASSIFICATION_RULES:
        for kw in keywords:
            if kw.lower() in msg_lower:
                return (category, error_type)

    return (ErrorCategory.UNKNOWN_ERROR, ErrorType.UNKNOWN)


def get_strategy(fail_stage: Optional[str], error_type: Optional[str]) -> Tuple[RetryLevel, int]:
    """
    根据失败阶段和错误类型查询自愈策略

    Args:
        fail_stage: UploadStage 值（可为 None，表示未知阶段）
        error_type: ErrorType 值（可为 None，表示未知错误类型）

    Returns:
        (RetryLevel, max_retries_for_this_error)
        默认返回 L5_MANUAL, 0
    """
    try:
        stage = UploadStage(fail_stage) if fail_stage else None
        etype = ErrorType(error_type) if error_type else None
    except ValueError:
        return (RetryLevel.L5_MANUAL, 0)

    # 精确匹配
    key = (stage, etype)
    if key in STRATEGY_MAP:
        return STRATEGY_MAP[key]

    # 全局匹配（stage 为 None 的规则）
    key_global = (None, etype)
    if key_global in STRATEGY_MAP:
        return STRATEGY_MAP[key_global]

    # 默认：人工兜底
    return (RetryLevel.L5_MANUAL, 0)


# ─── 错误类型元数据（用于分析报告展示） ───

ERROR_DESCRIPTIONS: Dict[str, Tuple[str, str]] = {
    # (标题, 根因描述)
    'browser_start_fail': ('浏览器启动失败', 'Chrome驱动版本不匹配、系统资源不足或浏览器被安全软件拦截'),
    'login_expired': ('登录态失效', '会话超时或Cookie过期，平台后端主动踢出登录'),
    'element_timeout': ('元素操作超时', '页面加载慢、Vue渲染延迟或选择器未适配新版本页面'),
    'page_load_timeout': ('页面加载超时', '网络不稳定或平台服务器响应慢'),
    'school_switch_fail': ('学校切换失败', '页面DOM结构变化或学校列表接口异常'),
    'school_not_found': ('学校不存在', '目标学校未在平台注册或名称不匹配'),
    'upload_submit_timeout': ('上传提交超时', '文件过大、平台限流或表单校验未通过'),
    'file_not_exist': ('文件不存在', '文件已被删除、移动或路径变更'),
    'file_unreadable': ('文件无法读取', '文件损坏、加密或格式不兼容'),
    'file_corrupted': ('文件已损坏', '文件内容损坏无法解析，需重新获取源文件'),
    'unsupported_format': ('文件格式不支持', '上传了平台不支持的文件类型'),
    'api_timeout': ('AI API超时', 'DeepSeek服务繁忙或网络连接不稳定'),
    'api_key_invalid': ('API密钥无效', '密钥过期或被吊销'),
    'api_rate_limit': ('API频率限制', '请求过于频繁触发限流'),
    'subject_empty': ('科目识别为空', 'AI未能从文件内容中识别出有效科目信息'),
    'form_validate_fail': ('表单校验失败', '必填字段缺失或数据格式不符合平台要求'),
    'subject_not_in_option': ('科目不在选项中', '平台科目列表变更或AI识别结果与平台不匹配'),
    'permission_denied': ('权限不足', '当前账号无该学校/年级的上传权限'),
    'school_not_activated': ('学校未开通服务', '目标学校未开通数智作业服务，需联系平台管理员开通'),
    'network_error': ('网络错误', '网络连接不稳定、DNS解析失败或防火墙拦截'),
    'database_error': ('数据库错误', 'SQLite文件损坏或磁盘空间不足'),
    'unknown': ('未知错误类型', '待进一步排查'),
}

ERROR_SUGGESTIONS: Dict[str, List[str]] = {
    'browser_start_fail': ['检查Chrome版本与ChromeDriver匹配性', '增加WebDriver自动更新机制', '添加系统资源预检（内存/磁盘）'],
    'login_expired': ['延长登录态保持时间（定期心跳保活）', '增加登录态预检与自动续期'],
    'element_timeout': ['增加选择器冗余（多套方案降级）', '延长等待超时阈值', '添加页面就绪状态检测'],
    'page_load_timeout': ['增加网络质量预检', '添加请求重试机制'],
    'school_switch_fail': ['检查学校列表接口是否有变更', '添加学校搜索的模糊匹配'],
    'school_not_found': ['提供学校名称标准化映射表', '增加学校名称容错（去除空格/标点）'],
    'upload_submit_timeout': ['优化大文件分片上传', '增加提交结果轮询间隔'],
    'file_not_exist': ['检查文件路径是否变更', '确认源文件未被移动或重命名', '在文件监控阶段记录原始路径'],
    'file_unreadable': ['添加文件完整性预检', '扩展更多文件格式支持'],
    'file_corrupted': ['在文件监控阶段校验文件完整性', '要求重新提供原始文件'],
    'unsupported_format': ['在文件监控阶段提前过滤不支持格式'],
    'api_timeout': ['增加本地科目缓存（相同文件名/内容不重复请求AI）', '增加备用AI服务商'],
    'api_key_invalid': ['添加API密钥有效性定期检查', '密钥过期时提前告警'],
    'api_rate_limit': ['增加请求队列与频率控制', '本地缓存AI识别结果'],
    'subject_empty': ['优化AI提示词提升识别率', '添加文件内容有效性预检（避免空壳文件）'],
    'form_validate_fail': ['在提交前做本地表单数据预校验'],
    'subject_not_in_option': ['建立AI识别科目 → 平台科目的映射表'],
    'permission_denied': ['在文件监控阶段检查学校权限'],
    'school_not_activated': ['联系平台管理员开通数智作业服务', '更换已开通服务的学校作为目标学校'],
    'network_error': ['添加网络连通性预检', '增加离线队列功能'],
    'database_error': ['添加数据库定期备份', '增加数据库完整性检查'],
    'unknown': ['排查日志定位具体原因'],
}
