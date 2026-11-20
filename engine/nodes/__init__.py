"""v2 LangGraph 節點群組與執行期介面（NodeRuntime）。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from engine.d1_gates import D1Packer
from engine.schemas import PlanSpec, UnitResult, UnitSpec, VerifyReport


class NodeRuntime(Protocol):
    """注入給每個 LangGraph 節點的執行環境介面（支援真實 CLI/Git 或測試 Mock）。"""

    d1: D1Packer

    def set_node(self, node_name: str, activity: str, *, unit_id: str = "") -> None: ...
    def emit(self, kind: str, text: str, **extra: Any) -> None: ...
    def check_pause(self) -> None: ...

    # Plan
    def load_existing_units(self) -> Tuple[List[UnitSpec], str]: ...
    def scan_triage_candidates(self) -> List[Dict[str, str]]: ...
    def generate_plan(self, goal: str, *, triage_hint: str = "", refine_hint: str = "") -> PlanSpec: ...
    def save_units(self, units: List[UnitSpec], global_test_cmd: str = "") -> None: ...

    # Implement (一單元一 Fresh Session)
    def run_unit_implement(
        self,
        unit: UnitSpec,
        *,
        goal: str,
        recent_commits: str,
        error_tail: str = "",
        review_blocking: Sequence[str] = (),
        change_strategy: bool = False,
    ) -> str: ...

    # Verify (SWE-agent 確定性驗證)
    def git_head_sha(self) -> str: ...
    def has_unit_changes(self, head_before: str) -> bool: ...
    def run_unit_verify(self, unit: UnitSpec, global_test_cmd: str) -> VerifyReport: ...

    # Review
    def git_diff_info(self) -> Tuple[str, str, str]:
        """回傳 (diff_stat, diff_text, recent_commits)。"""
        ...

    def run_micro_review(
        self,
        unit: UnitSpec,
        *,
        goal: str,
        diff_stat: str,
        diff_text: str,
        test_tail: str,
        focus_areas: Sequence[str],
    ) -> Optional[Tuple[List[str], List[str]]]:
        """回傳 (blocking, suggestions) 或 None（無可判讀結果）。"""
        ...

    # Commit & Artifact
    def commit_unit(self, unit: UnitSpec, result: UnitResult) -> str: ...
    def save_unit_artifact(self, result: UnitResult) -> None: ...
    def mark_finished(self) -> None: ...
