"""測試透過 SSH 遠端啟動並操作 ACP (kiro/agy) 與 Headless (claude/glm) CLI 驅動。"""
import os
import sys
from pathlib import Path

import pytest

from team.clis import CliInfo
from team.drivers import make_driver, seat_launch
from team.models import ModelConfig


ROOT = Path(__file__).resolve().parent.parent.parent
FAKE_ACP = str(ROOT / "team" / "fakes" / "fake_acp_agent.py")
FAKE_CLAUDE = str(ROOT / "team" / "fakes" / "fake_claude.py")


@pytest.fixture
def sim_remote_stdio(monkeypatch):
    """在單元測試中讓遠端 stdio 橋接器直接以本機 Python 模擬遠端 SSH 主機。"""
    monkeypatch.setenv("BUTTONAUTO_FAKE_SSH_LOCAL", "1")


def test_remote_acp_driver_over_ssh(tmp_path, sim_remote_stdio):
    remote_wt = tmp_path / "remote_wt_acp"
    remote_wt.mkdir()
    script_file = tmp_path / "acp_script.json"
    script_file.write_text(
        '[[ {"write": {"path": "from_acp.txt", "content": "ok"}}, {"say": "done: remote acp ok"} ]]',
        encoding="utf-8",
    )

    cli = CliInfo(
        name="kiro",
        label="Kiro",
        path=sys.executable,
        protocol="acp",
    )
    events = []
    drv = make_driver(
        cli,
        str(remote_wt),
        on_event=events.append,
        on_permission=lambda req: "allow",
        read_only=False,
        cmd_override=[sys.executable, "-u", FAKE_ACP, str(script_file)],
        ssh_host="remote-box",
    )
    try:
        drv.start()
        assert drv.client.session_id
        # 驗證遠端代理收到的 cwd 是遠端 worktree 路徑
        stop = drv.prompt("請確認遠端 ACP 連線")
        assert stop == "end_turn"
        msgs = "".join(ev.text for ev in events if ev.kind == "message")
        assert "done: remote acp ok" in msgs
        assert (remote_wt / "from_acp.txt").read_text(encoding="utf-8") == "ok"
    finally:
        drv.close()


def test_remote_headless_glm_driver_over_ssh_forwards_env_and_resumes(tmp_path, sim_remote_stdio):
    remote_wt = tmp_path / "remote_wt_claude"
    remote_wt.mkdir()
    capture_file = remote_wt / "captured_env.json"

    clis = {
        "claude": CliInfo(
            name="claude",
            label="Claude",
            path=sys.executable,
            protocol="headless",
        )
    }
    cfg = ModelConfig(
        base_url="http://127.0.0.1:4000",
        glm_key="sk-secret-remote-glm-token",
        glm_model="glm-5.3",
        decision1_key="sk-d1",
    )
    cli, env, extra = seat_launch(
        "glm",
        clis,
        cfg,
        plugin_dirs=[str(tmp_path)],
        worktree=str(remote_wt),
        tmpdir=str(tmp_path / "tmp"),
        ssh_host="remote-box",
    )
    # 遠端模式不可夾帶本機 plugin-dir 或本機 serena.mcp.json 路徑
    assert "--plugin-dir" not in extra
    assert "--mcp-config" not in extra

    env["FAKE_CLAUDE_CAPTURE"] = str(capture_file)
    events = []
    drv = make_driver(
        cli,
        str(remote_wt),
        on_event=events.append,
        on_permission=lambda req: "allow",
        read_only=False,
        cmd_override=[sys.executable, "-u", FAKE_CLAUDE],
        env=env,
        extra_args=extra,
        ssh_host="remote-box",
    )
    try:
        drv.start()
        r1 = drv.prompt("第一回合測試")
        assert r1
        sid = drv.claude.session_id
        assert sid

        r2 = drv.prompt("第二回合測試")
        assert r2
        assert drv.claude.session_id == sid
    finally:
        drv.close()
