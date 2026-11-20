"""U1 測試：SSH 主機設定解析、目標驗證、遠端目錄瀏覽／專案掃描與 CLI 嚴格握手偵測。"""
import json
import os
import subprocess
import sys

import pytest

from team.ssh import (
    REMOTE_PROBE_SCRIPT,
    SshError,
    SshHostStore,
    build_ssh_cmd,
    parse_ssh_config,
    probe_host_clis,
    remote_dirs,
    remote_scan,
    split_ssh_target,
    validate_ssh_target,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAKE_ACP = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")
FAKE_CLAUDE = os.path.join(ROOT, "team", "fakes", "fake_claude.py")
PY = sys.executable


def _make_cli_wrapper(dir_path, name, target_py, extra_env=None, fail_handshake=False):
    """在 dir_path 建立可執行的假 CLI（Windows .cmd 與 POSIX 無副檔名腳本）。"""
    runner_py = os.path.join(dir_path, f"_{name}_runner.py")
    with open(runner_py, "w", encoding="utf-8") as f:
        f.write(
            "import os, runpy, sys\n"
            f"if {bool(fail_handshake)!r}:\n"
            "    sys.stderr.write('Handshake failed: not logged in\\n')\n"
            "    sys.exit(1)\n"
            f"os.environ.update({dict(extra_env or {})!r})\n"
            "if sys.argv[1:] and sys.argv[1] == 'acp':\n"
            "    sys.argv = [sys.argv[0]]\n"
            f"sys.argv[0] = {target_py!r}\n"
            f"runpy.run_path({target_py!r}, run_name='__main__')\n"
        )
    bat_body = f'@echo off\r\n"{PY}" "{runner_py}" %*\r\n'
    sh_body = f'#!/bin/sh\nexec "{PY}" "{runner_py}" "$@"\n'

    cmd_path = os.path.join(dir_path, name + ".cmd")
    with open(cmd_path, "w", encoding="utf-8") as f:
        f.write(bat_body)
    sh_path = os.path.join(dir_path, name)
    with open(sh_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(sh_body)
    try:
        os.chmod(sh_path, 0o755)
    except OSError:
        pass
    return cmd_path if os.name == "nt" else sh_path


def test_validate_and_split_ssh_target():
    assert validate_ssh_target("gpu-dev") == ""
    assert validate_ssh_target("ubuntu@192.168.1.50") == ""
    assert validate_ssh_target("ubuntu@192.168.1.50:2222") == ""
    assert validate_ssh_target("deploy@my-server.internal:22") == ""

    # 防 SSH Option Injection 與非法字元
    assert validate_ssh_target("") != ""
    assert validate_ssh_target("-oProxyCommand=evil") != ""
    assert validate_ssh_target("user@-evil") != ""
    assert validate_ssh_target("user@host with space") != ""
    assert validate_ssh_target("user@host;rm") != ""
    assert validate_ssh_target("user@host:notaport") != ""
    assert validate_ssh_target("user@host:70000") != ""

    host, port = split_ssh_target("ubuntu@192.168.1.50:2222")
    assert host == "ubuntu@192.168.1.50" and port == 2222
    host2, port2 = split_ssh_target("gpu-dev")
    assert host2 == "gpu-dev" and port2 is None

    cmd = build_ssh_cmd("ubuntu@192.168.1.50:2222", ["git", "status"])
    assert cmd[:6] == ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    assert "-p" in cmd and "2222" in cmd
    assert cmd[cmd.index("2222") + 1] == "ubuntu@192.168.1.50"


def test_parse_ssh_config_and_store(tmp_path):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    inc_file = ssh_dir / "conf.d" / "work.conf"
    inc_file.parent.mkdir()
    inc_file.write_text(
        "Host work-gpu\n"
        "  HostName 10.20.30.40\n"
        "  User researcher\n"
        "  Port 22022\n",
        encoding="utf-8",
    )
    cfg_file = ssh_dir / "config"
    cfg_file.write_text(
        "Include conf.d/*.conf\n\n"
        "Host *\n"
        "  ServerAliveInterval 60\n\n"
        "Host dev-box prod-*\n"
        "  HostName dev.example.com\n"
        "  User alice\n",
        encoding="utf-8",
    )

    hosts = parse_ssh_config(str(cfg_file))
    by_id = {h["id"]: h for h in hosts}
    assert "work-gpu" in by_id
    assert by_id["work-gpu"]["hostname"] == "10.20.30.40"
    assert by_id["work-gpu"]["user"] == "researcher"
    assert by_id["work-gpu"]["port"] == 22022
    assert "dev-box" in by_id
    assert "*" not in by_id and "prod-*" not in by_id

    store = SshHostStore(
        custom_path=str(tmp_path / "ssh_hosts.local.json"),
        cache_path=str(tmp_path / "ssh_clis_cache.json"),
        ssh_config_path=str(cfg_file),
    )
    added = store.add_host("bob@192.168.10.99:2222", label="Bob GPU")
    assert added["id"] == "bob@192.168.10.99:2222"
    all_hosts = store.list_hosts()
    ids = [h["id"] for h in all_hosts]
    assert "work-gpu" in ids and "dev-box" in ids and "bob@192.168.10.99:2222" in ids

    with pytest.raises(SshError):
        store.add_host("-oProxyCommand=bad")

    store.remove_host("bob@192.168.10.99:2222")
    assert "bob@192.168.10.99:2222" not in [h["id"] for h in store.list_hosts()]


def test_strict_cli_handshake_hides_uncallable_clis(tmp_path):
    """核心需求：能偵測遠端是否有 claude、agy、kiro-cli 可以調用，不能調用的就別顯示出來。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # 1. kiro-cli：握手成功並回報模型
    _make_cli_wrapper(
        str(bin_dir),
        "kiro-cli",
        FAKE_ACP,
        extra_env={
            "FAKE_ACP_MODELS": "claude-sonnet-5.5",
            "FAKE_ACP_MODEL": "claude-sonnet-5.5",
        },
    )
    # 2. agy：存在執行檔，但握手失敗（例如未登入或崩潰）→ 絕不可出現在可調用清單！
    _make_cli_wrapper(str(bin_dir), "agy", FAKE_ACP, fail_handshake=True)
    # 3. claude：正常可調用
    _make_cli_wrapper(str(bin_dir), "claude", FAKE_CLAUDE)

    def local_runner(_ssh_host, payload, timeout=25.0):
        p = subprocess.run(
            [PY, "-c", REMOTE_PROBE_SCRIPT],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
        assert p.returncode == 0, p.stderr.decode("utf-8", errors="replace")
        return json.loads(p.stdout.decode("utf-8"))

    res = probe_host_clis(
        "work-gpu",
        has_glm_cfg=True,
        glm_model="glm-5.3",
        runner=local_runner,
        extra_search_paths=[str(bin_dir)],
        isolate_path=True,
    )
    assert res["ok"] is True
    assert res["reachable"] is True
    # agy 握手失敗，不能顯示在 clis 與 available_seats 中！
    assert "kiro" in res["clis"]
    assert "claude" in res["clis"]
    assert "agy" not in res["clis"]
    assert "agy" not in res["available_seats"]
    assert set(res["available_seats"]) == {"kiro", "claude", "glm", "glm_high"}
    assert [m["id"] for m in res["clis"]["kiro"]["models"]] == ["claude-sonnet-5.5"]


def test_remote_dirs_and_scan_via_probe(tmp_path):
    proj = tmp_path / "my_remote_proj"
    proj.mkdir()
    (proj / "frontend").mkdir()
    (proj / "backend").mkdir()
    (proj / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    def local_runner(_ssh_host, payload, timeout=25.0):
        p = subprocess.run(
            [PY, "-c", REMOTE_PROBE_SCRIPT],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
        assert p.returncode == 0
        return json.loads(p.stdout.decode("utf-8"))

    d = remote_dirs("work-gpu", str(proj), runner=local_runner)
    assert d["path"] == str(proj)
    assert {x["name"] for x in d["dirs"]} == {"frontend", "backend"}

    s = remote_scan("work-gpu", str(proj), runner=local_runner)
    assert s["name"] == "my_remote_proj"
    assert "frontend" in s["subdirs"] and "backend" in s["subdirs"]
    assert any("pytest" in c for c in s["checks"])
