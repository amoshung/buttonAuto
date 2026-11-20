"""Task 17 審查修正：沒好好結束的回合也記用量、角色說明與派工標示實際回答的模型、排隊中的模型呼叫、
ACP 沒回報費用 → None、快取讀取另計、依座位＋模型累計、用量檔每天一個。"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import pytest

from team.drivers import AcpDriver
from team.fleet import Assignment, Dispatcher, QuotaBook, SEAT_LABELS
from team.modelprefs import load_catalog
from team.models import ModelConfig, ModelReply, ModelRouter
from team.runner import _Paused, _TurnFailed
from team.usage import ModelCall, UsageBook, cli_event_text
from test_runner import BUILD_TURN, PLAN_TURN, SPEC_TURN, Env, repo  # noqa: F401  （repo 是 fixture）


class FakeDrv:
    def __init__(self, env, stop="end_turn", pause=False, error=""):
        self.env, self.stop, self.pause, self.error = env, stop, pause, error
        self.closed = False

    def prompt(self, text, timeout):
        if self.pause:
            self.env.runner._pause_ev.set()
        if self.error:
            self.env.runner._turn_errors.append(self.error)
        return self.stop

    def turn_usage(self):
        return {"model": "claude-sonnet-5.5", "in": 1200, "out": 40, "in_cache": 1000, "cost": 0.02,
                "currency": "USD"}

    def close(self):
        self.closed = True


class RaisingDrv(FakeDrv):
    """代理程序中途結束：prompt 丟 AcpError（用量在那之前已回報）。"""

    def prompt(self, text, timeout):
        from team.acp import AcpError
        raise AcpError("代理程序結束（exit 1）")


def _env(tmp_path, repo):  # noqa: F811
    from team.usage import UsageBook as UB
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    env.book = UB(None)
    env.runner.usage = env.book
    env.store.update(env.room.id, lambda r: (setattr(r, "impl_seat", "claude"),
                                            setattr(r, "impl_model", "sonnet")))
    env.runner.room = env.get()
    return env


@pytest.mark.parametrize("kind", ["error_stop", "agent_error", "quota", "paused", "acp_error"])
def test_unclean_turns_still_record_usage(tmp_path, repo, kind):  # noqa: F811
    env = _env(tmp_path, repo)
    drv = {"error_stop": lambda: FakeDrv(env, stop="error"),
           "agent_error": lambda: FakeDrv(env, error="something broke"),
           "quota": lambda: FakeDrv(env, error="You've hit your usage limit · resets at 3pm"),
           "paused": lambda: FakeDrv(env, pause=True),
           "acp_error": lambda: RaisingDrv(env)}[kind]()
    exc = _Paused if kind == "paused" else _TurnFailed
    with pytest.raises(exc) as ei:
        env.runner._run_turn(drv, "Claude", "hi")
    if kind == "quota":
        assert ei.value.quota is True
    ev = [e for e in env.events() if e["kind"] == "cli_usage"]
    assert len(ev) == 1 and ev[0]["ok"] is False and ev[0]["tokens_in"] == 1200 and ev[0]["tokens_in_cache"] == 1000
    assert "回合沒有好好結束" in ev[0]["text"] and "含快取（建立＋讀取）1000" in ev[0]["text"]
    room = env.get()
    assert room.tokens["claude"]["in"] == 1200 and room.tokens["claude"]["failed"] == 1
    assert room.tokens_models["claude|claude-sonnet-5.5"]["cost"] == pytest.approx(0.02)
    cli = env.book.summary({})["session"]["cli"][0]
    assert cli["failed_turns"] == 1 and cli["in_cache"] == 1000 and cli["cost"] == {"USD": 0.02}
    assert drv.closed or kind == "paused"


def test_failed_turn_without_numbers_records_nothing(tmp_path, repo):  # noqa: F811
    env = _env(tmp_path, repo)
    drv = FakeDrv(env, stop="error")
    drv.turn_usage = lambda: None
    with pytest.raises(_TurnFailed):
        env.runner._run_turn(drv, "Claude", "hi")
    assert not [e for e in env.events() if e["kind"] == "cli_usage"]


def test_tokens_keyed_by_seat_and_model_and_actual_resets(tmp_path, repo):  # noqa: F811
    env = _env(tmp_path, repo)
    env.runner._run_turn(FakeDrv(env), "Claude", "hi")
    assert env.get().impl_model_actual == "claude-sonnet-5.5"
    env.runner._assign_event("impl", Assignment("claude", "opus", "premium", "user", "x"))
    assert env.get().impl_model_actual == ""  # 同一座位換模型：等新的回報
    d = FakeDrv(env)
    d.turn_usage = lambda: {"model": "claude-opus-5.5", "in": 5, "out": 1, "cost": None, "currency": ""}
    env.runner._run_turn(d, "Claude", "hi")
    tm = env.get().tokens_models
    assert set(tm) == {"claude|claude-sonnet-5.5", "claude|claude-opus-5.5"}
    assert tm["claude|claude-opus-5.5"]["in"] == 5 and env.get().tokens["claude"]["in"] == 1205


def _glm_reply(model):
    def glm(prompt, effort, timeout):
        return ModelReply("請先寫測試再實作。", {"prompt_tokens": 10, "completion_tokens": 5}, model)
    return glm


def test_role_note_names_the_model_that_answered(tmp_path, repo):  # noqa: F811
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    note = env.runner._role_note("po", "PO 補充說明", "material")  # ScriptedRouter
    say = [e for e in env.events() if e["kind"] == "say"][-1]
    assert "PO 補充說明（示範（假模型））" in say["text"] and say["note_model"] == "scripted"
    assert "示範（假模型） 產生" in env.runner._noted("範本", "po", note)
    env.runner.router = ModelRouter(ModelConfig("http://x.invalid", "k", "d"), d1=lambda s, q: {},
                                    glm=_glm_reply("glm-5.3-0915"))
    note = env.runner._role_note("po", "PO 補充說明", "material")
    say = [e for e in env.events() if e["kind"] == "say"][-1]
    assert "（glm-5.3-0915 低思考量）" in say["text"] and say["note_model"] == "glm-5.3-0915"
    assert "glm-5.3-0915 低思考量 產生" in env.runner._noted("範本", "po", note)


def test_permission_event_labels_demo_model(tmp_path, repo):  # noqa: F811
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    room = env.run()
    assert room.status == "done", room.error
    p = [e for e in env.events() if e["kind"] == "permission"][0]
    assert p["model"] == "示範（假模型）" and "Decision-1" not in p["model"]


# ---------------------------------------------------------------- 派工標示誰回答
def _d1_answers(n):
    left = [n]

    def d1(state, questions):
        if left[0] <= 0:
            raise RuntimeError("down")
        left[0] -= 1
        return {"q": {"noul": 0.6}}
    return d1


def _glm_q(prompt, effort, timeout):
    return json.dumps({"q": {"noul": 0.5}})


@pytest.mark.parametrize("d1_n,by", [(99, "Decision-1"), (0, "glm-5.3"), (1, "Decision-1＋glm-5.3")])
def test_dispatch_names_who_answered(tmp_path, d1_n, by):
    r = ModelRouter(ModelConfig("http://x.invalid", "k", "d"), d1=_d1_answers(d1_n), glm=_glm_q)
    d = Dispatcher(r, load_catalog(""), QuotaBook(str(tmp_path)), lambda: {"claude", "glm"})
    a = d.pick("build", "task", ctx={"room_id": ""})
    assert a.by == by and a.reason.startswith(by)


# ---------------------------------------------------------------- 排隊中、延遲、例外後清乾淨
def test_inflight_queued_then_running_and_latency_excludes_wait():
    gate = threading.Event()

    def d1(state, questions):
        gate.wait(5)
        return {"q": {"noul": 0.7}}
    r = ModelRouter(ModelConfig("http://x.invalid", "k", "d"), d1=d1, glm=_glm_q)
    calls = []
    r.listeners.append(calls.append)
    for _ in range(8):
        r._d1_sem.acquire()
    t = threading.Thread(target=r.probability, args=("s", "q?"))
    t.start()
    time.sleep(0.3)
    f = r.inflight()
    assert len(f) == 1 and f[0]["state"] == "queued" and f[0]["state_label"] == "排隊中"
    for _ in range(8):
        r._d1_sem.release()
    time.sleep(0.1)
    assert r.inflight()[0]["state"] == "running"
    gate.set()
    t.join(5)
    assert r.inflight() == [] and calls[0].latency_ms < 250


def test_inflight_cleared_after_exception():
    def boom(*a):
        raise RuntimeError("down")
    r = ModelRouter(ModelConfig("http://x.invalid", "k", "d"), d1=boom, glm=boom)
    assert r.probability("s", "q?") is None
    assert r.inflight() == []


# ---------------------------------------------------------------- ACP 沒回報費用 → None
def test_acp_turn_without_usage_update_has_no_cost():
    drv = AcpDriver(["true"], ".", "Kiro", lambda ev: None, lambda req: "")
    c = drv.client
    c.cost, c.cost_updated = (0.5, "USD"), True
    assert drv.turn_usage()["cost"] == pytest.approx(0.5)
    c.cost_updated = False  # 下一回合沒有 usage_update
    assert drv.turn_usage()["cost"] is None
    c.cost, c.cost_updated = (0.8, "USD"), True
    assert drv.turn_usage()["cost"] == pytest.approx(0.3)


# ---------------------------------------------------------------- 用量檔：每天一個、跨午夜、並行寫入
def _call(ts):
    return ModelCall(ts=ts, room_id="", role="policy", model="Decision-1", effort="-", purpose="p", latency_ms=1,
                     ok=True, result="", kind="d1", tokens="not_counted")


def test_usage_file_rotates_at_midnight(tmp_path):
    clock = [datetime(2026, 11, 17, 23, 59, 50)]
    book = UsageBook(str(tmp_path / "usage.jsonl"), now=lambda: clock[0])
    book.record_model(_call(clock[0].isoformat()))
    book.record_cli({"seat": "claude", "model": "m", "in": 1, "out": 1, "cost": None, "currency": ""})
    clock[0] += timedelta(seconds=20)
    book.record_model(_call(clock[0].isoformat()))
    a, b = tmp_path / "usage-20261117.jsonl", tmp_path / "usage-20261118.jsonl"
    assert len(a.read_text(encoding="utf-8").splitlines()) == 2 and len(b.read_text(encoding="utf-8").splitlines()) == 1
    s = book.summary({})
    assert s["today"]["date"] == "2026-11-18" and s["today"]["models"][0]["calls"] == 1
    assert s["session"]["models"][0]["calls"] == 2
    # 重新啟動：只讀今天的檔
    again = UsageBook(str(tmp_path / "usage.jsonl"), now=lambda: clock[0]).summary({})
    assert again["today"]["models"][0]["calls"] == 1 and again["today"]["cli"] == []


def test_usage_concurrent_append(tmp_path):
    book = UsageBook(str(tmp_path / "usage.jsonl"))
    now = datetime.now().isoformat(timespec="seconds")

    def work():
        for _ in range(50):
            book.record_model(_call(now))
            book.record_cli({"seat": "kiro", "model": "", "in": 2, "out": 1, "cost": None, "currency": ""})
    ts = [threading.Thread(target=work) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lines = open(book.file_for(datetime.now().date().isoformat()), encoding="utf-8").read().splitlines()
    assert len(lines) == 800 and all(json.loads(x)["type"] in ("model", "cli") for x in lines)
    s = book.summary({})["today"]
    assert s["models"][0]["calls"] == 400 and s["cli"][0]["in"] == 800


def test_cli_event_text_cache_and_failed():
    t = cli_event_text({"seat": "claude", "model": "x", "in": 1200, "in_cache": 1000, "out": 3, "ok": False,
                        "cost": None, "currency": ""})
    assert "輸入 1200（含快取（建立＋讀取）1000）" in t and "回合沒有好好結束" in t


def test_demo_dispatch_label(tmp_path):
    from team.fakes.fake_models import ScriptedRouter
    d = Dispatcher(ScriptedRouter(), load_catalog(""), QuotaBook(str(tmp_path)), lambda: {"claude", "glm"})
    a = d.pick("build", "task", ctx={"room_id": ""})
    assert a.by == "Decision-1" and a.reason.startswith("示範（假模型，代 Decision-1）：")
