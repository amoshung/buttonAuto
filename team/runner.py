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
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from team import (
    autotune,
    baseline,
    ce_skills,
    ce_sync,
    diagnostics,
    evidence,
    gitops,
    kb,
    ladder,
    lessons,
    netcerts,
    notes,
    notify,
    personas,
    policy,
    prompts,
    question_router,
    rounds,
)
from team.acp import AcpError
from team.checks import (
    build_issues,
    deny_issue,
    git_problem,
    snapshot_reference_root,
    test_gate,
    verify_reference_untouched,
)
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
_DONE_STANDALONE = re.compile(r"^\s*(?:\*{1,2}|`)?DONE(?:\*{1,2}|`)?[.!。！]?\s*$")
_DONE_NEGATED = re.compile(
    r"(?i)(?:\b(?:not|never|don't|do not|won't|will not|cannot|can't|haven't|hasn't|isn't|aren't|refus(?:e|ing))\b"
    r"[^\n]{0,40}\bDONE\b"
    r"|(?:不|未|沒有|無法|別|勿|拒絕|並非)[^\n]{0,16}DONE)"
)
_FRESH_SESSION_PAT = re.compile(
    r"(?i)(?:"
    r"(?:past|over|exceed(?:ed|s)?|hit|reached?)\s+(?:the\s+)?\d{2,3}%\s+context\s+(?:limit|window)"
    r"|context\s+(?:limit|window)\s+(?:exceeded|reached|full)"
    r"|start\s+a\s+\*{0,2}new\s+conversation\*{0,2}"
    r"|new\s+conversation\s+is\s+needed"
    r"|`/clear`"
    r"|(?:請|需要|必須)(?:開啟|啟動|另開|切換到)新對話"
    r"|對話(?:空間|視窗|容量)已(?:滿|達上限)"
    r")"
)
_REVIEW_DIFF_MAX_CHARS = 40000


def _is_done_reply(para: str) -> bool:
    """判定代理最後一段是否真正宣告 DONE（排除 'not replying DONE' 等否定句與換手拒做回覆）。"""
    text = (para or "").strip()
    if not text or not _DONE.search(text):
        return False
    if _FRESH_SESSION_PAT.search(text):
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if _DONE_STANDALONE.match(ln):
            return True
    if _DONE_NEGATED.search(text):
        return False
    return True


def _requests_fresh_session(reply: str) -> bool:
    """偵測代理是否因 context 上限拒做並要求開啟新對話（即使系統估算用量仍偏低）。"""
    text = (reply or "").strip()
    if not text:
        return False
    return bool(_FRESH_SESSION_PAT.search(text))

OPT_PERMISSION = [{"id": "allow", "label": "允許一次"}, {"id": "reject", "label": "拒絕"}]
OPT_SPEC = [{"id": "start", "label": "開工"}, {"id": "edit", "label": "修改"}]
OPT_RETRY = [{"id": "retry", "label": "再試一次"}, {"id": "pause", "label": "我來處理"}]
OPT_UNVERIFIED = [{"id": "accept", "label": "我確認測試有效"}, {"id": "pause", "label": "我來處理"}]
OPT_MERGE = [{"id": "merge", "label": "合併"}, {"id": "later", "label": "先不要"}]
OPT_ERROR = [{"id": "retry", "label": "重試"}, {"id": "later", "label": "先放著"}]
OPT_LOGIN = [{"id": "open", "label": "開啟登入視窗"}, {"id": "check", "label": "我已登入，重新檢查"},
             {"id": "switch", "label": "改用其他座位"}]
OPT_NETWORK = [{"id": "retry", "label": "重試"}, {"id": "pause", "label": "我來處理"}]
OPT_STUCK = [
    {"id": "edit_criteria", "label": "修改驗收條件"},
    {"id": "mark_env", "label": "標記為環境既有失敗並繼續"},
    {"id": "info", "label": "提供資訊"},
    {"id": "archive", "label": "封存房間"},
]
OPT_STUCK_BUDGET = [{"id": "more_budget", "label": "再給一份預算"}] + OPT_STUCK
OPT_RECOMMEND = [
    {"id": "open", "label": "去能力庫看看"},
    {"id": "ignore", "label": "忽略"},
]
GATE_GAP_SECONDS = 30.0
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
REVIEW_ALIGNMENT_Q = "目前這批開發變更是否仍緊扣使用者的任務目標、沒有偏離方向或引入重大風險？（外部背景工具或其他房間對母資料夾／相鄰目錄的變動與本房間無關，應予忽略）"
REVIEW_ALIGNMENT_MIN = 0.70
DRIFT_OPTIONS = [
    ("aligned", "完全符合目標，沒有偏離方向"),
    ("minor_drift", "有輕微偏離或遺漏細節，需要指出位置讓高層主管與研發工程師決定微調"),
    ("major_drift", "明顯偏離目標或改錯方向，必須立即指出偏離位置交由高層主管拍板修正"),
]


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
                 cmd_overrides: Optional[Dict[str, List[str]]] = None,
                 dispatcher: Optional[Dispatcher] = None,
                 quota: Optional[QuotaBook] = None, model_cfg: Optional[ModelConfig] = None,
                 plugin_dirs: Sequence[str] = (), context_rollover: float = 0.62,
                 turn_timeout: float = 1800, permission_timeout: float = 1800,
                 login_checker: Optional[Callable[[str], Tuple[bool, str]]] = None,
                 usage: Optional[UsageBook] = None, prefs: Any = None,
                 prompts: Optional[prompts.PromptStore] = None,
                 lessons_book: Optional[lessons.LessonBook] = None,
                 budget_default: Optional[Dict[str, Any]] = None,
                 env_dirs: Sequence[str] = (),
                 gate_gap_seconds: float = 0.0,
                 on_stuck: Optional[Callable[[str, str], None]] = None,
                 max_fix_attempts: int = 3,
                 max_build_turns: int = 20):
        super().__init__(name="room-%s" % room_id, daemon=True)
        self.room_id = room_id
        self.store = store
        self.router = router
        self.clis = clis
        self.deny_patterns = list(deny_patterns or [])
        self.on_change = on_change
        self.cmd_overrides = dict(cmd_overrides or {})
        self.dispatcher = dispatcher
        self.quota = quota
        self.model_cfg = model_cfg
        # Task 19：事件細節裡要遮蔽的已知金鑰值（模型設定＋座位啟動環境變數）
        self._secrets = {
            v for v in (
                getattr(model_cfg, "glm_key", ""),
                getattr(model_cfg, "decision1_key", ""),
                getattr(model_cfg, "sovereign_key", ""),
            )
            if isinstance(v, str) and v
        }
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
        self.prompts = prompts if prompts is not None else globals()["prompts"].PromptStore(store.root)
        self.lessons = lessons_book if lessons_book is not None else lessons.LessonBook(store.root)
        self.kb = kb.KnowledgeBase(store.root, config=model_cfg, router=router, secrets=list(self._secrets))
        self.budget_default = dict(budget_default or {})
        self.env_dirs = list(env_dirs or ())
        self.gate_gap_seconds = float(gate_gap_seconds or 0.0)
        self.on_stuck = on_stuck
        self.max_fix_attempts = int(max_fix_attempts)
        self.max_build_turns = int(max_build_turns)
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
        self._last_prompt_sent = ""
        self._unverified_accepted = False
        self._suggestions = []  # type: List[str]
        self._stopped = False
        self._user_msgs = []  # type: List[str]
        self._delegate_flag = bool(getattr(self.room, "delegate", False))  # Task 20：Office 以 set_delegate 更新
        self._delegate_gen = 0  # 每次開啟代管 +1：關掉再開會重新試一次已交給你的項目
        self._asked_by = ""  # 最近一次 _ask 的答案是誰給的：user｜delegate
        self._ladder = ladder.Ladder.from_dict((self.room.ladder if self.room else None) or {})
        self._prompt_versions: Dict[str, str] = {}
        self._roles_used: Set[str] = set()
        self._turn_seconds = 0.0
        self._turn_tokens = 0
        self._turn_tokens_est = 0
        self._last_commits_count = 0
        self._last_gate_at = 0.0
        self._gate_no = len((self.room.rounds if self.room else None) or [])
        self._last_gate_output = ""
        self._last_diagnosis: Dict[str, Any] = {}
        self._last_blocker: Dict[str, Any] = {}
        self._concerns: List[str] = []
        self._env_links: List[Tuple[str, str]] = []
        self._recommended_caps: Set[str] = set()
        self._ref_snapshot: Optional[Dict[str, Tuple[int, int]]] = None
        self._role_comms: List[str] = []
        self._pending_comm_msgs: List[str] = []
        self._supervisor_decisions: List[str] = []
        self._review_round_seq: int = 0
        self._last_review_blocking_fps: Set[str] = set()
        self._perm_reject_streak: int = 0
        self._last_dev_summary: str = ""
        self._last_reviewed_commit_count: int = 0
        self._last_drift_alert: str = ""
        self._last_test_evidence: str = ""
        if self.room is not None and self.budget_default and not self.room.budget:
            self._set(budget=dict(self.budget_default))

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

    def has_pending_user_msgs(self) -> bool:
        """檢查是否仍有尚未交付給研發工程師的使用者新指示／新目標。"""
        with self._lock:
            return bool(self._user_msgs)

    def receive_user_directive(
        self,
        text: str,
        *,
        goal_updated: bool = False,
        added_criterion_id: str = "",
    ) -> None:
        """高層主管正式接收使用者底部對話指示：同步最新 goal/criteria、寫入三方共享決策並排入研發工程師下一回合。"""
        text = (text or "").strip()
        if not text:
            return
        self.send(text)
        # 立即從 store 同步最新 room.goal 與 room.criteria，讓盤中防偏離與審查工程師即時看到
        self._live_criteria()
        prefix = (
            f"使用者追加目標{f'（新增 {added_criterion_id}）' if added_criterion_id else ''}："
            if goal_updated
            else "使用者指示："
        )
        self._record_supervisor_decision(prefix + text[:180], actor="高層主管")
        self._record_role_comm(
            "user",
            "supervisor",
            prefix + text[:220],
            guidance="高層主管已吸收並同步更新目標與審查標準，交由研發工程師接續完成。",
            queue_for_impl=False,
            emit_say=False,
        )

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
            try:
                self._sync_store_progress()
            except Exception:
                pass
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
        try:
            ce_status = ce_skills.ensure_ce_installed(self.plugin_dirs)
            self._event("notice", "已確認 compound-engineering 技能就緒（Claude / Kiro / AGY / GLM-5.3 全角色已掛載）",
                        ce=ce_status)
        except Exception:
            log.debug("檢查 compound-engineering 技能失敗", exc_info=True)
        r = self.room
        ssh_host = getattr(r, "ssh_host", "") or ""
        in_place = bool(r.target_subdir or r.reference_root)
        if in_place:
            try:
                gitops.ensure_local_git_repo(r.repo, ssh_host=ssh_host)
            except GitError as e:
                raise _Fatal(str(e))
        prob = gitops.repo_problem(r.repo, ssh_host=ssh_host)
        if prob:
            raise _Fatal(prob)
        try:
            wt = gitops.create_worktree(
                r.repo, r.slug, base=r.base, in_place=in_place, ssh_host=ssh_host, skip_repo_check=True
            )
        except GitError as e:
            raise _Fatal(str(e))
        self._wt = wt
        cand = wt.branch.split("/", 1)[-1] if "/" in wt.branch else r.slug
        if ssh_host:
            nd = r.notes_dir or os.path.join(self.store._dir(self.room_id), "notes")
            os.makedirs(nd, exist_ok=True)
        else:
            nd = r.notes_dir or notes.notes_dir(r.repo, cand, in_place=in_place)
        self._set(worktree=wt.path, branch=wt.branch, base=r.base or wt.base,
                  target_branch=r.target_branch or wt.target_branch, notes_dir=nd)
        self._event("worktree", "工作目錄：%s（分支 %s）" % (wt.path, wt.branch))
        if r.reference_root and not ssh_host:
            self._ref_snapshot = snapshot_reference_root(
                r.reference_root, r.repo, exclude_subdir=r.target_subdir, exclude_subdirs=r.target_subdirs
            )
            targets_label = "、".join(r.target_subdirs) if r.target_subdirs else (r.target_subdir or os.path.basename(r.repo))
            self._event("notice", "唯讀參考來源：%s（僅允許修改目標子資料夾 %s）"
                        % (r.reference_root, targets_label),
                        reference_root=r.reference_root, target_subdir=r.target_subdir, target_subdirs=r.target_subdirs)
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

    def _record_supervisor_decision(self, summary: str, *, actor: str = "主管") -> None:
        """將高階主管／代管決策寫入三方共享紀錄，同步供實作、審查與 progress.md 使用。"""
        text = (summary or "").strip()
        if not text:
            return
        entry = f"[{actor}] {text[:260]}"
        with self._lock:
            self._supervisor_decisions.append(entry)
            if len(self._supervisor_decisions) > 20:
                self._supervisor_decisions = self._supervisor_decisions[-20:]
        self._sync_store_progress()

    def _decision(self, text: str, **detail: Any) -> None:
        """決策事件：代管選了什麼、為什麼（展開看得到每個模型的答案）。"""
        self._event("decision", "代管決定：" + text, delegate=True, demo=bool(self.room.demo),
                    detail=self._mask({k: v for k, v in detail.items() if v not in (None, "", [], {})}))
        reason = str(detail.get("reason") or "").strip()
        summary = f"{text}（理由：{reason}）" if reason else text
        self._record_supervisor_decision(summary, actor="高階主管/代管")

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
        """問題：選項類問題由 Decision-1 選選項，描述類問題由 GLM-5.3 結合唯讀參考資料回答；
        同一題連續第 2 次或連續達 MAX_QUESTION_STREAK 次 → 交給你。選「我來處理」也交給你。"""
        cur = self.store.get(self.room_id) or self.room
        if title and title == (cur.delegate_last or ""):
            self._decision("同一個問題連續第 2 次，暫停並交給你：%s" % title, item="問題", title=title, choice="交給你",
                           reason="代管已經替你回答過一次同樣的問題，避免來回打轉（關掉再開代管會重新計算）")
            return None
        streak = int(getattr(cur, "question_streak", 0) or 0)
        if streak >= question_router.MAX_QUESTION_STREAK:
            self._decision("連續問題已達 %d 次上限，暫停並交給你：%s" % (question_router.MAX_QUESTION_STREAK, title),
                           item="問題", title=title, choice="交給你",
                           reason="代管連續回答問題已達上限（%d 次）" % question_router.MAX_QUESTION_STREAK)
            return None
        qkind, eff_options = question_router.classify_question(title, detail, options)
        if qkind == "descriptive":
            res = question_router.route_question(
                self.router,
                goal=cur.goal,
                title=title,
                detail=detail,
                options=options,
                reference_root=getattr(cur, "reference_root", "") or "",
                worktree=self._wt.path if self._wt else "",
                streak=streak,
                ctx=self._dctx("代管：補充描述與參考背景"),
            )
            self._check_pause()
            if res.get("handoff"):
                self._decision("GLM-5.3 無法回答描述類問題，交給你：%s" % title, item="問題", title=title,
                               choice="交給你", reason=res.get("reason") or "描述類問題需要你確認或模型不可用",
                               question_kind="descriptive")
                return None
            ans, text = str(res.get("option_id") or "answer"), str(res.get("answer_text") or "")
            self._set(delegate_last=title, question_streak=streak + 1)
            self._decision("「%s」由 GLM-5.3 回答描述類問題" % title, item="問題", title=title,
                           choice=ans, reason=text[:200],
                           models=getattr(self.router, "glm_name", "glm-5.3"),
                           question_kind="descriptive")
            return ans, text
        labels = {str(o.get("id")): str(o.get("label") or o.get("id")) for o in eff_options}
        state = "%s\n\n問題：%s\n%s\n\n選項：%s" % (self._task_text(), title, detail[:3000],
                                                 "、".join("%s（%s）" % (v, k) for k, v in labels.items()))
        pick, probs = call_ctx(self.router.choose, state, title, eff_options, ctx=self._dctx("代管：選擇選項"))
        self._check_pause()
        nums = "、".join("%s %.2f" % (labels.get(k, k), v) for k, v in probs.items())
        if pick is None or pick not in labels:
            self._decision("Decision-1 無法選擇，交給你：%s" % title, item="問題", title=title, choice="交給你",
                           reason="判斷模型不可用或沒有回答", question_kind="multiple_choice")
            return None
        if pick == "pause":
            self._decision("Decision-1 認為這題要你處理：%s" % title, item="問題", title=title, choice="交給你",
                           reason="選了「%s」" % labels[pick], models=nums, question_kind="multiple_choice")
            return None
        self._set(delegate_last=title, question_streak=streak + 1)
        self._decision("「%s」選「%s」" % (title, labels[pick]), item="問題", title=title, choice=labels[pick],
                       reason="Decision-1 機率最高的選項", models=nums, question_kind="multiple_choice")
        return pick, ""

    def _delegate_permission(self, req: PermissionRequest, cmd: str, d: Any) -> Tuple[str, str]:
        """權限（代管）：規則擋下的（危險規則、超出範圍…）直接拒絕；模型判為灰色或高風險 → 審查模型第二意見＋
        Decision-1 再問一次，都允許才允許一次，否則拒絕。永遠不交給你。回傳（allow|reject, 理由）。"""
        if d.rule or d.verdict is None:
            why = "%s，代管不允許" % d.reason
            self._decision("拒絕「%s」（%s）" % (clip(cmd, 120), d.reason), item="權限", title=cmd, choice="拒絕", reason=why)
            return "reject", why
        v = d.verdict
        paths = policy.target_paths(req)
        raw_snippet = ""
        if req.raw_input:
            try:
                raw_snippet = clip(json.dumps(self._mask(req.raw_input), ensure_ascii=False), 800)
            except Exception:
                raw_snippet = clip(str(self._mask(req.raw_input)), 800)
        allowed_scope = "、".join(self.room.allowed_paths) if self.room.allowed_paths else (self._wt.path if self._wt else "")
        plan_ctx = self._permission_plan_context()
        material = (
            "目標：%s\n"
            "工作範圍：%s\n"
            "允許修改範圍：%s\n"
            "%s"
            "動作種類：%s\n"
            "指令：%s\n"
            "目標路徑：%s\n"
            "請求參數摘要：%s\n"
            "第一次判斷：%s"
            % (
                self.room.goal,
                self._wt.path if self._wt else "",
                allowed_scope or "（整個工作目錄）",
                ("已核定計畫／驗收條件與主管決策：\n%s\n" % plan_ctx) if plan_ctx else "",
                req.kind,
                cmd,
                "、".join(paths) if paths else "（工作目錄內操作）",
                raw_snippet or "（無額外參數）",
                d.reason,
            )
        )
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

    def _record_role_comm(
        self,
        from_role: str,
        to_role: str,
        summary: str,
        guidance: str = "",
        *,
        queue_for_impl: bool = True,
        emit_say: bool = True,
    ) -> str:
        """記錄跨角色協作溝通訊息，讓高層主管、研發工程師與審查工程師雙向傳遞意見、偏離定位與退回原因。"""
        canonical_from = "user" if from_role == "user" else personas.normalize_role_key(from_role)
        canonical_to = "user" if to_role == "user" else personas.normalize_role_key(to_role)
        role_names = {
            "supervisor": "高層主管",
            "developer": "研發工程師",
            "reviewer": "審查工程師",
            "user": "使用者",
        }
        f_label = role_names.get(canonical_from, from_role)
        t_label = role_names.get(canonical_to, to_role)
        msg = "【跨角色協作溝通：%s → %s】\n%s" % (f_label, t_label, summary.strip())
        if guidance.strip():
            msg += "\n👉 下一步建議與行動指引：%s" % guidance.strip()
        with self._lock:
            self._role_comms.append("%s → %s：%s" % (f_label, t_label, summary.strip()[:220]))
            if len(self._role_comms) > 25:
                self._role_comms = self._role_comms[-25:]
            if queue_for_impl and canonical_to in ("developer", "supervisor"):
                self._pending_comm_msgs.append(msg)
                if len(self._pending_comm_msgs) > 10:
                    self._pending_comm_msgs = self._pending_comm_msgs[-10:]
        if emit_say:
            say_role = canonical_from if canonical_from in ("supervisor", "developer", "reviewer") else "supervisor"
            self._event("say", personas.sign(say_role, "%s → %s：%s" % (f_label, t_label, summary.strip()[:260])),
                        role=say_role, comm_from=canonical_from, comm_to=canonical_to)
        self._sync_store_progress()
        return msg

    def _take_role_comms(self) -> str:
        """取出待轉達給實作工程師的跨角色協作回饋（如代管拒絕理由、QA/Reviewer 建議）。"""
        with self._lock:
            if not self._pending_comm_msgs:
                return ""
            items = list(self._pending_comm_msgs)
            self._pending_comm_msgs.clear()
        return "\n\n".join(items)

    def _notify_permission_rejected(
        self, req: PermissionRequest, cmd: str, reason: str, *, by: str = "delegate", read_only: bool = False
    ) -> None:
        """當代管、安全政策或使用者拒絕某個工具／權限請求時，立刻把拒絕原因與具體替代做法反映給 Developer，避免鬼打牆。"""
        if read_only or self._pause_ev.is_set() or self._stopped:
            return
        with self._lock:
            self._perm_reject_streak += 1
            streak = self._perm_reject_streak
        paths = policy.target_paths(req)
        path_info = "（目標路徑：%s）" % "、".join(paths) if paths else "（未偵測到明確目標檔案路徑）"
        allowed_txt = "、".join(self.room.allowed_paths) if self.room.allowed_paths else "工作目錄內檔案"
        ref_txt = (
            "；注意 `%s` 下非目標子資料夾為唯讀參考區，禁止寫入" % self.room.reference_root
            if self.room.reference_root
            else ""
        )
        guidance_parts = [
            "1. 請勿原封不動重複呼叫同一個被拒絕的動作「%s」。" % clip(cmd, 80),
            "2. 允許修改的範圍為：%s%s。" % (allowed_txt, ref_txt),
            "3. 若要修改或新增檔案，請在工具參數中明確填入允許範圍內的相對路徑與完整內容（優先使用精確的局部替換或標準檔案寫入）。",
        ]
        if streak >= 2:
            guidance_parts.append(
                "4. ⚠️【防鬼打牆破局提示】你已連續 %d 次因權限／審查被拒。請立刻改用允許範圍內的替代實作方式；"
                "若確實因範圍限制無法推進，請直接回覆 JSON `{\"status\":\"NEEDS_CONTEXT\",\"question\":\"說明被擋下的操作與原因\",\"detail\":\"...\"}` 與主管溝通！"
                % streak
            )
        from_role = "delegate" if by == "delegate" else ("user" if by == "user" else "scrum")
        self._record_role_comm(
            from_role,
            "implementer",
            "你的操作「%s」%s 已被%s拒絕。拒絕原因：%s"
            % (clip(cmd, 120), path_info, "代管主管" if by == "delegate" else ("使用者" if by == "user" else "安全把關"), reason),
            guidance="\n".join(guidance_parts),
            queue_for_impl=True,
            emit_say=True,
        )

    def _permission_plan_context(self) -> str:
        """組裝目前房間的計畫單元、驗收條件、主管決策與唯讀參考區，供權限判斷依任務動態調整。"""
        r = self.room
        return policy.format_plan_context(
            list(r.criteria or []) if r else [],
            self._supervisor_decisions,
            phase=(r.phase if r else ""),
            reference_root=(r.reference_root if r else ""),
        )

    def _on_permission(self, req: PermissionRequest, read_only: bool, who: str) -> str:
        if self._pause_ev.is_set():
            return policy.pick_option(req.options, "reject")
        cmd = command_text(req)
        wt = self._wt.path if self._wt else ""
        d = policy.decide(req, goal=self.room.goal, scope=wt, deny_patterns=self.deny_patterns,
                          router=self.router, read_only=read_only, ctx=self._ctx("policy", ""),
                          reference_root=self.room.reference_root,
                          plan_context=self._permission_plan_context())
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
            if d.outcome == "allow":
                self._perm_reject_streak = 0
            else:
                self._notify_permission_rejected(req, cmd, d.reason, by="policy", read_only=read_only)
            return d.option_id
        if self._delegate_flag:  # 代管：不交給你（規則擋下的直接拒絕；灰色／高風險再問兩個模型）
            if self._pause_ev.is_set():
                return policy.pick_option(req.options, "reject")
            out, why = self._delegate_permission(req, cmd, d)
            vdata["detail"] = dict(vdata.get("detail") or {}, decision=out, reason="代管：" + why)
            self._event("permission", "代管%s：%s（%s）" % ("允許一次" if out == "allow" else "拒絕", cmd, why),
                        outcome=out, who=who, delegate=True, **vdata)
            if out == "allow":
                self._perm_reject_streak = 0
            else:
                self._notify_permission_rejected(req, cmd, why, by="delegate", read_only=read_only)
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
            self._notify_permission_rejected(req, cmd, msg, by="timeout", read_only=read_only)
        elif by == "pause":
            self._resolve(item.id, "reject", "房間已暫停，請求已取消")
            self._event("permission", "暫停時自動拒絕：%s" % cmd, outcome="reject", item_id=item.id, who=who)
        elif by == "delegate":
            self._resolve(item.id, out, "代管：" + text)
            self._event("permission", "代管%s：%s（%s）" % ("允許一次" if out == "allow" else "拒絕", cmd, text),
                        outcome=out, item_id=item.id, who=who, delegate=True)
            if out == "allow":
                self._perm_reject_streak = 0
            else:
                self._notify_permission_rejected(req, cmd, text, by="delegate", read_only=read_only)
        elif by == "stop":
            self._resolve(item.id, "reject", "房間已停止，請求已取消")
            self._event("permission", "房間停止時自動拒絕：%s" % cmd, outcome="reject", item_id=item.id, who=who)
        else:
            self._resolve(item.id, out, text)
            self._event("permission", "你%s：%s" % ("允許" if out == "allow" else "拒絕", cmd),
                        outcome=out, item_id=item.id, who=who)
            if out == "allow":
                self._perm_reject_streak = 0
            else:
                self._notify_permission_rejected(
                    req, cmd, text or "使用者不同意執行此操作", by="user", read_only=read_only
                )
        if idle and not self._pause_ev.is_set() and not self._stopped:
            self._set(status="running", activity="")
        return policy.pick_option(req.options, out)

    def _resolve(self, item_id: str, answer: str, text: str) -> None:
        try:
            self.store.resolve(item_id, answer, text)
        except Exception:
            log.exception("無法解決收件匣項目")

    def _open(self, seat: str, model: str, read_only: bool, *, fresh_context: bool = False) -> Any:
        """開一個驅動並 start；失敗丟 _StartFailed（含處理建議）。fresh_context=True 時以獨立目錄啟動全新對話。"""
        label = _label(seat)
        if fresh_context:
            with self._lock:
                self._review_round_seq += 1
                seq = self._review_round_seq
            tmpdir = os.path.join(self.store.root, self.room_id, f"seat-fresh-{seq}")
        else:
            tmpdir = os.path.join(self.store.root, self.room_id, "seat")
        ssh_host = getattr(self.room, "ssh_host", "") or ""
        try:
            cli, env, extra = seat_launch(seat, self.clis, self.model_cfg, self.plugin_dirs,
                                          self._wt.path, tmpdir, reference_root=self.room.reference_root,
                                          ssh_host=ssh_host)
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
                          allowed_tools=() if read_only else self._test_tools(),
                          ssh_host=ssh_host)
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

    def _role_model_override(self, role: str) -> str:
        """依房間 role_prefs／supervisor_pref 解析高層主管協作生成模型或角色模型（未指定回 ""）。"""
        r = self.room
        role_prefs = getattr(r, "role_prefs", None) or {}
        canonical = personas.normalize_role_key(role)
        if canonical == "supervisor":
            pref = role_prefs.get("supervisor") or role_prefs.get(role) or getattr(r, "supervisor_pref", None) or {}
        elif canonical == "developer":
            pref = role_prefs.get("impl") or role_prefs.get("developer") or getattr(r, "impl_pref", None) or {}
        elif canonical == "reviewer":
            pref = role_prefs.get("review") or role_prefs.get("reviewer") or getattr(r, "review_pref", None) or {}
        else:
            pref = role_prefs.get(role) or getattr(r, "supervisor_pref", None) or {}
        if isinstance(pref, dict):
            return str(pref.get("model") or "").strip()
        return ""

    def _ctx(self, role: str, purpose: str, **extra: Any) -> Dict[str, Any]:
        """給 ModelRouter 的呼叫脈絡（只含房間 id、角色、目的與可選 model_override；不含提示詞或金鑰）。"""
        ctx = dict(extra, room_id=self.room_id, role=role, purpose=purpose)
        if "model_override" not in ctx:
            override = self._role_model_override(role)
            if override:
                ctx["model_override"] = override
        return ctx

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
                log_text = gitops.git(self._wt.path, "log", "--oneline", self._wt.base + "..HEAD",
                                      ssh_host=self._wt.ssh_host)
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
        role_comms = self._take_role_comms()
        if role_comms:
            text = role_comms + "\n\n" + text
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
            # 若此回合中有新的代管拒絕回饋產生，確保重試時也帶上
            extra_comms = self._take_role_comms()
            if extra_comms:
                text = extra_comms + "\n\n" + text
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
        self._sync_store_progress()
        if frac >= self.context_rollover:
            self._need_rollover = True

    def _notes_dir(self) -> str:
        r = self.room
        if r and r.notes_dir:
            os.makedirs(r.notes_dir, exist_ok=True)
            return r.notes_dir
        if r and getattr(r, "ssh_host", ""):
            p = os.path.join(self.store._dir(self.room_id), "notes")
            os.makedirs(p, exist_ok=True)
            return p
        in_place = bool(r and (r.target_subdir or r.reference_root))
        if self._wt is not None:
            branch = getattr(self._wt, "branch", "") or ""
            repo = getattr(self._wt, "repo", "") or (r.repo if r else "") or getattr(self._wt, "path", "")
            cand = branch.split("/", 1)[-1] if "/" in branch else (r.slug if r else "task")
            return notes.notes_dir(repo, cand, in_place=in_place)
        return notes.notes_dir(r.repo, r.slug, in_place=in_place)

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

    def _target_subdir_arg(self) -> str:
        r = self.room
        if r and r.target_subdirs:
            return ",".join(r.target_subdirs)
        return (r.target_subdir if r else "") or ""

    def _build_progress_markdown(self, extra_note: str = "") -> str:
        r = self.room
        try:
            commits_list = [f"{c[0]} {c[1]}" for c in gitops.commits_since(self._wt)] if self._wt else []
        except GitError:
            commits_list = []
        open_items = [i.title for i in self._room_items(("question", "approve_spec", "unverified", "permission"))]
        try:
            events_tail = self.store.events(self.room_id, limit=12)
        except Exception:
            events_tail = []
        return ce_sync.build_room_progress_markdown(
            r,
            commits=commits_list,
            open_items=open_items,
            events=events_tail,
            extra_note=extra_note,
            role_comms=list(reversed(self._role_comms)),
            supervisor_decisions=list(reversed(self._supervisor_decisions)),
        )

    def _live_criteria(self) -> List[Dict[str, Any]]:
        """從 store 重新同步最新驗收條件，確保中途動態調整的條件能即時反映給 QA 與 Reviewer。"""
        try:
            fresh = self.store.get(self.room_id)
            if fresh is not None and fresh.criteria is not None:
                self.room = fresh
        except Exception:
            log.debug("同步最新驗收條件失敗", exc_info=True)
        return list((self.room.criteria if self.room else None) or [])

    def _compound_learnings(self, query_text: str, *, max_chars: int = 1200) -> str:
        """查詢專案 ce-compound 歷史經驗（LessonBook + KnowledgeBase），供回合中途主動注入。"""
        parts: List[str] = []
        repo = getattr(self.room, "repo", "") if self.room else ""
        if not repo:
            return ""
        try:
            lesson_res = self.lessons.format_for_prompt(repo, max_chars=max(300, max_chars // 2))
            lesson_txt = lesson_res[0] if isinstance(lesson_res, tuple) else str(lesson_res or "")
            if lesson_txt and lesson_txt.strip():
                parts.append(lesson_txt.strip())
        except Exception:
            log.debug("讀取 LessonBook 失敗", exc_info=True)
        q = (query_text or "").strip()
        if q:
            try:
                kb_txt = self.kb.query(repo, q[:400], room_id=self.room_id, top_k=3)
                if kb_txt and kb_txt.strip():
                    parts.append(kb_txt.strip())
            except Exception:
                log.debug("查詢 KnowledgeBase 失敗", exc_info=True)
        merged = "\n\n".join(parts).strip()
        return merged[:max_chars] if len(merged) > max_chars else merged

    def _sync_store_progress(self, extra_note: str = "") -> None:
        """即時寫入 store 目錄下的 progress.md（不覆寫 notes_dir/progress.md 以免干擾 _rollover 存檔檢查）。"""
        try:
            text = self._build_progress_markdown(extra_note=extra_note)
            store_dir = self.store._dir(self.room_id)
            os.makedirs(store_dir, exist_ok=True)
            with open(os.path.join(store_dir, "progress.md"), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except Exception:
            log.debug("即時同步 store progress.md 失敗", exc_info=True)

    def _write_progress(self) -> None:
        text = self._build_progress_markdown()
        self._write_doc(self._progress_rel(), text)
        self._sync_store_progress()

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
        sh = getattr(self._wt, "ssh_host", "")
        try:
            gitops.git(wt, "add", "--", pathspec, ssh_host=sh)
            if not gitops.git(wt, "diff", "--cached", "--name-only", "--", pathspec, ssh_host=sh).strip():
                return
            # 助理的文件提交不跑專案的 hook（pre-commit 可能要裝工具或改檔）
            gitops.git(wt, *gitops.IDENTITY, "commit", "-q", "--no-verify", "-m", msg, "--", pathspec, ssh_host=sh)
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
            files = gitops.git(self._wt.path, "ls-files", ssh_host=self._wt.ssh_host).splitlines()
        except GitError:
            files = []
        more = "\n…（還有 %d 個）" % (len(files) - 150) if len(files) > 150 else ""
        base = "專案：%s\n工作來源：%s\n檔案（前 150 個）：\n%s%s" % (
            self.room.name or os.path.basename(self.room.repo), self.room.source, "\n".join(files[:150]), more)
        try:
            self.kb.sync_repo_compound_docs(self.room.repo, reference_root=self.room.reference_root)
            kb_text = self.kb.query(self.room.repo, self.room.goal or self.room.name, room_id=self.room_id)
            if kb_text:
                base += "\n\n專案 LLM Wiki 知識庫（歷次教訓與交接紀錄）：\n" + kb_text
        except Exception:
            pass
        return base

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
            first = self._noted(
                personas.kickoff(
                    self.room.goal, context, self.room.slug,
                    reference_root=self.room.reference_root,
                    target_subdir=self._target_subdir_arg(),
                ),
                "po", note,
            )
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
                text = personas.revise_spec(
                    issues, goal=goal,
                    reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                )
                continue
            manual = _unchecked_ids(spec["criteria"])
            if manual:  # 沒有可執行檢查的條件：用同樣的修訂次數要求補上；用完仍沒有就保留並標「未自動驗證」
                if fixes < MAX_SPEC_FIXES:
                    fixes += 1
                    msg = "這些條件沒有可執行的檢查指令（check）：%s。每條都要給能在專案根目錄執行、成功時結束碼為 0 的指令" \
                          % "、".join(manual)
                    self._say("po", msg)
                    text = personas.revise_spec(
                        [msg], goal=goal,
                        reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                    )
                    continue
                self._say("po", "%s：%s（保留，合併前請你自己確認）" % (UNCHECKED, "、".join(manual)))
            p = self._prob(personas.spec_quality_question(spec["criteria"], goal=goal), SPEC_QUALITY,
                           self._ctx("qa", "檢查驗收條件品質", pass_min=SPEC_QUALITY_MIN))
            self._check_pause()
            if p is not None and p < SPEC_QUALITY_MIN and quality < MAX_QUALITY_ROUNDS:
                quality += 1
                msg = "有些驗收條件無法用指令客觀驗證（具體度 %.2f）：每條 check 都要是可執行、成功時結束碼為 0 的指令" % p
                self._say("po", msg)
                text = personas.revise_spec(
                    [msg], goal=goal,
                    reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                )
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
                          + personas.revise_spec(
                              ["依照上面代管審查的意見調整驗收條件"], goal=self.room.goal,
                              reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                          ))
            else:
                self._phase("spec", "spec", "po", "PO 依你的意見修改驗收條件")
                prompt = ("【你】" + (text or "請修改驗收條件").strip() + "\n\n"
                          + personas.revise_spec(
                              ["依照上面使用者的修改意見調整驗收條件"], goal=self.room.goal,
                              reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                          ))
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
        self._phase("plan", "spec", "supervisor", "高層主管確認實作計畫")
        self._purge_notes()
        if not self._plan_missing():
            self._event("notice", "計畫已涵蓋所有驗收條件")
            return
        # 若驗收條件已就緒，直接以輕量計畫書定錨以省去多餘的計畫回合，避免 Token 浪費
        if self.room.criteria:
            plan_path = os.path.join(self._notes_dir(), self._doc_rel("plan.md"))
            try:
                os.makedirs(os.path.dirname(plan_path), exist_ok=True)
                lines = [
                    "# 實作計畫（精簡扁平模式）",
                    "",
                    "## 任務目標",
                    self.room.goal or "(未提供)",
                    "",
                    "## 驗收條件對應",
                ]
                for c in self.room.criteria:
                    cid = str(c.get("id") or "").strip()
                    ctext = str(c.get("text") or "").strip()
                    ccheck = str(c.get("check") or "manual").strip()
                    if cid:
                        lines.append("- [%s] %s（驗證：%s）" % (cid, ctext, ccheck))
                with open(plan_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                if not self._plan_missing():
                    self._event("notice", "已由驗收條件直接定錨實作計畫，跳過冗餘計畫回合")
                    return
            except OSError:
                pass
        text = personas.write_plan(
            self.room.slug, self.room.criteria, goal=self.room.goal,
            reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
        )
        fixes = 0
        while True:
            self._impl_turn("supervisor", text)
            self._purge_notes()
            issues = self._plan_missing()
            if not issues:
                return
            if fixes >= MAX_PLAN_FIXES:
                self._ask_retry("計畫沒有涵蓋所有驗收條件", "\n".join(issues))
                fixes = 0
            fixes += 1
            self._say("supervisor", "；".join(issues))
            text = personas.fix(
                "supervisor", issues, goal=self.room.goal,
                reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
            )

    # ================= build（Scrum Master） =================
    def _handle_agent_question(self, data: Dict[str, Any]) -> Optional[str]:
        q_title = str(data.get("question") or data.get("title") or "").strip()
        status = str(data.get("status") or "").strip().upper()
        if not q_title and status != "NEEDS_CONTEXT":
            return None
        q_title = q_title or "代理需要補充背景資訊（NEEDS_CONTEXT）"
        q_detail = str(data.get("detail") or data.get("context") or self._last_reply[-1500:]).strip()
        raw_opts = data.get("options")
        q_opts: List[Dict[str, str]] = []
        if isinstance(raw_opts, list):
            for idx, o in enumerate(raw_opts):
                if isinstance(o, dict) and (o.get("id") or o.get("label")):
                    oid = str(o.get("id") or f"opt_{idx + 1}").strip()
                    q_opts.append({"id": oid, "label": str(o.get("label") or oid).strip()})
                elif isinstance(o, str) and o.strip():
                    q_opts.append({"id": f"opt_{idx + 1}", "label": o.strip()})
        got = self._delegate_question(q_title, q_detail, q_opts)
        if got is not None:
            ans, text_ans = got
            reply_body = text_ans or ans
            self._record_supervisor_decision(
                "回答研發工程師提問「%s」：%s" % (q_title[:80], str(reply_body)[:180]),
                actor="高層主管",
            )
            self._record_role_comm(
                "supervisor",
                "developer",
                "針對提問「%s」高層主管回覆：%s" % (q_title[:80], str(reply_body)[:220]),
                queue_for_impl=False,
                emit_say=True,
            )
            return personas.sign(
                "supervisor",
                "針對你的問題「%s」，決策／補充如下：\n%s\n\n請依此繼續完成實作並 commit，全部完成後回覆一行 DONE。"
                % (q_title, reply_body),
            )
        ask_opts = q_opts if len(q_opts) >= 2 else OPT_RETRY
        ans, text_ans = self._ask("question", q_title, q_detail, ask_opts)
        if ans == "pause":
            raise _Paused()
        user_reply = ("%s %s" % (ans, text_ans)).strip()
        self._record_supervisor_decision(
            "使用者回答研發工程師提問「%s」：%s" % (q_title[:80], user_reply[:180]),
            actor="使用者",
        )
        self._record_role_comm(
            "user",
            "developer",
            "使用者針對「%s」回覆：%s" % (q_title[:80], user_reply[:220]),
            queue_for_impl=False,
            emit_say=True,
        )
        return personas.sign(
            "supervisor",
            "使用者針對「%s」回覆：%s %s\n\n請依此繼續完成實作並 commit，全部完成後回覆一行 DONE。"
            % (q_title, ans, text_ans),
        )

    def _adjudicate_outside_allowed(self, goal: str, issues: List[str]) -> List[str]:
        """當 worktree 內改動超出初期 allowed_paths 時，由高層主管（Decision-1）依目標與計畫自動裁決擴充或明確授權 revert。"""
        if not self._wt or not self.room.allowed_paths:
            return issues
        idx = next((i for i, msg in enumerate(issues) if msg.startswith("改到範圍外的檔案：")), -1)
        if idx < 0:
            return issues
        try:
            files = gitops.changed_files(self._wt)
            outside = gitops.outside_allowed(files, self.room.allowed_paths)
        except GitError:
            return issues
        if not outside:
            return [m for i, m in enumerate(issues) if i != idx]
        plan_ctx = self._permission_plan_context()
        state = (
            f"任務目標：{goal or '（未指定）'}\n"
            f"{('已核定計畫與主管決策：' + chr(10) + plan_ctx + chr(10)) if plan_ctx else ''}"
            f"初期 allowed_paths：{'、'.join(self.room.allowed_paths[:20])}\n"
            f"實際已修改但不在初期清單內的檔案（皆位於本房間允許的 worktree 目標資料夾內，未碰觸其他資料夾）：\n"
            + "\n".join(f"- {p}" for p in outside[:25])
            + f"\n研發工程師最近說明：{self._last_dev_summary or '（無）'}"
        )
        q = (
            "這些修改都位於本房間自己的目標 worktree 內（沒有改動其他專案資料夾）。"
            "依任務目標與已核定計畫來看，這些檔案是否屬於合理且必要的連動修改（例如共用設定、相關模組或對應測試），"
            "應由高層主管直接納入允許範圍（allowed_paths）以利繼續推進，而非退回要求 revert？"
        )
        p = self._prob(
            state,
            q,
            self._ctx("supervisor", "動態裁決範圍外檔案是否納入允許範圍", pass_min=0.70),
        )
        if p is not None and p >= 0.70:
            merged = list(dict.fromkeys(list(self.room.allowed_paths) + list(outside)))
            self._set(allowed_paths=merged)
            summary = "高層主管裁決：依目標與計畫自動將 worktree 內合理檔案納入允許範圍：" + "、".join(outside[:8])
            if len(outside) > 8:
                summary += f" 等共 {len(outside)} 個檔案"
            if summary not in self._supervisor_decisions:
                self._supervisor_decisions.append(summary[:280])
            self._say("supervisor", summary)
            return [m for i, m in enumerate(issues) if i != idx]
        updated = list(issues)
        updated[idx] = updated[idx] + "（高層主管裁決：上述檔案偏離本次目標，明確授權你 revert 這些檔案的改動）"
        return updated

    def _handle_reference_root_changes(self, violated: List[str]) -> None:
        """處理母資料夾 A 快照偵測到的異動：絕不退回給研發工程師（其無權寫入唯讀區），由 Decision-1 判斷是否為外部工具雜訊。"""
        state = (
            f"唯讀參考母資料夾 A：{self.room.reference_root}\n"
            f"本房間目標工作目錄：{self._wt.path if self._wt else self.room.repo}\n"
            f"本房間權限策略已硬性禁止任何寫入目標目錄以外路徑的工具呼叫。\n"
            f"快照比對發現母資料夾 A 出現異動的檔案（最多顯示 15 筆）：\n"
            + "\n".join(f"- {p}" for p in violated[:15])
            + f"\n本回合研發工程師回報摘要：{self._last_dev_summary or '（無）'}"
        )
        q = (
            "考量本房間權限系統已硬性攔截對母資料夾的寫入，這些異動是否幾乎確定是由外部背景程式、"
            "IDE、索引工具或其他平行視窗造成（而非本房間研發工程師刻意修改），可以直接重設快照基準繼續執行？"
        )
        p = self._prob(
            state,
            q,
            self._ctx("supervisor", "判定母資料夾異動是否為外部背景雜訊", pass_min=0.60),
        )
        if p is None or p >= 0.60:
            self._event(
                "notice",
                "偵測到母資料夾 A 外部異動（判定非本房間寫入，已自動重設快照基準繼續執行）："
                + "、".join(violated[:6]) + ("…" if len(violated) > 6 else ""),
            )
            self._ref_snapshot = snapshot_reference_root(
                self.room.reference_root,
                self.room.repo,
                exclude_subdir=self.room.target_subdir,
                exclude_subdirs=self.room.target_subdirs,
            )
            return
        ans, _ = self._ask(
            "question",
            "唯讀參考母資料夾 A 出現未預期異動",
            f"路徑：{self.room.reference_root}\n異動檔案：\n"
            + "\n".join(f"- {v}" for v in violated[:15])
            + "\n\n研發工程師無權修改唯讀區。若確認為外部操作，可重設快照並繼續。",
            [
                {"id": "continue", "label": "確認非本房間問題，重設快照並繼續"},
                {"id": "pause", "label": "先暫停，我來檢查"},
            ],
        )
        if ans != "continue":
            raise _Paused()
        self._ref_snapshot = snapshot_reference_root(
            self.room.reference_root,
            self.room.repo,
            exclude_subdir=self.room.target_subdir,
            exclude_subdirs=self.room.target_subdirs,
        )

    def _resolve_build_deadlock(self, issues: List[str], goal: str) -> bool:
        """連續兩輪出現完全相同查核缺口且 HEAD／工作目錄無變化時觸發熔斷：優先由 Decision-1 裁決豁免，無法自動裁決才交給使用者。"""
        has_git_state_issue = any(
            m.startswith("還沒有 commit") or m.startswith("有未提交的改動：")
            for m in issues
        )
        if not has_git_state_issue:
            plan_ctx = self._permission_plan_context()
            state = (
                f"任務目標：{goal or '（未指定）'}\n"
                f"{('已核定計畫與主管決策：' + chr(10) + plan_ctx + chr(10)) if plan_ctx else ''}"
                f"連續兩輪完全相同且無法由研發工程師自行消除的查核問題：\n"
                + "\n".join(f"- {m}" for m in issues)
                + f"\n研發工程師回覆說明：{self._last_dev_summary or '（無）'}"
            )
            q = (
                "研發工程師已連續兩輪遇到相同查核問題且無未提交變更。"
                "依目前目標、計畫與研發工程師說明來看，這些查核項是否屬於靜態檢查誤報或已合理完成，"
                "可以由高層主管裁決豁免並直接進入後續測試與審查階段？"
            )
            p = self._prob(
                state,
                q,
                self._ctx("supervisor", "死鎖熔斷：裁決是否豁免重複查核項", pass_min=0.72),
            )
            if p is not None and p >= 0.72:
                note = "高層主管死鎖裁決：判定連續重複查核項屬誤報或已合理解釋，予以豁免並進入驗證階段"
                self._supervisor_decisions.append(note)
                self._say("supervisor", note)
                return True
        ans, extra = self._ask(
            "question",
            "實作查核連續 2 輪出現相同缺口（已觸發死鎖熔斷）",
            "以下問題連續兩輪未變，已停止空轉：\n"
            + "\n".join(f"- {m}" for m in issues)
            + (f"\n\n研發工程師最後說明：\n{self._last_dev_summary}" if self._last_dev_summary else ""),
            [
                {"id": "waive", "label": "豁免上述查核項並繼續"},
                {"id": "retry", "label": "退回研發工程師再修一次"},
                {"id": "pause", "label": "先暫停，我來處理"},
            ],
        )
        if extra and extra.strip():
            self._supervisor_decisions.append(f"使用者裁決：{extra.strip()[:240]}")
        if ans == "waive":
            self._say("supervisor", "使用者已裁決豁免本次重複查核項，繼續推進流程")
            return True
        if ans != "retry":
            raise _Paused()
        return False

    def _build(self, first: Optional[str]) -> None:
        self._phase("build", "build", "developer", "研發工程師實作中")
        goal = self.room.goal
        text = first or personas.build(
            self.room.slug, self.room.criteria, [], goal=goal,
            reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
        )
        init_comms = self._take_role_comms()
        if init_comms:
            text = init_comms + "\n\n" + text
        if first is None and self._supervisor_decisions:
            dec_block = "【高層主管已核定決策（請遵守，勿偏離）】\n" + "\n".join(
                "- " + d for d in self._supervisor_decisions[-6:]
            )
            text = dec_block + "\n\n" + text
        turns = 0
        prev_issue_sig = None
        same_issue_streak = 0
        fresh_session_streak = 0
        while True:
            if turns >= self.max_build_turns:
                self._ask_retry("實作超過 %d 回合仍未完成" % self.max_build_turns,
                                "代理最後的回覆：\n" + self._last_reply[-1500:])
                turns = 0
            self._impl_turn("scrum", text)
            if _requests_fresh_session(self._last_reply) and fresh_session_streak < 2:
                fresh_session_streak += 1
                self._event(
                    "notice",
                    "偵測到代理回報對話空間已達上限並要求開新對話（目前系統估算用量 %.1f%%），立即強制存檔換手後重送本回合"
                    % float(self.room.context_pct or 0.0),
                    forced_rollover=True,
                )
                self._rollover()
                continue
            fresh_session_streak = 0
            turns += 1
            # 每回合結束後即時同步最新 goal 與 criteria（確保中途透過對話追加的目標立刻生效）
            self._live_criteria()
            goal = self.room.goal
            last_p = self._last_para().strip()
            if last_p:
                self._last_dev_summary = last_p[:600]
            reply_data = personas.parse_json_block(self._last_reply)
            if isinstance(reply_data, dict):
                raw_concerns = reply_data.get("concerns")
                if isinstance(raw_concerns, list):
                    for c_item in raw_concerns:
                        c_str = str(c_item or "").strip()
                        if c_str and c_str not in self._concerns:
                            self._concerns.append(c_str[:300])
                            if len(self._concerns) > 15:
                                self._concerns = self._concerns[-15:]
                elif isinstance(raw_concerns, str) and raw_concerns.strip():
                    c_str = raw_concerns.strip()[:300]
                    if c_str not in self._concerns:
                        self._concerns.append(c_str)
            if self.room.pending_fix:
                self._set(pending_fix="")
            try:
                issues = build_issues(self._wt, self.room.allowed_paths)
            except GitError as e:
                raise _Fatal("無法檢查 worktree：%s" % e)
            if issues:
                issues = self._adjudicate_outside_allowed(goal, issues)
            if self.room.reference_root and self._ref_snapshot is not None:
                violated = verify_reference_untouched(
                    self._ref_snapshot,
                    self.room.reference_root,
                    self.room.repo,
                    exclude_subdir=self.room.target_subdir,
                    exclude_subdirs=self.room.target_subdirs,
                )
                if violated:
                    self._handle_reference_root_changes(violated)
            if issues:
                try:
                    head_sha = gitops.git(self._wt.path, "rev-parse", "HEAD", ssh_host=self._wt.ssh_host).strip()
                except GitError:
                    head_sha = ""
                sig = (head_sha, tuple(issues))
                if sig == prev_issue_sig:
                    same_issue_streak += 1
                else:
                    prev_issue_sig = sig
                    same_issue_streak = 1
                if same_issue_streak >= 2:
                    if self._resolve_build_deadlock(issues, goal):
                        issues = []
                        prev_issue_sig = None
                        same_issue_streak = 0
                    else:
                        same_issue_streak = 0
            else:
                prev_issue_sig = None
                same_issue_streak = 0
            if issues:
                self._say("supervisor", "實作查核發現問題：" + "；".join(i.splitlines()[0] for i in issues))
                learnings = self._compound_learnings("\n".join(issues))
                text = personas.fix(
                    "supervisor", issues, goal=goal,
                    reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                    compound_learnings=learnings,
                )
                continue
            if _is_done_reply(self._last_para()):
                if self.has_pending_user_msgs():
                    # 使用者在研發工程師執行本回合期間透過對話追加了新目標或指示：不可直接結束，立即續做！
                    self._event("notice", "偵測到使用者於本回合期間追加新目標／指示，暫緩結束並立即交由研發工程師接續完成")
                    text = personas.build(
                        self.room.slug,
                        self._live_criteria(),
                        [],
                        goal=self.room.goal,
                        reference_root=self.room.reference_root,
                        target_subdir=self._target_subdir_arg(),
                    )
                    continue
                if self.room.question_streak:
                    self._set(question_streak=0)
                if self._last_dev_summary:
                    self._record_role_comm(
                        "developer",
                        "reviewer",
                        "研發工程師已完成本階段實作並提交驗證：%s" % self._last_dev_summary[:240],
                        queue_for_impl=False,
                        emit_say=False,
                    )
                return
            if isinstance(reply_data, dict):
                q_next = self._handle_agent_question(reply_data)
                if q_next is not None:
                    text = q_next
                    continue
            if self.room.question_streak:
                self._set(question_streak=0)
            # 盤中增量審查：有新 commit 或每 3 回合存在未提交變更，審查工程師即以 Decision-1 檢核是否偏離目標
            cp_fix = self._checkpoint_review(turns=turns)
            if cp_fix is not None:
                text = cp_fix
                continue
            # 若本回合中有代管拒絕權限或跨角色回饋（例如 Write File 被拒），下一回合立即把拒絕理由與替代指引傳給研發工程師
            pending_comms = self._take_role_comms()
            if pending_comms:
                text = (
                    pending_comms
                    + "\n\n"
                    + personas.build(
                        self.room.slug,
                        self.room.criteria,
                        [],
                        goal=goal,
                        reference_root=self.room.reference_root,
                        target_subdir=self._target_subdir_arg(),
                    )
                )
                continue
            p = self._prob(self._progress_state(), PROGRESS_QUESTION,
                           self._ctx("supervisor", "判斷是否偏離目標", pass_min=PROGRESS_MIN))
            self._check_pause()
            if p is not None and p < PROGRESS_MIN:
                text = self._nudge()
            else:
                text = personas.build(
                    self.room.slug, self.room.criteria, [], goal=goal,
                    reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                )

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
        criteria = self._live_criteria()
        r = self.room
        gate = test_gate(self._wt, r.test_cmd, criteria, cancel=self._pause_ev,
                         deny_patterns=self.deny_patterns)
        accepted, self._unverified_accepted = self._unverified_accepted, False  # 只用在這一次關卡
        if gate.cancelled:
            raise _Paused()
        note = ""
        if gate.issues and gate.env_failures and not gate.new_failures:
            env_state = (
                f"任務目標：{r.goal or '（未指定）'}\n"
                f"純外部環境失敗（env_failures）：\n" + "\n".join(f"- {e}" for e in gate.env_failures[:8]) + "\n"
                f"測試關卡輸出摘要：\n" + "\n".join(gate.issues[:4])[:1600]
            )
            env_q = (
                "這次測試失敗全部歸類為外部環境因素（env_failures，例如外部資料庫／網路／埠號被外部占用或缺少外部服務），"
                "並非本房間程式碼缺陷。高層主管是否應改為紀錄「環境限制審計軌跡」並直接進入審查階段，避免讓研發工程師對外部環境空轉？"
            )
            p_env = self._prob(
                env_state,
                env_q,
                self._ctx("supervisor", "判定純外部環境測試失敗是否豁免退回", pass_min=0.72),
            )
            if p_env is not None and p_env >= 0.72:
                note = "高層主管裁決：純外部環境限制（非本房間程式錯誤），附環境限制審計軌跡並推進審查"
                self._record_supervisor_decision(note, actor="高層主管")
                self._event("notice", note + "：" + "；".join(gate.env_failures[:3]))
                gate.issues = []
                gate.ok = True
        if gate.issues:
            n = self._bump_attempts("test")
            self._say("qa", "測試關卡沒過（第 %d 次）：\n%s" % (n, "\n".join(gate.issues)))
            note = self._role_note("qa", "QA 失敗摘要", "\n".join(gate.issues))
            learnings = self._compound_learnings("\n".join(gate.issues))
            self._record_role_comm(
                "supervisor",
                "developer",
                "測試關卡第 %d 次未通過：%s" % (n, "；".join(i.splitlines()[0] for i in gate.issues[:4])),
                guidance=note or "請對照測試失敗輸出修復並確認本地測試結束碼為 0 後再 commit。",
                queue_for_impl=False,
                emit_say=True,
            )
            return self._gate_failed(
                "test",
                n,
                self._noted(
                    personas.fix(
                        "qa", gate.issues, goal=r.goal,
                        reference_root=r.reference_root, target_subdir=self._target_subdir_arg(),
                        compound_learnings=learnings,
                    ),
                    "qa",
                    note,
                ),
                "測試修了 %d 次仍未通過" % (n - 1),
                "\n\n".join(gate.issues)[:4000],
            )
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
            self._record_supervisor_decision(
                "核准放行測試驗證關卡（%s）：%s" % (who, gate.unverified.splitlines()[0][:160]),
                actor="高層主管" if who == "代管" else "使用者",
            )
        elif not gate.ok:
            raise _Fatal("測試關卡結果不一致（沒有缺口也沒有通過）")
        self._say("qa", "測試關卡通過" + ("（%s）" % note if note else ""))
        self._write_evidence(gate, note)
        bounce = self._bounce_for_pending_user_directives("測試")
        if bounce is not None:
            return bounce
        return None

    def _bounce_for_pending_user_directives(self, stage_label: str) -> Optional[str]:
        """若在測試或審查期間收到使用者追加目標／新指示，先切回研發階段補做，絕不跳過新目標。"""
        if not self.has_pending_user_msgs():
            return None
        self._live_criteria()
        msg = "在%s階段收到使用者追加的新目標／新指示，高層主管先切回研發工程師補齊實作，再重新驗證與審查。" % stage_label
        self._say("supervisor", msg)
        self._event("notice", msg)
        self._set(phase="build")
        return personas.fix(
            "supervisor",
            ["使用者在%s階段追加了新目標／新指示，請依據最新目標與驗收條件補齊實作並 commit 後再回覆 DONE。" % stage_label],
            goal=self.room.goal,
            reference_root=self.room.reference_root,
            target_subdir=self._target_subdir_arg(),
        )

    def _strengthen_fix(self, unverified: str, notes: str) -> str:
        issues = ["QA 無法證明測試有效：%s" % (unverified.splitlines()[0] if unverified else "測試沒有檢查到改動"),
                  "代管審查：%s" % (notes or "測試不夠可信"),
                  "請補強測試，讓測試真的檢查這次的改動（把改動換回原狀時測試要會失敗）"]
        return personas.fix(
            "qa", issues, goal=self.room.goal,
            reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
        )

    def _gate_failed(self, key: str, n: int, fix: str, title: str, detail: str) -> str:
        """先把結果存起來（回 build、待送的修正），再決定要不要問你；重啟後直接送修正，不重跑關卡。"""
        self._set(phase="build", pending_fix=fix)
        try:
            self.kb.record(
                self.room.repo,
                self.room_id,
                key,
                f"{title}：{(detail or '').splitlines()[0] if detail else ''}",
                fingerprint=f"{self.room_id}:{key}:{n}",
                wait=False,
            )
        except Exception:
            pass
        if n > self.max_fix_attempts:
            self._ask_retry(title, detail)
            self._reset_attempts(key)
        return fix

    def _write_evidence(self, gate: Any, note: str) -> None:
        r = self.room
        head = gitops.git(self._wt.path, "rev-parse", "--short", "HEAD", ssh_host=self._wt.ssh_host).strip()
        lines = ["# 證據：%s" % r.slug, "", "- 時間：%s" % datetime.now().isoformat(timespec="seconds"),
                 "- 檢查的版本：%s（分支 %s）" % (head, r.branch), "", "## 助理自己跑的檢查", ""]
        for res in gate.results:
            lines += ["### `%s`" % res.cmd, "- 結束碼：%s%s；%.1f 秒" % (res.exit_code, "（逾時）" if res.timed_out else "",
                                                                   res.seconds),
                      "", "```", res.tail[-1500:].strip(), "```", ""]
        lines += ["## 換回原狀檢查", note or "通過：把改動換回原狀後測試會失敗，測試確實檢查了這次的改動", ""]
        manual = _unchecked_ids(r.criteria)
        if manual or (note and "無法驗證" in note):
            if manual:
                lines += ["## %s" % UNCHECKED, "這些條件沒有可執行的檢查指令，助理無法驗證，合併前請你自己確認：%s"
                          % "、".join(manual), ""]
            lines += ["## [環境限制-靜態驗證] 審計軌跡", ""]
            if note:
                lines.append(f"- 環境限制說明：{note}")
            for c in r.criteria or []:
                if _is_unchecked(c):
                    cid = c.get("id", "?")
                    ctext = c.get("text", "")
                    lines.append(f"- `{cid}` {ctext} — 缺少可自動執行的環境依賴；建議人工於目標環境確認或補跑對應驗證指令。")
            lines.append("")
        self._write_doc(self._doc_rel("evidence.md"), "\n".join(lines))
        ev_parts = [
            "%s -> exit=%s (%.1fs)" % (res.cmd, res.exit_code, res.seconds)
            for res in (getattr(gate, "results", None) or [])[:6]
        ]
        if note:
            ev_parts.append("備註：" + note[:240])
        if manual:
            ev_parts.append("需人工確認條件：" + "、".join(manual))
        self._last_test_evidence = "\n".join("- " + p for p in ev_parts)[:1200]

    @staticmethod
    def _blocking_fingerprint(item: str) -> str:
        """將審查 blocking 轉為正規化指紋，偵測是否連續 2 輪卡在同一個問題。"""
        s = re.sub(r"\s+", " ", (item or "").strip().lower())
        s = re.sub(r":\d+", "", s)  # 忽略行號微調
        return s[:100]

    # ================= review（Reviewer） =================
    def _review_stage(self) -> Optional[str]:
        """通過回 None（房間 done）；不通過回要送給代理的修正提示。"""
        self._phase("review", "review", "reviewer", "Reviewer 審查改動")
        criteria = self._live_criteria()
        crit = "\n".join("%s %s（檢查：%s）" % (c.get("id"), c.get("text"), c.get("check") or "manual")
                         for c in criteria)
        dev_ctx = ("\n\n實作工程師交接說明：\n" + self._last_dev_summary) if self._last_dev_summary else ""
        dec_ctx = (
            "\n\n高階主管決策紀錄：\n" + "\n".join("- " + d for d in self._supervisor_decisions[-8:])
            if self._supervisor_decisions else ""
        )
        note = self._role_note("reviewer", "Reviewer 審查重點",
                               "驗收條件：\n%s\n\n改動統計：\n%s%s%s" % (crit, gitops.diff_stat(self._wt), dev_ctx, dec_ctx))
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
            cur_fps = {self._blocking_fingerprint(b) for b in blocking}
            repeated = [b for b in blocking if self._blocking_fingerprint(b) in self._last_review_blocking_fps]
            if repeated:
                rep_state = (
                    f"任務目標：{self.room.goal or '（未指定）'}\n"
                    f"唯讀參考母資料夾 A：{self.room.reference_root or '（無）'}\n"
                    f"可讀寫目標子資料夾 B：{self._target_subdir_arg() or '（整個 worktree）'}\n"
                    f"連續 2 輪重複出現的審查阻斷項：\n" + "\n".join(f"- {b}" for b in repeated[:6]) + "\n"
                    f"研發工程師最近說明：{self._last_dev_summary or '（無）'}"
                )
                rep_q = (
                    "上述連續 2 輪重複出現的審查阻斷項，是否屬於外部環境因素、外部工具對母資料夾的變動、"
                    "或唯讀參考區權限限制（研發工程師在本房間 worktree 內根本無權修復），應由高層主管裁決豁免以避免開發與審查無限互踢？"
                )
                p_waive = self._prob(
                    rep_state,
                    rep_q,
                    self._ctx("supervisor", "審查斷路器：判定重複阻斷項是否屬外部／唯讀限制", pass_min=0.75),
                )
                if p_waive is not None and p_waive >= 0.75:
                    rep_fps = {self._blocking_fingerprint(b) for b in repeated}
                    blocking = [b for b in blocking if self._blocking_fingerprint(b) not in rep_fps]
                    waive_note = "高層主管斷路器裁決：重複阻斷項屬外部因素或唯讀參考區限制，予以豁免：" + "；".join(repeated[:3])
                    self._record_supervisor_decision(waive_note[:220], actor="高階主管/斷路器")
                    self._event("notice", waive_note[:240], circuit_breaker=True)
                    repeated = []
                    cur_fps = {self._blocking_fingerprint(b) for b in blocking}
            self._last_review_blocking_fps = cur_fps
        if blocking:
            n = self._bump_attempts("review")

            for b in blocking[:5]:
                try:
                    self.lessons.bump_review_blocking(self.room.repo, b)
                except Exception:
                    log.debug("紀錄審查阻斷教訓失敗", exc_info=True)
            learnings = self._compound_learnings("\n".join(blocking))
            fix_issues = list(blocking)
            if repeated:
                rep_txt = "；".join(repeated[:3])
                self._record_supervisor_decision(
                    f"觸發開發↔審查防互踢斷路器（同一問題連續 2 輪未解）：{rep_txt[:180]}；要求更換解法策略",
                    actor="高階主管/斷路器",
                )
                self._say("scrum", f"高階主管斷路器介入：同一審查問題連續 2 輪未解（{rep_txt[:160]}），禁止重複原修法，請改採替代策略。")
                self._event("notice", f"開發↔審查防互踢斷路器觸發：{rep_txt[:240]}", circuit_breaker=True)
                fix_issues.insert(
                    0,
                    f"【高階主管斷路器介入】以下審查問題已連續 2 輪未解，嚴禁重複前次修法，請先重新定位根因並改採不同實作策略：{rep_txt}",
                )
            self._say("reviewer", "審查發現必須修正的問題（第 %d 次）：\n%s" % (n, "\n".join("- " + b for b in blocking)))
            sugg_hint = ("；另附審查建議：" + "；".join(self._suggestions[:3])) if self._suggestions else ""
            drift_hint = ("；偏離警告：" + self._last_drift_alert[:180]) if self._last_drift_alert else ""
            summary_txt = "；".join(blocking[:4])
            # 三角色完整閉環：審查工程師指出位置 → 高層主管拍板修正方向 → 研發工程師決定改法（立即由 _gate_failed 送出，不重複排入 _pending_comm_msgs）
            self._record_role_comm(
                "reviewer",
                "supervisor",
                "審查工程師第 %d 次審查指出問題／偏離位置：%s%s%s" % (n, summary_txt, drift_hint, sugg_hint),
                guidance="請高層主管確認修正方向後交由研發工程師決定具體改法。",
                queue_for_impl=False,
                emit_say=True,
            )
            self._record_supervisor_decision(
                "第 %d 次審查退回修正方向：要求研發工程師優先解決「%s」並對齊原訂目標" % (n, summary_txt[:160]),
                actor="高層主管",
            )
            self._record_role_comm(
                "supervisor",
                "developer",
                "高層主管已確認第 %d 次審查修正方向：%s%s" % (n, summary_txt, sugg_hint),
                guidance="審查工程師已指出問題與偏離位置（檔案:行號與原因），請研發工程師決定最佳程式碼修正方案並補齊驗證後再回覆 DONE。",
                queue_for_impl=False,
                emit_say=True,
            )
            return self._gate_failed(
                "review",
                n,
                personas.fix(
                    "reviewer", fix_issues, goal=self.room.goal,
                    reference_root=self.room.reference_root, target_subdir=self._target_subdir_arg(),
                    compound_learnings=learnings,
                ),
                "審查修了 %d 次仍有問題" % (n - 1),
                "\n".join(fix_issues)[:4000],
            )
        bounce = self._bounce_for_pending_user_directives("審查")
        if bounce is not None:
            return bounce
        self._last_review_blocking_fps.clear()
        self._finish()
        return None

    def _reviewer_d1_alignment_check(self, commits: str, diff_stat: str) -> Tuple[Optional[float], str]:
        """審查工程師呼叫 Decision-1 檢核目前實作是否偏離任務目標，若低於門檻則判定偏離程度並回傳警示。"""
        state = (
            "【任務目標】\n%s\n\n【近期提交】\n%s\n\n【變更統計】\n%s\n\n【研發工程師自述】\n%s"
            % (
                self.room.goal or "(未提供)",
                (commits or "(尚無提交)")[:1200],
                (diff_stat or "(尚無 diff)")[:1200],
                (self._last_dev_summary or "(無)")[:400],
            )
        )
        p = self._prob(
            state,
            REVIEW_ALIGNMENT_Q,
            self._ctx("reviewer", "審查工程師以 Decision-1 檢核目標偏離", pass_min=REVIEW_ALIGNMENT_MIN),
        )
        self._check_pause()
        if p is not None and p < REVIEW_ALIGNMENT_MIN:
            choice = self._choose(
                state,
                "目前開發進度與目標的一致性偏低（%.0f%%），請判定偏離等級：" % (p * 100.0),
                DRIFT_OPTIONS,
                self._ctx("reviewer", "審查工程師以 Decision-1 判定目標偏離等級"),
            )
            self._check_pause()
            level = "重大偏離" if choice == "major_drift" else "輕微偏離"
            alert = (
                "Decision-1 目標對齊度 %.0f%%（低於門檻 %.0f%%，判定：%s），"
                "請精確指出偏離的檔案:行號與原因，交由高層主管與研發工程師決定如何修正。"
                % (p * 100.0, REVIEW_ALIGNMENT_MIN * 100.0, level)
            )
            self._last_drift_alert = alert
            return p, alert
        self._last_drift_alert = ""
        return p, ""

    def _checkpoint_review(self, turns: int = 0) -> Optional[str]:
        """盤中增量審查：開發過程中有新 commit 或每累積 3 回合存在未提交變更時，審查工程師即以 Decision-1 檢核防偏離；有偏離時由審查工程師指出位置，高層主管拍板方向，研發工程師決定改法。"""
        try:
            commit_pairs = gitops.commits_since(self._wt)
        except Exception:
            commit_pairs = []
        commit_count = len(commit_pairs)
        has_new_commit = commit_count > self._last_reviewed_commit_count
        periodic_uncommitted_check = turns > 0 and (turns % 3 == 0)
        if not has_new_commit and not periodic_uncommitted_check:
            return None
        try:
            stat = gitops.diff_stat(self._wt)
            diff_txt = gitops.diff_text(self._wt)
        except Exception:
            return None
        if not has_new_commit and not (stat or "").strip():
            return None
        commits_raw = "\n".join("%s %s" % (h[:7], msg) for h, msg in commit_pairs)
        delta = max(0, commit_count - self._last_reviewed_commit_count)
        if has_new_commit:
            self._last_reviewed_commit_count = commit_count
        p, drift_alert = self._reviewer_d1_alignment_check(commits_raw, stat)
        # 若 Decision-1 偵測到偏離，或已累積 >= 2 個新 commit，啟動盤中唯讀審查
        if not drift_alert and delta < 2:
            return None
        criteria = self._live_criteria()
        prompt = personas.checkpoint_review(
            self.room.goal,
            _marked(criteria),
            commits_raw,
            stat,
            diff_txt[:12000],
            drift_alert or "盤中例行增量防偏離抽查",
            dev_summary=self._last_dev_summary,
            concerns=self._concerns,
            reference_root=self.room.reference_root,
            target_subdir=self._target_subdir_arg(),
        )
        try:
            reply = call_ctx(
                self.router.write,
                prompt,
                effort="normal",
                ctx=self._ctx("reviewer", "審查工程師盤中增量防偏離審查"),
            )
        except Exception as e:
            log.debug("盤中增量審查略過：%s", type(e).__name__)
            return None
        self._check_pause()
        data = personas.parse_json_block(reply)
        if not isinstance(data, dict):
            return None
        self._record_compound_learnings(data)
        drift_locs = [str(x).strip() for x in (data.get("drift_locations") or []) if str(x).strip()]
        blocking = personas.normalize_review_blocking(data.get("blocking") or []) if isinstance(data.get("blocking"), list) else []
        findings = drift_locs + [b for b in blocking if b not in drift_locs]
        if not findings:
            if p is not None:
                self._say("reviewer", "盤中增量審查通過（Decision-1 目標對齊度 %.0f%%，無偏離）。" % (p * 100.0))
            return None
        # 三角色分工閉環：審查工程師只指出哪裡偏離 → 高層主管決定修正方向 → 研發工程師決定怎麼改
        loc_summary = "；".join(findings[:4])
        self._say("reviewer", "盤中增量審查發現偏離／風險（僅定位不代改）：\n%s" % "\n".join("- " + f for f in findings[:6]))
        self._record_role_comm(
            "reviewer",
            "supervisor",
            "盤中增量審查指出偏離位置：%s%s" % (loc_summary, ("（%s）" % drift_alert) if drift_alert else ""),
            guidance="請高層主管確認修正方向，並交由研發工程師決定具體程式碼改法。",
            queue_for_impl=False,
            emit_say=True,
        )
        self._record_supervisor_decision(
            "依據審查工程師盤中定位（%s），指示研發工程師立即對齊任務目標並修正偏離處" % loc_summary[:180],
            actor="高層主管",
        )
        self._record_role_comm(
            "supervisor",
            "developer",
            "高層主管已確認審查工程師指出的盤中偏離位置：%s" % loc_summary,
            guidance="請研發工程師針對上述檔案:行號與偏離原因，自行決定最佳程式碼修正方案後繼續開發。",
            queue_for_impl=False,
            emit_say=True,
        )
        learnings = self._compound_learnings("\n".join(findings))
        return personas.fix(
            "reviewer",
            findings,
            goal=self.room.goal,
            reference_root=self.room.reference_root,
            target_subdir=self._target_subdir_arg(),
            compound_learnings=learnings,
        )

    def _record_compound_learnings(self, data: Dict[str, Any]) -> None:
        """將審查工程師產出的 compound_learnings 沉澱到經驗庫（無論通過或退回都記錄）。"""
        raw = data.get("compound_learnings")
        if not isinstance(raw, list):
            return
        items = [str(x).strip() for x in raw if str(x).strip()]
        if not items:
            return
        for item in items[:5]:
            try:
                self.lessons.bump_review_blocking(self.room.repo, "[ce-compound] " + item[:240])
            except Exception:
                log.debug("寫入 compound_learnings 失敗", exc_info=True)
        try:
            self.kb.record_room_summary(
                self.room.repo,
                self.room_id,
                self.room.goal or self.room.name,
                summary="審查工程師 ce-compound 知識沉澱：" + "；".join(items[:5]),
                lessons=items[:5],
            )
        except Exception:
            log.debug("寫入 LLM Wiki compound_learnings 失敗", exc_info=True)

    def _review_once(self, phase: str, skip_seat: str = "", note: str = "") -> Tuple[Optional[List[str]], str]:
        """回傳（blocking 或 None＝沒有結果, 使用的座位）。每輪強制啟用全新審查 Context。"""
        cli, model = self._pin("review")
        a = self._want(phase, cli, exclude=self.room.impl_seat, model=model, role="review")
        if a is None or (phase == "final_review" and a.seat == skip_seat):
            return [], ""
        criteria = self._live_criteria()
        stat = gitops.diff_stat(self._wt)
        try:
            commits_raw = "\n".join("%s %s" % (h[:7], msg) for h, msg in gitops.commits_since(self._wt))
        except Exception:
            commits_raw = ""
        _, drift_alert = self._reviewer_d1_alignment_check(commits_raw, stat)
        raw_diff = gitops.diff_text(self._wt)
        if len(raw_diff) > _REVIEW_DIFF_MAX_CHARS:
            raw_diff = (
                raw_diff[:_REVIEW_DIFF_MAX_CHARS]
                + "\n\n[... diff 已截斷（原始長度 %d 字元，僅保留前 %d 字元）；請搭配上方改動統計與關鍵檔案審查 ...]"
                % (len(raw_diff), _REVIEW_DIFF_MAX_CHARS)
            )
        prompt = self._noted(
            personas.review(
                self.room.goal, _marked(criteria), stat,
                raw_diff,
                reference_root=self.room.reference_root, target_subdir=self.room.target_subdir,
                supervisor_decisions=self._supervisor_decisions[-8:],
                concerns=self._concerns,
                dev_summary=self._last_dev_summary,
                drift_alert=drift_alert,
                test_evidence=self._last_test_evidence,
            ),
            "reviewer", note,
        )
        first = a.seat  # 退回模型審查時也以這個座位計，最後審查不再找同一個座位
        tried = set()  # type: Set[str]
        while a is not None and a.seat not in tried:
            tried.add(a.seat)
            self._assign_event("review", a)
            self._set(review_seat=a.seat, review_model=a.model)
            label = _label(a.seat)
            nxt = None
            try:
                # 每輪審查前先徹底關閉任何殘留審查驅動，並以 fresh_context=True 啟動全新 Context
                if self._review is not None:
                    self._drop(self._review)
                drv = self._open(a.seat, a.model, read_only=True, fresh_context=True)
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
                                fresh_context=True, detail={"sent": self._mclip(prompt, 8000)})
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
        glm_prompt = (
            prompt
            + '\n\n【格式要求】請務必在回覆最後輸出合法 JSON 物件，至少包含 "blocking" 陣列（無阻斷項請回 {"blocking": [], "suggestions": []}）。'
        )
        try:
            text = call_ctx(self.router.write, glm_prompt, effort="normal",
                            ctx=self._ctx("reviewer", "代替審查座位審查改動"))
        except Exception as e:
            self._check_pause()
            self._event("notice", "glm 審查失敗：%s" % type(e).__name__)
            return None, first
        self._check_pause()
        blocking = self._parse_review(text)
        if blocking is None and (text or "").strip():
            # 容錯修復：將非結構化審查文字再整理一次成可判讀 JSON，避免白白重跑整輪審查
            repair_prompt = (
                "請把以下程式碼審查意見整理成純 JSON 物件（不要其他說明），"
                '格式為 {"blocking": ["[嚴重度] 檔案:行號 — 問題"], "suggestions": ["建議"]}；'
                "若原文明確表示沒有阻斷問題或審查通過，blocking 請回傳空陣列 []。\n\n"
                "【原始審查回覆】\n" + text[:6000]
            )
            try:
                repaired = call_ctx(self.router.write, repair_prompt, effort="low",
                                    ctx=self._ctx("reviewer", "整理審查回覆為 JSON"))
                self._check_pause()
                blocking = self._parse_review(repaired)
                if blocking is not None:
                    text = repaired
            except Exception as e:
                log.debug("整理審查 JSON 失敗：%s", type(e).__name__)
        if blocking is not None:
            self._suggestions_event(text)
        return blocking, first

    @staticmethod
    def _parse_review(text: str) -> Optional[List[str]]:
        data = personas.parse_json_block(text)
        if data is None:
            return None
        raw_blocking = data.get("blocking")
        if not isinstance(raw_blocking, list):
            for alt_key in ("blockers", "issues", "findings"):
                if isinstance(data.get(alt_key), list):
                    raw_blocking = data.get(alt_key)
                    break
            else:
                if data.get("approved") is True or data.get("ok") is True or data.get("pass") is True:
                    raw_blocking = []
                else:
                    return None
        items = personas.normalize_review_blocking(raw_blocking)
        raw_drifts = data.get("drift_locations")
        if isinstance(raw_drifts, list):
            for d in raw_drifts:
                ds = str(d or "").strip()
                if ds and not any(ds in x for x in items):
                    items.append("[目標偏離] " + ds)
        return items

    def _suggestions_event(self, text: str) -> None:
        data = personas.parse_json_block(text) or {}
        sugg = [str(s) for s in data.get("suggestions") or [] if str(s).strip()]
        drift_locs = [str(d) for d in data.get("drift_locations") or [] if str(d).strip()]
        self._record_compound_learnings(data)
        self._say("reviewer", "審查通過" if (not data.get("blocking") and not drift_locs) else "審查有意見")
        if drift_locs:
            self._event("notice", "審查工程師指出目標偏離位置：" + "；".join(drift_locs)[:1000])
        if sugg:
            self._event("notice", "審查建議：" + "；".join(sugg)[:1000])
        self._suggestions = sugg

    def _build_executive_summary(
        self, commits: str, stat: str, manual: List[str], sugg: List[str]
    ) -> str:
        """產出給人類主管的結案一頁式驗收摘要（明確區分已自動實測 vs 環境受限靜態驗證）。"""
        criteria = list((self.room.criteria if self.room else None) or [])
        auto_ids = [str(c.get("id", "?")) for c in criteria if not _is_unchecked(c)]
        lines = [
            "## 結案一頁式驗收摘要",
            f"- 目標：{self.room.goal or self.room.name or self.room.slug}",
            f"- 已自動實測通過條件：{'、'.join(auto_ids) if auto_ids else '（依整套測試指令通過）'}",
            f"- 環境受限／靜態驗證項目：{'、'.join(manual) + '（請於目標環境人工確認）' if manual else '無（全部皆已自動實測通過）'}",
        ]
        if self._supervisor_decisions:
            lines.append("- 高階主管關鍵決策：")
            for d in self._supervisor_decisions[-5:]:
                lines.append(f"  - {d}")
        if sugg:
            lines.append("- 審查建議與殘餘風險：" + "；".join(sugg[:4]))
        else:
            lines.append("- 審查建議與殘餘風險：無")
        return "\n".join(lines)

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
        commits = "\n".join("%s %s" % c for c in gitops.commits_since(self._wt))
        stat = gitops.diff_stat(self._wt)
        manual = _unchecked_ids(self.room.criteria)
        exec_summary = self._build_executive_summary(commits, stat, manual, sugg)
        ev += "\n## 審查\n- 審查座位：%s\n- 必須修正：無\n%s\n\n%s\n" % (
            _label(self.room.review_seat), "".join("- 建議：%s\n" % s for s in sugg), exec_summary)
        self._write_doc(rel, ev)
        unchecked = ("%s：%s（沒有可執行的檢查指令，合併前請你自己確認）\n\n" % (UNCHECKED, "、".join(manual))
                     if manual else "")
        self._say("scrum", exec_summary)
        self._set(phase="done", status="done", persona="", activity="全部關卡通過，等你合併")
        self._event("phase", "全部關卡通過", phase="done", persona="")
        try:
            dec_lines = "\n".join("- " + d for d in self._supervisor_decisions[-12:]) if self._supervisor_decisions else "（無）"
            self.kb.record(
                self.room.repo,
                self.room_id,
                "solutions",
                f"# 房間 {self.room.name or self.room.slug} 完成紀錄\n目標：{self.room.goal}\n\n## 高層主管關鍵決策\n{dec_lines}\n\n## Commits\n{commits}\n\n## 改動\n{stat}",
                fingerprint=f"{self.room_id}:done:{commits[:40]}",
                wait=False,
            )
        except Exception:
            pass
        self.store.add_inbox(self.room_id, "merge", "「%s」完成，合併到 %s？" % (
            self.room.name or self.room.slug, self.room.target_branch or "主線"),
            "%s%s\n\n分支：%s\n\ncommit：\n%s\n\n改動：\n%s" % (unchecked, exec_summary, self.room.branch, commits, stat), OPT_MERGE)
        self._event("inbox", "等你合併", inbox_kind="merge")
