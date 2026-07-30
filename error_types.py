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
    PIPELINE_STUCK = "pipeline_stuck"          # 流水线卡死（看门狗强制打断）

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
    PAGE_ERROR_PERSISTENT = "page_error_persistent"  # 页面显示不可恢复的业务错误

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
    (UploadStage.SCHOOL_CHECK, ErrorType.UPLOAD_SUBMIT_TIMEOUT): (RetryLevel.L3_ENV_RESET, 2),

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
    (UploadStage.SUBMIT_UPLOAD, ErrorType.PAGE_ERROR_PERSISTENT):  (RetryLevel.L5_MANUAL, 0),

    # 全局网络错误（任意阶段）
    (None, ErrorType.NETWORK_ERROR): (RetryLevel.L1_LIGHT_RETRY, 3),

    # 流水线卡死（任意阶段）——看门狗已强制关闭浏览器，必须完整恢复
    (None, ErrorType.PIPELINE_STUCK): (RetryLevel.L4_SERVICE_RESTART, 2),

    # 未知错误兜底（任意阶段/任意错误类型 → 人工处理）
    (None, ErrorType.UNKNOWN): (RetryLevel.L5_MANUAL, 0),
}


# ─── 错误分类推断规则 ───
# 根据异常信息和阶段推断 ErrorCategory 和 ErrorType
# 用于兼容旧数据（无结构化字段的失败记录）

ERROR_CLASSIFICATION_RULES: List[tuple] = [
    # (关键词列表, ErrorCategory, ErrorType)
    # 看门狗强制打断（必须排在"上传/超时"等泛化规则之前）
    (["[WATCHDOG]", "流水线卡死"],
     ErrorCategory.BROWSER_ERROR, ErrorType.PIPELINE_STUCK),
    # 浏览器相关
    (["浏览器启动失败", "browser_start", "chrome", "webdriver", "driver",
      "无法启动浏览器", "浏览器崩溃", "browser crash"],
     ErrorCategory.BROWSER_ERROR, ErrorType.BROWSER_START_FAIL),
    (["登录失效", "login expired", "未登录", "重新登录", "登录态",
      "login status", "请先登录",
      "已被迫下线", "异地登录", "另一个地点登录", "被迫下线", "账号在另一个地点",
      "您的账号", "被踢下线", "账号被踢"],
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
    (["只能上传一个文件", "上传文件数量超限"],
     ErrorCategory.BROWSER_ERROR, ErrorType.FORM_VALIDATE_FAIL),
    (["权限不足", "无权限", "permission denied", "没有权限", "无权操作",
      "科目不匹配", "不在可选范围", "已被禁用", "账号异常", "已被锁定",
      "文件格式错误", "不支持的文件类型", "文件大小超限", "文件已存在",
      "重复提交", "该作业已提交", "已提交过"],
     ErrorCategory.PLATFORM_BIZ_ERROR, ErrorType.PAGE_ERROR_PERSISTENT),
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
    'pipeline_stuck': ('流水线卡死', '浏览器/页面长时间无响应，看门狗强制关闭浏览器打断，需完整恢复环境'),
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
    'page_error_persistent': ('页面业务错误', '网页显示不可恢复的业务错误（权限/禁用/重复提交等），重试无法修复'),
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
    'pipeline_stuck': ['检查网络质量与平台服务器响应', '排查页面是否有未适配的弹窗/遮罩', '必要时调整WATCHDOG_STAGE_TIMEOUTS阈值'],
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
    'page_error_persistent': ['查看网页错误详情定位具体业务原因', '确认账号权限和学校服务状态', '联系平台管理员处理业务限制'],
    'network_error': ['添加网络连通性预检', '增加离线队列功能'],
    'database_error': ['添加数据库定期备份', '增加数据库完整性检查'],
    'unknown': ['排查日志定位具体原因'],
}

# ─── 页面错误关键词分类 ───
# 用于 capture_page_error() 快速判断页面错误是否可恢复
# 格式: (关键词列表, ErrorType, is_permanent)
# is_permanent=True 表示该错误无法通过重试修复，应直接转人工处理

PAGE_ERROR_PATTERNS: List[tuple] = [
    # 永久性业务错误（重试无法修复）
    (["该校未开通数智作业服务", "未开通数智作业", "school not activated"],
     ErrorType.SCHOOL_NOT_ACTIVATED, True),
    (["权限不足", "无权限", "permission denied", "没有权限", "无权操作"],
     ErrorType.PERMISSION_DENIED, True),
    (["科目不匹配", "科目不在可选范围", "不在可选范围"],
     ErrorType.SUBJECT_NOT_OPTION, True),
    (["文件格式错误", "不支持的文件类型", "文件格式不支持"],
     ErrorType.UNSUPPORTED_FORMAT, True),
    (["文件大小超限", "文件过大"],
     ErrorType.PAGE_ERROR_PERSISTENT, True),
    (["重复提交", "该作业已提交", "已提交过", "请勿重复提交"],
     ErrorType.PAGE_ERROR_PERSISTENT, True),
    (["账号异常", "已被禁用", "已被锁定", "账号已过期"],
     ErrorType.PAGE_ERROR_PERSISTENT, True),
    (["文件已存在", "同名文件已上传"],
     ErrorType.PAGE_ERROR_PERSISTENT, True),

    # 可恢复错误（重试/恢复后可修复）
    (["登录失效", "请重新登录", "登录过期", "会话过期", "login expired",
      "已被迫下线", "异地登录", "另一个地点登录", "被迫下线", "账号在另一个地点",
      "您的账号", "被踢下线", "账号被踢"],
     ErrorType.LOGIN_EXPIRED, False),
    (["网络超时", "网络异常", "连接超时", "请求超时", "network timeout"],
     ErrorType.NETWORK_ERROR, False),
    (["表单校验", "请选择", "必填", "格式不正确"],
     ErrorType.FORM_VALIDATE_FAIL, False),
    (["上传超时", "处理超时"],
     ErrorType.UPLOAD_SUBMIT_TIMEOUT, False),
    (["系统繁忙", "服务异常", "请稍后重试", "服务器错误"],
     ErrorType.PAGE_ERROR_PERSISTENT, False),  # 临时性服务端错误，可重试
]

# ─── 会话丢失关键词 ───
# 用于检测账号被踢下线/异地登录等会话失效场景
# browser_automation.py 和 upload_processor.py 统一从此处导入

SESSION_LOST_KEYWORDS = [
    "已被迫下线", "被迫下线", "异地登录", "另一个地点登录",
    "账号在另一个地点", "您的账号",
]
