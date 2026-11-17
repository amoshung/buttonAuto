"""使用者選模型（Task 18）：team_models.json 讀寫、ACP 偵測快取、驗證。

三層設定（都預設「自動」；越下層越優先）：
1. 全域座位模型 seats：{seat: 模型 id 或 ""（自動＝依任務等級從目錄挑）}
2. 全域判斷與撰寫用途 purposes：見 PURPOSES
3. 每間房 Room.impl_pref／review_pref（在 team.rooms，這裡只驗證）

team_models.json（只存模型 id、等級、成本，不含金鑰）：
  {"seats": {...}, "purposes": {...},
   "models": {seat: [{"id", "tier": economy|standard|premium|""（不參與自動）, "cost"}]}}
舊格式 {seat: [{id, tier, cost}]}（只有目錄）仍可讀，存檔時改寫成新格式。

可用清單：kiro／agy 來自 ACP session/new 的 availableModels（偵測結果快取在 <team root>/models_cache.json）；
claude 用目錄（haiku／sonnet／opus＋使用者加的完整 id）；glm 座位固定 glm 模型。
"""
import copy
import json
import logging
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from team.drivers import SEATS
from team.usage import now_iso

log = logging.getLogger(__name__)

ACP_SEATS = ("kiro", "agy")
# 使用者決定：Decision-1 自動派工只在這些座位裡選；Kiro／Antigravity 只在你指定時使用
AUTO_SEATS = ("claude", "glm", "glm_high")
AUTO_LABEL = "自動（Decision-1 在 Claude／GLM 中選）"
MANUAL_ONLY = "只在你指定時使用（自動派工不會選）"
GLM_SEATS = ("glm", "glm_high")
TIERS = ("economy", "standard", "premium")
TIER_COST = {"economy": 1, "standard": 3, "premium": 15}
CLAUDE_ALIASES = ("haiku", "sonnet", "opus")
MAX_ID = 120
MAX_MODELS = 60

DEFAULT_CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "kiro": [{"id": "", "tier": "standard", "cost": 2}],
    "agy": [{"id": "", "tier": "standard", "cost": 2}],
    "claude": [{"id": "haiku", "tier": "economy", "cost": 1},
               {"id": "sonnet", "tier": "standard", "cost": 3},
               {"id": "opus", "tier": "premium", "cost": 15}],
    "glm": [{"id": "glm-5.3", "tier": "standard", "cost": 0}],
    "glm_high": [{"id": "glm-5.3", "tier": "standard", "cost": 0}],
}

PURPOSES: Dict[str, Dict[str, Any]] = {
    "permission": {"label": "權限判斷", "default": "d1+glm", "options": [
        ("d1+glm", "Decision-1＋glm 低（預設）"),
        ("glm", "只用 glm 低（嚴格門檻）"),
        ("d1", "只用 Decision-1（灰色地帶交給你）")]},
    "judge": {"label": "驗收條件品質、是否偏離目標、派工", "default": "d1", "options": [
        ("d1", "Decision-1（預設）"),
        ("glm", "glm 低")]},
    "notes": {"label": "角色發言（PO／QA／Reviewer 說明文字）", "default": "glm_low", "options": [
        ("glm_low", "glm 低（預設）"),
        ("glm_normal", "glm 一般"),
        ("off", "關閉（只用範本，省 token）")]},
    "final_review": {"label": "合併前最後審查", "default": "glm_high", "options": [
        ("glm_high", "glm 高思考（預設）"),
        ("glm_normal", "glm 一般"),
        ("off", "關閉")]},
    "delegate": {"label": "代管的審查模型（第二意見）", "default": "glm_normal", "options": [
        ("glm_normal", "glm 一般（預設）"),
        ("glm_low", "glm 低（省 token）")]},
}
PURPOSE_DEFAULTS = {k: v["default"] for k, v in PURPOSES.items()}


def infer_tier(model_id: str) -> str:
    """偵測到的模型預設等級（使用者沒標過時用；明確選的等級、包含「不參與自動」永遠優先）。
    以片段比對：opus／ultra → 高階；haiku／luna／flash／mini／lite／nano → 經濟；sonnet／sol／terra／grok／pro → 標準；
    auto → 不參與；其他 → 標準。"""
    m = (model_id or "").lower()
    if m == "auto":
        return ""
    # 以完整片段比對（gemini-3-pro 的 pro 不會被當成別的字的一部分；o4-mini 的 mini 才算）
    toks = set(t for t in re.split(r"[^a-z0-9]+", m) if t)
    if toks & {"opus", "ultra"}:
        return "premium"
    if toks & {"haiku", "luna", "flash", "mini", "lite", "nano"}:
        return "economy"
    if toks & {"sonnet", "sol", "terra", "grok", "pro"}:
        return "standard"
    return "standard"


def _inferred(model_id: str) -> Dict[str, Any]:
    t = infer_tier(model_id)
    return {"id": model_id, "tier": t, "cost": TIER_COST.get(t, 0), "inferred": True}


def purpose_of(source: Any, key: str) -> str:
    """從 ModelPrefs、callable（回傳 dict）或 None 取用途設定；不認得的值回預設。"""
    vals = {}  # type: Dict[str, Any]
    try:
        if source is None:
            vals = {}
        elif hasattr(source, "purposes") and callable(source.purposes):
            vals = source.purposes()
        elif callable(source):
            vals = source()
        elif isinstance(source, dict):
            vals = source
    except Exception:
        log.debug("讀取用途設定失敗", exc_info=True)
        vals = {}
    v = (vals or {}).get(key)
    ok = {o[0] for o in PURPOSES.get(key, {}).get("options", [])}
    return v if v in ok else PURPOSE_DEFAULTS.get(key, "")


def _clean_id(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = v.strip()
    if len(v) > MAX_ID or any(c.isspace() for c in v) or any(ord(c) < 32 for c in v):
        return None
    return v


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f < 0 or f > 1000:
        return None
    return int(f) if f == int(f) else f


def _valid_old_entries(v: Any) -> bool:
    if not isinstance(v, list) or not v:
        return False
    for e in v:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or e.get("tier") not in TIERS:
            return False
        if _num(e.get("cost")) is None:
            return False
    return True


def _norm_entry(e: Any) -> Optional[Dict[str, Any]]:
    """{id, tier, cost[, context]}；tier "" ＝ 不參與自動派工。格式不對 → None。"""
    if not isinstance(e, dict):
        return None
    mid = _clean_id(e.get("id"))
    tier = e.get("tier", "")
    if mid is None or tier not in TIERS + ("",):
        return None
    cost = e.get("cost", TIER_COST.get(tier, 0) if tier else 0)
    cost = _num(cost)
    if cost is None:
        return None
    out = {"id": mid, "tier": tier, "cost": cost}  # type: Dict[str, Any]
    ctx = e.get("context")
    if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
        out["context"] = ctx
    return out


class PrefsError(ValueError):
    pass


class ModelPrefs:
    def __init__(self, path: str, cache_path: str, glm_model: Callable[[], str] = lambda: "glm-5.3"):
        self.path = path
        self.cache_path = cache_path
        self.glm_model = glm_model
        self._lock = threading.RLock()
        self._seats = {}  # type: Dict[str, str]
        self._purposes = {}  # type: Dict[str, str]
        self._models = {}  # type: Dict[str, List[Dict[str, Any]]]
        self._cache = {}  # type: Dict[str, Dict[str, Any]]
        # Dispatcher 讀這個 dict（就地更新，讓已建立的 Dispatcher 看到新設定）
        self.catalog = copy.deepcopy(DEFAULT_CATALOG)  # type: Dict[str, List[Dict[str, Any]]]
        self._load()
        self._load_cache()

    # ---- 讀檔 ----
    def _load(self) -> None:
        data = None
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError) as e:
                log.warning("模型設定讀取失敗，改用預設：%s（%s）", self.path, type(e).__name__)
        seats, purposes, models = {}, {}, {}  # type: Dict[str, str], Dict[str, str], Dict[str, List[Dict[str, Any]]]
        if isinstance(data, dict):
            if any(k in data for k in ("seats", "purposes", "models")):
                for s, m in (data.get("seats") or {}).items() if isinstance(data.get("seats"), dict) else ():
                    mid = _clean_id(m)
                    if s in SEATS and mid:
                        seats[s] = mid
                for k, v in (data.get("purposes") or {}).items() if isinstance(data.get("purposes"), dict) else ():
                    if k in PURPOSES and v in {o[0] for o in PURPOSES[k]["options"]}:
                        purposes[k] = v
                raw = data.get("models") if isinstance(data.get("models"), dict) else {}
                for s, entries in raw.items():
                    if s not in SEATS or not isinstance(entries, list):
                        continue
                    clean = [x for x in (_norm_entry(e) for e in entries) if x is not None]
                    if len(clean) != len(entries):
                        log.warning("模型設定有格式不對的項目，已略過：%s", s)
                    models[s] = clean
            elif all(_valid_old_entries(v) for v in data.values()):
                for s, entries in data.items():
                    if s not in SEATS:
                        log.warning("模型目錄有未知座位，略過：%s", s)
                        continue
                    models[s] = [x for x in (_norm_entry(e) for e in entries) if x is not None]
            else:
                log.warning("模型目錄格式錯誤（每個座位需為 [{id, tier: economy|standard|premium, cost}]），改用預設：%s",
                            self.path)
        with self._lock:
            self._seats, self._purposes, self._models = seats, purposes, models
            self._rebuild_catalog()

    def rows(self, seat: str) -> List[Dict[str, Any]]:
        """座位的模型與等級：你存過的（含「不參與自動」）優先，其餘依 infer_tier 推定（標 inferred）。
        偵測清單裡沒有、但你存過的 → missing。ACP 座位沒偵測過 → 只有你存過的。"""
        with self._lock:
            stored = {e["id"]: dict(e) for e in self._models.get(seat, [])}
            if seat in GLM_SEATS:
                return [dict(e) for e in DEFAULT_CATALOG[seat]]
            if seat == "claude":
                ids = list(CLAUDE_ALIASES) + [i for i in stored if i not in CLAUDE_ALIASES]
            else:
                c = self._cache.get(seat) or {}
                ids = [m["id"] for m in c.get("models") or []]
            out = []
            for i in ids:
                if i in stored:
                    out.append(stored[i])
                else:
                    d = next((dict(e) for e in DEFAULT_CATALOG.get(seat, []) if e["id"] == i and i), None)
                    out.append(d or _inferred(i))
            known = set(ids)
            for i, e in stored.items():
                if i not in known:
                    out.append(dict(e, missing=bool(ids)))
            return out

    def _rebuild_catalog(self) -> None:
        cat = copy.deepcopy(DEFAULT_CATALOG)
        for seat in SEATS:
            if seat in GLM_SEATS:
                continue
            ranked = [{k: v for k, v in e.items() if k in ("id", "tier", "cost", "context")}
                      for e in self.rows(seat) if e.get("tier") and not e.get("missing")]
            if ranked:
                cat[seat] = ranked
        self.catalog.clear()
        self.catalog.update(cat)

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        cache = {}
        if isinstance(data, dict):
            for s, v in data.items():
                if s in ACP_SEATS and isinstance(v, dict) and v.get("models"):  # 只有錯誤、沒有清單的舊紀錄不留
                    cache[s] = {"models": [m for m in (self._norm_detected(x) for x in v.get("models") or [])
                                           if m is not None],
                                "current": str(v.get("current") or "")[:MAX_ID],
                                "at": str(v.get("at") or "")[:40], "error": str(v.get("error") or "")[:300]}
        with self._lock:
            self._cache = cache
            self._rebuild_catalog()

    @staticmethod
    def _norm_detected(m: Any) -> Optional[Dict[str, str]]:
        if not isinstance(m, dict):
            return None
        mid = _clean_id(m.get("id", m.get("modelId")))
        if not mid:
            return None
        return {"id": mid, "name": str(m.get("name") or "")[:120], "description": str(m.get("description") or "")[:300]}

    def _write(self, path: str, data: Any) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _save(self) -> None:
        self._write(self.path, {"seats": dict(self._seats), "purposes": dict(self._purposes),
                                "models": copy.deepcopy(self._models)})

    # ---- 讀取 ----
    def seat_model(self, seat: str) -> str:
        with self._lock:
            return self._seats.get(seat, "")

    def seat_models(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._seats)

    def purposes(self) -> Dict[str, str]:
        with self._lock:
            return {k: self._purposes.get(k, d) for k, d in PURPOSE_DEFAULTS.items()}

    def purpose(self, key: str) -> str:
        return purpose_of(self, key)

    def entry(self, seat: str, model: str) -> Optional[Dict[str, Any]]:
        for e in self.rows(seat):
            if e["id"] == model and model:
                return e
        return None

    def available(self, seat: str) -> Optional[List[Dict[str, str]]]:
        """座位可用的模型清單 [{id, name, description}]；ACP 座位還沒偵測過 → None（未知）。"""
        if seat in GLM_SEATS:
            g = self.glm_model() or "glm-5.3"
            return [{"id": g, "name": g, "description": "一般思考量" if seat == "glm" else "高思考量"}]
        with self._lock:
            if seat == "claude":
                ids = list(CLAUDE_ALIASES) + [e["id"] for e in self._models.get("claude", [])
                                               if e["id"] and e["id"] not in CLAUDE_ALIASES]
                return [{"id": i, "name": i, "description": "別名（最新版）" if i in CLAUDE_ALIASES else "完整 id"}
                        for i in ids]
            c = self._cache.get(seat)
            if not c or not c.get("models"):
                return None
            return [dict(m) for m in c["models"]]

    def problem(self, seat: str, model: str) -> str:
        """指定的模型能不能用：可以回 ""；不在清單（或清單未知時無法確認）回中文原因。"""
        if not model:
            return ""
        if seat not in SEATS:
            return "不認得的座位：%s" % seat
        avail = self.available(seat)
        if avail is None:
            return "還沒偵測到 %s 的可用模型，無法確認「%s」；請到設定頁「模型」按「重新偵測」" % (seat, model)
        if model not in {m["id"] for m in avail}:
            return "「%s」不在 %s 的可用模型清單（%s）" % (
                model, seat, "、".join(m["id"] for m in avail[:12]) + ("…" if len(avail) > 12 else ""))
        return ""

    def detected(self, seat: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._cache.get(seat) or {})

    # ---- 寫入 ----
    def note_detected(self, seat: str, models: List[Any], current: str = "", error: str = "") -> None:
        """記下偵測結果（或錯誤）。錯誤時保留上次成功的清單。"""
        if seat not in ACP_SEATS:
            return
        clean = [m for m in (self._norm_detected(x) for x in models or []) if m is not None][:MAX_MODELS]
        with self._lock:
            old = self._cache.get(seat) or {}
            if error and not clean:
                rec = dict(old, error=str(error)[:300], at=now_iso())
                rec.setdefault("models", [])
                rec.setdefault("current", "")
            else:
                rec = {"models": clean, "current": str(current or "")[:MAX_ID], "at": now_iso(), "error": ""}
            self._cache[seat] = rec
            self._rebuild_catalog()
            try:
                self._write(self.cache_path, self._cache)
            except OSError:
                log.warning("無法寫入模型偵測快取：%s", self.cache_path)

    def update(self, body: Dict[str, Any]) -> List[str]:
        """套用設定頁送來的變更（只改有給的部分）。格式或值不對、或新選的模型不在清單 → PrefsError（全部不套用）。
        沒改到、但已經不在清單的舊選擇不擋存檔，回傳成警告（房間用到時會停下來問你）。"""
        if not isinstance(body, dict):
            raise PrefsError("格式不對")
        with self._lock:
            seats = dict(self._seats)
            purposes = dict(self._purposes)
            models = copy.deepcopy(self._models)
            if "models" in body:
                raw = body["models"]
                if not isinstance(raw, dict):
                    raise PrefsError("models 必須是 {座位: [模型]}")
                for s, entries in raw.items():
                    if s not in SEATS:
                        raise PrefsError("不認得的座位：%s" % s)
                    if s in GLM_SEATS:
                        continue  # glm 固定
                    if not isinstance(entries, list) or len(entries) > MAX_MODELS:
                        raise PrefsError("%s 的模型清單格式不對" % s)
                    clean, seen = [], set()
                    for e in entries:
                        n = _norm_entry(e)
                        if n is None or not n["id"]:
                            raise PrefsError("%s 的模型格式不對：%s" % (s, json.dumps(e, ensure_ascii=False)[:80]))
                        if n["id"] in seen:
                            continue
                        seen.add(n["id"])
                        clean.append(n)
                    models[s] = clean
            if "purposes" in body:
                raw = body["purposes"]
                if not isinstance(raw, dict):
                    raise PrefsError("purposes 格式不對")
                for k, v in raw.items():
                    if k not in PURPOSES:
                        raise PrefsError("不認得的用途：%s" % k)
                    if v not in {o[0] for o in PURPOSES[k]["options"]}:
                        raise PrefsError("%s 不能選 %s" % (PURPOSES[k]["label"], v))
                    purposes[k] = v
            if "seats" in body:
                raw = body["seats"]
                if not isinstance(raw, dict):
                    raise PrefsError("seats 格式不對")
                for s, m in raw.items():
                    if s not in SEATS:
                        raise PrefsError("不認得的座位：%s" % s)
                    if m in ("", None, "auto"):
                        seats.pop(s, None)
                        continue
                    if s in GLM_SEATS:
                        raise PrefsError("%s 固定用 glm 模型，不能指定" % s)
                    mid = _clean_id(m)
                    if not mid:
                        raise PrefsError("%s 的模型 id 格式不對" % s)
                    seats[s] = mid
            # 驗證：新選的模型要在清單裡（用新的目錄判斷 claude 自訂 id）；沒改的舊選擇只警告
            warnings = []  # type: List[str]
            old_models = self._models
            self._models = models
            try:
                for s, m in seats.items():
                    p = self.problem(s, m)
                    if not p:
                        continue
                    if m != self._seats.get(s):
                        raise PrefsError(p)
                    warnings.append("（未變更，仍保留）%s" % p)
            finally:
                self._models = old_models
            self._seats, self._purposes, self._models = seats, purposes, models
            self._rebuild_catalog()
            self._save()
            return warnings

    # ---- 給 API ----
    def view(self, available_seats: Optional[set] = None, detecting: Optional[set] = None,
             labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        out = {}
        with self._lock:
            for s in SEATS:
                avail = self.available(s)
                info = {m["id"]: m for m in avail or []}
                rows = []
                for e in self.rows(s):
                    m = info.get(e["id"]) or {"id": e["id"], "name": e["id"], "description": ""}
                    row = dict(m, tier=e.get("tier", ""), cost=e.get("cost", 0),
                               inferred=bool(e.get("inferred")))
                    if e.get("missing"):
                        row.update(missing=True, description="不在最近一次偵測結果")
                    rows.append(row)
                c = self._cache.get(s) or {}
                out[s] = {
                    "label": (labels or {}).get(s, s),
                    "available": True if available_seats is None else s in available_seats,
                    "source": "detected" if s in ACP_SEATS else ("catalog" if s == "claude" else "fixed"),
                    "pref": self._seats.get(s, ""),
                    "models": rows,
                    "known": avail is not None,
                    "current": c.get("current", ""),
                    "detected_at": c.get("at", ""),
                    "error": c.get("error", ""),
                    "detecting": bool(detecting and s in detecting),
                    "can_add": s == "claude",
                    "auto_eligible": s in AUTO_SEATS,
                    "editable": s not in GLM_SEATS,
                }
        return {"seats": out, "order": list(SEATS), "purposes": self.purposes(), "auto_label": AUTO_LABEL,
                "auto_seats": list(AUTO_SEATS), "manual_only_text": MANUAL_ONLY,
                "purpose_options": {k: {"label": v["label"], "default": v["default"],
                                        "options": [{"id": o[0], "label": o[1]} for o in v["options"]]}
                                    for k, v in PURPOSES.items()},
                "tiers": [{"id": "", "label": "不參與自動"}, {"id": "economy", "label": "經濟"},
                          {"id": "standard", "label": "標準"}, {"id": "premium", "label": "高階"}]}


def load_catalog(path: str) -> Dict[str, List[Dict[str, Any]]]:
    """只讀目錄（相容舊呼叫端）：新、舊格式的 team_models.json 都可以。"""
    p = ModelPrefs(path, os.devnull)
    return copy.deepcopy(p.catalog)


def validate_pref(prefs: Optional[ModelPrefs], seat: str, model: str) -> Tuple[bool, str]:
    if prefs is None or not model:
        return True, ""
    p = prefs.problem(seat, model)
    return (not p), p
