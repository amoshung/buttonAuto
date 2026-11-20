"""模型呼叫與 CLI 用量帳（Task 17）。

- ModelCall：ModelRouter 每一次實際呼叫（Decision-1 或 glm）一筆：誰（房間、角色）、用哪個模型版本、
  為了什麼、結果、延遲、token。token 只記模型回報的數字；沒有回報就標 unreported，不補數字。
- UsageBook：全域用量帳。每筆寫一行到 sessions/team/usage-YYYYMMDD.jsonl（每天一個檔），並在記憶體彙總「今日」與「本次啟動」，
  依模型、房間、角色、CLI 座位。重新啟動時從檔案讀回今日的部分。
- 不記錄提示詞、回覆內容或金鑰。
- 示範房的紀錄（model "scripted"、demo 為真的 CLI 回合）只在記憶體彙總、標「示範」，不寫進用量檔。
"""
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

ROLE_LABELS = {"po": "PO", "tech_lead": "Tech Lead", "scrum": "Scrum Master", "qa": "QA", "reviewer": "Reviewer",
               "dispatcher": "派工", "policy": "權限把關"}
EFFORT_LABELS = {"low": "低思考量", "normal": "一般思考量", "-": ""}
UNREPORTED = "—（未回報）"
CLI_DEFAULT = "CLI 預設（未回報）"
DEMO_MODEL = "scripted"
# tokens：reported＝模型回報了 usage；unreported＝應該有但沒回報；not_counted＝Decision-1 不回報 token；demo＝示範假模型
TOKEN_STATES = ("reported", "unreported", "not_counted", "demo")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ModelCall:
    ts: str
    room_id: str
    role: str
    model: str
    effort: str
    purpose: str
    latency_ms: int
    ok: bool
    result: str
    kind: str = "glm"  # d1 | glm
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tokens: str = "unreported"
    # Task 19：給時間軸展開看的細節（題目、狀態摘要、答案或生成文字、門檻、結論）；已遮蔽金鑰。不寫進用量帳。
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def model_tag(kind: str, model: str) -> str:
    if model == DEMO_MODEL:
        return "示範"
    return "D1" if kind == "d1" else "GLM"


def model_name(model: str, effort: str = "-", kind: str = "") -> str:
    """給人看的模型名：glm-5.3（低思考量）、Decision-1、示範（假模型，代 glm 低思考量）。"""
    eff = EFFORT_LABELS.get(effort or "-", effort)
    if model == DEMO_MODEL:
        stand = "Decision-1" if kind == "d1" else "glm" + (" " + eff if eff else "") if kind == "glm" else ""
        return "示範（假模型%s）" % ("，代 " + stand if stand else "")
    return "%s（%s）" % (model, eff) if eff else model


def tokens_text(c: Any) -> str:
    get = (lambda k: c.get(k)) if isinstance(c, dict) else (lambda k: getattr(c, k))
    st = get("tokens")
    if st == "reported":
        return "token 輸入 %d／輸出 %d（共 %d）" % (get("prompt_tokens"), get("completion_tokens"), get("total_tokens"))
    if st == "not_counted":
        return "不計 token"
    if st == "demo":
        return "示範不計 token"
    return "token " + UNREPORTED


def model_event_text(c: ModelCall) -> str:
    role = ROLE_LABELS.get(c.role, c.role or "辦公室")
    return "%s %s：%s → %s（%s；%d ms；%s）" % (
        model_tag(c.kind, c.model), role, c.purpose or "模型呼叫",
        c.result if c.ok else "失敗：" + c.result, model_name(c.model, c.effort, c.kind), c.latency_ms, tokens_text(c))


def _num(v: Any) -> str:
    return UNREPORTED if v is None else str(v)


def cost_text(cost: Any, currency: str) -> str:
    if cost is None:
        return ""
    return "費用 %s %s" % (("%.4f" % cost).rstrip("0").rstrip("."), currency or "（幣別未回報）")


def cli_event_text(rec: Dict[str, Any]) -> str:
    from team.fleet import SEAT_LABELS  # 延後匯入：避免循環
    seat = SEAT_LABELS.get(rec.get("seat", ""), rec.get("seat", ""))
    cache = "（含快取（建立＋讀取）%s）" % rec["in_cache"] if rec.get("in_cache") is not None else ""
    parts = ["%s・%s" % (seat, rec.get("model") or CLI_DEFAULT),
             "本回合 token 輸入 %s%s／輸出 %s" % (_num(rec.get("in")), cache, _num(rec.get("out")))]
    if rec.get("ok") is False:
        parts.append("回合沒有好好結束（用量照實計入）")
    if rec.get("total_in") is not None or rec.get("total_out") is not None:
        parts.append("房間累計 輸入 %s／輸出 %s" % (_num(rec.get("total_in")), _num(rec.get("total_out"))))
    c = cost_text(rec.get("cost"), rec.get("currency", ""))
    if c:
        parts.append(c)
    if rec.get("context_pct") is not None:
        parts.append("對話用量 %s%%" % rec["context_pct"])
    return "；".join(parts)


class _Agg:
    def __init__(self, date: str = ""):
        self.date = date
        self.models = {}  # type: Dict[tuple, Dict[str, Any]]
        self.cli = {}  # type: Dict[str, Dict[str, Any]]
        self.rooms = {}  # type: Dict[str, Dict[str, Any]]
        self.roles = {}  # type: Dict[str, Dict[str, Any]]

    def _room(self, rid: str) -> Dict[str, Any]:
        return self.rooms.setdefault(rid, {"room_id": rid, "model_calls": 0, "model_tokens": 0,
                                           "cli_in": None, "cli_out": None, "cli_in_cache": None, "cost": {}})

    def add_model(self, c: Dict[str, Any]) -> None:
        key = (str(c.get("model", "")), str(c.get("effort", "-")))
        m = self.models.setdefault(key, {"model": key[0], "effort": key[1], "kind": c.get("kind", ""),
                                         "calls": 0, "fail": 0, "latency_sum": 0, "prompt_tokens": 0,
                                         "completion_tokens": 0, "total_tokens": 0, "states": set()})
        m["calls"] += 1
        m["fail"] += 0 if c.get("ok") else 1
        m["latency_sum"] += int(c.get("latency_ms") or 0)
        st = c.get("tokens", "unreported")
        m["states"].add(st)
        tok = 0
        if st == "reported":
            m["prompt_tokens"] += int(c.get("prompt_tokens") or 0)
            m["completion_tokens"] += int(c.get("completion_tokens") or 0)
            tok = int(c.get("total_tokens") or 0)
            m["total_tokens"] += tok
        if c.get("room_id"):
            r = self._room(c["room_id"])
            r["model_calls"] += 1
            r["model_tokens"] += tok
        role = c.get("role") or ""
        if role:
            x = self.roles.setdefault(role, {"role": role, "label": ROLE_LABELS.get(role, role), "calls": 0,
                                             "total_tokens": 0})
            x["calls"] += 1
            x["total_tokens"] += tok

    def add_cli(self, rec: Dict[str, Any]) -> None:
        seat = str(rec.get("seat", ""))
        demo = bool(rec.get("demo"))
        s = self.cli.setdefault((seat, demo), {"seat": seat, "demo": demo, "turns": 0, "reported_turns": 0,
                                               "failed_turns": 0, "in": None, "out": None, "in_cache": None,
                                               "cost": {}, "models": [], "by_model": {}})
        s["turns"] += 1
        if rec.get("ok") is False:
            s["failed_turns"] += 1
        if rec.get("in") is not None or rec.get("out") is not None:
            s["reported_turns"] += 1
        model = rec.get("model") or ""
        bm = s["by_model"].setdefault(model, {"model": model, "turns": 0, "in": None, "out": None, "in_cache": None,
                                              "cost": {}})
        bm["turns"] += 1
        for k in ("in", "out", "in_cache"):
            if rec.get(k) is not None:
                s[k] = (s[k] or 0) + int(rec[k])
                bm[k] = (bm[k] or 0) + int(rec[k])
        if model and model not in s["models"]:
            s["models"].append(model)
        cur = rec.get("currency") or ""
        if rec.get("cost") is not None:
            s["cost"][cur] = round(s["cost"].get(cur, 0.0) + float(rec["cost"]), 6)
            bm["cost"][cur] = round(bm["cost"].get(cur, 0.0) + float(rec["cost"]), 6)
        if rec.get("room_id"):
            r = self._room(rec["room_id"])
            if rec.get("in_cache") is not None:
                r["cli_in_cache"] = (r["cli_in_cache"] or 0) + int(rec["in_cache"])
            for k in ("in", "out"):
                if rec.get(k) is not None:
                    r["cli_" + k] = (r["cli_" + k] or 0) + int(rec[k])
            if rec.get("cost") is not None:
                r["cost"][cur] = round(r["cost"].get(cur, 0.0) + float(rec["cost"]), 6)

    def out(self, names: Dict[str, str]) -> Dict[str, Any]:
        models = []
        for m in self.models.values():
            st = m["states"]
            tokens = ("reported" if "reported" in st else "demo" if st == {"demo"}
                      else "not_counted" if st <= {"not_counted", "demo"} else "unreported")
            models.append({"model": m["model"], "effort": m["effort"], "kind": m["kind"],
                           "label": model_name(m["model"], m["effort"], m["kind"]), "calls": m["calls"], "fail": m["fail"],
                           "avg_latency_ms": round(m["latency_sum"] / m["calls"]) if m["calls"] else 0,
                           "prompt_tokens": m["prompt_tokens"], "completion_tokens": m["completion_tokens"],
                           "total_tokens": m["total_tokens"], "tokens": tokens,
                           "partial": "reported" in st and len(st) > 1})
        models.sort(key=lambda x: (x["kind"] != "d1", x["model"], x["effort"]))
        rooms = [dict(r, name=names.get(r["room_id"], r["room_id"]), cost=dict(r["cost"]))
                 for r in self.rooms.values()]
        rooms.sort(key=lambda r: (-(r["model_tokens"] + (r["cli_in"] or 0) + (r["cli_out"] or 0)), -r["model_calls"]))
        cli = [dict(s, cost=dict(s["cost"]), models=list(s["models"]),
                    by_model=[dict(b, cost=dict(b["cost"])) for b in s["by_model"].values()]) for s in self.cli.values()]
        cli.sort(key=lambda x: (x["demo"], x["seat"]))
        return {"date": self.date, "models": models, "cli": cli, "rooms": rooms,
                "roles": sorted((dict(x) for x in self.roles.values()), key=lambda x: -x["calls"])}


class UsageBook:
    """執行緒安全；path 為 None 時只在記憶體（測試、示範）。

    每天一個檔：path 所在資料夾的 usage-YYYYMMDD.jsonl（path 本身只用來決定資料夾）；啟動時只讀今天的檔。
    now：可注入的時鐘（測試跨午夜用）。"""

    def __init__(self, path: Optional[str], now: Any = None):
        self.path = path
        self._now = now or datetime.now
        self.started_at = self._now().isoformat(timespec="seconds")
        self._lock = threading.Lock()
        today = self._now().date().isoformat()
        self._today = _Agg(today)
        self._session = _Agg(today)
        self._load_today()

    def file_for(self, date: str) -> Optional[str]:
        """某一天（YYYY-MM-DD）的用量檔路徑。"""
        if not self.path:
            return None
        return os.path.join(os.path.dirname(os.path.abspath(self.path)), "usage-%s.jsonl" % date.replace("-", ""))

    def _load_today(self) -> None:
        path = self.file_for(self._today.date)
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.startswith('{"'):
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("type") == "model":
                        self._today.add_model(rec)
                    elif rec.get("type") == "cli":
                        self._today.add_cli(rec)
        except OSError as e:
            log.warning("用量帳讀取失敗：%s", type(e).__name__)

    def _append(self, rec: Dict[str, Any]) -> None:
        # 示範房（假模型、假代理）的紀錄只在記憶體，不寫進真正的用量帳
        path = self.file_for(self._today.date)
        if not path or rec.get("demo") or rec.get("model") == DEMO_MODEL:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            log.warning("用量帳寫入失敗（只保留在記憶體）：%s", type(e).__name__)

    def _roll(self) -> None:
        today = self._now().date().isoformat()
        if self._today.date != today:
            self._today = _Agg(today)

    def record_model(self, call: ModelCall) -> None:
        rec = dict(call.to_dict(), type="model")
        rec.pop("detail", None)
        with self._lock:
            self._roll()
            self._today.add_model(rec)
            self._session.add_model(rec)
            self._append(rec)

    def record_cli(self, rec: Dict[str, Any]) -> None:
        rec = dict(rec, type="cli")
        rec.setdefault("ts", self._now().isoformat(timespec="seconds"))
        with self._lock:
            self._roll()
            self._today.add_cli(rec)
            self._session.add_cli(rec)
            self._append(rec)

    def summary(self, names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        names = names or {}
        with self._lock:
            self._roll()
            return {"today": self._today.out(names), "session": self._session.out(names),
                    "started_at": self.started_at}


def merge_tokens(prev: Optional[Dict[str, Any]], rec: Dict[str, Any]) -> Dict[str, Any]:
    """房間的 tokens[seat]：累加有回報的數字；沒回報的維持 None（畫面顯示「—（未回報）」）。"""
    cur = dict(prev or {"in": None, "out": None, "in_cache": None, "cost": None, "currency": "", "turns": 0,
                        "failed": 0})
    cur["turns"] = int(cur.get("turns") or 0) + 1
    if rec.get("ok") is False:
        cur["failed"] = int(cur.get("failed") or 0) + 1  # 沒有好好結束、但有回報用量的回合
    cur.setdefault("in_cache", None)
    for k in ("in", "out", "in_cache"):
        if rec.get(k) is not None:
            cur[k] = int(cur.get(k) or 0) + int(rec[k])
    if rec.get("cost") is not None:
        cur["cost"] = round(float(cur.get("cost") or 0.0) + float(rec["cost"]), 6)
        cur["currency"] = rec.get("currency") or cur.get("currency") or ""
    return cur



def attach(router: Any, store: Any, book: Optional[UsageBook], on_change: Optional[Any] = None) -> None:
    """把路由器的每筆 ModelCall 寫進該房的事件（kind "model"）與用量帳；進行中的呼叫變動時呼叫 on_change。

    同一個路由器可以服務很多房間（以 ModelCall.room_id 分房）；沒有房間或房間不存在 → 只記用量帳。"""
    def on_call(call: ModelCall) -> None:
        if book is not None:
            book.record_model(call)
        if call.room_id:
            try:
                data = {k: v for k, v in call.to_dict().items() if k not in ("ts", "kind", "room_id")}
                store.event(call.room_id, "model", model_event_text(call), model_kind=call.kind, **data)
            except (KeyError, ValueError):
                pass  # 房間已不存在
            except Exception:
                log.debug("寫入模型事件失敗", exc_info=True)
        if on_change is not None:
            on_change()
    router.listeners.append(on_call)
    if on_change is not None:
        router.on_inflight = on_change
