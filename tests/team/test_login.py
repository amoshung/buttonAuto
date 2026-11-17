"""登入流程與公司網路憑證。

- 辨識需要登入／網路或憑證的錯誤；同一座位共用一個 login 項目；開登入視窗、背景確認、自動繼續；
  你暫停的房間登入後不自動繼續；改用其他座位；後端重啟後清掉「確認中」狀態。
- CLI 說已登入或無法確認 → 網路／憑證項目（重試），不開登入視窗。
- netcerts：匯出 Windows 憑證成 PEM、逐鍵填 SSL_CERT_FILE／NODE_EXTRA_CA_CERTS、匯出狀態；所有 CLI 子程序都帶上。
- ACP：宣告終端機登入、記錄被拒絕的代理請求方法名。

真 git、假代理、ScriptedRouter；登入視窗、登入確認、憑證存放區一律注入假的，不跑真 CLI、不連網。
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from team import login
from team.acp import AcpClient
from team.clis import CliInfo
from team.demo import build_turn, plan_turn, spec_turn
from team.demo import create_demo_room
from team.drivers import seat_launch
from team.fakes.fake_models import ScriptedRouter
from team.fleet import QuotaBook
from team import netcerts
from team.drivers import make_driver
from team.login import check_login, is_login_error, login_command
from team.office import LOGIN_OPENED, Office
from team.rooms import RoomStore

SIGN_IN = [{"error": "Sign in to Kiro to send a prompt."}]
CLIS = {"kiro": CliInfo("kiro", "Kiro", r"C:\Kiro Cli\kiro-cli.exe", "acp"),
        "agy": CliInfo("agy", "Antigravity", r"C:\agy\agy.exe", "acp"),
        "claude": CliInfo("claude", "Claude", r"C:\u\claude.exe", "headless")}


def wait(cond, timeout=60, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


class Checker:
    """依序回傳結果（用完沿用最後一個）；記錄呼叫的座位。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, seat):
        with self.lock:
            self.calls.append(seat)
            ok = self.results[min(len(self.calls), len(self.results)) - 1]
        return ok, "已登入" if ok else "還沒登入"


class Env:
    def __init__(self, tmp_path, checker, max_running=2, **kw):
        self.store = RoomStore(str(tmp_path / "team"))
        self.launched = []
        self.checker = checker
        kw.setdefault("login_interval", 0.05)
        self.office = Office(self.store, lambda demo: ScriptedRouter(), lambda: [r"rm\s+-rf"],
                             lambda: max_running, lambda: None, clis_fn=lambda: {},
                             base_dir=str(tmp_path), model_cfg=None, runner_kw={"turn_timeout": 30},
                             login_launcher=lambda cmd, title: self.launched.append((cmd, title)),
                             login_checker=checker, **kw)

    def items(self, kind, rid=None, resolved=False):
        return [i for i in self.store.inbox(include_resolved=resolved)
                if i.kind == kind and (rid is None or i.room_id == rid or rid in i.rooms)]

    def room(self, rid):
        return self.store.get(rid)


@pytest.fixture
def envs():
    made = []
    yield made
    for e in made:
        e.office.stop(timeout=15)


# ---------------- 單元 ----------------
def test_is_login_error():
    for text in ["代理回傳錯誤（-32000）：Sign in to Kiro to send a prompt.", "You are signed out",
                 "Error: not logged in", "Please log in to continue", "Login required", "401 Unauthorized",
                 "HTTP 401", "Authentication failed", "authentication required", "token expired",
                 "Invalid API key · Please run /login", "請先登入", "帳號未登入"]:
        assert is_login_error(text), text
    for text in ["", "測試失敗", "port 4010 busy", "代理程序結束了（exit 1）", "請確認已在命令提示字元登入 kiro-cli"]:
        assert not is_login_error(text), text
    # 額度優先
    assert not is_login_error("401: usage limit reached, sign in later")


def test_login_command_and_check():
    cmd, how = login_command("kiro", CLIS)
    assert cmd == [r"C:\Kiro Cli\kiro-cli.exe", "login"] and "IAM Identity Center" in how
    cmd, how = login_command("claude", CLIS)
    assert cmd == [r"C:\u\claude.exe"] and "/login" in how
    cmd, how = login_command("agy", CLIS)
    assert cmd == [r"C:\agy\agy.exe"] and "Google" in how
    assert login_command("glm", CLIS) is None and login_command("glm_high", CLIS) is None
    with pytest.raises(ValueError):
        login_command("kiro", {})

    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        return outs.pop(0)

    outs = [(0, "Logged in with IAM Identity Center (secret-org)"), (0, "Not logged in"), (1, "")]
    assert check_login("kiro", CLIS, run=run) == (True, "已登入")
    assert check_login("kiro", CLIS, run=run)[0] is False
    assert check_login("kiro", CLIS, run=run)[0] is False
    assert calls[0] == [r"C:\Kiro Cli\kiro-cli.exe", "whoami"]
    outs = [(0, json.dumps({"type": "result", "is_error": False, "result": "OK"})),
            (0, json.dumps({"type": "result", "is_error": True, "result": "Invalid API key"}))]
    assert check_login("claude", CLIS, run=run)[0] is True
    assert check_login("claude", CLIS, run=run)[0] is False
    assert calls[-1][:3] == [r"C:\u\claude.exe", "-p", "回覆 OK"] and "--max-turns" in calls[-1]
    assert check_login("glm", CLIS, run=run)[0] is True

    def slow(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)
    ok, why = check_login("kiro", CLIS, run=slow)
    assert ok is None and "可能是網路或憑證" in why  # 無法確認，不是「沒登入」

    def boom(cmd, timeout):
        raise OSError("x")
    assert check_login("claude", CLIS, run=boom)[0] is None
    outs = [(1, "TypeError: fetch failed (UNABLE_TO_GET_ISSUER_CERT_LOCALLY)")]
    assert check_login("claude", CLIS, run=run)[0] is None


def test_open_login_window_uses_new_console(monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw

    monkeypatch.setattr(login, "IS_WINDOWS", True)
    monkeypatch.setattr(login.subprocess, "Popen", FakePopen)
    login.open_login_window([r"C:\Kiro Cli\kiro-cli.exe", "login"], 'Kiro 登入 & "x"')
    assert seen["kw"]["creationflags"] == getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    assert seen["cmd"].startswith('cmd.exe /s /k "title Kiro 登入')
    assert '"C:\\Kiro Cli\\kiro-cli.exe" login"' in seen["cmd"]
    assert "& \"x\"" not in seen["cmd"]  # 標題裡的特殊字元已去掉
    monkeypatch.setattr(login, "IS_WINDOWS", False)
    with pytest.raises(RuntimeError, match="Windows"):
        login.open_login_window(["kiro-cli", "login"], "Kiro")


def test_netcerts_bundle_and_env(tmp_path, monkeypatch):
    der = b"\x30\x82\x01\x0afake-der-certificate-bytes"
    calls = []

    def enum(store):
        calls.append(store)
        return [(der, "x509_asn", True), (der, "x509_asn", True), (b"junk", "pkcs_7_asn", True)]

    path = str(tmp_path / "sessions" / "windows-ca.pem")
    assert netcerts.windows_ca_bundle(path, enum) == path
    assert calls == ["ROOT", "CA"]
    text = open(path, encoding="ascii").read()
    assert text.count("BEGIN CERTIFICATE") == 1  # 去重、略過非 x509
    assert netcerts.windows_ca_bundle(path, enum) == path and calls == ["ROOT", "CA"]  # 24 小時內不重產
    old = time.time() - 25 * 3600
    os.utime(path, (old, old))
    assert netcerts.windows_ca_bundle(path, enum) == path and len(calls) == 4
    assert netcerts.windows_ca_bundle(str(tmp_path / "x.pem"), lambda s: []) is None
    monkeypatch.setattr(netcerts.ssl, "enum_certificates", None, raising=False)
    assert netcerts.windows_ca_bundle(str(tmp_path / "y.pem")) is None  # 非 Windows

    for k in netcerts.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    reg = {}
    monkeypatch.setattr(netcerts, "_registry_env", lambda k: reg.get(k, ""))
    env = netcerts.cert_env(str(tmp_path), enum)
    assert env == {"SSL_CERT_FILE": path, "NODE_EXTRA_CA_CERTS": path}
    assert netcerts.status()["state"] == "ok" and netcerts.status()["path"] == path
    # 逐鍵：只設了 NODE_EXTRA_CA_CERTS → SSL_CERT_FILE 仍然補上
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/corp/node.pem")
    assert netcerts.cert_env(str(tmp_path), enum) == {"SSL_CERT_FILE": path}
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS")
    # 登錄檔裡的使用者設定（還沒進 os.environ）：照使用者的值明確傳下去
    reg["SSL_CERT_FILE"] = "/corp/root.pem"
    assert netcerts.cert_env(str(tmp_path), enum) == {"SSL_CERT_FILE": "/corp/root.pem", "NODE_EXTRA_CA_CERTS": path}
    monkeypatch.setenv("SSL_CERT_FILE", "/corp/root.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/corp/node.pem")
    assert netcerts.cert_env(str(tmp_path), enum) == {}
    assert netcerts.user_cert_file() == "/corp/root.pem" and netcerts.status()["state"] == "user"

    # 重新產生失敗但舊檔有效 → 沿用舊檔；暫存檔名不固定
    os.utime(path, (old, old))
    real_replace = os.replace
    tmps = []

    def bad_replace(src, dst):
        tmps.append(src)
        raise OSError("locked")
    monkeypatch.setattr(netcerts.os, "replace", bad_replace)
    assert netcerts.windows_ca_bundle(path, enum) == path
    assert "沿用舊檔" in netcerts.status()["reason"] and netcerts.status()["state"] == "ok"
    assert tmps and tmps[0] != path + ".tmp" and str(os.getpid()) in tmps[0] and not os.path.exists(tmps[0])
    assert netcerts.windows_ca_bundle(str(tmp_path / "new.pem"), enum) is None
    assert netcerts.status()["state"] == "failed" and "無法寫入" in netcerts.status()["reason"]
    monkeypatch.setattr(netcerts.os, "replace", real_replace)


def test_every_driver_gets_cert_env(tmp_path, monkeypatch):
    import team.drivers as drivers_mod
    monkeypatch.setattr(drivers_mod.netcerts, "cert_env",
                        lambda *a, **k: {"SSL_CERT_FILE": "/c.pem", "NODE_EXTRA_CA_CERTS": "/c.pem"})
    acp = make_driver(CLIS["kiro"], str(tmp_path), lambda e: None, lambda r: "", False)
    assert acp.client.cmd == [r"C:\Kiro Cli\kiro-cli.exe", "acp", "--agent-engine", "v3", "--auth-method", "cli"]
    assert acp.client._env["SSL_CERT_FILE"] == "/c.pem"
    head = make_driver(CLIS["claude"], str(tmp_path), lambda e: None, lambda r: "", True,
                       env={"SSL_CERT_FILE": "/seat.pem", "ANTHROPIC_API_KEY": None})
    assert head.claude._env["SSL_CERT_FILE"] == "/seat.pem"  # 座位自己的設定優先
    assert head.claude._env["NODE_EXTRA_CA_CERTS"] == "/c.pem" and head.claude._env["ANTHROPIC_API_KEY"] is None
    assert "c.pem" not in repr(head.claude._env)

    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, "Logged in", "")

    monkeypatch.setattr(login, "cert_env", lambda: {"SSL_CERT_FILE": "/c.pem"})
    monkeypatch.setattr(login.subprocess, "run", fake_run)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    assert check_login("kiro", CLIS)[0] is True
    assert seen["SSL_CERT_FILE"] == "/c.pem"


def test_quota_reason(tmp_path):
    from datetime import datetime
    q = QuotaBook(str(tmp_path / "quota.json"))
    q.mark_exhausted("kiro", "", datetime.now(), reason="login")
    assert q.reason("kiro") == "login" and q.is_exhausted("kiro", None)
    assert QuotaBook(str(tmp_path / "quota.json")).snapshot()["kiro"]["reason"] == "login"
    q.mark_exhausted("kiro", "usage limit", datetime.now())
    assert q.reason("kiro") == ""


AGENT_SRC = r'''
import json, sys
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        caps = msg["params"]["clientCapabilities"]
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {},
              "authMethods": [{"id": "kiro-login", "name": "Log in to Kiro", "_meta": {"caps": caps}}]}})
    elif m == "session/new":
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "s1"}})
    elif m == "session/prompt":
        send({"jsonrpc": "2.0", "id": 900, "method": "_kiro/auth/getAccessToken", "params": {"scope": "x"}})
        send({"jsonrpc": "2.0", "id": 901, "method": "_kiro/auth/getAccessToken", "params": {"scope": "x"}})
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Sign in to Kiro to send a prompt."}})
    elif m is None and rid in (900, 901):
        sys.stderr.write("reply %s %s\n" % (rid, json.dumps(msg.get("error"))))
'''


def test_acp_declares_terminal_auth_and_logs_rejected_methods(tmp_path):
    script = tmp_path / "agent.py"
    script.write_text(AGENT_SRC, encoding="utf-8")
    events = []
    c = AcpClient([sys.executable, str(script)], str(tmp_path), events.append, lambda req: "")
    try:
        res = c.start(10)
        caps = res["authMethods"][0]["_meta"]["caps"]
        assert caps["terminal"] is False and caps["auth"] == {"terminal": True}
        assert caps["_meta"]["terminal-auth"] is True
        assert c.auth_methods == [{"id": "kiro-login", "name": "Log in to Kiro"}]
        c.new_session(10)
        with pytest.raises(Exception) as ei:
            c.prompt("hi", 10)
        assert is_login_error(str(ei.value))
        assert wait(lambda: c.rejected_methods == ["_kiro/auth/getAccessToken"], 5)
        notices = [e for e in events if e.kind == "notice"]
        assert len(notices) == 1 and "_kiro/auth/getAccessToken" in notices[0].text
        assert "scope" not in notices[0].text  # 不記參數
    finally:
        c.close()


# ---------------- 整合（Office＋房間執行緒＋假代理） ----------------
def test_sign_in_opens_login_window_and_resumes(tmp_path, envs):
    checker = Checker(False, False, True)  # 第 1 次：房間先問 whoami；之後背景確認第二次才成功
    env = Env(tmp_path, checker)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [SIGN_IN, spec_turn(), plan_turn(), build_turn()]})
    item = wait(lambda: next(iter(env.items("login", r.id)), None), 30)
    assert item is not None, env.room(r.id).error
    assert item.title == "Kiro 需要登入" and item.seat == "kiro" and item.rooms == [r.id]
    assert "Sign in to Kiro" in item.detail and "IAM Identity Center" in item.detail
    assert [o["id"] for o in item.options] == ["open", "check", "switch"]
    assert item.options[0]["label"] == "開啟登入視窗"
    assert wait(lambda: env.room(r.id).status == "waiting_user", 10)
    assert env.room(r.id).activity == "Kiro 需要登入"
    assert env.items("error", r.id) == []  # 不是錯誤
    assert checker.calls == ["kiro"]  # 先問一次 whoami：還沒登入才開登入項目
    time.sleep(0.3)
    assert len(checker.calls) == 1  # 沒按開啟前不會背景確認

    res = env.office.answer(item.id, "open")
    assert res == {"ok": True, "text": LOGIN_OPENED}
    assert env.launched == [(["demo-kiro", "login"], "Kiro 登入")]
    assert env.store.get_item(item.id).state == "opened"
    merge = wait(lambda: next(iter(env.items("merge", r.id)), None), 60)
    assert merge is not None, env.room(r.id).error
    assert len(checker.calls) == 3
    done = env.store.get_item(item.id)
    assert done.resolved and done.answer == "ok"
    texts = [e["text"] for e in env.store.events(r.id, limit=1000)]
    assert "Kiro 已登入，繼續工作" in texts
    assert env.items("error", r.id, resolved=True) == []
    assert not [i for i in env.store.inbox(include_resolved=True) if i.kind == "question"]  # 沒有網路憑證項目


def test_two_rooms_share_one_login_item(tmp_path, envs):
    checker = Checker(False)
    env = Env(tmp_path, checker, max_running=2)
    envs.append(env)
    env.office.start()
    script = {"kiro": [SIGN_IN, spec_turn(), plan_turn(), build_turn()]}
    r1 = create_demo_room(env.office, str(tmp_path), autonomy="bold", scripts=script)
    r2 = create_demo_room(env.office, str(tmp_path), autonomy="bold", scripts=script)
    assert wait(lambda: all(env.room(x.id).status == "waiting_user" for x in (r1, r2)), 30)
    items = env.items("login")
    assert len(items) == 1 and sorted(items[0].rooms) == sorted([r1.id, r2.id])
    snap = env.office.snapshot()
    assert [i["rooms"] for i in snap["inbox"] if i["kind"] == "login"][0] == items[0].rooms

    # 還沒登入：確認一次 → 說明更新，項目保留
    assert env.office.answer(items[0].id, "check")["ok"] is True
    assert wait(lambda: "還沒偵測到登入" in env.store.get_item(items[0].id).note, 10)
    assert not env.store.get_item(items[0].id).resolved
    checker.results = [True]
    assert env.office.answer(items[0].id, "check")["ok"] is True
    for x in (r1, r2):
        assert wait(lambda: next(iter(env.items("merge", x.id)), None), 60), env.room(x.id).error
    assert len(env.items("login", resolved=True)) == 1


def test_login_timeout_keeps_item(tmp_path, envs):
    env = Env(tmp_path, Checker(False), login_timeout=0.3)
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [SIGN_IN, spec_turn(), plan_turn(), build_turn()]})
    item = wait(lambda: next(iter(env.items("login", r.id)), None), 30)
    assert env.office.answer(item.id, "open")["ok"] is True
    assert env.store.get_item(item.id).state == "opened"
    cur = wait(lambda: (lambda i: i if i.state == "" and "已等" in i.note else None)(env.store.get_item(item.id)), 10)
    assert cur is not None and not cur.resolved and "還沒偵測到登入" in cur.note
    assert env.room(r.id).status == "waiting_user"


def test_switch_excludes_seat_and_continues(tmp_path, envs):
    env = Env(tmp_path, Checker(False))
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [SIGN_IN], "glm": [spec_turn(), plan_turn(), build_turn("C1: mul (glm)")]})
    item = wait(lambda: next(iter(env.items("login", r.id)), None), 30)
    assert item is not None, env.room(r.id).error
    assert "自動派座位" in item.detail and "60 分鐘" in item.detail
    res = env.office.answer(item.id, "switch")
    assert res["ok"] is True and "自動派座位" in res["text"] and "所有房間" in res["text"]
    assert env.office.demo_quota.reason("kiro") == "login"
    assert not env.office.quota.is_exhausted("kiro", None)  # 真座位不受影響
    merge = wait(lambda: next(iter(env.items("merge", r.id)), None), 60)
    assert merge is not None, env.room(r.id).error
    room = env.room(r.id)
    assert room.user_pinned is False and room.impl_seat != "kiro"
    assert env.store.get_item(item.id).answer == "switch"


def test_network_error_or_unknown_check_is_network_problem(tmp_path, envs):
    def raising(seat):
        raise RuntimeError("timeout")

    for checker, err, want_calls in [
            (Checker(False), "Sign in to Kiro: dispatch failure (InvalidCertificate(UnknownIssuer))", 0),
            (raising, "Sign in to Kiro to send a prompt.", None)]:
        env = Env(tmp_path / str(len(envs)), checker)
        envs.append(env)
        env.office.start()
        r = create_demo_room(env.office, str(tmp_path / str(len(envs))), autonomy="bold",
                             scripts={"kiro": [[{"error": err}], spec_turn(), plan_turn(), build_turn()]})
        q = wait(lambda: next(iter(env.items("question", r.id)), None), 30)
        assert q is not None, env.room(r.id).error
        assert q.title == "Kiro 連不上服務（可能是網路或憑證）" and "可能是網路或憑證" in q.detail
        assert env.items("login", resolved=True) == [] and env.launched == []
        if want_calls is not None:
            assert checker.calls == []  # 錯誤本身就是網路／憑證：不用問 whoami
        else:
            assert "無法確認 Kiro 的登入狀態" in q.detail
        env.office.pause(r.id)


def test_paused_room_not_resumed_after_login(tmp_path, envs):
    checker = Checker(False)
    env = Env(tmp_path, checker, max_running=2)
    envs.append(env)
    env.office.start()
    script = {"kiro": [SIGN_IN, spec_turn(), plan_turn(), build_turn()]}
    r1 = create_demo_room(env.office, str(tmp_path), autonomy="bold", scripts=script)
    r2 = create_demo_room(env.office, str(tmp_path), autonomy="bold", scripts=script)
    assert wait(lambda: all(env.room(x.id).status == "waiting_user" for x in (r1, r2)), 30)
    item = env.items("login")[0]
    env.office.pause(r2.id)  # 你暫停了第二間
    assert wait(lambda: env.room(r2.id).status == "paused", 10)
    assert env.store.get_item(item.id).paused == [r2.id]
    checker.results = [True]
    assert env.office.answer(item.id, "check")["ok"] is True
    assert wait(lambda: next(iter(env.items("merge", r1.id)), None), 60), env.room(r1.id).error
    time.sleep(0.5)
    assert env.room(r2.id).status == "paused"  # 不自動繼續
    texts = [e["text"] for e in env.store.events(r2.id, limit=1000)]
    assert "Kiro 已登入，按繼續接著做" in texts and "Kiro 已登入，繼續工作" not in texts


def test_restart_paused_room_resumes_and_opened_state_reset(tmp_path, envs):
    from team.runner import OPT_LOGIN
    env = Env(tmp_path, Checker(True), max_running=0)
    envs.append(env)
    a = create_demo_room(env.office, str(tmp_path))
    b = create_demo_room(env.office, str(tmp_path))
    env.store.update(a.id, lambda x: (setattr(x, "status", "waiting_user")))
    env.store.update(b.id, lambda x: (setattr(x, "status", "paused"), setattr(x, "activity", "已暫停")))
    item, _ = env.store.join_login(a.id, "kiro", "demo", "Kiro 需要登入", "d", OPT_LOGIN)
    env.store.join_login(b.id, "kiro", "demo", "Kiro 需要登入", "d", OPT_LOGIN)
    env.store.update_item(item.id, lambda x: setattr(x, "state", "opened"))
    env.office.start()  # 後端重啟：a 改為暫停（重新啟動），確認中的狀態清掉
    cur = env.store.get_item(item.id)
    assert cur.state == "" and "重新啟動" in cur.note
    assert env.room(a.id).status == "paused" and env.room(a.id).activity.startswith("後端重新啟動")
    env.office._login_succeeded(cur)
    assert env.room(a.id).status == "queued"  # 因重啟而暫停：自動繼續
    assert env.room(b.id).status == "paused"  # 你自己暫停的：不動
    assert "Kiro 已登入，按繼續接著做" in [e["text"] for e in env.store.events(b.id, limit=100)]


def test_whoami_ok_but_acp_sign_in_is_network_problem(tmp_path, envs):
    env = Env(tmp_path, Checker(True))
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [SIGN_IN, spec_turn(), plan_turn(), build_turn()]})
    q = wait(lambda: next(iter(env.items("question", r.id)), None), 30)
    assert q is not None, env.room(r.id).error
    assert q.title == "Kiro 連不上服務（可能是網路或憑證）"
    assert "Kiro 顯示已登入" in q.detail and "SSL_CERT_FILE" in q.detail and "Sign in to Kiro" in q.detail
    assert [o["id"] for o in q.options] == ["retry", "pause"] and q.options[0]["label"] == "重試"
    assert env.items("login", resolved=True) == [] and env.launched == []  # 不開登入視窗
    assert env.items("error", r.id) == []
    assert wait(lambda: env.office.answer(q.id, "retry")["ok"], 10)
    merge = wait(lambda: next(iter(env.items("merge", r.id)), None), 60)
    assert merge is not None, env.room(r.id).error


def test_quota_error_wins_over_login(tmp_path, envs):
    env = Env(tmp_path, Checker(False))
    envs.append(env)
    env.office.start()
    quota = [{"error": "401 You've hit your usage limit, try again in 1 seconds. Sign in later."}]
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [spec_turn(), plan_turn(), quota]})
    assert wait(lambda: next(iter(env.items("merge", r.id)), None), 60), env.room(r.id).error
    assert env.items("login", resolved=True) == []
    assert env.room(r.id).fallback_from == "kiro" or env.room(r.id).impl_seat == "glm"


# ---- 最終審查第 6 項：沒有登入字樣的純 TLS／網路錯誤也走網路與憑證說明 ----
@pytest.mark.parametrize("err", ["dispatch failure (InvalidCertificate(UnknownIssuer))",
                                 "error sending request: dispatch failure"])
def test_pure_network_error_turn_is_network_problem(tmp_path, envs, err):
    env = Env(tmp_path, Checker(True))
    envs.append(env)
    env.office.start()
    r = create_demo_room(env.office, str(tmp_path), autonomy="bold",
                         scripts={"kiro": [[{"error": err}], spec_turn(), plan_turn(), build_turn()]})
    q = wait(lambda: next(iter(env.items("question", r.id)), None), 30)
    assert q is not None, env.room(r.id).error
    assert q.title == "Kiro 連不上服務（可能是網路或憑證）" and "連線失敗，看起來是網路或憑證問題" in q.detail
    assert env.items("error", r.id) == [] and env.launched == []
    assert wait(lambda: env.office.answer(q.id, "retry")["ok"], 10)
    merge = wait(lambda: next(iter(env.items("merge", r.id)), None), 60)
    assert merge is not None, env.room(r.id).error
