import json
import os
import subprocess
import sys
import threading
import time

import pytest

from team.acp import AcpClient, AcpError
from team.events import AgentEvent, PermissionRequest

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


def make_client(tmp_path, repo, script, on_permission=None, events=None):
    sp = tmp_path / "script.json"
    sp.write_text(json.dumps(script), encoding="utf-8")
    events = events if events is not None else []
    client = AcpClient([sys.executable, AGENT, str(sp)], str(repo), events.append,
                       on_permission or (lambda req: "allow-once"))
    return client, events


def test_handshake_session_and_prompt(repo, tmp_path):
    c, events = make_client(tmp_path, repo, [[{"say": "你好"}]])
    try:
        res = c.start()
        assert res["protocolVersion"] == 1
        assert c.alive
        assert c.new_session() == "fake-1"
        assert c.prompt("hi") == "end_turn"
        msgs = [e for e in events if e.kind == "message"]
        assert msgs and msgs[0].text == "你好"
        assert all(isinstance(e, AgentEvent) for e in events)
    finally:
        c.close()
    assert not c.alive


def test_permission_callback_runs_off_reader_thread(repo, tmp_path):
    script = [[{"say": "先說話"},
               {"tool": {"title": "edit a.txt", "kind": "edit", "rawInput": {"path": "a.txt"}}},
               {"write": {"path": "a.txt", "content": "x"}},
               {"say": "寫好了"}]]
    seen = {}
    holder = {}

    def on_permission(req):
        assert isinstance(req, PermissionRequest)
        seen["req"] = req
        seen["thread"] = threading.current_thread().name
        time.sleep(0.5)
        # 讀取執行緒沒被卡住：期間送出的請求仍能收到回應（假代理回 -32601 → False）
        t0 = time.time()
        seen["set_model"] = holder["c"].set_model("glm-5.3")
        seen["set_model_secs"] = time.time() - t0
        return "allow-once"

    c, events = make_client(tmp_path, repo, script, on_permission)
    holder["c"] = c
    try:
        c.start()
        c.new_session()
        assert c.prompt("go") == "end_turn"
    finally:
        c.close()
    assert seen["req"].title == "edit a.txt"
    assert seen["req"].kind == "edit"
    assert seen["req"].options[0]["optionId"] == "allow-once"
    assert seen["thread"] != "acp-reader"
    assert seen["set_model"] is False
    assert seen["set_model_secs"] < 3
    kinds = [e.kind for e in events]
    assert kinds.index("message") < kinds.index("tool")
    assert "tool_update" in kinds
    assert [e.text for e in events if e.kind == "message"] == ["先說話", "寫好了"]
    assert (repo / "a.txt").read_text() == "x"


def test_process_exit_raises_with_stderr(repo, tmp_path):
    cmd = [sys.executable, "-c", "import sys; sys.stderr.write('not logged in\\n'); sys.exit(3)"]
    c = AcpClient(cmd, str(repo), lambda e: None, lambda r: "")
    with pytest.raises(AcpError) as ei:
        c.start(timeout=10)
    msg = str(ei.value)
    assert "exit 3" in msg
    assert "not logged in" in msg
    assert not c.alive
    with pytest.raises(AcpError):
        c.new_session(timeout=2)
    c.close()


def test_prompt_timeout(repo, tmp_path):
    script = [[{"run": '"%s" -c "import time; time.sleep(5)"' % sys.executable}]]
    c, _ = make_client(tmp_path, repo, script)
    try:
        c.start()
        c.new_session()
        t0 = time.time()
        with pytest.raises(AcpError) as ei:
            c.prompt("go", timeout=1)
        assert "沒有回應" in str(ei.value)
        assert "1 秒" in str(ei.value)
        assert time.time() - t0 < 3
    finally:
        c.close()


def test_cancel_returns_cancelled(repo, tmp_path):
    script = [[{"run": '"%s" -c "import time; time.sleep(3)"' % sys.executable},
               {"say": "never"}]]
    c, events = make_client(tmp_path, repo, script)
    out = {}
    try:
        c.start()
        c.new_session()
        th = threading.Thread(target=lambda: out.setdefault("r", c.prompt("go")))
        t0 = time.time()
        th.start()
        time.sleep(0.3)
        c.cancel()
        th.join(10)
        assert out.get("r") == "cancelled"
        assert time.time() - t0 < 2.5
        assert "never" not in [e.text for e in events]
    finally:
        c.close()


def test_close_kills_stubborn_process(repo, tmp_path):
    # 不理會 stdin EOF 的程序：close 在約 2 秒後強制結束
    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    c = AcpClient(cmd, str(repo), lambda e: None, lambda r: "")
    c._spawn()
    assert c.alive
    t0 = time.time()
    c.close()
    assert not c.alive
    assert time.time() - t0 < 6
