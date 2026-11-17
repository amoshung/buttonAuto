"""房間執行緒：真 git、真子程序假代理（ACP／headless）、ScriptedRouter；不連網、不跑真 CLI。"""
import json
import os
import subprocess
import sys
import time

import pytest

from team.clis import CliInfo
from team.fakes.fake_models import ScriptedRouter
from team.fleet import Dispatcher, QuotaBook, load_catalog
from team.models import ModelConfig
from team.rooms import RoomStore
from team.runner import RoomRunner, context_fraction

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")
FAKE_CLAUDE = os.path.join(ROOT, "team", "fakes", "fake_claude.py")
PY = sys.executable
SECRET = "sk-secret-glm-key-777"
D1_SECRET = "sk-secret-d1-key-888"
SLUG = "add-mul"
DOCS = "../proj-%s.notes" % SLUG

CALC_ADD = "def add(a, b):\n    return a + b\n"
TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
CALC_MUL = CALC_ADD + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_MUL = ("from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")
# 「assert True」會先被實作查核（空殼掃描）擋下；這裡用不檢查 mul 行為的斷言，交給 QA 的換回原狀檢查
TEST_FAKE = "import calc\n\n\ndef test_mul():\n    assert 1 + 1 == 2\n"

SPEC = {
    "criteria": [{"id": "C1", "text": "calc 加入 mul(a, b)",
                  "check": '"%s" -c "from calc import mul; assert mul(2, 3) == 6"' % PY}],
    "test_cmd": '"%s" -m pytest -q -p no:cacheprovider' % PY,
    "allowed_paths": ["calc.py", "test_calc.py"],
}
SPEC_TURN = [{"say": "我讀完專案了。\n```json\n%s\n```" % json.dumps(SPEC, ensure_ascii=False)}]
PLAN_TURN = [{"write": {"path": DOCS + "/plan.md", "content": "# 計畫\n1. 加入 mul 與測試（對應 C1）\n"}},
             {"say": "DONE"}]
SAFE_TOOL = {"tool": {"title": "跑測試", "kind": "execute",
                      "rawInput": {"command": "python -m pytest -q"}}}
BUILD_TURN = [{"write": {"path": "calc.py", "content": CALC_MUL}},
              {"write": {"path": "test_calc.py", "content": TEST_MUL}},
              SAFE_TOOL, {"commit": "C1: mul"}, {"say": "DONE"}]
REVIEW_OK = [[{"say": '審查完成。\n```json\n{"blocking":[],"suggestions":[]}\n```'}]]
QUOTA_ERR = [{"error": "You've hit your usage limit · resets at 3pm"}]
SECTIONS = ["目標", "驗收條件狀態", "已完成（commit）", "正在做", "下一步", "未決問題", "注意事項"]
PROGRESS = "# 進度\n" + "".join("## %s\n- 內容\n" % s for s in SECTIONS)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "calc.py").write_text(CALC_ADD, encoding="utf-8")
    (r / "test_calc.py").write_text(TEST_ADD, encoding="utf-8")
    (r / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


class Env:
    def __init__(self, tmp_path, repo, impl, review=REVIEW_OK, glm=None, available=("kiro", "agy"),
                 pinned=True, autonomy="bold", impl_cmd=None, on_change=None, **kw):
        self.tmp = tmp_path
        self.store = RoomStore(str(tmp_path / "team"))
        self.room = self.store.create(name="proj", repo=str(repo), slug=SLUG, goal="在 calc 加入 mul 函式",
                                      source="goal", impl_cli="kiro", review_cli="agy", autonomy=autonomy,
                                      user_pinned=pinned)
        files = {}
        for name, script in (("impl", impl), ("review", review), ("glm", glm or [[{"say": "DONE"}]])):
            p = tmp_path / ("%s.json" % name)
            p.write_text(json.dumps(script, ensure_ascii=False), encoding="utf-8")
            files[name] = str(p)
        overrides = {
            "kiro": impl_cmd or [PY, AGENT, files["impl"], str(tmp_path / "impl.state")],
            "agy": [PY, AGENT, files["review"], str(tmp_path / "review.state")],
            "glm": [PY, FAKE_CLAUDE, "--script", files["glm"]],
        }
        clis = {"kiro": CliInfo("kiro", "Kiro", "/nonexistent/kiro-cli", "acp"),
                "agy": CliInfo("agy", "Antigravity", "/nonexistent/agy", "acp"),
                "claude": CliInfo("claude", "Claude", "/nonexistent/claude", "headless")}
        self.router = ScriptedRouter()
        self.quota = QuotaBook(str(tmp_path / "team"))
        self.dispatcher = Dispatcher(self.router, load_catalog(""), self.quota, available=lambda: set(available))
        self.changes = 0
        self._hook = on_change

        def changed():
            self.changes += 1
            if self._hook:
                self._hook(self)

        kw.setdefault("turn_timeout", 30)
        self.runner = RoomRunner(self.room.id, self.store, self.router, clis, [r"rm\s+-rf"], changed,
                                 cmd_overrides=overrides, dispatcher=self.dispatcher, quota=self.quota,
                                 model_cfg=ModelConfig("http://litellm.invalid", SECRET, D1_SECRET), **kw)

    def get(self):
        return self.store.get(self.room.id)

    def events(self):
        return list(reversed(self.store.events(self.room.id, limit=100000)))

    def texts(self, kind=None):
        return [e["text"] for e in self.events() if kind is None or e["kind"] == kind]

    def items(self, kind=None, resolved=False):
        return [i for i in self.store.inbox(include_resolved=resolved)
                if i.room_id == self.room.id and (kind is None or i.kind == kind)]

    def wait(self, cond, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            if not self.runner.is_alive() and not cond():
                return cond()
            time.sleep(0.05)
        return False

    def run(self, timeout=60):
        self.runner.start()
        self.runner.join(timeout)
        assert not self.runner.is_alive(), "執行緒沒有在時限內結束"
        return self.get()

    def no_secrets(self):
        raw = open(os.path.join(self.store.root, self.room.id, "events.jsonl"), encoding="utf-8").read()
        raw += open(os.path.join(self.store.root, self.room.id, "room.json"), encoding="utf-8").read()
        assert SECRET not in raw and D1_SECRET not in raw


def test_happy_path_reaches_done(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    room = env.run()
    assert room.status == "done", room.error
    assert room.phase == "done" and room.persona == ""
    merges = env.items("merge")
    assert len(merges) == 1
    assert [o["id"] for o in merges[0].options] == ["merge", "later"]
    assert "C1: mul" in merges[0].detail and "calc.py" in merges[0].detail
    wt = room.worktree
    assert not os.path.exists(os.path.join(wt, "docs", "ai-team"))
    assert os.path.isfile(os.path.join(room.notes_dir, "evidence.md"))
    assert os.path.isfile(os.path.join(room.notes_dir, "spec.md"))
    assert os.path.isfile(os.path.join(room.notes_dir, "plan.md"))
    log = git(wt, "log", "--format=%an %s")
    assert "ai-team: spec" not in log and "ai-team: evidence" not in log
    assert git(wt, "status", "--porcelain").strip() == ""
    assert room.criteria[0]["id"] == "C1" and room.allowed_paths == ["calc.py", "test_calc.py"]
    assert room.base and room.branch == "ai-team/" + SLUG and room.target_branch == "main"
    # 派工：你指定實作 kiro、審查 agy（不同座位；自動派工不會選 Kiro／Antigravity）
    assigns = [e for e in env.events() if e["kind"] == "assign"]
    assert assigns[0]["seat"] == "kiro" and assigns[0]["by"] == "user"
    assert any(e["seat"] == "agy" and e["role"] == "review" for e in assigns)
    assert room.impl_seat == "kiro" and room.review_seat == "agy"
    assert room.impl_by == "user" and room.review_by == "user"  # 前端「你指定」標籤的依據
    # 真的 policy.decide 自動允許安全指令
    assert any(e["kind"] == "permission" and e.get("outcome") == "allow" for e in env.events())
    assert any(e["kind"] == "agent" and "DONE" in e["text"] for e in env.events())
    phases = [e["phase"] for e in env.events() if e["kind"] == "phase"]
    assert phases == ["spec", "plan", "build", "test", "review", "done"]
    assert room.context_pct > 0 and room.context_estimated
    env.no_secrets()


def test_fake_test_is_caught_by_qa(tmp_path, repo):
    fake = [{"write": {"path": "calc.py", "content": CALC_MUL}},
            {"write": {"path": "test_calc.py", "content": TEST_FAKE}},
            {"commit": "C1: mul"}, {"say": "DONE"}]
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, fake], max_fix_attempts=1)
    env.runner.start()
    assert env.wait(lambda: env.items("question") and env.get().status == "waiting_user", 60), env.get().error
    room = env.get()
    # 關卡結果先存起來：回到 build、待送的修正已記下（重啟後直接送，不重跑關卡）
    assert room.status == "waiting_user" and room.phase == "build"
    assert "沒有真正檢查" in room.pending_fix
    ev = env.events()
    qa = [i for i, e in enumerate(ev) if e["kind"] == "say" and e.get("role") == "qa" and "沒有真正檢查" in e["text"]]
    assert qa
    later_phases = [e["phase"] for e in ev[qa[0]:] if e["kind"] == "phase"]
    assert "build" in later_phases
    assert room.attempts["test"] == 2
    assert not env.items("merge", resolved=True)
    env.runner.pause()
    env.runner.join(10)
    assert not env.runner.is_alive()
    assert env.get().status == "paused"
    assert env.items("question")  # 暫停時不解決，繼續時再等
    # 繼續：先等同一個問題；回答再試一次後直接送存起來的修正（不重跑關卡、不重複計次）
    n_before = len(env.events())
    r2 = RoomRunner(env.room.id, env.store, env.router, env.runner.clis, [], lambda: None,
                    cmd_overrides=env.runner.cmd_overrides, dispatcher=env.dispatcher, quota=env.quota,
                    turn_timeout=30, max_fix_attempts=1)
    r2.start()
    env.runner = r2
    assert env.wait(lambda: env.get().status == "waiting_user")
    q = env.items("question")
    assert len(q) == 1
    assert r2.answer(q[0].id, "retry") is True
    assert env.wait(lambda: any(e["kind"] == "prompt" for e in env.events()[n_before:]))
    new = env.events()[n_before:]
    first = [e for e in new if e["kind"] == "prompt"][0]
    assert (first.get("body") or first["text"]).startswith("【QA】")  # 換新程序時前面接交接摘要
    assert not any(e["kind"] == "say" and e.get("role") == "qa" for e in new[:new.index(first)])
    r2.pause()
    r2.join(10)


def test_dangerous_permission_goes_to_inbox(tmp_path, repo):
    build = BUILD_TURN[:-1] + [{"tool": {"title": "清 build", "kind": "execute",
                                         "rawInput": {"command": "rm -rf build"}}}, {"say": "DONE"}]
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, build])
    env.runner.start()
    assert env.wait(lambda: env.items("permission"))
    item = env.items("permission")[0]
    assert "rm -rf build" in item.title + item.detail
    assert [o["id"] for o in item.options] == ["allow", "reject"]
    assert env.wait(lambda: env.get().status == "waiting_user")
    assert env.runner.answer(item.id, "maybe") is False
    assert env.runner.answer(item.id, "reject") is True
    env.runner.join(60)
    room = env.get()
    assert room.status == "done", room.error
    assert any("被拒絕" in t for t in env.texts("agent"))
    assert env.items("permission", resolved=True)[0].answer == "reject"


def test_permission_timeout_rejects_and_records(tmp_path, repo):
    build = BUILD_TURN[:-1] + [{"tool": {"title": "清 build", "kind": "execute",
                                         "rawInput": {"command": "rm -rf build"}}}, {"say": "DONE"}]
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, build], permission_timeout=0.5)
    room = env.run()
    assert room.status == "done", room.error
    it = env.items("permission", resolved=True)[0]
    assert it.answer == "reject" and "沒有回應，已拒絕" in it.text
    assert any("沒有回應，已拒絕" in t for t in env.texts())


def test_not_a_repo_sets_error(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    env = Env(tmp_path, plain, [SPEC_TURN])
    room = env.run(20)
    assert room.status == "error" and "不是 git repo" in room.error
    assert not (tmp_path / ".ai-team-worktrees").exists()


def test_cli_crash_sets_error(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN], impl_cmd=[PY, "-c", "import sys; sys.exit(3)"])
    room = env.run(30)
    assert room.status == "error" and "登入" in room.error and "kiro-cli" in room.error
    errs = env.items("error")
    assert len(errs) == 1 and [o["id"] for o in errs[0].options] == ["retry", "later"]
    assert errs[0].title == "proj 停下來了" and "登入" in errs[0].detail
    env.runner._fail("再一次")
    assert len(env.items("error")) == 1


def test_pause_stops_thread(tmp_path, repo):
    env = Env(tmp_path, repo, [[{"run": '"%s" -c "import time; time.sleep(10)"' % PY}]])
    env.runner.start()
    assert env.wait(lambda: any(e["kind"] == "prompt" for e in env.events()), 20)
    time.sleep(0.5)
    t0 = time.time()
    env.runner.pause()
    env.runner.join(3)
    assert not env.runner.is_alive() and time.time() - t0 < 3
    assert env.get().status == "paused"


def test_spec_approval_and_edit(tmp_path, repo):
    revised = dict(SPEC, criteria=SPEC["criteria"] + [{"id": "C2", "text": "mul 支援負數",
                                                        "check": '"%s" -c "from calc import mul; assert mul(-2, 3) == -6"' % PY}])
    rev_turn = [{"echo_prompt": True}, {"say": "```json\n%s\n```" % json.dumps(revised, ensure_ascii=False)}]
    env = Env(tmp_path, repo, [SPEC_TURN, rev_turn, [{"say": "稍等"}]], autonomy="balanced")
    env.runner.start()
    assert env.wait(lambda: env.items("approve_spec"))
    item = env.items("approve_spec")[0]
    assert [o["id"] for o in item.options] == ["start", "edit"] and "C1" in item.detail
    assert env.runner.answer(item.id, "edit", "請加一條負數的條件") is True
    assert env.wait(lambda: env.items("approve_spec") and env.items("approve_spec")[0].id != item.id)
    item2 = env.items("approve_spec")[0]
    assert "C2" in item2.detail
    assert any("請加一條負數的條件" in t for t in env.texts("agent"))
    assert [c["id"] for c in env.get().criteria] == ["C1", "C2"]
    env.runner.pause()
    env.runner.join(10)
    # 繼續時沿用尚未回答的 approve_spec 項目，不重問
    env2 = Env.__new__(Env)
    env2.__dict__.update(env.__dict__)
    env2.runner = RoomRunner(env.room.id, env.store, env.router, env.runner.clis, [], lambda: None,
                             cmd_overrides=env.runner.cmd_overrides, dispatcher=env.dispatcher,
                             quota=env.quota, turn_timeout=30)
    env2.runner.start()
    assert env2.wait(lambda: env2.get().status == "waiting_user")
    assert [i.id for i in env2.items("approve_spec")] == [item2.id]
    env2.runner.pause()
    env2.runner.join(10)


def test_quota_switches_to_glm(tmp_path, repo):
    glm = [[{"write": {"path": "calc.py", "content": CALC_MUL}},
            {"write": {"path": "test_calc.py", "content": TEST_MUL}},
            {"commit": "C1: mul (glm)"}, {"say": "DONE"}]]
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, QUOTA_ERR], glm=glm, pinned=True,
              available=("kiro", "agy", "claude", "glm"))
    room = env.run()
    assert room.status == "done", room.error
    kinds = [e["kind"] for e in env.events()]
    assert "quota" in kinds
    fb = [e for e in env.events() if e["kind"] == "assign" and e["by"] == "fallback"]
    assert fb and fb[0]["seat"] == "glm"
    assert room.fallback_from == "kiro" and room.impl_seat == "glm" and room.impl_by == "fallback"
    with open(env.quota.path, encoding="utf-8") as f:
        assert json.load(f)["kiro"]["state"] == "exhausted"
    assert not env.items("question", resolved=True)
    assert not any(room.attempts.values())  # 用量備援不計入修正次數
    assert "C1: mul (glm)" in git(room.worktree, "log", "--format=%s")
    env.no_secrets()


def test_restore_at_phase_boundary(tmp_path, repo):
    glm = [[{"write": {"path": DOCS + "/plan.md", "content": "# 計畫\n1. mul（對應 C1）\n"}},
            {"commit": "plan (glm)"}, {"say": "DONE"}]]

    def recover(env):
        if env.quota.is_exhausted("kiro", None):
            env.quota.mark_ok("kiro")

    env = Env(tmp_path, repo, [SPEC_TURN, QUOTA_ERR, BUILD_TURN], glm=glm, pinned=True,
              available=("kiro", "agy", "claude", "glm"), on_change=recover)
    room = env.run()
    assert room.status == "done", room.error
    ev = env.events()
    restore = [i for i, e in enumerate(ev) if e["kind"] == "restore"]
    assert restore
    after = [e for e in ev[restore[0]:] if e["kind"] == "assign"]
    assert after[0]["seat"] == "kiro" and after[0]["role"] == "impl"
    assert room.fallback_from == "" and room.impl_seat == "kiro"


def test_rollover_at_70_percent(tmp_path, repo):
    plan = PLAN_TURN[:-1] + [{"usage": {"used": 150000, "size": 200000}}, {"say": "DONE"}]
    save = [{"write": {"path": DOCS + "/progress.md", "content": PROGRESS}},
            {"say": "DONE"}]
    build = [{"echo_prompt": True}] + BUILD_TURN
    env = Env(tmp_path, repo, [SPEC_TURN, plan, save, build])
    room = env.run()
    assert room.status == "done", room.error
    ev = env.events()
    ro = [i for i, e in enumerate(ev) if e["kind"] == "rollover"]
    assert len(ro) == 1 and ev[ro[0]]["pct"] == 75.0
    first_prompt = [e for e in ev[ro[0]:] if e["kind"] == "prompt"][0]
    assert "progress.md" in first_prompt["text"]
    assert any("progress.md" in e["text"] for e in ev[ro[0]:] if e["kind"] == "agent")
    assert room.rollovers == 1
    assert "## 下一步" in open(os.path.join(room.notes_dir, "progress.md"), encoding="utf-8").read()
    assert not any("代寫" in t for t in env.texts())


def test_rollover_writes_progress_when_agent_doesnt(tmp_path, repo):
    plan = PLAN_TURN[:-1] + [{"usage": {"used": 150000, "size": 200000}}, {"say": "DONE"}]
    env = Env(tmp_path, repo, [SPEC_TURN, plan, [{"say": "好，存好了 DONE"}], BUILD_TURN])
    room = env.run()
    assert room.status == "done", room.error
    assert any("代寫" in t for t in env.texts())
    text = open(os.path.join(room.notes_dir, "progress.md"), encoding="utf-8").read()
    for s in SECTIONS:
        assert "## " + s in text
    assert "ai-team: progress" not in git(room.worktree, "log", "--format=%s")


def test_context_fraction():
    class D:
        last_usage = None
        chars = 384000
    assert context_fraction(D(), 128000) == (1.0, True)
    d = D()
    d.last_usage = (50, 200)
    assert context_fraction(d, 128000) == (0.25, False)


class JsonWriter(ScriptedRouter):
    def write(self, prompt, effort="low"):
        self.efforts = getattr(self, "efforts", []) + [effort]
        return '```json\n{"blocking":[],"suggestions":["模型建議"]}\n```'


def test_review_falls_back_to_model_when_cli_unavailable(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    env.runner.cmd_overrides["agy"] = [PY, "-c", "import sys; sys.exit(3)"]
    env.runner.router = env.router = JsonWriter()
    room = env.run()
    assert room.status == "done", room.error
    # 審查座位不可用 → 一般思考量審查一次；其餘是角色說明（PO 補充、Reviewer 審查重點，低思考量）
    assert env.router.efforts.count("normal") == 1 and set(env.router.efforts) == {"normal", "low"}
    assert any("改用" in t and "審查" in t for t in env.texts("notice"))
    ev = open(os.path.join(room.worktree, DOCS, "evidence.md"), encoding="utf-8").read()
    assert "模型建議" in ev


def test_unverified_goes_to_user(tmp_path, repo):
    spec = dict(SPEC, criteria=[{"id": "C1", "text": "補 add 的測試", "check": "manual"}])
    spec_turn = [{"say": "```json\n%s\n```" % json.dumps(spec, ensure_ascii=False)}]
    tests_only = [{"write": {"path": "test_calc.py",
                             "content": TEST_ADD + "\n\ndef test_add_neg():\n    assert add(-1, 1) == 0\n"}},
                  {"commit": "C1: more tests"}, {"say": "DONE"}]
    env = Env(tmp_path, repo, [spec_turn, spec_turn, spec_turn, PLAN_TURN, tests_only])
    env.runner.start()
    assert env.wait(lambda: env.items("unverified") and env.get().status == "waiting_user")
    # 沒有可執行檢查 → 先用 revise_spec 要求補上（2 次），仍沒有就保留並標「未自動驗證」
    revs = [e for e in env.events() if e["kind"] == "prompt" and "你提出的驗收條件有以下問題" in e["text"]]
    assert len(revs) == 2
    assert "未自動驗證" in open(os.path.join(env.get().worktree, DOCS, "spec.md"), encoding="utf-8").read()
    item = env.items("unverified")[0]
    assert [o["id"] for o in item.options] == ["accept", "pause"]
    assert "沒有原始碼改動" in env.get().unverified
    assert env.runner.answer(item.id, "accept") is True
    env.runner.join(60)
    room = env.get()
    assert room.status == "done", room.error
    assert room.unverified == "" and room.attempts.get("test", 0) == 0
    assert any("使用者確認測試有效" in t for t in env.texts())
    ev = open(os.path.join(room.worktree, DOCS, "evidence.md"), encoding="utf-8").read()
    assert "使用者確認測試有效" in ev and "未自動驗證" in ev
    assert "未自動驗證：C1" in env.items("merge")[0].detail


def test_pause_during_slow_test_cmd(tmp_path, repo):
    pids = tmp_path / "pids.txt"
    slow = ('"%s" -c "import os,time; t=\'mul\' in open(\'calc.py\').read(); '
            'open(r\'%s\',\'a\').write(str(os.getpid())+chr(10)); time.sleep(0 if t else 8)"' % (PY, pids))
    spec = dict(SPEC, test_cmd=slow)
    spec_turn = [{"say": "```json\n%s\n```" % json.dumps(spec, ensure_ascii=False)}]
    env = Env(tmp_path, repo, [spec_turn, PLAN_TURN, BUILD_TURN])
    mut = tmp_path / ".ai-team-worktrees" / ".ai-team-mutation"
    env.runner.start()
    # 測試指令以 python -c 開頭：積極模式也要你確認
    assert env.wait(lambda: env.items("approve_spec"))
    assert env.runner.answer(env.items("approve_spec")[0].id, "start", "") is True
    # 換回原狀的副本裡沒有 mul → 測試指令睡 8 秒
    assert env.wait(lambda: mut.exists() and pids.exists() and len(pids.read_text().split()) >= 2, 40)
    time.sleep(0.3)
    t0 = time.time()
    env.runner.pause()
    assert env.get().activity == "暫停中…"
    env.runner.join(3)
    assert not env.runner.is_alive() and time.time() - t0 < 3
    assert env.get().status == "paused"
    assert not mut.exists()
    time.sleep(0.2)
    for pid in pids.read_text().split():
        stat = "/proc/%s/stat" % pid
        assert not os.path.exists(stat) or open(stat).read().rsplit(")", 1)[1].split()[0] == "Z"


def test_review_blocking_goes_back_to_build(tmp_path, repo):
    review = [[{"say": '```json\n{"blocking":["缺少負數的測試"],"suggestions":[]}\n```'}], REVIEW_OK[0]]
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN], review=review)
    room = env.run()
    assert room.status == "done", room.error
    assert room.attempts.get("review") == 1 and room.pending_fix == ""
    ev = env.events()
    i = [k for k, e in enumerate(ev) if e["kind"] == "say" and e.get("role") == "reviewer" and "缺少負數的測試" in e["text"]]
    assert i
    assert [e["phase"] for e in ev[i[0]:] if e["kind"] == "phase"][:3] == ["build", "test", "review"]
    fixes = [e for e in ev[i[0]:] if e["kind"] == "prompt" and e.get("role") == "scrum"]
    assert fixes and fixes[0]["text"].startswith("【Reviewer】")


def test_pause_during_permission_wait(tmp_path, repo):
    build = BUILD_TURN[:-1] + [{"tool": {"title": "清 build", "kind": "execute",
                                         "rawInput": {"command": "rm -rf build"}}}, {"say": "DONE"}]
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, build])
    env.runner.start()
    assert env.wait(lambda: env.items("permission") and env.get().status == "waiting_user")
    item = env.items("permission")[0]
    env.runner.pause()
    env.runner.join(5)
    assert not env.runner.is_alive()
    assert env.get().status == "paused"
    assert env.wait(lambda: env.items("permission", resolved=True)[0].resolved, 3)
    it = env.items("permission", resolved=True)[0]
    assert it.answer == "reject" and "暫停" in it.text
    assert any(t.startswith("暫停時自動拒絕") for t in env.texts("permission"))
    assert env.runner.answer(item.id, "allow") is False


def test_glm_also_failing_sets_error(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, QUOTA_ERR], glm=[[{"error": "連線中斷"}]], pinned=True,
              available=("kiro", "agy", "claude", "glm"))
    room = env.run()
    assert room.status == "error" and "GLM" in room.error and "連線中斷" in room.error
    assert len(env.items("error")) == 1


def test_no_seat_left_pauses_with_question(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, QUOTA_ERR], pinned=True, available=("kiro", "agy"))
    room = env.run()
    assert room.status == "paused" and not room.error
    q = env.items("question")
    assert len(q) == 1 and q[0].title == "所有座位都沒有額度，等額度恢復或手動處理"
    assert [o["id"] for o in q[0].options] == ["retry", "pause"]
    assert not env.items("error")


def test_send_reaches_next_impl_turn_signed(tmp_path, repo):
    spec = [{"echo_prompt": True}] + SPEC_TURN
    env = Env(tmp_path, repo, [spec, PLAN_TURN, BUILD_TURN])
    env.runner.send("請用中文寫註解")
    room = env.run()
    assert room.status == "done", room.error
    agent = [e["text"] for e in env.events() if e["kind"] == "agent"]
    assert any("【你】請用中文寫註解" in t for t in agent)
    # 只送一次
    prompts = "\n".join(t for t in agent)
    assert prompts.count("【你】請用中文寫註解") == 1


# ---- Task 17：模型參與、版本與用量 ----

def _attach(env):
    from team.usage import UsageBook, attach
    env.book = UsageBook(None)
    attach(env.router, env.store, env.book)
    env.runner.usage = env.book


def test_model_calls_and_cli_usage_are_visible(tmp_path, repo):
    glm_turns = [SPEC_TURN, PLAN_TURN, [s for s in BUILD_TURN if "tool" not in s]]
    env = Env(tmp_path, repo, [[{"say": "unused"}]], glm=glm_turns, available=("glm", "agy"))
    _attach(env)
    room = env.run()
    assert room.status == "done", room.error
    ev = env.events()
    models = [e for e in ev if e["kind"] == "model"]
    # 每筆模型呼叫都標了角色、目的、模型與 token 狀態（示範假模型：標示範、不計 token）
    roles = {e["role"] for e in models}
    assert {"dispatcher", "qa", "po", "reviewer"} <= roles
    assert all(e["model"] == "scripted" and e["tokens"] == "demo" for e in models)
    purposes = {e["purpose"] for e in models}
    assert {"派工：評估任務難度", "檢查驗收條件品質", "PO 補充說明", "Reviewer 審查重點"} <= purposes
    assert any("0.90 ≥ 0.60 通過" in e["text"] for e in models if e["purpose"] == "檢查驗收條件品質")
    # glm 寫的角色說明附在範本後，並以角色發言出現在時間軸
    assert any(e["kind"] == "say" and e.get("role") == "po" and e.get("note") == "model" for e in ev)
    # 派工理由有具體分數
    a = [e for e in ev if e["kind"] == "assign" and e["role"] == "impl"][0]
    assert a["seat"] == "glm" and "GLM（一般） 0.90−成本 0.00＝0.90" in a["reason"] and "→ 選 GLM（一般）" in a["reason"]
    assert room.impl_reason == a["reason"]
    # CLI 用量：GLM 座位（headless）回報模型與 token；agy（ACP）沒回報 → None
    cli = [e for e in ev if e["kind"] == "cli_usage"]
    impl = [e for e in cli if e["role"] == "impl"]
    assert impl and impl[0]["model"] == "glm-5.3" and impl[0]["tokens_in"] == 2400 and impl[0]["tokens_out"] == 50
    assert impl[-1]["total_in"] == 2400 * len(impl)
    rev = [e for e in cli if e["role"] == "review"]
    assert rev and rev[0]["tokens_in"] is None and "CLI 預設（未回報）" in rev[0]["text"]
    assert room.impl_model_actual == "glm-5.3" and room.review_model_actual == ""
    assert room.tokens["glm"]["in"] == 2400 * len(impl) and room.tokens["glm"]["cost"] == pytest.approx(0.0123 * len(impl))
    assert room.tokens["agy"]["in"] is None and room.tokens["agy"]["turns"] == 1
    s = env.book.summary({})["session"]
    assert {c["seat"] for c in s["cli"]} == {"glm", "agy"}
    assert sum(m["calls"] for m in s["models"]) == len(models)
    env.no_secrets()


class NoGlm(ScriptedRouter):
    def write(self, prompt, effort="low", ctx=None):
        raise RuntimeError("glm down")


def test_role_notes_fall_back_to_template(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    env.runner.router = env.router = NoGlm()
    room = env.run()
    assert room.status == "done", room.error
    says = [e["text"] for e in env.events() if e["kind"] == "say"]
    assert any(t.startswith("【PO】PO 補充說明（模型不可用，用範本）") for t in says)
    assert any("Reviewer 審查重點（模型不可用，用範本）" in t for t in says)


def test_permission_event_has_numbers_and_model(tmp_path, repo):
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    room = env.run()
    assert room.status == "done", room.error
    p = [e for e in env.events() if e["kind"] == "permission" and e.get("outcome") == "allow"][0]
    assert "allow 0.95 risk 0.05" in p["text"] and p["model"] == "示範（假模型）" and p["risk"] == 0.05


# ---- 最終審查第 1 項：寫入座位放行房間的測試指令 ----
def test_open_passes_test_runner_pattern(tmp_path, repo, monkeypatch):
    import types
    import team.runner as runner_mod
    env = Env(tmp_path, repo, [[{"say": "DONE"}]])
    got = []

    class Drv:
        def __init__(self, kw):
            self.allowed = list(kw.get("allowed_tools") or [])

        def start(self):
            pass

        def set_allowed_tools(self, tools):
            self.allowed = list(tools)

    def fake_make(cli, cwd, on_event, on_perm, read_only, **kw):
        d = Drv(kw)
        got.append((read_only, d))
        return d

    monkeypatch.setattr(runner_mod, "make_driver", fake_make)
    r = env.runner
    r._wt = types.SimpleNamespace(path=str(repo))
    r._set(test_cmd="python -m pytest -q")
    w = r._open("glm", "", read_only=False)
    ro = r._open("claude", "", read_only=True)
    assert w.allowed == ["Bash(python -m pytest:*)"]
    assert ro.allowed == []
    # 測試指令定案（_store_spec）後同步到進行中的實作座位
    r._impl = w
    r._set(test_cmd="npm test")
    r._sync_test_tools()
    assert w.allowed == ["Bash(npm test:*)"]


# ---- 最終審查第 3 項：危險的驗收指令在積極模式也要使用者確認 ----
def test_denied_check_forces_spec_approval_in_bold(tmp_path, repo):
    bad = dict(SPEC, criteria=[{"id": "C1", "text": "mul", "check": "rm -rf build && " + SPEC["criteria"][0]["check"]}])
    turn = [{"say": "```json\n%s\n```" % json.dumps(bad, ensure_ascii=False)}]
    env = Env(tmp_path, repo, [turn, [{"say": "稍等"}]], autonomy="bold")
    env.runner.start()
    try:
        assert env.wait(lambda: env.items("approve_spec"))
        item = env.items("approve_spec")[0]
        assert "驗收指令命中危險規則：rm\\s+-rf（rm -rf build" in item.detail
        assert env.wait(lambda: env.get().status == "waiting_user")  # 收件匣項目先建立、狀態隨後更新
        assert "積極模式：驗收條件自動採用，直接開工" not in env.texts()
    finally:
        env.runner.pause()
        env.runner.join(10)


def test_git_in_check_forces_spec_approval(tmp_path, repo):
    bad = dict(SPEC, criteria=[{"id": "C1", "text": "mul", "check": "git stash && " + SPEC["criteria"][0]["check"]}])
    turn = [{"say": "```json\n%s\n```" % json.dumps(bad, ensure_ascii=False)}]
    env = Env(tmp_path, repo, [turn, [{"say": "稍等"}]], autonomy="bold")
    env.runner.start()
    try:
        assert env.wait(lambda: env.items("approve_spec"))
        assert "驗收檢查有問題" in env.items("approve_spec")[0].detail
    finally:
        env.runner.pause()
        env.runner.join(10)


# ---- 最終審查第 5 項：額度判斷不看代理的一般敘述 ----
class _CrashDrv:
    def __init__(self, runner, said, err, error_event=""):
        self.runner, self.said, self.err, self.error_event = runner, said, err, error_event

    def prompt(self, text, timeout):
        from team.acp import AcpError
        from team.events import AgentEvent
        self.runner._on_event("Kiro", AgentEvent("message", self.said, {}))
        if self.error_event:
            self.runner._on_event("Kiro", AgentEvent("error", self.error_event, {}))
            return "error"
        raise AcpError(self.err)

    def close(self):
        pass


def _turn_failure(env, drv):
    from team.runner import _TurnFailed
    with pytest.raises(_TurnFailed) as ei:
        env.runner._run_turn(drv, "Kiro", "go")
    return ei.value


def test_agent_prose_with_usage_word_is_not_quota(tmp_path, repo):
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    e = _turn_failure(env, _CrashDrv(env.runner, "我會降低記憶體用量，並減少 credits 的浪費", "程序異常結束（exit 139）"))
    assert e.quota is False
    e2 = _turn_failure(env, _CrashDrv(env.runner, "降低記憶體用量", "", error_event="Segmentation fault"))
    assert e2.quota is False


def test_quota_still_detected_from_driver_error_or_explicit_phrase(tmp_path, repo):
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    assert _turn_failure(env, _CrashDrv(env.runner, "好", "You've hit your usage limit")).quota is True
    assert _turn_failure(env, _CrashDrv(env.runner, "Claude usage limit reached. resets at 3pm",
                                        "exit 1")).quota is True
    assert _turn_failure(env, _CrashDrv(env.runner, "本月額度已用完", "exit 1")).quota is True


# ---- 最終審查第 6 項：純 TLS／網路錯誤走網路與憑證說明 ----
TLS = "dispatch failure (InvalidCertificate(UnknownIssuer))"


def _net_spy(r):
    calls = {"net": [], "login": []}
    r._network_problem = lambda seat, text, why: calls["net"].append((seat, why)) or "retry"
    r._login_blocked = lambda seat, text: calls["login"].append(seat) or "retry"
    return calls


def test_access_blocked_routes_network_and_login(tmp_path, repo):
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    r = env.runner
    calls = _net_spy(r)
    assert r._access_blocked("kiro", TLS) == "retry"
    assert r._access_blocked("glm", "error sending request: dispatch failure") == "retry"
    assert calls["net"] == [("kiro", "network"), ("glm", "network")] and calls["login"] == []
    assert r._access_blocked("kiro", "Segmentation fault") is None
    assert r._access_blocked("kiro", "not logged in") == "retry" and calls["login"] == ["kiro"]


class _OkDrv:
    def start(self):
        pass

    def close(self):
        pass

    def cancel(self):
        pass


def _flaky_open(r, raw):
    from team.runner import _StartFailed
    state = {"n": 0}

    def opener(seat, model, read_only):
        state["n"] += 1
        if state["n"] == 1:
            raise _StartFailed("無法啟動 %s：%s" % (seat, raw), raw=raw)
        return _OkDrv()
    r._open = opener
    return state


def test_switch_impl_start_failed_tls_asks_network_and_retries(tmp_path, repo):
    import types
    from team.fleet import Assignment
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    r = env.runner
    r._wt = types.SimpleNamespace(path=str(repo), base="HEAD")
    calls = _net_spy(r)
    state = _flaky_open(r, TLS)
    r._switch_impl(Assignment("kiro", "", "standard", "user", "測試"), "測試")
    assert state["n"] == 2 and calls["net"] == [("kiro", "network")]
    assert isinstance(r._impl, _OkDrv)


def test_rollover_restart_tls_failure_asks_network(tmp_path, repo, monkeypatch):
    import types
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    r = env.runner
    r._wt = types.SimpleNamespace(path=str(repo), base="HEAD")
    r._set(impl_seat="kiro", context_pct=90.0)
    monkeypatch.setattr(r, "_progress_ok", lambda before: True)
    monkeypatch.setattr(r, "_progress_blob", lambda: "")
    calls = _net_spy(r)
    state = _flaky_open(r, "error sending request: dispatch failure")
    r._rollover()
    assert state["n"] == 2 and calls["net"] == [("kiro", "network")]
    assert isinstance(r._impl, _OkDrv) and env.get().rollovers == 1


# ---- 最終審查 minor：助理的文件提交不簽章、不跑 hook ----
def test_commit_docs_skips_hooks_and_gpgsign(tmp_path, repo):
    import types
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    r = env.runner
    r._wt = types.SimpleNamespace(path=str(repo), base="HEAD", repo=str(repo), branch="ai-team/add-mul")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "false")
    legacy = repo / "docs" / "ai-team" / "x" / "spec.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# x\n", encoding="utf-8")
    r._commit_docs("docs/ai-team/x", "ai-team: spec")
    log = subprocess.run(["git", "log", "-1", "--format=%an %s"], cwd=repo, capture_output=True, text=True).stdout
    assert log.strip() == "ai-team ai-team: spec"


# ---- 允許清單：直譯器開頭的測試指令要確認；不在清單只提醒 ----
def _spec_room(tmp_path, repo, test_cmd):
    import types
    env = Env(tmp_path, repo, [[{"say": "x"}]])
    r = env.runner
    r._set(test_cmd=test_cmd, criteria=[])
    return env, r


@pytest.mark.parametrize("cmd", ["python -c \"print(1)\"", "node run-tests.js", "bash t.sh", "sh -c pytest",
                                 "powershell -File t.ps1", "cmd /c t.bat", "npx jest"])
def test_interpreter_test_cmd_forces_approval(tmp_path, repo, cmd):
    _, r = _spec_room(tmp_path, repo, cmd)
    alerts = r._spec_alerts()
    assert alerts and "直譯器" in alerts[0]


def test_git_test_cmd_forces_approval(tmp_path, repo):
    _, r = _spec_room(tmp_path, repo, "git stash && pytest")
    assert any("驗收測試指令有問題" in a for a in r._spec_alerts())


def test_non_allowlisted_test_cmd_is_only_a_note(tmp_path, repo):
    _, r = _spec_room(tmp_path, repo, "make test")
    assert r._spec_alerts() == []
    assert r._spec_notes() == ["測試指令不在允許清單，代理不能自己執行（助理仍會在關卡執行）：make test"]
    _, r2 = _spec_room(tmp_path / "b", repo, "python -m pytest -q")
    assert r2._spec_alerts() == [] and r2._spec_notes() == []
