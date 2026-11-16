"""網頁儀表板（E1～E5）：資料正確、只接受本機與存取碼、代按只接受代理目前列出的按鈕。"""
import http.client
import json

import pytest

from config import AppConfig
from dashboard import Dashboard, build_snapshot
from sessions import SessionStore, now_str


class FakeWorker:
    def __init__(self, store):
        self.store = store
        self.usage = {"text_reads": 3, "image_reads": 1}
        self.clicks = []

    def request_click(self, key, button):
        self.clicks.append((key, button))


def _setup(tmp_path):
    store = SessionStore(root=str(tmp_path / "sessions"))
    cfg = AppConfig()
    a = store.get("kiro|d:\\w\\a", "Kiro", "a", "D:\\w\\a")
    b = store.get("antigravity|ssh:h:/home/b", "Antigravity", "b", "/home/b", "h")

    def ma(st):
        st.goal = "加入 CSV 匯出"
        st.agent_state = "waiting_approval"
        st.pending_approval = "pytest -q"
        st.approval_buttons = ["Allow", "Always allow", "Deny"]
        st.acceptance = [{"text": "匯出", "test": "pytest -q", "status": "verified"},
                         {"text": "錯誤訊息", "test": "pytest -q", "status": "todo"}]
        st.auto_send_times = [now_str()]
    a.update(ma)
    b.update(lambda st: st.pending_human.append("確認驗收 spec"))
    a.history("action", text="🖱 Invoke「Allow」")
    a.history("action", action="send", text="繼續", by="assistant")
    cfg.set_mode("kiro|d:\\w\\a", "auto")
    cfg.set_mode("antigravity|ssh:h:/home/b", "suggest")
    return store, cfg


def test_snapshot_cards_decisions_feed_usage(tmp_path):
    store, cfg = _setup(tmp_path)
    s = build_snapshot(store, cfg.mode_of, {"text_reads": 3}, 12)
    names = [a["name"] for a in s["agents"]]
    assert names[0] == "Antigravity｜b"  # 需要你的排最上面
    kiro = next(a for a in s["agents"] if a["app"] == "Kiro")
    assert kiro["mode"] == "auto" and kiro["verified"] == 1 and kiro["core"] == 2 and kiro["hour_sends"] == 1
    assert s["decisions"][0]["kind"] == "approval" and s["decisions"][0]["buttons"][0] == "Allow"
    assert any("代按「Allow」" in f["text"] for f in s["feed"])
    assert s["usage"]["today"]["auto_clicks"] == 1 and s["usage"]["today"]["auto_sends"] == 1


@pytest.fixture
def dash(tmp_path):
    store, cfg = _setup(tmp_path)
    d = Dashboard(FakeWorker(store), cfg).start()
    yield d
    d.stop()


def _req(d, method, path, host=None, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", d.port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{d.port}"}
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data


def test_requires_token_and_local_host(dash):
    r, _ = _req(dash, "GET", "/api/state")
    assert r.status == 401
    r, _ = _req(dash, "GET", f"/?t={dash.token}", host=f"evil.example:{dash.port}")
    assert r.status == 403  # DNS rebinding
    r, _ = _req(dash, "GET", f"/?t={dash.token}")
    assert r.status == 303 and "HttpOnly" in r.getheader("Set-Cookie")
    cookie = {"Cookie": f"aea_t={dash.token}"}
    r, data = _req(dash, "GET", "/api/state", headers=cookie)
    assert r.status == 200 and json.loads(data)["agents"]
    r, data = _req(dash, "GET", "/", headers=cookie)
    assert r.status == 200 and dash.token.encode() in data and "default-src 'self'" in r.getheader("Content-Security-Policy")


def test_decide_requires_header_token_and_listed_button(dash):
    body = json.dumps({"key": "kiro|d:\\w\\a", "button": "Allow"})
    cookie = {"Cookie": f"aea_t={dash.token}", "Content-Type": "application/json"}
    r, _ = _req(dash, "POST", "/api/decide", headers=cookie, body=body)
    assert r.status == 401  # 只有 cookie 不夠（防跨站請求）
    hdr = {"X-Token": dash.token, "Content-Type": "application/json"}
    r, data = _req(dash, "POST", "/api/decide", headers=hdr,
                   body=json.dumps({"key": "kiro|d:\\w\\a", "button": "rm -rf"}))
    assert json.loads(data)["ok"] is False
    r, data = _req(dash, "POST", "/api/decide", headers=hdr, body=body)
    assert json.loads(data)["ok"] is True
    assert dash.worker.clicks == [("kiro|d:\\w\\a", "Allow")]


def test_office_script_is_served(dash):
    cookie = {"Cookie": f"aea_t={dash.token}"}
    r, data = _req(dash, "GET", "/office.js", headers=cookie)
    assert r.status == 200 and b"window.Office" in data
    r, data = _req(dash, "GET", "/", headers=cookie)
    assert b'id="office-canvas"' in data and b"/office.js" in data
