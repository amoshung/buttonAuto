"""辦公室人員（座位）：模型目錄、用量已滿偵測（QuotaBook）與 Decision-1 派工（Dispatcher）。

見設計文件附錄 A：
- 規則先決定「誰可以」（已安裝、未滿、審查者≠實作者），Decision-1 只在可行座位中挑「誰最適合」。
- 用量已滿的座位標為 exhausted（全辦公室共用、存 quota.json），到重置時間（或 60 分鐘後）列入試探。
"""
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from team.drivers import SEATS as _DRIVER_SEATS
from team.modelprefs import DEFAULT_CATALOG, load_catalog, purpose_of  # noqa: F401  （load_catalog 給舊呼叫端）
from team.models import call_ctx, prob01
from team.usage import DEMO_MODEL

log = logging.getLogger(__name__)

SEATS: List[str] = list(_DRIVER_SEATS)
SEAT_LABELS: Dict[str, str] = {
    "kiro": "Kiro",
    "agy": "Antigravity",
    "claude": "Claude",
    "glm": "GLM（一般）",
    "glm_high": "GLM（高思考）",
}
TIERS = ("economy", "standard", "premium")
TIER_LABELS = {"economy": "經濟", "standard": "標準", "premium": "高階"}
_TIER_RANK = {t: i for i, t in enumerate(TIERS)}
MAX_COST = 15.0

# 使用者決定：Decision-1 自動派工（實作、審查都一樣）只在 AUTO_SEATS 裡選；Kiro／Antigravity 只在你指定時使用
from team.modelprefs import AUTO_LABEL, AUTO_SEATS  # noqa: E402,F401
NO_AUTO_SEAT = "自動派工只用 Claude 與 GLM，目前都不可用；請在派工時指定座位"
COMPLEX_QUESTION = "此任務需要跨多檔重構或深入除錯嗎？"
DEFAULT_PROBE_MINUTES = 60


# ---- 用量已滿偵測 ----
QUOTA_RE = re.compile(
    r"quota|rate limit|usage limit|limit reached|credits|exceeded|(?<!\d)429(?!\d)|too many requests|額度|用量",
    re.I)
_IN_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|h|m)\b", re.I)
_AT_RE = re.compile(r"\bresets?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\b", re.I)


# 代理自己寫的文字（最後一段）只認這些明確說法：「降低記憶體用量」之類的一般敘述不算
STRICT_QUOTA_RE = re.compile(
    r"usage limit reached|rate limit|(?<!\d)429(?!\d)|quota exceeded|額度已用完", re.I)


def detect_quota(text: str) -> bool:
    """CLI／驅動的錯誤文字（stderr、結構化錯誤）用：寬鬆比對。"""
    return bool(text) and QUOTA_RE.search(text) is not None


def detect_quota_strict(text: str) -> bool:
    """代理的回覆文字用：只認明確的額度用完說法。"""
    return bool(text) and STRICT_QUOTA_RE.search(text) is not None


def parse_reset(text: str, now: datetime) -> Optional[datetime]:
    """解析「try again in N minutes/hours」或「resets at H(:MM)(am|pm)」；跨日則加一天。"""
    if not text:
        return None
    m = _IN_RE.search(text)
    if m:
        n = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("h"):
            return now + timedelta(hours=n)
        if unit.startswith("s"):
            return now + timedelta(seconds=n)
        return now + timedelta(minutes=n)
    m = _AT_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if ampm == "p" else 0)
        if hour > 23 or minute > 59:
            return None
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t
    return None


def _naive(dt: datetime) -> datetime:
    """一律以本機時間（無時區）比較與儲存。"""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


class QuotaBook:
    """全辦公室共用的用量狀態；執行緒安全，存 <root>/quota.json（傳入 .json 路徑則直接用該檔）。"""

    def __init__(self, root: str):
        self.path = root if root.lower().endswith(".json") else os.path.join(root, "quota.json")
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("quota.json 讀取失敗，視為全部可用：%s", type(e).__name__)
            return
        if not isinstance(data, dict):
            return
        for seat, rec in data.items():
            if not isinstance(rec, dict) or rec.get("state") != "exhausted":
                continue
            try:
                until = _naive(datetime.fromisoformat(rec["until"]))
                since = _naive(datetime.fromisoformat(rec["since"]))
            except (KeyError, TypeError, ValueError):
                continue
            self._state[seat] = {"state": "exhausted", "until": until.isoformat(), "since": since.isoformat()}
            if rec.get("reason"):
                self._state[seat]["reason"] = str(rec["reason"])

    def _save(self) -> None:
        """寫入失敗只記 warning：狀態仍保留在記憶體，mark_* 不丟例外。"""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("quota.json 寫入失敗（狀態只保留在記憶體）：%s", type(e).__name__)

    def mark_exhausted(self, seat: str, text: str, now: datetime, reason: str = "") -> datetime:
        """標為已滿；回傳下次試探時間（解析出的重置時間，否則 now+60 分）。不記錄 text 內容。

        reason：暫時排除的原因（例如 "login"＝未登入）；空字串＝用量已滿。"""
        now = _naive(now)
        until = parse_reset(text, now) or now + timedelta(minutes=DEFAULT_PROBE_MINUTES)
        with self._lock:
            prev = self._state.get(seat)
            since = prev["since"] if prev else now.isoformat()
            self._state[seat] = {"state": "exhausted", "until": until.isoformat(), "since": since}
            if reason:
                self._state[seat]["reason"] = reason
            self._save()
        log.info("座位 %s 用量已滿，%s 試探", seat, until.isoformat())
        return until

    def is_exhausted(self, seat: str, now: datetime) -> bool:
        """已滿的座位在試探成功（mark_ok）前都視為已滿，即使已過試探時間。"""
        with self._lock:
            return seat in self._state

    def due_probes(self, now: datetime) -> List[str]:
        now = _naive(now)
        with self._lock:
            due = [s for s, rec in self._state.items() if datetime.fromisoformat(rec["until"]) <= now]
        order = {s: i for i, s in enumerate(SEATS)}
        return sorted(due, key=lambda s: order.get(s, len(SEATS)))

    def reason(self, seat: str) -> str:
        with self._lock:
            return (self._state.get(seat) or {}).get("reason", "")

    def mark_ok(self, seat: str) -> None:
        with self._lock:
            if self._state.pop(seat, None) is not None:
                self._save()

    def snapshot(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            snap = {s: {"state": "ok", "until": "", "since": ""} for s in SEATS}
            snap.update({s: dict(rec) for s, rec in self._state.items()})
            return snap


# ---- 派工 ----
class NoSeatError(Exception):
    pass


@dataclass
class Assignment:
    seat: str
    model: str
    tier: str
    by: str
    reason: str
    model_by: str = ""  # 模型是你指定的：room（房間設定）｜global（設定頁座位模型）；要先驗證在可用清單裡


def phase_tier(phase: str, p_complex: float) -> str:
    """附錄 A2 第 3 點：實作依難度；小修正 economy；其餘 standard。"""
    if phase in ("build", "implement"):
        if p_complex >= 0.7:
            return "premium"
        if p_complex <= 0.3:
            return "economy"
        return "standard"
    if phase == "fix":
        return "economy"
    return "standard"


class Dispatcher:
    def __init__(self, router: Any, catalog: Dict[str, List[Dict[str, Any]]], quota: QuotaBook,
                 available: Callable[[], Set[str]], cost_weight: float = 0.3, prefs: Any = None):
        self.router = router
        self.catalog = catalog
        self.quota = quota
        self.available = available
        self.cost_weight = cost_weight
        # Task 18：team.modelprefs.ModelPrefs（全域座位模型與用途）；None ＝ 全部自動
        self.prefs = prefs

    def feasible(self, exclude: str = "") -> List[str]:
        avail = set(self.available() or ())
        now = datetime.now()
        return [s for s in SEATS if s in avail and s != exclude and not self.quota.is_exhausted(s, now)]

    def _entry(self, seat: str, tier: str) -> Dict[str, Any]:
        entries = self.catalog.get(seat) or DEFAULT_CATALOG.get(seat) or [{"id": "", "tier": "standard", "cost": MAX_COST}]
        want = _TIER_RANK.get(tier, 1)
        return min(entries, key=lambda e: (abs(_TIER_RANK.get(e.get("tier"), 1) - want), e.get("cost", MAX_COST)))

    def _assign(self, seat: str, tier: str, by: str, reason: str, user_model: str = "",
                use_global: bool = True) -> Assignment:
        """模型：房間指定（user_model）＞ 全域座位設定 ＞ 依等級從目錄挑。"""
        src = "room" if user_model else ""
        model = user_model
        if not model and self.prefs is not None and by != "fallback" and use_global:
            try:
                model = self.prefs.seat_model(seat)
            except Exception:
                model = ""
            src = "global" if model else ""
        if model:
            e = (self.prefs.entry(seat, model) if self.prefs is not None else None) or {}
            return Assignment(seat=seat, model=model, tier=str(e.get("tier") or tier), by=by,
                              reason="%s；模型 %s（你指定：%s）" % (reason, model, "房間設定" if src == "room" else "設定頁"),
                              model_by=src)
        e = self._entry(seat, tier)
        return Assignment(seat=seat, model=str(e.get("id", "")), tier=str(e.get("tier", tier)), by=by, reason=reason)

    def _prob(self, state: str, question: str, ctx: Optional[Dict[str, Any]] = None,
              purpose: str = "", seen: Optional[List[Tuple[str, str]]] = None) -> Optional[float]:
        """seen：每次有答案就記下（kind, 實際回答的模型）；路由器沒回報（舊介面）時當成 Decision-1。"""
        c = dict(ctx or {}, role="dispatcher", purpose=purpose)
        try:
            p = call_ctx(self.router.probability, state, question, ctx=c)
        except Exception as e:  # 路由器本身應不丟例外；保險起見當成不可用
            log.info("派工判斷失敗：%s", type(e).__name__)
            return None
        p = prob01(p)
        if p is not None and seen is not None:
            seen.append((str(c.get("model_kind") or "d1"), str(c.get("model_used") or "Decision-1")))
        return p

    @staticmethod
    def _who(seen: List[Tuple[str, str]]) -> Tuple[str, str]:
        """（by, 理由開頭）：全部由 Decision-1 回答才是 "Decision-1"；全由 glm → glm 的模型名；混合 → 「Decision-1＋glm」。"""
        d1 = [m for k, m in seen if k == "d1"]
        glm = [m for k, m in seen if k != "d1"]
        glm_name = glm[0] if glm else ""
        if not glm:
            by = "Decision-1"
        elif not d1:
            by = glm_name
        else:
            by = "Decision-1＋%s" % glm_name
        head = by
        if any(m == DEMO_MODEL for _, m in seen):
            head = "示範（假模型，代 %s）" % ("Decision-1" if not glm else by)
        elif glm and d1:
            head = "%s（Decision-1 沒回答的 %d 題改問 %s）" % (by, len(glm), glm_name)
        return by, head

    def final_review_seat(self) -> str:
        """合併前最後審查用哪個座位（用途設定）；關閉 → ""。"""
        mode = purpose_of(self.prefs, "final_review")
        return {"glm_high": "glm_high", "glm_normal": "glm"}.get(mode, "")

    def pick(self, phase: str, task_text: str, exclude: str = "", user_seat: str = "",
             ctx: Optional[Dict[str, Any]] = None, user_model: str = "", use_global: bool = True) -> Assignment:
        """ctx：{room_id}（給模型呼叫紀錄用；角色固定為 dispatcher）。user_model 只在 user_seat 有給時有效。
        use_global=False：這間房不用設定頁的座位模型（房間「改回自動」）。"""
        feas = self.feasible(exclude)
        if not feas:
            raise NoSeatError("所有座位都不可用")
        if user_seat and user_seat in feas:
            return self._assign(user_seat, phase_tier(phase, 0.5), "user", "使用者指定", user_model=user_model,
                                use_global=use_global)
        if phase == "final_review":
            fs = self.final_review_seat()
            if fs and fs in feas:
                return self._assign(fs, "standard", "rule", "規則：合併前最後審查由 %s 負責（設定頁「模型」可改）"
                                    % SEAT_LABELS.get(fs, fs), use_global=use_global)
        feas = [x for x in feas if x in AUTO_SEATS]  # 自動派工不選 Kiro／Antigravity
        if not feas:
            raise NoSeatError(NO_AUTO_SEAT)

        # 判斷模型只要回一次 None（不可用）就停止追問，直接依規則選，避免模型掛掉時逐座位等逾時。
        models_down = False
        p_complex = 0.5
        head = ""
        seen = []  # type: List[Tuple[str, str]]
        if phase in ("build", "implement"):
            p = self._prob(task_text, COMPLEX_QUESTION, ctx, "派工：評估任務難度", seen)
            if p is None:
                models_down = True
            else:
                p_complex = p
        tier = phase_tier(phase, p_complex)
        if phase in ("build", "implement") and not models_down:
            head = "難度 %.2f → %s；" % (p_complex, TIER_LABELS.get(tier, tier))

        scored = []
        for seat in ([] if models_down else feas):
            e = self._entry(seat, tier)
            label = SEAT_LABELS.get(seat, seat)
            state = "任務：%s\n階段：%s\n候選：%s（模型 %s，等級 %s）" % (
                task_text, phase, label, e.get("id") or "CLI 預設", e.get("tier"))
            p = self._prob(state, "%s 適合這個任務嗎？" % label, ctx, "派工：評估 %s 適合度" % label, seen)
            if p is None:
                scored = []
                break
            cost = self.cost_weight * float(e.get("cost", MAX_COST)) / MAX_COST
            scored.append((p - cost, seat, p, cost))
        if scored:
            best = max(scored, key=lambda x: (x[0], -SEATS.index(x[1])))
            ranked = sorted(scored, key=lambda x: (-x[0], SEATS.index(x[1])))
            parts = "；".join("%s %.2f−成本 %.2f＝%.2f" % (SEAT_LABELS.get(s, s), p, c, sc) for sc, s, p, c in ranked)
            by, who = self._who(seen)
            return self._assign(best[1], tier, by, "%s：%s%s → 選 %s（%s）" % (
                who, head, parts, SEAT_LABELS.get(best[1], best[1]), TIER_LABELS.get(tier, tier)), use_global=use_global)

        seat = min(feas, key=lambda s: (float(self._entry(s, tier).get("cost", MAX_COST)), SEATS.index(s)))
        return self._assign(seat, tier, "rule", "規則：判斷模型不可用，選 %s 等級成本最低的座位（%s，成本 %g）" % (
            TIER_LABELS.get(tier, tier), SEAT_LABELS.get(seat, seat),
            float(self._entry(seat, tier).get("cost", MAX_COST))), use_global=use_global)

    def fallback(self, seat: str) -> Assignment:
        if "glm" not in self.feasible(exclude=seat):
            raise NoSeatError("GLM（一般）不可用，無法接手 %s" % SEAT_LABELS.get(seat, seat))
        return self._assign("glm", "standard", "fallback",
                            "%s 用量已滿，改由 GLM（一般）接手" % SEAT_LABELS.get(seat, seat))
