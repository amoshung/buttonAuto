"""像素辦公室轉接（office_bridge.py）：工作階段 → 夥伴、網頁上的動作 → 助理指令。"""
import http.client
import json
from types import SimpleNamespace

import pytest

from config import AppConfig
from dashboard import Dashboard
from office_bridge import (ACT_DISMISS, ACT_NEW_CHAT, ACT_SEND_SUGGESTION, OfficeBridge, worker_status)
from sessions import SessionState, SessionStore, now_str


class FakeWorker:
    def __init__(self, store):
        self.store = store
        self.usage = {}
        self.calls = []

    def request_click(self, key, button):
        self.calls.append(("click", key, button))

    def request_send(self, key, text):
        self.calls.append(("send", key, text))

    def request_rollover(self, key):
        self.calls.append(("rollover", key))


def _win(key, app, project, path, remote=""):
    return SimpleNamespace(key=key, app_label=app, project=project, path=path, remote=remote, title=f"{project} - {app}")


KIRO = "kiro|d:\\w\\a"
AG = "antigravity|ssh://h/home/b"
SELF = "kiro|d:\\_ai_projs\\buttonauto"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import office_bridge
    monkeypatch.setattr(office_bridge, "contains_assistant", lambda path, remote="": "buttonauto" in (path or "").lower())
    store = SessionStore(root=str(tmp_path / "sessions"))
    cfg = AppConfig()
    wins = [_win(KIRO, "Kiro", "a", "D:\\w\\a"), _win(AG, "Antigravity", "b", "/home/b", "h"),
            _win(SELF, "Kiro", "buttonAuto", "D:\\_AI_Projs\\buttonAuto")]
    worker = FakeWorker(store)
    modes = []
    bridge = OfficeBridge(store, cfg, lambda: wins, worker, on_mode=lambda k, m: modes.append((k, m)),
                          on_running=lambda r: modes.append(("running", r)))
    a = store.get(KIRO, "Kiro", "a", "D:\\w\\a")

    def ma(st):
        st.goal = "加入匯出"
        st.agent_state = "waiting_approval"
        st.pending_approval = "Get-ChildItem -Recurse"
        st.approval_buttons = ["Yes, allow this time", "Yes, and always allow 'Get-ChildItem'", "Skip"]
        st.acceptance = [{"text": "匯出", "test": "pytest", "status": "verified"},
                         {"text": "錯誤", "test": "pytest", "status": "todo"}]
        st.auto_send_times = [now_str()]
    a.update(ma)
    a.history("observe", agent_state="waiting_approval", summary="代理想列出檔案")
    b = store.get(AG, "Antigravity", "b", "/home/b", "h")
    b.update(lambda st: (setattr(st, "goal", "G"), setattr(st, "agent_state", "working"),
                         setattr(st, "current_task", "實作 U2")))
    cfg.set_mode(KIRO, "auto")
    cfg.set_mode(AG, "suggest")
    return SimpleNamespace(store=store, cfg=cfg, bridge=bridge, worker=worker, modes=modes, a=a, b=b)


def test_snapshot_maps_sessions_to_office_workers(env):
    snap = env.bridge.snapshot({"today": {"auto_clicks": 1}})
    by = {w["id"]: w for w in snap["workers"]}
    assert set(by) == {KIRO, AG, SELF}  # 只列出開著的視窗
    k = by[KIRO]
    # 自動駕駛：等允許由助理判斷，不算「需要你」，但網頁上仍可以自己按
    assert k["status"] == "paused" and not k["needsYou"] and k["mode"] == "auto" and k["category"] == "ide"
    assert k["quickActions"][:3] == ["Yes, allow this time", "Yes, and always allow 'Get-ChildItem'", "Skip"]
    assert ACT_NEW_CHAT in k["quickActions"] and "Get-ChildItem" in k["approvalCommand"]
    assert (k["verified"], k["core"], k["hourSends"]) == (1, 2, 1)
    assert k["logs"] and k["logs"][0]["timestamp"].count(":") >= 2
    assert by[AG]["status"] == "working" and by[AG]["activity"] == "實作 U2"
    assert by[SELF]["status"] == "idle" and by[SELF]["modeLocked"] and by[SELF]["quickActions"] == []  # 關閉→茶水間
    assert by[SELF]["statusLabel"].startswith("關閉") and by[KIRO]["statusLabel"].startswith("等允許")
    assert by[KIRO]["deskId"] in ("desk-1", "desk-2", "desk-3", "desk-4")  # 自動駕駛區
    assert by[AG]["deskId"] in ("desk-5", "desk-6", "desk-7", "desk-8")  # 只建議區
    assert by[KIRO]["acceptance"][0] == {"text": "匯出", "status": "verified", "manual": False, "checked": False}
    desks = [w["deskId"] for w in snap["workers"]]
    assert len(set(desks)) == 3  # 每人一個座位
    assert snap["workers"][0]["id"] == KIRO  # 自動駕駛排最前面（沒有人需要你時）
    assert env.bridge.snapshot()["workers"][0]["deskId"] == by[KIRO]["deskId"]  # 座位不會換


def test_worker_status_rules():
    from office_bridge import status_label
    st = SessionState(goal="G", agent_state="idle", acceptance_confirmed_at="x", done_at="x")
    assert worker_status(st, "auto") == "paused" and status_label(st, "auto") == "目標已達成"
    st = SessionState(goal="G", agent_state="working", suggested_prompt="繼續")
    assert worker_status(st, "suggest") == "needs_help"  # 只建議：等你按送出
    assert worker_status(st, "auto") == "working"
    assert worker_status(st, "off") == "idle"  # 關閉：去茶水間
    st = SessionState(pending_human=["目標已達成，請你驗收"], agent_state="done")
    assert worker_status(st, "auto") == "paused"  # 達成的提醒不算「需要你」
    st = SessionState(goal="G", paused_reason="連續 3 次要不到合格的證據", pending_human=["連續 3 次要不到合格的證據"])
    assert status_label(st, "auto") == "自動駕駛已暫停" and worker_status(st, "auto") == "needs_help"


def test_resume_autopilot_from_web(env):
    from office_bridge import ACT_RESUME
    env.a.update(lambda st: (setattr(st, "paused_reason", "卡住"), st.pending_human.append("卡住")))
    snap = {w["id"]: w for w in env.bridge.snapshot()["workers"]}
    assert ACT_RESUME in snap[KIRO]["quickActions"]
    assert env.bridge.assist(KIRO, ACT_RESUME, "")["ok"] is True
    st = env.a.load()
    assert st.paused_reason == "" and "卡住" not in st.pending_human


def test_assist_only_accepts_listed_actions(env):
    br, wk = env.bridge, env.worker
    assert br.assist(KIRO, "rm -rf /", "")["ok"] is False
    assert br.assist(KIRO, "Yes, allow this time", "")["ok"] is True
    assert br.assist(KIRO, ACT_NEW_CHAT, "")["ok"] is True
    assert br.assist(KIRO, "自訂協助指令", "請先跑測試")["ok"] is True
    assert wk.calls == [("click", KIRO, "Yes, allow this time"), ("rollover", KIRO), ("send", KIRO, "請先跑測試")]
    assert br.assist(AG, ACT_SEND_SUGGESTION, "")["ok"] is False  # 沒有建議時不能送
    env.b.update(lambda st: setattr(st, "suggested_prompt", "請繼續 U2"))
    assert br.assist(AG, ACT_SEND_SUGGESTION, "")["ok"] is True and wk.calls[-1] == ("send", AG, "請繼續 U2")
    assert br.assist(SELF, "自訂協助指令", "hi")["ok"] is False  # 關閉的視窗不操作
    env.a.update(lambda st: st.pending_human.append("確認驗收 spec"))
    assert br.assist(KIRO, ACT_DISMISS, "")["ok"] is True and env.a.load().pending_human == []
    assert br.snapshot()["workers"][0]["celebrateUntil"] > 0


def test_mode_and_running_go_through_callbacks(env):
    br = env.bridge
    assert br.set_mode(SELF, "auto")["ok"] is False  # 助理自己的專案固定關閉
    assert br.set_mode(KIRO, "weird")["ok"] is False
    assert br.set_mode(AG, "auto")["ok"] is True and br.set_running(False)["ok"] is True
    assert env.modes == [(AG, "auto"), ("running", False)]


def test_http_endpoints_require_token_and_return_snapshot(env):
    d = Dashboard(env.worker, env.cfg, list_windows=env.bridge.list_windows,
                  on_mode=lambda k, m: env.modes.append((k, m))).start()
    try:
        def req(method, path, headers=None, body=None):
            c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
            h = {"Host": f"127.0.0.1:{d.port}"}
            h.update(headers or {})
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            data = r.read()
            c.close()
            return r, data
        cookie = {"Cookie": f"aea_t={d.token}"}
        r, data = req("GET", "/api/watcher/state", cookie)
        assert r.status == 200 and len(json.loads(data)["workers"]) == 3
        body = json.dumps({"workerId": AG, "mode": "auto"})
        r, _ = req("POST", "/api/office/mode", dict(cookie, **{"Content-Type": "application/json"}), body)
        assert r.status == 401  # 只有 cookie 不夠
        r, data = req("POST", "/api/office/mode", {"X-Token": d.token, "Content-Type": "application/json"}, body)
        out = json.loads(data)
        assert out["ok"] is True and out["snapshot"]["workers"] and env.modes[-1] == (AG, "auto")
        r, data = req("POST", "/api/watcher/assist", {"X-Token": d.token, "Content-Type": "application/json"},
                      json.dumps({"workerId": KIRO, "actionLabel": "Skip", "message": ""}))
        assert json.loads(data)["ok"] is True and env.worker.calls[-1] == ("click", KIRO, "Skip")
    finally:
        d.stop()


def test_cancel_is_never_offered_on_the_web(env):
    env.a.update(lambda st: st.approval_buttons.append("Cancel"))
    k = {w["id"]: w for w in env.bridge.snapshot()["workers"]}[KIRO]
    assert "Cancel" not in k["quickActions"]
    assert env.bridge.assist(KIRO, "Cancel", "")["ok"] is False


def test_suggest_mode_approval_needs_you(env):
    env.cfg.set_mode(KIRO, "suggest")
    k = {w["id"]: w for w in env.bridge.snapshot()["workers"]}[KIRO]
    assert k["needsYou"] and k["status"] == "needs_help" and k["statusLabel"] == "等你允許"
