"""Review Node：Decision-1 15 題審查分流閘門 → 低風險快過，中高風險小 diff Micro-Review。"""
from __future__ import annotations

from typing import Any, Dict

from engine.nodes import NodeRuntime
from engine.schemas import ReviewVerdict, UnitSpec, VerifyReport
from engine.state import RoomGraphState


def make_review_node(rt: NodeRuntime):
    def review_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]
        idx = int(state.get("current_idx") or 0)
        unit = units[idx]
        goal = str(state.get("goal") or "")
        fast_enabled = bool(state.get("d1_fast_review", True))
        max_attempts = max(1, int(state.get("max_unit_attempts") or 3))

        diff_stat, diff_text, _ = rt.git_diff_info()
        verify_raw = state.get("last_verify") or {}
        test_tail = str(verify_raw.get("tail_output") or "")

        # [D1 #3] 1 次呼叫打包 15 題全方位體檢
        rt.set_node("d1_gate", f"Decision-1 15 題審查分流體檢 [{unit.id}]", unit_id=unit.id)
        gate = rt.d1.evaluate_unit_review_gate(
            unit,
            goal=goal,
            diff_stat=diff_stat,
            diff_text=diff_text,
            test_tail=test_tail,
            fast_review_enabled=fast_enabled,
        )

        if gate.fast_pass:
            verdict = ReviewVerdict(
                d1_scores=gate.scores,
                d1_risk=gate.risk_level,
                d1_focus_areas=[],
                deep_reviewed=False,
                blocking=[],
                suggestions=[],
            )
            rt.emit(
                "notice",
                f"[{unit.id}] Decision-1 快速審查通過（risk={gate.risk_level}，"
                f"aligned={gate.scores.get('aligned_with_unit', 0):.0%}，"
                f"real_test={gate.scores.get('real_assertions', 0):.0%}），免叫大模型深度審查",
            )
            return {
                "last_review": verdict.model_dump(),
                "next_action": "commit",
            }

        # 需要大模型 Micro-Review（只審本單元小 diff，並帶入 D1 標出的低分焦點）
        rt.set_node("review", f"Micro-Review 深度審查 [{unit.id}]", unit_id=unit.id)
        res = rt.run_micro_review(
            unit,
            goal=goal,
            diff_stat=diff_stat,
            diff_text=diff_text,
            test_tail=test_tail,
            focus_areas=gate.focus_areas,
        )
        if res is None:
            return {
                "human_kind": "question",
                "human_reason": f"單元 [{unit.id}] 深度審查沒有得到可判讀結果",
                "human_detail": (
                    f"單元：[{unit.id}] {unit.title}\n"
                    f"D1 風險等級：{gate.risk_level}\n"
                    f"D1 焦點：{'; '.join(gate.focus_areas) or '（無）'}"
                ),
                "next_action": "human_gate",
            }

        blocking, suggestions = res
        verdict = ReviewVerdict(
            d1_scores=gate.scores,
            d1_risk=gate.risk_level,
            d1_focus_areas=gate.focus_areas,
            deep_reviewed=True,
            blocking=list(blocking),
            suggestions=list(suggestions),
        )

        if blocking:
            attempts = int(state.get("unit_attempts") or 0) + 1
            unit.attempts = attempts
            units[idx] = unit
            rt.save_units(units, global_test_cmd=str(state.get("global_test_cmd") or ""))
            rt.emit(
                "say",
                f"【Micro-Review 退回】[{unit.id}]（第 {attempts}/{max_attempts} 次）：\n"
                + "\n".join(f"- {b}" for b in blocking[:6]),
                role="reviewer",
            )
            replan_count = int(state.get("replan_count") or 0)
            max_replans = int(state.get("max_replans") if state.get("max_replans") is not None else 2)
            if attempts >= 2 and replan_count < max_replans:
                reason = f"單元 [{unit.id}] 審查退回 {attempts} 次，阻斷項：\n" + "\n".join(f"- {b}" for b in blocking[:5])
                rt.emit("notice", f"[{unit.id}] 審查多次退回，觸發動態 Replan 調整剩餘單元")
                return {
                    "units": [u.model_dump() for u in units],
                    "unit_attempts": attempts,
                    "last_review": verdict.model_dump(),
                    "replan_reason": reason,
                    "next_action": "plan",
                }
            if attempts >= max_attempts:
                return {
                    "units": [u.model_dump() for u in units],
                    "unit_attempts": attempts,
                    "last_review": verdict.model_dump(),
                    "human_kind": "question",
                    "human_reason": f"單元 [{unit.id}] 審查退回達上限（{attempts} 次）",
                    "human_detail": "\n".join(f"- {b}" for b in blocking),
                    "next_action": "human_gate",
                }
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": attempts,
                "last_review": verdict.model_dump(),
                "next_action": "implement",
            }

        rt.emit("say", f"【Micro-Review 通過】[{unit.id}] {unit.title}", role="reviewer")
        return {
            "last_review": verdict.model_dump(),
            "next_action": "commit",
        }

    return review_node
