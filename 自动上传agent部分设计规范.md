# 作业自动上传工具 - 双 Agent 功能 需求与设计文档

> 本文档用于指导 Claude Code 基于现有项目代码，实现「失败自动接管 Agent」和「失败原因分析 Agent」两个功能模块。所有设计均基于现有项目架构，保证低侵入、高复用、不破坏原有稳定性。

------

## 1. 文档说明与目标

### 1.1 背景

现有作业自动上传工具已实现：文件夹监控、AI 科目识别、Selenium 自动上传、GUI 管理、SQLite 持久化、数据统计等核心能力。但上传失败后仍需人工手动重试、人工排查原因，运营成本较高，且失败数据未形成结构化沉淀，无法支撑产品迭代。

### 1.2 新增目标

新增两个独立 Agent 模块，形成「自动自愈 + 数据沉淀」的闭环：

1. **失败自动接管 Agent（AutoRetryAgent）**：后台常驻，自动扫描失败记录，精准定位失败阶段与根因，执行分级自愈策略，最大化减少人工干预，仅将不可自愈的错误留给人工处理。
2. **失败原因分析 Agent（FailureAnalysisAgent）**：按需 / 定时聚合失败数据，做多维度归因分析，自动生成标准 Markdown 分析报告，为产品迭代提供量化数据支撑。

### 1.3 设计原则

- **低侵入**：不修改核心上传主流程，复用现有模块能力，仅做旁路增强
- **线程安全**：不破坏原有单线程上传机制，避免浏览器并发冲突
- **可配置**：所有策略、阈值、开关均通过配置文件控制，可灵活调整
- **可观测**：所有 Agent 动作全程留痕，可追溯、可复盘

------

## 2. 现有系统上下文

### 2.1 现有模块架构

项目为分层模块化 + 生产者消费者模式，核心模块如下：

表格

| 模块文件                | 核心职责                                                     |
| :---------------------- | :----------------------------------------------------------- |
| `main.py`               | 程序入口，启动 GUI 与后台工作线程，协调生命周期              |
| `upload_processor.py`   | 单线程消费任务队列，执行完整上传流程                         |
| `browser_automation.py` | Chrome 浏览器自动化，单例模式，封装登录、学校切换、上传等操作 |
| `db_manager.py`         | SQLite 数据库管理，单例模式，`upload_records` 表存储上传记录 |
| `file_monitor.py`       | watchdog 监听文件新增，放入任务队列                          |
| `gui_manager.py`        | tkinter 图形管理界面                                         |
| `config_manager.py`     | 配置文件管理，单例模式                                       |

### 2.2 现有线程模型

- 主线程：tkinter GUI 主循环
- 后台线程：`backend_worker()`，包含文件监控线程 + 上传处理线程
- 线程间通过 `queue.Queue` 安全通信：`task_queue`（任务队列）、`log_queue`（日志队列）

### 2.3 现有数据库表

当前 `upload_records` 核心字段：

sql

```
id, file_name, file_path, folder_name, school, grade, subject,
status (success/failed), error_message, retry_count, upload_time
```

------

## 3. Agent 1：失败自动接管 Agent (AutoRetryAgent)

### 3.1 定位与职责

- **定位**：后台常驻的自愈执行单元，替代人工完成失败文件的自动重试、故障自检与浏览器自愈
- **核心职责**：
  1. 定时扫描数据库中 `status='failed'` 的记录，筛选符合重试条件的文件
  2. 精准定位失败发生的阶段与根因，做交叉验证，避免误判
  3. 按错误类型匹配分级自愈策略，执行对应修复动作
  4. 重试前执行环境复位，确保页面状态干净，提高重试成功率
  5. 熔断保护：平台大面积故障时停止重试，避免无效请求
  6. 全程更新数据库状态，与手动重试功能兼容
  7. 自动重试上传成功后对本次作业数据进行记录,方便用户了解agent的执行情况,方便后续的对agent的完善与开发,在失败上传记录表再添加一列叫agent接管是否成功,成功的填时,失败的填否

### 3.2 线程与生命周期

- **启动方式**：在 `main.py` 的 `backend_worker` 中启动独立后台线程，随程序启停
- **运行模式**：定时轮询 + 事件驱动，单线程运行，不与主上传线程并发操作浏览器
- **执行边界**：仅做诊断、自愈前置操作、任务入队，实际上传仍通过 `task_queue` 交由原 `UploadProcessor` 串行执行，保证线程安全

### 3.3 失败定位机制（核心）

#### 3.3.1 上传全流程阶段埋点

新增全局阶段枚举，在上传流程每个关键节点埋点，失败时记录当前阶段，从源头定位故障位置。

新增文件 `error_types.py`，定义：

python

```
from enum import Enum

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
```

改造 `upload_processor.py` 的 `_process_file` 方法，每进入一个阶段更新当前阶段变量，异常捕获时将失败阶段写入数据库。

#### 3.3.2 结构化错误分类

在 `error_types.py` 中新增错误分类枚举，所有模块抛出异常时必须对应到标准错误类型：

python

```
class ErrorCategory(str, Enum):
    """错误一级分类"""
    BROWSER_ERROR = "browser_error"
    FILE_PROCESS_ERROR = "file_error"
    AI_SERVICE_ERROR = "ai_error"
    PLATFORM_BIZ_ERROR = "biz_error"
    SYSTEM_ENV_ERROR = "system_error"
    UNKNOWN_ERROR = "unknown_error"

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
    
    # 系统环境类
    NETWORK_ERROR = "network_error"
    DATABASE_ERROR = "database_error"
```

#### 3.3.3 根因交叉验证

Agent 不能直接信任存储的错误类型，必须按优先级做二次校验，避免误判：

1. **第一优先级**：结构化字段 `fail_stage` + `error_type` + `error_category`
2. **第二优先级**：`error_context` JSON 上下文字段（页面 URL、搜索结果、文件信息等）
3. **第三优先级**：主动轻量探测（如调用 `browser.check_login_status()`、检查文件是否存在）

### 3.4 分级自愈策略

按修复力度从低到高分为 5 级，Agent 根据「阶段 + 错误类型」匹配对应策略，避免小题大做。

表格

| 级别        | 策略动作                                   | 适用场景                               |
| :---------- | :----------------------------------------- | :------------------------------------- |
| L1 轻量重试 | 原地重试当前步骤，换用更鲁棒的元素操作方式 | 元素点击失效、偶发超时                 |
| L2 页面复位 | 关闭残留弹窗、返回首页、重新进入上传流程   | 弹窗遮挡、页面状态错乱                 |
| L3 环境重置 | 刷新页面、重新校验学校                     | 页面渲染异常、学校状态不对             |
| L4 服务重启 | 重启浏览器、重新登录                       | 登录失效、浏览器崩溃、页面卡死         |
| L5 人工兜底 | 标记为待人工处理，停止自动重试             | 文件损坏、学校不存在、权限不足等硬错误 |

#### 核心策略映射表

表格

| 失败阶段      | 错误类型              | 自愈策略                          | 最大重试次数 |
| :------------ | :-------------------- | :-------------------------------- | :----------- |
| READ_FILE     | FILE_NOT_EXIST        | L5：直接标记人工处理              | 0            |
| READ_FILE     | FILE_UNREADABLE       | L1：等待 5 秒重读；仍失败→L5      | 1            |
| AI_CLASSIFY   | API_TIMEOUT           | L1：间隔 2 秒重试                 | 2            |
| AI_CLASSIFY   | API_KEY_INVALID       | L5：标记人工处理                  | 0            |
| BROWSER_INIT  | BROWSER_START_FAIL    | L4：重启浏览器；失败→L5           | 2            |
| SCHOOL_CHECK  | ELEMENT_TIMEOUT       | L1：JS 点击重试；失败→L2 复位页面 | 2            |
| SCHOOL_CHECK  | SCHOOL_NOT_FOUND      | L5：标记人工处理                  | 0            |
| FORM_FILL     | SUBJECT_NOT_OPTION    | L5：标记人工处理                  | 0            |
| SUBMIT_UPLOAD | LOGIN_EXPIRED         | L4：重启浏览器重登，再完整重传    | 1            |
| SUBMIT_UPLOAD | UPLOAD_SUBMIT_TIMEOUT | L2：关闭弹窗重进，再提交          | 2            |
| 任意阶段      | NETWORK_ERROR         | L1：指数退避重试                  | 3            |

### 3.5 环境复位机制

每次重试前必须执行环境复位，将浏览器恢复到干净首页状态，避免中间状态导致的二次失败。

在 `browser_automation.py` 中新增 `reset_to_home()` 方法，执行流程：

1. 发送 ESC 键关闭所有下拉 / 浮层
2. 关闭所有残留对话框（上传框、学校切换框、错误提示）
3. 导航回平台首页
4. 校验登录状态，异常则触发重启
5. 返回复位是否成功

### 3.6 幂等与防冲突设计

1. **处理中状态**：数据库新增 `retry_status` 字段，取值 `pending / processing / finished`。Agent 扫描仅取 `pending` 记录，取到后先标记为 `processing` 再处理。
2. **与手动重试兼容**：人工点击「重新上传」时同样标记为 `processing`，Agent 自动跳过。
3. **重试次数控制**：复用现有 `retry_count` 字段，达到 `MAX_RETRY_COUNT` 后停止自动重试。
4. **指数退避**：不同重试次数对应不同等待间隔，默认 [30s, 120s, 600s]。

### 3.7 熔断保护

- **单类错误熔断**：5 分钟内同一错误类型连续失败 10 次，暂停该类错误自动重试 30 分钟
- **浏览器熔断**：连续重启浏览器 3 次仍失败，停止所有浏览器相关自愈
- **全量熔断**：整体失败率超过 30%，暂停全部自动重试，输出告警日志

### 3.8 新增配置项

加入 `config.json`，兼容现有配置体系：

json

```
{
  "AUTO_RETRY_ENABLE": true,
  "AUTO_RETRY_SCAN_INTERVAL": 60,
  "AUTO_RETRY_BACKOFF_SECONDS": [30, 120, 600],
  "AUTO_RETRY_CIRCUIT_BREAKER_THRESHOLD": 10,
  "AUTO_RETRY_CIRCUIT_BREAKER_DURATION": 1800
}
```

### 3.9 完整执行流程

plaintext

```
定时扫描 upload_records 表
    ↓
筛选：status='failed' 且 retry_status='pending' 且 retry_count < 上限
    ↓
读取结构化字段：fail_stage + error_type + error_context
    ↓
交叉验证：主动探测确认根因准确性
    ↓
匹配分级自愈策略
    ↓
执行环境复位（清理弹窗、回到首页）
    ↓
执行对应自愈动作（如重启浏览器、切换学校）
    ↓
将文件重新放入 task_queue，标记 retry_status='processing'
    ↓
等待 UploadProcessor 执行完成
    ↓
校验最终结果，更新数据库状态
    ↓
成功：status→success，retry_status→finished
失败：retry_count+1，retry_status→pending，更新错误信息
达上限：retry_status→finished，标记需人工处理
```

------

## 4. Agent 2：失败原因分析 Agent (FailureAnalysisAgent)

### 4.1 定位与职责

- **定位**：数据洞察单元，自动聚合失败数据、分类归因、输出结构化分析报告
- **核心职责**：
  1. 按周期拉取失败与重试记录，做结构化分类统计
  2. 多维度分析失败分布，识别高频根因
  3. 生成标准 Markdown 分析报告，自动归档
  4. 提供 GUI 手动触发生成入口

### 4.2 触发方式

1. **定时生成**：每周一启动软件时生成上周周报
2. **手动生成**：在「数据统计」标签页新增按钮，支持选择时间范围（今日 / 近 7 天 / 近 30 天 / 自定义）
3. **阈值触发**：单日失败率超过 20% 时，立即生成紧急分析报告

### 4.3 错误分类规则

基于 `error_category` 和 `error_type` 字段做聚合，对历史无结构化字段的旧数据，通过关键词正则匹配兜底分类。

### 4.4 分析维度

1. **整体概览**：总上传量、失败量、失败率、自动重试挽回数、重试成功率、待人工处理数
2. **错误类型分布**：一级 / 二级分类占比排名，定位 Top 问题
3. **时间维度**：按天 / 小时的失败率趋势，识别高峰时段
4. **业务维度**：
   - 按学校 / 年级：失败率异常的学校年级
   - 按科目：失败率高的科目
   - 按文件格式：各格式的失败占比
5. **重试效果分析**：各错误类型的重试成功率、平均重试次数
6. **典型案例**：Top5 高频失败文件详情

### 4.5 Markdown 报告标准结构

输出文件必须严格遵循以下结构，保存为 UTF-8 编码：

markdown

```
# 作业上传失败分析报告
## 一、统计概览
- 统计周期：YYYY-MM-DD ~ YYYY-MM-DD
- 总上传次数：XXX
- 首次失败数：XXX
- 整体失败率：XX.X%
- 自动重试挽回数：XXX
- 重试成功率：XX.X%
- 最终待人工处理数：XXX

## 二、错误类型分布
（表格：错误类型、数量、占比、环比变化）

## 三、分维度深度分析
### 3.1 时间趋势
### 3.2 学校年级分布
### 3.3 科目与文件格式分布

## 四、根因分析与迭代建议
### 4.1 Top1 问题：XXX（占比 XX%）
- 现象描述
- 根因推断
- 优化建议

### 4.2 Top2 问题：XXX（占比 XX%）
...

## 五、待人工处理清单
（表格：文件名、学校、年级、科目、失败原因、重试次数）

## 六、附录
- 报告生成时间
- 数据来源：SQLite upload_records 表
```

### 4.6 输出与归档

- 报告存放路径：程序目录下 `reports/` 文件夹
- 命名规则：`失败分析报告_日报_YYYYMMDD.md`、`失败分析报告_周报_YYYYMMDD_YYYYMMDD.md`
- GUI 中提供「打开报告目录」按钮

------

## 5. 数据库结构变更

### 5.1 表结构修改

在 `upload_records` 表中新增以下字段（兼容旧数据，默认值为 NULL）：

sql

```
-- 失败阶段
ALTER TABLE upload_records ADD COLUMN fail_stage TEXT DEFAULT NULL;
-- 错误一级分类
ALTER TABLE upload_records ADD COLUMN error_category TEXT DEFAULT NULL;
-- 错误二级类型
ALTER TABLE upload_records ADD COLUMN error_type TEXT DEFAULT NULL;
-- 错误上下文 JSON
ALTER TABLE upload_records ADD COLUMN error_context TEXT DEFAULT NULL;
-- 重试处理状态 pending/processing/finished
ALTER TABLE upload_records ADD COLUMN retry_status TEXT DEFAULT 'pending';
```

### 5.2 新增数据库接口

`db_manager.py` 中新增方法：

1. `add_failed_record_structured(...)`：结构化写入失败记录
2. `get_pending_failed_records(limit=20)`：获取待处理的失败记录
3. `update_retry_status(record_id, status)`：更新重试处理状态
4. `get_failed_stats_by_period(start_time, end_time)`：按时间范围聚合失败统计数据
5. `get_failed_records_by_period(start_time, end_time)`：获取指定周期所有失败记录

------

## 6. 代码模块与文件规划

### 6.1 新增文件

表格

| 文件名                      | 职责                                 |
| :-------------------------- | :----------------------------------- |
| `error_types.py`            | 全局错误枚举、阶段枚举、错误匹配规则 |
| `auto_retry_agent.py`       | 失败自动接管 Agent 主逻辑            |
| `failure_analysis_agent.py` | 失败分析报告 Agent 主逻辑            |

### 6.2 修改文件

表格

| 文件名                  | 修改内容                                 |
| :---------------------- | :--------------------------------------- |
| `db_manager.py`         | 新增字段、新增接口方法                   |
| `upload_processor.py`   | 全流程阶段埋点，结构化写入失败信息       |
| `browser_automation.py` | 新增 `reset_to_home()` 环境复位方法      |
| `main.py`               | 后台线程中启动 AutoRetryAgent            |
| `gui_manager.py`        | 统计页新增「生成失败分析报告」按钮与入口 |
| `config_manager.py`     | 新增配置项的 property 访问器             |
| `config.json`           | 新增 Agent 相关配置项                    |

------

## 7. 实现优先级与阶段

### 阶段一：基础能力（最高优先级）

1. 新增 `error_types.py` 枚举定义
2. 数据库字段扩展 + 对应接口
3. `upload_processor.py` 阶段埋点，结构化写入失败信息
4. AutoRetryAgent 基础框架：扫描 + 简单重试 + 状态管理

### 阶段二：增强能力

1. 分级自愈策略 + 环境复位机制
2. 交叉验证逻辑 + 熔断保护
3. FailureAnalysisAgent 核心：数据统计 + MD 报告生成
4. GUI 集成分析报告入口

### 阶段三：优化完善

1. 关键词兜底匹配引擎（兼容历史数据）
2. 定时报告生成
3. 阈值触发紧急报告
4. 准确率统计与人工标注反馈入口

------

## 8. 实现约束与注意事项

1. **线程安全红线**：AutoRetryAgent 绝对不能直接调用 `upload_file` 操作浏览器，必须通过 `task_queue` 交由原 UploadProcessor 串行执行，防止浏览器并发冲突、科目串位。
2. **单例复用**：所有数据库、浏览器、配置均复用现有单例，不创建新实例。
3. **向后兼容**：新增字段必须有默认值，旧数据、旧配置必须能正常运行，不能破坏已有功能。
4. **异常兜底**：Agent 自身所有循环必须包裹异常捕获，自身崩溃不能影响主程序运行。
5. **日志规范**：所有 Agent 动作都通过 `log_queue` 输出到 GUI 日志区，格式与现有日志保持一致。
6. **资源释放**：程序退出时 Agent 线程必须能随 stop_event 优雅停止，无残留进程。

------

## 9. 验收标准

### AutoRetryAgent

- 可自动扫描并重试失败文件，无需人工干预
- 元素超时、登录失效、浏览器崩溃三类高频错误可自动自愈
- 不会与手动重试、主上传流程产生并发冲突
- 达到熔断阈值时可自动停止重试
- 程序关闭时可优雅退出，无资源泄漏

### FailureAnalysisAgent

- 可手动生成指定时间范围的 Markdown 分析报告
- 报告包含概览、错误分布、多维度分析、优化建议四大核心部分
- 数据统计准确，与数据库记录一致
- 报告文件正常生成，格式规范、可直接阅读