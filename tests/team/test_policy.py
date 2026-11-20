import threading
import os

import pytest

from team.events import PermissionRequest
from team.fakes.fake_models import ScriptedRouter
from team.models import AllowVerdict
from team.policy import PendingPermission, decide, pick_option

OPTS = [
    {"optionId": "a", "name": "always", "kind": "allow_always"},
    {"optionId": "o", "name": "once", "kind": "allow_once"},
    {"optionId": "r", "name": "no", "kind": "reject_once"},
]
DENY = [r"rm\s+-rf"]


def req(kind="execute", cmd="ls"):
    return PermissionRequest("t1", "run", kind, {"command": cmd}, OPTS)


class Boom(ScriptedRouter):
    def decide_allow(self, state_text):
        raise AssertionError("router must not be called")


class NoAuto(ScriptedRouter):
    def decide_allow(self, state_text):
        return AllowVerdict(False, 0.5, 0.5, "x", "gray")


def run(r, router, read_only=False):
    return decide(r, goal="g", scope="s", deny_patterns=DENY, router=router, read_only=read_only)


def test_deny_escalates_without_router():
    d = run(req("read", "rm -rf /"), Boom())
    assert d.outcome == "escalate" and DENY[0] in d.reason


def test_read_kind_allows():
    assert run(req("read"), Boom()).outcome == "allow"


def test_read_only_rejects_edit():
    d = run(req("edit"), Boom(), read_only=True)
    assert d.outcome == "reject" and "審查座位只能讀" in d.reason


def test_router_auto_allows():
    d = run(req("execute", "ls"), ScriptedRouter())
    assert d.outcome == "allow" and d.option_id == "o" and d.verdict.auto


def test_router_not_auto_escalates():
    d = run(req("execute"), NoAuto())
    assert d.outcome == "escalate" and "risk" in d.reason and "allow" in d.reason


def test_pick_option():
    assert pick_option(OPTS, "allow") == "o"
    assert pick_option(OPTS, "reject") == "r"
    assert pick_option([{"optionId": "a", "kind": "allow_always"}], "allow") == "a"
    assert pick_option([], "allow") == ""
    assert pick_option([{"optionId": "a", "kind": "allow_once"}], "reject") == ""


def test_pending_timeout():
    assert PendingPermission(timeout=0.2).wait() == "timeout"


def test_pending_answer():
    p = PendingPermission(timeout=5)
    threading.Timer(0.1, p.answer, args=("allow",)).start()
    assert p.wait() == "allow"


def _esc(raw, title="run", kind="execute"):
    r = PermissionRequest("t1", title, kind, raw, OPTS)
    return run(r, Boom())


def test_deny_command_plus_args():
    assert _esc({"command": "ls", "args": ["rm", "-rf", "/"]}).outcome == "escalate"


def test_deny_unknown_key():
    assert _esc({"script": "rm -rf build"}).outcome == "escalate"


def test_deny_nested():
    assert _esc({"a": {"b": [{"c": "rm -rf x"}]}}).outcome == "escalate"


def test_deny_title_only():
    assert _esc({"command": "ls"}, title="rm -rf /").outcome == "escalate"


SCOPE = "/tmp/ws-scope"


def _edit(raw, router, scope=SCOPE):
    r = PermissionRequest("t1", "edit", "edit", raw, OPTS)
    return decide(r, goal="g", scope=scope, deny_patterns=DENY, router=router, read_only=False)


def test_relative_escape_escalates():
    d = _edit({"path": "../outside.txt"}, Boom())
    assert d.outcome == "escalate" and "路徑超出工作範圍：../outside.txt" in d.reason


def test_absolute_outside_escalates():
    assert _edit({"locations": [{"path": "/etc/passwd"}]}, Boom()).outcome == "escalate"


def test_inside_scope_reaches_router():
    assert _edit({"path": "src/a.py"}, ScriptedRouter()).outcome == "allow"
    assert _edit({"file_path": SCOPE + "/b.py"}, ScriptedRouter()).outcome == "allow"


def test_case_insensitive_compare(monkeypatch):
    monkeypatch.setattr(os.path, "normcase", lambda p: p.lower())
    assert _edit({"path": "/TMP/WS-SCOPE/a.py"}, ScriptedRouter()).outcome == "allow"


def test_pending_late_answer_ignored():
    p = PendingPermission(timeout=0.1)
    assert p.wait() == "timeout"
    p.answer("allow")
    assert p.wait() == "timeout"


def test_pending_first_wins_and_invalid():
    p = PendingPermission(timeout=1)
    with pytest.raises(ValueError):
        p.answer("maybe")
    p.answer("reject")
    p.answer("allow")
    assert p.wait() == "reject"


def test_deep_nesting_escalates():
    d = {}
    cur = d
    for _ in range(5000):
        cur["a"] = {}
        cur = cur["a"]
    r = _esc(d)
    assert r.outcome == "escalate" and "過深或過大" in r.reason


def test_cyclic_escalates():
    d = {}
    d["self"] = d
    assert _esc(d).outcome == "escalate"


def test_exception_fails_closed():
    class Bad(ScriptedRouter):
        def decide_allow(self, state_text):
            raise RuntimeError("x")
    d = run(req("execute"), Bad())
    assert d.outcome == "escalate" and d.option_id == "" and "RuntimeError" in d.reason


def test_nul_path_escalates():
    assert _edit({"path": "a\x00b"}, Boom()).outcome == "escalate"


def test_bad_shapes_do_not_crash():
    assert _edit({"locations": 5}, ScriptedRouter()).outcome == "allow"


def test_new_path_key_and_dict_key_scanned():
    assert _edit({"new_path": "../x"}, Boom()).outcome == "escalate"
    assert _esc({"rm -rf /": 1}).outcome == "escalate"


def test_pending_answer_reports_first_wins():
    p = PendingPermission(timeout=5)
    assert p.answer("allow") is True
    assert p.answer("reject") is False
    assert p.wait() == "allow"
    q = PendingPermission(timeout=0.05)
    assert q.wait() == "timeout"
    assert q.answer("allow") is False


def test_reason_has_numbers_model_and_ctx_reaches_router():
    import json as _json
    from team.models import ModelReply, ModelRouter, ModelConfig
    cfg = ModelConfig("http://x", "k", "k2")
    r = ModelRouter(cfg, d1=lambda s, q: ModelReply({"allow": {"noul": 0.93}, "risk": {"noul": 0.04}}, None,
                                                     "Decision-1-v2"),
                    glm=lambda p, e, t: _json.dumps({}))
    calls = []
    r.listeners.append(calls.append)
    d = decide(req("execute", "pytest -q"), goal="g", scope="", deny_patterns=DENY, router=r, read_only=False,
               ctx={"room_id": "r12345678", "role": "policy"})
    assert d.outcome == "allow"
    assert "Decision-1 判定安全" in d.reason and "allow 0.93 risk 0.04" in d.reason and "Decision-1-v2" in d.reason
    assert calls[0].room_id == "r12345678" and calls[0].model == "Decision-1-v2"
    # 舊介面的假路由器（沒有 ctx 參數）照常運作
    assert decide(req("execute"), goal="g", scope="", deny_patterns=DENY, router=NoAuto(), read_only=False,
                  ctx={"room_id": "r1"}).outcome == "escalate"


# ---- 最終審查第 4 項：讀取也要先檢查路徑範圍；fetch 交給模型 ----
def _kind(kind, raw, router, scope=SCOPE, read_only=False):
    r = PermissionRequest("t1", kind, kind, raw, OPTS)
    return decide(r, goal="g", scope=scope, deny_patterns=DENY, router=router, read_only=read_only)


def test_read_outside_scope_escalates():
    d = _kind("read", {"path": "../litellm.local.json"}, Boom())
    assert d.outcome == "escalate" and "路徑超出工作範圍" in d.reason


def test_read_outside_scope_escalates_for_review_seat():
    d = _kind("read", {"file_path": "../litellm.local.json"}, Boom(), read_only=True)
    assert d.outcome == "escalate"


def test_read_inside_scope_auto_allowed():
    d = _kind("read", {"path": "src/a.py"}, Boom())
    assert d.outcome == "allow" and d.option_id == "o"


def test_home_path_expanded_and_escalates():
    d = _kind("edit", {"path": "~/.bashrc"}, Boom())
    assert d.outcome == "escalate" and "~/.bashrc" in d.reason


def test_fetch_goes_to_router():
    calls = []

    class Spy(ScriptedRouter):
        def decide_allow(self, state_text):
            calls.append(state_text)
            return AllowVerdict(False, 0.5, 0.5, "x", "gray")

    d = _kind("fetch", {"url": "https://example.com"}, Spy())
    assert calls and d.outcome == "escalate"


def test_fetch_on_review_seat_goes_to_router_not_reject():
    d = _kind("fetch", {"url": "https://example.com"}, ScriptedRouter(), read_only=True)
    assert d.outcome == "allow" and d.verdict is not None


def test_tmp_commit_msg_allowed_while_reference_root_write_still_rejected():
    scope = "/home/bofmgr/rust_bof456_server/.ai-team-worktrees/wt1"
    ref_root = "/home/bofmgr"
    # 寫入 /tmp/c1msg 作為 git commit -F 訊息不應被誤判為修改唯讀參考區
    r_tmp = req("execute", "printf 'fix(audit): msg' > /tmp/c1msg && git commit -F /tmp/c1msg")
    d_tmp = decide(r_tmp, goal="g", scope=scope, deny_patterns=DENY,
                   router=ScriptedRouter(), read_only=False, reference_root=ref_root)
    assert d_tmp.outcome == "allow"

    # 真正試圖寫入唯讀參考區或透過 /tmp/.. 跳脫仍必須嚴格拒絕
    r_ref = req("execute", "echo x > /home/bofmgr/cpp_bof456_server/a.cpp")
    d_ref = decide(r_ref, goal="g", scope=scope, deny_patterns=DENY,
                   router=ScriptedRouter(), read_only=False, reference_root=ref_root)
    assert d_ref.outcome == "reject" and d_ref.rule == "scope"

    r_trav = req("execute", "echo x > /tmp/../home/bofmgr/cpp_bof456_server/a.cpp")
    d_trav = decide(r_trav, goal="g", scope=scope, deny_patterns=DENY,
                    router=ScriptedRouter(), read_only=False, reference_root=ref_root)
    assert d_trav.outcome == "reject" and d_trav.rule == "scope"


def test_plan_context_passed_to_decision_model():
    from team.policy import format_plan_context

    seen = []

    class SpyRouter(ScriptedRouter):
        def decide_allow(self, state_text):
            seen.append(state_text)
            return AllowVerdict(True, 0.96, 0.02, "Decision-1", "計畫內安全操作")

    plan_ctx = format_plan_context(
        [{"id": "C1", "text": "修改 tools/cpp_rust_audit/cli.py 並新增測試", "check": "pytest tools/cpp_rust_audit"}],
        ["高層主管核准使用 worktree 相對路徑"],
        phase="build",
        reference_root="/home/bofmgr",
    )
    r = PermissionRequest("t1", "edit", "Write File", {"path": "tools/cpp_rust_audit/cli.py"}, OPTS)
    d = decide(
        r,
        goal="CPP 遷移 RUST",
        scope="/home/bofmgr/rust_bof456_server/.ai-team-worktrees/wt1",
        deny_patterns=DENY,
        router=SpyRouter(),
        read_only=False,
        reference_root="/home/bofmgr",
        plan_context=plan_ctx,
    )
    assert d.outcome == "allow"
    assert seen and "C1: 修改 tools/cpp_rust_audit/cli.py" in seen[0]
    assert "高層主管核准使用 worktree 相對路徑" in seen[0]
    assert "唯讀參考母區" in seen[0]
    assert "/home/bofmgr/rust_bof456_server/.ai-team-worktrees/wt1/tools/cpp_rust_audit/cli.py" in seen[0]


def test_deterministic_gate_verdicts():
    from team.policy import deterministic_gate
    assert deterministic_gate(tests_passed=True) == "PASS"
    assert deterministic_gate(tests_passed=False) == "REWORK"
    assert deterministic_gate(tests_passed=True, security_critical=True) == "BLOCK"
    assert deterministic_gate(tests_passed=True, review_version="abc", head_commit="def") == "REWORK"
    assert deterministic_gate(tests_passed=True, blocking=["missing test"]) == "REWORK"
    assert deterministic_gate(tests_passed=True, retries=4, max_retries=3) == "ESCALATE"


def test_redirect_re_ignores_quoted_arrows_but_blocks_real_redirect():
    scope = "/home/bofmgr/rust_bof456_server/.ai-team-worktrees/wt1"
    ref = "/home/bofmgr"
    # 字串內的 -> /home/bofmgr/... 不應被誤判成寫入重導向
    r_grep = PermissionRequest(
        "t1", "Run Command", "execute",
        {"command": 'grep "-> /home/bofmgr/cpp_bof456_server/a.cpp" src/main.rs'},
        OPTS,
    )
    d_grep = decide(r_grep, goal="g", scope=scope, deny_patterns=DENY, router=ScriptedRouter(), read_only=False, reference_root=ref)
    assert d_grep.outcome == "allow"

    # 真正的外部寫入重導向仍必須硬拒
    r_bad = PermissionRequest(
        "t2", "Run Command", "execute",
        {"command": "echo hacked > '/home/bofmgr/cpp_bof456_server/a.cpp'"},
        OPTS,
    )
    d_bad = decide(r_bad, goal="g", scope=scope, deny_patterns=DENY, router=ScriptedRouter(), read_only=False, reference_root=ref)
    assert d_bad.outcome == "reject" and d_bad.rule == "scope"


def test_readonly_execute_with_reference_root_path_reaches_router():
    scope = "/home/bofmgr/rust_bof456_server/.ai-team-worktrees/wt1"
    ref = "/home/bofmgr"
    r = PermissionRequest(
        "t3", "Run Command", "execute",
        {"command": "cat /home/bofmgr/cpp_bof456_server/main.cpp", "path": "/home/bofmgr/cpp_bof456_server/main.cpp"},
        OPTS,
    )
    d = decide(r, goal="g", scope=scope, deny_patterns=DENY, router=ScriptedRouter(), read_only=False, reference_root=ref)
    assert d.outcome == "allow"


def test_posix_scope_tilde_is_outside_and_relative_is_inside():
    from team.policy import _outside_scope
    scope = "/home/bofmgr/rust_bof456_server/.ai-team-worktrees/wt1"
    assert _outside_scope("tools/cli.py", scope, base_dir=scope) is False
    assert _outside_scope("~/cpp_bof456_server/a.cpp", scope, base_dir=scope) is True


def test_folder_boundary_note_in_po_and_reviewer_prompts():
    from team import personas
    ref = "/home/bofmgr"
    tgt = "rust_bof456_server"
    assert "資料夾權限邊界" in personas.kickoff("g", "ctx", "slug", reference_root=ref, target_subdir=tgt)
    assert "資料夾權限邊界" in personas.revise_spec(["x"], goal="g", reference_root=ref, target_subdir=tgt)
    assert "資料夾權限邊界" in personas.review("g", [], "", "", reference_root=ref, target_subdir=tgt)
    assert "資料夾權限邊界" in personas.checkpoint_review("g", [], "", "", "", "alert", reference_root=ref, target_subdir=tgt)
