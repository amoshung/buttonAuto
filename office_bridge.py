"""像素辦公室（pixeloffice/，改自 amoshung/pixeloffice_y6p2）和 AI 工程助理之間的轉接。

- build_office_snapshot：把每個開著的 IDE 視窗（工作階段）轉成辦公室裡的一位「夥伴」（OfficeSnapshot 格式）。
- OfficeBridge.assist：夥伴詳情裡按的按鈕／打的字 → 助理背景執行緒的指令。只接受當下列出的按鈕，
  實際操作仍走 request_click／request_send／request_rollover，規則與桌面版完全相同。

角色的走路、坐下、去茶水間由瀏覽器依 status 自己規劃，這裡只給狀態與座位。
"""
import datetime
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from projects import contains_assistant
from sessions import Session, SessionState, core_items, phase_label, phase_of

# 和 pixeloffice/lib/office-map.ts 的 DESK_SLOTS 相同（座位 = 角色坐的那一格）
DESK_SEATS: Dict[str, Tuple[int, int]] = {
    "desk-1": (3, 5), "desk-2": (8, 5), "desk-3": (3, 10), "desk-4": (8, 10),
    "desk-5": (14, 5), "desk-6": (18, 5), "desk-7": (14, 10), "desk-8": (18, 10),
}
DESK_ORDER = ["desk-1", "desk-2", "desk-5", "desk-6", "desk-3", "desk-4", "desk-7", "desk-8"]

# 和 pixeloffice/lib/office-sim.ts 的 AVATAR_PRESETS 相同
AVATARS: List[Dict[str, str]] = [
    {"hairStyle": "spiky", "hairColor": "#2c1e1a", "shirtColor": "#3b82f6", "skinTone": "#f3c6a5", "accessory": "headphones"},
    {"hairStyle": "bob", "hairColor": "#5b3222", "shirtColor": "#10b981", "skinTone": "#f7d3ba", "accessory": "glasses"},
    {"hairStyle": "cap", "hairColor": "#1f2937", "shirtColor": "#f59e0b", "skinTone": "#e4b08b", "accessory": "none"},
    {"hairStyle": "bun", "hairColor": "#1a1a2e", "shirtColor": "#ec4899", "skinTone": "#f5ccb0", "accessory": "glasses"},
    {"hairStyle": "short", "hairColor": "#7c4a2d", "shirtColor": "#8b5cf6", "skinTone": "#ecc09f", "accessory": "headphones"},
    {"hairStyle": "bob", "hairColor": "#d97706", "shirtColor": "#06b6d4", "skinTone": "#f9d8c2", "accessory": "none"},
    {"hairStyle": "short", "hairColor": "#374151", "shirtColor": "#ef4444", "skinTone": "#dfb08c", "accessory": "glasses"},
    {"hairStyle": "spiky", "hairColor": "#4c1d95", "shirtColor": "#14b8a6", "skinTone": "#f3c6a5", "accessory": "none"},
]

MODE_TEXT = {"auto": "自動駕駛", "suggest": "只建議", "off": "關閉"}
ACT_SEND_SUGGESTION = "送出助理建議的訊息"
ACT_NEW_CHAT = "開新對話接續"
ACT_DISMISS = "我看過了（清除提醒）"
ACT_CUSTOM = "自訂協助指令"

_LOG_TYPE = {"ask_human": "help_request", "approval_needed": "help_request", "blocked": "error", "loop": "error",
             "paused": "error", "observe": "status_change", "spec_review": "status_change", "verify": "status_change"}


def _hash(s: str) -> int:
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return h


def _iso(stamp: str) -> str:
    """now_str 格式（本機時間）→ 帶時區的 ISO 字串，瀏覽器才能正確顯示時間。"""
    try:
        t = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        t = datetime.datetime.now()
    return t.astimezone().isoformat()


def _short(text: str, n: int) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def worker_status(st: SessionState, mode: str) -> str:
    """工作階段 → 夥伴狀態（working／needs_help／idle／paused／error）。"""
    if mode == "off":
        return "paused"
    if needs_you(st, mode):
        return "needs_help"
    if st.agent_state == "error":
        return "error"
    if phase_of(st) == "done" or st.agent_state in ("idle", "done", ""):
        return "idle"
    return "working"


def needs_you(st: SessionState, mode: str) -> bool:
    if mode == "off":
        return False
    if st.pending_approval and st.approval_buttons:
        return True
    if [p for p in st.pending_human if not p.startswith("目標已達成")]:
        return True
    return mode == "suggest" and bool(st.suggested_prompt)


def help_and_actions(st: SessionState, mode: str) -> Tuple[str, List[str]]:
    """夥伴詳情裡「需要您的協助」的說明文字與可按的動作。"""
    if mode == "off":
        return "", []
    lines: List[str] = []
    actions: List[str] = []
    if st.pending_approval and st.approval_buttons:
        lines.append("代理想執行：" + _short(st.pending_approval, 400))
        actions += list(st.approval_buttons)
    if st.pending_human:
        lines.append("需要你決定：" + "；".join(st.pending_human[:5]))
    if mode == "suggest" and st.suggested_prompt:
        lines.append("助理建議送出：" + _short(st.suggested_prompt, 300))
        actions.append(ACT_SEND_SUGGESTION)
    if st.pending_human:
        actions.append(ACT_DISMISS)
    if st.goal:
        actions.append(ACT_NEW_CHAT)
    return "\n".join(lines), actions


def worker_logs(sess: Session, n: int = 15) -> List[Dict[str, str]]:
    out = []
    for i, (t, kind, text) in enumerate(sess.recent_records(n)):
        typ = "assisted" if kind in ("web_decide", "manual_check", "spec_confirmed", "goal") else _LOG_TYPE.get(kind, "info")
        out.append({"id": f"{t}-{i}", "timestamp": _iso(t), "type": typ, "message": text})
    return out


class OfficeBridge:
    """記住座位（新視窗不會讓其他人換位子）與剛處理過的夥伴（頭上冒綠色勾勾）。"""

    def __init__(self, store, cfg, list_windows: Callable[[], list], worker=None,
                 on_mode: Optional[Callable[[str, str], None]] = None,
                 on_running: Optional[Callable[[bool], None]] = None,
                 running: Callable[[], bool] = lambda: True) -> None:
        self.store = store
        self.cfg = cfg
        self.list_windows = list_windows
        self.worker = worker
        self.on_mode = on_mode
        self.on_running = on_running
        self.running = running
        self._desks: Dict[str, str] = {}
        self._celebrate: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._win_cache: Tuple[float, list] = (0.0, [])

    def _windows(self) -> list:
        now = time.monotonic()
        if now - self._win_cache[0] > 2.0:
            try:
                wins = [w for w in self.list_windows() if getattr(w, "project", "")]
            except Exception:
                wins = self._win_cache[1]
            self._win_cache = (now, wins)
        return self._win_cache[1]

    def _desk_for(self, key: str) -> str:
        with self._lock:
            if key not in self._desks:
                used = set(self._desks.values())
                free = [d for d in DESK_ORDER if d not in used]
                self._desks[key] = free[0] if free else DESK_ORDER[len(self._desks) % len(DESK_ORDER)]
            return self._desks[key]

    def mode_of(self, st: SessionState) -> str:
        return "off" if contains_assistant(st.path, st.remote) else self.cfg.mode_of(st.key)

    def sessions(self) -> List[Tuple[Session, Any]]:
        out = []
        seen = set()
        for w in self._windows():
            if w.key in seen:
                continue
            seen.add(w.key)
            out.append((self.store.get(w.key, w.app_label, w.project, w.path, w.remote), w))
        return out

    def snapshot(self, usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        workers = []
        for sess, w in self.sessions():
            st = sess.load()
            mode = self.mode_of(st)
            desk = self._desk_for(st.key)
            seat = DESK_SEATS[desk]
            help_text, actions = help_and_actions(st, mode)
            core = core_items(st)
            logs = worker_logs(sess)
            cut = (datetime.datetime.now() - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            workers.append({
                "id": st.key,
                "name": st.project or w.project,
                "role": f"{MODE_TEXT.get(mode, mode)} · {phase_label(st)}",
                "appName": st.app or w.app_label,
                "windowTitle": st.chat_title or getattr(w, "title", "") or st.project,
                "category": "ide",
                "status": worker_status(st, mode),
                "activity": st.current_task or st.status_summary or st.suggested_reason or "（還沒有觀察紀錄）",
                "helpPrompt": help_text or None,
                "quickActions": actions,
                "deskId": desk,
                "avatar": AVATARS[_hash(st.key) % len(AVATARS)],
                "lastHeartbeat": logs[0]["timestamp"] if logs else _iso(""),
                "source": "watcher",
                "logs": logs,
                "pos": {"x": seat[0], "y": seat[1]},
                "path": [],
                "facing": "up",
                "behaviorState": "at_desk",
                "celebrateUntil": self._celebrate.get(st.key, 0),
                "mode": mode,
                "modeLocked": contains_assistant(st.path, st.remote),
                "phase": phase_of(st),
                "phaseLabel": phase_label(st),
                "goal": st.goal,
                "verified": sum(1 for a in core if a.get("status") == "verified"),
                "core": len(core),
                "hourSends": sum(1 for t in st.auto_send_times if t >= cut),
                "hourCap": int(self.cfg.autopilot_max_per_hour),
            })
        order = {"needs_help": 0, "error": 1, "working": 2, "idle": 3, "paused": 4}
        workers.sort(key=lambda x: (order.get(x["status"], 5), x["name"]))
        return {"workers": workers, "commands": [], "lastUpdated": datetime.datetime.now().astimezone().isoformat(),
                "externalEventsCount": 0, "running": bool(self.running()), "usage": usage or {}}

    def _find(self, key: str) -> Optional[Session]:
        for sess, _w in self.sessions():
            if sess.load().key == key:
                return sess
        return None

    def assist(self, key: str, label: str, message: str) -> Dict[str, Any]:
        sess = self._find(key)
        if sess is None:
            return {"ok": False, "text": "找不到這個視窗（可能已經關掉了）"}
        st = sess.load()
        mode = self.mode_of(st)
        label, message = (label or "").strip(), (message or "").strip()
        if label == ACT_DISMISS:
            sess.update(lambda s: setattr(s, "pending_human", []))
            sess.history("web_decide", action="dismiss", by="user")
            return self._ok(key, "已清除提醒")
        if mode == "off":
            return {"ok": False, "text": "這個視窗的模式是「關閉」，助理不會操作它。先切到「只建議」或「自動駕駛」。"}
        if self.worker is None:
            return {"ok": False, "text": "助理的背景執行緒沒有啟動"}
        if label in st.approval_buttons and st.pending_approval:
            self.worker.request_click(key, label)
            sess.history("web_decide", button=label, by="user")
            return self._ok(key, f"已排入：代按「{label}」")
        if label == ACT_SEND_SUGGESTION and st.suggested_prompt:
            self.worker.request_send(key, st.suggested_prompt)
            sess.history("web_decide", action="send_suggestion", by="user")
            return self._ok(key, "已排入：送出助理建議的訊息")
        if label == ACT_NEW_CHAT and st.goal:
            self.worker.request_rollover(key)
            sess.history("web_decide", action="new_chat", by="user")
            return self._ok(key, "已排入：開新對話並送出接續摘要")
        if message:
            self.worker.request_send(key, message[:4000])
            sess.history("web_decide", action="send_text", by="user")
            return self._ok(key, "已排入：把你的訊息輸入到 IDE 並送出")
        return {"ok": False, "text": f"「{label}」目前不能按（可能已經處理過了）"}

    def _ok(self, key: str, text: str) -> Dict[str, Any]:
        self._celebrate[key] = int(time.time() * 1000) + 3500
        return {"ok": True, "text": text}

    def set_mode(self, key: str, mode: str) -> Dict[str, Any]:
        if mode not in MODE_TEXT:
            return {"ok": False, "text": "不認得的模式"}
        sess = self._find(key)
        if sess is None:
            return {"ok": False, "text": "找不到這個視窗"}
        st = sess.load()
        if mode != "off" and contains_assistant(st.path, st.remote):
            return {"ok": False, "text": "這是 AI 工程助理自己的專案，固定關閉，以免干擾你調整助理的對話"}
        if self.on_mode is None:
            return {"ok": False, "text": "無法切換模式（桌面版沒有啟動）"}
        self.on_mode(key, mode)
        return {"ok": True, "text": f"{st.project}：模式改為「{MODE_TEXT[mode]}」"}

    def set_running(self, running: bool) -> Dict[str, Any]:
        if self.on_running is None:
            return {"ok": False, "text": "無法切換（桌面版沒有啟動）"}
        self.on_running(bool(running))
        return {"ok": True, "text": "已全部繼續運作" if running else "已全部暫停（F8 或再按一次繼續）"}
