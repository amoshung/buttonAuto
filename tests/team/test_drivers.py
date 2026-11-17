import json
import os
import sys
import threading
import time
from dataclasses import dataclass

import pytest

from team.acp import AcpClient, AcpError
from team.clis import CliInfo
from team.drivers import make_driver, seat_launch
from team.headless import ALLOWED, DISALLOWED, HeadlessClaude, runner_tool_pattern
from team.secrets import SecretEnv, merge_env

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")
FAKE_CLAUDE = os.path.join(ROOT, "team", "fakes", "fake_claude.py")
BASE = [sys.executable, FAKE_CLAUDE]
SECRET = "sk-very-secret-glm-key-123"


@dataclass
class Cfg:
    base_url: str = "http://litellm.local:4000"
    glm_key: str = SECRET
    glm_model: str = "glm-5.3"


def _clis():
    return {
        "kiro": CliInfo("kiro", "Kiro", "/x/kiro-cli", "acp"),
        "agy": CliInfo("agy", "Antigravity", "/x/agy", "acp"),
        "claude": CliInfo("claude", "Claude", "/x/claude", "headless"),
    }


# ---- HeadlessClaude ----

def test_headless_build_cmd(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=False)
    cmd = h.build_cmd()
    assert cmd[:len(BASE)] == BASE
    assert cmd[len(BASE):len(BASE) + 6] == ["-p", "--output-format", "stream-json",
                                            "--verbose", "--permission-mode", "acceptEdits"]
    assert "做事" not in cmd  # 提示詞走 stdin
    assert cmd[-len(DISALLOWED):] == DISALLOWED
    assert "--resume" not in cmd
    assert h.prompt("一") == "end_turn"
    sid = h.session_id
    assert sid
    assert h.prompt("二") == "end_turn"
    cmd2 = h.build_cmd()
    i = cmd2.index("--resume")
    assert cmd2[i + 1] == sid
    assert cmd2[-len(DISALLOWED):] == DISALLOWED

    ro = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True)
    assert "plan" in ro.build_cmd()
    assert "acceptEdits" not in ro.build_cmd()


def test_headless_extra_args_after_disallowed(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=False,
                       extra_args=["--model", "glm-5.3"])
    cmd = h.build_cmd()
    assert cmd[-2:] == ["--model", "glm-5.3"]
    assert cmd[-2 - len(DISALLOWED):-2] == DISALLOWED


def test_headless_parses_stream(tmp_path):
    events = []
    h = HeadlessClaude(BASE, str(tmp_path), events.append, read_only=False)
    assert h.prompt("寫檔") == "end_turn"
    kinds = [e.kind for e in events]
    assert "message" in kinds and "tool" in kinds
    assert kinds.index("message") < kinds.index("tool")
    assert any("寫檔" in e.text for e in events if e.kind == "message")
    tool = [e for e in events if e.kind == "tool"][0]
    assert tool.text == "Write"
    assert (tmp_path / "claude_was_here.txt").exists()

    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    ro = HeadlessClaude(BASE, str(ro_dir), lambda e: None, read_only=True)
    assert ro.prompt("看看") == "end_turn"
    assert not (ro_dir / "claude_was_here.txt").exists()


def test_headless_is_error_result(tmp_path):
    events = []
    h = HeadlessClaude(BASE, str(tmp_path), events.append, read_only=True,
                       env={"FAKE_CLAUDE_IS_ERROR": "1"})
    assert h.prompt("x") == "error"
    assert any(e.kind == "error" for e in events)


def test_headless_nonzero_exit_raises(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                       env={"FAKE_CLAUDE_FAIL": "1", "SOME_TOKEN": SECRET})
    with pytest.raises(AcpError) as ei:
        h.prompt("x")
    msg = str(ei.value)
    assert "exit 3" in msg
    assert "not logged in" in msg
    assert SECRET not in msg


def test_headless_timeout_kills(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                       env={"FAKE_CLAUDE_SLEEP": "20"})
    t0 = time.time()
    with pytest.raises(AcpError) as ei:
        h.prompt("x", timeout=1)
    assert "1 秒" in str(ei.value)
    assert time.time() - t0 < 5


def test_headless_cancel(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                       env={"FAKE_CLAUDE_SLEEP": "20"})
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", h.prompt("x")))
    t0 = time.time()
    th.start()
    time.sleep(0.5)
    h.cancel()
    th.join(10)
    assert out.get("r") == "cancelled"
    assert time.time() - t0 < 5


def test_headless_env_passed(tmp_path):
    events = []
    h = HeadlessClaude(BASE, str(tmp_path), events.append, read_only=True,
                       env={"ANTHROPIC_MODEL": "glm-5.3", "ANTHROPIC_AUTH_TOKEN": SECRET})
    assert h.prompt("hi") == "end_turn"
    assert any("glm-5.3" in e.text for e in events if e.kind == "message")
    assert SECRET not in json.dumps([[e.kind, e.text, e.data] for e in events],
                                    ensure_ascii=False)
    assert SECRET not in repr(h)


def test_headless_usage_from_stream(tmp_path):
    events = []
    h = HeadlessClaude(BASE, str(tmp_path), events.append, read_only=True)
    assert h.last_usage is None
    h.prompt("hi")
    usage = [e for e in events if e.kind == "usage"]
    assert len(usage) == 1
    # 假 claude：input 100 + cache_read 2000 + cache_creation 300；contextWindow 200000
    assert usage[0].data == {"used": 2400, "size": 200000}
    assert h.last_usage == (2400, 200000)
    assert h.chars > len("hi")


# ---- make_driver ----

def test_make_driver_acp_uses_fake_agent(tmp_path):
    sp = tmp_path / "script.json"
    sp.write_text(json.dumps([[{"say": "你好"}]]), encoding="utf-8")
    events = []
    d = make_driver(CliInfo("kiro", "Kiro", "x", "acp"), str(tmp_path), events.append,
                    lambda r: "allow-once", read_only=False,
                    cmd_override=[sys.executable, AGENT, str(sp)])
    try:
        assert d.label == "Kiro"
        d.start()
        assert d.prompt("hi") == "end_turn"
        assert [e.text for e in events if e.kind == "message"] == ["你好"]
        assert d.chars >= len("hi") + len("你好")
        assert d.last_usage is None
    finally:
        d.close()


def test_make_driver_acp_model_failure_is_nonfatal(tmp_path):
    sp = tmp_path / "script.json"
    sp.write_text(json.dumps([[{"say": "ok"}]]), encoding="utf-8")
    events = []
    d = make_driver(CliInfo("agy", "Antigravity", "x", "acp"), str(tmp_path), events.append,
                    lambda r: "", read_only=False,
                    cmd_override=[sys.executable, AGENT, str(sp)], model="glm-5.3")
    try:
        d.start()  # 假代理不支援 set_model：只記事件
        notes = [e for e in events if "glm-5.3" in e.text]
        assert notes and all(e.kind == "notice" for e in notes)
        assert not any(e.kind == "error" for e in events)
        assert d.prompt("hi") == "end_turn"
    finally:
        d.close()


def test_make_driver_headless(tmp_path):
    events = []
    d = make_driver(CliInfo("claude", "Claude", "x", "headless"), str(tmp_path),
                    events.append, lambda r: "", read_only=False, cmd_override=BASE,
                    env={"ANTHROPIC_MODEL": "glm-5.3"}, extra_args=["--x"], model="glm-5.3")
    try:
        assert d.label == "Claude"
        d.start()
        cmd = d.claude.build_cmd()
        assert cmd[-3:] == ["--x", "--model", "glm-5.3"]
        assert d.prompt("hi") == "end_turn"
        assert any("glm-5.3" in e.text for e in events if e.kind == "message")
        assert d.last_usage == (2400, 200000)
        assert d.chars > 0
        assert (tmp_path / "claude_was_here.txt").exists()
    finally:
        d.close()


# ---- usage（ACP）----

def test_acp_usage_update_event(tmp_path):
    sp = tmp_path / "script.json"
    sp.write_text(json.dumps([[{"usage": {"used": 1234, "size": 128000}}, {"say": "x"}]]),
                  encoding="utf-8")
    events = []
    c = AcpClient([sys.executable, AGENT, str(sp)], str(tmp_path), events.append,
                  lambda r: "")
    try:
        c.start()
        c.new_session()
        assert c.last_usage is None
        c.prompt("go")
        u = [e for e in events if e.kind == "usage"]
        assert len(u) == 1 and u[0].data == {"used": 1234, "size": 128000}
        assert c.last_usage == (1234, 128000)
    finally:
        c.close()


# ---- seat_launch ----

def test_seat_launch_glm_env(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    clis = _clis()
    cli, env, extra = seat_launch("glm", clis, Cfg(), [], str(tmp_path), str(tmp_path / "t"))
    assert cli == clis["claude"]
    assert env["ANTHROPIC_BASE_URL"] == "http://litellm.local:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert env["ANTHROPIC_MODEL"] == "glm-5.3"
    assert "MAX_THINKING_TOKENS" not in env
    assert SECRET not in repr(env)
    assert isinstance(env.copy(), SecretEnv)
    assert SECRET not in repr(env.copy())
    assert SECRET not in repr((cli, env, extra))
    assert SECRET not in str(env)

    _, env_h, _ = seat_launch("glm_high", clis, Cfg(), [], str(tmp_path), str(tmp_path / "t"))
    assert env_h["MAX_THINKING_TOKENS"] == "16000"
    assert env_h["ANTHROPIC_MODEL"] == "glm-5.3"

    with pytest.raises(ValueError) as ei:
        seat_launch("glm", clis, None, [], str(tmp_path), str(tmp_path / "t"))
    assert "litellm.local.json" in str(ei.value)

    # 金鑰不會出現在事件裡
    events = []
    d = make_driver(cli, str(tmp_path), events.append, lambda r: "", read_only=True,
                    cmd_override=BASE, env=env, extra_args=extra)
    d.prompt("hi")
    assert any("glm-5.3" in e.text for e in events)
    assert SECRET not in json.dumps([[e.kind, e.text, e.data] for e in events],
                                    ensure_ascii=False)
    assert SECRET not in repr(d)


def test_seat_launch_plugins_and_serena(tmp_path, monkeypatch):
    clis = _clis()
    plug = tmp_path / "ce"
    plug.mkdir()
    missing = str(tmp_path / "nope")
    wt = str(tmp_path / "wt @中文")
    td = tmp_path / "tmp"

    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    cli, env, extra = seat_launch("claude", clis, None, [str(plug), missing], wt, str(td))
    assert cli == clis["claude"]
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert extra == ["--plugin-dir", str(plug)]
    assert not (td / "serena.mcp.json").exists()

    monkeypatch.setattr("shutil.which",
                        lambda name, *a, **k: "/usr/bin/uvx" if name == "uvx" else None)
    _, _, extra = seat_launch("glm", clis, Cfg(), [str(plug)], wt, str(td))
    mcp = td / "serena.mcp.json"
    assert mcp.exists()
    assert extra == ["--plugin-dir", str(plug), "--mcp-config", str(mcp)]
    conf = json.loads(mcp.read_text(encoding="utf-8"))
    srv = conf["mcpServers"]["serena"]
    assert srv["command"] == "/usr/bin/uvx"
    assert srv["args"] == ["--from", "git+https://github.com/oraios/serena", "serena",
                           "start-mcp-server", "--context", "claude-code", "--project", wt]

    # kiro／agy 不注入
    for seat in ("kiro", "agy"):
        c, e, x = seat_launch(seat, clis, Cfg(), [str(plug)], wt, str(td))
        assert c == clis[seat] and dict(e) == {} and x == []

    with pytest.raises(ValueError):
        seat_launch("bogus", clis, Cfg(), [], wt, str(td))


def test_seat_launch_disables_autocompact(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    clis = _clis()
    for seat in ("claude", "glm", "glm_high"):
        _, env, _ = seat_launch(seat, clis, Cfg(), [], str(tmp_path), str(tmp_path))
        assert env["DISABLE_AUTO_COMPACT"] == "1"
    for seat in ("kiro", "agy"):
        _, env, _ = seat_launch(seat, clis, Cfg(), [], str(tmp_path), str(tmp_path))
        assert "DISABLE_AUTO_COMPACT" not in env


# ---- 加固（fix round 1）----

def test_headless_exit_zero_without_result_raises(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                       env={"FAKE_CLAUDE_NO_RESULT": "1"})
    with pytest.raises(AcpError) as ei:
        h.prompt("x")
    assert "沒有回傳結果就結束了" in str(ei.value)
    assert "exit 0" in str(ei.value)


def test_headless_result_wins_over_timeout(tmp_path):
    # result 已送出後程序才拖著不結束：逾時不應蓋掉已拿到的結果
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                       env={"FAKE_CLAUDE_LINGER": "3"})
    t0 = time.time()
    assert h.prompt("x", timeout=1) == "end_turn"
    assert time.time() - t0 < 8


def test_headless_idle_cancel_is_noop(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=False)
    h.cancel()  # 沒有回合在跑：不做事
    assert h.prompt("x") == "end_turn"
    assert (tmp_path / "claude_was_here.txt").exists()


def test_headless_env_masked_in_vars(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                       env={"ANTHROPIC_AUTH_TOKEN": SECRET})
    assert SECRET not in repr(vars(h))
    assert SECRET not in repr(h)


def test_merge_env_removes_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic")
    out = merge_env({"ANTHROPIC_API_KEY": None, "A": "1"})
    assert "ANTHROPIC_API_KEY" not in out and out["A"] == "1"


def test_seat_launch_glm_strips_anthropic_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-anthropic-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "real-oauth-token")
    clis = _clis()
    for seat in ("glm", "glm_high"):
        _, env, _ = seat_launch(seat, clis, Cfg(), [], str(tmp_path), str(tmp_path))
        child = merge_env(env)
        assert "ANTHROPIC_API_KEY" not in child
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in child
        assert child["ANTHROPIC_AUTH_TOKEN"] == SECRET
    # 子程序真的看不到
    _, env, _ = seat_launch("glm", clis, Cfg(), [], str(tmp_path), str(tmp_path))
    events = []
    h = HeadlessClaude(BASE, str(tmp_path), events.append, read_only=True,
                       env=dict(env, FAKE_CLAUDE_SHOW_ENV="ANTHROPIC_API_KEY,CLAUDE_CODE_OAUTH_TOKEN"))
    h.prompt("x")
    shown = " ".join(e.text for e in events if e.kind == "message")
    assert "ANTHROPIC_API_KEY=<unset>" in shown
    assert "CLAUDE_CODE_OAUTH_TOKEN=<unset>" in shown
    # 自己的 claude 座位保留登入
    _, env_c, _ = seat_launch("claude", clis, None, [], str(tmp_path), str(tmp_path))
    assert merge_env(env_c).get("ANTHROPIC_API_KEY") == "real-anthropic-key"


def test_fake_claude_script_mode(tmp_path):
    import subprocess as sp
    repo = tmp_path / "r"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "--allow-empty", "-m", "i"],
           cwd=repo, check=True)
    script = tmp_path / "glm.json"
    script.write_text(json.dumps([
        [{"say": "第一回"}, {"write": {"path": "a.txt", "content": "x"}}, {"commit": "add a"}, {"say": "DONE"}],
        [{"error": "You've hit your usage limit"}],
    ]), encoding="utf-8")
    events = []
    h = HeadlessClaude(BASE + ["--script", str(script)], str(repo), events.append, read_only=False)
    assert h.prompt("go") == "end_turn"
    assert [e.text for e in events if e.kind == "message"] == ["第一回", "DONE"]
    assert (repo / "a.txt").exists()
    log = sp.run(["git", "log", "-1", "--format=%an %s"], cwd=repo, capture_output=True, text=True).stdout
    assert log.strip() == "fake-agent add a"
    assert not (repo / "claude_was_here.txt").exists()
    assert h.prompt("again") == "error"
    assert any(e.kind == "error" and "usage limit" in e.text for e in events)


# ---- Task 17：每回合的 CLI 用量 ----

def test_headless_turn_usage_from_result(tmp_path):
    d = make_driver(CliInfo("claude", "Claude", "x", "headless"), str(tmp_path), lambda e: None,
                    lambda r: "", read_only=False, cmd_override=BASE, model="claude-sonnet-4-5")
    assert d.turn_usage() is None
    assert d.prompt("hi") == "end_turn"
    u = d.turn_usage()
    # 假 claude 的 result：input 100 + cache 300 + 2000；output 50；total_cost_usd 0.0123
    assert u["model"] == "claude-sonnet-4-5" and u["models"] == ["claude-sonnet-4-5"]
    assert (u["in"], u["out"], u["in_cache"]) == (2400, 50, 2300)
    assert (u["cost"], u["currency"]) == (0.0123, "USD")


def test_headless_turn_usage_missing_fields_stay_none(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=False)
    u = h._result_usage({"type": "result"})
    assert u["in"] is None and u["out"] is None and u["cost"] is None and u["currency"] == ""


def test_acp_turn_usage_only_what_agent_reports(tmp_path, monkeypatch):
    sp = tmp_path / "script.json"
    sp.write_text(json.dumps([[{"say": "x"}]]), encoding="utf-8")
    d = make_driver(CliInfo("kiro", "Kiro", "x", "acp"), str(tmp_path), lambda e: None,
                    lambda r: "allow-once", read_only=False, cmd_override=[sys.executable, AGENT, str(sp)])
    try:
        d.start()
        d.prompt("hi")
        u = d.turn_usage()
        # 真的 kiro 預設不回報模型、token、費用：全部是空的，不補數字
        assert u["model"] == "" and u["in"] is None and u["out"] is None and u["cost"] is None
        assert u["context_pct"] is None
    finally:
        d.close()


def test_acp_turn_usage_model_cost_and_context(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_MODEL", "claude-sonnet-4.5")
    monkeypatch.setenv("FAKE_ACP_PROMPT_USAGE", "1")
    sp = tmp_path / "script.json"
    sp.write_text(json.dumps([
        [{"usage": {"used": 10, "size": 100, "cost": {"amount": 0.5, "currency": "USD"}}},
         {"info": {"usagePercentage": 0.8}}, {"say": "a"}],
        [{"usage": {"used": 20, "size": 100, "cost": {"amount": 0.75, "currency": "USD"}}}, {"say": "b"}],
    ]), encoding="utf-8")
    d = make_driver(CliInfo("kiro", "Kiro", "x", "acp"), str(tmp_path), lambda e: None,
                    lambda r: "allow-once", read_only=False, cmd_override=[sys.executable, AGENT, str(sp)])
    try:
        d.start()
        d.prompt("1")
        u = d.turn_usage()
        assert u["model"] == "claude-sonnet-4.5" and (u["in"], u["out"]) == (1500, 120)
        assert (u["cost"], u["currency"], u["context_pct"]) == (0.5, "USD", 0.8)
        d.prompt("2")
        assert d.turn_usage()["cost"] == 0.25  # 累計 0.75 − 上一回合 0.5
    finally:
        d.close()


# ---- 最終審查第 1、2 項：寫入座位的 allowedTools；提示詞走 stdin ----

def test_headless_write_seat_allowed_tools(tmp_path):
    h = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=False,
                       allowed_tools=["Bash(python -m pytest:*)"])
    cmd = h.build_cmd()
    i = cmd.index("--allowedTools")
    j = cmd.index("--disallowedTools")
    allowed = cmd[i + 1:j]
    for t in ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)",
              "Bash(git commit:*)", "Bash(python -m pytest:*)", "Bash(ls:*)", "Bash(cat:*)",
              "Bash(type:*)", "Bash(dir:*)", "Bash(findstr:*)", "Bash(grep:*)"]:
        assert t in allowed
    assert cmd[-len(DISALLOWED):] == DISALLOWED  # 拒絕清單仍在
    assert "acceptEdits" in cmd and "bypassPermissions" not in cmd
    assert not any("git push" in t for t in allowed)


def test_headless_review_seat_has_no_allowed_tools(tmp_path):
    ro = HeadlessClaude(BASE, str(tmp_path), lambda e: None, read_only=True,
                        allowed_tools=["Bash(pytest:*)"])
    cmd = ro.build_cmd()
    assert "--allowedTools" not in cmd and "plan" in cmd


def test_make_driver_passes_allowed_tools(tmp_path):
    d = make_driver(CliInfo("claude", "Claude", "x", "headless"), str(tmp_path), lambda e: None,
                    lambda r: "", read_only=False, cmd_override=BASE, allowed_tools=["Bash(npm test:*)"])
    assert "Bash(npm test:*)" in d.claude.build_cmd()
    d.set_allowed_tools(["Bash(pytest:*)"])
    cmd = d.claude.build_cmd()
    assert "Bash(pytest:*)" in cmd and "Bash(npm test:*)" not in cmd and "Bash(git commit:*)" in cmd


@pytest.mark.parametrize("cmd,want", [
    ("python -m pytest -q", "Bash(python -m pytest:*)"),
    ("pytest tests", "Bash(pytest:*)"),
    ("npm test", "Bash(npm test:*)"),
    ("npm run test:unit", "Bash(npm run test:unit:*)"),
    ("py -m unittest discover", "Bash(py -m unittest:*)"),
    ("dotnet test Foo.sln", "Bash(dotnet test:*)"),
    ("py.test -q", "Bash(py.test:*)"),
    ("tox -e py311", "Bash(tox:*)"),
    ("jest --ci", "Bash(jest:*)"),
    ("vitest run", "Bash(vitest:*)"),
    ("pnpm test", "Bash(pnpm test:*)"),
    ("yarn test", "Bash(yarn test:*)"),
    ("go test ./...", "Bash(go test:*)"),
    ("cargo test", "Bash(cargo test:*)"),
    ("mvn test", "Bash(mvn test:*)"),
    ("./gradlew test", "Bash(./gradlew test:*)"),
    ("", ""),
])
def test_runner_tool_pattern(cmd, want):
    assert runner_tool_pattern(cmd) == want


@pytest.mark.parametrize("cmd", [
    "python -c \"import os; os.system('x')\"", "python script.py", "node x.js", "bash run.sh", "sh -c pytest",
    "powershell -Command pytest", "cmd /c pytest", "git log", "npx jest", "npx some-pkg",
    "pytest; curl x", "pytest && curl x", "pytest | tee out", "pytest > out.txt", "pytest `id`", "pytest $(id)",
    "cd sub && py -m pytest", "npm run 'a b'", "npm install", "python -m pip install x", "make test",
])
def test_runner_tool_pattern_rejects_non_allowlisted(cmd):
    assert runner_tool_pattern(cmd) == ""


def test_headless_long_prompt_via_stdin(tmp_path):
    events = []
    text = ("第一行 line\n" * 5000)[:40000]
    assert len(text) == 40000 and "\n" in text
    h = HeadlessClaude(BASE, str(tmp_path), events.append, read_only=True)
    assert text not in h.build_cmd()
    assert h.prompt(text) == "end_turn"
    msg = [e.text for e in events if e.kind == "message"][0]
    assert "chars=40000" in msg


def test_fake_claude_commit_needs_allowed_tools(tmp_path):
    import subprocess as sp
    repo = tmp_path / "r"
    repo.mkdir()
    sp.run(["git", "init", "-q"], cwd=repo, check=True)
    sp.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "--allow-empty", "-m", "i"],
           cwd=repo, check=True)
    script = tmp_path / "s.json"
    script.write_text(json.dumps([[{"write": {"path": "a.txt", "content": "x"}}, {"commit": "add a"}]]),
                      encoding="utf-8")
    args = BASE + ["--script", str(script), "-p", "--permission-mode", "acceptEdits"]
    r = sp.run(args, cwd=repo, input=b"go", capture_output=True)
    assert "權限被拒" in r.stdout.decode("utf-8")
    log = sp.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
    assert "add a" not in log
