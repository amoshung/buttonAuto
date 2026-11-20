"""Plan Node：載入或依目標生成嚴格 Schema 的 PlanSpec，並以 Decision-1 單次全量體檢。"""
from __future__ import annotations

from typing import Any, Dict, List

from langgraph.types import interrupt

from engine.nodes import NodeRuntime
from engine.schemas import PlanSpec, UnitSpec
from engine.state import RoomGraphState


def make_plan_node(rt: NodeRuntime):
    def _build_refine_hint(units: List[UnitSpec], check: Any) -> str:
        weak_set = set(getattr(check, "weak_unit_ids", ()) or ())
        if weak_set:
            weak_lines = [
                f"- [{u.id}] {u.title}（目前 check_cmd={u.check_cmd or '無'}）"
                for u in units
                if u.id in weak_set
            ]
            keep_ids = [u.id for u in units if u.id not in weak_set]
            return (
                "以下單元粒度過粗、依賴不清或缺少客觀驗證指令，請只針對這些單元拆小並補齊具體 check_cmd"
                "（純文件/盤點單元用檔案存在與非空檢查；程式單元用對應模組測試，避免每個單元都跑全倉測試）：\n"
                + "\n".join(weak_lines)
                + (f"\n\n其餘已合格單元請盡量保留原樣：{', '.join(keep_ids)}" if keep_ids else "")
            )
        return "計畫未完整覆蓋目標或單元依賴順序不佳，請重新整理為原子步驟，並為每項補齊具體 check_cmd。"

    def plan_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        rt.set_node("plan", "規劃與校驗執行單元 (Plan + D1 Gate)")

        goal = str(state.get("goal") or "").strip()
        task_mode = str(state.get("task_mode") or "standard")
        existing_raw = list(state.get("units") or [])
        global_test = str(state.get("global_test_cmd") or "")
        replan_reason = str(state.get("replan_reason") or "").strip()
        replan_count = int(state.get("replan_count") or 0)

        units: List[UnitSpec] = []
        loaded_from_existing = False
        if existing_raw:
            units = [UnitSpec.model_validate(u) for u in existing_raw]
            loaded_from_existing = True
        else:
            loaded_units, loaded_test = rt.load_existing_units()
            if loaded_units:
                units = loaded_units
                loaded_from_existing = True
                if loaded_test and not global_test:
                    global_test = loaded_test

        # 開發中動態 Replan：僅針對當前卡住的單元重拆或修正，完整保留已完成與後續尚未執行的其他單元
        if replan_reason and units:
            cur_idx = int(state.get("current_idx") or 0)
            has_stuck = 0 <= cur_idx < len(units) and units[cur_idx].status not in ("done", "waived")
            stuck_unit = units[cur_idx] if has_stuck else None
            done_units = [u for u in units if u.status in ("done", "waived")]
            other_pending = [
                u for i, u in enumerate(units)
                if i != cur_idx and u.status not in ("done", "waived")
            ]
            done_summary = "\n".join(f"- [{u.id}] {u.title}（{u.status}）" for u in done_units[-10:]) or "（無）"
            rt.emit("notice", f"依執行回饋動態重規劃卡住單元（第 {replan_count + 1} 次）：{replan_reason[:120]}")
            if stuck_unit is not None:
                later_preview = "\n".join(f"- [{u.id}] {u.title}" for u in other_pending[:8])
                if len(other_pending) > 8:
                    later_preview += f"\n- ...（其餘 {len(other_pending) - 8} 個後續單元將由系統原封不動保留）"
                refine_msg = (
                    f"【開發中動態重規劃原因】\n{replan_reason}\n\n"
                    f"【唯一需重拆／修正的卡住單元】\n- [{stuck_unit.id}] {stuck_unit.title}（check={stuck_unit.check_cmd or '無'}）\n"
                    + (f"  說明：{stuck_unit.description}\n" if stuck_unit.description else "")
                    + f"\n【已完成單元（系統已保留，勿重複輸出）】\n{done_summary}\n"
                    + (f"\n【後續尚未執行單元（系統將原封不動保留，切勿重複或刪減）】\n{later_preview}\n" if later_preview else "")
                    + "\n請「只」輸出用來取代上述卡住單元的新原子單元清單（例如拆小成 2～5 個具體步驟或修正 check_cmd），系統會自動插回原位並保留所有後續單元。"
                )
            else:
                pending_summary = "\n".join(f"- [{u.id}] {u.title}（check={u.check_cmd or '無'}）" for u in other_pending) or "（無）"
                refine_msg = (
                    f"【開發中動態重規劃原因】\n{replan_reason}\n\n"
                    f"【已完成單元（請勿重複規劃）】\n{done_summary}\n\n"
                    f"【原剩餘單元】\n{pending_summary}\n\n"
                    "請只輸出「接下來尚未完成」的新原子單元清單，不要包含已完成單元。"
                )
            replanned = rt.generate_plan(goal, refine_hint=refine_msg)
            if replanned.units:
                keep_others = (units[:cur_idx] + units[cur_idx + 1 :]) if stuck_unit is not None else done_units
                used_ids = {u.id for u in keep_others}
                new_sub: List[UnitSpec] = []
                for idx, nu in enumerate(replanned.units, start=1):
                    cand_id = nu.id or f"P{replan_count + 1}_{idx}"
                    while cand_id in used_ids:
                        cand_id = f"{cand_id}_r{replan_count + 1}_{idx}"
                    used_ids.add(cand_id)
                    nu.id = cand_id
                    nu.status = "pending"
                    new_sub.append(nu)
                if stuck_unit is not None:
                    units = units[:cur_idx] + new_sub + units[cur_idx + 1 :]
                else:
                    units = done_units + new_sub
                if replanned.global_test_cmd and not global_test:
                    global_test = replanned.global_test_cmd
            replan_count += 1
        elif not units:
            triage_hint = ""
            if task_mode == "triage":
                rt.set_node("plan", "Decision-1 批次分診掃描候選項目")
                candidates = rt.scan_triage_candidates()
                if candidates:
                    triaged = rt.d1.batch_triage_items(candidates, goal)
                    needed = [t for t in triaged if t.needs_work]
                    rt.emit(
                        "notice",
                        f"Decision-1 批次分診完成：掃描 {len(triaged)} 項，篩出 {len(needed)} 項需處理",
                    )
                    if needed:
                        triage_hint = "\n".join(
                            f"- [{t.item_id}] {t.label}（分類={t.category}，優先度={t.priority_score:.2f}）"
                            for t in needed[:40]
                        )

            plan = rt.generate_plan(goal, triage_hint=triage_hint)
            units = list(plan.units)
            if plan.global_test_cmd and not global_test:
                global_test = plan.global_test_cmd

        if not units:
            units = [
                UnitSpec(
                    id="C1",
                    title=goal[:160] or "完成房間指定任務",
                    check_cmd=global_test,
                )
            ]

        # [D1 #1] Plan 全量體檢 + 外科細化複檢迴圈（盤中 Replan 已精準替換卡住單元，不落入全量覆蓋迴圈）
        refinements = int(state.get("plan_refinements") or 0)
        max_refinements = 2
        was_existing_plan = loaded_from_existing
        while not replan_reason:
            plan_spec = PlanSpec(
                objective=goal,
                task_mode="triage" if task_mode == "triage" else "standard",
                global_test_cmd=global_test,
                units=units,
            )
            check = rt.d1.check_plan_quality(plan_spec)
            if not check.used_d1:
                break

            rt.emit(
                "notice",
                f"Decision-1 計畫體檢（{len(units)} 單元）：覆蓋={check.covers_full_goal:.0%}、"
                f"順序={check.logical_order_valid:.0%}、待細化={len(check.weak_unit_ids)} 項",
            )
            # 若是使用者已手動對齊／同步好的 CE 計畫：
            # - 沒有待細化項就直接開工
            # - 有待細化項時，直接交由 AI 高層主管指揮外科細化弱單元（保留其餘合格單元），不打斷使用者
            if loaded_from_existing:
                if not check.weak_unit_ids or state.get("plan_ready") or refinements >= max_refinements:
                    break
                rt.emit("notice", f"AI 高層主管依 Decision-1 體檢自動指揮外科細化 {len(check.weak_unit_ids)} 項弱單元（其餘保留原樣）")
                loaded_from_existing = False
            needs_refine = (not check.ok) or bool(check.weak_unit_ids)
            if not needs_refine or refinements >= max_refinements:
                break

            done_prefix = [u for u in units if u.status in ("done", "waived")]
            pending_before = [u for u in units if u.status not in ("done", "waived")]
            weak_set = set(getattr(check, "weak_unit_ids", ()) or ())
            refine_hint = _build_refine_hint(units, check)
            refined = rt.generate_plan(goal, refine_hint=refine_hint)
            refinements += 1
            if not refined.units:
                break
            max_allowed = max(len(units) * 3, 60)
            if len(refined.units) > max_allowed:
                rt.emit(
                    "notice",
                    f"防暴衝保護：細化輸出過大（{len(refined.units)} > {max_allowed} 單元），保留原清單避免單元風暴",
                )
                break

            if weak_set and check.covers_full_goal >= 0.55 and check.logical_order_valid >= 0.55:
                # 真外科式合併：保留所有已合格單元原樣，依原位置替換對應的 weak_set 項目
                strong_ids = {u.id for u in units if u.id not in weak_set}
                replacements = [u for u in refined.units if u.id not in strong_ids]
                if replacements:
                    # 依 ID 前綴把新單元配對回原本的弱單元位置，避免多處弱單元被擠到同一處
                    grouped: Dict[str, List[UnitSpec]] = {wid: [] for wid in weak_set}
                    unmatched: List[UnitSpec] = []
                    weak_sorted = sorted(weak_set, key=len, reverse=True)
                    for ru in replacements:
                        matched_wid = ""
                        for wid in weak_sorted:
                            if ru.id == wid or ru.id.startswith(wid):
                                matched_wid = wid
                                break
                        if matched_wid:
                            grouped[matched_wid].append(ru)
                        else:
                            unmatched.append(ru)
                    merged: List[UnitSpec] = []
                    inserted_unmatched = False
                    for u in units:
                        if u.id not in weak_set:
                            merged.append(u)
                        else:
                            bucket = grouped.get(u.id) or []
                            if bucket:
                                merged.extend(bucket)
                            if not inserted_unmatched and unmatched:
                                merged.extend(unmatched)
                                inserted_unmatched = True
                            elif not bucket and not (not inserted_unmatched and unmatched):
                                # 若模型未產出該弱單元的替代項，保留原單元避免無聲遺失
                                if len(weak_set) > 1:
                                    merged.append(u)
                    if merged:
                        units = merged
            elif not was_existing_plan:
                # 僅限全新生成（非既有 CE/已載入計畫），才允許整份重整，且避免新清單大幅縮水
                if done_prefix:
                    done_ids = {u.id for u in done_prefix}
                    tail = [u for u in refined.units if u.id not in done_ids]
                    if tail and len(tail) >= len(pending_before):
                        units = done_prefix + tail
                elif len(refined.units) >= len(units):
                    units = list(refined.units)
            if refined.global_test_cmd and not global_test:
                global_test = refined.global_test_cmd

        rt.save_units(units, global_test_cmd=global_test)
        if refinements > 0 or replan_reason:
            persist_fn = getattr(rt, "persist_units_to_plan", None)
            if callable(persist_fn):
                try:
                    persist_fn(units)
                except Exception:
                    pass
        return {
            "units": [u.model_dump() for u in units],
            "global_test_cmd": global_test,
            "plan_ready": True,
            "plan_refinements": refinements,
            "replan_reason": "",
            "replan_count": replan_count,
            "next_action": "select_unit",
        }

    return plan_node


def make_select_unit_node(rt: NodeRuntime):
    def select_unit_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]

        # 嚴格依序挑出第一個尚未 done / waived 的單元（絕不跳著做）
        target_idx = -1
        for i, u in enumerate(units):
            if u.status not in ("done", "waived"):
                target_idx = i
                break

        if target_idx < 0:
            rt.set_node("d1_gate", "Decision-1 結案前全盤覆檢（確認目標是否已無 Gap）")
            return {
                "current_idx": len(units),
                "current_unit_id": "",
                "next_action": "goal_audit",
            }

        target = units[target_idx]
        target.status = "running"
        units[target_idx] = target
        rt.save_units(units, global_test_cmd=str(state.get("global_test_cmd") or ""))
        rt.set_node("implement", f"鎖定執行 [{target.id}] {target.title}", unit_id=target.id)
        return {
            "units": [u.model_dump() for u in units],
            "current_idx": target_idx,
            "current_unit_id": target.id,
            "unit_attempts": int(target.attempts),
            "last_error_tail": "",
            "prev_error_tail": "",
            "change_strategy_hint": False,
            "last_review": {},
            "waive_current": False,
            "waive_reason": "",
            "next_action": "implement",
        }

    return select_unit_node


def make_goal_audit_node(rt: NodeRuntime):
    """結案前全盤覆檢節點：由 Decision-1 檢核是否仍有剩餘 Gap，若有且未達輪次上限則自動規劃下一輪。"""

    def goal_audit_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        goal = str(state.get("goal") or "").strip()
        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]
        artifacts = dict(state.get("artifacts") or {})
        audit_round = int(state.get("audit_round") or 0) + 1
        max_rounds = max(1, int(state.get("max_audit_rounds") or 2))

        rt.set_node("d1_gate", f"Decision-1 結案前全盤覆檢（第 {audit_round}/{max_rounds} 輪）")
        audit = rt.d1.evaluate_goal_completion(goal, units, artifacts, round_num=audit_round)

        if audit.used_d1:
            rt.emit(
                "notice",
                f"Decision-1 第 {audit_round} 輪目標覆檢：滿足度={audit.goal_fully_satisfied:.0%}、"
                f"剩餘 Gap={audit.remaining_gaps_exist:.0%}、"
                f"決策={'開下一輪補充單元' if audit.needs_followup_round else '可結案合併'}",
            )

        if audit.needs_followup_round and audit_round < max_rounds:
            done_summary = "\n".join(f"- [{u.id}] {u.title}" for u in units)
            followup = rt.generate_plan(
                goal,
                refine_hint=(
                    f"第 {audit_round} 輪以下單元已完成：\n{done_summary}\n\n"
                    "Decision-1 覆檢判定仍存在未解決的 Gap 或目標要求的後續檢查／修復尚未執行，"
                    "請只針對「尚未完成的剩餘 Gap」拆出下一輪補充實作單元（不要重複已完成項目）。"
                ),
            )
            if followup.units:
                base_idx = len(units)
                new_units: List[UnitSpec] = []
                for offset, fu in enumerate(followup.units, start=1):
                    fu.id = f"R{audit_round + 1}_{offset}"
                    fu.status = "pending"
                    new_units.append(fu)
                all_units = units + new_units
                global_test = str(state.get("global_test_cmd") or followup.global_test_cmd or "")
                rt.save_units(all_units, global_test_cmd=global_test)
                rt.emit("notice", f"已依 Decision-1 覆檢結果自動追加第 {audit_round + 1} 輪 {len(new_units)} 個補充單元")
                return {
                    "units": [u.model_dump() for u in all_units],
                    "current_idx": base_idx,
                    "audit_round": audit_round,
                    "next_action": "select_unit",
                }

        rt.set_node("final_merge", "所有單元與目標覆檢已完成，準備最終確認合併")
        return {
            "audit_round": audit_round,
            "next_action": "final_merge",
        }

    return goal_audit_node
