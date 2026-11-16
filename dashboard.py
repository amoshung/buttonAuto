"""網頁儀表板（E1～E5）：只綁 127.0.0.1，附隨機存取碼。

- 資料來自各工作階段的 state.json／history.jsonl（和桌面版同一份），即時更新用 SSE。
- 唯一會「動手」的 API 是 /api/decide：代按代理正在等的允許按鈕；按鈕名稱必須是該工作階段目前列出的其中一個，
  實際執行交給背景執行緒的 request_click，規則與桌面版的「代按」完全相同。
- 防護：Host 必須是 127.0.0.1／localhost（防 DNS rebinding）；GET 用存取碼或 HttpOnly cookie；
  POST 另外要求 X-Token 標頭（cookie 不算，防跨站請求）；頁面不使用 innerHTML 塞資料。
"""
import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ai_client import call_counts
from projects import contains_assistant
from office_bridge import OfficeBridge
from sessions import SessionStore, phase_label, phase_of, core_items, is_automated

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PIXEL_DIR = os.path.join(WEB_DIR, "pixel")  # 像素辦公室（pixeloffice/ 的 npm run build 輸出）
# 經典版（清單式）儀表板
_STATIC = {"/classic/": ("index.html", "text/html; charset=utf-8"),
           "/classic/app.js": ("app.js", "text/javascript; charset=utf-8"),
           "/classic/office.js": ("office.js", "text/javascript; charset=utf-8"),
           "/classic/style.css": ("style.css", "text/css; charset=utf-8")}
_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml",
         ".png": "image/png", ".ico": "image/x-icon"}
# 經典版不需要內嵌腳本；像素辦公室（Next.js 靜態輸出）有內嵌的啟動腳本與樣式，資料一律由 React 跳脫後顯示
_CSP_STRICT = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
_CSP_PIXEL = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'")


def pixel_file(url_path: str) -> Optional[str]:
    """網址 → web/pixel 裡的檔案；不在資料夾內（例如 ../）或不存在就回傳 None。"""
    rel = url_path.lstrip("/") or "index.html"
    full = os.path.realpath(os.path.join(PIXEL_DIR, rel))
    root = os.path.realpath(PIXEL_DIR)
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        return None
    return full
_APP_AVATAR = {"Kiro": "👻", "Antigravity": "🚀"}
_STATE_AVATAR = {"working": "⌨️", "waiting_approval": "🙋", "asking_choice": "🙋", "waiting_input": "💬",
                 "done": "🎉", "error": "⚠️", "idle": "💤"}
_MODE_ORDER = {"auto": 0, "suggest": 1, "off": 2}


def _hour_count(times: List[str], now: datetime.datetime) -> int:
    cut = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for t in times if t >= cut)


def build_snapshot(store: SessionStore, mode_of: Callable[[str], str], usage: Optional[Dict[str, int]] = None,
                   max_per_hour: float = 12.0, running: bool = True,
                   now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """儀表板要顯示的全部資料（純讀取）。"""
    now = now or datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    agents: List[Dict[str, Any]] = []
    feed: List[Dict[str, str]] = []
    decisions: List[Dict[str, Any]] = []
    totals = {"auto_clicks": 0, "auto_sends": 0, "user_actions": 0, "verifies": 0, "asks": 0}
    for sess in store.all():
        st = sess.load()
        if not st.key:
            continue
        mode = "off" if contains_assistant(st.path, st.remote) else mode_of(st.key)
        name = f"{st.app}｜{st.project}"
        core = core_items(st)
        manual = [a for a in st.acceptance if not is_automated(a)]
        hour = _hour_count(st.auto_send_times, now)
        needs = list(st.pending_human)
        agents.append({
            "key": st.key, "name": name, "app": st.app, "project": st.project, "mode": mode,
            "avatar": _APP_AVATAR.get(st.app, "🤖"), "mood": _STATE_AVATAR.get(st.agent_state, "·"),
            "agent_state": st.agent_state, "phase": phase_of(st), "phase_label": phase_label(st),
            "goal": st.goal, "doing": st.current_task or st.status_summary, "chat": st.chat_title,
            "verified": sum(1 for a in core if a.get("status") == "verified"), "core": len(core),
            "manual_checked": sum(1 for a in manual if a.get("user_checked")), "manual": len(manual),
            "paused": st.paused_reason, "needs_you": needs, "hour_sends": hour,
            "hour_cap": int(max_per_hour), "suggested": st.suggested_reason,
        })
        if st.pending_approval and st.approval_buttons and mode != "off":
            decisions.append({"key": st.key, "name": name, "kind": "approval", "text": st.pending_approval[:600],
                              "buttons": list(st.approval_buttons)})
        for p in (needs if mode != "off" else []):
            decisions.append({"key": st.key, "name": name, "kind": "info", "text": p, "buttons": []})
        for t, kind, text in sess.recent_records(15):
            feed.append({"time": t, "name": name, "kind": kind, "text": text, "avatar": _APP_AVATAR.get(st.app, "🤖")})
        for h in sess.history_on(today):
            k, a = h.get("kind"), h.get("action")
            if k == "action" and "Invoke" in str(h.get("text", "")):
                totals["auto_clicks"] += 1
            elif k == "action" and a == "send" and h.get("by") == "assistant":
                totals["auto_sends"] += 1
            elif k == "action" and h.get("by") == "user":
                totals["user_actions"] += 1
            elif k == "verify":
                totals["verifies"] += 1
            elif k == "ask_human":
                totals["asks"] += 1
    agents.sort(key=lambda a: (0 if (a["needs_you"] or a["paused"]) and a["mode"] != "off" else 1,
                               _MODE_ORDER.get(a["mode"], 3), a["name"]))
    feed.sort(key=lambda f: f["time"], reverse=True)
    decisions.sort(key=lambda d: 0 if d["kind"] == "approval" else 1)
    return {"time": now.strftime("%H:%M:%S"), "running": running, "agents": agents, "feed": feed[:40],
            "decisions": decisions, "usage": {"today": totals, "reads": dict(usage or {}), "models": call_counts()}}


class Dashboard:
    def __init__(self, worker, cfg, port: int = 0, running: Callable[[], bool] = lambda: True,
                 on_mode: Optional[Callable[[str, str], None]] = None,
                 on_running: Optional[Callable[[bool], None]] = None,
                 list_windows: Optional[Callable[[], list]] = None) -> None:
        self.worker = worker
        self.cfg = cfg
        self.running = running
        if list_windows is None:
            detector = getattr(worker, "detector", None)
            list_windows = detector.detect if detector is not None else (lambda: [])
        self.office = OfficeBridge(worker.store, cfg, list_windows, worker, on_mode, on_running, running)
        self.token = secrets.token_urlsafe(18)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler_class())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> "Dashboard":
        self._thread = threading.Thread(target=self._server.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def office_snapshot(self) -> Dict[str, Any]:
        usage = build_snapshot(self.worker.store, self.cfg.mode_of, getattr(self.worker, "usage", {}),
                               self.cfg.autopilot_max_per_hour, self.running())["usage"]
        return self.office.snapshot(usage)

    def snapshot(self) -> Dict[str, Any]:
        return build_snapshot(self.worker.store, self.cfg.mode_of, getattr(self.worker, "usage", {}),
                              self.cfg.autopilot_max_per_hour, self.running())

    def decide(self, key: str, button: str) -> Dict[str, Any]:
        """網頁上的〔允許一次〕〔拒絕〕：只接受該工作階段目前列出的按鈕，交給背景執行緒用同一套規則代按。"""
        for sess in self.worker.store.all():
            st = sess.load()
            if st.key != key:
                continue
            if contains_assistant(st.path, st.remote) or self.cfg.mode_of(key) == "off":
                return {"ok": False, "text": "這個視窗的模式是「關閉」，助理不會操作它"}
            if button not in st.approval_buttons:
                return {"ok": False, "text": f"「{button}」不是代理目前在等的按鈕（可能已經處理過了）"}
            self.worker.request_click(key, button)
            sess.history("web_decide", button=button, by="user")
            return {"ok": True, "text": f"已排入：代按「{button}」"}
        return {"ok": False, "text": "找不到這個工作階段"}

    def _handler_class(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AIEngineeringAssistant"

            def log_message(self, *_a) -> None:  # 不在主控台印出每個請求
                pass

            def _host_ok(self) -> bool:
                return self.headers.get("Host", "") in (f"127.0.0.1:{dash.port}", f"localhost:{dash.port}")

            def _cookie_token(self) -> str:
                for part in self.headers.get("Cookie", "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "aea_t":
                        return v
                return ""

            def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None,
                      csp: str = _CSP_STRICT, cache: str = "no-store") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", csp)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            def do_GET(self) -> None:
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                u = urlparse(self.path)
                qt = (parse_qs(u.query).get("t") or [""])[0]
                if u.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                if u.path == "/" and qt and hmac.compare_digest(qt, dash.token):
                    # 第一次用帶存取碼的網址打開：換成 HttpOnly cookie，再導向乾淨的網址
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"aea_t={dash.token}; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not hmac.compare_digest(self._cookie_token(), dash.token):
                    return self._send(401, "請從 AI 工程助理主視窗的「🌐」按鈕打開儀表板".encode("utf-8"),
                                      "text/plain; charset=utf-8")
                if u.path == "/classic":
                    self.send_response(301)
                    self.send_header("Location", "/classic/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if u.path == "/api/watcher/state":
                    return self._json(200, dash.office_snapshot())
                if u.path == "/api/watcher/stream":
                    return self._sse(dash.office_snapshot)
                if u.path in _STATIC:
                    fname, ctype = _STATIC[u.path]
                    try:
                        with open(os.path.join(WEB_DIR, fname), "rb") as f:
                            body = f.read()
                    except OSError:
                        return self._send(404, b"not found", "text/plain")
                    if fname == "index.html":
                        body = body.replace(b"__TOKEN__", dash.token.encode("ascii"))
                    return self._send(200, body, ctype)
                if u.path == "/api/state":
                    return self._json(200, dash.snapshot())
                if u.path == "/api/events":
                    return self._sse(dash.snapshot)
                full = pixel_file(u.path)
                if full is not None:
                    with open(full, "rb") as f:
                        body = f.read()
                    ext = os.path.splitext(full)[1].lower()
                    if ext == ".html":
                        body = body.replace(b"__TOKEN__", dash.token.encode("ascii"))
                    # _next/static 的檔名含雜湊，可以長期快取；頁面本身不快取
                    cache = "public, max-age=31536000, immutable" if "/_next/static/" in u.path else "no-store"
                    return self._send(200, body, _MIME.get(ext, "application/octet-stream"), csp=_CSP_PIXEL, cache=cache)
                return self._send(404, b"not found", "text/plain")

            def _sse(self, source) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last, beat = "", time.monotonic()
                try:
                    while True:
                        data = json.dumps(source(), ensure_ascii=False)
                        h = hashlib.sha1(data.encode("utf-8")).hexdigest()
                        if h != last:
                            last = h
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            beat = time.monotonic()
                        elif time.monotonic() - beat > 15:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            beat = time.monotonic()
                        time.sleep(2.0)
                except (BrokenPipeError, ConnectionError, OSError):
                    return

            def do_POST(self) -> None:
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                if not hmac.compare_digest(self.headers.get("X-Token", ""), dash.token):
                    return self._json(401, {"ok": False, "text": "存取碼不對"})
                path = urlparse(self.path).path
                if path not in ("/api/decide", "/api/watcher/assist", "/api/office/mode", "/api/office/running"):
                    return self._send(404, b"not found", "text/plain")
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = -1
                if not 0 < n <= 4096:
                    return self._json(400, {"ok": False, "text": "請求太大或是空的"})
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise TypeError
                    if path == "/api/decide":
                        result = dash.decide(str(body["key"]), str(body["button"]))
                    elif path == "/api/watcher/assist":
                        result = dash.office.assist(str(body["workerId"]), str(body.get("actionLabel") or ""),
                                                    str(body.get("message") or ""))
                    elif path == "/api/office/mode":
                        result = dash.office.set_mode(str(body["workerId"]), str(body["mode"]))
                    else:
                        result = dash.office.set_running(bool(body["running"]))
                except (ValueError, KeyError, TypeError):
                    return self._json(400, {"ok": False, "text": "格式不對"})
                if path != "/api/decide":
                    result["snapshot"] = dash.office_snapshot()
                return self._json(200, result)

        return Handler
