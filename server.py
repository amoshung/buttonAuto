"""AI 工程助理：網頁版後端（取代 Tk 浮動面板）。

執行：restart.bat（會先關掉舊的助理），或 `python server.py --open`。

- 主畫面是多 CLI 辦公室（team/office.py）：每個專案一間房，代理在獨立 worktree 工作，等你的事集中在收件匣。
- 本檔只負責「殼」：設定、事件轉成網頁可讀的狀態、REST API（/api/team/*）、SSE 即時推送。
- 舊的 IDE 視窗自動化（UIA 輪巡、F8 熱鍵、圖片比對、Next.js 像素辦公室）已停用，相關模組留在磁碟上但不再被引用。
- 只綁 127.0.0.1。存取碼存在 web_token.local（重開不變，書籤與 cookie 可以繼續用）；
  GET 用 cookie 或 ?t=；會動手的 POST 一律要 X-Token 標頭（cookie 不算，防跨站請求）；Host 必須是本機（防 DNS rebinding）。
- 網頁只用 textContent 放資料；CSP 不允許內嵌腳本。
"""
import argparse
import collections
import datetime
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import asdict
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CONSOLE_DIR = os.path.join(WEB_DIR, "console")
PIXEL_DIR = os.path.join(WEB_DIR, "pixel")
RUNTIME_PATH = os.path.join(BASE_DIR, "runtime.local.json")
TOKEN_PATH = os.path.join(BASE_DIR, "web_token.local")
ERROR_LOG = os.path.join(BASE_DIR, "error.log")
SERVER_LOG = os.path.join(BASE_DIR, "server.log")
APP_NAME = "AI 工程助理"

if sys.stdout is None or sys.stderr is None:
    try:
        _log_fh = open(SERVER_LOG, "a", encoding="utf-8", errors="replace", buffering=1)
        if sys.stdout is None:
            sys.stdout = _log_fh
        if sys.stderr is None:
            sys.stderr = _log_fh
    except OSError:
        pass

from ai_settings import AISettings, DEFAULT_SECRETS_PATH  # noqa: E402
from automation import AutomationWorker, check_deny  # noqa: E402
from config import DEFAULT_CONFIG_PATH, AppConfig  # noqa: E402
from projects import ProjectDetector  # noqa: E402
from sessions import now_str  # noqa: E402

_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
         ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
         ".md": "text/markdown; charset=utf-8"}
CSP_STRICT = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
              "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


# ---------------------------------------------------------------- 小工具

def code_version() -> str:
    try:
        p = subprocess.run(["git", "-C", BASE_DIR, "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                           timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except Exception:
        pass
    return "dev"


def code_mtime() -> float:
    """程式與網頁檔案最新的修改時間（偵測「磁碟上的程式比執行中的新」）。"""
    newest = 0.0
    for d in (BASE_DIR, PIXEL_DIR):
        try:
            for f in os.listdir(d):
                if f.endswith((".py", ".js", ".css", ".html")):
                    newest = max(newest, os.path.getmtime(os.path.join(d, f)))
        except OSError:
            continue
    return newest


def load_or_create_token(path: str = TOKEN_PATH) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            tok = f.read().strip()
        if re.fullmatch(r"[A-Za-z0-9_\-]{20,}", tok):
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(tok)
    except OSError:
        pass
    return tok


def safe_child(root: str, rel: str) -> Optional[str]:
    """root 底下的檔案（擋掉 ../ 與絕對路徑）；不存在回傳 None。"""
    full = os.path.realpath(os.path.join(root, rel.lstrip("/\\")))
    base = os.path.realpath(root)
    if not (full == base or full.startswith(base + os.sep)) or not os.path.isfile(full):
        return None
    return full


def open_in_os(path: str) -> bool:
    try:
        if not os.path.splitext(path)[1]:
            os.makedirs(path, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(path)  # type: ignore[attr-defined]
            return True
    except OSError:
        pass
    return False


def _short(text: str, n: int) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _hour_count(times: List[str], now: datetime.datetime) -> int:
    cut = (now - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for t in times if t >= cut)


# ---------------------------------------------------------------- 事件中心

class TeamError(Exception):
    """/api/team/* 的可預期錯誤：狀態碼＋中文訊息（不洩漏堆疊）。"""

    def __init__(self, code: int, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


LOCAL_PATH_TEXT = "只接受本機資料夾的完整路徑"


def local_path_problem(raw: Any) -> str:
    """路徑必須是本機完整路徑；UNC、網址、混用斜線、相對路徑都擋下。要在任何 isdir／git 之前呼叫。"""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1024 or "\x00" in raw:
        return LOCAL_PATH_TEXT
    raw = raw.strip()
    if "://" in raw:
        return LOCAL_PATH_TEXT
    for form in (raw, raw.replace("/", "\\")):
        if form.startswith("\\\\") or form[:2] in ("//", "/\\", "\\/"):
            return LOCAL_PATH_TEXT
    if not os.path.isabs(raw):
        return LOCAL_PATH_TEXT
    drive = os.path.splitdrive(os.path.abspath(raw))[0]
    if drive.startswith("\\\\") or drive.startswith("//"):
        return LOCAL_PATH_TEXT
    return ""


class Hub:
    """背景執行緒的事件 → 網頁要顯示的狀態。版本號一變，SSE 就推送新的快照。"""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.version = 0
        self.logs: Deque[Dict[str, Any]] = collections.deque(maxlen=800)
        self.toasts: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        self._seq = 0
        self.session_status: Dict[str, str] = {}
        self.humans: List[Dict[str, Any]] = []
        self.status_text = ""
        self.count = 0
        self.summary_path = ""

    def bump(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def log(self, text: str, level: str = "") -> None:
        with self.cond:
            self.logs.append({"id": self._next(), "text": text, "level": level or classify_log(text),
                              "at": time.time()})
            self.version += 1
            self.cond.notify_all()

    def toast(self, text: str, ok: bool = True, key: str = "") -> None:
        with self.cond:
            self.toasts.append({"id": self._next(), "text": text, "ok": ok, "key": key, "at": time.time()})
            self.version += 1
            self.cond.notify_all()

    def logs_after(self, after: int) -> List[Dict[str, Any]]:
        with self.cond:
            return [x for x in self.logs if x["id"] > after]

    def toasts_after(self, after: int) -> List[Dict[str, Any]]:
        with self.cond:
            return [x for x in self.toasts if x["id"] > after]

    def wait(self, version: int, timeout: float) -> int:
        with self.cond:
            if self.version == version:
                self.cond.wait(timeout)
            return self.version


_LOG_LEVELS = (("🙋", "human"), ("⛔", "error"), ("⚠", "warn"), ("🖱", "action"), ("⌨", "action"), ("🧠", "think"),
               ("👁", "think"), ("✍", "think"), ("💡", "suggest"), ("🎯", "suggest"), ("🆕", "suggest"),
               ("⏸", "warn"), ("🔄", "action"), ("🔓", "action"), ("✅", "ok"), ("📝", "ok"), ("🔔", "info"))


def classify_log(text: str) -> str:
    body = re.sub(r"^\[\d\d:\d\d:\d\d\]\s*", "", text or "")
    for mark, level in _LOG_LEVELS:
        if body.startswith(mark):
            return level
    return "info"


# ---------------------------------------------------------------- 主程式

class AssistantServer:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH, secrets_path: str = DEFAULT_SECRETS_PATH,
                 worker: Optional[AutomationWorker] = None, detector: Optional[ProjectDetector] = None,
                 start_threads: bool = True, token: Optional[str] = None, team_root: Optional[str] = None,
                 team_base_dir: Optional[str] = None, team_opts: Optional[Dict[str, Any]] = None) -> None:
        self.config_path = config_path
        self._team_root = team_root or os.path.join(BASE_DIR, "sessions", "team")
        self._team_base_dir = team_base_dir or BASE_DIR
        self._team_opts = dict(team_opts or {})
        self._team: Optional[Any] = None
        self._team_lock = threading.Lock()
        self._team_snap_lock = threading.Lock()
        self._team_snap: Tuple[int, float, Optional[Dict[str, Any]]] = (-1, 0.0, None)
        self._team_errs: set = set()
        self.secrets_path = secrets_path
        self.cfg = AppConfig.load(config_path)
        self.ai_settings = AISettings.load(secrets_path)
        self.hub = Hub()
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        # 舊的 AutomationWorker 不再建立：它的建構子會掛全域鍵盤／滑鼠攔截（InputMonitor）。只有呼叫端明確傳入時才保留。
        self.worker = worker
        if worker is not None:
            self.events = worker.events
        self.detector = detector
        self.token = token or load_or_create_token()
        self.version = code_version()
        self.started_at = time.time()
        self.code_mtime_at_start = code_mtime()
        self.cfg_lock = threading.RLock()
        self._stop = threading.Event()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.port = 0
        # 舊的 IDE 視窗自動化（UIA 輪巡、F8 熱鍵、圖片比對、像素辦公室橋接）不再啟動；模組留在磁碟上但沒有被引用。
        if start_threads:
            self.team.start()
            for target, name in ((self._pump, "event-pump"), (self._watch_code, "code-watch")):
                threading.Thread(target=target, name=name, daemon=True).start()

    # ---- 背景迴圈
    def _pump(self) -> None:
        """AutomationWorker 的事件 → Hub。"""
        while not self._stop.is_set():
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._apply_event(ev)
            except Exception as e:  # 不讓單一事件中斷轉送
                self.hub.log(f"⚠ 事件處理失敗：{e!r}")

    def _apply_event(self, ev: Dict[str, Any]) -> None:
        kind = ev.get("type")
        hub = self.hub
        if kind == "log":
            hub.log(str(ev.get("text", "")))
            try:
                print(ev.get("text", ""), flush=True)
            except (OSError, ValueError, AttributeError):
                pass  # 沒有主控台（pythonw）或編碼不支援：網頁與 automation.log 仍有紀錄
        elif kind == "count":
            hub.count = int(ev.get("value", 0))
            hub.bump()
        elif kind == "status":
            hub.status_text = str(ev.get("text", ""))
            hub.bump()
        elif kind == "session":
            hub.session_status[str(ev.get("key", ""))] = str(ev.get("text", ""))
            hub.bump()
        elif kind == "humans":
            hub.humans = list(ev.get("items", []))
            hub.bump()
        elif kind == "send_result":
            hub.toast(("✅ " if ev.get("ok") else "⛔ ") + str(ev.get("text", "")), bool(ev.get("ok")),
                      str(ev.get("key", "")))
        elif kind == "summary":
            hub.summary_path = str(ev.get("path", ""))
            hub.toast(f"📝 每日摘要已產生：{os.path.basename(hub.summary_path)}", True)
        elif kind == "modes_changed":
            hub.bump()
        elif kind in ("web_mode", "web_running"):
            pass
        else:
            hub.bump()

    # ---- 多 CLI 辦公室（team）
    @property
    def team(self):
        """Office 延後建立（第一次用到才建；不啟動執行緒），避免單純建立伺服器就讀設定檔、建資料夾。"""
        with self._team_lock:
            if self._team is None:
                self._team = self._build_team()
            return self._team

    def _build_team(self):
        from team.fakes.fake_models import ScriptedRouter
        from team.models import ModelRouter
        from team.office import Office
        from team.rooms import RoomStore
        holder: Dict[str, Any] = {}

        def router(demo: bool):
            if demo:
                return ScriptedRouter()  # 示範房不連網
            return ModelRouter(holder["office"].model_cfg)

        def num(key: str, default: float):
            def fn() -> float:
                try:
                    return float(getattr(self.cfg, key))
                except (TypeError, ValueError):
                    return default
            return fn
        kw: Dict[str, Any] = {"base_dir": self._team_base_dir,
                              "plugin_dirs_fn": lambda: self.cfg.team_plugin_list(),
                              "cost_weight_fn": num("team_cost_weight", 0.3),
                              "context_rollover_fn": num("team_context_rollover", 0.70)}
        kw.update(self._team_opts)
        clis_fn = kw.pop("clis_fn", None)
        args: List[Any] = [RoomStore(self._team_root), router, lambda: list(self.cfg.deny_patterns),
                           lambda: int(num("team_max_running", 4.0)()), self.hub.bump]
        if clis_fn is not None:
            args.append(clis_fn)
        office = Office(*args, **kw)
        holder["office"] = office
        return office

    def team_snapshot(self) -> Dict[str, Any]:
        """依 Hub 版本號快取（TTL 1 秒），多個分頁共用；出錯不快取。回傳的是共用物件，呼叫端不可修改。"""
        with self._team_snap_lock:
            ver, at, snap = self._team_snap
            if snap is not None and ver == self.hub.version and time.monotonic() - at < 1.0:
                return snap
            ver = self.hub.version
            t = self.team
            snap = t.snapshot()
            usage = dict(snap.get("usage") or {})
            usage["models_ready"] = t.model_cfg is not None
            snap["usage"] = usage
            self._team_snap = (ver, time.monotonic(), snap)
            return snap

    def team_known_repos(self) -> List[str]:
        out: List[str] = []
        for r in self.team.store.list(include_archived=True):
            if r.repo and r.repo not in out and not r.demo:
                out.append(r.repo)
        return out

    def team_get(self, path: str, q: Dict[str, List[str]]) -> Optional[Dict[str, Any]]:
        """GET /api/team/*；錯誤以 TeamError 丟出（帶狀態碼與中文訊息）。"""
        arg = lambda k: (q.get(k) or [""])[0]  # noqa: E731
        t = self.team
        if path == "/api/team/state":
            return self.team_snapshot()
        if path == "/api/team/projects":
            from team.scan import known_projects
            return {"projects": known_projects(self.team_known_repos())}
        if path == "/api/team/ssh/hosts":
            return t.ssh_hosts_view()
        if path == "/api/team/scan":
            from team.scan import scan_project
            raw = arg("path").strip()
            ssh_host = arg("ssh_host").strip()
            if not raw:
                raise TeamError(400, "請提供專案資料夾路徑")
            if ssh_host:
                try:
                    return t.remote_scan_project(ssh_host, raw)
                except Exception as e:
                    raise TeamError(400, str(e))
            bad = local_path_problem(raw)
            if bad:
                raise TeamError(400, bad)
            if not os.path.isdir(raw):
                raise TeamError(400, "找不到資料夾：%s" % raw)
            return scan_project(raw)
        if path == "/api/team/dirs":
            raw = arg("path").strip()
            ssh_host = arg("ssh_host").strip()
            if ssh_host:
                try:
                    return t.remote_browse_dirs(ssh_host, raw)
                except Exception as e:
                    raise TeamError(400, str(e))
            drives = []
            if os.name == "nt":
                for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    dp = f"{d}:\\"
                    if os.path.isdir(dp):
                        drives.append(dp)
            cur = os.path.abspath(raw) if raw and os.path.isdir(raw) else os.getcwd()
            parent = os.path.dirname(cur.rstrip("/\\"))
            if parent == cur:
                parent = ""
            skip = {".git", ".ai-team-worktrees", ".ai-team-mutation", "node_modules", "__pycache__", ".venv", "venv"}
            dirs = []
            try:
                for entry in sorted(os.scandir(cur), key=lambda e: e.name.lower()):
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".") and entry.name not in skip:
                        dirs.append({"name": entry.name, "path": entry.path})
            except OSError:
                pass
            return {"path": cur, "parent": parent, "dirs": dirs[:120], "drives": drives}
        if path == "/api/team/room":
            room = self._team_room(arg("id"))
            # 最近 100 筆、不含 detail（大）；細節用 /api/team/room/events?id=&ids= 依事件 id 讀
            from team.rooms import slim_event
            from team import ce_sync
            sh = (getattr(room, "ssh_host", "") or "").strip()
            ce_info = (
                ce_sync.sync_remote_ce_plan(
                    sh,
                    room.repo,
                    reference_root=room.reference_root,
                    target_subdir=room.target_subdir,
                    target_subdirs=list(getattr(room, "target_subdirs", None) or ()),
                    plan_rel=getattr(room, "selected_plan_rel", "") or "",
                )
                if sh and room.repo
                else (
                    ce_sync.sync_room_ce_plan(
                        room.repo,
                        reference_root=room.reference_root,
                        plan_rel=getattr(room, "selected_plan_rel", "") or "",
                    )
                    if room.repo
                    else {}
                )
            )
            wiki_info = t.kb.wiki_view(room.repo, reference_root=room.reference_root, sync=False) if room.repo and not room.demo else {}
            progress_info = t.get_room_progress(room.id)
            return {"room": asdict(room), "events": [slim_event(e) for e in reversed(t.store.events(room.id, limit=100))],
                    "inbox": [asdict(i) for i in t.store.inbox() if i.room_id == room.id],
                    "ce": ce_info, "wiki": wiki_info, "progress": progress_info}
        if path == "/api/team/room/progress":
            room = self._team_room(arg("id"))
            return {"ok": True, "progress": t.get_room_progress(room.id)}
        if path == "/api/team/room/trace":
            room = self._team_room(arg("id"))
            try:
                limit = int(arg("limit") or "200")
            except ValueError:
                limit = 200
            from engine.trace import RoomTracer

            tracer = RoomTracer(t.store._dir(room.id), room.id)
            return {"ok": True, "room_id": room.id, "path": tracer.path, "entries": tracer.read(limit=limit)}
        if path == "/api/team/wiki":
            from team import ce_sync
            rid = arg("room").strip()
            repo = arg("repo").strip()
            ref_root = ""
            t_sub = ""
            t_subs: List[str] = []
            if rid:
                room = self._team_room(rid)
                repo = room.repo
                ref_root = room.reference_root
                t_sub = room.target_subdir
                t_subs = list(getattr(room, "target_subdirs", None) or [])
            if not repo:
                raise TeamError(400, "請提供 room 或 repo")
            return {
                "wiki": t.kb.wiki_view(repo, reference_root=ref_root, target_subdir=t_sub, target_subdirs=t_subs),
                "ce": ce_sync.sync_room_ce_plan(repo, reference_root=ref_root),
            }
        if path == "/api/team/room/events":
            room = self._team_room(arg("id"))
            ids = [x.strip() for x in arg("ids").split(",") if x.strip()][:20]
            if not ids:
                raise TeamError(400, "請提供事件 id（ids）")
            return {"events": t.store.events_by_id(room.id, ids)}
        if path == "/api/team/fleet":
            return {"fleet": self.team_snapshot()["fleet"], "catalog": t.catalog}
        if path == "/api/team/usage":
            summary = t.usage_summary()
            self._enforce_daily_token_budget(summary)
            return summary
        if path == "/api/team/preflight":
            from team import bootstrap
            return bootstrap.run_preflight_check(self._team_base_dir)
        if path == "/api/team/budget":
            summary = t.usage_summary()
            today_tokens = self._today_total_tokens(summary)
            limit = int(getattr(self.cfg, "team_daily_token_limit", 0) or 0)
            exceeded = self._enforce_daily_token_budget(summary)
            rooms = t.store.list() if hasattr(t, "store") else []
            running_rooms = sum(1 for r in rooms if getattr(r, "status", "") in ("running", "working", "building"))
            return {
                "ok": True,
                "daily_token_limit": limit,
                "today_tokens": today_tokens,
                "exceeded": exceeded,
                "running_rooms": running_rooms,
                "total_rooms": len(rooms),
            }
        if path == "/api/team/profile/export":
            from team import bootstrap
            return bootstrap.export_safe_profile(self.cfg, self._team_base_dir)
        if path == "/api/team/models":
            return t.models_view(ssh_host=arg("ssh_host").strip())
        if path == "/api/team/legacy_docs":
            from team import notes
            dirs = self.cfg.team_legacy_doc_list()
            repos = []
            for r in self.team_known_repos():
                found = notes.legacy_in_repo(r, dirs)
                if found:
                    repos.append({"repo": r, "dirs": found})
            return {"repos": repos}
        if path == "/api/team/weather":
            svc = self._get_weather_service()
            try:
                return svc.get_weather(
                    lat=arg("lat").strip() or None,
                    lon=arg("lon").strip() or None,
                    city=arg("city").strip(),
                    force_refresh=arg("refresh").strip() in ("1", "true", "yes"),
                )
            except ValueError as e:
                raise TeamError(400, str(e))
        if path == "/api/team/catalog":
            cat = self._get_model_catalog()
            return {"ok": True, **cat.snapshot()}
        if path == "/api/team/prompts":
            rid = arg("room").strip()
            if rid:
                self._team_room(rid)
            return t.prompts.view(rid)
        if path == "/api/team/prompts/preview":
            from team import personas
            role = arg("role").strip()
            rid = arg("room").strip()
            if role not in t.prompts.view()["roles"]:
                raise TeamError(400, "不認得的角色：%s" % role)
            room = self._team_room(rid) if rid else None
            comp = personas.Composer(
                t.prompts,
                room.id if room else "",
                notes_dir=(room.notes_dir if room and room.notes_dir else "<notes_dir>"),
            )
            p = comp.preview(role, room)
            return {"ok": True, "role": role, "version": p.version, "text": p.text}
        return None

    def _get_weather_service(self):
        svc = getattr(self, "_weather_service", None)
        if svc is None:
            from team.weather import WeatherService
            svc = WeatherService()
            self._weather_service = svc
        return svc

    def _get_model_catalog(self):
        cat = getattr(self, "_model_catalog", None)
        if cat is None:
            from team.model_catalog import ModelCatalogService
            cat = ModelCatalogService(self._team_base_dir)
            cat.start_daily_scheduler()
            self._model_catalog = cat
        return cat

    def _team_room(self, room_id: str):
        try:
            room = self.team.store.get(room_id)
        except ValueError:
            raise TeamError(400, "房間 id 格式不對")
        if room is None:
            raise TeamError(404, "找不到房間：%s" % room_id)
        return room

    @staticmethod
    def _need(body: Dict[str, Any], key: str) -> str:
        v = body.get(key)
        if not isinstance(v, str) or not v.strip():
            raise TeamError(400, "缺少欄位：%s" % key)
        return v

    def team_post(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST /api/team/*。業務上的拒絕回 {ok: false, text}（200）；格式錯 400、找不到 404。"""
        t = self.team
        if path == "/api/team/ssh/hosts":
            action = str(body.get("action") or "add").strip().lower()
            target = self._need(body, "target").strip()
            try:
                if action in ("remove", "delete"):
                    return t.remove_ssh_host(target)
                return t.add_ssh_host(target, label=str(body.get("label") or "").strip())
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/ssh/probe":
            ssh_host = str(body.get("ssh_host") or "").strip()
            force = bool(body.get("force", True))
            password = str(body.get("password") or "")
            install_key = bool(body.get("install_key", False))
            quick_auth = bool(body.get("quick_auth", False))
            try:
                return t.probe_ssh_host(
                    ssh_host,
                    force=force,
                    password=password,
                    install_key=install_key,
                    quick_auth=quick_auth,
                )
            except Exception as e:
                from team.ssh import is_ssh_auth_error
                return {"ok": False, "ssh_host": ssh_host, "text": str(e), "error": str(e),
                        "auth_required": is_ssh_auth_error(str(e)),
                        "clis": {}, "available_seats": [], "seats_models": {}}
        if path == "/api/team/rooms":
            paths = body.get("allowed_paths", [])
            if not isinstance(paths, list) or not all(isinstance(x, str) for x in paths):
                raise TeamError(400, "allowed_paths 必須是字串清單")
            target_subdirs = body.get("target_subdirs", None)
            if target_subdirs is not None and (not isinstance(target_subdirs, list) or not all(isinstance(x, str) for x in target_subdirs)):
                raise TeamError(400, "target_subdirs 必須是字串清單")
            for k in ("repo", "goal", "source", "impl_cli", "review_cli", "autonomy", "name", "impl_model",
                      "review_model", "target_subdir", "ssh_host", "reference_root", "supervisor_cli", "supervisor_model",
                      "task_mode", "engine_version"):
                if k in body and body[k] is not None and not isinstance(body[k], str):
                    raise TeamError(400, "欄位 %s 必須是字串" % k)
            role_prefs = body.get("role_prefs", None)
            if role_prefs is not None and not isinstance(role_prefs, dict):
                raise TeamError(400, "role_prefs 必須是物件")
            ssh_host = str(body.get("ssh_host") or "").strip()
            if not ssh_host:
                if body.get("repo", "").strip():
                    bad = local_path_problem(body["repo"])
                    if bad:
                        raise TeamError(400, bad)
                if body.get("reference_root", "") and str(body.get("reference_root")).strip():
                    bad_ref = local_path_problem(str(body["reference_root"]).strip())
                    if bad_ref:
                        raise TeamError(400, bad_ref)
                if target_subdirs:
                    for sub_p in target_subdirs:
                        if os.path.isabs(sub_p.strip()):
                            bad_sub = local_path_problem(sub_p.strip())
                            if bad_sub:
                                raise TeamError(400, bad_sub)
            try:
                room = t.create_room(repo=body.get("repo", ""), goal=body.get("goal", ""),
                                     source=body.get("source", "goal") or "goal",
                                     impl_cli=body.get("impl_cli", "auto") or "auto",
                                     review_cli=body.get("review_cli", "auto") or "auto",
                                     autonomy=body.get("autonomy", "balanced") or "balanced",
                                     allowed_paths=paths, name=body.get("name", ""),
                                     impl_model=body.get("impl_model", ""), review_model=body.get("review_model", ""),
                                     target_subdir=body.get("target_subdir", "") or "",
                                     target_subdirs=target_subdirs,
                                     reference_root=str(body.get("reference_root") or "").strip(),
                                     supervisor_cli=str(body.get("supervisor_cli") or "auto").strip() or "auto",
                                     supervisor_model=str(body.get("supervisor_model") or "").strip(),
                                     role_prefs=role_prefs,
                                     ssh_host=ssh_host,
                                     task_mode=str(body.get("task_mode") or "standard").strip() or "standard",
                                     d1_fast_review=bool(body.get("d1_fast_review", True)),
                                     use_worktree=bool(body.get("use_worktree", False)),
                                     engine_version=str(body.get("engine_version") or "v2").strip() or "v2")
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "room": asdict(room)}
        if path == "/api/team/rooms/model":
            room = self._team_room(self._need(body, "id"))
            for k in ("role", "seat", "model"):
                if k in body and not isinstance(body[k], str):
                    raise TeamError(400, "欄位 %s 必須是字串" % k)
            try:
                room = t.set_room_model(room.id, self._need(body, "role"), body.get("seat", "") or "",
                                        body.get("model", "") or "")
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "text": "已更新，下一回合生效", "room": asdict(room)}
        if path == "/api/team/rooms/delegate":
            room = self._team_room(self._need(body, "id"))
            if not isinstance(body.get("on"), bool):
                raise TeamError(400, "欄位 on 必須是 true 或 false")
            try:
                room = t.set_delegate(room.id, body["on"])
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "text": "已開啟代管" if room.delegate else "已關閉代管", "room": asdict(room)}
        if path == "/api/team/rooms/goal":
            room = self._team_room(self._need(body, "id"))
            goal = body.get("goal", "")
            if not isinstance(goal, str):
                raise TeamError(400, "欄位 goal 必須是字串")
            try:
                room = t.set_room_goal(room.id, goal)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "text": "已更新房間目標" if room.goal else "已清空房間目標", "room": asdict(room)}
        if path == "/api/team/rooms/folders":
            room = self._team_room(self._need(body, "id"))
            repo = str(body.get("repo") or "")
            reference_root = str(body.get("reference_root") or "")
            target_subdir = str(body.get("target_subdir") or "")
            target_subdirs = body.get("target_subdirs", None)
            if target_subdirs is not None and (not isinstance(target_subdirs, list) or not all(isinstance(x, str) for x in target_subdirs)):
                raise TeamError(400, "target_subdirs 必須是字串清單")
            try:
                room = t.set_room_folders(
                    room.id,
                    repo=repo,
                    reference_root=reference_root,
                    target_subdir=target_subdir,
                    target_subdirs=target_subdirs,
                )
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {
                "ok": True,
                "text": "已儲存目標工作資料夾與唯讀參考範圍設定",
                "room": asdict(room),
            }
        if path == "/api/team/rooms/progress":
            room = self._team_room(self._need(body, "id"))
            note = str(body.get("note") or "")
            prog = t.sync_room_progress(room.id, extra_note=note)
            return {"ok": True, "text": "已更新房間 progress.md 斷點紀錄", "progress": prog}
        if path == "/api/team/rooms/sync_plan":
            room = self._team_room(self._need(body, "id"))
            plan_rel = str(body.get("plan_rel") or "")
            apply_goal = bool(body.get("apply_goal", False))
            try:
                res = t.sync_room_plan(room.id, plan_rel=plan_rel, apply_goal=apply_goal)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return res
        if path == "/api/team/rooms/plan_content":
            room = self._team_room(self._need(body, "id"))
            plan_rel = str(body.get("plan_rel") or getattr(room, "selected_plan_rel", "") or "").strip().replace("\\", "/")
            if not plan_rel or ".." in plan_rel.split("/") or plan_rel.startswith("/"):
                return {"ok": False, "text": "無效的計畫檔案路徑"}
            sh = (getattr(room, "ssh_host", "") or "").strip()
            base_dir = (getattr(room, "worktree", "") or getattr(room, "repo", "") or "").strip()
            if sh and base_dir:
                from team.ssh import remote_fs
                try:
                    res = remote_fs(sh, "read_file", base_dir, rel=plan_rel, timeout=10.0)
                    if isinstance(res, dict) and res.get("ok"):
                        return {"ok": True, "plan_rel": plan_rel, "content": str(res.get("content") or "")}
                    # fallback via remote_exec cat
                    from team.ssh import remote_exec
                    import shlex
                    r2 = remote_exec(sh, base_dir, f"cat {shlex.quote(plan_rel)}", shell=True, timeout=10.0)
                    if int(r2.get("exit_code", 1)) == 0:
                        return {"ok": True, "plan_rel": plan_rel, "content": str(r2.get("stdout") or "")}
                except Exception as e:
                    return {"ok": False, "text": f"讀取遠端計畫失敗：{e}"}
                return {"ok": False, "text": f"找不到遠端計畫：{plan_rel}"}
            if base_dir and os.path.isdir(base_dir):
                full = os.path.abspath(os.path.join(base_dir, plan_rel))
                if not full.startswith(os.path.abspath(base_dir)):
                    return {"ok": False, "text": "無效的計畫檔案路徑"}
                if os.path.isfile(full):
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        return {"ok": True, "plan_rel": plan_rel, "content": f.read()}
            return {"ok": False, "text": f"找不到計畫檔案：{plan_rel}"}
        if path == "/api/team/rooms/clear_plan":
            room = self._team_room(self._need(body, "id"))
            try:
                return t.clear_room_plan_and_progress(room.id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/rooms/clear_wiki":
            room = self._team_room(self._need(body, "id"))
            try:
                return t.clear_room_wiki(room.id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/rooms/generate_plan":
            room = self._team_room(self._need(body, "id"))
            goal = str(body.get("goal") or "")
            seat = str(body.get("seat") or "")
            model = str(body.get("model") or "")
            interactive = bool(body.get("interactive", False))
            try:
                return t.generate_room_plan_from_goal(
                    room.id, goal=goal, seat=seat, model=model, interactive=interactive
                )
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            except Exception as e:
                return {"ok": False, "text": f"根據目標生成計畫失敗：{e}"}
        if path == "/api/team/rooms/d1_fast_review":
            room = self._team_room(self._need(body, "id"))
            enabled = bool(body.get("enabled", True))
            return t.set_room_d1_fast_review(room.id, enabled)
        if path == "/api/team/rooms/unit/rerun":
            room = self._team_room(self._need(body, "id"))
            uid = str(body.get("unit_id") or "")
            try:
                return t.rerun_room_unit(room.id, uid)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/rooms/unit/waive":
            room = self._team_room(self._need(body, "id"))
            uid = str(body.get("unit_id") or "")
            reason = str(body.get("reason") or "")
            try:
                return t.waive_room_unit(room.id, uid, reason=reason)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/rooms/unit/edit":
            room = self._team_room(self._need(body, "id"))
            uid = str(body.get("unit_id") or "")
            check_cmd = body.get("check_cmd") if "check_cmd" in body else None
            text_val = body.get("text") if "text" in body else None
            try:
                return t.edit_room_unit(room.id, uid, check_cmd=check_cmd, text=text_val)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/rooms/brainstorm":
            room = self._team_room(self._need(body, "id"))
            messages = body.get("messages") or []
            if not isinstance(messages, list):
                raise TeamError(400, "欄位 messages 必須是陣列")
            mode = str(body.get("mode") or "chat")
            seat = str(body.get("seat") or "")
            model = str(body.get("model") or "")
            goal_draft = str(body.get("goal_draft") or "")
            try:
                return t.brainstorm_room(
                    room.id,
                    messages=messages,
                    mode=mode,
                    seat=seat,
                    model=model,
                    goal_draft=goal_draft,
                )
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            except Exception as e:
                return {"ok": False, "text": f"Brainstorm 執行失敗：{e}"}
        if path == "/api/team/rooms/brainstorm/save_plan":
            room = self._team_room(self._need(body, "id"))
            filename = str(body.get("filename") or "")
            content = str(body.get("content") or "")
            apply_goal = bool(body.get("apply_goal", True))
            try:
                return t.save_brainstorm_plan(
                    room.id,
                    filename=filename,
                    content=content,
                    apply_goal=apply_goal,
                )
            except ValueError as e:
                return {"ok": False, "text": str(e)}
        if path == "/api/team/rooms/reset":
            room = self._team_room(self._need(body, "id"))
            try:
                room = t.reset_room(room.id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "text": "已恢復專案至介入前狀態（分支與舊驗收條件已清除）", "room": asdict(room)}
        if path == "/api/team/rooms/handoff":
            room = self._team_room(self._need(body, "id"))
            new_goal = str(body.get("goal") or "")
            new_name = str(body.get("name") or "")
            try:
                new_room = t.handoff_to_new_room(room.id, new_goal=new_goal, name=new_name)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "text": "已封存舊房間並開啟新房間接續開發（過去紀錄已載入 LLM Wiki）", "room": asdict(new_room)}
        if path == "/api/team/models":
            return t.save_models(body)
        if path == "/api/team/models/detect":
            from team.modelprefs import DETECTABLE_SEATS
            seat = body.get("seat", "")
            if not isinstance(seat, str):
                raise TeamError(400, "欄位 seat 必須是字串")
            seats_arg = list(DETECTABLE_SEATS) if seat == "all" else ([seat] if seat else None)
            started = t.detect_models(seats_arg)
            if not started:
                view = t.models_view()["seats"]
                check_seats = seats_arg or DETECTABLE_SEATS
                busy = [s for s in check_seats if view.get(s, {}).get("detecting")]
                return {"ok": bool(busy), "started": [],
                        "text": "正在偵測中" if busy else "沒有可偵測的座位（需已安裝 Kiro／Antigravity／Claude CLI）"}
            return {"ok": True, "started": started, "text": "已開始偵測：%s（不花 token）" % "、".join(started)}
        if path.startswith("/api/team/rooms/"):
            action = path.rsplit("/", 1)[1]
            if action not in ("pause", "resume", "archive"):
                return None
            room = self._team_room(self._need(body, "id"))
            try:
                if action == "archive":
                    t.archive(room.id, force=bool(body.get("force", False)))
                else:
                    getattr(t, action)(room.id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "text": {"pause": "已暫停", "resume": "已排入繼續", "archive": "已封存（紀錄已存入 LLM Wiki）"}[action]}
        if path == "/api/team/pause_all":
            n, failed = 0, []
            for r in t.store.list():
                if r.status in ("running", "queued", "waiting_user"):
                    try:
                        t.pause(r.id)
                        n += 1
                    except ValueError as e:
                        failed.append("%s：%s" % (r.name, e))
            if failed:
                return {"ok": False, "paused": n, "text": "已暫停 %d 間；這些沒暫停：%s" % (n, "；".join(failed[:5]))}
            return {"ok": True, "paused": n, "text": "已暫停 %d 間房" % n if n else "沒有運作中或排隊的房間"}
        if path == "/api/team/send":
            room = self._team_room(self._need(body, "id"))
            text = body.get("text", "")
            if not isinstance(text, str):
                raise TeamError(400, "欄位 text 必須是字串")
            return t.send(room.id, text)
        if path == "/api/team/answer":
            item_id = self._need(body, "item_id")
            if not any(i.id == item_id for i in t.store.inbox(include_resolved=True)):
                raise TeamError(404, "找不到這個項目")
            ans, txt = body.get("answer"), body.get("text", "")
            if not isinstance(ans, str) or not isinstance(txt, str):
                raise TeamError(400, "欄位 answer、text 必須是字串")
            return t.answer(item_id, ans, txt)
        if path == "/api/team/demo":
            from team.demo import create_demo_room
            autonomy = str(body.get("autonomy", "bold") or "bold")
            try:
                room = create_demo_room(t, t.base_dir, autonomy=autonomy)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            return {"ok": True, "room": asdict(room)}
        if path in ("/api/team/fleet/probe", "/api/team/fleet/reset"):
            from team.fleet import SEATS
            seat = self._need(body, "seat")
            if seat not in SEATS:
                raise TeamError(400, "不認得的座位：%s（可選：%s）" % (seat, "、".join(SEATS)))
            if path.endswith("/probe"):
                started = t.probe_now(seat)
                return {"ok": True, "text": "已開始試探" if started else "這個座位正在試探中"}
            t.reset_seat(seat)
            return {"ok": True, "text": "已標為可用"}
        if path == "/api/team/legacy_docs/remove":
            from team import notes
            repo = self._need(body, "repo").strip()
            bad = local_path_problem(repo)
            if bad:
                raise TeamError(400, bad)
            if not os.path.isdir(repo):
                return {"ok": False, "text": "找不到資料夾：%s" % repo, "moved": []}
            moved = notes.remove_legacy_in_repo(repo, self.cfg.team_legacy_doc_list())
            text = ("已移除舊助理文件：%s" % "、".join(moved)) if moved else "沒有發現舊的助理文件"
            return {"ok": True, "text": text, "moved": moved}
        if path == "/api/team/prompts":
            role = self._need(body, "role").strip()
            scope = str(body.get("scope") or "global").strip()
            room_id = str(body.get("room_id") or "").strip()
            if scope == "room":
                self._team_room(room_id)
            sections = body.get("sections")
            if not isinstance(sections, dict):
                raise TeamError(400, "缺少 sections 物件")
            try:
                ver = t.prompts.save(role, sections, scope, room_id=room_id, note=str(body.get("note") or ""))
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            self.hub.bump()
            return {"ok": True, "version": ver, "text": "已儲存提示詞（%s）" % ver, "prompts": t.prompts.view(room_id)}
        if path == "/api/team/prompts/reset":
            role = self._need(body, "role").strip()
            scope = str(body.get("scope") or "global").strip()
            room_id = str(body.get("room_id") or "").strip()
            if scope == "room":
                self._team_room(room_id)
            try:
                t.prompts.reset(role, scope, room_id=room_id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            self.hub.bump()
            return {"ok": True, "text": "已回復預設", "prompts": t.prompts.view(room_id)}
        if path == "/api/team/prompts/restore":
            role = self._need(body, "role").strip()
            scope = str(body.get("scope") or "global").strip()
            version = self._need(body, "version").strip()
            room_id = str(body.get("room_id") or "").strip()
            if scope == "room":
                self._team_room(room_id)
            try:
                ver = t.prompts.restore(role, scope, version, room_id=room_id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            self.hub.bump()
            return {"ok": True, "version": ver, "text": "已回復到 %s" % ver, "prompts": t.prompts.view(room_id)}
        if path == "/api/team/prompts/promote":
            role = self._need(body, "role").strip()
            room_id = self._need(body, "room_id").strip()
            self._team_room(room_id)
            try:
                ver = t.prompts.promote(role, room_id)
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            self.hub.bump()
            return {"ok": True, "version": ver, "text": "已升為全域（%s）" % ver, "prompts": t.prompts.view(room_id)}
        if path == "/api/team/preflight/test":
            from team import bootstrap
            return bootstrap.test_model_endpoint(
                base_url=str(body.get("base_url") or ""),
                api_key=str(body.get("key") or body.get("api_key") or ""),
                model=str(body.get("glm_model") or body.get("model") or ""),
            )
        if path == "/api/team/preflight/save":
            from team import bootstrap
            from team.models import ModelRouter, load_model_config
            try:
                saved = bootstrap.save_model_config(self._team_base_dir, body)
            except ValueError as e:
                raise TeamError(400, str(e))
            new_cfg = load_model_config(os.path.join(self._team_base_dir, "litellm.local.json"))
            if new_cfg is not None:
                t.model_cfg = new_cfg
                if hasattr(t, "models") and hasattr(t.models, "cfg"):
                    t.models.cfg = new_cfg
                else:
                    t.models = ModelRouter(new_cfg)
            cat = self._get_model_catalog()
            threading.Thread(target=cat.scan_now, name="litellm-catalog-save-scan", daemon=True).start()
            self.hub.bump()
            return {**saved, "preflight": bootstrap.run_preflight_check(self._team_base_dir)}
        if path == "/api/team/catalog/scan":
            cat = self._get_model_catalog()
            snap = cat.scan_now()
            self.hub.bump()
            return {"ok": True, **snap}
        if path == "/api/team/budget":
            try:
                limit = int(body.get("daily_token_limit", 0))
            except (TypeError, ValueError):
                raise TeamError(400, "daily_token_limit 必須是整數")
            limit = max(0, min(100_000_000, limit))
            with self.cfg_lock:
                self.cfg.team_daily_token_limit = limit
                self.cfg.save(self.config_path)
            self.hub.bump()
            return {"ok": True, "daily_token_limit": limit}
        if path == "/api/team/profile/import":
            from team import bootstrap
            try:
                res = bootstrap.import_safe_profile(self.cfg, self.config_path, self._team_base_dir, body)
            except ValueError as e:
                raise TeamError(400, str(e))
            self.hub.bump()
            return res
        return None

    @staticmethod
    def _today_total_tokens(summary: Dict[str, Any]) -> int:
        today = summary.get("today") if isinstance(summary, dict) else None
        if isinstance(today, dict):
            if isinstance(today.get("total_tokens"), (int, float)):
                return int(today["total_tokens"])
            by_model = today.get("by_model")
            if isinstance(by_model, dict):
                return sum(int(v.get("total_tokens", 0) or 0) for v in by_model.values() if isinstance(v, dict))
        return 0

    def _enforce_daily_token_budget(self, summary: Dict[str, Any]) -> bool:
        limit = int(getattr(self.cfg, "team_daily_token_limit", 0) or 0)
        if limit <= 0:
            return False
        used = self._today_total_tokens(summary)
        if used >= limit:
            t = self._team
            if t is not None and not getattr(t, "paused_all", False):
                t.set_paused_all(True)
                self.hub.log(f"⚠️ 今日 Token 用量 ({used:,}) 已達成本護欄上限 ({limit:,})，已自動暫停全部房間。")
            return True
        return False

    def _watch_code(self) -> None:
        while not self._stop.wait(20.0):
            if code_mtime() > self.code_mtime_at_start + 1:
                self.hub.bump()

    # ---- 設定
    def save_cfg(self) -> None:
        with self.cfg_lock:
            self.cfg.save(self.config_path)

    # ---- 讀取
    def snapshot(self) -> Dict[str, Any]:
        """主控台上方列需要的應用程式資訊（辦公室的資料走 team 事件）。不含每秒都變的欄位。"""
        return {"app": {"name": APP_NAME, "version": self.version, "port": self.port,
                        "code_updated": code_mtime() > self.code_mtime_at_start + 1}}

    # ---- 工作階段動作（規則與桌面版相同）
    # ---- 全域設定
    def settings(self) -> Dict[str, Any]:
        c = self.cfg
        return {
            "ai": {k: getattr(c, k) for k in AI_FIELDS},
            "deny_patterns": list(c.deny_patterns),
            "models": {"file": os.path.join(self._team_base_dir, "litellm.local.json"),
                       "ready": self._team is not None and self._team.model_cfg is not None},
            "files": {"config": self.config_path, "log": os.path.join(BASE_DIR, "automation.log")},
        }

    def save_ai(self, body: Dict[str, Any]) -> Dict[str, Any]:
        bad = []
        with self.cfg_lock:
            for k, (typ, lo, hi) in AI_FIELDS.items():
                if k not in body:
                    continue
                v = body[k]
                try:
                    if typ is bool:
                        val: Any = bool(v)
                    elif typ is float:
                        val = max(lo, min(hi, float(v)))
                        if k == "team_max_running":
                            val = float(int(val))
                    else:  # team_plugin_dirs：可清空；以分號分隔多個資料夾
                        val = str(v).strip()[:2000]
                except (TypeError, ValueError):
                    bad.append(k)
                    continue
                setattr(self.cfg, k, val)
            self.cfg.save(self.config_path)
        self.hub.bump()
        return {"ok": not bad, "text": "已儲存" if not bad else f"已儲存；這些欄位格式不對沒有套用：{', '.join(bad)}"}

    def save_deny(self, patterns: List[str]) -> Dict[str, Any]:
        out, bad = [], []
        for p in patterns:
            p = str(p).strip()
            if not p:
                continue
            try:
                re.compile(p, re.I)
                out.append(p)
            except re.error:
                bad.append(p)
        if bad:
            return {"ok": False, "text": "這些規則不是有效的正規表示式：" + "；".join(bad[:5])}
        with self.cfg_lock:
            self.cfg.deny_patterns = out
            self.cfg.save(self.config_path)
        return {"ok": True, "text": f"已儲存 {len(out)} 條危險指令規則"}

    def test_deny(self, text: str) -> Dict[str, Any]:
        hit = check_deny([text], self.cfg.deny_patterns)
        return {"ok": True, "hit": hit or "", "text": f"命中 /{hit}/：會交給你決定" if hit else "沒有命中任何規則"}

    # ---- 基本按鈕精靈
    # ---- 生命週期
    def write_runtime(self) -> None:
        try:
            with open(RUNTIME_PATH, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "port": self.port, "token": self.token,
                           "url": f"http://127.0.0.1:{self.port}/", "started": now_str()}, f)
        except OSError:
            pass

    def shutdown(self, restart: bool = False) -> None:
        def later() -> None:
            time.sleep(0.4)
            self._stop.set()
            try:
                if self.worker is not None:
                    self.worker.stop()
            except Exception:
                pass
            try:
                if self._team is not None:
                    self._team.stop()
            except Exception:
                pass
            try:
                self.save_cfg()
            except Exception:
                pass
            if restart and os.name == "nt":
                bat = os.path.join(BASE_DIR, "restart.bat")
                subprocess.Popen(["cmd", "/c", "start", "", "/min", bat, "--from-server"], cwd=BASE_DIR,
                                 creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                if self.httpd is not None:
                    self.httpd.shutdown()
            except Exception:
                pass
            try:
                os.remove(RUNTIME_PATH)
            except OSError:
                pass
        threading.Thread(target=later, daemon=True).start()

    # ---- HTTP
    def serve(self, port: int = 0) -> ThreadingHTTPServer:
        last_err: Optional[Exception] = None
        ports = [port] if port == 0 else list(range(port, port + 12))
        for p in ports:
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(self))
                break
            except OSError as e:
                last_err = e
        if self.httpd is None:
            raise OSError(f"找不到可用的連接埠：{last_err}")
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        return self.httpd

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


# 網頁上可改的設定：只剩辦公室用得到的（舊 IDE 自動化的門檻、開關不再提供）
AI_FIELDS: Dict[str, Tuple[type, float, float]] = {
    "team_max_running": (float, 1.0, 16.0), "team_cost_weight": (float, 0.0, 1.0),
    "team_context_rollover": (float, 0.3, 0.95), "team_plugin_dirs": (str, 0, 0),
}

_POST_MAX = 64 * 1024


def make_handler(app: AssistantServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AIEngineeringAssistant"
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a) -> None:
            pass

        # ---- 共用
        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _cookie_token(self) -> str:
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "aea_t":
                    return v
            return ""

        def _send(self, code: int, body: bytes, ctype: str, csp: str = CSP_STRICT, cache: str = "no-store",
                  extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _redirect(self, where: str, cookie: str = "") -> None:
            self.send_response(303)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Location", where)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _file(self, full: str, inject: bool = False, csp: str = CSP_STRICT, cache: str = "no-store") -> None:
            with open(full, "rb") as f:
                body = f.read()
            ext = os.path.splitext(full)[1].lower()
            if inject and ext == ".html":
                body = body.replace(b"__TOKEN__", app.token.encode("ascii"))
            self._send(200, body, _MIME.get(ext, "application/octet-stream"), csp=csp, cache=cache)

        def _pixel_html(self, full: str) -> None:
            with open(full, "rb") as f:
                body = f.read()
            nonce = secrets.token_urlsafe(16)
            tok_bytes = app.token.encode("ascii")
            if b"__TOKEN__" in body:
                body = body.replace(b"__TOKEN__", tok_bytes)
            elif b'name="aea-token"' not in body:
                meta = b'<meta name="aea-token" content="' + tok_bytes + b'">'
                if b"</head>" in body:
                    body = body.replace(b"</head>", meta + b"</head>", 1)
                else:
                    body = meta + body
            nonce_attr = b' nonce="' + nonce.encode("ascii") + b'"'
            body = re.sub(rb"<script(?![^>]*\bnonce=)", b"<script" + nonce_attr, body, flags=re.I)
            csp = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self'; "
                "font-src 'self' data:; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
            )
            self._send(200, body, "text/html; charset=utf-8", csp=csp, cache="no-store")

        # ---- GET
        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path
            if path == "/healthz":
                return self._json(200, {"ok": True, "pid": os.getpid(), "version": app.version})
            if path == "/favicon.ico":
                full = safe_child(PIXEL_DIR, "favicon.ico") or safe_child(PIXEL_DIR, "favicon.svg")
                return self._file(full, cache="max-age=86400") if full and os.path.isfile(full) else self._send(204, b"", "image/x-icon")
            qt = (q.get("t") or [""])[0]
            if qt and hmac.compare_digest(qt, app.token):
                # 帶存取碼的網址：換成 HttpOnly cookie（30 天），再導向乾淨的網址
                cookie = f"aea_t={app.token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
                return self._redirect(path or "/", cookie)
            if not hmac.compare_digest(self._cookie_token(), app.token):
                full = safe_child(CONSOLE_DIR, "locked.html")
                if full and not path.startswith("/api/"):
                    with open(full, "rb") as f:
                        return self._send(401, f.read(), "text/html; charset=utf-8")
                return self._json(401, {"ok": False, "text": "請執行 restart.bat 重新開啟主控台"})
            try:
                return self._get_authed(path, q)
            except (BrokenPipeError, ConnectionError):
                return
            except Exception as e:
                traceback.print_exc()
                if path.startswith("/api/team/"):
                    return self._json(500, {"ok": False, "text": "辦公室發生錯誤，詳情請看主控台輸出"})
                return self._json(500, {"ok": False, "text": f"伺服器錯誤：{e!r}"})

        def _get_authed(self, path: str, q: Dict[str, List[str]]) -> None:
            arg = lambda k: (q.get(k) or [""])[0]  # noqa: E731
            if path in ("/", "/index.html", "/pixel", "/pixel/", "/pixel/index.html"):
                idx = safe_child(PIXEL_DIR, "index.html")
                if idx and os.path.isfile(idx):
                    return self._pixel_html(idx)
                return self._send(404, b"pixel office not built", "text/plain")
            if path.startswith("/_next/"):
                full = safe_child(PIXEL_DIR, path.lstrip("/"))
                if full and os.path.isfile(full):
                    return self._file(full, cache="public, max-age=31536000, immutable")
                return self._send(404, b"not found", "text/plain")
            if path.startswith("/pixel-assets/"):
                full = safe_child(PIXEL_DIR, path.lstrip("/"))
                if full and os.path.isfile(full):
                    return self._file(full, cache="public, max-age=3600")
                return self._send(404, b"not found", "text/plain")
            if path.startswith("/pixel/"):
                full = safe_child(PIXEL_DIR, path[len("/pixel/"):])
                if full and os.path.isfile(full):
                    return self._pixel_html(full) if full.endswith(".html") else self._file(full)
                return self._send(404, b"not found", "text/plain")
            if path == "/api/state":
                return self._json(200, app.snapshot())
            if path.startswith("/api/team/"):
                try:
                    res = app.team_get(path, q)
                except TeamError as e:
                    return self._json(e.code, {"ok": False, "text": e.text})
                if res is None:
                    return self._send(404, b"not found", "text/plain")
                return self._json(200, res)
            if path == "/api/stream":
                return self._stream(int(arg("log") or 0), int(arg("toast") or 0))
            if path == "/api/settings":
                return self._json(200, app.settings())
            if path == "/api/logs":
                return self._json(200, {"logs": app.hub.logs_after(int(arg("after") or 0))})
            if path.startswith("/static/"):
                full = safe_child(PIXEL_DIR, path[len("/static/"):])
                if full and os.path.isfile(full):
                    return self._file(full)
            return self._send(404, b"not found", "text/plain")

        def _stream(self, log_after: int, toast_after: int) -> None:
            """SSE：狀態有變就推送快照；日誌與通知各自只推新的。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True
            last_hash, last_team, ver, beat = "", "", -1, time.monotonic()
            try:
                self.wfile.write(b"retry: 2000\n\n")
                while not app._stop.is_set():
                    ver = app.hub.wait(ver, 1.5)
                    out = []
                    logs = app.hub.logs_after(log_after)
                    if logs:
                        log_after = logs[-1]["id"]
                        out.append(("log", logs[-200:]))
                    toasts = app.hub.toasts_after(toast_after)
                    if toasts:
                        toast_after = toasts[-1]["id"]
                        out.append(("toast", toasts))
                    snap = json.dumps(app.snapshot(), ensure_ascii=False)
                    h = hashlib.sha1(snap.encode("utf-8")).hexdigest()
                    if h != last_hash:
                        last_hash = h
                        out.insert(0, ("state", None))
                    tsnap = ""
                    if app._team is not None:  # 辦公室建好之後才推；快照不變就不送
                        try:
                            tsnap = json.dumps(app.team_snapshot(), ensure_ascii=False)
                            th = hashlib.sha1(tsnap.encode("utf-8")).hexdigest()
                            if th != last_team:
                                last_team = th
                                out.append(("team", None))
                        except Exception as e:  # 辦公室出錯不能弄斷整條串流；同類錯誤只記一次
                            if type(e).__name__ not in app._team_errs:
                                app._team_errs.add(type(e).__name__)
                                traceback.print_exc()
                                app.hub.log("⚠ 辦公室快照失敗：%s" % type(e).__name__)
                    for name, data in out:
                        payload = (snap if name == "state" else tsnap if name == "team"
                                   else json.dumps(data, ensure_ascii=False))
                        self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode("utf-8"))
                    if not out and time.monotonic() - beat > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        out.append(("beat", None))
                    if out:
                        beat = time.monotonic()
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                return

        # ---- POST
        def do_POST(self) -> None:
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if not self._host_ok():
                if 0 < n <= _POST_MAX:
                    self.rfile.read(n)
                return self._send(403, b"forbidden", "text/plain")
            if not hmac.compare_digest(self.headers.get("X-Token", ""), app.token):
                if 0 < n <= _POST_MAX:
                    self.rfile.read(n)
                return self._json(401, {"ok": False, "text": "存取碼不對（請重新整理頁面）"})
            if not 0 <= n <= _POST_MAX:
                return self._json(400, {"ok": False, "text": "請求太大"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
                if not isinstance(body, dict):
                    raise TypeError
            except (ValueError, TypeError):
                return self._json(400, {"ok": False, "text": "格式不對"})
            path = urlparse(self.path).path
            try:
                result = app.team_post(path, body) if path.startswith("/api/team/") else self._post(path, body)
            except TeamError as e:
                return self._json(e.code, {"ok": False, "text": e.text})
            except (KeyError, TypeError, ValueError) as e:
                if path.startswith("/api/team/"):
                    return self._json(400, {"ok": False, "text": "請求格式不對"})
                return self._json(400, {"ok": False, "text": f"格式不對：{e!r}"})
            except Exception as e:
                traceback.print_exc()
                if path.startswith("/api/team/"):
                    return self._json(500, {"ok": False, "text": "辦公室發生錯誤，詳情請看主控台輸出"})
                return self._json(500, {"ok": False, "text": f"伺服器錯誤：{e!r}"})
            if result is None:
                return self._send(404, b"not found", "text/plain")
            return self._json(200, result)

        def _post(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if path == "/api/settings/ai":
                return app.save_ai(body)
            if path == "/api/settings/deny":
                return app.save_deny(list(body["patterns"]))
            if path == "/api/settings/deny/test":
                return app.test_deny(str(body["text"]))
            if path == "/api/open":
                what = str(body.get("what", ""))
                target = {"reports": os.path.join(BASE_DIR, "reports"), "log": os.path.join(BASE_DIR, "automation.log"),
                          "folder": BASE_DIR, "summary": app.hub.summary_path}.get(what, "")
                if not target:
                    return {"ok": False, "text": "沒有可開啟的位置"}
                return {"ok": open_in_os(target), "text": "已開啟"}
            if path == "/api/shutdown":
                app.shutdown(restart=False)
                return {"ok": True, "text": "助理正在關閉"}
            if path == "/api/restart":
                if os.name != "nt":
                    return {"ok": False, "text": "只有 Windows 能自動重新啟動"}
                app.shutdown(restart=True)
                return {"ok": True, "text": "正在重新啟動，頁面會自動重新連線"}
            return None

    return Handler


# ---------------------------------------------------------------- 進入點

def _single_instance() -> Optional[object]:
    """系統互斥鎖（和舊的桌面版同名）：已經有一個 AI 工程助理在執行時回傳 None。"""
    if os.name != "nt":
        return object()
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    handle = k32.CreateMutexW(None, False, "Local\\AIEngineeringAssistant")
    if not handle or k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--open", action="store_true", help="啟動後在預設瀏覽器打開主控台")
    ap.add_argument("--no-open", action="store_true", help="不要打開瀏覽器")
    ap.add_argument("--port", type=int, default=0, help="連接埠（預設用 config.json 的 web_port）")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    lock = _single_instance()
    if lock is None:
        print("AI 工程助理已經在執行中。請用 restart.bat 重新啟動（會先關掉舊的）。")
        try:
            with open(RUNTIME_PATH, "r", encoding="utf-8") as f:
                rt = json.load(f)
            webbrowser.open(f"{rt['url']}?t={rt['token']}")
        except (OSError, ValueError, KeyError):
            pass
        return 2
    try:
        from clicker import enable_dpi_awareness
        enable_dpi_awareness()
        cfg_port = int(AppConfig.load(DEFAULT_CONFIG_PATH).web_port or 8765)
        app = AssistantServer()
        httpd = app.serve(args.port or cfg_port)
        app.write_runtime()
        print(f"[{APP_NAME}] 主控台：http://127.0.0.1:{app.port}/  （PID {os.getpid()}，版本 {app.version}）", flush=True)
        try:
            from team import bootstrap
            pf = bootstrap.run_preflight_check(app._team_base_dir)
            found_clis = [c["name"] for c in pf["clis"] if c["installed"]]
            cli_str = ", ".join(found_clis) if found_clis else "無（請於網頁精靈查看安裝指引）"
            model_str = f"已就緒 ({pf['model']['base_url']})" if pf["model"]["ready"] else "尚未設定（開啟主控台後將引導輸入 IP/Port 與金鑰）"
            print(f"[{APP_NAME}] 開機健檢 — 本機 CLI：{cli_str} ｜ 模型 API：{model_str}", flush=True)
        except Exception:
            pass
        app.hub.log(f"🚀 {APP_NAME} 已啟動：http://127.0.0.1:{app.port}/（版本 {app.version}）")
        if (args.open or app.cfg.open_browser) and not args.no_open:
            threading.Timer(0.6, lambda: webbrowser.open(app.url)).start()
        httpd.serve_forever(poll_interval=0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        err = traceback.format_exc()
        print(err, file=sys.stderr)
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"{now_str()}\n{err}\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
