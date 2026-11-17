"""模型路由：Decision-1（/v1/systemone，只回機率）與 glm-5.3（一般文字模型）。

- decide_allow：權限請求的自動允許判斷（門檻見 Global Constraints）。
- probability：單一是非題的機率（Decision-1 優先，失敗改 glm）。
- write：glm 產生文字（一般思考量逾時 → 以低思考量重試一次）。
金鑰只放在 ModelConfig 與 HTTP 標頭裡；repr、例外訊息與 usage 都不含金鑰。

Task 17：每一次實際呼叫（Decision-1 或 glm）都產生一筆 team.usage.ModelCall，交給 listeners
（Office 寫進房間事件與用量帳）；進行中的呼叫由 inflight() 取得。呼叫端以 ctx 傳
{room_id, role, purpose[, pass_min]}；舊呼叫端不傳 ctx 也照常運作（記成沒有房間的呼叫）。
注入的 d1／glm 函式可回傳 ModelReply（附 usage 與伺服器回報的模型名），或像以前一樣回傳答案／文字。
"""
import functools
import inspect
import itertools
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from ai_client import DecisionClient, build_llm_decision_prompt, parse_llm_decision
from team.events import DETAIL_MAX, clip, mask_detail
from team.usage import ModelCall, now_iso

log = logging.getLogger(__name__)

D1_TIMEOUT = 10.0
GLM_TIMEOUTS = {"low": 30.0, "normal": 150.0}
D1_CONCURRENCY = 8
GLM_CONCURRENCY = {"low": 4, "normal": 1}
GLM_MAX_TOKENS = 8000

def parse_opinion(text: Any) -> Tuple[Optional[bool], str]:
    """審查模型第二意見：從回覆中找最後一個含 ok 的 JSON 物件；找不到 → (None, 原因)。"""
    if not isinstance(text, str):
        return None, "回覆不是文字"
    for raw in reversed(re.findall(r"\{[^{}]*\}", text)):
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("ok"), bool):
            return obj["ok"], str(obj.get("reason") or "").strip()[:500]
    return None, "回覆沒有可判讀的 ok／reason"


# 允許判斷門檻
SAFE_RISK, SAFE_ALLOW = 0.3, 0.8
HIGH_RISK = 0.7
STRICT_RISK, STRICT_ALLOW = 0.15, 0.9


class ModelError(Exception):
    pass


def _mask(v: str) -> str:
    return "***" if v else "''"


@dataclass
class ModelConfig:
    base_url: str
    glm_key: str
    decision1_key: str
    glm_model: str = "glm-5.3"
    d1_model: str = "Decision-1"

    def __repr__(self) -> str:
        return "ModelConfig(base_url=%r, glm_key=%s, decision1_key=%s, glm_model=%r, d1_model=%r)" % (
            self.base_url, _mask(self.glm_key), _mask(self.decision1_key), self.glm_model, self.d1_model)

    __str__ = __repr__


def load_model_config(path: str) -> Optional[ModelConfig]:
    """讀 litellm.local.json：base_url、key（glm）、decision1_key；可選 glm_model、d1_model。

    檔案不存在、格式錯誤或缺欄位 → None（不記錄任何值）。
    """
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    base_url = d.get("base_url")
    glm_key = d.get("key") or d.get("glm_key")
    d1_key = d.get("decision1_key")
    if not all(isinstance(v, str) and v.strip() for v in (base_url, glm_key, d1_key)):
        log.warning("模型設定缺少欄位（需要 base_url、key、decision1_key）：%s", path)
        return None
    cfg = ModelConfig(base_url=base_url.strip(), glm_key=glm_key.strip(), decision1_key=d1_key.strip())
    if isinstance(d.get("glm_model"), str) and d["glm_model"].strip():
        cfg.glm_model = d["glm_model"].strip()
    if isinstance(d.get("d1_model"), str) and d["d1_model"].strip():
        cfg.d1_model = d["d1_model"].strip()
    return cfg


@dataclass
class AllowVerdict:
    auto: bool
    allow: float
    risk: float
    by: str
    reason: str
    detail: str = ""  # 每個模型各自的數字與版本，例：Decision-1 allow 0.90 risk 0.50；glm-5.3 allow 0.95 risk 0.05


@dataclass
class ModelReply:
    """注入函式或 HTTP 實作的回傳：value（答案字典或文字）、usage（模型回報的 token）、model（伺服器回報的模型名）。"""
    value: Any
    usage: Optional[Dict[str, Any]] = None
    model: str = ""


PURPOSE_ALLOW = "判斷指令是否安全"
PURPOSE_GRAY = "灰色地帶複核指令是否安全"
PURPOSE_STRICT = "Decision-1 不可用，判斷指令是否安全（嚴格門檻）"
PURPOSE_GLM_ONLY = "判斷指令是否安全（設定為只用 glm，嚴格門檻）"
PURPOSE_OPINION = "代管：審查模型第二意見"
PURPOSE_CHOOSE = "代管：選擇選項"
OPINION_FORMAT = '只回一個 JSON：{"ok": true 或 false, "reason": "一兩句理由（繁體中文）"}'


@functools.lru_cache(maxsize=256)
def _accepts_ctx(fn: Any) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "ctx" or p.kind == p.VAR_KEYWORD for p in params)


def call_ctx(fn: Callable[..., Any], *args: Any, ctx: Optional[Dict[str, Any]] = None, **kw: Any) -> Any:
    """呼叫路由器方法並帶上 ctx；舊介面（沒有 ctx 參數，例如測試或外部的假路由器）就不帶。"""
    if ctx is not None:
        target = getattr(fn, "__func__", fn)
        try:
            ok = _accepts_ctx(target)
        except TypeError:  # 不可雜湊
            ok = False
        if ok:
            kw["ctx"] = ctx
    return fn(*args, **kw)


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class _Track:
    """一次實際的模型呼叫：進入時登記為進行中，結束時產生 ModelCall 交給 listeners。"""

    def __init__(self, router: "ModelRouter", kind: str, model: str, effort: str,
                 ctx: Optional[Dict[str, Any]], purpose: str):
        self.ctx = ctx  # 呼叫端的 ctx（dict）：結束時寫回 model_used／model_kind／effort_used（實際回答的模型）
        ctx = ctx or {}
        self.router = router
        self.kind = kind
        self.model = model
        self.effort = effort
        self.room_id = str(ctx.get("room_id") or "")
        self.role = str(ctx.get("role") or "")
        self.purpose = str(ctx.get("purpose") or purpose or "")
        self.ok = True
        self.result = ""
        self.usage = None  # type: Optional[Dict[str, Any]]
        self.tokens = "not_counted" if kind == "d1" else "unreported"
        self.latency_ms = 0
        self._t0 = 0.0
        self._id = 0
        self.detail = {}  # type: Dict[str, Any]  # Task 19：題目、狀態摘要、答案／生成文字

    def __enter__(self) -> "_Track":
        self._t0 = time.monotonic()
        self._id = self.router._inflight_add(self)
        return self

    def start(self) -> None:
        """拿到同時呼叫名額、真的送出：從這裡起算延遲；進行中的呼叫從「排隊中」改為「進行中」。"""
        self._t0 = time.monotonic()
        self.router._inflight_start(self._id)

    def got(self, ret: Any) -> Any:
        """記下回應時間、伺服器回報的模型名與 usage；回傳內容本身。"""
        self.latency_ms = int((time.monotonic() - self._t0) * 1000)
        if isinstance(ret, ModelReply):
            if isinstance(ret.model, str) and ret.model.strip():
                self.model = ret.model.strip()[:80]
            u = ret.usage if isinstance(ret.usage, dict) else None
            if u:
                p, c, t = (_int(u.get("prompt_tokens")), _int(u.get("completion_tokens")),
                           _int(u.get("total_tokens")))
                if p is not None or c is not None or t is not None:
                    p, c = p or 0, c or 0
                    self.usage = {"prompt_tokens": p, "completion_tokens": c,
                                  "total_tokens": t if t is not None else p + c}
                    self.tokens = "reported"
            return ret.value
        return ret

    def __exit__(self, et: Any, e: Any, tb: Any) -> bool:
        if not self.latency_ms:
            self.latency_ms = int((time.monotonic() - self._t0) * 1000)
        self.router._inflight_remove(self._id)
        if et is not None:
            self.ok = False
            self.result = self.result or "%s%s" % (type(e).__name__, "（逾時）" if _is_timeout(e) else "")
        u = self.usage or {}
        call = ModelCall(ts=now_iso(), room_id=self.room_id, role=self.role, model=self.model,
                         effort=self.effort, purpose=self.purpose, latency_ms=self.latency_ms, ok=self.ok,
                         result=self.result[:200], kind=self.kind,
                         prompt_tokens=u.get("prompt_tokens", 0), completion_tokens=u.get("completion_tokens", 0),
                         total_tokens=u.get("total_tokens", 0), tokens=self.tokens, detail=self._detail())
        if self.ok and isinstance(self.ctx, dict):
            self.ctx["model_used"] = self.model
            self.ctx["model_kind"] = self.kind
            self.ctx["effort_used"] = self.effort
        self.router._emit(call)
        return False

    def _detail(self) -> Dict[str, Any]:
        d = dict(self.detail)
        lim = prob01((self.ctx or {}).get("pass_min")) if isinstance(self.ctx, dict) else None
        if lim is not None:
            d["threshold"] = lim
        d["verdict"] = self.result if self.ok else "失敗：" + (self.result or "")
        cfg = self.router.cfg
        secrets = (getattr(cfg, "glm_key", ""), getattr(cfg, "decision1_key", "")) if cfg is not None else ()
        # 先遮蔽再截斷（先截可能把金鑰切成兩半而遮不到）
        d = mask_detail({k: v for k, v in d.items() if v not in (None, "", {}, [])}, secrets)
        for k, v in list(d.items()):
            if isinstance(v, str):
                d[k] = clip(v, STATE_EXCERPT if k == "state_excerpt" else DETAIL_MAX)
        return d


STATE_EXCERPT = 2000


def _questions_text(questions: Any) -> str:
    """問模型的題目全文：每題「名稱（型別）：說明」。"""
    if not isinstance(questions, dict):
        return str(questions)
    lines = []
    for name, q in questions.items():
        q = q if isinstance(q, dict) else {"instructions": q}
        lines.append("%s（%s）：%s" % (name, q.get("type", ""), q.get("instructions", "")))
    return "\n".join(lines)


def _answers_text(answers: Any) -> str:
    """Decision-1 的回答：每題的機率（或原值）。"""
    if not isinstance(answers, dict):
        return str(answers)
    lines = []
    for name, v in answers.items():
        if isinstance(v, dict):
            v = "、".join("%s %s" % (k, ("%.2f" % x) if isinstance(x, float) else x) for k, x in v.items())
        lines.append("%s：%s" % (name, v))
    return "\n".join(lines)


def approval_questions() -> Dict[str, Dict[str, Any]]:
    # 延後匯入：automation 會載入 Windows UI 自動化模組，只在真的要判斷時才付出代價。
    from automation import build_approval_questions
    return build_approval_questions()


def _is_timeout(e: BaseException) -> bool:
    while e is not None:
        if isinstance(e, (TimeoutError, requests.Timeout)):
            return True
        e = e.__cause__
    return False


def prob01(v: Any) -> Optional[float]:
    """機率值：非數字、NaN、無限大 → None（當成沒有答案）；其餘夾在 0~1。"""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return max(0.0, min(1.0, f))


def _noul(answers: Any, name: str) -> Optional[float]:
    try:
        return prob01(answers[name]["noul"])
    except (KeyError, TypeError, IndexError):
        return None


def _allow_risk(answers: Any) -> Optional[Tuple[float, float]]:
    allow, risk = _noul(answers, "allow"), _noul(answers, "risk")
    if allow is None or risk is None:
        return None
    return allow, risk


def _ar(allow: float, risk: float) -> str:
    return "allow %.2f risk %.2f" % (allow, risk)


class ModelRouter:
    def __init__(self, cfg: Optional[ModelConfig],
                 d1: Optional[Callable[[str, Dict], Any]] = None,
                 glm: Optional[Callable[[str, str, float], Any]] = None):
        self.cfg = cfg
        self.d1_name = cfg.d1_model if cfg else "Decision-1"
        self.glm_name = cfg.glm_model if cfg else "glm-5.3"
        self._d1 = d1 if d1 is not None else self._http_d1
        self._glm = glm if glm is not None else self._http_glm
        self._d1_sem = threading.BoundedSemaphore(D1_CONCURRENCY)
        self._glm_sem = {k: threading.BoundedSemaphore(n) for k, n in GLM_CONCURRENCY.items()}
        self._lock = threading.Lock()
        self._usage = {k: 0 for k in ("d1_calls", "d1_fail", "d1_timeout",
                                      "glm_calls", "glm_fail", "glm_timeout")}
        # Task 17：每次呼叫的紀錄交給 listeners；進行中的呼叫變動時呼叫 on_inflight（例如通知畫面更新）
        self.listeners = []  # type: List[Callable[[ModelCall], None]]
        self.on_inflight = None  # type: Optional[Callable[[], None]]
        self._inflight = {}  # type: Dict[int, Dict[str, str]]
        self._ids = itertools.count(1)
        # Task 18：用途設定（team.modelprefs.ModelPrefs 或回傳 dict 的函式）；None ＝ 預設
        self.purposes = None  # type: Any

    def _purpose(self, key: str) -> str:
        from team.modelprefs import purpose_of  # 延後匯入，避免循環
        return purpose_of(self.purposes, key)

    def __repr__(self) -> str:
        return "ModelRouter(%r)" % (self.cfg,)

    # ---- 預設 HTTP 實作（測試一律注入假函式）----
    def _http_d1(self, state: str, questions: Dict) -> ModelReply:
        if self.cfg is None:
            raise ModelError("缺少 litellm.local.json，Decision-1 不可用")
        client = DecisionClient(self.cfg.base_url, self.cfg.decision1_key, self.cfg.d1_model, timeout=D1_TIMEOUT)
        answers, meta = client.systemone_meta(state, questions)
        return ModelReply(answers, meta.get("usage"), meta.get("model") or "")

    def _http_glm(self, prompt: str, effort: str, timeout: float) -> ModelReply:
        if self.cfg is None:
            raise ModelError("缺少 litellm.local.json，glm 不可用")
        payload: Dict[str, Any] = {"model": self.cfg.glm_model, "max_tokens": GLM_MAX_TOKENS,
                                   "messages": [{"role": "user", "content": prompt}]}
        if effort == "low":
            payload["reasoning_effort"] = "low"
        try:
            r = requests.post(self.cfg.base_url.rstrip("/") + "/v1/chat/completions",
                              headers={"Authorization": "Bearer " + self.cfg.glm_key},
                              json=payload, timeout=timeout)
        except requests.Timeout as e:
            raise TimeoutError("%s 逾時（%s 秒）" % (self.cfg.glm_model, timeout)) from e
        except requests.RequestException as e:
            raise ModelError("%s 連線失敗：%s" % (self.cfg.glm_model, type(e).__name__)) from e
        if r.status_code != 200:
            raise ModelError("%s 回應 %s" % (self.cfg.glm_model, r.status_code))
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ModelError("%s 回應格式錯誤" % self.cfg.glm_model) from e
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        model = data.get("model") if isinstance(data.get("model"), str) else ""
        return ModelReply(text, usage, model)

    # ---- 計數、進行中、紀錄 ----
    def _count(self, key: str) -> None:
        with self._lock:
            self._usage[key] += 1

    def usage(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._usage)

    def inflight(self) -> List[Dict[str, str]]:
        """進行中的模型呼叫：[{room_id, role, model, effort, purpose, since, queued_since, state, state_label}]。
        state：queued（等同時呼叫名額；since 暫時＝排隊開始）｜running（since 改成真的送出的時間）。"""
        with self._lock:
            return [dict(v) for _, v in sorted(self._inflight.items())]

    def _inflight_add(self, t: "_Track") -> int:
        i = next(self._ids)
        with self._lock:
            self._inflight[i] = {"room_id": t.room_id, "role": t.role, "model": t.model, "effort": t.effort,
                                 "purpose": t.purpose, "since": now_iso(), "queued_since": now_iso(),
                                 "state": "queued", "state_label": "排隊中"}
        self._inflight_changed()
        return i

    def _inflight_start(self, i: int) -> None:
        with self._lock:
            v = self._inflight.get(i)
            if v is None:
                return
            v.update(since=now_iso(), state="running", state_label="進行中")
        self._inflight_changed()

    def _inflight_remove(self, i: int) -> None:
        with self._lock:
            self._inflight.pop(i, None)
        self._inflight_changed()

    def _inflight_changed(self) -> None:
        fn = self.on_inflight
        if fn is not None:
            try:
                fn()
            except Exception:
                log.debug("on_inflight 失敗", exc_info=True)

    def _emit(self, call: ModelCall) -> None:
        for fn in list(self.listeners):
            try:
                fn(call)
            except Exception:
                log.debug("模型呼叫紀錄失敗", exc_info=True)

    def _track(self, kind: str, model: str, effort: str, ctx: Optional[Dict[str, Any]], purpose: str) -> _Track:
        return _Track(self, kind, model, effort, ctx, purpose)

    def _call_d1(self, state: str, questions: Dict, ctx: Optional[Dict[str, Any]] = None, purpose: str = "",
                 summarize: Optional[Callable[[Dict], Tuple[bool, str]]] = None) -> Tuple[Dict, str]:
        """回傳（answers, 實際模型名）。summarize(answers) → (是否可用, 結果摘要)。"""
        self._count("d1_calls")
        with self._track("d1", self.d1_name, "-", ctx, purpose) as t:
            t.detail = {"question": _questions_text(questions), "state_excerpt": state}
            try:
                with self._d1_sem:
                    t.start()
                    answers = t.got(self._d1(state, questions))
            except Exception as e:
                self._count("d1_timeout" if _is_timeout(e) else "d1_fail")
                raise
            if not isinstance(answers, dict):
                self._count("d1_fail")
                t.ok, t.result = False, "回應格式錯誤"
                raise ModelError("Decision-1 回應格式錯誤")
            t.detail["answer"] = _answers_text(answers)
            if summarize is not None:
                t.ok, t.result = summarize(answers)
            return answers, t.model

    def _call_glm(self, prompt: str, effort: str, ctx: Optional[Dict[str, Any]] = None, purpose: str = "",
                  summarize: Optional[Callable[[str], Tuple[bool, str]]] = None,
                  detail: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        """回傳（文字, 實際模型名）。detail：給時間軸看的題目／狀態摘要（沒給就記整段提示）。"""
        effort = "normal" if effort == "normal" else "low"
        self._count("glm_calls")
        with self._track("glm", self.glm_name, effort, ctx, purpose) as t:
            t.detail = dict(detail) if detail else {"question": prompt}
            try:
                with self._glm_sem[effort]:
                    t.start()
                    text = t.got(self._glm(prompt, effort, GLM_TIMEOUTS[effort]))
            except Exception as e:
                self._count("glm_timeout" if _is_timeout(e) else "glm_fail")
                raise
            if not isinstance(text, str) or not text.strip():
                self._count("glm_fail")
                t.ok, t.result = False, "回覆是空的"
                raise ModelError("%s 回覆是空的" % self.glm_name)
            t.detail["answer"] = text
            if summarize is not None:
                t.ok, t.result = summarize(text)
            else:
                t.result = "產生 %d 字" % len(text.strip())
            return text, t.model

    def _glm_answers(self, state: str, questions: Dict, ctx: Optional[Dict[str, Any]] = None, purpose: str = "",
                     summarize: Optional[Callable[[Dict], Tuple[bool, str]]] = None) -> Tuple[Dict, str]:
        def summ(raw: str) -> Tuple[bool, str]:
            ans = parse_llm_decision(raw, questions)
            return summarize(ans) if summarize is not None else (True, "已回答")
        raw, model = self._call_glm(build_llm_decision_prompt(state, questions), "low", ctx, purpose, summ,
                                    detail={"question": _questions_text(questions), "state_excerpt": state})
        return parse_llm_decision(raw, questions), model

    @staticmethod
    def _summ_allow(answers: Dict) -> Tuple[bool, str]:
        pair = _allow_risk(answers)
        return (True, _ar(*pair)) if pair is not None else (False, "回應缺少 allow／risk")

    @staticmethod
    def _summ_prob(name: str, ctx: Optional[Dict[str, Any]]) -> Callable[[Dict], Tuple[bool, str]]:
        def summ(answers: Dict) -> Tuple[bool, str]:
            p = _noul(answers, name)
            if p is None:
                return False, "回應沒有機率"
            lim = prob01((ctx or {}).get("pass_min"))
            if lim is None:
                return True, "機率 %.2f" % p
            return True, ("%.2f ≥ %.2f 通過" % (p, lim)) if p >= lim else ("%.2f < %.2f 不通過" % (p, lim))
        return summ

    # ---- 對外介面 ----
    def decide_allow(self, state_text: str, ctx: Optional[Dict[str, Any]] = None) -> AllowVerdict:
        try:
            questions = approval_questions()
        except Exception as e:
            log.warning("無法建立允許判斷題目，交給使用者：%s", type(e).__name__)
            return AllowVerdict(False, 0.0, 1.0, "none", "無法建立判斷題目，交給使用者")
        mode = self._purpose("permission")  # d1+glm（預設）｜glm（只用 glm，嚴格門檻）｜d1（只用 Decision-1）
        d1_pair = None
        d1_model = self.d1_name
        if mode != "glm":
            try:
                answers, d1_model = self._call_d1(state_text, questions, ctx, PURPOSE_ALLOW, self._summ_allow)
                d1_pair = _allow_risk(answers)
                if d1_pair is None:
                    self._count("d1_fail")
            except Exception as e:
                log.info("Decision-1 不可用：%s", type(e).__name__)

        if mode == "d1" and d1_pair is None:
            return AllowVerdict(False, 0.0, 1.0, "none", "設定為只用 Decision-1，但 Decision-1 不可用，交給使用者")
        if d1_pair is not None:
            allow, risk = d1_pair
            d1_detail = "%s %s" % (d1_model, _ar(allow, risk))
            if risk <= SAFE_RISK and allow >= SAFE_ALLOW:
                return AllowVerdict(True, allow, risk, d1_model, "Decision-1 判定安全", d1_detail)
            if risk > HIGH_RISK:
                return AllowVerdict(False, allow, risk, d1_model, "Decision-1 判定高風險，交給使用者", d1_detail)
            if mode == "d1":
                return AllowVerdict(False, allow, risk, d1_model, "灰色地帶（設定為只用 Decision-1），交給使用者",
                                    d1_detail)
            g_model = self.glm_name
            try:
                ans, g_model = self._glm_answers(state_text, questions, ctx, PURPOSE_GRAY, self._summ_allow)
                g = _allow_risk(ans)
            except Exception as e:
                log.info("%s 不可用：%s", self.glm_name, type(e).__name__)
                g = None
            if g is None:
                return AllowVerdict(False, allow, risk, d1_model,
                                    "灰色地帶且 %s 無法判斷，交給使用者" % self.glm_name, d1_detail)
            g_allow, g_risk = g
            both = "%s+%s" % (d1_model, g_model)
            detail = "%s；%s %s" % (d1_detail, g_model, _ar(g_allow, g_risk))
            worst_allow, worst_risk = min(allow, g_allow), max(risk, g_risk)
            if g_risk <= SAFE_RISK and g_allow >= SAFE_ALLOW:
                return AllowVerdict(True, worst_allow, worst_risk, both, "灰色地帶，兩個模型都判定安全", detail)
            return AllowVerdict(False, worst_allow, worst_risk, both, "灰色地帶且未達安全門檻，交給使用者", detail)

        g_model = self.glm_name
        strict_purpose = PURPOSE_GLM_ONLY if mode == "glm" else PURPOSE_STRICT
        try:
            ans, g_model = self._glm_answers(state_text, questions, ctx, strict_purpose, self._summ_allow)
            g = _allow_risk(ans)
        except Exception as e:
            log.info("%s 不可用：%s", self.glm_name, type(e).__name__)
            g = None
        if g is None:
            return AllowVerdict(False, 0.0, 1.0, "none", "判斷模型都不可用，交給使用者")
        g_allow, g_risk = g
        detail = "%s %s" % (g_model, _ar(g_allow, g_risk))
        head = "設定為只用 %s" % self.glm_name if mode == "glm" else "Decision-1 不可用"
        if g_risk <= STRICT_RISK and g_allow >= STRICT_ALLOW:
            return AllowVerdict(True, g_allow, g_risk, g_model,
                                "%s，%s 以嚴格門檻判定安全" % (head, self.glm_name), detail)
        return AllowVerdict(False, g_allow, g_risk, g_model,
                            "%s，%s 未達嚴格門檻，交給使用者" % (head, self.glm_name), detail)

    def probability(self, state_text: str, question: str, ctx: Optional[Dict[str, Any]] = None) -> Optional[float]:
        questions = {"q": {"type": "noul", "instructions": question}}
        purpose = (ctx or {}).get("purpose") or "是非題"
        if self._purpose("judge") != "glm":  # 設定為 glm 低 → 不問 Decision-1
            try:
                answers, _ = self._call_d1(state_text, questions, ctx, purpose, self._summ_prob("q", ctx))
                p = _noul(answers, "q")
                if p is not None:
                    return p
                self._count("d1_fail")
            except Exception as e:
                log.info("Decision-1 不可用：%s", type(e).__name__)
        try:
            ans, _ = self._glm_answers(state_text, questions, ctx, purpose, self._summ_prob("q", ctx))
            p = _noul(ans, "q")
            if p is not None:
                return p
        except Exception as e:
            log.info("%s 不可用：%s", self.glm_name, type(e).__name__)
        return None

    def write(self, prompt: str, effort: str = "low", ctx: Optional[Dict[str, Any]] = None) -> str:
        purpose = (ctx or {}).get("purpose") or "產生文字"
        if effort == "normal":
            try:
                return self._call_glm(prompt, "normal", ctx, purpose)[0]
            except Exception as e:
                if not _is_timeout(e):
                    raise ModelError("%s 產生文字失敗：%s" % (self.glm_name, type(e).__name__)) from e
                log.info("%s 一般思考量逾時，改用低思考量重試", self.glm_name)
        try:
            return self._call_glm(prompt, "low", ctx, purpose)[0]
        except Exception as e:
            raise ModelError("%s 產生文字失敗：%s" % (self.glm_name, type(e).__name__)) from e

    # ---- Task 20：代管 ----
    def second_opinion(self, question: str, material: str, ctx: Optional[Dict[str, Any]] = None) -> Tuple[Optional[bool], str]:
        """審查模型（設定頁「代管的審查模型」，預設 glm 一般）的第二意見：(通過與否, 理由)；不可用 → (None, 原因)。"""
        purpose = (ctx or {}).get("purpose") or PURPOSE_OPINION
        effort = "low" if self._purpose("delegate") == "glm_low" else "normal"
        from team.personas import Fence  # 延後匯入
        f = Fence()  # 每則訊息一個隨機標記；代理內容裡的框字樣先拿掉，無法提早關框
        prompt = ("你是審查模型，替不在場的使用者做決定前提供第二意見。\n%s\n\n問題：%s\n\n%s\n\n%s"
                  % (f.notice, question, f.wrap("資料", material), OPINION_FORMAT))
        detail = {"question": question, "state_excerpt": material}

        def summ(text: str) -> Tuple[bool, str]:
            ok, reason = parse_opinion(text)
            if ok is None:
                return False, reason
            return True, ("通過：" if ok else "不通過：") + reason[:150]
        for eff in ((effort, "low") if effort == "normal" else (effort,)):
            try:
                text, _ = self._call_glm(prompt, eff, ctx, purpose, summ, detail)
                return parse_opinion(text)
            except Exception as e:
                if eff == "normal" and _is_timeout(e):
                    log.info("%s 一般思考量逾時，改用低思考量重試", self.glm_name)
                    continue
                log.info("審查模型不可用：%s", type(e).__name__)
                return None, "審查模型不可用（%s）" % type(e).__name__
        return None, "審查模型不可用"

    def choose(self, state_text: str, question: str, options: List[Dict[str, str]],
               ctx: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Dict[str, float]]:
        """Decision-1 從選項中選一個（每個選項一題「選這個對嗎」的機率，取最高）；Decision-1 不可用改 glm 低。
        回傳（選項 id 或 None, {選項 id: 機率}）。"""
        opts = [o for o in options or [] if isinstance(o, dict) and o.get("id")]
        if not opts:
            return None, {}
        from team.personas import fenced  # 延後匯入
        state_text = fenced("狀態（題目細節、代理與檢查輸出）", state_text)  # 狀態裡有代理寫的內容：整段放進隨機資料框
        questions = {"o%d" % i: {"type": "noul", "instructions": "%s——應該選「%s」嗎？" % (question, o.get("label") or o["id"])}
                     for i, o in enumerate(opts)}
        purpose = (ctx or {}).get("purpose") or PURPOSE_CHOOSE

        def pick(answers: Any) -> Dict[str, float]:
            out = {}  # type: Dict[str, float]
            for i, o in enumerate(opts):
                p = _noul(answers, "o%d" % i)
                if p is not None:
                    out[str(o["id"])] = p
            return out

        def summ(answers: Dict) -> Tuple[bool, str]:
            got = pick(answers)
            if not got:
                return False, "回應沒有機率"
            best = max(got, key=lambda k: got[k])
            return True, "選「%s」（%.2f）" % (best, got[best])
        calls = []  # type: List[Callable[[], Dict]]
        if self._purpose("judge") != "glm":
            calls.append(lambda: self._call_d1(state_text, questions, ctx, purpose, summ)[0])
        calls.append(lambda: self._glm_answers(state_text, questions, ctx, purpose, summ)[0])
        for call in calls:
            try:
                got = pick(call())
            except Exception as e:
                log.info("選擇選項失敗：%s", type(e).__name__)
                continue
            if got:
                return max(got, key=lambda k: got[k]), got
        return None, {}
