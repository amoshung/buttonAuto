"""LangGraph v2 StateGraph 端到端測試（循序不跳步、一單元一 Fresh Session、interrupt 恢復、SQLite 斷點）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from langgraph.types import Command

from engine.d1_gates import D1Packer
from engine.schemas import PlanSpec, UnitResult, UnitSpec, VerifyReport
from engine.state import RoomGraphState, open_sqlite_checkpointer
from engine.workflow import build_room_workflow


class FakeRuntime:
    def __init__(self, units: List[UnitSpec], d1: D1Packer, fail_unit_id: str = "") -> None:
        self.d1 = d1
        self._units = units
        self.fail_unit_id = fail_unit_id
        self.impl_order: List[str] = []
        self.micro_reviewed: List[str] = []
        self.committed: List[str] = []
        self.nodes_visited: List[str] = []
        self.finished = False

    def set_node(self, node_name: str, activity: str, *, unit_id: str = "") -> None:
        self.nodes_visited.append(f"{node_name}:{unit_id}")

    def emit(self, kind: str, text: str, **extra: Any) -> None:
        pass

    def check_pause(self) -> None:
        pass

    def load_existing_units(self) -> Tuple[List[UnitSpec], str]:
        return list(self._units), "pytest"

    def scan_triage_candidates(self) -> List[Dict[str, str]]:
        return []

    def generate_plan(self, goal: str, *, triage_hint: str = "", refine_hint: str = "") -> PlanSpec:
        return PlanSpec(objective=goal, units=list(self._units), global_test_cmd="pytest")

    def save_units(self, units: List[UnitSpec], global_test_cmd: str = "") -> None:
        self._units = list(units)

    def run_unit_implement(
        self,
        unit: UnitSpec,
        *,
        goal: str,
        recent_commits: str,
        error_tail: str = "",
        review_blocking: Sequence[str] = (),
        change_strategy: bool = False,
    ) -> str:
        self.impl_order.append(unit.id)
        return "DONE"

    def run_unit_verify(self, unit: UnitSpec, global_test_cmd: str) -> VerifyReport:
        if unit.id == self.fail_unit_id:
            return VerifyReport(unit_id=unit.id, cmd="pytest", exit_code=1, tail_output="AssertionError: boom", passed=False)
        return VerifyReport(unit_id=unit.id, cmd="pytest", exit_code=0, tail_output="1 passed", passed=True)

    def git_head_sha(self) -> str:
        return "abc1234"

    def has_unit_changes(self, head_before: str) -> bool:
        return True

    def git_diff_info(self) -> Tuple[str, str, str]:
        return "1 file changed", "+def ok(): return True", "abc1234 init"

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
        self.micro_reviewed.append(unit.id)
        return [], []

    def commit_unit(self, unit: UnitSpec, result: UnitResult) -> str:
        self.committed.append(f"{unit.id}:{result.status}")
        return f"sha_{unit.id}"

    def save_unit_artifact(self, result: UnitResult) -> None:
        pass

    def mark_finished(self) -> None:
        self.finished = True


def _low_risk_d1(_state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: {"noul": 0.92} for k in questions}
    for neg in ("missing_critical_step", "security_risk", "concurrency_risk", "breaking_change",
                "large_or_complex_diff", "needs_deep_review", "same_root_cause", "is_external_env",
                "is_flaky", "is_spec_broken", "needs_human"):
        if neg in out:
            out[neg] = {"noul": 0.05}
    if "risk_level" in questions:
        out["risk_level"] = {"choice": "low"}
    if "failure_class" in questions:
        out["failure_class"] = {"choice": "code_bug"}
    if "suggested_action" in questions:
        out["suggested_action"] = {"choice": "retry_same_strategy"}
    return out


def test_strict_sequential_c1_c2_c3_with_d1_fast_pass(tmp_path):
    db = str(tmp_path / "graph.sqlite")
    saver, conn = open_sqlite_checkpointer(db)
    try:
        units = [
            UnitSpec(id="C1", title="First", check_cmd="pytest -k 1"),
            UnitSpec(id="C2", title="Second", check_cmd="pytest -k 2"),
            UnitSpec(id="C3", title="Third", check_cmd="pytest -k 3"),
        ]
        rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
        graph = build_room_workflow(rt, checkpointer=saver)
        res = graph.invoke(
            RoomGraphState(
                room_id="r001",
                goal="完成 3 個循序單元",
                task_mode="standard",
                d1_fast_review=True,
                max_unit_attempts=2,
            ),
            config={"configurable": {"thread_id": "r001"}},
        )
        assert res["completed"] is True
        assert rt.finished is True
        # 嚴格依序執行，絕不跳步
        assert rt.impl_order == ["C1", "C2", "C3"]
        assert rt.committed == ["C1:done", "C2:done", "C3:done"]
        # D1 低風險快速放行，免叫大模型 Micro-Review
        assert rt.micro_reviewed == []
        assert set(res["artifacts"].keys()) == {"C1", "C2", "C3"}
    finally:
        conn.close()


def test_human_gate_interrupt_and_sqlite_resume(tmp_path):
    db = str(tmp_path / "graph.sqlite")
    saver, conn = open_sqlite_checkpointer(db)
    try:
        units = [
            UnitSpec(id="C1", title="Always Fails", check_cmd="pytest -k fail"),
            UnitSpec(id="C2", title="Succeeds", check_cmd="pytest -k ok"),
        ]
        rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1), fail_unit_id="C1")
        graph = build_room_workflow(rt, checkpointer=saver)
        cfg = {"configurable": {"thread_id": "r002"}}

        paused_res = graph.invoke(
            RoomGraphState(
                room_id="r002",
                goal="測試人工閘門",
                task_mode="standard",
                d1_fast_review=True,
                max_unit_attempts=2,
            ),
            config=cfg,
        )
        # C1 連敗 2 次後應停在 interrupt()，尚未進入 C2
        assert "__interrupt__" in paused_res
        assert rt.impl_order == ["C1", "C1"]
        assert rt.finished is False

        # 模擬重啟：重新建立 graph 並使用同一個 SQLite Checkpointer，以 waive 恢復
        graph2 = build_room_workflow(rt, checkpointer=saver)
        resumed = graph2.invoke(
            Command(resume={"answer": "waive", "text": "先跳過 C1"}),
            config=cfg,
        )
        assert resumed["completed"] is True
        assert rt.finished is True
        assert rt.committed == ["C1:waived", "C2:done"]
        assert rt.impl_order == ["C1", "C1", "C2"]
    finally:
        conn.close()


def test_goal_audit_triggers_followup_round_when_gaps_remain(tmp_path):
    db = str(tmp_path / "graph_audit.sqlite")
    saver, conn = open_sqlite_checkpointer(db)
    try:
        units = [UnitSpec(id="C1", title="第一輪：盤點缺口", check_cmd="pytest -k 1")]
        audit_calls = []

        def d1_with_one_followup(_state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
            out = _low_risk_d1(_state, questions)
            if "next_step" in questions:
                audit_calls.append(1)
                if len(audit_calls) == 1:
                    out["goal_fully_satisfied"] = {"noul": 0.30}
                    out["remaining_gaps_exist"] = {"noul": 0.90}
                    out["next_step"] = {"choice": "followup_round"}
                else:
                    out["goal_fully_satisfied"] = {"noul": 0.95}
                    out["remaining_gaps_exist"] = {"noul": 0.05}
                    out["next_step"] = {"choice": "merge_ready"}
            return out

        rt = FakeRuntime(units, D1Packer(d1_fn=d1_with_one_followup))
        rt.generate_plan = lambda goal, **kw: PlanSpec(  # type: ignore[method-assign]
            objective=goal,
            units=[UnitSpec(id="F1", title="第二輪：修補剩餘 Gap", check_cmd="pytest -k 2")],
            global_test_cmd="pytest",
        )
        graph = build_room_workflow(rt, checkpointer=saver)
        res = graph.invoke(
            RoomGraphState(
                room_id="r003",
                goal="先盤點再補齊全部 Gap",
                task_mode="standard",
                d1_fast_review=True,
                max_unit_attempts=2,
                max_audit_rounds=2,
            ),
            config={"configurable": {"thread_id": "r003"}},
        )
        assert res["completed"] is True
        assert len(audit_calls) == 2
        assert rt.impl_order == ["C1", "R2_1"]
        assert rt.committed == ["C1:done", "R2_1:done"]
    finally:
        conn.close()
