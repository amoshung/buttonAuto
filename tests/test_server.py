"""網頁版後端（server.py）：權限、快照、設定、舊 IDE 介面已移除。不連 AI、不碰任何 IDE。"""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

import automation
import server
from projects import ProjectWindow
from sessions import SessionStore

TOKEN = "T" * 24


class FakeDetector:
    def __init__(self, wins):
        self.wins = wins

    def detect(self):
        return list(self.wins)

    def describe_hwnd(self, hwnd):
        return None


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    store = SessionStore(root=str(tmp_path / "sessions"))
    monkeypatch.setattr(automation, "SessionStore", lambda: store)
    proj = tmp_path / "proj"
    proj.mkdir()
    wins = [ProjectWindow(11, "kiro", "Kiro", "a - proj - Kiro", "proj", "", str(proj), False, False),
            ProjectWindow(12, "antigravity", "Antigravity", "b", "remote1", "host", "/home/x", False, True)]
    app = server.AssistantServer(config_path=str(tmp_path / "config.json"), secrets_path=str(tmp_path / "s.json"),
                                 detector=FakeDetector(wins), start_threads=False, token=TOKEN)
    httpd = app.serve(0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield app, wins
    httpd.shutdown()
    httpd.server_close()


def _req(app, path, body=None, cookie=True, token=True, host=None):
    headers = {"Host": host or f"127.0.0.1:{app.port}"}
    if cookie:
        headers["Cookie"] = "aea_t=" + TOKEN
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        if token:
            headers["X-Token"] = TOKEN
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _json(app, path, body=None, **kw):
    code, data, _h = _req(app, path, body, **kw)
    return code, json.loads(data)


def test_auth_cookie_token_and_host(srv):
    app, _ = srv
    assert _req(app, "/api/state", cookie=False)[0] == 401
    assert _req(app, "/api/state", host="evil.example:80")[0] == 403
    code, _b, headers = _req(app, "/?t=" + TOKEN, cookie=False)
    assert code == 303 and "aea_t=" + TOKEN in headers.get("Set-Cookie", "") and "HttpOnly" in headers["Set-Cookie"]
    code, page, headers = _req(app, "/")
    assert code == 200 and TOKEN.encode() in page and "script-src 'self'" in headers["Content-Security-Policy"]
    # cookie 不夠：會動手的 POST 一定要 X-Token
    assert _req(app, "/api/settings/deny/test", {"text": "x"}, token=False)[0] == 401
    assert _req(app, "/healthz", cookie=False)[0] == 200


def test_snapshot_is_app_info_only(srv):
    app, _ = srv
    code, snap = _json(app, "/api/state")
    assert code == 200 and set(snap) == {"app"} and snap["app"]["version"] == app.version
    assert "time" not in snap["app"]  # 快照沒有每秒都變的欄位，SSE 只在真的有變化時推送


def test_old_ide_ui_routes_are_gone(srv):
    """舊的 IDE 視窗頁、基本按鈕精靈、API 連線頁與 Next.js 像素辦公室都已移除。"""
    app, wins = srv
    for path in ("/office/", "/office", "/_next/static/x.js", "/classic", "/api/watcher/state", "/api/watcher/stream",
                 "/api/session?key=x", "/api/session/report?key=x&name=y", "/api/ai/status", "/api/target.png?i=0"):
        assert _req(app, path)[0] == 404, path
    for path, body in (("/api/running", {"running": False}), ("/api/session/mode", {"key": wins[0].key, "mode": "auto"}),
                       ("/api/session/send", {"key": wins[0].key, "text": "繼續"}), ("/api/office/mode", {}),
                       ("/api/office/running", {"running": True}), ("/api/watcher/assist", {}), ("/api/decide", {}),
                       ("/api/humans/resume", {"hwnd": 1}), ("/api/app/mode", {"mode": "ai"}),
                       ("/api/settings/autonomy", {"level": "bold"}), ("/api/settings/api", {}),
                       ("/api/basic", {}), ("/api/basic/capture", {"kind": "targets"}), ("/api/summary", {})):
        assert _req(app, path, body)[0] == 404, path
    assert app.cfg.mode_of(wins[0].key) == "off"  # 沒有任何舊動作被執行


def test_no_old_automation_threads(tmp_path, monkeypatch):
    """start_threads=True 也不建立／不啟動 AutomationWorker（含 InputMonitor 全域攔截）、UIA 輪巡、F8 熱鍵與圖片比對。"""
    import threading as th
    store = SessionStore(root=str(tmp_path / "sessions"))
    monkeypatch.setattr(automation, "SessionStore", lambda: store)
    started = []
    monkeypatch.setattr(automation.AutomationWorker, "start", lambda self: started.append("worker"))
    monkeypatch.setattr(automation.AutomationWorker, "__init__", lambda self, *a, **k: started.append("worker-init"))
    import win_utils
    monkeypatch.setattr(win_utils.InputMonitor, "get", classmethod(lambda cls: started.append("input-monitor")))
    had_monitor = any(t.name == "InputMonitor" for t in th.enumerate())
    app = server.AssistantServer(config_path=str(tmp_path / "c.json"), secrets_path=str(tmp_path / "s.json"),
                                 detector=FakeDetector([]), start_threads=True, token=TOKEN,
                                 team_root=str(tmp_path / "team"), team_base_dir=str(tmp_path),
                                 team_opts={"clis_fn": lambda: {}})
    try:
        names = {t.name for t in th.enumerate()}
        assert not started and "window-scan" not in names and "hotkey-image" not in names
        assert had_monitor or "InputMonitor" not in names  # 沒有掛全域鍵盤／滑鼠攔截
        assert not hasattr(app, "office") and app.worker is None
    finally:
        app.team.stop(timeout=10)
        app._stop.set()


def test_settings_team_fields_and_deny(srv):
    app, _ = srv
    _c, r = _json(app, "/api/settings/ai", {"team_context_rollover": 0.8, "team_max_running": 99, "ai_max_risk": 0.9})
    assert r["ok"] and app.cfg.team_context_rollover == 0.8 and app.cfg.team_max_running == 16.0
    _c, s = _json(app, "/api/settings")
    assert set(s["ai"]) == {"team_max_running", "team_cost_weight", "team_context_rollover", "team_plugin_dirs"}
    assert "api" not in s and "basic" not in s and "autonomy" not in s
    assert _json(app, "/api/settings/deny", {"patterns": ["(unclosed"]})[1]["ok"] is False
    assert _json(app, "/api/settings/deny/test", {"text": "git push --force origin main"})[1]["hit"]


def test_static_paths_cannot_escape(srv):
    app, _ = srv
    assert _req(app, "/static/../../config.py")[0] == 404
    assert _req(app, "/_next/../../server.py")[0] == 404
    assert _req(app, "/static/app.js")[0] == 200
