"""tools/team_smoke.py：以假 CLI（--cli-override）驗證流程；不呼叫真 CLI、不連網。"""
import glob
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import team_smoke  # noqa: E402

AGENT = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")
FAKE_CLAUDE = os.path.join(ROOT, "team", "fakes", "fake_claude.py")
GLM_KEY = "SECRET-GLM-KEY-123"


def _scripts(tmp_path, glm=False):
    kiro = tmp_path / "kiro.json"
    kiro.write_text(json.dumps([[
        {"say": "開始"},
        {"tool": {"title": "write hello_kiro.txt", "kind": "edit",
                  "rawInput": {"path": "hello_kiro.txt"}}},
        {"write": {"path": "hello_kiro.txt", "content": "hello from kiro\n"}},
        {"say": "DONE"}]]), encoding="utf-8")
    cl = tmp_path / "claude.json"
    cl.write_text(json.dumps([[
        {"write": {"path": "hello_claude.txt", "content": "hello from claude\n"}},
        {"commit": "smoke: hello_claude.txt"},
        {"say": "DONE"}]]), encoding="utf-8")
    glm_s = tmp_path / "glm.json"
    glm_s.write_text(json.dumps([[
        {"write": {"path": "hello.txt", "content": "hello from glm\n"}},
        {"commit": "smoke: hello.txt"},
        {"say": "DONE"}]]), encoding="utf-8")
    return kiro, cl, glm_s


def _args(tmp_path, *extra):
    return ["--sandbox", str(tmp_path / "sb"), "--out-dir", str(tmp_path / "recon"),
            "--litellm", str(tmp_path / "nope.json"), *extra]


def _result_json(tmp_path):
    files = glob.glob(str(tmp_path / "recon" / "team-smoke-*.json"))
    assert len(files) == 1
    with open(files[0], encoding="utf-8") as f:
        return json.load(f)


def test_smoke_with_fake_clis(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    kiro, cl, _ = _scripts(tmp_path)
    code = team_smoke.main(_args(
        tmp_path,
        "--cli-override", "kiro=%s %s %s" % (sys.executable, AGENT, kiro),
        "--cli-override", "claude=%s %s --script %s" % (sys.executable, FAKE_CLAUDE, cl)))
    out = capsys.readouterr().out
    assert code == 0, out
    data = _result_json(tmp_path)
    by = {r["cli"]: r for r in data["results"]}
    assert set(by) == {"kiro", "claude"}
    assert all(r["ok"] for r in by.values())
    assert by["kiro"]["file_ok"] and by["kiro"]["stop_reason"] == "end_turn"
    assert by["kiro"]["events"] >= 2
    # edit 類權限請求：沒有模型設定 → 一律允許
    perms = by["kiro"]["permissions"]
    assert perms and perms[0]["outcome"] == "allow"
    assert "✅" in out
    assert open(tmp_path / "sb" / "hello_kiro.txt", encoding="utf-8").read().strip() == "hello from kiro"
    # sandbox 是 git repo 且有 commit
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path / "sb"),
                          capture_output=True).returncode == 0


def test_smoke_failure_exit_code_and_reason(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    _, cl, _ = _scripts(tmp_path)
    code = team_smoke.main(_args(
        tmp_path, "--only", "claude",
        "--cli-override", "claude=%s %s --script %s" % (sys.executable, FAKE_CLAUDE, cl)))
    out = capsys.readouterr().out
    assert code == 1
    assert "❌" in out and "claude" in out and "Traceback" not in out
    assert _result_json(tmp_path)["results"][0]["ok"] is False


def test_smoke_missing_cli(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    code = team_smoke.main(_args(tmp_path, "--only", "agy"))
    out = capsys.readouterr().out
    assert code == 1
    assert "找不到" in out


def test_smoke_wrong_file_content_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    cl = tmp_path / "c.json"
    cl.write_text(json.dumps([[{"write": {"path": "hello_claude.txt", "content": "oops"}}]]),
                  encoding="utf-8")
    code = team_smoke.main(_args(
        tmp_path, "--only", "claude",
        "--cli-override", "claude=%s %s --script %s" % (sys.executable, FAKE_CLAUDE, cl)))
    assert code == 1
    assert "內容" in capsys.readouterr().out


def _litellm(tmp_path):
    p = tmp_path / "litellm.local.json"
    p.write_text(json.dumps({"base_url": "http://x.invalid", "key": GLM_KEY,
                             "decision1_key": "D1-SECRET"}), encoding="utf-8")
    return str(p)


def test_smoke_glm_seat(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    _, _, glm_s = _scripts(tmp_path)
    code = team_smoke.main([
        "--seat", "glm", "--out-dir", str(tmp_path / "recon"), "--litellm", _litellm(tmp_path),
        "--cli-override", "claude=%s %s --script %s" % (sys.executable, FAKE_CLAUDE, glm_s)])
    out = capsys.readouterr().out
    assert code == 0, out
    r = _result_json(tmp_path)["results"][0]
    assert r["cli"] == "glm" and r["ok"] and r["file_ok"]
    assert GLM_KEY not in out


def test_smoke_glm_without_config(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    _, _, glm_s = _scripts(tmp_path)
    code = team_smoke.main([
        "--seat", "glm", "--out-dir", str(tmp_path / "recon"),
        "--litellm", str(tmp_path / "nope.json"),
        "--cli-override", "claude=%s %s --script %s" % (sys.executable, FAKE_CLAUDE, glm_s)])
    out = capsys.readouterr().out
    assert code == 1 and "litellm.local.json" in out


def test_glm_failure_reasons_are_clear():
    r = team_smoke.glm_reason("404 page not found POST /v1/messages")
    assert "/v1/messages" in r and "GLM 座位不可用" in r
    assert "金鑰" in team_smoke.glm_reason("401 Unauthorized invalid api key")


def test_failure_never_leaks_key():
    assert GLM_KEY not in team_smoke.mask("boom " + GLM_KEY, [GLM_KEY])


def test_probe_all(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    _, cl, _ = _scripts(tmp_path)
    code = team_smoke.main([
        "--probe-all", "--out-dir", str(tmp_path / "recon"), "--litellm", _litellm(tmp_path),
        "--cli-override", "claude=%s %s" % (sys.executable, FAKE_CLAUDE)])
    out = capsys.readouterr().out
    assert "QuotaBook" in out and "claude" in out
    data = _result_json(tmp_path)
    seats = {p["seat"]: p for p in data["probe"]}
    assert seats["claude"]["state"] == "ok"
    assert seats["glm"]["state"] == "ok"
    assert seats["kiro"]["state"] == "unavailable"
    assert GLM_KEY not in out
    assert code == 0
    assert seats["glm_high"]["state"] == "ok" and seats["agy"]["state"] == "unavailable"


def test_decide_without_models():
    from team.events import PermissionRequest
    opts = [{"optionId": "a", "name": "A", "kind": "allow_once"},
            {"optionId": "r", "name": "R", "kind": "reject_once"}]
    edit = PermissionRequest("1", "t", "edit", {"path": "x"}, opts)
    ex = PermissionRequest("2", "t", "execute", {"command": "ls"}, opts)
    assert team_smoke.decide_permission(edit, "/tmp", None, [])[0] == "a"
    assert team_smoke.decide_permission(ex, "/tmp", None, [])[0] == "r"


def test_bat_is_ascii_and_has_pieces():
    p = os.path.join(ROOT, "tools", "team_smoke.bat")
    raw = open(p, "rb").read()
    raw.decode("ascii")
    txt = raw.decode("ascii")
    assert "team_smoke.py" in txt and "%*" in txt and "timeout /t 30" in txt
    assert "chcp 65001" in txt and "Python313" in txt


# ---- fix round 1 ----
def _fake_claude_cmd(script):
    return "claude=%s %s --script %s" % (sys.executable, FAKE_CLAUDE, script)


def test_failure_has_raw_detail_in_console_and_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    _, cl, _ = _scripts(tmp_path)
    code = team_smoke.main(_args(tmp_path, "--only", "claude", "--cli-override", _fake_claude_cmd(cl)))
    out = capsys.readouterr().out
    assert code == 1
    assert "原始訊息：" in out and "not logged in" in out
    r = _result_json(tmp_path)["results"][0]
    assert "not logged in" in r["detail"] and len(r["detail"]) <= 1500
    assert r["rejected_methods"] == []


def test_error_event_with_key_is_masked_everywhere(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    s = tmp_path / "e.json"
    s.write_text(json.dumps([[{"error": "upstream said bad key %s" % GLM_KEY}]]), encoding="utf-8")
    code = team_smoke.main([
        "--seat", "glm", "--out-dir", str(tmp_path / "recon"), "--litellm", _litellm(tmp_path),
        "--cli-override", _fake_claude_cmd(s)])
    out = capsys.readouterr().out
    assert code == 1
    assert GLM_KEY not in out and "原始訊息：" in out
    raw = open(glob.glob(str(tmp_path / "recon" / "team-smoke-*.json"))[0], encoding="utf-8").read()
    assert GLM_KEY not in raw and "***" in raw


def test_rejected_methods_are_recorded():
    class C:
        def __init__(self):
            self.sent = []

        def _write(self, msg):
            self.sent.append(msg)

    class D:
        client = C()

    run = team_smoke._Run([])
    team_smoke._hook_rejections(D, run)
    D.client._write({"id": 1, "error": {"code": -32601, "message": "Method not found: fs/read_text_file"}})
    D.client._write({"id": 2, "result": {}})
    assert run.rejected == ["fs/read_text_file"]


def test_sandbox_refuses_nonempty_without_marker(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    sb = tmp_path / "sb"
    sb.mkdir()
    (sb / "precious.txt").write_text("x")
    _, cl, _ = _scripts(tmp_path)
    code = team_smoke.main(_args(tmp_path, "--only", "claude", "--cli-override", _fake_claude_cmd(cl)))
    out = capsys.readouterr().out
    assert code == 1 and "拒絕" in out
    assert not (sb / "hello_claude.txt").exists() and not (sb / ".git").exists()


def test_sandbox_marker_and_minimal_commit(tmp_path):
    sb = str(tmp_path / "sb")
    assert team_smoke.ensure_repo(sb) is None
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=sb,
                           capture_output=True, text=True).stdout.split()
    assert sorted(files) == [".ai-team-sandbox", "README.md"]
    assert team_smoke.ensure_repo(sb) is None  # 有標記檔：可重複使用


def test_tmp_dirs_removed_including_readonly(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    monkeypatch.setattr(tempfile_mod(), "tempdir", str(tmp_path / "tmp"))
    os.makedirs(tmp_path / "tmp")
    _, _, glm_s = _scripts(tmp_path)
    team_smoke.main(["--seat", "glm", "--probe-all", "--out-dir", str(tmp_path / "recon"),
                     "--litellm", _litellm(tmp_path), "--cli-override", _fake_claude_cmd(glm_s)])
    assert os.listdir(tmp_path / "tmp") == []


def tempfile_mod():
    import tempfile
    return tempfile


def test_cleanup_handles_readonly_files(tmp_path):
    d = tmp_path / "ats-x"
    (d / "objs").mkdir(parents=True)
    f = d / "objs" / "ro"
    f.write_text("x")
    os.chmod(f, 0o444)
    team_smoke.cleanup_tmp(str(d))
    assert not d.exists()


def test_catch_all_returns_1_without_traceback(tmp_path, capsys, monkeypatch):
    def boom():
        raise RuntimeError("kaboom " + GLM_KEY)
    monkeypatch.setattr(team_smoke, "discover", boom)
    code = team_smoke.main(_args(tmp_path))
    out = capsys.readouterr().out
    assert code == 1 and "測試工具本身發生錯誤" in out and "Traceback" not in out


def test_keyboard_interrupt_exit_130(tmp_path, capsys, monkeypatch):
    def boom():
        raise KeyboardInterrupt()
    monkeypatch.setattr(team_smoke, "discover", boom)
    assert team_smoke.main(_args(tmp_path)) == 130
    assert "已中斷" in capsys.readouterr().out


def test_glm_run_prints_handshake_and_permissions(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    _, _, glm_s = _scripts(tmp_path)
    team_smoke.main(["--seat", "glm", "--out-dir", str(tmp_path / "recon"),
                     "--litellm", _litellm(tmp_path), "--cli-override", _fake_claude_cmd(glm_s)])
    out = capsys.readouterr().out
    assert "啟動與交握：" in out and "權限請求：" in out
    assert _result_json(tmp_path)["results"][0]["handshake"] is True


def test_glm_end_turn_without_file_is_not_endpoint_problem(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    s = tmp_path / "n.json"
    s.write_text(json.dumps([[{"say": "DONE"}]]), encoding="utf-8")
    code = team_smoke.main(["--seat", "glm", "--out-dir", str(tmp_path / "recon"),
                            "--litellm", _litellm(tmp_path), "--cli-override", _fake_claude_cmd(s)])
    out = capsys.readouterr().out
    assert code == 1 and "模型沒有照指示建立檔案" in out
    assert "沒有 /v1/messages" not in out


def test_glm_reason_precision():
    g = team_smoke.glm_reason
    assert "/v1/messages" in g("API Error: 404 {\"detail\":\"Not Found\"} POST /v1/messages")
    assert "/v1/messages" not in g("some 404 elsewhere on /v1/chat/completions")
    assert "不認得模型" in g("400 invalid model glm-9")
    assert "不認得模型" in g("model `glm-5.3` not found")
    assert "/v1/messages" not in g("404 model glm-5.3 not found (messages)")
    assert "拒絕金鑰" in g("HTTP 401 Unauthorized")
    assert "模型沒有照指示" in g("", no_file=True)
    assert "原因不明" in g("weird thing")


def test_cli_reason_precision():
    c = team_smoke.cli_reason
    assert "登入" in c("kiro", "Authentication required, please log in")[0]
    assert "登入" not in c("kiro", "author wrote a command not found message")[0]
    assert "無法啟動" in c("kiro", "x", spawn_failed=True)[0]
    assert "無法啟動" not in c("kiro", "bash: foo: command not found")[0]
    assert "原因不明" in c("kiro", "")[0]


def test_probe_all_exit_code_and_states(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    code = team_smoke.main(["--probe-all", "--out-dir", str(tmp_path / "recon"),
                            "--litellm", _litellm(tmp_path),
                            "--cli-override", "claude=%s %s" % (sys.executable, FAKE_CLAUDE)])
    assert code == 1
    states = {p["seat"]: p["state"] for p in _result_json(tmp_path)["probe"]}
    assert states["claude"] == "failed" and states["kiro"] == "unavailable"


def test_summary_lists_skipped(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    _, cl, _ = _scripts(tmp_path)
    team_smoke.main(_args(tmp_path, "--cli-override", _fake_claude_cmd(cl)))
    out = capsys.readouterr().out
    assert "略過" in out and _result_json(tmp_path)["skipped"] == ["kiro", "agy"]


# ---- 公司網路憑證 ----
def test_cert_env_applied_to_override_child(tmp_path, capsys, monkeypatch):
    """--cli-override 的子程序也拿到 SSL_CERT_FILE（經 make_driver → with_cert_env）。"""
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    pem = str(tmp_path / "fake.pem")
    from team import drivers
    monkeypatch.setattr(drivers, "cert_env", lambda: {"SSL_CERT_FILE": pem, "NODE_EXTRA_CA_CERTS": pem})
    s = tmp_path / "e.json"
    s.write_text(json.dumps([[{"run": "echo x"}]]), encoding="utf-8")
    shown = tmp_path / "env.py"
    shown.write_text("import os,sys\nopen(sys.argv[1],'w').write(os.environ.get('SSL_CERT_FILE',''))\n")
    # fake_claude 的 run 步驟繼承環境：用 python 寫出 SSL_CERT_FILE
    out_file = tmp_path / "seen.txt"
    s.write_text(json.dumps([[{"run": "%s %s %s" % (sys.executable, shown, out_file)},
                              {"write": {"path": "hello_claude.txt", "content": "hello from claude"}},
                              {"commit": "smoke: hello_claude.txt"}]]),
                 encoding="utf-8")
    code = team_smoke.main(_args(tmp_path, "--only", "claude", "--cli-override", _fake_claude_cmd(s)))
    assert code == 0
    assert out_file.read_text() == pem
    assert "公司網路憑證：%s（已套用）" % pem in capsys.readouterr().out


def test_cert_line_not_applied(monkeypatch):
    from team import drivers
    monkeypatch.setattr(drivers, "cert_env", lambda: {})
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    assert "未套用" in team_smoke.cert_line()


def test_kiro_launch_uses_office_flags():
    from team.clis import launch_cmd
    assert launch_cmd(CliInfo_("kiro")) == ["k", "acp", "--agent-engine", "v3", "--auth-method", "cli"]


def CliInfo_(name):
    from team.clis import CliInfo
    return CliInfo(name, name, "k", "acp")


def test_network_class_before_login():
    c = team_smoke.cli_reason
    for txt in ("InvalidCertificate(UnknownIssuer) please sign in", "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
                "self signed certificate in chain", "dispatch failure", "read ECONNRESET", "getaddrinfo ENOTFOUND x",
                "ETIMEDOUT"):
        r, h = c("kiro", txt)
        assert r == team_smoke.NET_REASON and "acp-trace" in h, txt
    assert "登入" in c("kiro", "Sign in to Kiro")[0]


def test_kiro_signin_with_whoami_logged_in_is_network(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    r, h = team_smoke.cli_reason("kiro", "Sign in to Kiro", logged_in=True)
    assert "連不上服務" in r and "acp-trace" in h
    # run_cli 只在非 override 的 kiro 才呼叫 whoami
    calls = []
    monkeypatch.setattr(team_smoke, "whoami_logged_in", lambda info, timeout=15.0: calls.append(1) or True)
    s = tmp_path / "k.json"
    s.write_text(json.dumps([[{"error": "Sign in to Kiro"}]]), encoding="utf-8")
    code = team_smoke.main(_args(tmp_path, "--only", "kiro",
                                 "--cli-override", "kiro=%s %s %s" % (sys.executable, AGENT, s)))
    assert code == 1 and calls == []


@pytest.mark.skipif(os.name == "nt", reason="用 shebang 腳本當假 CLI")
def test_whoami_logged_in_parsing(tmp_path):
    from team.clis import CliInfo

    def mk(name, text, code=0):
        p = tmp_path / name
        p.write_text("#!%s\nimport sys\nprint(%r)\nsys.exit(%d)\n" % (sys.executable, text, code))
        p.chmod(0o755)
        return CliInfo("kiro", "K", str(p), "acp")
    assert team_smoke.whoami_logged_in(mk("a", "Logged in with Builder ID")) is True
    assert team_smoke.whoami_logged_in(mk("b", "Not logged in", 1)) is False
    assert team_smoke.whoami_logged_in(CliInfo("kiro", "K", str(tmp_path / "missing"), "acp")) is False


# ---- 最終審查第 1 項：claude／GLM 冒煙測試要求提交 ----
def test_smoke_claude_commit_checked(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    _, cl, _ = _scripts(tmp_path)
    assert team_smoke.main(_args(tmp_path, "--only", "claude", "--cli-override", _fake_claude_cmd(cl))) == 0
    sb = str(tmp_path / "sb")
    log = subprocess.run(["git", "log", "-1", "--format=%s"], cwd=sb, capture_output=True, text=True).stdout
    assert "smoke: hello_claude.txt" in log
    assert _result_json(tmp_path)["results"][0]["commit_ok"] is True
    # 第二次跑同一個 sandbox：先移除舊檔，代理的提交仍是新的
    for f in glob.glob(str(tmp_path / "recon" / "*.json")):
        os.remove(f)
    capsys.readouterr()
    assert team_smoke.main(_args(tmp_path, "--only", "claude", "--cli-override", _fake_claude_cmd(cl))) == 0


def test_smoke_claude_without_commit_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    s = tmp_path / "nc.json"
    s.write_text(json.dumps([[{"write": {"path": "hello_claude.txt", "content": "hello from claude"}},
                              {"say": "DONE"}]]), encoding="utf-8")
    code = team_smoke.main(_args(tmp_path, "--only", "claude", "--cli-override", _fake_claude_cmd(s)))
    out = capsys.readouterr().out
    assert code == 1 and "git log 看不到新的提交" in out and "--allowedTools" in out


def test_smoke_glm_commit_checked(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(team_smoke, "discover", lambda: {})
    s = tmp_path / "g.json"
    s.write_text(json.dumps([[{"write": {"path": "hello.txt", "content": "hello from glm"}},
                              {"say": "DONE"}]]), encoding="utf-8")
    code = team_smoke.main(["--seat", "glm", "--out-dir", str(tmp_path / "recon"),
                            "--litellm", _litellm(tmp_path), "--cli-override", _fake_claude_cmd(s)])
    out = capsys.readouterr().out
    assert code == 1 and "git log 看不到新的提交" in out
    assert _result_json(tmp_path)["results"][0]["commit_ok"] is False
