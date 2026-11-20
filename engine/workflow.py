"""LangGraph v2 StateGraph 工作流組裝（含 SQLite Checkpointer 與 interrupt() 人工閘門）。"""
from __future__ import annotations

from typing import Any, Dict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from engine.nodes import NodeRuntime
from engine.nodes.commit import make_commit_node
from engine.nodes.implement import make_implement_node
from engine.nodes.plan import make_goal_audit_node, make_plan_node, make_select_unit_node
from engine.nodes.review import make_review_node
from engine.nodes.verify import make_verify_node
from engine.schemas import UnitSpec
from engine.state import RoomGraphState


def _make_human_gate_node(rt: NodeRuntime):
    def human_gate_node(state: RoomGraphState) -> Dict[str, Any]:
        unit_id = str(state.get("current_unit_id") or "")
        reason = str(state.get("human_reason") or "需要人工裁決")
        detail = str(state.get("human_detail") or "")
        kind = str(state.get("human_kind") or "question")
        rt.set_node("human_gate", f"等待收件匣回覆：{reason}", unit_id=unit_id)

        # LangGraph 原生 interrupt()：狀態立即落盤至 SQLite，等待使用者回覆後由 Command(resume=...) 續跑
        decision = interrupt(
            {
                "kind": kind,
                "unit_id": unit_id,
                "title": reason,
                "detail": detail,
                "options": [
                    {"id": "retry", "label": "重試此單元"},
                    {"id": "replan", "label": "回到計畫重新拆解剩餘單元"},
                    {"id": "waive", "label": "豁免並跳至下一單元"},
                    {"id": "pause", "label": "先暫停，我來處理"},
                ],
            }
        )
        ans = str((decision or {}).get("answer") or "pause")
        extra_text = str((decision or {}).get("text") or "").strip()

        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]
        idx = int(state.get("current_idx") or 0)

        if ans == "waive" and 0 <= idx < len(units):
            return {
                "waive_current": True,
                "waive_reason": extra_text or "使用者於收件匣裁決豁免此單元",
                "next_action": "commit",
            }
        if ans == "retry" and 0 <= idx < len(units):
            units[idx].attempts = 0
            err = str(state.get("last_error_tail") or "")
            if extra_text:
                err = f"【使用者補充指示】{extra_text}\n\n{err}".strip()
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": 0,
                "last_error_tail": err,
                "change_strategy_hint": True,
                "next_action": "implement",
            }
        if ans == "replan":
            return {
                "replan_reason": extra_text or f"使用者裁決針對單元 [{unit_id}] 重新調整計畫：{reason}",
                "unit_attempts": 0,
                "next_action": "plan",
            }
        return {"paused": True, "next_action": "end"}

    return human_gate_node


def _make_final_merge_node(rt: NodeRuntime):
    def final_merge_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.set_node("done", "全部單元完成，已送出合併確認")
        rt.mark_finished()
        return {"completed": True, "next_action": "end"}

    return final_merge_node


def build_room_workflow(rt: NodeRuntime, *, checkpointer: Any = None):
    """建立並編譯房間專用的 LangGraph StateGraph。"""
    g = StateGraph(RoomGraphState)

    g.add_node("plan", make_plan_node(rt))
    g.add_node("select_unit", make_select_unit_node(rt))
    g.add_node("implement", make_implement_node(rt))
    g.add_node("verify", make_verify_node(rt))
    g.add_node("review", make_review_node(rt))
    g.add_node("commit", make_commit_node(rt))
    g.add_node("goal_audit", make_goal_audit_node(rt))
    g.add_node("human_gate", _make_human_gate_node(rt))
    g.add_node("final_merge", _make_final_merge_node(rt))

    g.set_entry_point("plan")
    g.add_edge("plan", "select_unit")

    g.add_conditional_edges(
        "select_unit",
        lambda s: s.get("next_action", "goal_audit"),
        {"implement": "implement", "goal_audit": "goal_audit", "final_merge": "final_merge"},
    )
    g.add_conditional_edges(
        "goal_audit",
        lambda s: s.get("next_action", "final_merge"),
        {"select_unit": "select_unit", "final_merge": "final_merge"},
    )
    g.add_edge("implement", "verify")

    g.add_conditional_edges(
        "verify",
        lambda s: s.get("next_action", "human_gate"),
        {
            "review": "review",
            "verify": "verify",
            "implement": "implement",
            "commit": "commit",
            "plan": "plan",
            "human_gate": "human_gate",
        },
    )
    g.add_conditional_edges(
        "review",
        lambda s: s.get("next_action", "human_gate"),
        {
            "commit": "commit",
            "implement": "implement",
            "plan": "plan",
            "human_gate": "human_gate",
        },
    )
    g.add_edge("commit", "select_unit")

    g.add_conditional_edges(
        "human_gate",
        lambda s: s.get("next_action", "end"),
        {
            "implement": "implement",
            "commit": "commit",
            "plan": "plan",
            "end": END,
        },
    )
    g.add_edge("final_merge", END)

    return g.compile(checkpointer=checkpointer)
