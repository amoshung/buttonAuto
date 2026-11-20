"""MetaGPT 風格的結構化交接契約（Pydantic v2）。

所有節點之間只交換這些嚴格 Schema 的 Artifact，不傳遞長篇自由對話歷史。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class UnitSpec(BaseModel):
    """單一原子實作單元（對應 C1..Cn）。"""

    id: str
    title: str
    description: str = ""
    scope_files: List[str] = Field(default_factory=list)
    check_cmd: str = ""
    status: Literal["pending", "running", "done", "waived", "failed"] = "pending"
    attempts: int = 0
    d1_triage_score: Optional[float] = None
    waive_reason: str = ""

    def to_criterion_dict(self) -> Dict[str, Any]:
        """轉為既有 Room.criteria 相容格式（供前端與 progress.md 同步）。"""
        return {
            "id": self.id,
            "text": self.title,
            "description": self.description,
            "check": self.check_cmd,
            "scope_files": list(self.scope_files),
            "done": self.status in ("done", "waived"),
            "status": "verified" if self.status == "done" else self.status,
            "attempts": self.attempts,
            "d1_triage_score": self.d1_triage_score,
            "waive_reason": self.waive_reason,
        }

    @classmethod
    def from_criterion_dict(cls, raw: Dict[str, Any], idx: int = 1) -> "UnitSpec":
        cid = str(raw.get("id") or f"C{idx}").strip()
        title = str(raw.get("text") or raw.get("title") or "").strip() or f"單元 {cid}"
        done = bool(raw.get("done"))
        raw_status = str(raw.get("status") or "").strip().lower()
        if raw_status in ("pending", "running", "done", "waived", "failed"):
            status: Any = raw_status
        elif raw_status == "verified" or done:
            status = "done"
        else:
            status = "pending"
        return cls(
            id=cid,
            title=title,
            description=str(raw.get("description") or "").strip(),
            scope_files=[str(x).strip() for x in (raw.get("scope_files") or []) if str(x).strip()],
            check_cmd=str(raw.get("check") or raw.get("check_cmd") or "").strip(),
            status=status,
            attempts=int(raw.get("attempts") or 0),
            d1_triage_score=raw.get("d1_triage_score"),
            waive_reason=str(raw.get("waive_reason") or "").strip(),
        )


class PlanSpec(BaseModel):
    """房間總體計畫規格。"""

    objective: str
    task_mode: Literal["standard", "triage"] = "standard"
    global_test_cmd: str = ""
    units: List[UnitSpec] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now_iso)


class VerifyReport(BaseModel):
    """SWE-agent 風格的確定性驗證報告（直接保存精確指令輸出尾段，不經低配 LLM 轉述）。"""

    unit_id: str
    cmd: str
    exit_code: int
    seconds: float = 0.0
    tail_output: str = ""
    passed: bool = False
    timed_out: bool = False
    failure_class: Literal["", "code_bug", "env_issue", "flaky_test", "spec_mismatch"] = ""


class ReviewVerdict(BaseModel):
    """Decision-1 多維度審查體檢＋（選配）大模型 Micro-Review 結果。"""

    d1_scores: Dict[str, float] = Field(default_factory=dict)
    d1_risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    d1_focus_areas: List[str] = Field(default_factory=list)
    deep_reviewed: bool = False
    blocking: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)

    @property
    def approved(self) -> bool:
        return len(self.blocking) == 0


class UnitResult(BaseModel):
    """單一單元完成後的完整結構化 Artifact。"""

    unit_id: str
    title: str = ""
    status: Literal["done", "waived"] = "done"
    commit_sha: str = ""
    verify: Optional[VerifyReport] = None
    review: Optional[ReviewVerdict] = None
    completed_at: str = Field(default_factory=_now_iso)


class D1PlanCheckResult(BaseModel):
    """Decision-1 Plan 全量體檢結果（單次呼叫最多 128 題）。"""

    ok: bool = True
    covers_full_goal: float = 1.0
    logical_order_valid: float = 1.0
    missing_critical_step: float = 0.0
    weak_unit_ids: List[str] = Field(default_factory=list)
    unit_scores: Dict[str, Dict[str, float]] = Field(default_factory=dict)
    used_d1: bool = False


class D1FailureDiagnosis(BaseModel):
    """Decision-1 失敗歸因＋防死鎖合併診斷結果（單次 8 題）。"""

    failure_class: Literal["code_bug", "env_issue", "flaky_test", "spec_mismatch"] = "code_bug"
    same_root_cause: float = 0.0
    is_external_env: float = 0.0
    needs_human: float = 0.0
    suggested_action: Literal[
        "retry_same_strategy", "change_strategy", "waive_env", "ask_human"
    ] = "retry_same_strategy"
    used_d1: bool = False


class D1ReviewGateResult(BaseModel):
    """Decision-1 單元全方位審查分流結果（單次 15 題）。"""

    fast_pass: bool = False
    risk_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    scores: Dict[str, float] = Field(default_factory=dict)
    focus_areas: List[str] = Field(default_factory=list)
    used_d1: bool = False


class D1GoalAuditResult(BaseModel):
    """Decision-1 結案前全盤覆檢結果（確認目標是否已無 Gap，或需再開下一輪補充單元）。"""

    goal_fully_satisfied: float = 1.0
    remaining_gaps_exist: float = 0.0
    needs_followup_round: bool = False
    used_d1: bool = False


class D1TriageItem(BaseModel):
    """Decision-1 批次分診單項結果（選配，用於大規模跨模組盤點）。"""

    item_id: str
    label: str
    needs_work: bool = True
    priority_score: float = 0.5
    category: str = "unknown"
    scores: Dict[str, float] = Field(default_factory=dict)
