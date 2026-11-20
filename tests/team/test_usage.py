"""Task 17：模型呼叫紀錄（ModelCall）、進行中呼叫、用量帳（UsageBook）。不連網。"""
import json
import threading
from datetime import datetime, timedelta

import pytest

from team.fakes.fake_models import ScriptedRouter
from team.models import ModelConfig, ModelReply, ModelRouter, call_ctx
from team.usage import ModelCall, UsageBook, cli_event_text, model_event_text

KEY = "sk-secret-123"


def _cfg():
    return ModelConfig(base_url="http://litellm.local:4000", glm_key=KEY, decision1_key=KEY + "d1")


def _glm_usage(allow=0.95, risk=0.05, model="glm-5.3-20261101"):
    def glm(prompt, effort, timeout):
        text = json.dumps({"allow": {"noul": allow}, "risk": {"noul": risk}, "q": {"noul": 0.7}})
        return ModelReply(text, {"prompt_tokens": 120, "completion_tokens": 48, "total_tokens": 168}, model)
    return glm


def _d1(allow, risk):
    def d1(state, questions):
        return {"allow": {"noul": allow}, "risk": {"noul": risk}, "q": {"noul": 0.82}}
    return d1


def _down(*a, **k):
    raise RuntimeError("down")


def _router(**kw):
    r = ModelRouter(_cfg(), **kw)
    calls = []
    r.listeners.append(calls.append)
    return r, calls


def test_d1_call_recorded_without_tokens():
    r, calls = _router(d1=_d1(0.93, 0.04), glm=_down)
    v = r.decide_allow("pytest -q", ctx={"room_id": "r12345678", "role": "policy"})
    assert v.auto is True
    assert len(calls) == 1
    c = calls[0]
    assert isinstance(c, ModelCall)
    assert (c.room_id, c.role, c.model, c.effort, c.kind) == ("r12345678", "policy", "Decision-1", "-", "d1")
    assert c.purpose == "判斷指令是否安全" and c.ok is True
    assert c.result == "allow 0.93 risk 0.04"
    assert c.tokens == "not_counted" and c.total_tokens == 0
    assert c.latency_ms >= 0
    assert "不計 token" in model_event_text(c)
    assert KEY not in json.dumps(c.to_dict())


def test_glm_call_records_usage_and_server_model():
    r, calls = _router(d1=_d1(0.9, 0.5), glm=_glm_usage())
    v = r.decide_allow("git push", ctx={"room_id": "r12345678", "role": "policy"})
    assert v.auto is True
    assert [c.kind for c in calls] == ["d1", "glm"]
    g = calls[1]
    assert g.model == "glm-5.3-20261101" and g.effort == "low"
    assert (g.prompt_tokens, g.completion_tokens, g.total_tokens, g.tokens) == (120, 48, 168, "reported")
    assert g.purpose == "灰色地帶複核指令是否安全"
    # 判定理由附上兩個模型各自的數字與版本
    assert "Decision-1 allow 0.90 risk 0.50" in v.detail and "glm-5.3-20261101 allow 0.95 risk 0.05" in v.detail
    assert "glm-5.3-20261101" in v.by
    text = model_event_text(g)
    assert "GLM" in text and "168" in text and "權限把關" in text


def test_glm_without_usage_is_unreported_not_zero():
    r, calls = _router(glm=lambda p, e, t: "寫好的文字")
    assert r.write("x", effort="low", ctx={"room_id": "", "role": "scrum", "purpose": "寫督促訊息"}) == "寫好的文字"
    c = calls[0]
    assert c.tokens == "unreported" and c.model == "glm-5.3" and c.result == "產生 5 字"
    assert "未回報" in model_event_text(c)


def test_failed_call_recorded():
    r, calls = _router(d1=_down, glm=_down)
    assert r.probability("s", "q?", ctx={"room_id": "r1", "role": "qa", "purpose": "檢查驗收條件品質"}) is None
    assert [(c.kind, c.ok) for c in calls] == [("d1", False), ("glm", False)]
    assert "RuntimeError" in calls[0].result


def test_probability_pass_min_phrasing():
    r, calls = _router(d1=_d1(0.9, 0.1), glm=_down)
    r.probability("s", "q?", ctx={"role": "qa", "purpose": "檢查驗收條件品質", "pass_min": 0.6})
    assert calls[0].result == "0.82 ≥ 0.60 通過"


def test_inflight_visible_during_call_and_gone_after():
    gate = threading.Event()
    started = threading.Event()

    def slow(prompt, effort, timeout):
        started.set()
        gate.wait(5)
        return "ok"
    r, calls = _router(glm=slow)
    seen = []
    r.on_inflight = lambda: seen.append(len(r.inflight()))
    t = threading.Thread(target=r.write, args=("x",), kwargs={"ctx": {"room_id": "r1", "role": "reviewer",
                                                                       "purpose": "Reviewer 審查重點"}})
    t.start()
    assert started.wait(5)
    fl = r.inflight()
    assert len(fl) == 1
    assert fl[0]["room_id"] == "r1" and fl[0]["role"] == "reviewer" and fl[0]["model"] == "glm-5.3"
    assert fl[0]["purpose"] == "Reviewer 審查重點" and fl[0]["since"]
    gate.set()
    t.join(5)
    assert r.inflight() == []
    assert seen[0] == 1 and seen[-1] == 0


def test_scripted_router_records_demo_calls():
    r = ScriptedRouter()
    calls = []
    r.listeners.append(calls.append)
    r.decide_allow("pytest -q", ctx={"room_id": "r1", "role": "policy"})
    r.probability("s", "q", ctx={"room_id": "r1", "role": "dispatcher", "purpose": "派工：評估任務難度"})
    r.write("目標：x", ctx={"room_id": "r1", "role": "po", "purpose": "PO 補充說明"})
    assert [c.model for c in calls] == ["scripted"] * 3
    assert all(c.tokens == "demo" and c.total_tokens == 0 for c in calls)
    assert "示範（假模型，代 Decision-1）" in model_event_text(calls[0])
    assert "示範（假模型，代 glm 低思考量）" in model_event_text(calls[2])


def test_call_ctx_supports_old_signatures():
    class Old:
        def probability(self, state, question):
            return 0.5

    class New:
        def probability(self, state, question, ctx=None):
            return ctx
    assert call_ctx(Old().probability, "s", "q", ctx={"a": 1}) == 0.5
    assert call_ctx(New().probability, "s", "q", ctx={"a": 1}) == {"a": 1}


def _call(**kw):
    base = dict(ts=datetime.now().isoformat(timespec="seconds"), room_id="r1", role="policy", model="Decision-1",
                effort="-", purpose="判斷指令是否安全", latency_ms=100, ok=True, result="allow 0.9 risk 0.1",
                kind="d1", tokens="not_counted")
    base.update(kw)
    return ModelCall(**base)


def test_usage_book_aggregates_and_persists(tmp_path):
    path = str(tmp_path / "usage.jsonl")
    book = UsageBook(path)
    book.record_model(_call(latency_ms=100))
    book.record_model(_call(latency_ms=300, ok=False))
    book.record_model(_call(kind="glm", model="glm-5.3", effort="low", role="reviewer", latency_ms=1000,
                            prompt_tokens=100, completion_tokens=20, total_tokens=120, tokens="reported"))
    book.record_model(_call(kind="glm", model="glm-5.3", effort="normal", room_id="r2", role="reviewer",
                            tokens="unreported"))
    book.record_cli({"room_id": "r1", "seat": "claude", "model": "claude-sonnet-4-5", "in": 1000, "out": 200,
                     "cost": 0.012, "currency": "USD"})
    book.record_cli({"room_id": "r1", "seat": "kiro", "model": "", "in": None, "out": None,
                     "cost": None, "currency": ""})
    s = book.summary({"r1": "房間一", "r2": "房間二"})
    for scope in ("today", "session"):
        m = {(x["model"], x["effort"]): x for x in s[scope]["models"]}
        d1 = m[("Decision-1", "-")]
        assert (d1["calls"], d1["fail"], d1["avg_latency_ms"], d1["tokens"]) == (2, 1, 200, "not_counted")
        low = m[("glm-5.3", "low")]
        assert (low["calls"], low["total_tokens"], low["prompt_tokens"], low["tokens"]) == (1, 120, 100, "reported")
        assert m[("glm-5.3", "normal")]["tokens"] == "unreported"
        cli = {x["seat"]: x for x in s[scope]["cli"]}
        assert cli["claude"]["in"] == 1000 and cli["claude"]["out"] == 200 and cli["claude"]["cost"] == {"USD": 0.012}
        assert cli["claude"]["models"] == ["claude-sonnet-4-5"]
        assert cli["kiro"]["in"] is None and cli["kiro"]["turns"] == 1 and cli["kiro"]["cost"] == {}
        rooms = s[scope]["rooms"]
        assert rooms[0]["room_id"] == "r1" and rooms[0]["name"] == "房間一"
        assert rooms[0]["model_calls"] == 3 and rooms[0]["model_tokens"] == 120 and rooms[0]["cli_in"] == 1000
        roles = {x["role"]: x for x in s[scope]["roles"]}
        assert roles["reviewer"]["calls"] == 2 and roles["policy"]["calls"] == 2
    lines = open(book.file_for(datetime.now().date().isoformat()), encoding="utf-8").read().splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["type"] == "model" and json.loads(lines[-1])["type"] == "cli"
    # 重新啟動：今日從檔案讀回，本次啟動歸零
    again = UsageBook(path).summary({})
    assert sum(x["calls"] for x in again["today"]["models"]) == 4
    assert again["session"]["models"] == [] and again["session"]["cli"] == []


def test_usage_book_today_ignores_old_lines(tmp_path):
    path = tmp_path / "usage.jsonl"
    yday = datetime.now() - timedelta(days=1)
    old = yday.isoformat(timespec="seconds")
    line = json.dumps(dict(_call(ts=old).to_dict(), type="model")) + "\n{broken\n"
    (tmp_path / ("usage-%s.jsonl" % yday.strftime("%Y%m%d"))).write_text(line, encoding="utf-8")
    path.write_text(line, encoding="utf-8")  # 舊的單一檔不再讀
    s = UsageBook(str(path)).summary({})
    assert s["today"]["models"] == []


def test_cli_event_text():
    t = cli_event_text({"seat": "claude", "model": "claude-sonnet-4-5", "in": 1200, "out": 300,
                        "cost": 0.02, "currency": "USD", "total_in": 5000, "total_out": 900})
    assert "claude-sonnet-4-5" in t and "1200" in t and "300" in t and "USD" in t
    t2 = cli_event_text({"seat": "kiro", "model": "", "in": None, "out": None, "cost": None, "currency": ""})
    assert "CLI 預設（未回報）" in t2 and "—（未回報）" in t2


def test_demo_records_stay_in_memory(tmp_path):
    path = tmp_path / "usage.jsonl"
    book = UsageBook(str(path))
    book.record_model(_call(model="scripted", tokens="demo"))
    book.record_cli({"room_id": "r1", "seat": "claude", "model": "claude-default", "in": 10, "out": 1,
                     "cost": None, "currency": "", "demo": True})
    book.record_cli({"room_id": "r2", "seat": "claude", "model": "claude-sonnet-4-5", "in": 5, "out": 2,
                     "cost": None, "currency": ""})
    assert len(open(book.file_for(datetime.now().date().isoformat()), encoding="utf-8").read().splitlines()) == 1
    cli = book.summary({})["session"]["cli"]
    assert [(c["seat"], c["demo"], c["in"]) for c in cli] == [("claude", False, 5), ("claude", True, 10)]
    m = book.summary({})["session"]["models"][0]
    assert m["label"] == "示範（假模型，代 Decision-1）" and m["tokens"] == "demo"
