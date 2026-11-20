"""Commit Node：提交本單元變更、寫入結構化 UnitResult Artifact，解鎖下一個單元。"""
from __future__ import annotations

from typing import Any, Dict

from engine.nodes import NodeRuntime
from engine.schemas import ReviewVerdict, UnitResult, UnitSpec, VerifyReport
from engine.state import RoomGraphState


def make_commit_node(rt: NodeRuntime):
    def commit_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]
        idx = int(state.get("current_idx") or 0)
        unit = units[idx]

        waived = bool(state.get("waive_current"))
        waive_reason = str(state.get("waive_reason") or "")
        unit.status = "waived" if waived else "done"
        if waive_reason:
            unit.waive_reason = waive_reason
        units[idx] = unit

        rt.set_node("commit", f"提交與歸檔 [{unit.id}]（{unit.status}）", unit_id=unit.id)

        verify_raw = state.get("last_verify") or {}
        review_raw = state.get("last_review") or {}
        result = UnitResult(
            unit_id=unit.id,
            title=unit.title,
            status="waived" if waived else "done",
            verify=VerifyReport.model_validate(verify_raw) if verify_raw else None,
            review=ReviewVerdict.model_validate(review_raw) if review_raw else None,
        )

        sha = rt.commit_unit(unit, result)
        result.commit_sha = sha
        rt.save_unit_artifact(result)

        discover_fn = getattr(rt, "discover_subplan_units", None)
        expanded_note = ""
        if callable(discover_fn):
            try:
                new_units, src_files = discover_fn(
                    [u.id for u in units],
                    [u.title for u in units],
                )
                if new_units:
                    units = units[: idx + 1] + list(new_units) + units[idx + 1 :]
                    expanded_note = f"[衍伸子計畫展開] {unit.id} 產出 {', '.join(src_files)}，已依序排入 {len(new_units)} 個單元至計畫清單"
                    rt.emit(
                        "notice",
                        f"已從新子計畫（{', '.join(src_files)}）自動展開 {len(new_units)} 個單元至里程碑佇列（總計 {len(units)} 項）",
                    )
            except Exception:
                pass

        rt.save_units(units, global_test_cmd=str(state.get("global_test_cmd") or ""))

        artifacts = dict(state.get("artifacts") or {})
        artifacts[unit.id] = result.model_dump()

        ledger = list(state.get("completed_ledger") or [])
        scope_hint = ", ".join(unit.scope_files[:4]) if unit.scope_files else ""
        entry = f"[{unit.id}] {unit.title[:80]}（{result.status}" + (f", 檔案: {scope_hint}" if scope_hint else "") + "）"
        if entry not in ledger:
            ledger.append(entry)
        if expanded_note and expanded_note not in ledger:
            ledger.append(expanded_note)
        ledger = ledger[-12:]

        rt.emit(
            "notice",
            f"單元 [{unit.id}] 已完成（{result.status}，commit={sha[:7] if sha else '—'}），即將切換新對話進入下一單元",
        )
        return {
            "units": [u.model_dump() for u in units],
            "artifacts": artifacts,
            "completed_ledger": ledger,
            "waive_current": False,
            "waive_reason": "",
            "last_error_tail": "",
            "prev_error_tail": "",
            "last_review": {},
            "next_action": "select_unit",
        }

    return commit_node
