"""Verify Node：SWE-agent 極簡確定性驗證＋Decision-1 失敗歸因與防死鎖合併包（1 次 8 題）。"""
from __future__ import annotations

from typing import Any, Dict

from engine.nodes import NodeRuntime
from engine.schemas import UnitSpec, VerifyReport
from engine.state import RoomGraphState


def make_verify_node(rt: NodeRuntime):
    def verify_node(state: RoomGraphState) -> Dict[str, Any]:
        rt.check_pause()
        units = [UnitSpec.model_validate(u) for u in (state.get("units") or [])]
        idx = int(state.get("current_idx") or 0)
        unit = units[idx]
        global_test = str(state.get("global_test_cmd") or "")
        max_attempts = max(1, int(state.get("max_unit_attempts") or 3))
        head_before = str(state.get("head_before_impl") or "")

        rt.set_node("verify", f"確定性驗證 [{unit.id}]", unit_id=unit.id)
        replan_count = int(state.get("replan_count") or 0)
        max_replans = int(state.get("max_replans") if state.get("max_replans") is not None else 2)
        if not rt.has_unit_changes(head_before):
            attempts = int(state.get("unit_attempts") or 0) + 1
            unit.attempts = attempts
            units[idx] = unit
            rt.save_units(units, global_test_cmd=global_test)
            scope_hint = f"（目標檔案：{', '.join(unit.scope_files)}）" if unit.scope_files else ""
            tail = (
                f"單元 [{unit.id}] 結束實作回合，但工作區沒有任何檔案修改、新增或新 commit{scope_hint}。\n"
                "請務必實際修改程式碼或產出對應文件後，再回覆 DONE。"
            )
            report = VerifyReport(
                unit_id=unit.id,
                cmd="git status / diff check",
                exit_code=1,
                seconds=0.0,
                tail_output=tail,
                passed=False,
            )
            if attempts >= 2 and replan_count < max_replans:
                rt.emit("notice", f"[{unit.id}] 連續 {attempts} 次無檔案變動，觸發動態 Replan 拆細或明確指定產出")
                return {
                    "units": [u.model_dump() for u in units],
                    "unit_attempts": attempts,
                    "last_error_tail": tail,
                    "last_verify": report.model_dump(),
                    "replan_reason": f"單元 [{unit.id}] 連續 {attempts} 次實作未產出任何檔案修改，請拆小任務並明確指定目標檔案與 check_cmd。",
                    "next_action": "plan",
                }
            if attempts >= max_attempts:
                return {
                    "units": [u.model_dump() for u in units],
                    "unit_attempts": attempts,
                    "last_error_tail": tail,
                    "last_verify": report.model_dump(),
                    "human_kind": "question",
                    "human_reason": f"單元 [{unit.id}] 連續 {attempts} 次實作皆未修改任何檔案",
                    "human_detail": tail,
                    "next_action": "human_gate",
                }
            rt.emit("say", f"【驗證失敗】[{unit.id}] 第 {attempts}/{max_attempts} 次（工作區無檔案變動，改採強制寫檔策略重試）", role="qa")
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": attempts,
                "last_error_tail": tail,
                "change_strategy_hint": True,
                "last_verify": report.model_dump(),
                "next_action": "implement",
            }
        else:
            report = rt.run_unit_verify(unit, global_test)

        if report.passed:
            rt.emit("say", f"【驗證通過】[{unit.id}] {unit.title}（{report.seconds:.1f}s）", role="qa")
            return {
                "last_verify": report.model_dump(),
                "last_error_tail": "",
                "next_action": "review",
            }

        # 失敗：不做低配 LLM 摘要，直接用 Decision-1 單次 8 題完成失敗歸因＋防死鎖仲裁
        attempts = int(state.get("unit_attempts") or 0) + 1
        unit.attempts = attempts
        units[idx] = unit
        prev_tail = str(state.get("last_error_tail") or "")

        rt.set_node("d1_gate", f"Decision-1 失敗歸因與防死鎖診斷 [{unit.id}]（第 {attempts}/{max_attempts} 次）", unit_id=unit.id)
        diag = rt.d1.classify_failure_and_circuit(
            unit,
            report,
            prev_failure_tail=prev_tail,
            attempts=attempts,
            max_attempts=max_attempts,
        )
        report.failure_class = diag.failure_class
        rt.save_units(units, global_test_cmd=global_test)

        rt.emit(
            "say",
            f"【驗證失敗】[{unit.id}] 第 {attempts}/{max_attempts} 次（結束碼 {report.exit_code}，"
            f"D1 分類={diag.failure_class}，建議={diag.suggested_action}）",
            role="qa",
        )

        if diag.suggested_action == "waive_env":
            reason = f"Decision-1 判定外部環境限制（信心 {diag.is_external_env:.0%}）：{report.tail_output[:240]}"
            rt.emit("notice", f"[{unit.id}] {reason}")
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": attempts,
                "last_verify": report.model_dump(),
                "last_diagnosis": diag.model_dump(),
                "waive_current": True,
                "waive_reason": reason,
                "next_action": "commit",
            }

        if diag.failure_class == "flaky_test" and attempts < max_attempts:
            rt.emit("notice", f"[{unit.id}] Decision-1 判定為偶發抖動（flaky），立即重跑驗證一次")
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": attempts,
                "prev_error_tail": prev_tail,
                "last_error_tail": report.tail_output,
                "last_verify": report.model_dump(),
                "last_diagnosis": diag.model_dump(),
                "next_action": "verify",
            }

        replan_count = int(state.get("replan_count") or 0)
        max_replans = int(state.get("max_replans") if state.get("max_replans") is not None else 2)
        if (diag.failure_class == "spec_mismatch" or (diag.same_root_cause >= 0.75 and attempts >= 2)) and replan_count < max_replans:
            reason = (
                f"單元 [{unit.id}] 驗證失敗（{diag.failure_class}，重複根因={diag.same_root_cause:.0%}），"
                f"指令=`{report.cmd}`，結束碼={report.exit_code}，錯誤尾段：{report.tail_output[-500:]}"
            )
            rt.emit("notice", f"[{unit.id}] 觸發動態重規劃（{diag.failure_class}），回到 Plan 調整剩餘單元")
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": attempts,
                "prev_error_tail": prev_tail,
                "last_error_tail": report.tail_output,
                "last_verify": report.model_dump(),
                "last_diagnosis": diag.model_dump(),
                "replan_reason": reason,
                "next_action": "plan",
            }

        if diag.suggested_action == "ask_human" or attempts >= max_attempts:
            return {
                "units": [u.model_dump() for u in units],
                "unit_attempts": attempts,
                "prev_error_tail": prev_tail,
                "last_error_tail": report.tail_output,
                "last_verify": report.model_dump(),
                "last_diagnosis": diag.model_dump(),
                "human_kind": "question",
                "human_reason": f"單元 [{unit.id}] 驗證連續失敗 {attempts} 次（{diag.failure_class}）",
                "human_detail": (
                    f"單元：[{unit.id}] {unit.title}\n"
                    f"驗證指令：{report.cmd}\n"
                    f"結束碼：{report.exit_code}\n"
                    f"Decision-1 診斷：分類={diag.failure_class}，重複根因={diag.same_root_cause:.0%}\n\n"
                    f"錯誤尾段：\n{report.tail_output[-2500:]}"
                ),
                "next_action": "human_gate",
            }

        return {
            "units": [u.model_dump() for u in units],
            "unit_attempts": attempts,
            "prev_error_tail": prev_tail,
            "last_error_tail": report.tail_output,
            "change_strategy_hint": diag.suggested_action == "change_strategy" or diag.same_root_cause >= 0.70,
            "last_verify": report.model_dump(),
            "last_diagnosis": diag.model_dump(),
            "next_action": "implement",
        }

    return verify_node
