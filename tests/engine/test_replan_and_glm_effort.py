"""驗證 glm-5.3 reasoning_effort、Plan 細化複檢迴圈、與開發中動態 Replan 行為。"""
from __future__ import annotations

from typing import Any, Dict, List

from engine.d1_gates import D1FailureDiagnosis, D1Packer, D1PlanCheckResult, D1ReviewGateResult
from engine.nodes.plan import make_plan_node
from engine.nodes.verify import make_verify_node
from engine.schemas import PlanSpec, UnitSpec, VerifyReport
from engine.state import open_sqlite_checkpointer
from engine.workflow import build_room_workflow
from team.drivers import CliInfo, seat_launch
from team.models import ModelConfig, ModelRouter, _normalize_glm_effort
from tests.engine.test_workflow_v2 import FakeRuntime, _low_risk_d1


def test_glm_effort_normalization_and_http_payload(monkeypatch):
    assert _normalize_glm_effort("normal") == "medium"
    assert _normalize_glm_effort("medium") == "medium"
    assert _normalize_glm_effort("high") == "high"
    assert _normalize_glm_effort("low") == "low"

    captured: List[Dict[str, Any]] = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"ok": true}'}}], "model": "glm-5.3"}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.append(dict(json or {}))
        return FakeResp()

    monkeypatch.setattr("team.models.requests.post", fake_post)
    cfg = ModelConfig(base_url="http://localhost:4000", glm_key="k", decision1_key="dk")
    router = ModelRouter(cfg=cfg)
    out = router.write("hello", effort="normal")
    assert out == '{"ok": true}'
    assert captured[0]["reasoning_effort"] == "medium"


def test_seat_launch_sets_bounded_thinking_for_glm(tmp_path):
    cfg = ModelConfig(base_url="http://localhost:4000", glm_key="k", decision1_key="dk")
    clis = {"claude": CliInfo(name="claude", label="Claude", path="claude", protocol="headless")}
    _, env_glm, _ = seat_launch("glm", clis, cfg, [], str(tmp_path), str(tmp_path / "t"))
    _, env_high, _ = seat_launch("glm_high", clis, cfg, [], str(tmp_path), str(tmp_path / "t"))
    assert "MAX_THINKING_TOKENS" not in env_glm
    assert env_high["MAX_THINKING_TOKENS"] == "16000"


def test_plan_node_refines_weak_units_and_rechecks():
    initial_units = [
        UnitSpec(id="C1", title="OK unit", check_cmd="pytest a.py"),
        UnitSpec(id="C2", title="Too broad", check_cmd=""),
    ]
    # 1. 自動拆解產生的計畫（無既有同步單元）應進行細化＋複檢
    rt = FakeRuntime([], D1Packer(d1_fn=_low_risk_d1))
    calls = {"check": 0, "gen": 0}

    def fake_check(plan: PlanSpec) -> D1PlanCheckResult:
        calls["check"] += 1
        if calls["check"] == 1:
            return D1PlanCheckResult(ok=True, covers_full_goal=0.9, logical_order_valid=0.9, weak_unit_ids=["C2"], used_d1=True)
        return D1PlanCheckResult(ok=True, covers_full_goal=0.95, logical_order_valid=0.95, weak_unit_ids=[], used_d1=True)

    def fake_gen(goal: str, *, triage_hint: str = "", refine_hint: str = "") -> PlanSpec:
        calls["gen"] += 1
        if refine_hint:
            return PlanSpec(
                objective=goal,
                units=[
                    UnitSpec(id="C1", title="OK unit", check_cmd="pytest a.py"),
                    UnitSpec(id="C2a", title="Split part 1", check_cmd="pytest b1.py"),
                    UnitSpec(id="C2b", title="Split part 2", check_cmd="pytest b2.py"),
                ],
            )
        return PlanSpec(objective=goal, units=initial_units)

    rt.d1.check_plan_quality = fake_check
    rt.generate_plan = fake_gen

    node = make_plan_node(rt)
    out = node({"goal": "goal", "units": [], "plan_refinements": 0})
    assert calls["check"] == 2
    assert calls["gen"] == 2
    assert out["plan_refinements"] == 1
    assert [u["id"] for u in out["units"]] == ["C1", "C2a", "C2b"]

    # 2. 已手動跟隨 CE 計畫同步且無待細化項（weak_unit_ids=[]）時直接保留，不浪費 token
    calls2 = {"gen": 0}
    rt2 = FakeRuntime(initial_units, D1Packer(d1_fn=_low_risk_d1))
    rt2.d1.check_plan_quality = lambda p: D1PlanCheckResult(ok=True, covers_full_goal=0.9, logical_order_valid=0.9, weak_unit_ids=[], used_d1=True)
    rt2.generate_plan = lambda g, **kw: (calls2.__setitem__("gen", calls2["gen"] + 1) or PlanSpec(objective=g, units=[]))
    out2 = make_plan_node(rt2)({"goal": "goal", "units": [], "plan_refinements": 0})
    assert calls2["gen"] == 0
    assert [u["id"] for u in out2["units"]] == ["C1", "C2"]


def test_verify_spec_mismatch_triggers_mid_flight_replan(tmp_path):
    initial_units = [
        UnitSpec(id="C1", title="First", check_cmd="echo 1"),
        UnitSpec(id="C2", title="Broken spec", check_cmd="nonexistent_cmd"),
    ]
    rt = FakeRuntime(initial_units, D1Packer(d1_fn=_low_risk_d1))
    verify_calls = {"C2": 0}

    def custom_verify(unit: UnitSpec, global_test_cmd: str) -> VerifyReport:
        if unit.id == "C2":
            verify_calls["C2"] += 1
            return VerifyReport(unit_id="C2", cmd=unit.check_cmd, exit_code=127, passed=False, tail_output="command not found")
        return VerifyReport(unit_id=unit.id, cmd=unit.check_cmd or "echo ok", exit_code=0, passed=True, tail_output="ok")

    def custom_diag(unit: UnitSpec, report: VerifyReport, **kwargs):
        if unit.id == "C2":
            return D1FailureDiagnosis(failure_class="spec_mismatch", same_root_cause=0.2, suggested_action="retry_same_strategy", used_d1=True)
        return D1FailureDiagnosis(failure_class="code_bug", used_d1=True)

    def custom_gen(goal: str, *, triage_hint: str = "", refine_hint: str = "") -> PlanSpec:
        if "動態重規劃" in refine_hint:
            return PlanSpec(objective=goal, units=[UnitSpec(id="C2_fixed", title="Fixed check", check_cmd="echo ok")])
        return PlanSpec(objective=goal, units=initial_units)

    rt.run_unit_verify = custom_verify
    rt.d1.classify_failure_and_circuit = custom_diag
    rt.generate_plan = custom_gen

    saver, conn = open_sqlite_checkpointer(str(tmp_path / "g.sqlite"))
    try:
        graph = build_room_workflow(rt, checkpointer=saver)
        res = graph.invoke(
            {
                "room_id": "r1",
                "goal": "goal",
                "task_mode": "standard",
                "d1_fast_review": True,
                "max_unit_attempts": 3,
                "max_audit_rounds": 1,
                "max_replans": 2,
                "units": [],
                "completed_ledger": [],
            },
            config={"configurable": {"thread_id": "r1"}},
        )
        assert res["completed"] is True
        assert verify_calls["C2"] == 1
        assert [u["id"] for u in res["units"]] == ["C1", "C2_fixed"]
        assert len(res["completed_ledger"]) == 2
    finally:
        conn.close()


def test_noop_verify_triggers_replan_on_second_empty_turn():
    units = [UnitSpec(id="C1", title="Write doc", scope_files=["docs/out.md"], check_cmd="test -s docs/out.md")]
    rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    rt.has_unit_changes = lambda _head: False
    vnode = make_verify_node(rt)

    out1 = vnode({"units": [u.model_dump() for u in units], "current_idx": 0, "unit_attempts": 0, "max_unit_attempts": 3, "replan_count": 0, "max_replans": 2})
    assert out1["next_action"] == "implement"
    assert out1["change_strategy_hint"] is True

    out2 = vnode({"units": out1["units"], "current_idx": 0, "unit_attempts": 1, "max_unit_attempts": 3, "replan_count": 0, "max_replans": 2})
    assert out2["next_action"] == "plan"
    assert "replan_reason" in out2


def test_review_blocking_twice_triggers_replan():
    from engine.nodes.review import make_review_node

    units = [UnitSpec(id="C1", title="Big task", check_cmd="pytest")]
    rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    rt.d1.evaluate_unit_review_gate = lambda *a, **kw: D1ReviewGateResult(fast_pass=False, risk_level="high", focus_areas=["scope"])
    rt.run_micro_review = lambda *a, **kw: (["[Major] 任務過大需拆細"], [])
    rnode = make_review_node(rt)

    out = rnode({"units": [u.model_dump() for u in units], "current_idx": 0, "unit_attempts": 1, "max_unit_attempts": 3, "replan_count": 0, "max_replans": 2, "d1_fast_review": True})
    assert out["next_action"] == "plan"
    assert "審查退回" in out["replan_reason"]


def test_select_strongest_planner_excludes_paid_litellm(tmp_path):
    import json
    from team.model_catalog import select_strongest_planner

    (tmp_path / "litellm_catalog.json").write_text(
        json.dumps(
            {
                "models": [
                    {"id": "bedrock-gpt-6-astra", "input_usd_per_1m": 10.0, "output_usd_per_1m": 50.0},
                    {"id": "glm-5.3", "input_usd_per_1m": 0.0, "output_usd_per_1m": 0.0},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "claude": {"models": [{"id": "bedrock-gpt-6-astra"}, {"id": "opus"}]},
                "kiro": {"models": [{"id": "claude-opus-5.5"}, {"id": "gpt-5.6-sol"}]},
            }
        ),
        encoding="utf-8",
    )

    seen_prompts = []

    def fake_d1(state, questions):
        seen_prompts.append(state)
        assert "bedrock-gpt-6-astra" not in state
        out = {}
        for k, q in questions.items():
            out[k] = {"noul": 0.95 if "gpt-5.6-sol" in q["instructions"] else 0.70}
        return out

    cfg = ModelConfig(base_url="http://localhost:4000", glm_key="k", decision1_key="dk")
    seat, model, reason = select_strongest_planner(
        {"claude": True, "kiro": True},
        store_root=str(tmp_path),
        model_cfg=cfg,
        d1_fn=fake_d1,
    )
    assert seat == "kiro"
    assert model == "gpt-5.6-sol"
    assert "d1_selected" in reason


def test_read_only_safe_cmd_allowlist():
    from engine.runner_v2 import EngineV2Runner

    fn = EngineV2Runner._is_safe_read_only_cmd
    assert fn("Load skill: ce-doc-review") is True
    assert fn("Get-ChildItem -Path backend -Recurse -File | ForEach-Object { $_.FullName }") is True
    assert fn("git status; git diff --stat") is True
    assert fn("rg -n foo src/") is True

    assert fn("Get-Content a.txt > b.txt") is False
    assert fn("Set-Content a.txt 'hi'") is False
    assert fn("rm -rf /tmp/x") is False
    assert fn("git commit -m test") is False
    assert fn("python -c 'print(1)'") is False


def test_acp_activity_extends_timeout_until_idle():
    import threading
    import time
    import pytest
    from team.acp import AcpClient, AcpError

    c = AcpClient(["true"], ".", lambda _ev: None, lambda _req: "deny")
    c._proc = object()  # fake running proc
    c._write = lambda _obj: None

    # 1. 有持續 session/update 活動時，即使總時間超過 timeout (0.25s) 也不應超時
    def _emit_activity_then_done():
        for _ in range(4):
            time.sleep(0.12)
            c._handle({"jsonrpc": "2.0", "method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x"}}}})
        time.sleep(0.05)
        with c._lock:
            rid = max(c._pending)
        c._handle({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})

    threading.Thread(target=_emit_activity_then_done, daemon=True).start()
    res = c._request("session/prompt", {"sessionId": "s1", "prompt": []}, timeout=0.25)
    assert res["stopReason"] == "end_turn"

    # 2. 完全無活動超過 timeout 時仍會正常逾時
    with pytest.raises(AcpError):
        c._request("session/prompt", {"sessionId": "s1", "prompt": []}, timeout=0.15)


def test_impl_fallback_chain_agy_to_kiro_to_glm(tmp_path, monkeypatch):
    from engine.runner_v2 import EngineV2Runner
    from team.models import ModelConfig
    from team.rooms import RoomStore

    store = RoomStore(tmp_path)
    room = store.create(name="t", slug="t", source="goal", repo=str(tmp_path), goal="test", impl_cli="auto", review_cli="auto")
    clis = {
        "agy": CliInfo(name="agy", label="Agy", path="agy", protocol="acp"),
        "kiro": CliInfo(name="kiro", label="Kiro", path="kiro-cli", protocol="acp"),
        "claude": CliInfo(name="claude", label="Claude", path="claude", protocol="headless"),
    }
    cfg = ModelConfig(base_url="http://localhost:4000", glm_key="k", decision1_key="dk")
    runner = EngineV2Runner(
        room.id,
        store,
        None,
        clis,
        [],
        lambda: None,
        model_cfg=cfg,
    )
    assert runner._pick_seat("impl") == ("agy", "gemini-4-hydrogen")
    assert runner._pick_seat("review") == ("kiro", "claude-sonnet-5.5")

    tried = []

    class FakeDrv:
        def __init__(self, seat, model):
            self.seat = seat
            self.model = model

        def start(self):
            tried.append((self.seat, self.model))
            if self.seat == "agy" or (self.seat == "kiro" and self.model == "claude-sonnet-5.5"):
                raise RuntimeError("quota exceeded")

        def prompt(self, text, timeout=10):
            return "ok"

        def close(self):
            pass

    monkeypatch.setattr("engine.runner_v2.seat_launch", lambda s, *a, **kw: (clis.get(s) or clis["claude"], {}, []))
    monkeypatch.setattr("engine.runner_v2.make_driver", lambda cli, cwd, on_ev, on_perm, model="", **kw: FakeDrv(cli.name, model))

    out = runner._run_fresh_cli_turn("agy", "", "hello", read_only=False, role="developer")
    assert out == "ok"
    assert tried == [
        ("agy", "gemini-4-hydrogen"),
        ("kiro", "claude-sonnet-5.5"),
        ("kiro", "claude-opus-5.5"),
    ]


def test_synced_plan_with_weak_units_auto_surgically_refines(tmp_path):
    from engine.workflow import build_room_workflow

    initial_units = [
        UnitSpec(id="C1", title="OK unit", check_cmd="echo 1"),
        UnitSpec(id="C2", title="Too broad", check_cmd=""),
    ]
    rt = FakeRuntime(initial_units, D1Packer(d1_fn=_low_risk_d1))
    checks = {"n": 0}

    def fake_check(plan):
        checks["n"] += 1
        if any(u.id == "C2" for u in plan.units):
            return D1PlanCheckResult(ok=True, covers_full_goal=0.9, logical_order_valid=0.9, weak_unit_ids=["C2"], used_d1=True)
        return D1PlanCheckResult(ok=True, covers_full_goal=0.95, logical_order_valid=0.95, weak_unit_ids=[], used_d1=True)

    rt.d1.check_plan_quality = fake_check
    rt.generate_plan = lambda g, **kw: PlanSpec(
        objective=g,
        units=[
            UnitSpec(id="C2a", title="Split 1", check_cmd="echo a"),
            UnitSpec(id="C2b", title="Split 2", check_cmd="echo b"),
        ],
    )

    saver, conn = open_sqlite_checkpointer(str(tmp_path / "g.sqlite"))
    try:
        graph = build_room_workflow(rt, checkpointer=saver)
        cfg = {"configurable": {"thread_id": "r_refine"}}
        res = graph.invoke(
            {"room_id": "r_refine", "goal": "goal", "task_mode": "standard", "d1_fast_review": True, "max_audit_rounds": 1, "units": []},
            config=cfg,
        )
        assert res["completed"] is True
        assert [u["id"] for u in res["units"]] == ["C1", "C2a", "C2b"]
    finally:
        conn.close()


def test_parse_json_block_handles_large_plan_with_many_braces():
    import json
    from team.personas import parse_json_block

    big = {
        "global_test_cmd": "pytest",
        "units": [
            {"id": f"U{i}", "title": f"Unit {i} with {{brace}} {i}", "description": "a {b} {c} {d}", "check_cmd": "echo ok"}
            for i in range(1, 80)
        ],
    }
    raw = json.dumps(big, ensure_ascii=False)
    parsed = parse_json_block(raw)
    assert parsed is not None
    assert len(parsed["units"]) == 79


def test_commit_node_auto_expands_subplan_units():
    from engine.nodes.commit import make_commit_node

    units = [
        UnitSpec(id="U1", title="Generate subplans", status="running"),
        UnitSpec(id="U2", title="Final verification", status="pending"),
    ]
    rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    rt.discover_subplan_units = lambda ids, titles: (
        [
            UnitSpec(id="SP1_1", title="Subtask A", check_cmd="cargo test a"),
            UnitSpec(id="SP1_2", title="Subtask B", check_cmd="cargo test b"),
        ],
        ["d1r2-01.md"],
    )
    cnode = make_commit_node(rt)
    out = cnode(
        {
            "units": [u.model_dump() for u in units],
            "current_idx": 0,
            "artifacts": {},
            "completed_ledger": [],
        }
    )
    assert [u["id"] for u in out["units"]] == ["U1", "SP1_1", "SP1_2", "U2"]
    assert out["units"][0]["status"] == "done"
    assert out["units"][1]["status"] == "pending"


def test_recursive_subplan_expansion_a_to_b_to_c(tmp_path):
    from types import SimpleNamespace
    from engine.runner_v2 import _V2Runtime

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-11-20-plan-a.md").write_text("# Plan A\n- [x] U1\n- [ ] U2\n", encoding="utf-8")
    (plans_dir / "2026-11-20-d1r2-01.md").write_text(
        "# Plan B1\n- [ ] Fix B1 referencing docs/plans/2026-11-20-plan-c.md\n",
        encoding="utf-8",
    )
    (plans_dir / "2026-11-20-d1r2-02.md").write_text(
        "# Plan B2\n- [ ] Fix B2 item\n",
        encoding="utf-8",
    )
    (plans_dir / "2026-11-20-plan-c.md").write_text(
        "# Plan C\n- [x] Already done C0\n- [ ] Implement C1\n",
        encoding="utf-8",
    )

    fake_room = SimpleNamespace(
        repo=str(tmp_path),
        reference_root="",
        target_subdir="",
        target_subdirs=[],
        ssh_host="",
        selected_plan_rel="docs/plans/2026-11-20-plan-a.md",
    )
    fake_runner = SimpleNamespace(
        room=fake_room,
        _wt=None,
        router=None,
        tracer=SimpleNamespace(record=lambda *a, **kw: None),
    )
    rt = _V2Runtime(fake_runner)

    units = [
        UnitSpec(id="U1", title="Triage produced docs/plans/2026-11-20-d1r2-", status="done"),
        UnitSpec(id="U2", title="Final closeout", status="pending"),
    ]
    expanded = rt._expand_referenced_subplans(units)
    titles = [u.title for u in expanded]

    assert titles[0] == "Triage produced docs/plans/2026-11-20-d1r2-"
    assert titles[-1] == "Final closeout"
    assert any("Implement C1" in t for t in titles)
    assert not any("Already done C0" in t for t in titles)
    assert any("Fix B1" in t for t in titles)
    assert any("Fix B2 item" in t for t in titles)
    idx_c1 = next(i for i, t in enumerate(titles) if "Implement C1" in t)
    idx_b1 = next(i for i, t in enumerate(titles) if "Fix B1" in t)
    idx_b2 = next(i for i, t in enumerate(titles) if "Fix B2 item" in t)
    assert idx_c1 < idx_b1 < idx_b2


def test_plan_lineage_context_and_commit_ledger_subplan_notice(tmp_path):
    from types import SimpleNamespace
    from engine.runner_v2 import _V2Runtime
    from engine.nodes.commit import make_commit_node

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2026-11-20-main.md").write_text("# Main\n", encoding="utf-8")
    (plans_dir / "2026-11-20-d1r2-01.md").write_text("# Sub1\n", encoding="utf-8")

    fake_room = SimpleNamespace(
        repo=str(tmp_path),
        ssh_host="",
        selected_plan_rel="docs/plans/2026-11-20-main.md",
    )
    fake_runner = SimpleNamespace(
        room=fake_room,
        _wt=None,
        router=None,
        tracer=SimpleNamespace(record=lambda *a, **kw: None),
    )
    rt_v2 = _V2Runtime(fake_runner)
    lineage = rt_v2._plan_lineage_context()
    assert "docs/plans/2026-11-20-main.md" in lineage
    assert "docs/plans/2026-11-20-d1r2-01.md" in lineage
    assert "衍生自（Parent Plan）" in lineage

    units = [UnitSpec(id="U1", title="Generate subplan", status="running")]
    rt_fake = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    rt_fake.discover_subplan_units = lambda ids, titles: (
        [UnitSpec(id="SP1_1", title="Subtask 1", check_cmd="echo ok")],
        ["2026-11-20-d1r2-01.md"],
    )
    out = make_commit_node(rt_fake)(
        {"units": [u.model_dump() for u in units], "current_idx": 0, "artifacts": {}, "completed_ledger": []}
    )
    assert any("衍伸子計畫展開" in x and "2026-11-20-d1r2-01.md" in x for x in out["completed_ledger"])


def test_mid_flight_replan_preserves_subsequent_pending_units():
    from engine.nodes.plan import make_plan_node

    units = [
        UnitSpec(id="U0", title="Already done", status="done"),
        UnitSpec(id="U1A", title="Stuck unit", status="running"),
        UnitSpec(id="U2", title="Keep unit 2", status="pending"),
        UnitSpec(id="U3", title="Keep unit 3", status="pending"),
    ]
    rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    rt.generate_plan = lambda goal, triage_hint="", refine_hint="": PlanSpec(
        objective=goal,
        units=[
            UnitSpec(id="U1A1", title="Substep 1", check_cmd="echo 1"),
            UnitSpec(id="U1A2", title="Substep 2", check_cmd="echo 2"),
        ],
    )
    pnode = make_plan_node(rt)
    out = pnode(
        {
            "room_id": "r1",
            "goal": "goal",
            "task_mode": "standard",
            "units": [u.model_dump() for u in units],
            "current_idx": 1,
            "replan_reason": "U1A 無檔案變動",
            "replan_count": 0,
            "plan_refinements": 0,
        }
    )
    ids = [u["id"] for u in out["units"]]
    assert ids == ["U0", "U1A1", "U1A2", "U2", "U3"]


def test_surgical_refine_preserves_multi_weak_positions_and_blocks_existing_shrink():
    from engine.nodes.plan import make_plan_node
    from engine.schemas import D1PlanCheckResult

    units = [
        UnitSpec(id="U1", title="Weak 1", status="pending"),
        UnitSpec(id="U2", title="Strong 2", status="pending"),
        UnitSpec(id="U3", title="Weak 3", status="pending"),
    ]
    rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    calls = {"n": 0}

    def check_fn(ps):
        calls["n"] += 1
        if calls["n"] == 1:
            return D1PlanCheckResult(
                ok=False,
                covers_full_goal=0.85,
                logical_order_valid=0.85,
                weak_unit_ids=["U1", "U3"],
                used_d1=True,
            )
        return D1PlanCheckResult(ok=True, covers_full_goal=0.95, logical_order_valid=0.95, used_d1=True)

    rt.d1.check_plan_quality = check_fn
    rt.generate_plan = lambda goal, triage_hint="", refine_hint="": PlanSpec(
        objective=goal,
        units=[
            UnitSpec(id="U1a", title="U1 part a", check_cmd="echo 1a"),
            UnitSpec(id="U3a", title="U3 part a", check_cmd="echo 3a"),
        ],
    )
    out = make_plan_node(rt)(
        {
            "room_id": "r1",
            "goal": "goal",
            "task_mode": "standard",
            "units": [u.model_dump() for u in units],
            "plan_refinements": 0,
        }
    )
    assert [u["id"] for u in out["units"]] == ["U1a", "U2", "U3a"]


def test_sync_expand_subplans_forward_and_backlink(tmp_path):
    from team.ce_sync import expand_referenced_subplan_criteria
    from engine.runner_v2 import _is_quota_error

    assert _is_quota_error("⚠ Individual quota reached. Please upgrade your subscription") is True

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-11-20-main.md").write_text("# Main\n", encoding="utf-8")
    (plans / "2026-11-20-d1r2-01.md").write_text("# R2-01\n- [ ] Fix A\n", encoding="utf-8")
    (plans / "2026-11-20-extra-02.md").write_text(
        "# Extra\n> 衍生自（Parent Plan）：docs/plans/2026-11-20-main.md\n\n- [ ] Fix Extra\n",
        encoding="utf-8",
    )

    base_crit = [
        {"id": "U1", "text": "Done step", "check": "echo 1", "done": True},
        {"id": "U6", "text": "Triage produced docs/plans/2026-11-20-d1r2-", "check": "echo 6", "done": True},
        {"id": "U7", "text": "Continue after U6", "check": "echo 7", "done": False},
        {"id": "C13", "text": "終止判定：結案", "check": "echo 13", "done": False},
    ]
    expanded, sources = expand_referenced_subplan_criteria(
        base_crit,
        str(tmp_path),
        selected_plan_rel="docs/plans/2026-11-20-main.md",
    )
    texts = [c["text"] for c in expanded]
    assert "Fix A" in texts
    assert "Fix Extra" in texts
    assert texts.index("Triage produced docs/plans/2026-11-20-d1r2-") < texts.index("Fix A") < texts.index("Continue after U6")
    assert texts.index("Continue after U6") < texts.index("Fix Extra") < texts.index("終止判定：結案")
    assert "2026-11-20-d1r2-01.md" in sources and "2026-11-20-extra-02.md" in sources


def test_persist_refined_units_to_plan_roundtrip_and_explosion_cap(tmp_path):
    from team.ce_sync import persist_refined_units_to_plan, sync_room_ce_plan
    from engine.nodes.plan import make_plan_node
    from engine.schemas import D1PlanCheckResult

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    plan_file = plans / "2026-11-20-test-plan.md"
    plan_file.write_text("# Test Plan\n\n## Implementation Units\n- [ ] U1 Old coarse\n", encoding="utf-8")

    refined_units = [
        {"id": "U1a", "title": "Refined A", "check_cmd": "pytest -k a", "status": "pending"},
        {"id": "U1b", "title": "Refined B", "check_cmd": "pytest -k b", "status": "done"},
    ]
    assert persist_refined_units_to_plan(str(tmp_path), "docs/plans/2026-11-20-test-plan.md", refined_units) is True
    synced = sync_room_ce_plan(str(tmp_path), plan_rel="docs/plans/2026-11-20-test-plan.md")
    assert [(c["id"], c["done"]) for c in synced["criteria"]] == [("U1a", False), ("U1b", True)]

    # Verify plan_node explosion cap blocks >60 bloated units
    units = [UnitSpec(id="C1", title="Coarse", status="pending")]
    rt = FakeRuntime(units, D1Packer(d1_fn=_low_risk_d1))
    rt.d1.check_plan_quality = lambda ps: D1PlanCheckResult(
        ok=False, covers_full_goal=0.8, logical_order_valid=0.8, weak_unit_ids=["C1"], used_d1=True
    )
    rt.generate_plan = lambda goal, triage_hint="", refine_hint="": PlanSpec(
        objective=goal,
        units=[UnitSpec(id=f"X{i}", title=f"Spam {i}", check_cmd="echo 1") for i in range(90)],
    )
    out = make_plan_node(rt)(
        {"room_id": "r1", "goal": "goal", "task_mode": "standard", "units": [u.model_dump() for u in units], "plan_refinements": 0}
    )
    assert [u["id"] for u in out["units"]] == ["C1"]
