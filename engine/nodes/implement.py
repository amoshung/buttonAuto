"""Implement Node：一單元一 Fresh Session，只派單一鎖定單元，絕不混雜其他未解鎖條件。"""
from __future__ import annotations

from typing import Any, Dict

from engine.nodes import NodeRuntime
from engine.schemas import UnitSpec
from engine.state import RoomGraphState


def make_implement_node(rt: NodeRuntime):
    def implement_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]
        idx = int(state.get("current_idx") or 0)
        unit = units[idx]

        rt.set_node("implement", f"實作中（Fresh Session）：[{unit.id}] {unit.title}", unit_id=unit.id)
        head_before = rt.git_head_sha()
        _, _, recent_commits = rt.git_diff_info()
        ledger = list(state.get("completed_ledger") or [])
        if ledger:
            ledger_block = "已完成單元摘要：\n" + "\n".join(f"- {x}" for x in ledger[-8:])
            recent_commits = (ledger_block + ("\n\n最近 Git 提交：\n" + recent_commits if recent_commits else "")).strip()
        last_review = state.get("last_review") or {}
        review_blocking = list(last_review.get("blocking") or [])
        error_tail = str(state.get("last_error_tail") or "")
        change_strategy = bool(state.get("change_strategy_hint"))

        rt.run_unit_implement(
            unit,
            goal=str(state.get("goal") or ""),
            recent_commits=recent_commits,
            error_tail=error_tail,
            review_blocking=review_blocking,
            change_strategy=change_strategy,
        )
        return {
            "head_before_impl": head_before,
            "last_review": {},
            "change_strategy_hint": False,
            "next_action": "verify",
        }

    return implement_node
