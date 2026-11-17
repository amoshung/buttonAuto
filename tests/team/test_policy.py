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
