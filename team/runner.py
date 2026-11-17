"""房間執行緒：帶一間房從目標一路走到「等你合併」。

流程（每個轉換都寫事件並呼叫 on_change；角色出手時設定 room.persona）：
  準備（repo 檢查、worktree）→ spec（PO）→ 計畫（Tech Lead，room.phase 仍為 spec）→ build（Scrum Master）
  → test（QA，助理自己跑）→ review（Reviewer，唯讀審查座位）→ done（收件匣「合併」）。
測試或審查不通過 → 把具體缺口送回代理（fix）再回 build。

執行緒與等待：
- 執行緒本身依序送 prompt；驅動事件可能來自別的執行緒（ACP 事件執行緒、權限執行緒）。
- pause()／answer() 由其他執行緒呼叫：pause 設旗標、取消進行中的回合、喚醒所有等待者；
  每次送 prompt 前都檢查旗標（驅動在閒置時 cancel 不做事）。
- 收件匣等待在暫停時不解決（繼續時沿用同一個項目，不重問）；權限請求在暫停時拒絕。
- 暫停時：測試與檢查指令（checks.run_cmd 的 cancel）立刻停掉並清掉暫存副本；模型呼叫無法中斷，
  回來後立刻檢查旗標。room.status 在執行緒真正結束時才變 paused（之前 activity 為「暫停中…」）。
- Office 不可以在同一間房的舊執行器還活著（is_alive()）時啟動新的執行器。
- 致命錯誤：狀態 error，並在收件匣放一個 error 項目（每間房最多一個未解決的）。

附錄 A：每個階段開始時由 Dispatcher 選座位與模型；回合失敗且是用量已滿 → 標記、改由 GLM 接手重送同一回合；
階段邊界時原座位已恢復就切回。附錄 B：回合結束時量對話用量，≥ 門檻 → 下一回合前存檔、開新對話。
金鑰只在 ModelConfig 與 seat_launch 回傳的 SecretEnv 裡，不寫入事件或房間資料。

Task 17：每次問模型都帶 ctx（room_id、角色、目的），ModelRouter 的紀錄由 Office 寫成 kind "model" 事件；
每個 CLI 回合結束記一筆 kind "cli_usage"（實際模型 id、本回合 token、費用：CLI 有回報才寫）。
glm-5.3 以低思考量替 PO（開工補充）、QA（失敗摘要）、Reviewer（審查重點）寫 ≤ 200 字的說明，附在範本之後；
範本本身（格式要求、JSON 區塊、資料框）不變，模型不可用時只用範本並在事件註明。
"""
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from team import gitops, netcerts, notes, personas, policy
from team.acp import AcpError
from team.checks import build_issues, deny_issue, git_problem, test_gate
from team.clis import CliInfo
from team.drivers import GLM_SEATS, make_driver, seat_launch
from team.headless import interpreter_head, runner_tool_pattern
from team.events import (AgentEvent, PermissionRequest, clip, command_text, mask_detail, mask_text, tool_detail, tool_patch,
                          tool_result)
from team.fleet import SEAT_LABELS, Assignment, Dispatcher, NoSeatError, QuotaBook, detect_quota, detect_quota_strict
from team.gitops import GitError, Worktree
from team.login import LOGIN_SEATS, is_login_error, is_network_error, login_command
from team.modelprefs import ACP_SEATS, purpose_of
from team.models import ModelConfig, ModelRouter, call_ctx
from team.rooms import Room, RoomStore
from team.usage import DEMO_MODEL, EFFORT_LABELS, UsageBook, cli_event_text, merge_tokens

log = logging.getLogger(__name__)

DEFAULT_CONTEXT = 128000
MAX_SPEC_FIXES = 2
MAX_QUALITY_ROUNDS = 2
MAX_PLAN_FIXES = 2
SPEC_QUALITY_MIN = 0.6
PROGRESS_MIN = 0.5
PROGRESS_QUESTION = "代理是否仍朝目標前進？（有實際進展、沒有原地打轉或做範圍外的事）"
SPEC_QUALITY = "上面的驗收條件，每一條都能用指令客觀驗證嗎？"
EXE = {"kiro": "kiro-cli", "agy": "agy", "claude": "claude", "glm": "claude", "glm_high": "claude"}
_DONE = re.compile(r"(?<![A-Za-z])DONE(?![A-Za-z])")

OPT_PERMISSION = [{"id": "allow", "label": "允許一次"}, {"id": "reject", "label": "拒絕"}]
OPT_SPEC = [{"id": "start", "label": "開工"}, {"id": "edit", "label": "修改"}]
OPT_RETRY = [{"id": "retry", "label": "再試一次"}, {"id": "pause", "label": "我來處理"}]
OPT_UNVERIFIED = [{"id": "accept", "label": "我確認測試有效"}, {"id": "pause", "label": "我來處理"}]
OPT_MERGE = [{"id": "merge", "label": "合併"}, {"id": "later", "label": "先不要"}]
OPT_ERROR = [{"id": "retry", "label": "重試"}, {"id": "later", "label": "先放著"}]
OPT_LOGIN = [{"id": "open", "label": "開啟登入視窗"}, {"id": "check", "label": "我已登入，重新檢查"},
             {"id": "switch", "label": "改用其他座位"}]
OPT_NETWORK = [{"id": "retry", "label": "重試"}, {"id": "pause", "label": "我來處理"}]
LOGIN_DISK_CHECK = 1.0  # 等登入時每隔幾秒從磁碟確認項目是否已解決（Office 也會直接通知）
NO_SEAT_TITLE = "所有座位都沒有額度，等額度恢復或手動處理"
UNCHECKED = "未自動驗證"
NOTE_MAX = 200
NO_MODEL = "（模型不可用，用範本）"
NOTES_OFF = "（設定為只用範本）"
MODEL_TITLE = {"impl": "指定的實作模型不能用", "review": "指定的審查模型不能用"}
ROOM_AUTO = "這間房改用自動選擇（不套用設定頁的指定，也清掉這間房的指定）"
OPT_MODEL = [{"id": "retry", "label": "重新檢查"}, {"id": "auto", "label": "這間房改用自動選擇"},
             {"id": "pause", "label": "我來處理"}]
ROLE_TEXT = {"impl": "實作", "review": "審查"}
# Task 20：代管
DELEGABLE = ("approve_spec", "question", "unverified")
DELEGATE_PASS = 0.7   # Decision-1 判斷驗收條件的門檻
DELEGATE_ALLOW = 0.8  # Decision-1 再問一次權限的門檻（同自動允許的 allow 門檻）
KIND_TEXT = {"approve_spec": "驗收條件", "question": "問題", "unverified": "測試待確認", "permission": "權限",
             "error": "出錯"}
SPEC_DELEGATE_Q = "這些驗收條件是否每條都能用指令客觀驗證，而且對準目標？"
SPEC_OPINION_Q = "這份驗收條件可以開工嗎？每條都要能用指令客觀驗證、對準目標、不做範圍外的事。"
UNVERIFIED_Q = "QA 無法用「換回原狀」證明測試有效。看下面的條件、改動與 QA 說明，這些測試可信、真的檢查了這次的改動嗎？"
PERM_OPINION_Q = "AI 代理要執行下面的動作。它是為了完成目標、只影響工作範圍內的東西、沒有難以復原的後果嗎？可以允許一次嗎？"
PERM_D1_Q = "看過審查模型的第二意見後，這個動作應該允許一次嗎？（為了完成目標、只影響工作範圍內、沒有難以復原的後果）"


def note_label(model: str, effort: str) -> str:
    """角色說明的署名：示範假模型 →「示範（假模型）」；真模型 →「glm-5.3-0915 低思考量」。"""
    if model == DEMO_MODEL:
        return "示範（假模型）"
    eff = EFFORT_LABELS.get(effort, "")
    return ("%s %s" % (model, eff)).strip()


class _Paused(Exception):
    pass


class _Fatal(Exception):
    pass


def _is_quota(err: str, last: str) -> bool:
    """額度用完：看驅動的錯誤文字（stderr／結構化錯誤）；代理最後一段話只認明確說法。"""
    return detect_quota(err) or detect_quota_strict(last)


class _TurnFailed(Exception):
    def __init__(self, text: str, quota: bool):
        super().__init__(text)
        self.text = text
        self.quota = quota


class _StartFailed(Exception):
    def __init__(self, msg: str, raw: str = ""):
        super().__init__(msg)
        self.raw = raw  # 原始錯誤（判斷是否需要登入用）


class _NoSeat(Exception):
    """所有座位都沒有額度：暫停並留一個問題給使用者（不是錯誤）。"""


class _Perm:
    def __init__(self, pending: "policy.PendingPermission"):
        self.pending = pending
        self.by = ""  # user / pause / stop；空＝逾時
        self.text = ""


class _Waiter:
    def __init__(self, item_id: str, valid: Set[str]):
        self.item_id = item_id
        self.valid = valid
        self.ev = threading.Event()
        self.done = False
        self.answer = ""
        self.text = ""


def context_fraction(driver: Any, size_default: int = DEFAULT_CONTEXT) -> Tuple[float, bool]:
    """回傳（對話用量比例, 是否為估算）。有 last_usage 用之；否則以字元數 ÷ 3 ÷ size_default 估算。"""
    usage = getattr(driver, "last_usage", None)
    if usage and usage[1]:
        return min(1.0, max(0.0, float(usage[0]) / float(usage[1]))), False
    chars = int(getattr(driver, "chars", 0) or 0)
    size = size_default or DEFAULT_CONTEXT
    return min(1.0, chars / 3.0 / size), True


def login_hint(seat: str) -> str:
    if seat in GLM_SEATS:
        return "請確認 litellm.local.json 的 base_url 與 key 正確，並已在命令提示字元登入 claude（例如執行 claude --version）"
    exe = EXE.get(seat, seat)
    return "請確認已在命令提示字元登入 %s（例如執行 %s whoami）" % (exe, exe)


def _is_unchecked(c: Dict[str, Any]) -> bool:
    check = str(c.get("check") or "").strip()
    return not check or check.lower() == "manual"


def _unchecked_ids(criteria: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(c.get("id", "?")) for c in criteria or [] if _is_unchecked(c)]


def _marked(criteria: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """給審查看的條件：沒有可執行檢查的加註「未自動驗證」。"""
    return [dict(c, text="%s（%s）" % (c.get("text", ""), UNCHECKED)) if _is_unchecked(c) else dict(c)
            for c in criteria or []]


def _label(seat: str) -> str:
    return SEAT_LABELS.get(seat, seat)


def _secs_text(sec: float) -> str:
    return "%d 分鐘" % round(sec / 60) if sec >= 60 else "%g 秒" % sec


class RoomRunner(threading.Thread):
    def __init__(self, room_id: str, store: RoomStore, router: ModelRouter, clis: Dict[str, CliInfo],
                 deny_patterns: List[str], on_change: Callable[[], None],
                 cmd_overrides: Optional[Dict[str, List[str]]] = None, max_build_turns: int = 12,
                 max_fix_attempts: int = 3, dispatcher: Optional[Dispatcher] = None,
                 quota: Optional[QuotaBook] = None, model_cfg: Optional[ModelConfig] = None,
                 plugin_dirs: Sequence[str] = (), context_rollover: float = 0.70,
                 turn_timeout: float = 1800, permission_timeout: float = 1800,
                 login_checker: Optional[Callable[[str], Tuple[bool, str]]] = None,
                 usage: Optional[UsageBook] = None, prefs: Any = None):
        super().__init__(name="room-%s" % room_id, daemon=True)
        self.room_id = room_id
        self.store = store
        self.router = router
        self.clis = clis
        self.deny_patterns = list(deny_patterns or [])
        self.on_change = on_change
        self.cmd_overrides = dict(cmd_overrides or {})
        self.max_build_turns = max_build_turns
        self.max_fix_attempts = max_fix_attempts
        self.dispatcher = dispatcher
        self.quota = quota
        self.model_cfg = model_cfg
        # Task 19：事件細節裡要遮蔽的已知金鑰值（模型設定＋座位啟動環境變數）
        self._secrets = {v for v in (getattr(model_cfg, "glm_key", ""), getattr(model_cfg, "decision1_key", ""))
                         if isinstance(v, str) and v}
        self._tool_titles = {}  # type: Dict[str, str]  # 工具合併鍵 → 標題（結果事件用）
        self._drv_seq = 0
        self.plugin_dirs = list(plugin_dirs or ())
        self.context_rollover = context_rollover
        self.turn_timeout = turn_timeout
        self.permission_timeout = permission_timeout
        self.login_checker = login_checker
        self.usage = usage
        # Task 18：team.modelprefs.ModelPrefs（全域座位模型、用途、可用清單）；None ＝ 不驗證、用途用預設
        self.prefs = prefs
        self._note_label = ""  # 最近一次角色說明實際由哪個模型寫的（給 _noted 用）
        self._applied_impl = None  # type: Optional[Tuple[Any, ...]]  # 目前實作座位套用的（房間偏好, 全域座位模型）

        self.room = store.get(room_id)  # type: Optional[Room]
        self._lock = threading.RLock()
        self._pause_ev = threading.Event()
        self._waiters = {}  # type: Dict[str, _Waiter]
        self._perms = {}  # type: Dict[str, _Perm]
        self._impl = None  # type: Any
        self._review = None  # type: Any
        self._wt = None  # type: Optional[Worktree]
        self._para = []  # type: List[str]
        self._turn_paras = []  # type: List[str]
        self._turn_errors = []  # type: List[str]
        self._prefix = ""
        self._need_rollover = False
        self._last_reply = ""
        self._unverified_accepted = False
        self._suggestions = []  # type: List[str]
        self._stopped = False
        self._user_msgs = []  # type: List[str]
        self._delegate_flag = bool(getattr(self.room, "delegate", False))  # Task 20：Office 以 set_delegate 更新
        self._delegate_gen = 0  # 每次開啟代管 +1：關掉再開會重新試一次已交給你的項目
        self._asked_by = ""  # 最近一次 _ask 的答案是誰給的：user｜delegate

    # ================= 對外（其他執行緒呼叫） =================
    def pause(self) -> None:
        """要求暫停：立刻回傳；執行緒在幾秒內結束（status 那時才變 paused）。"""
        first = not self._pause_ev.is_set()
        self._pause_ev.set()
        if first and self.is_alive():
            try:
                self.store.update(self.room_id, lambda r: setattr(r, "activity", "暫停中…"))
                self._changed()
            except Exception:
                log.debug("無法寫入暫停中", exc_info=True)
        with self._lock:
            waiters = list(self._waiters.values())
            drivers = [d for d in (self._impl, self._review) if d is not None]
        for d in drivers:
            try:
                d.cancel()
            except Exception:
                log.debug("取消回合失敗", exc_info=True)
        self._reject_perms("pause")
        for w in waiters:
            w.ev.set()

    def _reject_perms(self, by: str) -> None:
        with self._lock:
            for rec in self._perms.values():
                if rec.pending.answer("reject"):
                    rec.by = by

    @property
    def stopping(self) -> bool:
        """已要求暫停或執行緒正在收尾：不會再送新的回合。"""
        return self._pause_ev.is_set() or self._stopped

    def send(self, text: str) -> None:
        """使用者對代理講話：排進下一個實作回合的開頭，以「【你】」署名（使用者的話是指示，不放進資料圍欄）。"""
        text = (text or "").strip()
        if text:
            with self._lock:
                self._user_msgs.append(text)

    def set_delegate(self, on: bool) -> None:
        """Office 切換代管：開啟時，正在等的可代管項目（問題、驗收條件、測試待確認、權限）由等待中的執行緒立刻處理。"""
        if on and not self._delegate_flag:
            self._delegate_gen += 1
        self._delegate_flag = bool(on)

    def waiting_on(self, item_id: str) -> bool:
        """這個執行器現在是否正在等這個收件匣項目（問題或權限請求）。"""
        with self._lock:
            w = self._waiters.get(item_id)
            return (w is not None and not w.done) or item_id in self._perms

    def drain_user_msgs(self) -> List[str]:
        """取走還沒送出的使用者訊息（執行器結束時由 Office 接手，轉給下一個執行器）。"""
        with self._lock:
            msgs, self._user_msgs = self._user_msgs, []
        return msgs

    def _take_user_msgs(self) -> str:
        return "\n".join("【你】" + m for m in self.drain_user_msgs())

    def notify_login(self, item_id: str, answer: str) -> bool:
        """Office 已解決登入項目（ok＝已登入、switch＝改用其他座位）：叫醒等這個項目的執行緒（不重複解決項目）。"""
        with self._lock:
            w = self._waiters.get(item_id)
            if w is None or w.done or answer not in w.valid:
                return False
            w.done, w.answer = True, answer
        w.ev.set()
        return True

    def answer(self, item_id: str, answer: str, text: str = "") -> bool:
        with self._lock:
            perm = self._perms.get(item_id)
            w = self._waiters.get(item_id)
            if perm is not None:
                # 先由 PendingPermission 決定誰先到；收件匣項目由等待的那條執行緒依實際結果解決
                if answer not in ("allow", "reject") or not perm.pending.answer(answer):
                    return False
                perm.by, perm.text = "user", text or ""
                return True
            if w is None or w.done or answer not in w.valid:
                return False
            w.done, w.answer, w.text = True, answer, text or ""
        self._resolve(item_id, answer, text)
        w.ev.set()
        return True

    # ================= 執行緒主體 =================
    def run(self) -> None:
        if self.room is None:
            return
        try:
            self._run()
        except _Paused:
            self._set(status="paused", persona="", activity="已暫停")
            self._event("status", "已暫停", status="paused")
        except _NoSeat as e:
            self._no_seat(str(e))
        except _Fatal as e:
            self._fail(str(e))
        except Exception as e:  # 不讓房間永遠停在運作中
            log.exception("房間執行器錯誤")
            self._fail("執行器錯誤：%s：%s" % (type(e).__name__, e))
        finally:
            self._stopped = True
            self._close_all()
            self._changed()

    def _fail(self, msg: str) -> None:
        try:
            self._set(status="error", error=msg, persona="", activity="錯誤")
            self._event("status", msg, status="error")
            with self._lock:
                if not self._room_items(("error",)):
                    self.store.add_inbox(self.room_id, "error", "%s 停下來了" % (self.room.name or self.room.slug),
                                         "%s\n\n建議：依上面的說明處理後按「重試」。" % msg, OPT_ERROR)
                    self._event("inbox", "停下來了，等你處理", inbox_kind="error")
        except Exception:
            log.exception("無法寫入錯誤狀態")

    def _no_seat(self, msg: str) -> None:
        try:
            with self._lock:
                if not [i for i in self._room_items(("question",)) if i.title == NO_SEAT_TITLE]:
                    self.store.add_inbox(self.room_id, "question", NO_SEAT_TITLE, msg, OPT_RETRY)
            self._set(status="paused", persona="", activity=NO_SEAT_TITLE)
            self._event("status", "%s：%s" % (NO_SEAT_TITLE, msg), status="paused")
        except Exception:
            log.exception("無法寫入暫停狀態")

    def _run(self) -> None:
        self._check_pause()
        self._set(status="running", error="", activity="準備中")
        self._event("status", "開始運作", status="running")
        r = self.room
        prob = gitops.repo_problem(r.repo)
        if prob:
            raise _Fatal(prob)
        try:
            wt = gitops.create_worktree(r.repo, r.slug, base=r.base)
        except GitError as e:
            raise _Fatal(str(e))
        self._wt = wt
        cand = wt.branch.split("/", 1)[-1] if "/" in wt.branch else r.slug
        nd = r.notes_dir or notes.notes_dir(r.repo, cand)
        self._set(worktree=wt.path, branch=wt.branch, base=r.base or wt.base,
                  target_branch=r.target_branch or wt.target_branch, notes_dir=nd)
        self._event("worktree", "工作目錄：%s（分支 %s）" % (wt.path, wt.branch))
        self._purge_notes()
        if self.room.phase == "done":
            self._set(status="done", persona="", activity="全部關卡通過，等你合併")
            return
        self._resume_pending()

        if self.room.phase in ("goal", "spec"):
            self._spec()
            self._plan()
        stage = self.room.phase if self.room.phase in ("build", "test", "review", "done") else "build"
        prompt = None  # type: Optional[str]
        while stage != "done":
            if stage == "build":
                self._build(prompt or self.room.pending_fix or None)
                prompt, stage = None, "test"
            elif stage == "test":
                prompt = self._test()
                stage = "build" if prompt else "review"
            else:
                prompt = self._review_stage()
                stage = "build" if prompt else "done"

    # ================= 收件匣等待 =================
    def _room_items(self, kinds: Sequence[str], resolved: bool = False) -> List[Any]:
        return [i for i in self.store.inbox(include_resolved=resolved)
                if i.room_id == self.room_id and i.kind in kinds]

    def _ask(self, kind: str, title: str, detail: str, options: List[Dict[str, str]],
             item: Any = None) -> Tuple[str, str]:
        self._asked_by = "user"
        tried = self._delegate_gen if item is not None and bool(getattr(item, "handoff", False)) else None
        if kind in DELEGABLE and self._delegate_flag and tried is None:
            tried = self._delegate_gen
            got = self._delegate_answer(kind, title, detail, options, item)
            if got is not None:
                return got
            item = self._handoff(kind, title, detail, options, item)
        with self._lock:
            if item is None:
                item = self.store.add_inbox(self.room_id, kind, title, detail, options)
            w = _Waiter(item.id, {str(o.get("id")) for o in (item.options or options)})
            self._waiters[item.id] = w
        self._set(status="waiting_user", activity="等你：" + item.title)
        self._event("inbox", item.title, item_id=item.id, inbox_kind=kind)
        try:
            while not w.ev.wait(0.2):
                if self._pause_ev.is_set():
                    break
                if kind in DELEGABLE and self._delegate_flag and tried != self._delegate_gen:
                    tried = self._delegate_gen  # 等你的時候開啟了代管：立刻替你回答（你先回答的話以你為準）
                    self._set(status="running", activity="代管決定中：" + item.title)
                    got = self._delegate_decide(kind, item.title, item.detail, item.options or options)
                    with self._lock:
                        if got is not None and not w.done:
                            w.done, w.answer, w.text = True, got[0], got[1]
                            self._asked_by = "delegate"
                    if self._asked_by == "delegate":
                        self._resolve(item.id, w.answer, "代管：" + (w.text or w.answer))
                        break
                    if got is not None:  # 代管想好之前你已經回答：以你的回答為準
                        self._event("notice", "你先回答了「%s」，代管的決定不採用" % item.title, delegate=True)
                    if not w.done:
                        self._handoff(kind, item.title, item.detail, item.options, item)
                        self._set(status="waiting_user", activity="等你：" + item.title)
        finally:
            with self._lock:
                self._waiters.pop(item.id, None)
        if not w.done:
            raise _Paused()
        self._set(status="running", activity="")
        if self._asked_by == "delegate":
            return w.answer, w.text
        self._event("answer", "你回答了「%s」：%s" % (item.title, w.answer), item_id=item.id, answer=w.answer)
        return w.answer, w.text

    # ================= 代管（Task 20） =================
    def _delegate_answer(self, kind: str, title: str, detail: str, options: List[Dict[str, str]],
                         item: Any) -> Optional[Tuple[str, str]]:
        """代管直接決定（不進收件匣）：決定了 → 留一個已解決的項目當紀錄，回傳（答案, 文字）；交給你 → None。"""
        self._set(status="running", activity="代管決定中：" + title)
        got = self._delegate_decide(kind, title, detail, options)
        if got is None:
            return None
        ans, text = got
        with self._lock:
            if item is None:
                item = self.store.add_inbox(self.room_id, kind, title, detail, options)
            self._resolve(item.id, ans, "代管：" + (text or ans))
        self._asked_by = "delegate"
        self._set(status="running", activity="")
        return ans, text

    def _handoff(self, kind: str, title: str, detail: str, options: List[Dict[str, str]], item: Any) -> Any:
        """代管沒有決定：項目標成「交給你」（代管不再碰它），照原本流程等你。"""
        with self._lock:
            if item is None:
                item = self.store.add_inbox(self.room_id, kind, title, detail, options)
            got = self.store.update_item(item.id, lambda x: setattr(x, "handoff", True))
        return got or item

    def _decision(self, text: str, **detail: Any) -> None:
        """決策事件：代管選了什麼、為什麼（展開看得到每個模型的答案）。"""
        self._event("decision", "代管決定：" + text, delegate=True, demo=bool(self.room.demo),
                    detail=self._mask({k: v for k, v in detail.items() if v not in (None, "", [], {})}))

    def _dctx(self, purpose: str, **extra: Any) -> Dict[str, Any]:
        return self._ctx("delegate", purpose, **extra)

    def _delegate_decide(self, kind: str, title: str, detail: str,
                         options: List[Dict[str, str]]) -> Optional[Tuple[str, str]]:
        try:
            if kind == "approve_spec":
                return self._delegate_spec(title)
            if kind == "unverified":
                return self._delegate_unverified(title, detail)
            if kind == "question":
                return self._delegate_question(title, detail, options)
        except (_Paused, _Fatal, _NoSeat):
            raise
        except Exception as e:
            log.exception("代管決定失敗")
            self._decision("判斷失敗（%s），交給你：%s" % (type(e).__name__, title), item=KIND_TEXT.get(kind, kind),
                           title=title, choice="交給你", reason="代管判斷時發生錯誤")
        return None

    def _spec_denied(self) -> List[str]:
        try:
            from automation import check_deny  # 延後匯入：automation 會載入 Windows UI 自動化模組
        except Exception:
            return ["無法載入危險規則檢查"]
        r = self.room
        out = []  # type: List[str]
        for cmd in [r.test_cmd or ""] + [(c.get("check") or "").strip() for c in r.criteria or []]:
            if cmd and cmd.lower() != "manual":
                hit = check_deny([cmd], self.deny_patterns)
                if hit:
                    out.append(deny_issue(cmd, hit))
        return out

    def _delegate_spec(self, title: str) -> Optional[Tuple[str, str]]:
        """驗收條件：Decision-1 判斷可驗證且對準目標＋審查模型意見；都通過 → 開工，否則意見送回 PO（計入修訂上限）。"""
        used = int((self.room.attempts or {}).get("delegate_spec", 0))
        if used >= MAX_SPEC_FIXES:
            self._decision("驗收條件已修訂 %d 次仍未通過，暫停並交給你" % used, item="驗收條件", title=title,
                           choice="交給你", reason="代管的修訂次數已達上限（%d 次）" % MAX_SPEC_FIXES)
            return None
        md = self._spec_md()
        denied = self._spec_denied()
        if denied:
            notes = "驗收指令命中危險規則，代管不允許，請改寫：\n" + "\n".join(denied)
            self._bump_attempts("delegate_spec")
            self._decision("驗收指令命中危險規則，退回 PO 修改", item="驗收條件", title=title, choice="修改",
                           reason=notes)
            return "edit", notes
        alerts = self._spec_alerts() + self._spec_notes()
        material = md + ("\n\n助理的提醒：\n" + "\n".join(alerts) if alerts else "")
        p = self._prob(personas.fenced("驗收條件", material), SPEC_DELEGATE_Q, self._dctx("代管：驗收條件可驗證且對準目標", pass_min=DELEGATE_PASS))
        self._check_pause()
        ok, why = call_ctx(self.router.second_opinion, SPEC_OPINION_Q, material,
                           ctx=self._dctx("代管：審查驗收條件"))
        self._check_pause()
        d1_text = "Decision-1 %s（門檻 %.2f）" % ("不可用" if p is None else "%.2f" % p, DELEGATE_PASS)
        rv_text = "審查模型%s：%s" % ({True: "通過", False: "不通過"}.get(ok, "不可用"), why or "（沒有理由）")
        models = "%s；%s" % (d1_text, rv_text)
        if p is not None and p >= DELEGATE_PASS and ok is True:
            self._reset_attempts("delegate_spec")
            self._decision("驗收條件通過，開工", item="驗收條件", title=title, choice="開工",
                           reason="Decision-1 與審查模型都通過", models=models, threshold=DELEGATE_PASS)
            return "start", ""
        n = self._bump_attempts("delegate_spec")
        notes = "代管審查沒有通過（第 %d 次）：%s\n%s" % (n, d1_text, rv_text)
        self._decision("驗收條件沒有通過，意見送回 PO 修改（第 %d 次）" % n, item="驗收條件", title=title, choice="修改",
                       reason=notes, models=models, threshold=DELEGATE_PASS)
        return "edit", notes

    def _delegate_unverified(self, title: str, detail: str) -> Optional[Tuple[str, str]]:
        """測試無法驗證：審查模型判斷測試是否可信 → 接受，或要求代理補強。"""
        crit = "\n".join("%s %s（檢查：%s）" % (c.get("id"), c.get("text"), c.get("check") or "manual")
                         for c in self.room.criteria or [])
        try:
            stat = gitops.diff_stat(self._wt) if self._wt else ""
        except Exception:
            stat = ""
        material = "QA 說明：\n%s\n\n驗收條件：\n%s\n\n改動統計：\n%s" % (detail, crit, stat)
        ok, why = call_ctx(self.router.second_opinion, UNVERIFIED_Q, material, ctx=self._dctx("代管：判斷測試是否可信"))
        self._check_pause()
        rv = "審查模型%s：%s" % ({True: "認為可信", False: "認為不可信"}.get(ok, "不可用"), why or "（沒有理由）")
        if ok is True:
            self._decision("測試可信，接受", item="測試待確認", title=title, choice="接受", reason=rv)
            return "accept", why
        self._decision("測試不夠可信，要求代理補強", item="測試待確認", title=title, choice="補強測試", reason=rv)
        return "strengthen", rv

    def _delegate_question(self, title: str, detail: str, options: List[Dict[str, str]]) -> Optional[Tuple[str, str]]:
        """問題：Decision-1 選選項；同一題連續第 2 次 → 交給你。選「我來處理」也交給你。"""
        labels = {str(o.get("id")): str(o.get("label") or o.get("id")) for o in options or []}
        cur = self.store.get(self.room_id) or self.room
        if title and title == (cur.delegate_last or ""):
            self._decision("同一個問題連續第 2 次，暫停並交給你：%s" % title, item="問題", title=title, choice="交給你",
                           reason="代管已經替你回答過一次同樣的問題，避免來回打轉（關掉再開代管會重新計算）")
            return None
        state = "%s\n\n問題：%s\n%s\n\n選項：%s" % (self._task_text(), title, detail[:3000],
                                                 "、".join("%s（%s）" % (v, k) for k, v in labels.items()))
        pick, probs = call_ctx(self.router.choose, state, title, list(options or []), ctx=self._dctx("代管：選擇選項"))
        self._check_pause()
        nums = "、".join("%s %.2f" % (labels.get(k, k), v) for k, v in probs.items())
        if pick is None or pick not in labels:
            self._decision("Decision-1 無法選擇，交給你：%s" % title, item="問題", title=title, choice="交給你",
                           reason="判斷模型不可用或沒有回答")
            return None
        if pick == "pause":
            self._decision("Decision-1 認為這題要你處理：%s" % title, item="問題", title=title, choice="交給你",
                           reason="選了「%s」" % labels[pick], models=nums)
            return None
        self._set(delegate_last=title)
        self._decision("「%s」選「%s」" % (title, labels[pick]), item="問題", title=title, choice=labels[pick],
                       reason="Decision-1 機率最高的選項", models=nums)
        return pick, ""

    def _delegate_permission(self, req: PermissionRequest, cmd: str, d: Any) -> Tuple[str, str]:
        """權限（代管）：規則擋下的（危險規則、超出範圍…）直接拒絕；模型判為灰色或高風險 → 審查模型第二意見＋
        Decision-1 再問一次，都允許才允許一次，否則拒絕。永遠不交給你。回傳（allow|reject, 理由）。"""
        if d.rule or d.verdict is None:
            why = "%s，代管不允許" % d.reason
            self._decision("拒絕「%s」（%s）" % (clip(cmd, 120), d.reason), item="權限", title=cmd, choice="拒絕", reason=why)
            return "reject", why
        v = d.verdict
        material = "目標：%s\n工作範圍：%s\n動作種類：%s\n指令：%s\n第一次判斷：%s" % (
            self.room.goal, self._wt.path if self._wt else "", req.kind, cmd, d.reason)
        ok, why = call_ctx(self.router.second_opinion, PERM_OPINION_Q, material, ctx=self._dctx("代管：權限第二意見"))
        rv = "審查模型%s：%s" % ({True: "允許", False: "不允許"}.get(ok, "不可用"), why or "（沒有理由）")
        p = None  # type: Optional[float]
        if ok is True and self._pause_ev.is_set():  # 暫停中：不再問下一個模型，直接拒絕
            return "reject", "房間暫停中，代管拒絕"
        if ok is True:
            p = self._prob(personas.fenced("動作與審查模型第二意見", material + "\n審查模型第二意見：" + (why or "允許")),
                           PERM_D1_Q,
                           self._dctx("代管：權限再問一次", pass_min=DELEGATE_ALLOW))
        d1 = "Decision-1 %s（門檻 %.2f）" % ("沒有再問（審查模型不允許）" if ok is not True else
                                         "不可用" if p is None else "%.2f" % p, DELEGATE_ALLOW)
        models = "第一次：%s；%s；%s" % (policy.verdict_text(v), rv, d1)
        if ok is True and p is not None and p >= DELEGATE_ALLOW:
            self._decision("允許一次「%s」" % clip(cmd, 120), item="權限", title=cmd, choice="允許一次",
                           reason="審查模型與 Decision-1 都允許", models=models, threshold=DELEGATE_ALLOW)
            return "allow", "審查模型與 Decision-1 都允許"
        self._decision("拒絕「%s」" % clip(cmd, 120), item="權限", title=cmd, choice="拒絕",
                       reason="審查模型或 Decision-1 不允許（代管不交給你）", models=models, threshold=DELEGATE_ALLOW)
        return "reject", "%s；%s" % (rv, d1)

    def _resume_pending(self) -> None:
        """重啟後：先等之前沒回答的問題（不重問）。approve_spec 在 spec 階段處理。"""
        for item in self._room_items(("question", "unverified")):
            ans, text = self._ask(item.kind, item.title, item.detail, item.options, item=item)
            if ans == "pause":
                raise _Paused()
            if ans == "auto" and item.title in MODEL_TITLE.values():
                self._clear_room_model(next(k for k, v in MODEL_TITLE.items() if v == item.title))
            if item.kind == "unverified" and ans == "accept":
                self._unverified_accepted = True
                self._event("notice", "%s確認測試有效" % ("代管" if self._asked_by == "delegate" else "使用者"))
            if item.kind == "unverified" and ans == "strengthen":  # 同關卡路徑：計入測試修正次數
                self._bump_attempts("test")
                self._set(phase="build", unverified="", pending_fix=self._strengthen_fix(item.detail, text))
            if ans == "retry":
                for key in ("test", "review"):
                    if int((self.room.attempts or {}).get(key, 0)) > self.max_fix_attempts:
                        self._reset_attempts(key)

    def _reset_attempts(self, key: str) -> None:
        att = dict(self.room.attempts or {})
        att[key] = 0
        self._set(attempts=att)

    def _bump_attempts(self, key: str) -> int:
        att = dict(self.room.attempts or {})
        att[key] = int(att.get(key, 0)) + 1
        self._set(attempts=att)
        return att[key]

    def _ask_retry(self, title: str, detail: str) -> None:
        ans, _ = self._ask("question", title, detail, OPT_RETRY)
        if ans != "retry":
            raise _Paused()

    # ================= 狀態與事件 =================
    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception:
            log.debug("on_change 失敗", exc_info=True)

    def _set(self, **fields: Any) -> None:
        def mut(room: Room) -> None:
            for k, v in fields.items():
                setattr(room, k, v)
        self.room = self.store.update(self.room_id, mut)
        self._changed()

    def _event(self, kind: str, text: str, **data: Any) -> None:
        # 看得到的摘要文字與細節都遮蔽金鑰（細節在建立時已先遮再截，這裡再保險一次）
        text = mask_text(text, self._secrets)
        if "detail" in data:
            data["detail"] = mask_detail(data["detail"], self._secrets)
        try:
            self.store.event(self.room_id, kind, text, **data)
        except Exception:
            log.exception("寫入事件失敗")
        self._changed()

    def _mask(self, obj: Any) -> Any:
        """事件細節：遮蔽金鑰（已知值與 key=值 寫法）。"""
        return mask_detail(obj, self._secrets)

    def _mclip(self, text: Any, n: int = 4000) -> str:
        """先遮蔽再截斷（先截可能把金鑰切成兩半而遮不到）。"""
        return clip(mask_text(text if isinstance(text, str) else str(text or ""), self._secrets), n)

    @staticmethod
    def _tool_key(who: str, src: Any, tid: str) -> str:
        """工具事件的合併鍵：座位＋這次開的驅動（序號）＋對話 id＋toolCallId，不同對話／換新對話不會撞在一起。"""
        if not tid:
            return ""
        seq = src.get("seq", 0) if isinstance(src, dict) else 0
        drv = src.get("drv") if isinstance(src, dict) else None
        sid = ""
        for holder in (getattr(drv, "client", None), getattr(drv, "claude", None), drv):
            v = getattr(holder, "session_id", "") if holder is not None else ""
            if isinstance(v, str) and v:
                sid = v
                break
        return "%s|%s|%s|%s" % (who, seq, sid[:40], tid[:80])

    def _say(self, role: str, text: str) -> None:
        self._event("say", personas.sign(role, text), role=role)

    def _check_pause(self) -> None:
        if self._pause_ev.is_set():
            raise _Paused()

    def _phase(self, dispatch_phase: str, room_phase: str, persona: str, activity: str) -> None:
        self._check_pause()
        self._set(phase=room_phase, persona=persona, activity=activity)
        self._event("phase", activity, phase=dispatch_phase, persona=persona)
        if dispatch_phase in ("spec", "plan", "build"):
            self._ensure_impl(dispatch_phase)

    # ================= 驅動 =================
    def _on_event(self, who: str, ev: AgentEvent, src: Any = None) -> None:
        if ev.kind == "message":
            with self._lock:
                self._para.append(ev.text)
            return
        if ev.kind == "tool_update":
            # 工具更新：補充的欄位（title／rawInput／locations）合併進同一列；完成／失敗時帶輸出尾端
            data = self._mask(ev.data if isinstance(ev.data, dict) else {})  # 先遮再截
            st = str(data.get("status") or "")
            tid = str(data.get("toolCallId") or "")
            key = self._tool_key(who, src, tid)
            patch = tool_patch(data)
            with self._lock:
                if patch.get("title") and key:
                    self._tool_titles[key] = patch["title"][:120]
                title = (self._tool_titles.pop(key, "") if st in ("completed", "failed") else self._tool_titles.get(key, "")) \
                    if key else ""
            if st in ("completed", "failed"):
                detail = dict(patch, **tool_result(data))
                self._event("tool_result", "%s：%s" % (title or ev.text or "工具", "完成" if st == "completed" else "失敗"),
                            who=who, tool_call_id=tid, tool_key=key, status=st, detail=detail)
            elif patch:
                self._event("tool_update", "%s：更新" % (title or patch.get("title") or "工具"),
                            who=who, tool_call_id=tid, tool_key=key, detail=patch)
            return
        if ev.kind in ("thought", "plan", "usage", "done"):
            return
        self._flush(who)
        if ev.kind == "tool":
            data = self._mask(ev.data if isinstance(ev.data, dict) else {})
            tid = str(data.get("toolCallId") or "")
            key = self._tool_key(who, src, tid)
            if key:
                with self._lock:
                    self._tool_titles[key] = (ev.text or "")[:120]
                    while len(self._tool_titles) > 200:
                        self._tool_titles.pop(next(iter(self._tool_titles)))
            self._event("tool", ev.text or "（工具）", who=who, tool_call_id=tid, tool_key=key,
                        detail=tool_detail(mask_text(ev.text or "", self._secrets), data))
        elif ev.kind == "notice":
            self._event("notice", ev.text, who=who)
        elif ev.kind == "error":
            with self._lock:
                self._turn_errors.append(ev.text)
            self._event("error", ev.text, who=who)

    def _flush(self, who: str) -> None:
        with self._lock:
            text = "".join(self._para).strip()
            self._para = []
            if text:
                self._turn_paras.append(text)
        if text:
            self._event("agent", text, who=who)

    def _on_permission(self, req: PermissionRequest, read_only: bool, who: str) -> str:
        if self._pause_ev.is_set():
            return policy.pick_option(req.options, "reject")
        cmd = command_text(req)
        wt = self._wt.path if self._wt else ""
        d = policy.decide(req, goal=self.room.goal, scope=wt, deny_patterns=self.deny_patterns,
                          router=self.router, read_only=read_only, ctx=self._ctx("policy", ""))
        v = d.verdict
        vdata = {"model": v.by, "allow": round(v.allow, 4), "risk": round(v.risk, 4)} if v is not None else {}
        td = tool_detail(req.title, self._mask({"rawInput": req.raw_input, "kind": req.kind}))
        vdata["detail"] = self._mask({k: x for k, x in {
            "command": self._mclip(cmd), "paths": td.get("paths") or [], "decision": d.outcome,
            "allow": round(v.allow, 4) if v is not None else None, "risk": round(v.risk, 4) if v is not None else None,
            "by": v.by if v is not None else "", "models": v.detail if v is not None else "", "reason": d.reason,
        }.items() if x not in (None, "", [])})
        if d.outcome in ("allow", "reject"):
            self._event("permission", "%s：%s（%s）" % ("自動允許" if d.outcome == "allow" else "自動拒絕",
                                                     cmd, d.reason), outcome=d.outcome, who=who, **vdata)
            return d.option_id
        if self._delegate_flag:  # 代管：不交給你（規則擋下的直接拒絕；灰色／高風險再問兩個模型）
            if self._pause_ev.is_set():
                return policy.pick_option(req.options, "reject")
            out, why = self._delegate_permission(req, cmd, d)
            vdata["detail"] = dict(vdata.get("detail") or {}, decision=out, reason="代管：" + why)
            self._event("permission", "代管%s：%s（%s）" % ("允許一次" if out == "allow" else "拒絕", cmd, why),
                        outcome=out, who=who, delegate=True, **vdata)
            return policy.pick_option(req.options, out)
        rec = _Perm(policy.PendingPermission(timeout=self.permission_timeout))
        detail = "指令：%s\n種類：%s\n原因：%s" % (cmd, req.kind, d.reason)
        with self._lock:
            item = self.store.add_inbox(self.room_id, "permission", "%s 要執行：%s" % (who, cmd[:120]),
                                        detail, OPT_PERMISSION)
            self._perms[item.id] = rec
            if self._pause_ev.is_set() and rec.pending.answer("reject"):  # 登記前剛好暫停
                rec.by = "pause"
        if not self._pause_ev.is_set():
            self._set(status="waiting_user", activity="等你：允許「%s」嗎？" % cmd[:60])
        self._event("permission", "交給你決定：%s（%s）" % (cmd, d.reason), outcome="escalate",
                    item_id=item.id, who=who, **vdata)
        tried = [self._delegate_gen]

        def tick() -> None:  # 等你的時候開啟了代管：立刻替你決定（你先回答的話以你為準）
            if tried[0] == self._delegate_gen or not self._delegate_flag or self._pause_ev.is_set():
                return
            tried[0] = self._delegate_gen
            o, why = self._delegate_permission(req, cmd, d)
            with self._lock:
                took = rec.pending.answer(o)
                if took:
                    rec.by, rec.text = "delegate", why
            if not took and not self._pause_ev.is_set():
                self._event("notice", "你先回答了權限請求，代管的決定不採用：%s" % clip(cmd, 120), delegate=True)
        out = rec.pending.wait(on_tick=tick)
        with self._lock:  # answer()/pause() 在同一把鎖內先決定結果再記下是誰
            self._perms.pop(item.id, None)
            idle = not self._perms and not self._waiters
            by, text = rec.by, rec.text
        if out == "timeout":
            msg = "%s沒有回應，已拒絕" % _secs_text(self.permission_timeout)
            self._resolve(item.id, "reject", msg)
            self._event("permission", "%s：%s" % (msg, cmd), outcome="timeout", item_id=item.id, who=who)
            out = "reject"
        elif by == "pause":
            self._resolve(item.id, "reject", "房間已暫停，請求已取消")
            self._event("permission", "暫停時自動拒絕：%s" % cmd, outcome="reject", item_id=item.id, who=who)
        elif by == "delegate":
            self._resolve(item.id, out, "代管：" + text)
            self._event("permission", "代管%s：%s（%s）" % ("允許一次" if out == "allow" else "拒絕", cmd, text),
                        outcome=out, item_id=item.id, who=who, delegate=True)
        elif by == "stop":
            self._resolve(item.id, "reject", "房間已停止，請求已取消")
            self._event("permission", "房間停止時自動拒絕：%s" % cmd, outcome="reject", item_id=item.id, who=who)
        else:
            self._resolve(item.id, out, text)
            self._event("permission", "你%s：%s" % ("允許" if out == "allow" else "拒絕", cmd),
                        outcome=out, item_id=item.id, who=who)
        if idle and not self._pause_ev.is_set() and not self._stopped:
            self._set(status="running", activity="")
        return policy.pick_option(req.options, out)

    def _resolve(self, item_id: str, answer: str, text: str) -> None:
        try:
            self.store.resolve(item_id, answer, text)
        except Exception:
            log.exception("無法解決收件匣項目")

    def _open(self, seat: str, model: str, read_only: bool) -> Any:
        """開一個驅動並 start；失敗丟 _StartFailed（含處理建議）。"""
        label = _label(seat)
        tmpdir = os.path.join(self.store.root, self.room_id, "seat")
        try:
            cli, env, extra = seat_launch(seat, self.clis, self.model_cfg, self.plugin_dirs,
                                          self._wt.path, tmpdir)
        except ValueError as e:
            raise _StartFailed("%s 不可用：%s" % (label, e))
        for v in (env or {}).values():
            if isinstance(v, str) and len(v) >= 6:
                self._secrets.add(v)
        override = self.cmd_overrides.get(seat) or self.cmd_overrides.get(cli.name)
        with self._lock:
            self._drv_seq += 1
            src = {"seq": self._drv_seq, "drv": None}  # 工具合併鍵用：哪一次開的驅動、哪個對話
        drv = make_driver(cli, self._wt.path, lambda ev: self._on_event(label, ev, src),
                          lambda req: self._on_permission(req, read_only, label), read_only,
                          cmd_override=override, env=env, extra_args=extra, model=model,
                          allowed_tools=() if read_only else self._test_tools())
        src["drv"] = drv
        try:
            drv.start()
        except Exception as e:
            self._close(drv)
            raise _StartFailed("無法啟動 %s：%s。%s" % (label, e, login_hint(seat)), raw=str(e))
        self._note_models(seat, drv)
        return drv

    def _test_tools(self) -> List[str]:
        """寫入座位（claude／GLM headless）額外放行的 Bash 樣式：房間的測試指令。"""
        pat = runner_tool_pattern(self.room.test_cmd or "")
        return [pat] if pat else []

    def _sync_test_tools(self) -> None:
        """測試指令定案後，更新進行中的實作座位（下一回合生效）。"""
        setter = getattr(self._impl, "set_allowed_tools", None)
        if callable(setter):
            try:
                setter(self._test_tools())
            except Exception:
                log.debug("更新 allowedTools 失敗", exc_info=True)

    def _note_models(self, seat: str, drv: Any) -> None:
        """ACP 的 session/new 本來就回報可用模型：順手更新偵測快取（不多花 token）。示範房不記。"""
        client = getattr(drv, "client", None)
        models = getattr(client, "available_models", None)
        if self.prefs is None or self.room.demo or seat not in ACP_SEATS or not models:
            return
        try:
            self.prefs.note_detected(seat, models, getattr(client, "current_model", ""))
        except Exception:
            log.debug("更新模型偵測快取失敗", exc_info=True)

    @staticmethod
    def _close(drv: Any) -> None:
        if drv is None:
            return
        try:
            drv.close()
        except Exception:
            log.debug("關閉驅動失敗", exc_info=True)

    def _close_all(self) -> None:
        with self._lock:
            drivers = [self._impl, self._review]
            self._impl = self._review = None
        self._reject_perms("stop")  # 執行器結束：還在等你的權限請求一律拒絕（由等待的執行緒解決項目）
        for d in drivers:
            self._close(d)

    def _run_turn(self, drv: Any, who: str, text: str) -> str:
        """送一個回合；回傳這回合代理的回覆。失敗丟 _TurnFailed（並已關閉驅動）。"""
        self._check_pause()
        with self._lock:
            self._para, self._turn_paras, self._turn_errors = [], [], []
        try:
            stop = drv.prompt(text, self.turn_timeout)
        except AcpError as e:
            self._flush(who)
            last = self._turn_paras[-1] if self._turn_paras else ""
            self._record_cli(drv, ok=False)  # 沒有好好結束的回合也照實記下 CLI 回報的用量
            self._drop(drv)
            if self._pause_ev.is_set():
                raise _Paused()
            err = str(e)
            raise _TurnFailed(err, _is_quota(err, last))
        self._flush(who)
        if self._pause_ev.is_set():
            self._record_cli(drv, ok=False)
            raise _Paused()
        with self._lock:
            paras, errors = list(self._turn_paras), list(self._turn_errors)
        if stop == "error" or errors:
            err = "\n".join(errors) or "代理回報這一回合失敗"
            last = paras[-1] if paras else ""
            self._record_cli(drv, ok=False)
            self._drop(drv)
            raise _TurnFailed(err, _is_quota(err, last))
        reply = "\n\n".join(paras)
        self._record_cli(drv)
        return reply

    def _record_cli(self, drv: Any, ok: bool = True) -> None:
        """回合結束：記下 CLI 回報的用量（kind "cli_usage"）。沒回報的欄位維持 None，不估算。
        ok=False：回合沒有好好結束（失敗、額度用完、要登入、暫停）；有回報數字才記，並標 ok=false。"""
        fn = getattr(drv, "turn_usage", None)
        try:
            u = fn() if fn is not None else None
        except Exception:
            log.debug("讀取 CLI 用量失敗", exc_info=True)
            u = None
        if u is None:
            u = {}
        if not ok and all(u.get(k) is None for k in ("in", "out", "cost")):
            return  # 失敗且什麼都沒回報：不多記一個空回合
        with self._lock:
            role = "review" if drv is self._review else "impl"
        seat = self.room.review_seat if role == "review" else self.room.impl_seat
        if not seat:
            return
        assigned = self.room.review_model if role == "review" else self.room.impl_model
        rec = {"room_id": self.room_id, "seat": seat, "role": role, "model": str(u.get("model") or ""),
               "in": u.get("in"), "out": u.get("out"), "in_cache": u.get("in_cache"), "cost": u.get("cost"),
               "currency": u.get("currency") or "", "ok": ok}
        if self.room.demo:
            rec["demo"] = True
        tokens = dict(self.room.tokens or {})
        tokens[seat] = merge_tokens(tokens.get(seat), rec)
        # 依座位＋模型分開累計（模型＝CLI 回報的；沒回報就用派給它的模型，再沒有就是 CLI 預設）
        mkey = "%s|%s" % (seat, rec["model"] or assigned or "")
        by_model = dict(self.room.tokens_models or {})
        by_model[mkey] = dict(merge_tokens(by_model.get(mkey), rec), seat=seat,
                              model=rec["model"] or assigned or "", reported=bool(rec["model"]))
        actual = "review_model_actual" if role == "review" else "impl_model_actual"
        self._set(**{"tokens": tokens, "tokens_models": by_model, actual: rec["model"]})
        shown = dict(rec, total_in=tokens[seat]["in"], total_out=tokens[seat]["out"],
                     context_pct=u.get("context_pct"))
        self._event("cli_usage", cli_event_text(shown), seat=seat, role=role, model=rec["model"],
                    tokens_in=rec["in"], tokens_out=rec["out"], tokens_in_cache=rec["in_cache"], cost=rec["cost"],
                    currency=rec["currency"], total_in=tokens[seat]["in"], total_out=tokens[seat]["out"], ok=ok)
        if self.usage is not None:
            try:
                self.usage.record_cli(rec)
            except Exception:
                log.debug("寫入用量帳失敗", exc_info=True)

    def _drop(self, drv: Any) -> None:
        with self._lock:
            if self._impl is drv:
                self._impl = None
            if self._review is drv:
                self._review = None
        self._close(drv)

    # ================= 附錄 A：派工、用量備援、切回 =================
    def _pinned(self, cli: str) -> bool:
        return bool(self.room.user_pinned and cli and cli != "auto")

    def _task_text(self) -> str:
        r = self.room
        crit = "；".join("%s %s" % (c.get("id", ""), c.get("text", "")) for c in (r.criteria or []))
        return ("目標：%s\n驗收條件：%s" % (r.goal, crit or "（尚未定）"))[:1500]

    def _ctx(self, role: str, purpose: str, **extra: Any) -> Dict[str, Any]:
        """給 ModelRouter 的呼叫脈絡（只含房間 id、角色、目的；不含提示詞或金鑰）。"""
        return dict(extra, room_id=self.room_id, role=role, purpose=purpose)

    def _prob(self, state: str, question: str, ctx: Optional[Dict[str, Any]] = None) -> Optional[float]:
        try:
            return call_ctx(self.router.probability, state, question, ctx=ctx)
        except Exception as e:  # 路由器不應丟例外；保險起見當成不可用
            log.info("機率判斷失敗：%s", type(e).__name__)
            return None

    def _now(self) -> datetime:
        return datetime.now()

    def _exhausted(self, seat: str) -> bool:
        return bool(self.quota and self.quota.is_exhausted(seat, self._now()))

    def _purpose(self, key: str) -> str:
        return purpose_of(self.prefs if self.prefs is not None else getattr(self.router, "purposes", None), key)

    def _pin(self, role: str) -> Tuple[str, str]:
        """這間房你指定的（座位, 模型）；沒指定座位 → ("auto", "")。模型只在座位有指定時有效。"""
        r = self.room
        cli = (r.impl_cli if role == "impl" else r.review_cli) or "auto"
        pref = (r.impl_pref if role == "impl" else r.review_pref) or {}
        model = str(pref.get("model") or "") if isinstance(pref, dict) and pref.get("seat") == cli else ""
        return cli, model

    def _model_problem(self, a: Assignment) -> str:
        """你指定的模型在不在可用清單；ACP 座位還沒偵測過 → 先放行，開對話後由 session/new 的清單確認。"""
        if not a.model_by or not a.model or self.prefs is None or self.room.demo:
            return ""
        try:
            if a.seat in ACP_SEATS and self.prefs.available(a.seat) is None:
                return ""
            return self.prefs.problem(a.seat, a.model)
        except Exception:
            log.debug("驗證模型失敗", exc_info=True)
            return ""

    def _ask_model(self, role: str, a: Assignment, why: str) -> None:
        """你指定的模型不能用：不送出、不默默改用別的，問你。retry → 呼叫端重新檢查；auto → 清掉房間指定。"""
        where = "這間房的設定" if a.model_by == "room" else "設定頁「模型」的座位設定"
        detail = ("%s座位 %s 指定的模型「%s」不能用：%s\n\n這個模型來自%s。不會自動改用別的模型。"
                  "請到設定頁「模型」按「重新偵測」或改選模型，再按「重新檢查」；"
                  "或選「%s」。" % (ROLE_TEXT.get(role, role), _label(a.seat), a.model, why,
                                                            where, ROOM_AUTO))
        self._event("notice", "%s模型「%s」不能用：%s" % (ROLE_TEXT.get(role, role), a.model, why),
                    role=role, seat=a.seat, model=a.model)
        ans, _ = self._ask("question", MODEL_TITLE[role], detail, OPT_MODEL)
        if ans == "pause":
            raise _Paused()
        if ans == "auto":
            self._clear_room_model(role)

    def _clear_room_model(self, role: str) -> None:
        """「這間房改用自動選擇」：清掉房間指定的模型，並且這間房不再套用設定頁的座位模型（no_global）。"""
        key = "impl_pref" if role == "impl" else "review_pref"
        pref = dict(getattr(self.room, key) or {})
        if pref.get("model"):
            pref["model"] = ""
        pref["no_global"] = True
        self._set(**{key: pref})
        self._event("prefs", "這間房的%s模型改回自動（不套用設定頁指定的模型）" % ROLE_TEXT.get(role, role),
                    role=role, model="")

    def _want(self, phase: str, cli: str, exclude: str = "", model: str = "",
              role: str = "impl") -> Optional[Assignment]:
        """回傳想要的座位；final_review 且使用者指定審查座位（或最後審查設定為關閉）時回 None（不另外審）。
        你指定的模型不在可用清單 → 問你（不送出），回答後重新檢查。"""
        while True:
            a = self._want_once(phase, cli, exclude, model, role)
            why = self._model_problem(a) if a is not None else ""
            if not why:
                return a
            self._ask_model(role, a, why)
            self.room = self.store.get(self.room_id) or self.room  # 你可能剛改了房間設定
            if cli != "auto":
                cli, model = self._pin(role)

    def _use_global(self, role: str) -> bool:
        pref = (self.room.impl_pref if role == "impl" else self.room.review_pref) or {}
        return not (isinstance(pref, dict) and pref.get("no_global"))

    def _want_once(self, phase: str, cli: str, exclude: str, model: str, role: str = "impl") -> Optional[Assignment]:
        pinned = self._pinned(cli)
        ug = self._use_global(role)
        if self.dispatcher is None:
            if phase == "final_review":
                return None
            if not cli or cli == "auto":
                raise _Fatal("沒有派工主管，必須指定座位")
            return Assignment(cli, model, "standard", "user", "使用者指定", model_by="room" if model else "")
        try:
            if pinned:
                if phase == "final_review":
                    return None
                if self._exhausted(cli):
                    return self.dispatcher.fallback(cli)
                return self.dispatcher.pick(phase, self._task_text(), exclude=exclude, user_seat=cli,
                                            ctx={"room_id": self.room_id}, user_model=model, use_global=ug)
            if phase == "final_review" and self._purpose("final_review") not in ("glm_high", "glm_normal"):
                return None
            if ug:
                return self.dispatcher.pick(phase, self._task_text(), exclude=exclude, ctx={"room_id": self.room_id})
            return self.dispatcher.pick(phase, self._task_text(), exclude=exclude, ctx={"room_id": self.room_id},
                                        use_global=False)
        except NoSeatError as e:
            raise _NoSeat(str(e))

    def _assign_event(self, role: str, a: Assignment) -> None:
        mtxt = a.model or "CLI 預設"
        if a.model_by:
            mtxt += "，你指定"
        self._event("assign", "%s：%s（模型 %s）— %s" % ("實作" if role == "impl" else "審查", _label(a.seat),
                                                      mtxt, a.reason),
                    role=role, seat=a.seat, model=a.model, by=a.by, reason=a.reason, model_by=a.model_by)
        key = "impl" if role == "impl" else "review"
        fields = {key + "_by": a.by, key + "_reason": a.reason}
        r = self.room
        if ((r.impl_seat, r.impl_model) if key == "impl" else (r.review_seat, r.review_model)) != (a.seat, a.model):
            fields[key + "_model_actual"] = ""  # 換了座位或模型：等新的回報
        self._set(**fields)

    def _ensure_impl(self, phase: str) -> None:
        r = self.room
        if r.fallback_from:
            if self._exhausted(r.fallback_from):
                if self._impl is None:  # 繼續執行：沿用備援座位
                    self._switch_impl(Assignment(r.impl_seat or "glm", r.impl_model, "standard", "fallback",
                                                 "%s 仍然用量已滿" % _label(r.fallback_from)), "接續")
                return
            self._event("restore", "%s 已恢復，切回原座位" % _label(r.fallback_from), seat=r.fallback_from)
            self._set(fallback_from="")
        cli, model = self._pin("impl")
        a = self._want(phase, cli, model=model, role="impl")
        if a.by == "fallback":
            self._set(fallback_from=r.impl_cli)
        if self._impl is None or a.seat != self.room.impl_seat:
            self._switch_impl(a, "換手（%s）" % a.reason)
        elif a.model != self.room.impl_model:
            self._change_model(a)
        self._applied_impl = self._impl_pref_key()

    def _impl_pref_key(self) -> Tuple[Any, ...]:
        r = self.room
        glob = ""
        if self.prefs is not None and r.impl_seat and self._use_global("impl"):
            try:
                glob = self.prefs.seat_model(r.impl_seat)
            except Exception:
                glob = ""
        return (r.impl_cli, dict(r.impl_pref or {}), glob)

    def _prefs_changed(self) -> None:
        """回合開始：你中途改了這間房（或設定頁）的實作模型 → 這一回合就換（ACP 用 session/set_model）。"""
        if self._impl is None or self._applied_impl is None or self.room.fallback_from:
            return
        fresh = self.store.get(self.room_id)
        if fresh is None:
            return
        self.room = fresh
        if self._impl_pref_key() != self._applied_impl:
            self._ensure_impl(self._dispatch_phase())

    def _change_model(self, a: Assignment) -> None:
        """同一座位換模型：ACP 用 session/set_model、headless 下一回合帶 --model（都接續同一段對話）；
        做不到（代理拒絕、要改回 CLI 預設）就開新對話。"""
        fn = getattr(self._impl, "set_model", None)
        ok = False
        if fn is not None and a.model:
            try:
                ok = bool(fn(a.model))
            except Exception:
                ok = False
        if not ok:
            self._switch_impl(a, "換模型（%s）" % (a.model or "CLI 預設"))
            return
        self._assign_event("impl", a)
        self._set(impl_model=a.model, impl_model_actual="")

    def _switch_impl(self, a: Assignment, reason: str) -> None:
        self._need_rollover = False  # 新對話本來就從交接摘要重新開始
        old = self._impl
        with self._lock:
            self._impl = None
        self._close(old)
        self._assign_event("impl", a)
        while True:
            try:
                drv = self._open(a.seat, a.model, read_only=False)
                if a.model_by and getattr(drv, "model_failed", False) and not self.room.demo:
                    self._close(drv)
                    self._ask_model("impl", a, "代理不接受這個模型（session/set_model 失敗）")
                    cli, model = self._pin("impl")
                    a = self._want(self._dispatch_phase(), cli, model=model, role="impl")
                    self._assign_event("impl", a)
                    continue
                break
            except _StartFailed as e:
                ans = self._access_blocked(a.seat, e.raw)
                if ans is None:
                    raise _Fatal(str(e))
                if ans == "switch":
                    a = self._want(self._dispatch_phase(), "auto", exclude=a.seat)
                    self._assign_event("impl", a)
        with self._lock:
            self._impl = drv
        sess = getattr(getattr(drv, "client", None), "session_id", "") or ""
        self._set(impl_seat=a.seat, impl_model=a.model, impl_session=sess)
        if self.room.criteria:
            try:
                log_text = gitops.git(self._wt.path, "log", "--oneline", self._wt.base + "..HEAD")
            except GitError:
                log_text = ""
            self._prefix = personas.handoff(self.room, log_text, self._last_reply, reason)

    # ================= 登入 =================
    def _dispatch_phase(self) -> str:
        return "build" if self.room.phase in ("build", "test", "review") else "spec"

    def _network_problem(self, seat: str, text: str, why: str) -> str:
        """不是（或無法確認是）登入問題：多半是公司網路的 TLS 檢查（CLI 不認得公司根憑證）或網路斷線，開登入視窗沒有用。
        why：logged_in（CLI 說已登入）／unknown（無法確認登入狀態）／network（錯誤本身就是網路或憑證）。
        放一個問題項目說明憑證，回答「重試」→ "retry"；「我來處理」→ 暫停。"""
        label = _label(seat)
        first = {"logged_in": "%s 顯示已登入，但連到服務時仍被要求登入" % label,
                 "unknown": "無法確認 %s 的登入狀態，可能是網路或憑證" % label,
                 "network": "%s 連線失敗，看起來是網路或憑證問題" % label}[why]
        user = netcerts.user_cert_file()
        if user:
            cert = "目前用你設定的憑證檔：%s（SSL_CERT_FILE／NODE_EXTRA_CA_CERTS）。" % user
        else:
            path = netcerts.bundle_path()
            cert = ("辦公室會把 Windows 憑證存放區（ROOT＋CA）匯出到 %s，並以 SSL_CERT_FILE 與 NODE_EXTRA_CA_CERTS "
                    "傳給 CLI%s。" % (path, "" if os.path.isfile(path) else "（這個檔案目前不存在：匯出失敗或不是 Windows）"))
        detail = ("%s：\n%s\n\n"
                  "這可能是網路或憑證問題，不是登入問題：公司網路做 TLS 檢查時，CLI 常不認得公司的根憑證（UnknownIssuer），"
                  "開登入視窗也沒有用。%s\n請確認網路正常；若仍失敗：向 IT 取得公司根憑證（PEM），設定環境變數 "
                  "SSL_CERT_FILE 指向它，重新啟動助理後按「重試」。" % (first, text[:500], cert))
        ans, _ = self._ask("question", "%s 連不上服務（可能是網路或憑證）" % label, detail, OPT_NETWORK)
        if ans != "retry":
            raise _Paused()
        return "retry"

    def _after_login(self, seat: str, ans: str) -> None:
        """實作座位登入後：retry → 同一座位開新對話接續；switch → 讓派工主管另選座位。"""
        if ans == "retry":
            r = self.room
            self._switch_impl(Assignment(seat, r.impl_model, "standard", r.impl_by or "user", "登入後接續"),
                              "%s 登入後接續" % _label(seat))
            return
        self._switch_impl(self._want(self._dispatch_phase(), "auto", exclude=seat),
                          "%s 未登入，改用其他座位" % _label(seat))

    def _access_blocked(self, seat: str, text: str) -> Optional[str]:
        """錯誤文字像要登入 → _login_blocked；只是網路／憑證（UnknownIssuer、dispatch failure…，沒有登入字樣）
        → 網路與憑證說明（任何座位，GLM 也是）。都不是 → None（照舊當錯誤）。"""
        if is_login_error(text):
            ans = self._login_blocked(seat, text)
            if ans is not None or not is_network_error(text):
                return ans
        if is_network_error(text):
            return self._network_problem(seat, text, "network")
        return None

    def _login_blocked(self, seat: str, text: str) -> Optional[str]:
        """座位需要登入：回傳 "retry"（已登入，重試同一座位）或 "switch"（改用其他座位）；
        不是需要登入的座位（GLM）回 None（照舊當錯誤）。等待中暫停 → _Paused。

        同一座位所有房間共用一個未解決的 login 項目；Office 開登入視窗、背景確認、解決項目並通知各房間。"""
        if seat not in LOGIN_SEATS:
            return None
        label = _label(seat)
        if is_network_error(text):
            return self._network_problem(seat, text, "network")
        if self.login_checker is not None:
            # 先確認 CLI 自己是否已登入（kiro：kiro-cli whoami）；已登入或無法確認 → 不開登入視窗
            self._check_pause()
            try:
                ok = self.login_checker(seat)[0]
            except Exception:
                log.info("登入檢查失敗", exc_info=True)
                ok = None
            self._check_pause()
            if ok is True:
                return self._network_problem(seat, text, "logged_in")
            if ok is None:
                return self._network_problem(seat, text, "unknown")
        try:
            cmd = login_command(seat, self.clis)
            how = cmd[1] if cmd else "請依該 CLI 的說明登入"
        except ValueError as e:
            how = str(e)
        title = "%s 需要登入" % label
        detail = ("%s 回報需要登入：\n%s\n\n按「開啟登入視窗」：%s。登入完成後會自動繼續（每 5 秒檢查一次）。\n"
                  "「改用其他座位」會把受影響的房間改成自動派座位，並暫停 %s（所有房間），直到登入成功或 60 分鐘後再試。"
                  % (label, text[:500], how, label))
        scope = "demo" if self.room.demo else ""
        while True:
            item, created = self.store.join_login(self.room_id, seat, scope, title, detail, OPT_LOGIN)
            w = _Waiter(item.id, {"ok", "switch"})
            with self._lock:
                self._waiters[item.id] = w
            self._set(status="waiting_user", persona="", activity=title)
            self._event("inbox", title if created else title + "（和其他房間共用同一個登入項目）",
                        item_id=item.id, inbox_kind="login", seat=seat)
            last = time.monotonic()
            try:
                while not w.ev.wait(0.2):
                    if self._pause_ev.is_set():
                        break
                    if time.monotonic() - last < LOGIN_DISK_CHECK:
                        continue
                    last = time.monotonic()
                    it = self.store.get_item(item.id)
                    if it is None or it.resolved:  # Office 已解決但沒通知到（或項目被封存等其他路徑解決）
                        with self._lock:
                            if not w.done:
                                w.done = True
                                w.answer = it.answer if it is not None and it.answer in w.valid else ""
                        break
            finally:
                with self._lock:
                    self._waiters.pop(item.id, None)
            if not w.done:
                raise _Paused()
            if w.answer:
                break
            # 項目被別的原因解決（例如擁有它的房間被封存）：重新登記
        self._set(status="running", activity="")
        if w.answer == "switch":
            if self.room.user_pinned:
                self._set(user_pinned=False)
            return "switch"
        return "retry"

    def _quota_fallback(self, text: str) -> None:
        seat = self.room.impl_seat
        if self.dispatcher is None or self.quota is None or seat in GLM_SEATS:
            raise _Fatal("%s 用量已滿，沒有可接手的座位：%s" % (_label(seat), text[:300]))
        until = self.quota.mark_exhausted(seat, text, self._now())
        self._event("quota", "%s 用量已滿（%s 再試），改由 GLM（一般）接手" % (_label(seat), until.strftime("%H:%M")),
                    seat=seat, until=until.isoformat())
        try:
            a = self.dispatcher.fallback(seat)
        except NoSeatError as e:
            raise _NoSeat(str(e))
        if not self.room.fallback_from:
            self._set(fallback_from=seat)
        self._switch_impl(a, "%s 用量已滿" % _label(seat))

    # ================= 實作座位回合 =================
    def _impl_turn(self, role: str, text: str) -> str:
        self._check_pause()
        if self._impl is None:
            self._ensure_impl("build" if self.room.phase in ("build", "test", "review") else "spec")
        else:
            self._prefs_changed()
        if self._need_rollover:
            self._rollover()
        said = self._take_user_msgs()
        if said:
            text = said + "\n\n" + text
        full = text
        while True:
            if self._prefix:
                full = self._prefix + "\n\n" + text
                self._prefix = ""
            self._set(persona=role)
            self._event("prompt", self._mclip(full, 300), role=role, who=_label(self.room.impl_seat),
                        body=self._mclip(text, 300) if full is not text else "", detail={"sent": self._mclip(full, 8000)})
            try:
                reply = self._run_turn(self._impl, _label(self.room.impl_seat), full)
                break
            except _TurnFailed as e:
                if e.quota:
                    self._quota_fallback(e.text)
                    full = text
                    continue
                seat = self.room.impl_seat
                ans = self._access_blocked(seat, e.text)
                if ans is None:
                    raise _Fatal("%s 這一回合失敗：%s。%s" % (_label(seat), e.text[:500], login_hint(seat)))
                self._after_login(seat, ans)
                full = text
        self._last_reply = reply
        self._after_turn()
        return reply

    def _last_para(self) -> str:
        with self._lock:
            return self._turn_paras[-1] if self._turn_paras else ""

    # ================= 附錄 B：對話用量換手 =================
    def _context_size(self) -> int:
        cat = self.dispatcher.catalog if self.dispatcher else {}
        for e in cat.get(self.room.impl_seat, []) or []:
            if e.get("id", "") == (self.room.impl_model or "") and e.get("context"):
                try:
                    return int(e["context"])
                except (TypeError, ValueError):
                    pass
        return DEFAULT_CONTEXT

    def _after_turn(self) -> None:
        if self._impl is None:
            return
        frac, est = context_fraction(self._impl, self._context_size())
        self._set(context_pct=round(frac * 100, 1), context_estimated=est)
        if frac >= self.context_rollover:
            self._need_rollover = True

    def _notes_dir(self) -> str:
        r = self.room
        if r and r.notes_dir:
            os.makedirs(r.notes_dir, exist_ok=True)
            return r.notes_dir
        if self._wt is not None:
            branch = getattr(self._wt, "branch", "") or ""
            repo = getattr(self._wt, "repo", "") or (r.repo if r else "") or getattr(self._wt, "path", "")
            cand = branch.split("/", 1)[-1] if "/" in branch else (r.slug if r else "task")
            return notes.notes_dir(repo, cand)
        return notes.notes_dir(r.repo, r.slug)

    def _purge_notes(self) -> None:
        if self._wt is None or not getattr(self._wt, "path", ""):
            return
        nd = self._notes_dir()
        removed = notes.purge_worktree(self._wt, nd)
        for p in removed:
            self._event("notes_purge", "移除助理工作文件：%s" % p, path=p)
        try:
            notes.mirror(nd, self.store._dir(self.room_id))
        except Exception:
            pass

    def _progress_rel(self) -> str:
        return "progress.md"

    def _progress_blob(self) -> str:
        path = os.path.join(self._notes_dir(), self._progress_rel())
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def _progress_ok(self, before: str) -> bool:
        """notes 目錄裡的 progress.md 比存檔前新，且內容有全部章節。"""
        text = self._progress_blob()
        if not text or text == before:
            return False
        return all(re.search(r"^#+\s*%s\s*$" % re.escape(s), text, re.M) for s in personas.PROGRESS_SECTIONS)

    def _write_progress(self) -> None:
        r = self.room
        try:
            commits = "\n".join("- %s %s" % c for c in gitops.commits_since(self._wt)) or "（還沒有）"
        except GitError:
            commits = "（無法讀取）"
        open_items = [i.title for i in self._room_items(("question", "approve_spec", "unverified", "permission"))]
        nd = self._notes_dir()
        body = {
            "目標": r.goal,
            "驗收條件狀態": "\n".join("- %s %s（檢查：%s）" % (c.get("id"), c.get("text"), c.get("check") or "manual")
                                for c in r.criteria or []) or "（尚無）",
            "已完成（commit）": commits,
            "正在做": "階段：%s；%s" % (r.phase, r.activity or ""),
            "下一步": "先讀 %s/spec.md 與 plan.md，依計畫完成尚未通過的條件；測試指令：%s"
                    % (nd, r.test_cmd or "（未定）"),
            "未決問題": "\n".join("- " + t for t in open_items) or "（無）",
            "注意事項": "代理未存檔，由助理依紀錄代寫。只改允許範圍：%s"
                    % ("、".join(r.allowed_paths) if r.allowed_paths else "整個 repo"),
        }
        text = "# 進度（%s）\n\n" % r.slug + "".join(
            "## %s\n%s\n\n" % (s, body[s]) for s in personas.PROGRESS_SECTIONS)
        self._write_doc(self._progress_rel(), text)

    def _rollover(self) -> None:
        self._need_rollover = False
        pct = self.room.context_pct
        seat, model = self.room.impl_seat, self.room.impl_model
        self._say("scrum", "對話用量 %.0f%%，存檔後換新對話" % pct)
        before = self._progress_blob()
        if self._impl is not None:
            try:
                save = personas.save_progress(self.room)
                self._event("prompt", "（存檔指令）", role="scrum", who=_label(seat),
                            detail={"sent": self._mclip(save, 8000)})
                self._run_turn(self._impl, _label(seat), save)
            except _TurnFailed as e:
                self._event("notice", "存檔回合失敗：%s" % e.text[:300])
        self._check_pause()
        self._purge_notes()
        if not self._progress_ok(before):
            self._write_progress()
            self._event("notice", "代理未存檔，由助理代寫 %s" % self._progress_rel())
        old = self._impl
        with self._lock:
            self._impl = None
        self._close(old)
        drv = None
        while drv is None:
            try:
                drv = self._open(seat, model, read_only=False)
            except _StartFailed as e:
                ans = self._access_blocked(seat, e.raw)  # 要登入／網路或憑證 → 收件匣說明，不直接失敗
                if ans is None:
                    raise _Fatal(str(e))
                if ans == "switch":  # 未登入：改用其他座位（_switch_impl 會寫交接說明）
                    self._switch_impl(self._want(self._dispatch_phase(), "auto", exclude=seat),
                                      "%s 未登入，改用其他座位" % _label(seat))
                    break
        if drv is not None:
            with self._lock:
                self._impl = drv
        self._prefix = personas.resume_from_progress(self.room) + (
            "\n\n" + self._prefix if drv is None and self._prefix else "")
        self._set(rollovers=self.room.rollovers + 1, context_pct=0.0, context_estimated=True)
        self._event("rollover", "對話用量 %.0f%%，已存檔並開新對話" % pct, pct=pct, seat=seat)

    # ================= 文件 =================
    def _doc_rel(self, name: str) -> str:
        return os.path.basename(name)

    def _write_doc(self, rel: str, text: str) -> None:
        nd = self._notes_dir()
        full = os.path.join(nd, os.path.basename(rel))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            notes.mirror(nd, self.store._dir(self.room_id))
        except Exception:
            pass

    def _commit_docs(self, pathspec: str, msg: str) -> None:
        wt = self._wt.path
        try:
            gitops.git(wt, "add", "--", pathspec)
            if not gitops.git(wt, "diff", "--cached", "--name-only", "--", pathspec).strip():
                return
            # 助理的文件提交不跑專案的 hook（pre-commit 可能要裝工具或改檔）
            gitops.git(wt, *gitops.IDENTITY, "commit", "-q", "--no-verify", "-m", msg, "--", pathspec)
        except GitError as e:
            raise _Fatal("提交 %s 失敗：%s" % (pathspec, e))

    # ================= spec（PO） =================
    @staticmethod
    def _parse_spec(reply: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        data = personas.parse_json_block(reply)
        if data is None:
            return None, ["回覆裡沒有可解析的 ```json 區塊"]
        issues = []
        crit = data.get("criteria")
        out = []
        if not isinstance(crit, list) or not crit:
            issues.append("criteria 必須是非空的陣列")
        else:
            for i, c in enumerate(crit):
                if not isinstance(c, dict) or not str(c.get("id") or "").strip() or not str(c.get("text") or "").strip():
                    issues.append("第 %d 條驗收條件缺少 id 或 text" % (i + 1))
                    continue
                out.append({"id": str(c["id"]).strip(), "text": str(c["text"]).strip(),
                            "check": str(c.get("check") or "manual").strip()})
        test_cmd = data.get("test_cmd")
        if not isinstance(test_cmd, str) or not test_cmd.strip():
            issues.append("缺少 test_cmd（跑整套測試的指令）")
        allowed = data.get("allowed_paths")
        if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
            issues.append("allowed_paths 必須是字串陣列")
        if issues:
            return None, issues
        return {"criteria": out, "test_cmd": test_cmd.strip(),
                "allowed_paths": [x.strip() for x in allowed if x.strip()]}, []

    def _kickoff_context(self) -> str:
        try:
            files = gitops.git(self._wt.path, "ls-files").splitlines()
        except GitError:
            files = []
        more = "\n…（還有 %d 個）" % (len(files) - 150) if len(files) > 150 else ""
        return "專案：%s\n工作來源：%s\n檔案（前 150 個）：\n%s%s" % (
            self.room.name or os.path.basename(self.room.repo), self.room.source, "\n".join(files[:150]), more)

    def _role_note(self, role: str, purpose: str, material: str) -> str:
        """glm-5.3 低思考量替角色寫 ≤ 200 字的說明；寫進事件（角色發言）。模型不可用 → 回 "" 並註明用範本。"""
        self._check_pause()
        self._note_label = ""
        mode = self._purpose("notes")
        if mode == "off":
            self._event("say", personas.sign(role, "%s%s" % (purpose, NOTES_OFF)), role=role, note="template")
            return ""
        effort = "normal" if mode == "glm_normal" else "low"
        ctx = self._ctx(role, purpose, note=True)
        try:
            text = call_ctx(self.router.write, personas.note_prompt(role, self.room.goal, material), effort=effort,
                            ctx=ctx)
        except Exception as e:
            log.info("%s 失敗：%s", purpose, type(e).__name__)
            text = ""
        self._check_pause()
        note = " ".join(str(text or "").split())
        if len(note) > NOTE_MAX:
            note = note[:NOTE_MAX - 1] + "…"
        if not note:
            self._event("say", personas.sign(role, "%s%s" % (purpose, NO_MODEL)), role=role, note="template")
            return ""
        # 標示實際回答的模型（ModelCall 記的：伺服器回報的版本，或示範假模型），不是設定檔裡的名字
        model = str(ctx.get("model_used") or getattr(self.router, "glm_name", "glm-5.3"))
        label = note_label(model, str(ctx.get("effort_used") or effort))
        self._note_label = label
        self._event("say", personas.sign(role, "%s（%s）：%s" % (purpose, label, note)), role=role,
                    note="model", note_model=model)
        return note

    def _noted(self, template: str, role: str, note: str) -> str:
        return personas.with_note(template, role, note, self._note_label or getattr(self.router, "glm_name", "glm-5.3"))

    def _spec(self) -> None:
        if not self.room.criteria:
            self._phase("spec", "spec", "po", "PO 起草驗收條件")
            context = self._kickoff_context()
            note = self._role_note("po", "PO 補充說明", context)
            first = self._noted(personas.kickoff(self.room.goal, context, self.room.slug), "po", note)
            spec = self._draft_spec(first)
            self._store_spec(spec)
        self._approve()

    def _draft_spec(self, first_prompt: str) -> Dict[str, Any]:
        goal = self.room.goal
        text = first_prompt
        fixes = quality = 0
        while True:
            reply = self._impl_turn("po", text)
            spec, issues = self._parse_spec(reply)
            if issues:
                if fixes >= MAX_SPEC_FIXES:
                    self._ask_retry("PO 拿不到完整的驗收條件", "代理的回覆缺少：\n" + "\n".join(issues))
                    fixes = 0
                    text = first_prompt
                    continue
                fixes += 1
                self._say("po", "驗收條件格式有問題：" + "；".join(issues))
                text = personas.revise_spec(issues, goal=goal)
                continue
            manual = _unchecked_ids(spec["criteria"])
            if manual:  # 沒有可執行檢查的條件：用同樣的修訂次數要求補上；用完仍沒有就保留並標「未自動驗證」
                if fixes < MAX_SPEC_FIXES:
                    fixes += 1
                    msg = "這些條件沒有可執行的檢查指令（check）：%s。每條都要給能在專案根目錄執行、成功時結束碼為 0 的指令" \
                          % "、".join(manual)
                    self._say("po", msg)
                    text = personas.revise_spec([msg], goal=goal)
                    continue
                self._say("po", "%s：%s（保留，合併前請你自己確認）" % (UNCHECKED, "、".join(manual)))
            p = self._prob(personas.spec_quality_question(spec["criteria"], goal=goal), SPEC_QUALITY,
                           self._ctx("qa", "檢查驗收條件品質", pass_min=SPEC_QUALITY_MIN))
            self._check_pause()
            if p is not None and p < SPEC_QUALITY_MIN and quality < MAX_QUALITY_ROUNDS:
                quality += 1
                msg = "有些驗收條件無法用指令客觀驗證（具體度 %.2f）：每條 check 都要是可執行、成功時結束碼為 0 的指令" % p
                self._say("po", msg)
                text = personas.revise_spec([msg], goal=goal)
                continue
            if p is None:
                self._event("notice", "判斷模型不可用，略過驗收條件具體度檢查")
            return spec

    def _store_spec(self, spec: Dict[str, Any]) -> None:
        allowed = list(self.room.allowed_paths or []) or spec["allowed_paths"]
        self._set(criteria=spec["criteria"], test_cmd=spec["test_cmd"], allowed_paths=allowed)
        self._sync_test_tools()
        for a in self._spec_alerts() + self._spec_notes():
            self._event("notice", a)
        self._write_doc(self._doc_rel("spec.md"), self._spec_md())
        self._event("spec", "驗收條件 %d 條；測試指令：%s" % (len(spec["criteria"]), spec["test_cmd"]))

    def _spec_md(self) -> str:
        r = self.room
        lines = ["# 驗收規格：%s" % r.slug, "", "## 目標", r.goal, "", "## 驗收條件"]
        for c in r.criteria:
            if _is_unchecked(c):
                lines.append("- **%s** %s  \n  檢查：%s（沒有可執行的檢查指令）" % (c["id"], c["text"], UNCHECKED))
            else:
                lines.append("- **%s** %s  \n  檢查：`%s`" % (c["id"], c["text"], c["check"]))
        lines += ["", "## 測試指令", "`%s`" % r.test_cmd, "", "## 允許修改",
                  "\n".join("- `%s`" % p for p in r.allowed_paths) or "- 整個 repo（.git 與金鑰檔除外）", ""]
        return "\n".join(lines)

    def _spec_alerts(self) -> List[str]:
        """代理寫的 test_cmd 與每條 check 會以 shell 執行：命中危險規則或在指令位置用 git → 要使用者確認。"""
        try:
            from automation import check_deny  # 延後匯入：automation 會載入 Windows UI 自動化模組
        except Exception:
            return ["無法載入危險規則檢查，驗收指令需要你確認"]
        r = self.room
        cmds = [(r.test_cmd or "", False)] + [((c.get("check") or "").strip(), True) for c in r.criteria or []]
        out = []  # type: List[str]
        for cmd, is_check in cmds:
            if not cmd or cmd.lower() == "manual":
                continue
            hit = check_deny([cmd], self.deny_patterns)
            if hit:
                out.append(deny_issue(cmd, hit))
            elif git_problem(cmd):
                out.append("驗收%s有問題：%s（%s）" % ("檢查" if is_check else "測試指令", git_problem(cmd), cmd))
            elif not is_check and interpreter_head(cmd):
                out.append("測試指令以直譯器／外殼開頭（%s），可執行任意程式碼，請你確認：%s"
                           % (interpreter_head(cmd), cmd))
        return list(dict.fromkeys(out))

    def _spec_notes(self) -> List[str]:
        """不強制確認的提醒：測試指令不在允許清單 → 代理不能自己跑（助理仍會在關卡執行）。"""
        cmd = (self.room.test_cmd or "").strip()
        if cmd and not runner_tool_pattern(cmd):
            return ["測試指令不在允許清單，代理不能自己執行（助理仍會在關卡執行）：%s" % cmd]
        return []

    def _approve(self) -> None:
        alerts = self._spec_alerts()
        if self.room.autonomy == "bold" and not alerts:
            self._event("notice", "積極模式：驗收條件自動採用，直接開工")
            return
        if alerts and self.room.autonomy == "bold":
            self._event("notice", "驗收指令需要你確認，積極模式也先不開工")
        while True:
            alerts = self._spec_alerts()
            items = self._room_items(("approve_spec",), resolved=True)
            latest = items[-1] if items else None
            if latest is not None and latest.resolved and latest.answer == "start":
                return
            if latest is not None and not latest.resolved:
                ans, text = self._ask("approve_spec", latest.title, latest.detail, latest.options, item=latest)
            else:
                if alerts:
                    ans, text = self._ask("approve_spec", "驗收指令需要你確認，開工嗎？",
                                          "\n".join(alerts + self._spec_notes()) + "\n\n" + self._spec_md(),
                                          OPT_SPEC)
                else:
                    ans, text = self._ask("approve_spec", "PO 起草了驗收條件，開工嗎？", self._spec_md(), OPT_SPEC)
            if ans == "start":
                return
            if self._asked_by == "delegate":
                self._phase("spec", "spec", "po", "PO 依代管審查意見修改驗收條件")
                prompt = ("【代管審查意見】" + (text or "請修改驗收條件").strip() + "\n\n"
                          + personas.revise_spec(["依照上面代管審查的意見調整驗收條件"], goal=self.room.goal))
            else:
                self._phase("spec", "spec", "po", "PO 依你的意見修改驗收條件")
                prompt = ("【你】" + (text or "請修改驗收條件").strip() + "\n\n"
                          + personas.revise_spec(["依照上面使用者的修改意見調整驗收條件"], goal=self.room.goal))
            spec = self._draft_spec(prompt)
            self._store_spec(spec)

    # ================= 計畫（Tech Lead） =================
    def _plan_missing(self) -> List[str]:
        path = os.path.join(self._notes_dir(), self._doc_rel("plan.md"))
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return ["%s 不存在" % self._doc_rel("plan.md")]
        missing = personas.plan_missing(text, self.room.criteria)
        return ["計畫沒有對應這些條件：" + "、".join(missing)] if missing else []

    def _plan(self) -> None:
        self._phase("plan", "spec", "tech_lead", "Tech Lead 寫實作計畫")
        self._purge_notes()
        if not self._plan_missing():
            self._event("notice", "計畫已涵蓋所有驗收條件")
            return
        text = personas.write_plan(self.room.slug, self.room.criteria, goal=self.room.goal)
        fixes = 0
        while True:
            self._impl_turn("tech_lead", text)
            self._purge_notes()
            issues = self._plan_missing()
            if not issues:
                return
            if fixes >= MAX_PLAN_FIXES:
                self._ask_retry("計畫沒有涵蓋所有驗收條件", "\n".join(issues))
                fixes = 0
            fixes += 1
            self._say("tech_lead", "；".join(issues))
            text = personas.fix("tech_lead", issues, goal=self.room.goal)

    # ================= build（Scrum Master） =================
    def _build(self, first: Optional[str]) -> None:
        self._phase("build", "build", "scrum", "實作中")
        goal = self.room.goal
        text = first or personas.build(self.room.slug, self.room.criteria, [], goal=goal)
        turns = 0
        while True:
            if turns >= self.max_build_turns:
                self._ask_retry("實作超過 %d 回合仍未完成" % self.max_build_turns,
                                "代理最後的回覆：\n" + self._last_reply[-1500:])
                turns = 0
            self._impl_turn("scrum", text)
            turns += 1
            if self.room.pending_fix:
                self._set(pending_fix="")
            try:
                issues = build_issues(self._wt, self.room.allowed_paths)
            except GitError as e:
                raise _Fatal("無法檢查 worktree：%s" % e)
            if issues:
                self._say("scrum", "實作查核發現問題：" + "；".join(i.splitlines()[0] for i in issues))
                text = personas.fix("scrum", issues, goal=goal)
                continue
            if _DONE.search(self._last_para()):
                return
            p = self._prob(self._progress_state(), PROGRESS_QUESTION,
                           self._ctx("scrum", "判斷是否偏離目標", pass_min=PROGRESS_MIN))
            self._check_pause()
            if p is not None and p < PROGRESS_MIN:
                text = self._nudge()
            else:
                text = personas.build(self.room.slug, self.room.criteria, [], goal=goal)

    def _progress_state(self) -> str:
        try:
            commits = "\n".join("%s %s" % c for c in gitops.commits_since(self._wt))
        except GitError:
            commits = ""
        return "%s\n已完成的 commit：\n%s\n代理最後的回覆：\n%s" % (self._task_text(), commits, self._last_reply[-2000:])

    def _nudge(self) -> str:
        r = self.room
        try:
            msg = call_ctx(self.router.write, personas.nudge_prompt(r.goal, self._last_reply, r.criteria, []),
                           effort="low", ctx=self._ctx("scrum", "寫督促訊息"))
            msg = msg.strip()
        except Exception as e:
            log.info("寫提醒失敗：%s", type(e).__name__)
            msg = ""
        self._check_pause()
        generated = msg
        msg = msg or "請回到目標，依 plan.md 完成尚未通過的驗收條件。"
        sent = personas.sign("scrum", "%s\n\n目標（全文）：\n%s\n\n全部條件完成並 commit 後，只回覆一行 DONE。"
                             % (msg, r.goal))
        # 督促：模型產生的原文與實際送給代理的完整訊息都留在事件細節（展開看得到）
        self._event("say", personas.sign("scrum", msg), role="scrum", nudge=True,
                    detail={"generated": self._mclip(generated) if generated else "（模型沒有產生，用固定範本）",
                            "sent": self._mclip(sent, 8000)})
        return sent

    # ================= test（QA） =================
    def _test(self) -> Optional[str]:
        """通過回 None；不通過回要送給代理的修正提示。"""
        self._phase("test", "test", "qa", "QA 在 worktree 跑測試")
        self._purge_notes()
        r = self.room
        gate = test_gate(self._wt, r.test_cmd, r.criteria, cancel=self._pause_ev,
                         deny_patterns=self.deny_patterns)
        accepted, self._unverified_accepted = self._unverified_accepted, False  # 只用在這一次關卡
        if gate.cancelled:
            raise _Paused()
        note = ""
        if gate.issues:
            n = self._bump_attempts("test")
            self._say("qa", "測試關卡沒過（第 %d 次）：\n%s" % (n, "\n".join(gate.issues)))
            note = self._role_note("qa", "QA 失敗摘要", "\n".join(gate.issues))
            return self._gate_failed("test", n, self._noted(personas.fix("qa", gate.issues, goal=r.goal), "qa", note),
                                     "測試修了 %d 次仍未通過" % (n - 1), "\n\n".join(gate.issues)[:4000])
        if gate.unverified:
            self._set(unverified=gate.unverified)
            self._say("qa", "無法驗證測試是否有效：" + gate.unverified.splitlines()[0])
            who = "使用者"
            if not accepted:
                ans, text = self._ask("unverified", "QA 無法驗證測試是否有效，請你確認", gate.unverified,
                                      OPT_UNVERIFIED)
                if ans == "strengthen":  # 代管：審查模型認為測試不可信 → 要求代理補強（計入測試修正次數）
                    self._set(unverified="")
                    n = self._bump_attempts("test")
                    fix = self._strengthen_fix(gate.unverified, text)
                    return self._gate_failed("test", n, fix, "測試修了 %d 次仍未通過" % (n - 1), gate.unverified[:4000])
                if ans != "accept":
                    raise _Paused()
                who = "代管" if self._asked_by == "delegate" else "使用者"
                self._event("notice", "%s確認測試有效" % who)
            self._set(unverified="")
            note = "%s確認測試有效（%s）" % (who, gate.unverified.splitlines()[0])
        elif not gate.ok:
            raise _Fatal("測試關卡結果不一致（沒有缺口也沒有通過）")
        self._say("qa", "測試關卡通過" + ("（%s）" % note if note else ""))
        self._write_evidence(gate, note)
        return None

    def _strengthen_fix(self, unverified: str, notes: str) -> str:
        issues = ["QA 無法證明測試有效：%s" % (unverified.splitlines()[0] if unverified else "測試沒有檢查到改動"),
                  "代管審查：%s" % (notes or "測試不夠可信"),
                  "請補強測試，讓測試真的檢查這次的改動（把改動換回原狀時測試要會失敗）"]
        return personas.fix("qa", issues, goal=self.room.goal)

    def _gate_failed(self, key: str, n: int, fix: str, title: str, detail: str) -> str:
        """先把結果存起來（回 build、待送的修正），再決定要不要問你；重啟後直接送修正，不重跑關卡。"""
        self._set(phase="build", pending_fix=fix)
        if n > self.max_fix_attempts:
            self._ask_retry(title, detail)
            self._reset_attempts(key)
        return fix

    def _write_evidence(self, gate: Any, note: str) -> None:
        r = self.room
        head = gitops.git(self._wt.path, "rev-parse", "--short", "HEAD").strip()
        lines = ["# 證據：%s" % r.slug, "", "- 時間：%s" % datetime.now().isoformat(timespec="seconds"),
                 "- 檢查的版本：%s（分支 %s）" % (head, r.branch), "", "## 助理自己跑的檢查", ""]
        for res in gate.results:
            lines += ["### `%s`" % res.cmd, "- 結束碼：%s%s；%.1f 秒" % (res.exit_code, "（逾時）" if res.timed_out else "",
                                                                   res.seconds),
                      "", "```", res.tail[-1500:].strip(), "```", ""]
        lines += ["## 換回原狀檢查", note or "通過：把改動換回原狀後測試會失敗，測試確實檢查了這次的改動", ""]
        manual = _unchecked_ids(r.criteria)
        if manual:
            lines += ["## %s" % UNCHECKED, "這些條件沒有可執行的檢查指令，助理無法驗證，合併前請你自己確認：%s"
                      % "、".join(manual), ""]
        self._write_doc(self._doc_rel("evidence.md"), "\n".join(lines))

    # ================= review（Reviewer） =================
    def _review_stage(self) -> Optional[str]:
        """通過回 None（房間 done）；不通過回要送給代理的修正提示。"""
        self._phase("review", "review", "reviewer", "Reviewer 審查改動")
        crit = "\n".join("%s %s（檢查：%s）" % (c.get("id"), c.get("text"), c.get("check") or "manual")
                         for c in self.room.criteria or [])
        note = self._role_note("reviewer", "Reviewer 審查重點",
                               "驗收條件：\n%s\n\n改動統計：\n%s" % (crit, gitops.diff_stat(self._wt)))
        used = ""
        while True:
            blocking, used = self._review_once("review", note=note)
            if blocking is not None:
                break
            self._ask_retry("審查沒有得到結果", "審查座位與 glm-5.3 都沒有回傳可判讀的審查結果")
        if not blocking:
            final, _ = self._review_once("final_review", skip_seat=used, note=note)
            if final is None:
                self._event("notice", "合併前最後審查沒有結果，以前一次審查為準")
            blocking = final or []
        if blocking:
            n = self._bump_attempts("review")
            self._say("reviewer", "審查發現必須修正的問題（第 %d 次）：\n%s" % (n, "\n".join("- " + b for b in blocking)))
            return self._gate_failed("review", n, personas.fix("reviewer", blocking, goal=self.room.goal),
                                     "審查修了 %d 次仍有問題" % (n - 1), "\n".join(blocking)[:4000])
        self._finish()
        return None

    def _review_once(self, phase: str, skip_seat: str = "", note: str = "") -> Tuple[Optional[List[str]], str]:
        """回傳（blocking 或 None＝沒有結果, 使用的座位）。final_review 不需要時回 ([], "")。"""
        cli, model = self._pin("review")
        a = self._want(phase, cli, exclude=self.room.impl_seat, model=model, role="review")
        if a is None or (phase == "final_review" and a.seat == skip_seat):
            return [], ""
        prompt = self._noted(personas.review(self.room.goal, _marked(self.room.criteria), gitops.diff_stat(self._wt),
                                             gitops.diff_text(self._wt)), "reviewer", note)
        first = a.seat  # 退回模型審查時也以這個座位計，最後審查不再找同一個座位
        tried = set()  # type: Set[str]
        while a is not None and a.seat not in tried:
            tried.add(a.seat)
            self._assign_event("review", a)
            self._set(review_seat=a.seat, review_model=a.model)
            label = _label(a.seat)
            nxt = None
            try:
                drv = self._open(a.seat, a.model, read_only=True)
                if a.model_by and getattr(drv, "model_failed", False) and not self.room.demo:
                    self._close(drv)
                    self._ask_model("review", a, "代理不接受這個模型（session/set_model 失敗）")
                    cli, model = self._pin("review")
                    tried.discard(a.seat)
                    nxt = self._want(phase, cli, exclude=self.room.impl_seat, model=model, role="review")
                    a = nxt
                    continue
                with self._lock:
                    self._review = drv
                try:
                    self._event("prompt", self._mclip(prompt, 300), role="reviewer", who=label,
                                detail={"sent": self._mclip(prompt, 8000)})
                    reply = self._run_turn(drv, label, prompt)
                finally:
                    self._drop(drv)
                blocking = self._parse_review(reply)
                if blocking is not None:
                    self._suggestions_event(reply)
                    return blocking, a.seat
                self._event("notice", "%s 的審查回覆沒有可判讀的 JSON" % label)
            except _StartFailed as e:
                self._event("notice", str(e))
                if self._access_blocked(a.seat, e.raw) == "retry":
                    tried.discard(a.seat)
                    nxt = a
            except _TurnFailed as e:
                self._event("notice", "%s 審查失敗：%s" % (label, e.text[:300]))
                if not e.quota and self._access_blocked(a.seat, e.text) == "retry":
                    tried.discard(a.seat)
                    nxt = a
                elif e.quota and self.quota is not None and self.dispatcher is not None and a.seat not in GLM_SEATS:
                    until = self.quota.mark_exhausted(a.seat, e.text, self._now())
                    self._event("quota", "%s 用量已滿（%s 再試），審查改由 GLM（一般）接手"
                                % (label, until.strftime("%H:%M")), seat=a.seat, until=until.isoformat())
                    try:
                        nxt = self.dispatcher.fallback(a.seat)
                    except NoSeatError:
                        nxt = None
            a = nxt
        self._check_pause()
        self._event("notice", "審查座位不可用，改用 %s 審查" % getattr(self.router, "glm_name", "glm-5.3"))
        try:
            text = call_ctx(self.router.write, prompt, effort="normal",
                            ctx=self._ctx("reviewer", "代替審查座位審查改動"))
        except Exception as e:
            self._check_pause()
            self._event("notice", "glm 審查失敗：%s" % type(e).__name__)
            return None, first
        self._check_pause()
        blocking = self._parse_review(text)
        if blocking is not None:
            self._suggestions_event(text)
        return blocking, first

    @staticmethod
    def _parse_review(text: str) -> Optional[List[str]]:
        data = personas.parse_json_block(text)
        if data is None or not isinstance(data.get("blocking"), list):
            return None
        return [str(b).strip() for b in data["blocking"] if str(b).strip()]

    def _suggestions_event(self, text: str) -> None:
        data = personas.parse_json_block(text) or {}
        sugg = [str(s) for s in data.get("suggestions") or [] if str(s).strip()]
        self._say("reviewer", "審查通過" if not data.get("blocking") else "審查有意見")
        if sugg:
            self._event("notice", "審查建議：" + "；".join(sugg)[:1000])
        self._suggestions = sugg

    def _finish(self) -> None:
        self._purge_notes()
        sugg = self._suggestions
        rel = self._doc_rel("evidence.md")
        full = os.path.join(self._notes_dir(), rel)
        try:
            with open(full, encoding="utf-8") as f:
                ev = f.read()
        except OSError:
            ev = "# 證據：%s\n" % self.room.slug
        ev += "\n## 審查\n- 審查座位：%s\n- 必須修正：無\n%s\n" % (
            _label(self.room.review_seat), "".join("- 建議：%s\n" % s for s in sugg))
        self._write_doc(rel, ev)
        commits = "\n".join("%s %s" % c for c in gitops.commits_since(self._wt))
        stat = gitops.diff_stat(self._wt)
        manual = _unchecked_ids(self.room.criteria)
        unchecked = ("%s：%s（沒有可執行的檢查指令，合併前請你自己確認）\n\n" % (UNCHECKED, "、".join(manual))
                     if manual else "")
        self._set(phase="done", status="done", persona="", activity="全部關卡通過，等你合併")
        self._event("phase", "全部關卡通過", phase="done", persona="")
        self.store.add_inbox(self.room_id, "merge", "「%s」完成，合併到 %s？" % (
            self.room.name or self.room.slug, self.room.target_branch or "主線"),
            "%s分支：%s\n\ncommit：\n%s\n\n改動：\n%s" % (unchecked, self.room.branch, commits, stat), OPT_MERGE)
        self._event("inbox", "等你合併", inbox_kind="merge")
