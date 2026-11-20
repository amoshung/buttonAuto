"""Decision-1 128 題打包器（D1Packer）單元測試。"""
from __future__ import annotations

from typing import Any, Dict, List

from engine.d1_gates import D1Packer, MAX_QUESTIONS_PER_CALL
from engine.schemas import PlanSpec, UnitSpec, VerifyReport


def test_plan_quality_packs_and_chunks_within_128_limit():
    calls: List[int] = []

    def fake_d1(state: str, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        calls.append(len(questions))
        assert len(questions) <= MAX_QUESTIONS_PER_CALL
        return {k: {"noul": 0.90 if k != "missing_critical_step" else 0.10} for k in questions}

    # 45 個單元 × 3 題 + 全局 3 題 = 138 題 → 應自動切成 2 批（128 + 10）
    units = [UnitSpec(id=f"C{i}", title=f"Task {i}", check_cmd=f"pytest -k t{i}") for i in range(1, 46)]
    plan = PlanSpec(objective="大任務", units=units)

    packer = D1Packer(d1_fn=fake_d1)
    res = packer.check_plan_quality(plan)
    assert res.used_d1 is True
    assert res.ok is True
    assert calls == [128, 10]
    assert len(res.unit_scores) == 45


def test_review_gate_fast_pass_vs_deep_review_focus():
    unit = UnitSpec(id="C1", title="Add helper", check_cmd="pytest")

    def fake_low_risk(_state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        assert len(questions) == 15
        out = {k: {"noul": 0.92} for k in questions}
        out["security_risk"] = {"noul": 0.05}
        out["concurrency_risk"] = {"noul": 0.05}
        out["breaking_change"] = {"noul": 0.05}
        out["large_or_complex_diff"] = {"noul": 0.10}
        out["needs_deep_review"] = {"noul": 0.15}
        out["risk_level"] = {"choice": "low"}
        return out

    res_low = D1Packer(d1_fn=fake_low_risk).evaluate_unit_review_gate(
        unit, goal="G", diff_stat="1 file", diff_text="+def a(): pass", test_tail="1 passed"
    )
    assert res_low.fast_pass is True
    assert res_low.risk_level == "low"
    assert res_low.focus_areas == []

    def fake_high_risk(_state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: {"noul": 0.90} for k in questions}
        out["security_risk"] = {"noul": 0.85}
        out["real_assertions"] = {"noul": 0.40}
        out["needs_deep_review"] = {"noul": 0.90}
        out["risk_level"] = {"choice": "high"}
        return out

    res_high = D1Packer(d1_fn=fake_high_risk).evaluate_unit_review_gate(
        unit, goal="G", diff_stat="2 files", diff_text="+os.system(x)", test_tail="1 passed"
    )
    assert res_high.fast_pass is False
    assert res_high.risk_level == "high"
    assert any("安全" in f for f in res_high.focus_areas)
    assert any("測試斷言" in f for f in res_high.focus_areas)


def test_failure_and_circuit_breaker_single_8_question_call():
    calls: List[int] = []

    def fake_d1(_state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(len(questions))
        return {
            "failure_class": {"choice": "env_issue"},
            "same_root_cause": {"noul": 0.85},
            "is_external_env": {"noul": 0.91},
            "is_flaky": {"noul": 0.10},
            "is_spec_broken": {"noul": 0.05},
            "needs_human": {"noul": 0.20},
            "worth_changing_strategy": {"noul": 0.10},
            "suggested_action": {"choice": "waive_env"},
        }

    unit = UnitSpec(id="C2", title="Connect external DB", check_cmd="pytest test_db.py")
    rep = VerifyReport(unit_id="C2", cmd="pytest test_db.py", exit_code=1, tail_output="ConnectionRefusedError")
    diag = D1Packer(d1_fn=fake_d1).classify_failure_and_circuit(unit, rep, attempts=2, max_attempts=3)
    assert calls == [8]
    assert diag.failure_class == "env_issue"
    assert diag.suggested_action == "waive_env"


def test_batch_triage_packs_multiple_items_per_call():
    calls: List[int] = []

    def fake_d1(_state: str, questions: Dict[str, Any]) -> Dict[str, Any]:
        calls.append(len(questions))
        assert len(questions) <= MAX_QUESTIONS_PER_CALL
        ans: Dict[str, Any] = {}
        for k in questions:
            if k.endswith("_category"):
                ans[k] = {"choice": "done" if k.startswith("U00_") else "major"}
            elif k.endswith("_needs_work") or k.endswith("_has_stub"):
                ans[k] = {"noul": 0.05 if k.startswith("U00_") else 0.92}
            else:
                ans[k] = {"noul": 0.80}
        return ans

    items = [{"id": f"mod_{i}", "label": f"src/mod_{i}.py", "content": f"def f{i}(): pass"} for i in range(10)]
    results = D1Packer(d1_fn=fake_d1).batch_triage_items(items, goal="全面盤點")
    # 10 項 × 4 題 = 40 題，應打包在單一呼叫中完成
    assert calls == [40]
    assert len(results) == 10
    done_items = [r for r in results if not r.needs_work]
    assert len(done_items) == 1 and done_items[0].item_id == "mod_0"


def test_d1_packer_unwraps_model_reply_from_router():
    from types import SimpleNamespace

    fake_router = SimpleNamespace(
        cfg=SimpleNamespace(base_url="http://x:4000", decision1_key="k"),
        _d1=lambda _s, qs: SimpleNamespace(
            value={k: {"noul": 0.90 if k != "missing_critical_step" else 0.10} for k in qs}
        ),
    )
    plan = PlanSpec(objective="測試", units=[UnitSpec(id="C1", title="單元一", check_cmd="pytest")])
    res = D1Packer(router=fake_router).check_plan_quality(plan)
    assert res.used_d1 is True
    assert res.ok is True
