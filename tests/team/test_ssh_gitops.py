"""U2 測試：透過 SSH 在遠端執行 Git worktree 操作（create_worktree、changed_files、commits_since、merge_into_repo、remove_worktree）與 QA checks（run_cmd、test_gate）。"""
import os
import subprocess
import sys

import pytest

from team import checks, gitops, ssh
from team.gitops import GitError, Worktree


@pytest.fixture
def sim_ssh(monkeypatch):
    """將 ssh_host='remote-box' 的遠端指令模擬導向本機子程序執行，驗證所有參數、路徑與回傳處理。"""
    calls = []

    def fake_ssh_cmd(ssh_host, remote_argv, **kwargs):
        calls.append((ssh_host, list(remote_argv)))
        joined = " ".join(str(a) for a in remote_argv)
        if "__aea_rpc__" in joined:
            return [
                sys.executable,
                "-u",
                "-c",
                "import sys; s=sys.stdin.buffer.readline(); n=int(s); "
                "code=sys.stdin.buffer.read(n).decode('utf-8'); "
                "exec(compile(code,'<aea_rpc>','exec'), {'__name__':'__aea_rpc__'})",
            ]
        return [sys.executable, "-"]

    monkeypatch.setattr(ssh, "build_ssh_cmd", fake_ssh_cmd)
    ssh.close_ssh_channels("remote-box")
    try:
        yield calls
    finally:
        ssh.close_ssh_channels("remote-box")


def test_remote_gitops_worktree_and_merge(tmp_path, sim_ssh):
    remote_repo = tmp_path / "remote_repo"
    remote_repo.mkdir()
    (remote_repo / "app.py").write_text("x = 1\n", encoding="utf-8")

    # 遠端初始化 git repo
    created = gitops.ensure_local_git_repo(str(remote_repo), ssh_host="remote-box")
    assert created is True
    assert gitops.repo_problem(str(remote_repo), ssh_host="remote-box") == ""

    # 遠端建立 worktree
    wt = gitops.create_worktree(str(remote_repo), "feat-ssh", ssh_host="remote-box")
    assert wt.ssh_host == "remote-box"
    assert os.path.isdir(wt.path)

    # 在遠端 worktree 修改檔案並 commit
    with open(os.path.join(wt.path, "app.py"), "w", encoding="utf-8") as f:
        f.write("x = 2\n")
    assert "app.py" in gitops.uncommitted(wt)
    gitops.git(wt.path, *gitops.IDENTITY, "add", "app.py", ssh_host="remote-box")
    gitops.git(wt.path, *gitops.IDENTITY, "commit", "-m", "feat: update x to 2", ssh_host="remote-box")

    assert gitops.uncommitted(wt) == []
    assert gitops.changed_files(wt) == ["app.py"]
    commits = gitops.commits_since(wt)
    assert len(commits) == 1 and "update x to 2" in commits[0][1]
    assert "x = 2" in gitops.diff_text(wt)

    # 遠端合併回主分支並移除 worktree
    sha = gitops.merge_into_repo(wt)
    assert len(sha) >= 7
    gitops.remove_worktree(wt)
    assert not os.path.exists(wt.path)
    assert (remote_repo / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert any(h == "remote-box" for h, _ in sim_ssh)


def test_remote_checks_run_cmd(tmp_path, sim_ssh):
    remote_dir = tmp_path / "remote_check"
    remote_dir.mkdir()
    (remote_dir / "hello.txt").write_text("world", encoding="utf-8")

    res = checks.run_cmd(
        str(remote_dir),
        f'"{sys.executable}" -c "import pathlib; print(pathlib.Path(\'hello.txt\').read_text())"',
        timeout=15,
        ssh_host="remote-box",
    )
    assert res.exit_code == 0
    assert "world" in res.tail


def test_remote_ssh_error_not_masked_as_missing_dir(monkeypatch):
    from team.ssh import SshError, is_ssh_auth_error

    def _boom(ssh_host, action, path, **kw):
        raise SshError("SSH 連線失敗（exit 255）：Connection closed by 172.22.102.111 port 22")

    monkeypatch.setattr("team.ssh.remote_fs", _boom)
    assert is_ssh_auth_error("Connection closed by 172.22.102.111 port 22")
    with pytest.raises(gitops.GitError) as ei:
        gitops.ensure_local_git_repo("/home/bofmgr/rust_bof456_server", ssh_host="BOF456_NewServer")
    msg = str(ei.value)
    assert "SSH 遠端連線失敗" in msg
    assert "找不到目標資料夾" not in msg


def test_persistent_ssh_channel_reuses_single_process(tmp_path, monkeypatch):
    from team import ssh as ssh_mod

    spawn_count = {"n": 0}
    real_popen = ssh_mod.subprocess.Popen

    def fake_build_ssh_cmd(host, remote_argv, **kw):
        # 在本機直接用 Python 模擬遠端 _RPC_BOOTSTRAP_CMD 驗證常駐通道複用
        return [
            sys.executable,
            "-u",
            "-c",
            "import sys; s=sys.stdin.buffer.readline(); n=int(s); "
            "code=sys.stdin.buffer.read(n).decode('utf-8'); "
            "exec(compile(code,'<aea_rpc>','exec'), {'__name__':'__aea_rpc__'})",
        ]

    def counting_popen(*args, **kwargs):
        spawn_count["n"] += 1
        return real_popen(*args, **kwargs)

    monkeypatch.delenv("BUTTONAUTO_FAKE_SSH_LOCAL", raising=False)
    monkeypatch.setattr(ssh_mod, "build_ssh_cmd", fake_build_ssh_cmd)
    monkeypatch.setattr(ssh_mod.subprocess, "Popen", counting_popen)
    ssh_mod.close_ssh_channels("reuse-test-host")
    try:
        sub = tmp_path / "proj_reuse"
        sub.mkdir()
        r1 = ssh_mod.remote_fs("reuse-test-host", "stat", str(sub))
        r2 = ssh_mod.remote_fs("reuse-test-host", "stat", str(tmp_path))
        r3 = ssh_mod.remote_exec("reuse-test-host", str(sub), [sys.executable, "-c", "print('ok-rpc')"], shell=False)
        assert r1.get("isdir") is True
        assert r2.get("isdir") is True
        assert r3.get("ok") is True and "ok-rpc" in str(r3.get("stdout") or "")
        # 3 次遠端呼叫只啟動了 1 次常駐 SSH 通道，徹底避免 MaxStartups 連線風暴
        assert spawn_count["n"] == 1
        assert ssh_mod._is_transient_ssh_error("Connection timed out during banner exchange") is True
        assert ssh_mod._is_transient_ssh_error("Permission denied (publickey,password).") is False
    finally:
        ssh_mod.close_ssh_channels("reuse-test-host")
