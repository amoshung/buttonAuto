"""測試 v2 追蹤器（trace.jsonl）、權限把關、額度備援偵測、即時插話注入與 SSH 遠端 verify。"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from engine.d1_gates import D1Packer
from engine.runner_v2 import EngineV2Runner, _V2Runtime, _is_quota_error
from engine.schemas import PlanSpec, UnitSpec
from engine.trace import RoomTracer
from team.events import PermissionRequest
from team.rooms import RoomStore


def test_room_tracer_and_d1_packer_logging(tmp_path):
    tracer = RoomTracer(str(tmp_path), "r_trace")
    packer = D1Packer(
        d1_fn=lambda _s, qs: {k: {"noul": 0.9} for k in qs},
        on_trace=tracer.record,
    )
    plan = PlanSpec(objective="測試目標", units=[UnitSpec(id="C1", title="單元一", check_cmd="pytest")])
    res = packer.check_plan_quality(plan)
    assert res.used_d1 is True

    entries = tracer.read(limit=20)
    assert len(entries) == 1
    assert entries[0]["kind"] == "d1_gate"
    assert entries[0]["gate"] == "plan_quality"
    assert entries[0]["questions"] == 6
    assert entries[0]["ok"] is True


def test_ssh_remote_verify_and_trace(tmp_path):
    store = RoomStore(str(tmp_path / "sessions"))
    room = store.create(
        name="ssh-demo",
        repo="/home/dev/proj",
        slug="ssh-demo",
        goal="SSH 遠端測試",
        source="goal",
        impl_cli="auto",
        review_cli="auto",
        ssh_host="dev@10.0.0.1",
    )
    runner = EngineV2Runner(room.id, store, MagicMock(), {}, [], lambda: None)
    rt = _V2Runtime(runner)

    with patch("engine.runner_v2.remote_exec", return_value={"exit_code": 0, "stdout": "2 passed\n", "stderr": ""}) as m_exec:
        rep = rt.run_unit_verify(UnitSpec(id="C1", title="遠端檢查", check_cmd="pytest -q"), "")
        m_exec.assert_called_once()
        assert rep.passed is True
        assert "2 passed" in rep.tail_output

    trace_entries = runner.tracer.read()
    assert any(e["kind"] == "verify" and e.get("remote") is True and e.get("passed") is True for e in trace_entries)


def test_permission_guard_and_live_user_inject(tmp_path):
    store = RoomStore(str(tmp_path / "sessions"))
    room = store.create(
        name="perm-demo",
        repo=str(tmp_path),
        slug="perm-demo",
        goal="測試權限與插話",
        source="goal",
        impl_cli="auto",
        review_cli="auto",
    )
    fake_router = MagicMock()
    # 灰色地帶需要問人
    fake_router.allow.return_value = SimpleNamespace(allow=False, ask=True, reason="需要確認")
    runner = EngineV2Runner(room.id, store, fake_router, {}, [r"rm\s+-rf"], lambda: None)

    # 1. 危險指令直接拒絕
    danger_req = PermissionRequest("t1", "Bash", "execute", {"command": "rm -rf /"}, [])
    assert runner._evaluate_permission(danger_req, read_only=False) == "deny"

    # 2. 唯讀座位拒絕寫入、放行讀取
    write_req = PermissionRequest("t2", "Write", "write", {"path": "a.py"}, [])
    read_req = PermissionRequest("t3", "Read", "read", {"path": "a.py"}, [])
    assert runner._evaluate_permission(write_req, read_only=True) == "deny"
    assert runner._evaluate_permission(read_req, read_only=True) == "allow"

    # 3. 灰色地帶送收件匣，使用者回覆 allow 後放行
    result_holder = []

    def ask_thread():
        result_holder.append(runner._evaluate_permission(write_req, read_only=False))

    th = threading.Thread(target=ask_thread)
    th.start()
    # 等待收件匣出現項目並回覆
    for _ in range(50):
        items = [i for i in store.inbox() if i.room_id == room.id and not i.resolved]
        if items:
            runner.answer(items[0].id, "allow")
            break
        threading.Event().wait(0.05)
    th.join(timeout=5)
    assert result_holder == ["allow"]

    # 4. 即時插話注入與額度錯誤偵測
    runner.send("請順便補上型別註記")
    captured_prompt = []
    runner._run_fresh_cli_turn = lambda seat, model, prompt, **kw: (captured_prompt.append(prompt) or "DONE")  # type: ignore[method-assign]
    rt = _V2Runtime(runner)
    rt.run_unit_implement(UnitSpec(id="C1", title="改寫函式"), goal="目標", recent_commits="")
    assert captured_prompt and "請順便補上型別註記" in captured_prompt[0]
    assert "Compound-Engineering" in captured_prompt[0]
    assert _is_quota_error("HTTP 429 Too Many Requests: rate limit exceeded") is True
    assert _is_quota_error("SyntaxError: invalid syntax") is False
