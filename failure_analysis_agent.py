"""
失败原因分析 Agent (FailureAnalysisAgent)
功能: 按需/定时聚合失败数据,多维度归因分析,自动生成标准Markdown分析报告
特点:
  - 支持手动触发 + 定时生成 + 阈值触发三种模式
  - 多维度分析: 概览、错误分布、时间趋势、业务维度、重试效果
  - 报告自动归档到 reports/ 目录
  - 兼容历史无结构化字段的旧数据（关键词正则匹配兜底）
"""
import os
import re
import json
import threading
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from db_manager import DatabaseManager
from config_manager import ConfigManager
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
        生成失败分析报告

        Args:
            start_time: 起始时间 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'
            end_time: 截止时间 'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DD'
            report_type: 报告类型 'daily' / 'weekly' / 'custom'

        Returns:
            生成的报告文件路径, 失败返回 None
        """
        try:
            # 标准化时间格式
            if ' ' not in start_time:
                start_time = f"{start_time} 00:00:00"
            if ' ' not in end_time:
                end_time = f"{end_time} 23:59:59"

            self._log(f"开始生成失败分析报告 ({start_time} ~ {end_time})")

            # 收集所有维度数据
            report_data = self._collect_data(start_time, end_time)

            # 生成 Markdown 内容
            md_content = self._build_markdown(report_data, start_time, end_time, report_type)

            # 写入文件
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
        # 本周一 00:00
        monday = today - timedelta(days=today.weekday())
        start_time = monday.strftime('%Y-%m-%d 00:00:00')
        end_time = today.strftime('%Y-%m-%d 23:59:59')
        return self.generate_report(start_time, end_time, 'weekly')

    def check_threshold_alert(self, threshold: float = 0.20) -> Optional[str]:
        """
        检查当日失败率是否超过阈值，超过则生成紧急报告

        Args:
            threshold: 失败率阈值（默认 20%）

        Returns:
            超过阈值时返回报告路径，否则返回 None
        """
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

        # ── 五、待人工处理清单 ──
        lines.append("## 五、待人工处理清单")
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

        # ── 六、附录 ──
        lines.append("## 六、附录")
        lines.append("")
        lines.append(f"- 报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 数据来源：SQLite upload_records 表")
        lines.append(f"- 统计周期：{display_start} ~ {display_end}")
        lines.append("")

        return "\n".join(lines)

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
