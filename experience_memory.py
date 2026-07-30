"""
经验记忆模块 (ExperienceMemory)
功能: 记录 AutoRetryAgent 每次失败处置的经验（错误指纹→动作序列→是否成功），
实现"越用越聪明"的自愈闭环:
  1. 处理新失败时，查同指纹的历史成功方案注入 prompt，LLM 优先复用
  2. 用成功率统计动态修正 STRATEGY_MAP 静态映射（规则引擎路径）
安全约束: L5_MANUAL（永久性业务错误）映射永不被统计修正，保持人工兜底
"""
import json
from typing import Dict, List, Optional, Tuple

from db_manager import DatabaseManager
from error_types import RetryLevel, get_strategy


# 动态策略修正阈值
MIN_SAMPLES = 5              # 参与修正的最小样本数
OVERRIDE_SUCCESS_RATE = 0.6  # 其他级别成功率达到该值才可覆盖静态级别
POOR_SUCCESS_RATE = 0.2      # 静态级别成功率低于该值时升一级

# 重试级别升级顺序（L4 封顶，L5 不参与动态修正）
_LEVEL_ORDER = [
    RetryLevel.L1_LIGHT_RETRY,
    RetryLevel.L2_PAGE_RESET,
    RetryLevel.L3_ENV_RESET,
    RetryLevel.L4_SERVICE_RESTART,
]


class ExperienceMemory:
    """
    经验记忆管理器
    持有 DatabaseManager，负责指纹计算、经验记录/回填、
    历史成功方案的 prompt 提示构建、动态策略修正
    """

    def __init__(self, db: DatabaseManager):
        self.db = db

    # ─── 指纹 ───

    @staticmethod
    def build_fingerprint(error_type: str, fail_stage: str, page_state: str) -> str:
        """
        构建错误指纹: error_type|fail_stage|page_state，空值归一为 unknown

        Args:
            error_type: ErrorType 枚举值
            fail_stage: UploadStage 枚举值
            page_state: 决策时页面状态(home/login/no_browser/...)

        Returns:
            指纹字符串
        """
        parts = [
            (error_type or "unknown").strip() or "unknown",
            (fail_stage or "unknown").strip() or "unknown",
            (page_state or "unknown").strip() or "unknown",
        ]
        return "|".join(parts)

    # ─── 记录与回填 ───

    def record_disposal(self, error_type: str, fail_stage: str, page_state: str,
                        record_id: int, file_name: str,
                        action_sequence: List[str], decision_action: str,
                        retry_level: str = None, source: str = "react",
                        outcome: str = "pending") -> Optional[int]:
        """
        记录一次处置经验

        Args:
            error_type/fail_stage/page_state: 指纹组成字段
            record_id: 关联的上传记录ID
            file_name: 文件名
            action_sequence: 动作序列列表(修复+决策工具名)
            decision_action: enqueue/manual/skip
            retry_level: L1~L4（enqueue 时有效）
            source: 决策来源 react/rule/fastpath
            outcome: 初始结果(enqueue为pending, manual/skip立即定格)

        Returns:
            经验记录ID（失败返回 None）
        """
        fingerprint = self.build_fingerprint(error_type, fail_stage, page_state)
        return self.db.add_repair_experience(
            fingerprint=fingerprint,
            error_type=error_type or "unknown",
            fail_stage=fail_stage or "unknown",
            page_state=page_state or "unknown",
            record_id=record_id,
            file_name=file_name,
            action_sequence=json.dumps(action_sequence or [], ensure_ascii=False),
            decision_action=decision_action,
            retry_level=retry_level,
            decision_source=source,
            outcome=outcome,
        )

    def mark_outcome(self, exp_id: int, success: bool):
        """
        回填入队重试的最终结果（由 on_upload_result 回调触发）

        Args:
            exp_id: 经验记录ID
            success: 重试上传是否成功
        """
        self.db.update_experience_outcome(exp_id, "success" if success else "failed")

    # ─── 历史经验注入 ───

    def build_history_hint(self, error_type: str, fail_stage: str,
                           page_state: str, top_n: int = 3) -> str:
        """
        构建同指纹历史成功方案的 prompt 注入文本
        按 action_sequence 聚合统计成功率，取成功率最高的前 top_n 条

        Returns:
            注入文本；无成功经验时返回空字符串
        """
        fingerprint = self.build_fingerprint(error_type, fail_stage, page_state)
        experiences = self.db.get_experiences_by_fingerprint(fingerprint, limit=50)
        if not experiences:
            return ""

        # 按动作序列聚合成功/尝试次数
        stats: Dict[str, Dict] = {}
        for exp in experiences:
            seq_json = exp.get("action_sequence") or "[]"
            try:
                seq = json.loads(seq_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not seq:
                continue
            key = " → ".join(str(s) for s in seq)
            entry = stats.setdefault(key, {"total": 0, "success": 0})
            entry["total"] += 1
            if exp.get("outcome") == "success":
                entry["success"] += 1

        # 只保留有成功记录的方案，按成功率降序、尝试次数降序排序
        candidates = [
            (key, v["success"], v["total"], v["success"] / v["total"])
            for key, v in stats.items() if v["success"] > 0
        ]
        if not candidates:
            return ""
        candidates.sort(key=lambda x: (-x[3], -x[2]))

        lines = [f"## 历史处置经验（同指纹: {fingerprint}）",
                 "以下是过去处理相同错误指纹时验证过的方案（按成功率排序）："]
        for i, (key, success, total, _rate) in enumerate(candidates[:top_n], 1):
            lines.append(f"{i}. {key}  [成功{success}/尝试{total}]")
        lines.append("请优先复用上述成功方案的动作序列；若执行中某步失败，再自行诊断换用其他方案。")
        return "\n".join(lines)

    # ─── 动态策略修正 ───

    def get_adjusted_strategy(self, fail_stage: str,
                              error_type: str) -> Tuple[RetryLevel, int, bool]:
        """
        基于历史成功率统计动态修正 STRATEGY_MAP 静态策略

        规则:
          1. 静态级别为 L5_MANUAL 时永不修正（永久性错误安全约束）
          2. 存在样本数≥MIN_SAMPLES 且成功率≥OVERRIDE_SUCCESS_RATE 的级别
             且与静态不同 → 采用该级别（取成功率最高，平手取更轻量级别）
          3. 否则静态级别样本数≥MIN_SAMPLES 且成功率<POOR_SUCCESS_RATE → 升一级(L4封顶)
          4. max_retries 沿用静态值

        Returns:
            (RetryLevel, max_retries, adjusted) — adjusted=True 表示被经验修正
        """
        static_level, max_retries = get_strategy(fail_stage, error_type)

        # L5 人工兜底永不修正
        if static_level == RetryLevel.L5_MANUAL:
            return (static_level, max_retries, False)

        try:
            rows = self.db.get_experience_strategy_stats(fail_stage, error_type)
        except Exception:
            return (static_level, max_retries, False)

        # 级别 → (总数, 成功数, 成功率)，只统计 L1~L4 有效级别
        level_stats: Dict[RetryLevel, Tuple[int, int, float]] = {}
        for row in rows:
            try:
                level = RetryLevel(row.get("retry_level"))
            except (ValueError, TypeError):
                continue
            if level not in _LEVEL_ORDER:
                continue
            total = row.get("total") or 0
            success = row.get("success_count") or 0
            if total > 0:
                level_stats[level] = (total, success, success / total)

        # 规则2: 高成功率级别覆盖（成功率降序，平手取更轻量级别）
        override_candidates = [
            (level, stat[2]) for level, stat in level_stats.items()
            if stat[0] >= MIN_SAMPLES and stat[2] >= OVERRIDE_SUCCESS_RATE
        ]
        if override_candidates:
            override_candidates.sort(
                key=lambda x: (-x[1], _LEVEL_ORDER.index(x[0])))
            best_level = override_candidates[0][0]
            if best_level != static_level:
                return (best_level, max_retries, True)
            return (static_level, max_retries, False)

        # 规则3: 静态级别表现差 → 升一级
        static_stat = level_stats.get(static_level)
        if (static_stat and static_stat[0] >= MIN_SAMPLES
                and static_stat[2] < POOR_SUCCESS_RATE):
            idx = _LEVEL_ORDER.index(static_level)
            if idx < len(_LEVEL_ORDER) - 1:
                return (_LEVEL_ORDER[idx + 1], max_retries, True)

        return (static_level, max_retries, False)


# ─── CLI 独立测试入口 ───
if __name__ == "__main__":
    """
    独立测试 ExperienceMemory 基本功能（使用临时数据库）
    用法: python experience_memory.py
    """
    import os
    import tempfile

    # 用临时库替换单例，避免污染 data.db
    tmp_path = os.path.join(tempfile.gettempdir(), "exp_memory_test.db")
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    DatabaseManager._instance = None
    DatabaseManager._connection = None
    db = DatabaseManager(tmp_path)
    mem = ExperienceMemory(db)

    print("=" * 60)
    print("1. 指纹构建")
    fp = mem.build_fingerprint("upload_submit_timeout", "submit_upload", "home")
    assert fp == "upload_submit_timeout|submit_upload|home", fp
    assert mem.build_fingerprint(None, "", "home") == "unknown|unknown|home"
    print(f"   OK: {fp}")

    print("2. 记录处置 + 回填结果")
    ids = []
    for i in range(4):
        exp_id = mem.record_disposal(
            "upload_submit_timeout", "submit_upload", "home",
            record_id=100 + i, file_name=f"test_{i}.docx",
            action_sequence=["close_dialog", "verify_recovery", "enqueue_retry(L2)"],
            decision_action="enqueue", retry_level="L2", source="react")
        ids.append(exp_id)
    # 3成功1失败
    for exp_id in ids[:3]:
        mem.mark_outcome(exp_id, True)
    mem.mark_outcome(ids[3], False)
    print(f"   OK: 写入{len(ids)}条, 回填3成功1失败")

    print("3. 历史经验注入文本")
    hint = mem.build_history_hint("upload_submit_timeout", "submit_upload", "home")
    assert "历史处置经验" in hint and "成功3/尝试4" in hint, hint
    print("   " + hint.replace("\n", "\n   "))
    assert mem.build_history_hint("unknown", "unknown", "unknown") == ""
    print("   OK: 无经验指纹返回空字符串")

    print("4. 动态策略修正 - 高成功率覆盖")
    # 静态 (submit_upload, upload_submit_timeout) → L2；写入 L3 高成功率样本
    for i in range(6):
        exp_id = mem.record_disposal(
            "upload_submit_timeout", "submit_upload", "no_browser",
            record_id=200 + i, file_name=f"t{i}.docx",
            action_sequence=["full_recovery", "enqueue_retry(L3)"],
            decision_action="enqueue", retry_level="L3", source="react")
        mem.mark_outcome(exp_id, True)
    level, max_r, adjusted = mem.get_adjusted_strategy(
        "submit_upload", "upload_submit_timeout")
    assert adjusted and level == RetryLevel.L3_ENV_RESET, (level, adjusted)
    print(f"   OK: L2 → {level.value} (adjusted={adjusted}, max_retries={max_r})")

    print("5. 动态策略修正 - 低成功率升级")
    # 静态 (ai_classify, api_timeout) → L1；写入 L1 低成功率样本
    for i in range(5):
        exp_id = mem.record_disposal(
            "api_timeout", "ai_classify", "unknown",
            record_id=300 + i, file_name=f"a{i}.docx",
            action_sequence=["enqueue_retry(L1)"],
            decision_action="enqueue", retry_level="L1", source="react")
        mem.mark_outcome(exp_id, False)
    level, _, adjusted = mem.get_adjusted_strategy("ai_classify", "api_timeout")
    assert adjusted and level == RetryLevel.L2_PAGE_RESET, (level, adjusted)
    print(f"   OK: L1 → {level.value} (低成功率升级)")

    print("6. L5 永不修正")
    for i in range(6):
        exp_id = mem.record_disposal(
            "permission_denied", "submit_upload", "home",
            record_id=400 + i, file_name=f"p{i}.docx",
            action_sequence=["enqueue_retry(L1)"],
            decision_action="enqueue", retry_level="L1", source="react")
        mem.mark_outcome(exp_id, True)
    level, _, adjusted = mem.get_adjusted_strategy("submit_upload", "permission_denied")
    assert not adjusted and level == RetryLevel.L5_MANUAL, (level, adjusted)
    print(f"   OK: 保持 {level.value} (adjusted={adjusted})")

    print("7. 样本不足不修正")
    level, _, adjusted = mem.get_adjusted_strategy("form_fill", "element_timeout")
    assert not adjusted and level == RetryLevel.L2_PAGE_RESET, (level, adjusted)
    print(f"   OK: 保持 {level.value} (无样本)")

    db.close()
    os.remove(tmp_path)
    print("=" * 60)
    print("全部测试通过 ✓")
