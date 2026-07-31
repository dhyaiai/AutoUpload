"""
失败原因分析 Agent (FailureAnalysisAgent) — AI Agent 版
功能: 按需触发,ReAct 循环驱动 LLM 自主探索数据、深度归因、生成 Markdown 分析报告
特点:
  - 手动触发(点击按钮) → 启动一次 ReAct 分析会话
  - LLM 自主决定分析路径: 发现异常→深挖→形成洞察→生成报告
  - 数据采集全部通过工具调用,LLM 按需查询,避免不必要的数据收集
  - 模板报告作为 AI 禁用/失败时的兜底
"""
import os
import re
import json
import threading
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from deepseek_helper import DeepSeekHelper
from react_loop import ReActLoop, tool
from db_manager import DatabaseManager
from config_manager import ConfigManager
import run_logger
from error_types import (
    UploadStage, ErrorCategory, ErrorType,
    classify_error, ERROR_CLASSIFICATION_RULES,
    ERROR_DESCRIPTIONS, ERROR_SUGGESTIONS,
)


class FailureAnalysisAgent:
    """
    失败原因分析 Agent
    负责数据聚合、归因分析、Markdown 报告生成
    """

    def __init__(self, log_queue=None):
        """
        Args:
            log_queue: 日志队列（可选，用于向GUI输出日志）
        """
        self.db = DatabaseManager()
        self.config = ConfigManager()

        # LLM 提供商：优先 Qwen，否则 DeepSeek
        qwen_key = self.config.qwen_api_key
        if qwen_key:
            self.deepseek = DeepSeekHelper(
                api_url=self.config.qwen_api_url,
                api_key=qwen_key,
                model=self.config.qwen_model
            )
            self._log(f"AnalysisAgent: 使用 Qwen/{self.config.qwen_model}")
        else:
            self.deepseek = DeepSeekHelper()
            self._log("AnalysisAgent: 使用 DeepSeek")

        self.log_queue = log_queue

        # 报告输出目录
        self.reports_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "reports"
        )
        os.makedirs(self.reports_dir, exist_ok=True)

    def _log(self, message: str):
        """发送日志"""
        try:
            if self.log_queue:
                self.log_queue.put(message)
            else:
                print(message)
        except Exception:
            print(message)

    # ─── 公开接口 ───

    def generate_report(self,
                        start_time: str,
                        end_time: str,
                        report_type: str = "custom") -> Optional[str]:
        """
        生成失败分析报告（AI ReAct 优先，模板兜底）

        Args:
            start_time: 起始时间 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'
            end_time: 截止时间 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'
            report_type: 'daily' / 'weekly' / 'custom'

        Returns:
            生成的报告文件路径, 失败返回 None
        """
        try:
            if ' ' not in start_time:
                start_time = f"{start_time} 00:00:00"
            if ' ' not in end_time:
                end_time = f"{end_time} 23:59:59"

            self._log(f"开始生成失败分析报告 ({start_time} ~ {end_time})")

            # ── AI ReAct 分析 + 生成报告 ──
            md_content = None
            if self.config.ai_analysis_agent_enable and self.deepseek.api_key:
                md_content = self._run_analysis_react_loop(start_time, end_time, report_type)
                if md_content:
                    self._log("AI Agent 分析报告生成成功")

            # ── 兜底：模板生成 ──
            if not md_content:
                report_data = self._collect_data(start_time, end_time)
                md_content = self._build_markdown(report_data, start_time, end_time, report_type)
                self._log("使用模板生成分析报告（AI 未启用或失败）")

            filepath = self._save_report(md_content, start_time, end_time, report_type)
            self._log(f"分析报告已生成: {filepath}")
            return filepath

        except Exception as e:
            self._log(f"生成分析报告失败: {e}")
            traceback.print_exc()
            return None

    def generate_weekly_report(self) -> Optional[str]:
        """生成本周周报"""
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        start_time = monday.strftime('%Y-%m-%d 00:00:00')
        end_time = today.strftime('%Y-%m-%d 23:59:59')
        return self.generate_report(start_time, end_time, 'weekly')

    def check_threshold_alert(self, threshold: float = 0.20) -> Optional[str]:
        """检查当日失败率是否超过阈值"""
        today = datetime.now().strftime('%Y-%m-%d')
        start_time = f"{today} 00:00:00"
        end_time = f"{today} 23:59:59"

        records = self.db.get_all_records_by_period(start_time, end_time)
        if not records:
            return None

        total = len(records)
        failed = sum(1 for r in records if r.get('status') == 'failed')
        rate = failed / total if total > 0 else 0

        if rate >= threshold:
            self._log(f"⚠ 当日失败率 {rate:.1%} 超过阈值 {threshold:.0%}，生成紧急报告")
            return self.generate_report(start_time, end_time, 'daily')
        return None

    # ─── AI Agent ReAct 分析 ───

    REACT_ANALYSIS_SYSTEM_PROMPT = """你是一个作业上传系统的数据分析 AI Agent。你会收到一个统计周期，需要自主探索数据、发现模式、生成专业的 Markdown 分析报告。

## 工作流程

1. 先用 query_overview 了解全局
2. 如果发现异常（如高失败率），用 query_error_distribution 查看错误分布
3. 针对占比高的错误类型，用 drill_down_errors 深入分析具体案例
4. 用 query_daily_trend 检查时间趋势，用 query_school_grade_stats 检查是否有特定学校/年级的问题
5. 用 query_retry_effectiveness 评估自动重试的效果
6. 必须用 query_runtime_errors 查看软件运行日志中收集到的错误（即使数据库无失败记录，运行日志中也可能有错误），对这些错误进行归纳，分析出现原因和可能的修复方向
7. 收集到足够的洞察后，生成完整的 Markdown 报告，调用 save_report 保存

## 报告要求

报告必须包含以下七大部分：

## 一、统计概览 — 总上传量、失败量、失败率、重试挽回数、待人工处理数
## 二、错误类型分布 — 一级+二级分类表格，Top 3 深度分析
## 三、分维度深度分析 — 3.1 时间趋势 3.2 学校年级分布 3.3 科目与文件格式
## 四、根因分析与迭代建议 — 基于数据的具体根因和可落地建议（每条至少2条建议）
## 五、运行日志错误分析 — 对运行日志收集到的错误分组归纳（表格：错误模式/分类/次数/首次末次时间），逐组分析错误出现的原因和可能的修复方向；若无错误则说明运行健康
## 六、待人工处理清单 — 表格形式
## 七、附录 — 生成时间、数据来源（SQLite upload_records 表 + logs/ 运行日志）、统计周期

## 输出格式

数据探索通过原生工具调用(tool calls)完成。
报告已通过 save_report 保存后，不再调用工具，直接回复最终结果，内容必须是一个合法的 JSON 对象（不要附加其他文字）：

{"status": "completed"}

注意：
- 先查询再得出结论，不要编造数据
- 优化建议必须具体可执行，避免空话套话
- Markdown 表格要对齐
- 即使数据库无失败记录，也必须检查运行日志错误；两者都无异常时，生成简短的“无异常”报告即可"""

    def _run_analysis_react_loop(self, start_time: str, end_time: str,
                                  report_type: str) -> Optional[str]:
        """
        启动 ReAct 循环让 LLM 自主探索数据并生成报告

        Returns:
            生成的 Markdown 内容，失败返回 None
        """
        type_label = {'daily': '日报', 'weekly': '周报', 'custom': '自定义'}.get(report_type, '自定义')

        # ── 构建工具（闭包捕获 self + start_time/end_time）──

        @tool(description="查询统计周期内的总体数据（总量、失败数、失败率、重试挽回数）。无参数")
        def tool_query_overview():
            stats = self.db.get_failed_stats_by_period(start_time, end_time)
            total = stats['total_uploads']
            failed = stats['total_failed']
            rate = (failed / total * 100) if total > 0 else 0.0
            return {
                "total_uploads": total,
                "total_failed": failed,
                "failure_rate_pct": round(rate, 1),
                "agent_recovered": stats['agent_recovered'],
                "retry_success": stats['retry_success'],
                "manual_pending": stats['manual_pending'],
            }

        @tool(description="查询错误一级分类和二级类型的分布。无参数")
        def tool_query_error_distribution():
            stats = self.db.get_failed_stats_by_period(start_time, end_time)
            return {
                "category_distribution": stats['category_distribution'],
                "type_distribution": stats['type_distribution'][:10],
            }

        @tool(description="查询按天的失败率趋势。无参数")
        def tool_query_daily_trend():
            trend = self.db.get_daily_failure_trend(start_time, end_time)
            return [{
                "date": d.get('date_label', ''),
                "total": d['total'],
                "failed": d['failed'],
                "failure_rate_pct": round((d['failed'] / d['total'] * 100) if d['total'] > 0 else 0, 1)
            } for d in trend[:30]]

        @tool(description="查询按学校+年级维度的失败率排行（Top 15）。无参数")
        def tool_query_school_grade_stats():
            stats = self.db.get_failure_rate_by_school_grade(start_time, end_time)
            result = []
            for s in stats[:15]:
                tot = s['total']
                fail = s['failed']
                result.append({
                    "school": s.get('school', ''),
                    "grade": s.get('grade', ''),
                    "total": tot,
                    "failed": fail,
                    "failure_rate_pct": round((fail / tot * 100) if tot > 0 else 0, 1)
                })
            return result

        @tool(description="查询按科目维度的失败率。无参数")
        def tool_query_subject_stats():
            stats = self.db.get_failure_rate_by_subject(start_time, end_time)
            result = []
            for s in stats:
                tot = s['total']
                fail = s['failed']
                result.append({
                    "subject": s.get('subject', ''),
                    "total": tot,
                    "failed": fail,
                    "failure_rate_pct": round((fail / tot * 100) if tot > 0 else 0, 1)
                })
            return result

        @tool(description="查询各错误类型的重试挽回效果。无参数")
        def tool_query_retry_effectiveness():
            stats = self.db.get_error_type_retry_stats(start_time, end_time)
            return [{
                "error_type": s.get('error_type', '未知'),
                "total": s['total'],
                "retry_success_count": s['retry_success_count'],
                "avg_retry_count": round(s['avg_retry_count'], 1) if s['avg_retry_count'] else 0
            } for s in stats[:10]]

        @tool(description="深入查询某类错误的具体案例。参数: error_type=错误类型, limit=数量(默认10)",
              params={"error_type": "错误类型", "limit": "返回数量(默认10)"})
        def tool_drill_down_errors(error_type="", limit=10):
            records = self.db.get_failed_records_by_period(start_time, end_time)
            if error_type:
                records = [r for r in records if r.get('error_type') == error_type]
            records = records[:int(limit)]
            return [{
                "file_name": r.get('file_name', ''),
                "school": r.get('school', ''),
                "grade": r.get('grade', ''),
                "subject": r.get('subject', ''),
                "error_message": (r.get('error_message') or '')[:100],
                "retry_count": r.get('retry_count', 0),
                "upload_time": r.get('upload_time', ''),
            } for r in records]

        @tool(description="查询待人工处理的记录清单。参数: limit=数量(默认20)",
              params={"limit": "返回数量(默认20)"})
        def tool_query_manual_pending(limit=20):
            records = self.db.get_failed_records_by_period(start_time, end_time)
            pending = [r for r in records
                       if r.get('retry_status') == 'finished' and r.get('status') == 'failed']
            pending = pending[:int(limit)]
            return [{
                "file_name": r.get('file_name', ''),
                "school": r.get('school', ''),
                "grade": r.get('grade', ''),
                "subject": r.get('subject', ''),
                "error_message": (r.get('error_message') or '')[:80],
                "retry_count": r.get('retry_count', 0),
            } for r in pending]

        @tool(description="查询运行日志中收集到的错误（已按同类消息聚合）。返回错误总数与分组列表（模式/分类/次数/首末次时间/样例）。无参数")
        def tool_query_runtime_errors():
            entries = run_logger.collect_errors(start_time, end_time)
            groups = run_logger.aggregate_errors(entries, top_n=15)
            return {
                "total_errors": len(entries),
                "error_groups": groups,
            }

        @tool(description="保存最终 Markdown 报告到文件。参数: content=完整的Markdown报告内容",
              params={"content": "完整的Markdown报告内容"})
        def tool_save_report(content=""):
            """保存报告到文件"""
            if not content or '#' not in content:
                return {"success": False, "error": "报告内容为空或格式异常"}
            filepath = self._save_report(content, start_time, end_time, report_type)
            return {"success": True, "filepath": filepath}

        tools = [
            tool_query_overview,
            tool_query_error_distribution,
            tool_query_daily_trend,
            tool_query_school_grade_stats,
            tool_query_subject_stats,
            tool_query_retry_effectiveness,
            tool_drill_down_errors,
            tool_query_manual_pending,
            tool_query_runtime_errors,
            tool_save_report,
        ]

        task = f"""请分析 {type_label} 数据并生成报告。

统计周期: {start_time} ~ {end_time}
报告类型: {type_label}
生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

请先用 query_overview 了解全局，然后根据数据特征自主决定需要深入查询哪些维度。
收集到足够的洞察后，生成完整的 Markdown 报告并调用 save_report 保存。"""

        max_steps = self.config.get("AI_AGENT_MAX_STEPS", 12)
        agent = ReActLoop(
            llm=self.deepseek,
            system_prompt=self.REACT_ANALYSIS_SYSTEM_PROMPT,
            tools=tools,
            max_steps=max_steps,
            log_fn=lambda msg: self._log(f"AnalysisAgent ReAct: {msg}")
        )

        result = agent.run(task)
        if result["success"]:
            # 尝试从 save_report 的结果获取文件路径
            self._log(f"AI 分析完成 (steps={result['steps']})")
            # 从历史中查找 save_report 工具的返回结果（role=tool 消息）
            for msg in reversed(result.get("history", [])):
                if msg.get("role") != "tool" or msg.get("name") != "save_report":
                    continue
                try:
                    data = json.loads(msg.get("content", ""))
                except (json.JSONDecodeError, TypeError):
                    continue
                saved_path = data.get("filepath")
                if saved_path:
                    # 读取保存的报告内容
                    try:
                        with open(saved_path, 'r', encoding='utf-8') as f:
                            return f.read()
                    except Exception as e:
                        # AI 报告已保存但读回失败 → 会回退模板覆盖 AI 结果, 需留痕排查
                        self._log(f"读取已保存的 AI 报告失败({saved_path}): {e}, 将回退模板重新生成")
            # save_report 未被调用 → 回退模板，不读取旧报告避免返回错误周期的数据

        self._log(f"AI Agent 分析失败(steps={result['steps']}), 回退模板")
        return None

    # ─── 数据收集 ───

    def _collect_data(self, start_time: str, end_time: str) -> Dict:
        """收集所有分析维度数据"""
        data = {}

        # 1. 基础统计
        stats = self.db.get_failed_stats_by_period(start_time, end_time)
        data['stats'] = stats

        # 2. 失败率
        total = stats['total_uploads']
        failed = stats['total_failed']
        data['failure_rate'] = (failed / total * 100) if total > 0 else 0.0

        # 3. 重试成功率
        retry_success = stats['retry_success']
        data['retry_success_rate'] = (retry_success / failed * 100) if failed > 0 else 0.0

        # 4. 错误类型分布
        data['type_distribution'] = stats['type_distribution']

        # 5. 错误分类分布
        data['category_distribution'] = stats['category_distribution']

        # 6. 时间趋势
        data['daily_trend'] = self.db.get_daily_failure_trend(start_time, end_time)

        # 7. 按学校+年级分布
        data['school_grade_stats'] = self.db.get_failure_rate_by_school_grade(start_time, end_time)

        # 8. 按科目分布
        data['subject_stats'] = self.db.get_failure_rate_by_subject(start_time, end_time)

        # 9. 重试效果分析
        data['retry_stats'] = self.db.get_error_type_retry_stats(start_time, end_time)

        # 10. Top 失败详情
        failed_records = self.db.get_failed_records_by_period(start_time, end_time)
        data['top_failed'] = self._get_top_failed(failed_records, top_n=5)

        # 11. 待人工处理清单
        data['manual_pending'] = [
            r for r in failed_records
            if r.get('retry_status') == 'finished' and r.get('status') == 'failed'
        ]

        # 12. 文件格式分布（从文件扩展名推断）
        data['format_stats'] = self._analyze_file_formats(failed_records)

        # 13. 运行日志错误（logs/ 目录初步收集的错误，聚合后供归纳分析）
        runtime_entries = run_logger.collect_errors(start_time, end_time)
        data['runtime_error_total'] = len(runtime_entries)
        data['runtime_error_groups'] = run_logger.aggregate_errors(runtime_entries, top_n=15)

        return data

    def _get_top_failed(self, records: List[Dict], top_n: int = 5) -> List[Dict]:
        """取高频失败文件 Top N（按文件名聚合）"""
        counter = Counter()
        file_details = {}
        for r in records:
            fname = r.get('file_name', '')
            counter[fname] += 1
            if fname not in file_details:
                file_details[fname] = r

        result = []
        for fname, count in counter.most_common(top_n):
            detail = file_details[fname]
            detail['fail_count'] = count
            result.append(detail)
        return result

    def _analyze_file_formats(self, records: List[Dict]) -> List[Dict]:
        """分析文件格式分布"""
        format_counter = Counter()
        for r in records:
            fname = r.get('file_name', '')
            ext = os.path.splitext(fname)[1].lower() if '.' in fname else '未知'
            format_counter[ext] += 1
        return [{'format': k, 'count': v} for k, v in format_counter.most_common()]

    # ─── Markdown 报告生成 ───

    def _build_markdown(self, data: Dict, start_time: str,
                        end_time: str, report_type: str) -> str:
        """构建 Markdown 格式报告"""

        stats = data['stats']
        display_start = start_time[:10]
        display_end = end_time[:10]
        type_label = {'daily': '日报', 'weekly': '周报', 'custom': '自定义'}.get(report_type, '自定义')

        lines = []
        lines.append(f"# 作业上传失败分析报告（{type_label}）")
        lines.append("")
        lines.append(f"> 报告类型: {type_label}")
        lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        # ── 一、统计概览 ──
        lines.append("## 一、统计概览")
        lines.append("")
        lines.append(f"- 统计周期：{display_start} ~ {display_end}")
        lines.append(f"- 总上传次数：{stats['total_uploads']}")
        lines.append(f"- 首次失败数：{stats['total_failed']}")
        lines.append(f"- 整体失败率：{data['failure_rate']:.1f}%")
        lines.append(f"- 自动重试挽回数：{stats['agent_recovered']}")
        lines.append(f"- 重试成功率：{data['retry_success_rate']:.1f}%")
        lines.append(f"- 最终待人工处理数：{stats['manual_pending']}")
        lines.append("")

        # ── 二、错误类型分布 ──
        lines.append("## 二、错误类型分布")
        lines.append("")

        # 一级分类
        lines.append("### 2.1 一级分类（ErrorCategory）")
        lines.append("")
        lines.append("| 错误分类 | 数量 | 占比 |")
        lines.append("| :--- | ---: | ---: |")
        total_failed = stats['total_failed'] or 1
        for item in data['category_distribution']:
            cat = item.get('error_category') or '未分类'
            cnt = item['cnt']
            pct = cnt / total_failed * 100
            lines.append(f"| {self._e(cat)} | {cnt} | {pct:.1f}% |")
        lines.append("")

        # 二级分类
        lines.append("### 2.2 二级分类（ErrorType）Top 10")
        lines.append("")
        lines.append("| 错误类型 | 数量 | 占比 |")
        lines.append("| :--- | ---: | ---: |")
        for item in data['type_distribution'][:10]:
            etype = item.get('error_type') or '未分类'
            cnt = item['cnt']
            pct = cnt / total_failed * 100
            lines.append(f"| {self._e(etype)} | {cnt} | {pct:.1f}% |")
        lines.append("")

        # ── 三、分维度深度分析 ──
        lines.append("## 三、分维度深度分析")
        lines.append("")

        # 3.1 时间趋势
        lines.append("### 3.1 时间趋势")
        lines.append("")
        daily = data['daily_trend']
        if daily:
            lines.append("| 日期 | 总量 | 失败 | 失败率 |")
            lines.append("| :--- | ---: | ---: | ---: |")
            for d in daily:
                date_label = d.get('date_label', '')
                tot = d['total']
                fail = d['failed']
                rate = (fail / tot * 100) if tot > 0 else 0
                lines.append(f"| {date_label} | {tot} | {fail} | {rate:.1f}% |")
        else:
            lines.append("该周期内无数据")
        lines.append("")

        # 3.2 学校年级分布
        lines.append("### 3.2 学校年级分布（失败率 Top 10）")
        lines.append("")
        sg_stats = sorted(data['school_grade_stats'],
                         key=lambda x: x['failed'], reverse=True)[:10]
        if sg_stats:
            lines.append("| 学校 | 年级 | 总量 | 失败 | 失败率 |")
            lines.append("| :--- | :--- | ---: | ---: | ---: |")
            for item in sg_stats:
                school = self._e(item.get('school', ''))
                grade = self._e(item.get('grade', ''))
                tot = item['total']
                fail = item['failed']
                rate = (fail / tot * 100) if tot > 0 else 0
                lines.append(f"| {school} | {grade} | {tot} | {fail} | {rate:.1f}% |")
        else:
            lines.append("该周期内无数据")
        lines.append("")

        # 3.3 科目与文件格式
        lines.append("### 3.3 科目与文件格式分布")
        lines.append("")

        lines.append("**按科目：**")
        lines.append("")
        subj_stats = sorted(data['subject_stats'],
                           key=lambda x: x['failed'], reverse=True)
        if subj_stats:
            lines.append("| 科目 | 总量 | 失败 | 失败率 |")
            lines.append("| :--- | ---: | ---: | ---: |")
            for item in subj_stats:
                subj = self._e(item.get('subject', '未知'))
                tot = item['total']
                fail = item['failed']
                rate = (fail / tot * 100) if tot > 0 else 0
                lines.append(f"| {subj} | {tot} | {fail} | {rate:.1f}% |")
        else:
            lines.append("该周期内无数据")
        lines.append("")

        lines.append("**按文件格式：**")
        lines.append("")
        fmt_stats = data['format_stats']
        if fmt_stats:
            lines.append("| 格式 | 失败数 |")
            lines.append("| :--- | ---: |")
            for item in fmt_stats:
                lines.append(f"| {self._e(item['format'])} | {item['count']} |")
        else:
            lines.append("该周期内无数据")
        lines.append("")

        # ── 四、根因分析与迭代建议 ──
        lines.append("## 四、根因分析与迭代建议")
        lines.append("")

        type_dist = data['type_distribution']
        top_issues = type_dist[:2] if type_dist else []

        for i, issue in enumerate(top_issues, 1):
            etype = issue.get('error_type') or 'unknown'
            cnt = issue['cnt']
            pct = cnt / total_failed * 100

            desc = ERROR_DESCRIPTIONS.get(etype, ERROR_DESCRIPTIONS['unknown'])
            sug = ERROR_SUGGESTIONS.get(etype, ERROR_SUGGESTIONS['unknown'])

            lines.append(f"### 4.{i} Top{i} 问题：{desc[0]}（占比 {pct:.1f}%）")
            lines.append("")
            lines.append(f"- **现象描述**：统计周期内发生 {cnt} 次，占全部失败的 {pct:.1f}%")
            lines.append(f"- **根因推断**：{desc[1]}")
            lines.append(f"- **优化建议**：")
            for s in sug:
                lines.append(f"  - {s}")
            lines.append("")

        if not top_issues:
            lines.append("该周期内无失败数据，无需分析")
            lines.append("")

        # ── 五、运行日志错误分析 ──
        lines.extend(self._build_runtime_error_section(data))

        # ── 六、待人工处理清单 ──
        lines.append("## 六、待人工处理清单")
        lines.append("")
        manual = data['manual_pending']
        if manual:
            lines.append("| 文件名 | 学校 | 年级 | 科目 | 失败原因 | 重试次数 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | ---: |")
            for r in manual[:20]:
                fname = self._e(r.get('file_name', ''))
                school = self._e(r.get('school', ''))
                grade = self._e(r.get('grade', ''))
                subject = self._e(r.get('subject', ''))
                error = self._e((r.get('error_message') or '未知')[:50])
                retry = r.get('retry_count', 0)
                lines.append(f"| {fname} | {school} | {grade} | {subject} | {error} | {retry} |")
        else:
            lines.append("无待人工处理项目")
        lines.append("")

        # ── 七、附录 ──
        lines.append("## 七、附录")
        lines.append("")
        lines.append(f"- 报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 数据来源：SQLite upload_records 表 + logs/ 运行日志")
        lines.append(f"- 统计周期：{display_start} ~ {display_end}")
        lines.append("")

        return "\n".join(lines)

    def _build_runtime_error_section(self, data: Dict) -> List[str]:
        """
        构建“运行日志错误分析”章节
        先展示初步收集的错误分组表格，再由 LLM 归纳原因与修复方向（失败则规则兜底）
        """
        lines = []
        lines.append("## 五、运行日志错误分析")
        lines.append("")

        total = data.get('runtime_error_total', 0)
        groups = data.get('runtime_error_groups', [])

        if not groups:
            lines.append("该周期内运行日志未收集到错误，软件运行健康")
            lines.append("")
            return lines

        lines.append(f"该周期内运行日志共收集到 {total} 条错误，归纳为 {len(groups)} 类：")
        lines.append("")
        lines.append("| 错误模式 | 分类 | 次数 | 首次出现 | 末次出现 |")
        lines.append("| :--- | :--- | ---: | :--- | :--- |")
        for g in groups:
            lines.append(
                f"| {self._e(g['pattern'])} | {self._e(g.get('category', ''))}"
                f"/{self._e(g.get('error_type', ''))} | {g['count']} "
                f"| {g.get('first_time', '')} | {g.get('last_time', '')} |")
        lines.append("")

        # LLM 归纳分析：错误原因 + 修复方向
        ai_analysis = self._summarize_runtime_errors_llm(groups, total)
        if ai_analysis:
            lines.append("### 智能体归纳分析")
            lines.append("")
            lines.append(ai_analysis)
        else:
            # 规则兜底：基于错误类型给出预定义的原因与建议
            lines.append("### 错误原因与修复方向（规则归纳）")
            lines.append("")
            seen_types = set()
            for g in groups[:5]:
                etype = g.get('error_type', 'unknown')
                if etype in seen_types:
                    continue
                seen_types.add(etype)
                desc = ERROR_DESCRIPTIONS.get(etype, ERROR_DESCRIPTIONS['unknown'])
                sug = ERROR_SUGGESTIONS.get(etype, ERROR_SUGGESTIONS['unknown'])
                lines.append(f"- **{desc[0]}**（{g['count']} 次）")
                lines.append(f"  - 可能原因：{desc[1]}")
                lines.append(f"  - 修复方向：{'；'.join(sug)}")
        lines.append("")
        return lines

    def _summarize_runtime_errors_llm(self, groups: List[Dict],
                                      total: int) -> Optional[str]:
        """
        调用 LLM 对运行日志错误进行归纳：分析出现原因与可能的修复方向

        Returns:
            Markdown 分析文本，LLM 不可用或调用失败返回 None
        """
        if not self.deepseek.api_key or not groups:
            return None
        try:
            system_prompt = (
                "你是一个作业自动上传软件的运维分析专家。用户会提供软件运行日志中收集的错误分组数据（JSON）。"
                "请对这些错误进行归纳，输出 Markdown（不要代码块包裹）："
                "按错误组逐一分析，每组包含：**错误归纳**（一句话概括）、**出现原因**（结合错误消息推断根因）、"
                "**修复方向**（至少 2 条具体可执行的建议）。"
                "同类错误可合并分析，优先分析高频错误，不要编造数据中不存在的信息。"
            )
            user_content = json.dumps({
                "error_total": total,
                "error_groups": groups,
            }, ensure_ascii=False)
            result = self.deepseek.chat(system_prompt, user_content)
            if result and result.strip():
                self._log("运行日志错误 LLM 归纳分析完成")
                return result.strip()
        except Exception as e:
            self._log(f"运行日志错误 LLM 分析失败，使用规则兜底: {e}")
        return None

    # ─── 报告文件管理 ───

    def _save_report(self, content: str, start_time: str,
                     end_time: str, report_type: str) -> str:
        """保存报告到文件"""
        type_prefix = {'daily': '日报', 'weekly': '周报', 'custom': '自定义'}.get(report_type, '自定义')
        start_date = start_time[:10].replace('-', '')
        end_date = end_time[:10].replace('-', '')

        if start_date == end_date:
            filename = f"失败分析报告_{type_prefix}_{start_date}.md"
        else:
            filename = f"失败分析报告_{type_prefix}_{start_date}_{end_date}.md"

        filepath = os.path.join(self.reports_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        return filepath

    def open_reports_dir(self) -> str:
        """用系统文件管理器打开报告目录"""
        abs_path = os.path.abspath(self.reports_dir)
        os.startfile(abs_path)
        return abs_path

    # ─── 工具方法 ───

    @staticmethod
    def _e(text: str) -> str:
        """转义 Markdown 特殊字符"""
        if not text:
            return ''
        return str(text).replace('|', '\\|').replace('\n', ' ')


# ─── CLI 独立测试入口 ───
if __name__ == "__main__":
    """
    独立测试 FailureAnalysisAgent 报告生成
    用法: python failure_analysis_agent.py
    """
    agent = FailureAnalysisAgent()

    # 生成近 7 天报告
    today = datetime.now()
    start = (today - timedelta(days=7)).strftime('%Y-%m-%d')
    end = today.strftime('%Y-%m-%d')

    print(f"生成 {start} ~ {end} 的失败分析报告...")
    path = agent.generate_report(start, end, 'weekly')

    if path:
        print(f"报告已生成: {path}")
        # 读取并打印前 50 行
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            print("".join(lines[:50]))
    else:
        print("报告生成失败（可能无数据）")
