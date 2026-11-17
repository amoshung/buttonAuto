"""辦公室：排隊與同時運作上限、合併、重啟、用量試探。真 git、假代理、ScriptedRouter；不連網、不跑真 CLI。"""
import json
from datetime import datetime
import os
import subprocess
import threading
import time

import pytest

from config import AppConfig
from team.clis import CliInfo
from team.demo import create_demo_room
from team.fakes.fake_models import ScriptedRouter
from team.office import Office
from team.rooms import RoomStore


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


def wait(cond, timeout=60, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


class Env:
    def __init__(self, tmp_path, max_running=1, clis=None, store=None, **kw):
        self.tmp = tmp_path
        self.store = store or RoomStore(str(tmp_path / "team"))
        self.max = max_running
        self.changes = 0
        kw.setdefault("model_cfg", None)
        kw.setdefault("runner_kw", {"turn_timeout": 30})
        self.office = Office(self.store, lambda demo: ScriptedRouter(), lambda: [r"rm\s+-rf"],
                             lambda: self.max, self._changed, clis_fn=lambda: dict(clis or {}),
                             base_dir=str(tmp_path), **kw)

    def _changed(self):
        self.changes += 1

    def room(self, rid):
        return self.store.get(rid)

    def item(self, rid, kind, timeout=60):
        return wait(lambda: next((i for i in self.store.inbox() if i.room_id == rid and i.kind == kind), None),
                    timeout)

    def close(self):
        self.office.stop(timeout=15)


@pytest.fixture
def envs():
    made = []
    yield made
    for e in made:
        e.close()


def make_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def test_queue_limit_merge_and_snapshot(tmp_path, envs):
    env = Env(tmp_path, max_running=1)
    envs.append(env)
    env.office.start()
    env.office.start()  # 重複呼叫不會多開背景執行緒
    loop = env.office._loop_thread
    r1 = create_demo_room(env.office, str(tmp_path), autonomy="bold")
    r2 = create_demo_room(env.office, str(tmp_path))
    assert env.office._loop_thread is loop
    assert r1.demo and r1.impl_cli == "kiro" and r1.review_cli == "agy" and r1.user_pinned
    assert os.path.isfile(os.path.join(r1.repo, "progress.md"))
    # 上限 1：第一間運作、第二間排隊
    assert env.room(r1.id).status in ("queued", "running")
    assert env.room(r2.id).status == "queued"

    snap = env.office.snapshot()
    assert set(snap) >= {"rooms", "inbox", "roles", "clis", "max_running", "usage", "fleet"}
    assert snap["max_running"] == 1 and isinstance(snap["usage"], dict)
    assert [r["id"] for r in snap["rooms"]] == [r1.id, r2.id]
    for key in ("impl_seat", "impl_model", "review_seat", "review_model", "fallback_from", "events", "status"):
        assert key in snap["rooms"][0]
    assert len(snap["rooms"][0]["events"]) <= 5
    assert set(snap["roles"]) == {"po", "tech_lead", "scrum", "qa", "reviewer"}
    assert set(snap["fleet"]) == {"kiro", "agy", "claude", "glm", "glm_high"}
    assert snap["fleet"]["kiro"]["quota"]["state"] == "ok"
    assert set(snap["fleet"]["kiro"]) == {"label", "available", "quota", "rooms"}

    merge = env.item(r1.id, "merge")
    assert merge is not None, env.room(r1.id).error
    assert env.room(r1.id).status == "done"
    # 第一間完成後第二間自動開始
    assert wait(lambda: env.room(r2.id).status in ("running", "waiting_user"))
    # 已完成的房間：不能繼續、傳話不轉告
    act = env.room(r1.id).activity
    with pytest.raises(ValueError, match="已完成"):
        env.office.resume(r1.id)
    assert env.room(r1.id).activity == act
    res = env.office.send(r1.id, "再加除法")
    assert res == {"ok": False, "text": "這間房已經不在實作階段，訊息不會轉告代理"}

    # 合併只在你回答 merge 時發生
    assert "def mul" not in open(os.path.join(r1.repo, "calc.py"), encoding="utf-8").read()
    wt1 = env.room(r1.id).worktree
    assert os.path.isdir(wt1)
    res = env.office.answer(merge.id, "merge")
    assert res["ok"] is True, res
    assert "def mul" in open(os.path.join(r1.repo, "calc.py"), encoding="utf-8").read()
    room = env.room(r1.id)
    assert room.status == "done" and room.activity.startswith("已合併 ")
    assert room.worktree == "" and wait(lambda: not os.path.isdir(wt1), 10)
    assert "ai-team/add-mul" in git(r1.repo, "branch", "--list", "ai-team/*")  # 分支保留
    assert env.office.answer(merge.id, "merge")["ok"] is False  # 已處理過
    assert env.changes > 0

    # 第二間停在等你按開工：等你不占名額，第三間照樣開始
    spec = env.item(r2.id, "approve_spec")
    assert spec is not None
    assert wait(lambda: env.room(r2.id).status == "waiting_user", 10)
    r3 = create_demo_room(env.office, str(tmp_path), autonomy="bold")
    assert wait(lambda: env.room(r3.id).status in ("running", "done"), 10)
    snap = env.office.snapshot()
    assert any(i["id"] == spec.id for i in snap["inbox"])
    assert env.office.answer(spec.id, "nope")["ok"] is False
    merge3 = env.item(r3.id, "merge")
    assert merge3 is not None, env.room(r3.id).error
    assert wait(lambda: env.office._alive(r3.id) is None, 10)
    with pytest.raises(ValueError, match="待合併"):
        env.office.archive(r3.id)
    assert env.office.answer(merge3.id, "later")["ok"] is True
    env.office.archive(r3.id)
    assert env.room(r3.id).status == "archived"

    # 收尾：第二間暫停後封存
    env.office.pause(r2.id)
    assert wait(lambda: env.room(r2.id).status == "paused", 20)
    assert wait(lambda: env.office._alive(r2.id) is None, 10)
    wt2 = env.room(r2.id).worktree
    env.office.archive(r2.id)
    assert env.room(r2.id).status == "archived"
    assert not wt2 or not os.path.isdir(wt2)
    assert r2.id not in [r["id"] for r in env.office.snapshot()["rooms"]]


def test_merge_failure_returns_git_error(tmp_path, envs):
    env = Env(tmp_path, max_running=2)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold")
    merge = env.item(r.id, "merge")
    assert merge is not None, env.room(r.id).error
    with open(os.path.join(r.repo, "calc.py"), "a", encoding="utf-8") as f:
        f.write("# 使用者自己的改動\n")
    res = env.office.answer(merge.id, "merge")
    assert res["ok"] is False and "未提交" in res["text"]
    assert any(i.id == merge.id for i in env.store.inbox())  # 仍可再按一次
    git(r.repo, "checkout", "--", "calc.py")
    wt = env.room(r.id).worktree
    res = env.office.answer(merge.id, "merge")
    assert res["ok"] is True and res["sha"] and res["text"].startswith("已合併")
    assert wait(lambda: not os.path.isdir(wt), 10)
    assert any(e["kind"] == "merge" for e in env.store.events(r.id))


def test_create_room_validation(tmp_path, envs):
    repo = make_repo(tmp_path / "proj")
    env = Env(tmp_path, max_running=0, clis={"kiro": CliInfo("kiro", "Kiro", "/x/kiro-cli", "acp"),
                                             "claude": CliInfo("claude", "Claude", "/x/claude", "headless")})
    envs.append(env)
    kw = dict(repo=str(repo), goal="加入 CSV 匯出", source="goal", autonomy="balanced", allowed_paths=[])
    with pytest.raises(ValueError, match="Antigravity"):
        env.office.create_room(impl_cli="kiro", review_cli="agy", **kw)
    with pytest.raises(ValueError, match="不認得"):
        env.office.create_room(impl_cli="cursor", review_cli="auto", **kw)
    with pytest.raises(ValueError, match="litellm"):
        env.office.create_room(impl_cli="glm", review_cli="auto", **kw)  # 有 claude 但沒有模型設定
    with pytest.raises(ValueError, match="不同"):
        env.office.create_room(impl_cli="kiro", review_cli="kiro", **kw)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="git"):
        env.office.create_room(**dict(kw, repo=str(plain)), impl_cli="auto", review_cli="auto")
    with pytest.raises(ValueError, match="相對路徑"):
        env.office.create_room(**dict(kw, allowed_paths=["../x"]), impl_cli="auto", review_cli="auto")
    assert env.store.list() == []
    room = env.office.create_room(impl_cli="kiro", review_cli="auto", **kw)
    assert room.status == "queued" and room.user_pinned and room.name == "proj"
    assert room.slug and room.slug != "" and room.allowed_paths == []
    auto = env.office.create_room(impl_cli="auto", review_cli="auto", **kw)
    assert not auto.user_pinned and auto.slug != room.slug  # 同一個 repo 的 slug 不重複


def test_restart_pauses_running_and_answer_resumes(tmp_path, envs):
    env1 = Env(tmp_path, max_running=0)
    envs.append(env1)
    r = create_demo_room(env1.office, str(tmp_path))
    assert env1.room(r.id).status == "queued"
    env1.store.update(r.id, lambda x: setattr(x, "status", "running"))  # 模擬運作中當掉

    env2 = Env(tmp_path, max_running=1, store=RoomStore(str(tmp_path / "team")))
    envs.append(env2)
    env2.office.start()
    room = env2.room(r.id)
    assert room.status == "paused" and "重新啟動" in room.activity
    env2.office.resume(r.id)
    spec = env2.item(r.id, "approve_spec")
    assert spec is not None
    env2.office.stop(timeout=15)  # 停在等你按開工時關掉後端
    assert env2.room(r.id).status == "paused"

    env3 = Env(tmp_path, max_running=1, store=RoomStore(str(tmp_path / "team")))
    envs.append(env3)
    env3.office.start()
    assert env3.room(r.id).status == "paused"
    res = env3.office.answer(spec.id, "start")  # 沒有執行器在等：先繼續，答案隨後送到
    assert res["ok"] is True
    assert env3.item(r.id, "merge") is not None, env3.room(r.id).error
    assert [i.id for i in env3.store.inbox(include_resolved=True) if i.kind == "approve_spec"] == [spec.id]


def test_error_retry_resume_send_and_pause(tmp_path, envs):
    env = Env(tmp_path, max_running=0)
    envs.append(env)
    r = create_demo_room(env.office, str(tmp_path))
    res = env.office.send(r.id, "請用中文寫註解")
    assert res["ok"] is True
    assert env.office.send(r.id, "  ")["ok"] is False
    assert any(e["kind"] == "user" and "【你】請用中文寫註解" in e["text"] for e in env.store.events(r.id))
    env.office.pause(r.id)
    assert env.room(r.id).status == "paused"
    env.store.update(r.id, lambda x: (setattr(x, "status", "error"), setattr(x, "error", "壞了")))
    item = env.store.add_inbox(r.id, "error", "停下來了", "壞了", [{"id": "retry", "label": "重試"},
                                                                {"id": "later", "label": "先放著"}])
    res = env.office.answer(item.id, "retry")
    assert res["ok"] is True
    room = env.room(r.id)
    assert room.status == "queued" and room.error == ""
    assert not [i for i in env.store.inbox() if i.kind == "error"]
    with pytest.raises(ValueError):
        env.office.archive(r.id)  # 排隊中不能封存
    with pytest.raises(ValueError):
        env.office.pause("rffffffff")
    # 放寬上限後開始跑，傳的話在第一個實作回合送到
    env.max = 1
    env.office.schedule()
    assert wait(lambda: env.office._alive(r.id) is not None, 10)
    assert env.item(r.id, "approve_spec") is not None
    prompts = [e for e in env.store.events(r.id, limit=1000) if e["kind"] == "prompt"]
    assert any("【你】請用中文寫註解" in e["text"] for e in prompts)


def test_demo_quota_is_separate_from_real_fleet(tmp_path, envs):
    probed = []
    env = Env(tmp_path, max_running=1, prober=lambda seat: probed.append(seat) or True, probe_interval=0.1)
    envs.append(env)
    env.office.start()
    from team.demo import plan_turn, spec_turn
    quota = [{"error": "You've hit your usage limit, try again in 1 seconds"}]
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [spec_turn(), plan_turn(), quota]})
    assert env.item(r.id, "merge") is not None, env.room(r.id).error
    room = env.room(r.id)
    assert room.impl_seat == "glm" and room.fallback_from == "kiro"  # 示範房自己的用量狀態讓它改由 glm
    assert env.office.demo_quota.is_exhausted("kiro", None)
    snap = env.office.snapshot()
    assert snap["fleet"]["kiro"]["quota"]["state"] == "ok"  # 真座位不受影響
    assert not os.path.exists(env.office.quota.path) or "kiro" not in open(env.office.quota.path).read()
    time.sleep(1.5)
    assert probed == []  # 只試探真的用量狀態
    assert env.office.demo_quota.is_exhausted("kiro", None)


def test_probe_restores_real_seat(tmp_path, envs):
    from datetime import datetime
    gate = threading.Event()
    probed = []

    def prober(seat):
        probed.append(seat)
        gate.wait(30)
        return True

    env = Env(tmp_path, max_running=1, prober=prober, probe_interval=0.1)
    envs.append(env)
    env.office.quota.mark_exhausted("kiro", "usage limit, try again in 1 seconds", datetime.now())
    env.office.start()
    assert env.office.snapshot()["fleet"]["kiro"]["quota"]["state"] == "exhausted"
    assert wait(lambda: probed, 10) and probed[0] == "kiro"
    time.sleep(0.4)
    gate.set()
    assert wait(lambda: env.office.snapshot()["fleet"]["kiro"]["quota"]["state"] == "ok", 10)
    assert probed.count("kiro") == 1  # 同一座位同時只試探一次


def test_permission_forwarded_and_expired(tmp_path, envs):
    from team.demo import build_turn, plan_turn, spec_turn
    danger = {"tool": {"title": "清理", "kind": "execute", "rawInput": {"command": "rm -rf build"}}}
    env = Env(tmp_path, max_running=1)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [spec_turn(), plan_turn(), [danger] + build_turn()]})
    perm = env.item(r.id, "permission")
    assert perm is not None, env.room(r.id).error
    assert wait(lambda: env.office._alive(r.id) is not None and env.office._alive(r.id).waiting_on(perm.id), 10)
    res = env.office.answer(perm.id, "allow")
    assert res == {"ok": True, "text": "已轉告"}
    assert env.item(r.id, "merge") is not None, env.room(r.id).error
    done = [i for i in env.store.inbox(include_resolved=True) if i.id == perm.id][0]
    assert done.resolved and done.answer == "allow"
    # 沒有執行器在等的權限請求：失效
    old = env.store.add_inbox(r.id, "permission", "要執行：x", "x", [{"id": "allow", "label": "允許一次"},
                                                                  {"id": "reject", "label": "拒絕"}])
    assert wait(lambda: env.office._alive(r.id) is None, 10)
    res = env.office.answer(old.id, "allow")
    assert res["ok"] is False and "失效" in res["text"]
    old = [i for i in env.store.inbox(include_resolved=True) if i.id == old.id][0]
    assert old.resolved and old.answer == "reject"


def test_resume_while_old_thread_alive(tmp_path, envs):
    env = Env(tmp_path, max_running=1)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path))
    spec = env.item(r.id, "approve_spec")
    assert spec is not None
    runner = env.office._alive(r.id)
    gate = threading.Event()
    orig = runner._close_all
    runner._close_all = lambda: (gate.wait(20), orig())
    env.office.pause(r.id)
    assert wait(lambda: env.room(r.id).status == "paused", 10)
    assert runner.is_alive() and runner.stopping
    env.office.resume(r.id)  # 舊執行器還在收尾：記下要繼續
    assert [x for x in env.office.snapshot()["rooms"] if x["id"] == r.id][0]["resume_requested"]
    assert env.office._alive(r.id) is runner
    gate.set()
    assert wait(lambda: env.office._alive(r.id) not in (None, runner), 10)
    assert wait(lambda: env.room(r.id).status == "waiting_user", 20)
    assert [i.id for i in env.store.inbox() if i.room_id == r.id] == [spec.id]  # 沿用同一個項目
    assert env.office.answer(spec.id, "start")["ok"] is True
    assert env.item(r.id, "merge") is not None, env.room(r.id).error
    assert env.office._pending == {}


def test_start_failure_creates_error_item(tmp_path, envs):
    def factory(demo):
        if demo:
            raise RuntimeError("路由器壞了")
        return ScriptedRouter()

    env = Env(tmp_path, max_running=1)
    env.office.router_factory = factory
    envs.append(env)
    r = create_demo_room(env.office, str(tmp_path))
    room = env.room(r.id)
    assert room.status == "error" and "路由器壞了" in room.error
    items = [i for i in env.store.inbox() if i.room_id == r.id and i.kind == "error"]
    assert len(items) == 1 and [o["id"] for o in items[0].options] == ["retry", "later"]
    assert env.changes > 0


def test_config_team_fields(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"team_max_running": 99, "team_cost_weight": -1, "team_context_rollover": 0.8,
                             "team_plugin_dirs": "C:\\a; D:\\b ;"}), encoding="utf-8")
    cfg = AppConfig.load(str(p))
    assert cfg.team_max_running == 4.0 and cfg.team_cost_weight == 0.3 and cfg.team_context_rollover == 0.8
    assert cfg.team_plugin_list() == ["C:\\a", "D:\\b"]
    d = AppConfig()
    assert (d.team_max_running, d.team_plugin_dirs, d.team_cost_weight, d.team_context_rollover) == (4.0, "", 0.3, 0.70)


def test_model_calls_reach_room_events_usage_and_snapshot(tmp_path, envs):
    """真的 ModelRouter（假 glm 回傳 usage）：呼叫寫成房間的 model 事件、用量帳（usage.jsonl）與快照的進行中清單。"""
    from team.models import ModelConfig, ModelReply, ModelRouter
    gate, started = threading.Event(), threading.Event()

    def glm(prompt, effort, timeout):
        started.set()
        gate.wait(5)
        return ModelReply("重點：先驗證除以零", {"prompt_tokens": 300, "completion_tokens": 40, "total_tokens": 340},
                          "glm-5.3-0901")
    cfg = ModelConfig("http://x.invalid", "SECRET-GLM", "SECRET-D1")
    env = Env(tmp_path, max_running=0, model_cfg=cfg)
    env.office.router_factory = lambda demo: ModelRouter(cfg, d1=lambda s, q: {}, glm=glm)
    envs.append(env)
    repo = make_repo(tmp_path / "repo")
    rid = env.office.create_room(repo=str(repo), goal="加除法", source="goal").id
    router = env.office._router(False)
    t = threading.Thread(target=router.write, args=("x",),
                         kwargs={"ctx": {"room_id": rid, "role": "reviewer", "purpose": "Reviewer 審查重點"}})
    t.start()
    assert started.wait(5)
    snap = env.office.snapshot()
    room = next(r for r in snap["rooms"] if r["id"] == rid)
    assert room["inflight"][0]["model"] == "glm-5.3" and room["inflight"][0]["role"] == "reviewer"
    assert snap["inflight"][0]["purpose"] == "Reviewer 審查重點"
    gate.set()
    t.join(5)
    assert env.office.snapshot()["inflight"] == []
    ev = [e for e in env.store.events(rid, limit=50) if e["kind"] == "model"]
    assert len(ev) == 1 and ev[0]["model"] == "glm-5.3-0901" and ev[0]["total_tokens"] == 340
    assert "Reviewer：Reviewer 審查重點" in ev[0]["text"] and "共 340" in ev[0]["text"]
    u = env.office.usage_summary()
    m = u["today"]["models"][0]
    assert (m["model"], m["effort"], m["calls"], m["total_tokens"]) == ("glm-5.3-0901", "low", 1, 340)
    assert u["today"]["rooms"][0]["name"] and u["today"]["rooms"][0]["model_tokens"] == 340
    raw = open(env.office.usage.file_for(datetime.now().date().isoformat()), encoding="utf-8").read()
    assert "glm-5.3-0901" in raw and "SECRET" not in raw


# ---- Task 20：代管 ----
def test_delegate_demo_room_runs_to_merge_without_asking(tmp_path, envs):
    env = Env(tmp_path, max_running=1)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path))  # 平衡模式：沒代管時要你按開工
    env.office.set_delegate(r.id, True)
    assert env.room(r.id).delegate is True
    merge = env.item(r.id, "merge")
    assert merge is not None, env.room(r.id).error
    kinds = {i.kind for i in env.store.inbox() if i.room_id == r.id}
    assert kinds == {"merge"}  # 合併仍交給你
    ev = env.store.events(r.id, limit=1000)
    assert any(e["kind"] == "delegate" and e["text"] == "你開啟代管" for e in ev)
    dec = [e for e in ev if e["kind"] == "decision"]
    assert dec and all(e["text"].startswith("代管決定：") and e.get("demo") is True for e in dec)
    assert any(e["kind"] == "model" and e.get("role") == "delegate" and e["model"] == "scripted" for e in ev)
    assert env.office.snapshot()["rooms"][0]["delegate"] is True
    # 關閉代管：恢復原本行為
    env.office.set_delegate(r.id, False)
    assert env.room(r.id).delegate is False
    assert any(e["kind"] == "delegate" and e["text"] == "你關閉代管" for e in env.store.events(r.id, limit=1000))


def test_delegate_on_resumes_paused_room_with_open_item(tmp_path, envs):
    env = Env(tmp_path, max_running=1)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path))
    spec = env.item(r.id, "approve_spec")
    assert spec is not None
    env.office.pause(r.id)
    assert wait(lambda: env.room(r.id).status == "paused" and env.office._alive(r.id) is None, 15)
    env.office.set_delegate(r.id, True)
    assert env.item(r.id, "merge") is not None, env.room(r.id).error
    got = env.store.get_item(spec.id)
    assert got.answer == "start" and got.text.startswith("代管")


def test_delegate_retries_error_only_once(tmp_path, envs):
    env = Env(tmp_path, max_running=0)
    envs.append(env)
    r = create_demo_room(env.office, str(tmp_path))
    env.office.pause(r.id)
    env.store.update(r.id, lambda x: (setattr(x, "status", "error"), setattr(x, "error", "壞了")))
    item = env.store.add_inbox(r.id, "error", "停下來了", "壞了", [{"id": "retry", "label": "重試"},
                                                                {"id": "later", "label": "先放著"}])
    env.office.set_delegate(r.id, True)
    assert env.store.get_item(item.id).answer == "retry"
    assert env.room(r.id).status == "queued" and env.room(r.id).attempts["delegate_error"] == 1
    # 第二次出錯：不再自動重試，交給你
    env.store.update(r.id, lambda x: (setattr(x, "status", "error"), setattr(x, "error", "又壞了")))
    item2 = env.store.add_inbox(r.id, "error", "停下來了", "又壞了", [{"id": "retry", "label": "重試"},
                                                                    {"id": "later", "label": "先放著"}])
    env.office._delegate_idle(r.id)
    it2 = env.store.get_item(item2.id)
    assert not it2.resolved and it2.handoff is True
    texts = [e["text"] for e in env.store.events(r.id, limit=100) if e["kind"] == "decision"]
    assert any("自動按「重試」" in t for t in texts) and any("交給你" in t for t in texts)
    with pytest.raises(ValueError):
        env.office.set_delegate("rffffffff", True)


def test_delegate_never_touches_merge_or_login(tmp_path, envs):
    env = Env(tmp_path, max_running=0)
    envs.append(env)
    r = create_demo_room(env.office, str(tmp_path))
    env.office.pause(r.id)
    m = env.store.add_inbox(r.id, "merge", "合併？", "x", [{"id": "merge", "label": "合併"}, {"id": "later", "label": "先不要"}])
    lg, _ = env.store.join_login(r.id, "kiro", "demo", "需要登入 Kiro", "x",
                                 [{"id": "open", "label": "開啟登入視窗"}, {"id": "check", "label": "我已登入，重新檢查"}])
    env.office.set_delegate(r.id, True)
    assert not env.store.get_item(m.id).resolved and not env.store.get_item(lg.id).resolved
    assert env.room(r.id).status == "paused"  # 只有合併與登入項目：房間不動
    assert not [e for e in env.store.events(r.id, limit=100) if e["kind"] == "decision"]


def test_delegate_runner_exit_error_retries_once_then_hands_off(tmp_path, envs):
    """執行器先寫好錯誤項目才結束（_fail 在 run() 裡，_runner_exited 在外層 finally）：代管自動重試一次，再錯交給你。"""
    import shutil
    env = Env(tmp_path, max_running=0)
    envs.append(env)
    r = create_demo_room(env.office, str(tmp_path))
    env.office.set_delegate(r.id, True)
    os.rename(os.path.join(r.repo, ".git"), os.path.join(r.repo, ".notgit"))  # 不再是 git repo → 執行器 _Fatal → 錯誤項目
    seen = []
    real = env.office._runner_exited

    def spy(runner):
        seen.append([i.kind for i in env.store.inbox() if i.room_id == r.id])
        real(runner)
    env.office._runner_exited = spy
    env.max = 1
    env.office.start()
    assert wait(lambda: [i for i in env.store.inbox() if i.room_id == r.id and i.kind == "error" and i.handoff], 30)
    assert seen and seen[0] == ["error"]  # 結束回呼時錯誤項目已經在
    errs = [i for i in env.store.inbox(include_resolved=True) if i.room_id == r.id and i.kind == "error"]
    assert len(errs) == 2 and errs[0].answer == "retry" and errs[0].text.startswith("代管") and not errs[1].resolved
    texts = [e["text"] for e in env.store.events(r.id, limit=500) if e["kind"] == "decision"]
    assert any("自動按「重試」" in t for t in texts) and any("交給你" in t for t in texts)
    assert env.room(r.id).status == "error"
