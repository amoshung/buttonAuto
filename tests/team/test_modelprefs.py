"""Task 18：使用者選模型（偏好讀寫與驗證、派工尊重偏好、判斷用途、ACP 偵測、房間中途改模型）。

不連網、不跑真 CLI：ACP 用 team/fakes/fake_acp_agent.py（FAKE_ACP_MODELS 模擬 kiro 的 availableModels）。
"""
import json
import os
import sys
import time

import pytest

from team.acp import AcpClient
from team.fleet import Dispatcher, QuotaBook
from team.modelprefs import DEFAULT_CATALOG, PURPOSE_DEFAULTS, ModelPrefs, PrefsError, load_catalog
from team.models import ModelConfig, ModelRouter
from team.usage import UsageBook

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")
PY = sys.executable

# 使用者機器上 kiro 3.3 回報的真實模型 id（只當測試資料；程式不寫死）
KIRO_IDS = ["auto", "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "gpt-5.6-sol", "gpt-5.6-terra",
            "gpt-5.6-luna", "grok-4.7", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4.5"]
KIRO_MODELS = [{"modelId": m, "name": m, "description": "kiro %s" % m} for m in KIRO_IDS]


def _prefs(tmp_path, detected=True):
    p = ModelPrefs(str(tmp_path / "team_models.json"), str(tmp_path / "team" / "models_cache.json"))
    if detected:
        p.note_detected("kiro", KIRO_MODELS, "auto")
    return p


# ---------------------------------------------------------------- 偏好讀寫與驗證
def test_defaults_and_unknown_acp_list(tmp_path):
    p = _prefs(tmp_path, detected=False)
    assert p.catalog == DEFAULT_CATALOG
    assert p.purposes() == PURPOSE_DEFAULTS
    assert p.seat_models() == {}
    assert p.available("kiro") is None  # 還沒偵測
    assert "重新偵測" in p.problem("kiro", "claude-sonnet-5.5")
    assert [m["id"] for m in p.available("claude")] == ["haiku", "sonnet", "opus"]
    assert [m["id"] for m in p.available("glm_high")] == ["glm-5.3"]
    assert p.problem("glm", "glm-5.3") == "" and "不在" in p.problem("glm", "gpt-4")


def test_detected_list_cached_and_validates(tmp_path):
    p = _prefs(tmp_path)
    assert [m["id"] for m in p.available("kiro")] == KIRO_IDS
    assert p.problem("kiro", "claude-sonnet-5.5") == ""
    assert "不在 kiro 的可用模型清單" in p.problem("kiro", "gpt-9")
    # 快取在磁碟：新物件讀得到
    p2 = _prefs(tmp_path, detected=False)
    assert p2.detected("kiro")["current"] == "auto" and len(p2.available("kiro")) == len(KIRO_IDS)
    # 偵測失敗：保留上次清單、記錯誤
    p2.note_detected("kiro", [], error="偵測失敗：boom")
    assert len(p2.available("kiro")) == len(KIRO_IDS) and p2.detected("kiro")["error"] == "偵測失敗：boom"


def test_update_persists_and_rejects_bad_values(tmp_path):
    p = _prefs(tmp_path)
    p.update({"seats": {"kiro": "claude-sonnet-5.5"}, "purposes": {"judge": "glm", "notes": "off"},
              "models": {"kiro": [{"id": "claude-haiku-5.5", "tier": "economy", "cost": 1},
                                  {"id": "claude-opus-5.5", "tier": "premium", "cost": 12},
                                  {"id": "grok-4.7", "tier": "", "cost": 0}],
                         "claude": [{"id": "haiku", "tier": "economy", "cost": 1},
                                    {"id": "claude-opus-4-1-20250805", "tier": "premium", "cost": 15}]}})
    raw = json.loads((tmp_path / "team_models.json").read_text(encoding="utf-8"))
    assert raw["seats"] == {"kiro": "claude-sonnet-5.5"} and raw["purposes"] == {"judge": "glm", "notes": "off"}
    assert "key" not in json.dumps(raw).lower()
    # 目錄：標了等級（含推定）的參與自動派工；明確選「不參與」的 grok 與 auto 不在；就地更新（Dispatcher 拿同一個 dict）
    cat = p.catalog
    ids = [e["id"] for e in cat["kiro"]]
    assert "grok-4.7" not in ids and "auto" not in ids and "claude-sonnet-5.5" in ids
    assert {e["id"]: e for e in cat["kiro"]}["claude-opus-5.5"] == {"id": "claude-opus-5.5", "tier": "premium", "cost": 12}
    assert "claude-opus-4-1-20250805" in [m["id"] for m in p.available("claude")]
    p2 = _prefs(tmp_path, detected=False)
    assert p2.seat_model("kiro") == "claude-sonnet-5.5" and p2.purpose("judge") == "glm"
    # 沒有偵測快取時（只讀目錄）：只有你存過、標了等級的
    assert [e["id"] for e in load_catalog(str(tmp_path / "team_models.json"))["kiro"]] == ["claude-haiku-5.5",
                                                                                         "claude-opus-5.5"]
    # 驗證：不在清單、未知座位、未知用途值 → 全部不套用
    for bad in ({"seats": {"kiro": "gpt-9"}}, {"seats": {"nope": "x"}}, {"purposes": {"judge": "gpt"}},
                {"purposes": {"x": "d1"}}, {"models": {"kiro": [{"id": "a", "tier": "huge"}]}},
                {"seats": {"claude": "claude-unknown"}}, {"seats": {"glm": "gpt-4"}}):
        with pytest.raises(PrefsError):
            p.update(bad)
    assert p.seat_model("kiro") == "claude-sonnet-5.5" and p.purpose("judge") == "glm"
    p.update({"seats": {"kiro": ""}})  # 改回自動
    assert p.seat_models() == {}


def test_old_catalog_format_still_loads(tmp_path):
    (tmp_path / "team_models.json").write_text(json.dumps(
        {"kiro": [{"id": "claude-sonnet-4.5", "tier": "premium", "cost": 4}]}), encoding="utf-8")
    p = _prefs(tmp_path, detected=False)
    assert p.catalog["kiro"] == [{"id": "claude-sonnet-4.5", "tier": "premium", "cost": 4}]
    assert p.purposes() == PURPOSE_DEFAULTS


# ---------------------------------------------------------------- 派工尊重偏好
class Prob:
    def __init__(self):
        self.calls = 0

    def probability(self, state, question):
        self.calls += 1
        return 0.5


def test_dispatch_uses_global_and_room_models(tmp_path):
    p = _prefs(tmp_path)
    router = Prob()
    d = Dispatcher(router, p.catalog, QuotaBook(str(tmp_path)), lambda: {"kiro", "agy", "claude", "glm", "glm_high"},
                   prefs=p)
    a = d.pick("build", "task", user_seat="kiro")
    assert a.by == "user" and a.model == "claude-sonnet-5.5" and a.model_by == ""  # 自動：依推定等級挑
    p.update({"seats": {"kiro": "claude-sonnet-5.5"}})
    a = d.pick("build", "task", user_seat="kiro")
    assert a.model == "claude-sonnet-5.5" and a.model_by == "global" and "你指定" in a.reason
    a = d.pick("build", "task", user_seat="kiro", user_model="claude-haiku-5.5")
    assert a.model == "claude-haiku-5.5" and a.model_by == "room"
    # 代班不套用你指定的模型
    f = d.fallback("kiro")
    assert f.seat == "glm" and f.by == "fallback" and f.model_by == ""
    # 合併前最後審查：依用途設定
    assert d.pick("final_review", "t").seat == "glm_high"
    p.update({"purposes": {"final_review": "glm_normal"}})
    assert d.pick("final_review", "t").seat == "glm"
    p.update({"purposes": {"final_review": "off"}})
    assert d.final_review_seat() == ""


# ---------------------------------------------------------------- 判斷用途切換 → router 用對的模型
def _cfg():
    return ModelConfig("http://litellm.invalid", "sk-glm-1", "sk-d1-2")


def _router(d1_answer=(0.9, 0.5), glm_answer=(0.95, 0.05)):
    calls = {"d1": 0, "glm": []}

    def d1(state, questions):
        calls["d1"] += 1
        out = {"allow": {"noul": d1_answer[0]}, "risk": {"noul": d1_answer[1]}}
        if "q" in questions:
            out["q"] = {"noul": 0.8}
        return out

    def glm(prompt, effort, timeout):
        calls["glm"].append(effort)
        return json.dumps({"allow": {"noul": glm_answer[0]}, "risk": {"noul": glm_answer[1]}, "q": {"noul": 0.7}})
    r = ModelRouter(_cfg(), d1=d1, glm=glm)
    book = UsageBook(None)
    r.listeners.append(book.record_model)
    return r, calls, book


def _models_used(book):
    return sorted({m["model"] for m in book.summary({})["session"]["models"]})


def test_judge_purpose_switches_router(tmp_path):
    p = _prefs(tmp_path)
    r, calls, book = _router()
    r.purposes = p
    assert r.probability("s", "q?") == pytest.approx(0.8) and calls["d1"] == 1 and calls["glm"] == []
    p.update({"purposes": {"judge": "glm"}})
    assert r.probability("s", "q?") == pytest.approx(0.7)
    assert calls["d1"] == 1 and calls["glm"] == ["low"]
    assert _models_used(book) == ["Decision-1", "glm-5.3"]


def test_permission_purposes(tmp_path):
    p = _prefs(tmp_path)
    # 只用 glm：不問 Decision-1，嚴格門檻
    r, calls, book = _router()
    r.purposes = p
    p.update({"purposes": {"permission": "glm"}})
    v = r.decide_allow("pytest -q")
    assert v.auto is True and calls["d1"] == 0 and calls["glm"] == ["low"] and "只用" in v.reason
    assert _models_used(book) == ["glm-5.3"]
    r2, calls2, _ = _router(glm_answer=(0.85, 0.2))  # 一般門檻會過、嚴格門檻不過
    r2.purposes = p
    assert r2.decide_allow("x").auto is False
    # 只用 Decision-1：灰色地帶交給你，不問 glm
    p.update({"purposes": {"permission": "d1"}})
    r3, calls3, _ = _router(d1_answer=(0.9, 0.5))
    r3.purposes = p
    v = r3.decide_allow("git push")
    assert v.auto is False and calls3["glm"] == [] and "只用 Decision-1" in v.reason
    r4, calls4, _ = _router(d1_answer=(0.95, 0.05))
    r4.purposes = p
    assert r4.decide_allow("ls").auto is True and calls4["glm"] == []


# ---------------------------------------------------------------- ACP：可用模型與 set_model
def _acp(tmp_path, monkeypatch, models=KIRO_IDS):
    script = tmp_path / "s.json"
    script.write_text(json.dumps([[{"say": "hi"}]]), encoding="utf-8")
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(models))
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.log"))
    return AcpClient([PY, AGENT, str(script)], str(tmp_path), lambda ev: None, lambda req: "")


def test_acp_session_new_reports_available_models(tmp_path, monkeypatch):
    c = _acp(tmp_path, monkeypatch)
    try:
        c.start()
        c.new_session()
        assert [m["id"] for m in c.available_models] == KIRO_IDS and c.current_model == "auto"
        assert c.set_model("claude-sonnet-5.5") and c.current_model == "claude-sonnet-5.5"
        assert not c.set_model("gpt-9")
    finally:
        c.close()


def test_acp_driver_model_failed(tmp_path, monkeypatch):
    from team.clis import CliInfo
    from team.drivers import make_driver
    _acp(tmp_path, monkeypatch)
    script = tmp_path / "s.json"
    cli = CliInfo("kiro", "Kiro", "/nonexistent/kiro-cli", "acp")
    drv = make_driver(cli, str(tmp_path), lambda ev: None, lambda req: "", False,
                      cmd_override=[PY, AGENT, str(script)], model="gpt-9")
    try:
        drv.start()
        assert drv.model_failed is True
        assert drv.set_model("claude-haiku-5.5") and drv.model_failed is False
    finally:
        drv.close()


# ---------------------------------------------------------------- 執行器：房間偏好、驗證、中途改模型
from test_runner import (BUILD_TURN, PLAN_TURN, QUOTA_ERR, SPEC_TURN, CALC_MUL, TEST_MUL, Env,  # noqa: E402
                         repo)  # noqa: F401,E402  （repo 是 fixture）


def _pin_room(env, model):
    env.store.update(env.room.id, lambda r: (setattr(r, "impl_pref", {"seat": "kiro", "model": model}),
                                            setattr(r, "user_pinned", True)))
    env.runner.room = env.store.get(env.room.id)


def test_room_model_change_applies_next_turn(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
    log_path = tmp_path / "acp.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log_path))
    prefs = _prefs(tmp_path)
    changed = []

    def hook(env):
        r = env.store.get(env.room.id)
        if r is not None and r.phase == "build" and not changed:
            changed.append(1)
            env.store.update(r.id, lambda x: setattr(x, "impl_pref", {"seat": "kiro", "model": "claude-sonnet-5.5"}))

    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], pinned=True, prefs=prefs, on_change=hook)
    _pin_room(env, "claude-haiku-5.5")
    room = env.run()
    assert room.status == "done", room.error
    log = log_path.read_text(encoding="utf-8").splitlines()
    impl_log = [x for x in log]
    assert log.count("session/new") == 2  # 實作一段對話（中途換模型沒有開新對話）＋審查一段
    # 同一段對話：先切到 haiku，build 回合前改成 sonnet（session/set_model，不開新對話）
    assert "set_model claude-haiku-5.5" in impl_log and "set_model claude-sonnet-5.5" in impl_log
    assert impl_log.index("set_model claude-haiku-5.5") < impl_log.index("set_model claude-sonnet-5.5")
    assigns = [e for e in env.events() if e["kind"] == "assign" and e["role"] == "impl"]
    assert assigns[0]["model"] == "claude-haiku-5.5" and assigns[0]["model_by"] == "room"
    assert assigns[-1]["model"] == "claude-sonnet-5.5" and "你指定" in assigns[-1]["text"]
    cli = [e for e in env.events() if e["kind"] == "cli_usage" and e["role"] == "impl"]
    assert cli[0]["model"] == "claude-haiku-5.5" and cli[-1]["model"] == "claude-sonnet-5.5"
    assert room.impl_model == "claude-sonnet-5.5"


def test_room_model_not_in_list_asks_and_sends_nothing(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.log"))
    prefs = _prefs(tmp_path)
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], pinned=True, prefs=prefs)
    _pin_room(env, "gpt-9")
    env.runner.start()
    assert env.wait(lambda: env.items("question"))
    q = env.items("question")[0]
    assert q.title == "指定的實作模型不能用" and "gpt-9" in q.detail and "不會自動改用別的模型" in q.detail
    assert {o["id"] for o in q.options} == {"retry", "auto", "pause"}
    assert env.get().status == "waiting_user"
    assert not (tmp_path / "acp.log").exists()  # 沒有開對話、沒有送出
    assert env.runner.answer(q.id, "auto")
    env.runner.join(60)
    room = env.get()
    assert room.status == "done", room.error
    assert room.impl_pref == {"seat": "kiro", "model": "", "no_global": True} and room.impl_seat == "kiro"
    assert any(e["kind"] == "prefs" for e in env.events())


def test_agent_rejecting_model_asks(tmp_path, repo, monkeypatch):  # noqa: F811
    # 清單未知（從沒偵測過）：先開對話，session/new 的清單裡沒有 → 不送 prompt、問你
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
    prefs = _prefs(tmp_path, detected=False)
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], pinned=True, prefs=prefs)
    _pin_room(env, "gpt-9")
    env.runner.start()
    assert env.wait(lambda: env.items("question"))
    assert "session/set_model" in env.items("question")[0].detail
    assert not [e for e in env.events() if e["kind"] == "prompt"]
    # 順手記下了 session/new 回報的清單
    assert [m["id"] for m in prefs.available("kiro")] == KIRO_IDS
    env.runner.pause()
    env.runner.join(20)


def test_quota_fallback_still_happens_and_returns_to_your_model(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
    prefs = _prefs(tmp_path)
    glm = [[{"write": {"path": "docs/ai-team/add-mul/plan.md", "content": "# 計畫\n1. mul（對應 C1）\n"}},
            {"commit": "plan (glm)"}, {"say": "DONE"}]]

    def recover(env):
        if env.quota.is_exhausted("kiro", None):
            env.quota.mark_ok("kiro")

    env = Env(tmp_path, repo, [SPEC_TURN, QUOTA_ERR, BUILD_TURN], glm=glm, pinned=True, prefs=prefs,
              available=("kiro", "agy", "claude", "glm"), on_change=recover)
    _pin_room(env, "claude-haiku-5.5")
    room = env.run()
    assert room.status == "done", room.error
    assigns = [e for e in env.events() if e["kind"] == "assign" and e["role"] == "impl"]
    assert [a["by"] for a in assigns].count("fallback") >= 1
    fb = [a for a in assigns if a["by"] == "fallback"][0]
    assert fb["seat"] == "glm" and fb["model_by"] == ""
    assert assigns[-1]["seat"] == "kiro" and assigns[-1]["model"] == "claude-haiku-5.5"
    assert not env.items("question", resolved=True)


def test_notes_off_uses_templates_without_model_calls(tmp_path, repo):  # noqa: F811
    from test_runner import _attach
    prefs = _prefs(tmp_path)
    prefs.update({"purposes": {"notes": "off"}})
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], prefs=prefs)
    _attach(env)
    room = env.run()
    assert room.status == "done", room.error
    says = [e["text"] for e in env.events() if e["kind"] == "say"]
    assert any("PO 補充說明（設定為只用範本）" in t for t in says)
    assert not [e for e in env.events() if e["kind"] == "model" and e.get("purpose") == "PO 補充說明"]


# ================================================================ Task 18A 審查修正
from team.modelprefs import infer_tier  # noqa: E402


def test_infer_tier_defaults_and_explicit_choice_wins(tmp_path):
    assert [infer_tier(m) for m in ("auto", "claude-opus-5.5", "claude-sonnet-5", "gpt-5.6-sol", "gpt-5.6-terra",
                                    "grok-4.7", "claude-haiku-4.5", "gpt-5.6-luna", "gemini-flash", "o4-mini",
                                    "mystery-1", "gemini-3-pro", "gemini-2.5-flash-lite", "gemini-3-ultra",
                                    "gemini-pro-max", "minimax-m2", "solar-pro", "prometheus")] == [
        "", "premium", "standard", "standard", "standard", "standard", "economy", "economy", "economy", "economy",
        "standard", "standard", "economy", "premium", "standard", "standard", "standard", "standard"]
    p = _prefs(tmp_path)
    tiers = {e["tier"] for e in p.catalog["kiro"]}
    assert tiers == {"economy", "standard", "premium"}
    assert "auto" not in [e["id"] for e in p.catalog["kiro"]]
    opus = {e["id"]: e for e in p.catalog["kiro"]}["claude-opus-5.5"]
    assert opus["tier"] == "premium" and opus["cost"] == 15
    row = [r for r in p.view()["seats"]["kiro"]["models"] if r["id"] == "claude-opus-5.5"][0]
    assert row["inferred"] is True and row["tier"] == "premium"
    # 你明確選「不參與自動」→ 存下來、重新讀取也還在，優先於推定
    p.update({"models": {"kiro": [{"id": "claude-opus-5.5", "tier": "", "cost": 0}]}})
    p2 = _prefs(tmp_path, detected=False)
    assert "claude-opus-5.5" not in [e["id"] for e in p2.catalog["kiro"]]
    row = [r for r in p2.view()["seats"]["kiro"]["models"] if r["id"] == "claude-opus-5.5"][0]
    assert row["tier"] == "" and row["inferred"] is False


def test_unchanged_stale_pref_warns_and_glm_seat_rejected(tmp_path):
    (tmp_path / "team_models.json").write_text(json.dumps({"seats": {"kiro": "gpt-9"}}), encoding="utf-8")
    p = _prefs(tmp_path)
    warnings = p.update({"purposes": {"notes": "off"}, "seats": {"kiro": "gpt-9"}})
    assert warnings and "gpt-9" in warnings[0] and p.purpose("notes") == "off"
    with pytest.raises(PrefsError):
        p.update({"seats": {"glm": "glm-5.3"}})
    with pytest.raises(PrefsError):
        p.update({"seats": {"kiro": "gpt-10"}})  # 新選的仍要驗證


def test_bad_global_model_room_override_auto(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
    (tmp_path / "team_models.json").write_text(json.dumps({"seats": {"kiro": "gpt-9"}}), encoding="utf-8")
    prefs = _prefs(tmp_path)
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], pinned=True, prefs=prefs)
    env.dispatcher.prefs, env.dispatcher.catalog = prefs, prefs.catalog
    env.runner.start()
    assert env.wait(lambda: env.items("question"))
    q = env.items("question")[0]
    assert q.title == "指定的實作模型不能用" and "設定頁「模型」的座位設定" in q.detail
    assert "這間房改用自動選擇" in [o["label"] for o in q.options]
    assert "這間房改用自動選擇（不套用設定頁的指定，也清掉這間房的指定）" in q.detail
    assert env.runner.answer(q.id, "auto")
    env.runner.join(60)
    room = env.get()
    assert room.status == "done", room.error
    assert room.impl_pref.get("no_global") is True and room.impl_model != "gpt-9"
    assert prefs.seat_model("kiro") == "gpt-9"  # 全域設定沒被動到
    assert len(env.items("question", resolved=True)) == 1


def test_global_pref_change_applies_mid_room(tmp_path, repo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
    log_path = tmp_path / "acp.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log_path))
    prefs = _prefs(tmp_path)
    done = []

    def hook(env):
        r = env.store.get(env.room.id)
        if r is not None and r.phase == "build" and not done:
            done.append(1)
            prefs.update({"seats": {"kiro": "claude-opus-5.5"}})

    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], pinned=True, prefs=prefs, on_change=hook)
    env.dispatcher.prefs, env.dispatcher.catalog = prefs, prefs.catalog
    room = env.run()
    assert room.status == "done", room.error
    log = log_path.read_text(encoding="utf-8").splitlines()
    assert "set_model claude-opus-5.5" in log and log.count("session/new") == 2
    a = [e for e in env.events() if e["kind"] == "assign" and e["role"] == "impl"][-1]
    assert a["model"] == "claude-opus-5.5" and a["model_by"] == "global"


def test_runner_final_review_off_and_notes_normal(tmp_path, repo):  # noqa: F811
    from test_runner import _attach
    prefs = _prefs(tmp_path)
    prefs.update({"purposes": {"final_review": "off", "notes": "glm_normal"}})
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], prefs=prefs)
    _attach(env)
    phases = []
    real = env.dispatcher.pick
    env.dispatcher.pick = lambda phase, *a, **k: (phases.append(phase), real(phase, *a, **k))[1]
    room = env.run()
    assert room.status == "done", room.error
    assert "review" in phases and "final_review" not in phases  # 最後審查關閉：不派座位
    notes = [e for e in env.events() if e["kind"] == "model" and e.get("purpose") == "PO 補充說明"]
    assert notes and all(e["effort"] == "normal" for e in notes)


def test_headless_set_model_next_turn():
    from team.drivers import HeadlessDriver
    from team.headless import HeadlessClaude
    c = HeadlessClaude(["claude"], ".", lambda ev: None, False, extra_args=["--model", "haiku", "--x"])
    d = HeadlessDriver(c, "Claude")
    assert d.set_model("claude-sonnet-5.5")
    cmd = c.build_cmd()
    assert cmd.count("--model") == 1 and cmd[cmd.index("--model") + 1] == "claude-sonnet-5.5" and "--x" in cmd


# ---------------------------------------------------------------- Office：真的偵測（假 ACP 代理）
def _office(tmp_path, clis, **kw):
    from team.fakes.fake_models import ScriptedRouter
    from team.office import Office
    from team.rooms import RoomStore
    return Office(RoomStore(str(tmp_path / "team")), lambda demo: ScriptedRouter(), lambda: [], lambda: 1,
                  lambda: None, clis_fn=lambda: dict(clis), base_dir=str(tmp_path), model_cfg=None,
                  detect_on_start=False, **kw)


def test_office_detect_seat_no_prompt_closed_cert_env(tmp_path, monkeypatch):
    from team import drivers, office as office_mod
    from team.clis import CliInfo
    script = tmp_path / "s.json"
    script.write_text(json.dumps([[{"say": "hi"}]]), encoding="utf-8")
    monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.log"))
    monkeypatch.setattr(drivers.netcerts, "cert_env", lambda: {"SSL_CERT_FILE": "/corp/root.pem"})
    made = []
    real = office_mod.make_driver

    def fake_make(cli, cwd, *a, **k):
        k["cmd_override"] = [PY, AGENT, str(script)]
        drv = real(cli, cwd, *a, **k)
        made.append(drv)
        return drv
    monkeypatch.setattr(office_mod, "make_driver", fake_make)
    o = _office(tmp_path, {"kiro": CliInfo("kiro", "Kiro", "/nonexistent/kiro-cli", "acp")})
    res = o._detect_seat("kiro")
    assert [m["id"] for m in res["models"]] == KIRO_IDS and res["current"] == "auto"
    log = (tmp_path / "acp.log").read_text(encoding="utf-8").splitlines()
    assert log == ["session/new"]  # 沒有 session/prompt、沒有 set_model
    drv = made[0]
    assert drv.client._closed and drv.client._proc.poll() is not None
    assert drv.client._env["SSL_CERT_FILE"] == "/corp/root.pem"
    # 走 detect_models：結果進快取與 view
    assert o.detect_models() == ["kiro"]
    assert wait_for(lambda: o.models_view()["seats"]["kiro"]["known"])
    o.stop()


def wait_for(cond, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_office_detect_error_surfaces_in_view(tmp_path):
    from team.clis import CliInfo

    def boom(seat):
        raise RuntimeError("kiro-cli 沒有回應")
    o = _office(tmp_path, {"kiro": CliInfo("kiro", "Kiro", "/x", "acp")}, model_detector=boom)
    assert o.detect_models(["kiro"]) == ["kiro"]
    assert wait_for(lambda: not o.models_view()["seats"]["kiro"]["detecting"])
    k = o.models_view()["seats"]["kiro"]
    assert "偵測失敗" in k["error"] and "沒有回應" in k["error"] and k["known"] is False
    o.stop()


def test_office_detect_claude_and_all_clis(tmp_path):
    from team.clis import CliInfo
    o = _office(tmp_path, {
        "kiro": CliInfo("kiro", "Kiro", "/x/kiro-cli", "acp"),
        "claude": CliInfo("claude", "Claude Code", "/x/claude", "claude-headless"),
    }, model_detector=lambda s: o._detect_seat("claude") if s == "claude" else {"models": KIRO_MODELS, "current": "auto"})
    assert o.detect_models(["claude"]) == ["claude"]
    assert wait_for(lambda: bool(o.models_view()["seats"]["claude"]["detected_at"]))
    c_view = o.models_view()["seats"]["claude"]
    c_ids = [m["id"] for m in c_view["models"]]
    assert "haiku" in c_ids and "sonnet" in c_ids and "claude-sonnet-4-5" in c_ids
    assert o.prefs.problem("claude", "claude-sonnet-4-5") == ""
    started = o.detect_models(["all"])
    assert "kiro" in started and "claude" in started
    assert wait_for(lambda: not o.models_view()["seats"]["kiro"]["detecting"])
    o.stop()


def test_no_global_for_review_role(tmp_path, repo):  # noqa: F811
    prefs = _prefs(tmp_path)
    prefs._seats["agy"] = "gpt-9"  # 設定頁殘留的壞選擇（未偵測的 agy）
    env = Env(tmp_path, repo, [[{"say": "x"}]], prefs=prefs)
    env.dispatcher.prefs, env.dispatcher.catalog = prefs, prefs.catalog
    r = env.runner
    r._wt = None
    a = r._want_once("review", "agy", "kiro", "", "review")
    assert a.model == "gpt-9" and a.model_by == "global"
    env.store.update(env.room.id, lambda x: setattr(x, "review_pref", {"no_global": True}))
    r.room = env.get()
    assert r._use_global("review") is False and r._use_global("impl") is True
    a = r._want_once("review", "agy", "kiro", "", "review")
    assert a.seat == "agy" and a.model_by == "" and a.model != "gpt-9"
    # 實作的偏好鍵：no_global 時不看全域座位模型
    prefs._seats["kiro"] = "claude-opus-5.5"
    env.store.update(env.room.id, lambda x: (setattr(x, "impl_seat", "kiro"), setattr(x, "impl_pref", {"no_global": True})))
    r.room = env.get()
    assert r._impl_pref_key()[2] == ""
