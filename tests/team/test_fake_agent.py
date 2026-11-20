import json
import os
import subprocess
import sys
import threading
import time

import pytest

from team.events import AgentEvent, PermissionRequest, command_text

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo with space"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@l")
    (r / "README.md").write_text("hi")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


class Client:
    def __init__(self, script, tmp_path, answer="allow-once"):
        sp = tmp_path / "script.json"
        sp.write_text(json.dumps(script), encoding="utf-8")
        self.p = subprocess.Popen([sys.executable, AGENT, str(sp)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.answer = answer
        self.msgs = []
        self.perms = []
        self.lines = []
        self._n = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            m = json.loads(line)
            self.lines.append(m)
            if m.get("method") == "session/request_permission":
                self.perms.append(m)
                if self.answer is not None:
                    self.send({"jsonrpc": "2.0", "id": m["id"], "result": {
                        "outcome": {"outcome": "selected", "optionId": self.answer}}})

    def send(self, m):
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def call(self, method, params=None, timeout=10):
        self._n += 100
        rid = self._n
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        return self.wait(rid, timeout)

    def wait(self, rid, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            for m in list(self.lines):
                if m.get("id") == rid and "method" not in m:
                    return m
            time.sleep(0.02)
        raise AssertionError("timeout waiting for %s" % rid)

    def texts(self):
        return [m["params"]["update"]["content"]["text"] for m in self.lines
                if m.get("method") == "session/update"
                and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


SCRIPT = [[{"say": "開始"},
           {"tool": {"title": "edit a.txt", "kind": "edit", "rawInput": {"path": "a.txt"}}},
           {"write": {"path": "a.txt", "content": "x"}},
           {"commit": "add a"}]]


def start(c, repo):
    assert c.call("initialize", {"protocolVersion": 1})["result"]["protocolVersion"] == 1
    assert c.call("session/new", {"cwd": str(repo), "mcpServers": []})["result"]["sessionId"] == "fake-1"


def test_fake_agent_runs_turn_and_asks_permission(repo, tmp_path):
    c = Client(SCRIPT, tmp_path)
    start(c, repo)
    r = c.call("session/prompt", {"sessionId": "fake-1", "prompt": [{"type": "text", "text": "go"}]})
    assert r["result"]["stopReason"] == "end_turn"
    assert "開始" in c.texts()
    assert c.perms[0]["params"]["options"][0]["kind"] == "allow_once"
    assert (repo / "a.txt").exists()
    assert len(git(repo, "log", "--oneline").splitlines()) == 2
    assert "fake-agent" in git(repo, "log", "-1", "--format=%an")
    c.close()


def test_fake_agent_reject_skips_write(repo, tmp_path):
    c = Client(SCRIPT, tmp_path, answer="reject-once")
    start(c, repo)
    r = c.call("session/prompt", {"sessionId": "fake-1", "prompt": [{"type": "text", "text": "go"}]})
    assert r["result"]["stopReason"] == "end_turn"
    assert not (repo / "a.txt").exists()
    assert any("被拒絕" in t for t in c.texts())
    assert len(git(repo, "log", "--oneline").splitlines()) == 1
    c.close()


def test_unknown_method_and_echo_and_repeat_last_turn(repo, tmp_path):
    c = Client([[{"say": "one"}], [{"echo_prompt": True}]], tmp_path)
    start(c, repo)
    assert c.call("foo/bar")["error"]["code"] == -32601
    c.call("session/prompt", {"prompt": [{"type": "text", "text": "A" * 300}]})
    c.call("session/prompt", {"prompt": [{"type": "text", "text": "hello"}]})
    c.call("session/prompt", {"prompt": [{"type": "text", "text": "again"}]})
    assert "again" in c.texts()
    c.close()


def test_run_step_reports_output(repo, tmp_path):
    c = Client([[{"run": "echo hi"}]], tmp_path)
    start(c, repo)
    c.call("session/prompt", {"prompt": []})
    assert any("hi" in t and "exit 0" in t for t in c.texts())
    c.close()


def test_cancel_interrupts_run(repo, tmp_path):
    c = Client([[{"run": f'"{sys.executable}" -c "import time; time.sleep(30)"'}, {"say": "never"}]], tmp_path)
    start(c, repo)
    c._n += 100
    rid = c._n
    c.send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt", "params": {"prompt": []}})
    time.sleep(0.5)
    t0 = time.time()
    c.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "fake-1"}})
    r = c.wait(rid, 5)
    assert r["result"]["stopReason"] == "cancelled"
    assert time.time() - t0 < 5
    assert "never" not in c.texts()
    c.close()


def test_eof_exits_cleanly(repo, tmp_path):
    c = Client([[{"say": "x"}]], tmp_path)
    c.p.stdin.close()
    assert c.p.wait(10) == 0


def test_command_text():
    mk = lambda raw, title="T": PermissionRequest("t1", title, "execute", raw, [])
    assert command_text(mk({"command": "ls -l"})) == "ls -l"
    assert command_text(mk({"args": ["git", "push"]})) == "git push"
    assert command_text(mk({})) == "T"
    assert AgentEvent("done", data={"stop_reason": "end_turn"}).text == ""


def test_error_step_returns_jsonrpc_error(repo, tmp_path):
    c = Client([[{"say": "before"}, {"error": "You've hit your usage limit"}, {"say": "after"}]], tmp_path)
    start(c, repo)
    r = c.call("session/prompt", {"prompt": []})
    assert "usage limit" in r["error"]["message"]
    assert "before" in c.texts() and "after" not in c.texts()
    c.close()


def test_state_file_continues_turns_across_processes(repo, tmp_path):
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps([[{"say": "t0"}], [{"say": "t1"}], [{"say": "t2"}]]), encoding="utf-8")
    state = tmp_path / "s.state"
    seen = []
    for _ in range(2):
        p = subprocess.Popen([sys.executable, AGENT, str(sp), str(state)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, encoding="utf-8")
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(repo)}},
                {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {"prompt": []}}]
        for m in msgs:
            p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
        for line in p.stdout:
            m = json.loads(line)
            if m.get("method") == "session/update":
                seen.append(m["params"]["update"]["content"]["text"])
            if m.get("id") == 3:
                break
        p.stdin.close()
        p.wait(10)
    assert seen == ["t0", "t1"]
    assert state.read_text().strip() == "2"
