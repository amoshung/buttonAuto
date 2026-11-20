"""U4 測試：Server 與 Office 的 SSH 主機管理、遠端 CLI 嚴格偵測過濾、遠端目錄瀏覽／掃描與遠端房間派工。"""
import os
import subprocess
from pathlib import Path

import pytest

from server import AssistantServer, TeamError
from team.models import ModelConfig
from team.ssh import run_remote_probe


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("# Demo Remote Repo\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@local", "add", "-A"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@local", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def ssh_server(tmp_path, monkeypatch):
    monkeypatch.setenv("BUTTONAUTO_FAKE_SSH_LOCAL", "1")
    cfg_path = tmp_path / "config.json"
    sec_path = tmp_path / "secrets.json"
    team_root = tmp_path / "sessions" / "team"
    base_dir = tmp_path / "base"
    base_dir.mkdir()

    # 模擬遠端 probe runner：只回報 claude 與 kiro 可調用，agy 不可調用（應被隱藏）
    def fake_probe_runner(ssh_host, payload, timeout=25.0):
        op = payload.get("op")
        if op in ("dirs", "scan", "exec", "fs"):
            return run_remote_probe("", payload, timeout)
        return {
            "ok": True,
            "hostname": "remote-gpu-01",
            "platform": "Linux-6.8-x86_64",
            "home": str(tmp_path),
            "claude_bin_ok": True,
            "clis": {
                "claude": {
                    "name": "claude",
                    "label": "Claude Code",
                    "path": "/usr/local/bin/claude",
                    "protocol": "headless",
                    "callable": True,
                    "version": "2.1.0",
                    "models": [
                        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "description": ""},
                    ],
                    "current_model": "claude-sonnet-4-6",
                },
                "kiro": {
                    "name": "kiro",
                    "label": "Kiro",
                    "path": "/usr/local/bin/kiro-cli",
                    "protocol": "acp",
                    "callable": True,
                    "version": "1.20.0",
                    "models": [
                        {"id": "claude-sonnet-4.5", "name": "Claude Sonnet 4.5", "description": ""},
                    ],
                    "current_model": "claude-sonnet-4.5",
                },
                "agy": {
                    "name": "agy",
                    "label": "Antigravity",
                    "path": "/usr/local/bin/agy",
                    "protocol": "acp",
                    "callable": False,
                    "error": "not logged in",
                    "models": [],
                },
            },
        }

    srv = AssistantServer(
        config_path=str(cfg_path),
        secrets_path=str(sec_path),
        start_threads=False,
        team_root=str(team_root),
        team_base_dir=str(base_dir),
        team_opts={
            "detect_on_start": False,
            "model_cfg": ModelConfig("http://127.0.0.1:4000", "sk-glm", "glm-5.3", "sk-d1"),
            "ssh_probe_runner": fake_probe_runner,
        },
    )
    return srv


def test_ssh_hosts_crud_and_validation(ssh_server):
    # 1. 初始列出主機
    res = ssh_server.team_get("/api/team/ssh/hosts", {})
    assert "hosts" in res

    # 2. 新增合法的 user@host:port
    added = ssh_server.team_post(
        "/api/team/ssh/hosts",
        {"action": "add", "target": "ubuntu@10.0.0.8:2222", "label": "GPU 伺服器"},
    )
    assert added["ok"] is True
    assert any(h["target"] == "ubuntu@10.0.0.8:2222" for h in added["hosts"])

    # 3. 拒絕危險的 -oProxyCommand 參數注入
    bad = ssh_server.team_post(
        "/api/team/ssh/hosts",
        {"action": "add", "target": "-oProxyCommand=calc.exe"},
    )
    assert bad["ok"] is False

    # 4. 刪除自訂主機
    removed = ssh_server.team_post(
        "/api/team/ssh/hosts",
        {"action": "remove", "target": "ubuntu@10.0.0.8:2222"},
    )
    assert removed["ok"] is True
    assert not any(h["target"] == "ubuntu@10.0.0.8:2222" for h in removed["hosts"])


def test_ssh_probe_strictly_filters_uncallable_clis_and_models_view(ssh_server):
    probe = ssh_server.team_post("/api/team/ssh/probe", {"ssh_host": "ubuntu@10.0.0.8:2222"})
    assert probe["ok"] is True
    # agy 不可調用，必須從 clis 與 available_seats 中隱藏！
    assert "claude" in probe["clis"]
    assert "kiro" in probe["clis"]
    assert "agy" not in probe["clis"]
    assert set(probe["available_seats"]) == {"claude", "kiro", "glm", "glm_high"}

    # GET /api/team/models?ssh_host=... 也只標記可調用的座位為 available
    mview = ssh_server.team_get("/api/team/models", {"ssh_host": ["ubuntu@10.0.0.8:2222"]})
    assert mview["ssh_host"] == "ubuntu@10.0.0.8:2222"
    assert set(mview["available_seats"]) == {"claude", "kiro", "glm", "glm_high"}
    assert mview["seats"]["claude"]["available"] is True
    assert mview["seats"]["kiro"]["available"] is True
    assert mview["seats"]["agy"]["available"] is False


def test_remote_dirs_scan_and_room_creation_enforces_callable_cli(ssh_server, tmp_path):
    remote_repo = tmp_path / "remote_project"
    _init_git_repo(remote_repo)

    # 遠端瀏覽資料夾與掃描專案
    dirs_res = ssh_server.team_get(
        "/api/team/dirs",
        {"path": [str(tmp_path)], "ssh_host": ["ubuntu@10.0.0.8:2222"]},
    )
    assert any(d["name"] == "remote_project" for d in dirs_res["dirs"])

    scan_res = ssh_server.team_get(
        "/api/team/scan",
        {"path": [str(remote_repo)], "ssh_host": ["ubuntu@10.0.0.8:2222"]},
    )
    assert scan_res["exists"] is True
    assert scan_res["is_git"] is True

    # 先執行 probe 快取該遠端主機的可用 CLI
    ssh_server.team_post("/api/team/ssh/probe", {"ssh_host": "ubuntu@10.0.0.8:2222"})

    # 若強行指定遠端不可調用的 agy，建立房間必須拒絕！
    bad_room = ssh_server.team_post(
        "/api/team/rooms",
        {
            "repo": str(remote_repo),
            "goal": "在遠端實作功能",
            "impl_cli": "agy",
            "review_cli": "claude",
            "ssh_host": "ubuntu@10.0.0.8:2222",
        },
    )
    assert bad_room["ok"] is False
    assert "Antigravity" in bad_room["text"] or "agy" in bad_room["text"]

    # 指定遠端可調用的 kiro 與 claude，成功建立遠端房間並記錄 ssh_host
    ok_room = ssh_server.team_post(
        "/api/team/rooms",
        {
            "repo": str(remote_repo),
            "goal": "在遠端實作功能",
            "impl_cli": "kiro",
            "review_cli": "claude",
            "ssh_host": "ubuntu@10.0.0.8:2222",
        },
    )
    assert ok_room["ok"] is True
    assert ok_room["room"]["ssh_host"] == "ubuntu@10.0.0.8:2222"
    assert ok_room["room"]["impl_cli"] == "kiro"
    assert ok_room["room"]["review_cli"] == "claude"


def test_ssh_password_auth_required_and_role_prefs(ssh_server, tmp_path):
    from team.ssh import SshError, clear_ssh_passwords, has_ssh_password

    clear_ssh_passwords()
    host = "bofmgr@172.22.102.111"
    orig_runner = ssh_server.team.ssh_probe_runner

    def pw_guarded_runner(ssh_host, payload, timeout=25.0):
        if ssh_host == host and not has_ssh_password(host):
            raise SshError(
                "SSH 連線失敗（exit 255）：bofmgr@172.22.102.111: Permission denied (publickey,gssapi-keyex,gssapi-with-mic,password)."
            )
        return orig_runner(ssh_host, payload, timeout)

    ssh_server.team.ssh_probe_runner = pw_guarded_runner

    # 1. 未提供密碼時，probe 回報 ok=False 且 auth_required=True
    res1 = ssh_server.team_post("/api/team/ssh/probe", {"ssh_host": host, "force": True})
    assert res1["ok"] is False
    assert res1["auth_required"] is True

    # 2. 提供密碼後，probe 自動透過記憶體密碼驗證成功並偵測遠端 CLI
    res2 = ssh_server.team_post(
        "/api/team/ssh/probe",
        {"ssh_host": host, "force": True, "password": "my-secret-password"},
    )
    assert res2["ok"] is True
    assert res2["has_password"] is True
    assert "claude" in res2["clis"]

    # 3. 建立房間時可指定全部 6 大職稱角色 role_prefs
    remote_repo = tmp_path / "bof_remote_repo"
    _init_git_repo(remote_repo)
    rm = ssh_server.team_post(
        "/api/team/rooms",
        {
            "repo": str(remote_repo),
            "goal": "各職位模型配置測試",
            "ssh_host": host,
            "impl_cli": "claude",
            "impl_model": "sonnet",
            "review_cli": "kiro",
            "review_model": "claude-sonnet-4.5",
            "supervisor_cli": "litellm",
            "supervisor_model": "Decision-1",
            "role_prefs": {
                "po": {"seat": "litellm", "model": "Decision-1"},
                "tech_lead": {"seat": "litellm", "model": "glm-5.3"},
                "scrum": {"seat": "litellm", "model": "Decision-1"},
                "qa": {"seat": "litellm", "model": "glm-5.3"},
                "impl": {"seat": "claude", "model": "sonnet"},
                "review": {"seat": "kiro", "model": "claude-sonnet-4.5"},
            },
        },
    )
    assert rm["ok"] is True
    assert rm["room"]["role_prefs"]["po"]["model"] == "Decision-1"
    assert rm["room"]["role_prefs"]["tech_lead"]["model"] == "glm-5.3"
    assert rm["room"]["role_prefs"]["impl"]["seat"] == "claude"
    clear_ssh_passwords()

