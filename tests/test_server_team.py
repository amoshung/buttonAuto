"""後端 /api/team/*：驗證、示範房跑到合併、掃描、艦隊、SSE。不連網、不呼叫真 CLI。"""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import pytest

import automation
import server
from projects import ProjectDetector
from sessions import SessionStore

TOKEN = "T" * 24
GLM_KEY = "SECRET-GLM-KEY-123"
D1_KEY = "SECRET-D1-KEY-456"


class NoWins(ProjectDetector):
    def __init__(self):
        pass

    def detect(self):
        return []

    def describe_hwnd(self, hwnd):
        return None


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    store = SessionStore(root=str(tmp_path / "sessions"))
    monkeypatch.setattr(automation, "SessionStore", lambda: store)
    with open(tmp_path / "litellm.local.json", "w", encoding="utf-8") as f:
        json.dump({"base_url": "http://models.invalid", "key": GLM_KEY, "decision1_key": D1_KEY}, f)
    probes = []
    app = server.AssistantServer(
        config_path=str(tmp_path / "config.json"), secrets_path=str(tmp_path / "s.json"),
        detector=NoWins(), start_threads=False, token=TOKEN,
        team_root=str(tmp_path / "team"), team_base_dir=str(tmp_path),
        team_opts={"clis_fn": lambda: {}, "runner_kw": {"turn_timeout": 30},
                   "prober": lambda seat: probes.append(seat) or True, "probe_interval": 3600})
    app.probes = probes
    app.team.start()
    httpd = app.serve(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app
    app.team.stop(timeout=10)
    httpd.shutdown()
    httpd.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _req(app, path, body=None, cookie=True, token=True):
    headers = {"Host": f"127.0.0.1:{app.port}"}
    if cookie:
        headers["Cookie"] = "aea_t=" + TOKEN
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        if token:
            headers["X-Token"] = TOKEN
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def J(app, path, body=None, **kw):
    code, data = _req(app, path, body, **kw)
    return code, json.loads(data)


def wait(cond, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.1)
    return None


def git_repo(path, dirty=False):
    os.makedirs(path)
    run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", *a], cwd=path,  # noqa: E731
                                    check=True, capture_output=True)
    run("init", "-q")
    with open(os.path.join(path, "progress.md"), "w", encoding="utf-8") as f:
        f.write("# 進度\n\n## 進行中\n- [ ] 加入 mul 函式\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")


def test_team_state_requires_cookie(srv):
    assert _req(srv, "/api/team/state", cookie=False)[0] == 401
    code, snap = J(srv, "/api/team/state")
    assert code == 200 and snap["rooms"] == [] and snap["inbox"] == []
    assert snap["usage"]["models_ready"] is True


def test_models_not_ready_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "SessionStore", lambda: SessionStore(root=str(tmp_path / "sessions")))
    app = server.AssistantServer(config_path=str(tmp_path / "c.json"), secrets_path=str(tmp_path / "s.json"),
                                 detector=NoWins(), start_threads=False, token=TOKEN,
                                 team_root=str(tmp_path / "team"), team_base_dir=str(tmp_path),
                                 team_opts={"clis_fn": lambda: {}})
    assert app.team_snapshot()["usage"]["models_ready"] is False


def test_create_room_validation(srv, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    body = {"repo": str(plain), "goal": "做點事", "source": "goal"}
    assert _req(srv, "/api/team/rooms", body, token=False)[0] == 401
    code, res = J(srv, "/api/team/rooms", body)
    assert res["ok"] is False and "不是 git repo" in res["text"]
    code, res = J(srv, "/api/team/rooms", {"goal": "x"})
    assert res["ok"] is False
    code, res = J(srv, "/api/team/rooms", {"repo": str(plain), "goal": "x", "allowed_paths": "oops"})
    assert code == 400 and res["ok"] is False
    assert J(srv, "/api/team/state")[1]["rooms"] == []


def test_create_room_ok(srv, tmp_path):
    repo = str(tmp_path / "repo")
    git_repo(repo)
    code, res = J(srv, "/api/team/rooms", {"repo": repo, "goal": "加入 mul", "source": "progress.md",
                                           "impl_cli": "auto", "review_cli": "auto", "autonomy": "balanced",
                                           "allowed_paths": [], "name": "測試房"})
    assert code == 200 and res["ok"] is True and res["room"]["name"] == "測試房"
    rid = res["room"]["id"]
    code, d = J(srv, "/api/team/room?id=" + rid)
    assert code == 200 and d["room"]["id"] == rid and isinstance(d["events"], list) and d["events"]
    code, projs = J(srv, "/api/team/projects")
    assert code == 200 and any(os.path.normcase(p["path"]) == os.path.normcase(repo) for p in projs["projects"])


def test_room_and_action_errors(srv):
    assert J(srv, "/api/team/room?id=../x")[0] in (400, 404)
    assert J(srv, "/api/team/room?id=r-doesnotexist")[0] in (400, 404)
    code, res = J(srv, "/api/team/rooms/pause", {"id": "nope"})
    assert code in (400, 404) and res["ok"] is False and "traceback" not in json.dumps(res).lower()
    assert _req(srv, "/api/team/rooms/explode", {"id": "x"})[0] == 404
    assert J(srv, "/api/team/rooms/pause", {})[0] == 400
    assert J(srv, "/api/team/answer", {"item_id": "zzz", "answer": "x"})[0] in (400, 404)
    assert J(srv, "/api/team/send", {"id": "nope", "text": "hi"})[0] in (400, 404)


def test_scan_endpoint(srv, tmp_path):
    repo = str(tmp_path / "scanme")
    git_repo(repo)
    code, res = J(srv, "/api/team/scan?path=" + urllib.request.quote(repo))
    assert code == 200 and "mul" in res["progress"]["task"] and res["problem"] == ""
    assert J(srv, "/api/team/scan?path=" + urllib.request.quote(str(tmp_path / "missing")))[0] == 400
    assert J(srv, "/api/team/scan?path=" + urllib.request.quote("\\\\host\\share\\x"))[0] == 400
    assert J(srv, "/api/team/scan?path=" + urllib.request.quote("https://x.invalid/a"))[0] == 400
    assert J(srv, "/api/team/scan")[0] == 400


def test_demo_room_runs_to_merge_via_api(srv):
    code, res = J(srv, "/api/team/demo", {})
    assert code == 200 and res["ok"] is True
    rid = res["room"]["id"]

    def done():
        snap = J(srv, "/api/team/state")[1]
        room = next(r for r in snap["rooms"] if r["id"] == rid)
        if room["status"] == "error":
            pytest.fail(room["error"])
        return snap if room["status"] == "done" else None
    snap = wait(done, 60)
    assert snap is not None
    merge = next((i for i in snap["inbox"] if i["room_id"] == rid and i["kind"] == "merge"), None)
    assert merge is not None
    code, res = J(srv, "/api/team/answer", {"item_id": merge["id"], "answer": "merge", "text": ""})
    assert code == 200 and res["ok"] is True, res
    room = J(srv, "/api/team/room?id=" + rid)[1]["room"]
    assert room["status"] == "done" and "已合併" in room["activity"]


def test_pause_send_archive(srv, tmp_path):
    srv.team.max_running_fn = lambda: 0  # 不要真的開工
    repo = str(tmp_path / "r2")
    git_repo(repo)
    rid = J(srv, "/api/team/rooms", {"repo": repo, "goal": "g", "source": "goal"})[1]["room"]["id"]
    assert J(srv, "/api/team/send", {"id": rid, "text": "請用中文"})[1]["ok"] is True
    assert J(srv, "/api/team/send", {"id": rid, "text": " "})[1]["ok"] is False
    assert J(srv, "/api/team/rooms/pause", {"id": rid})[1]["ok"] is True
    assert J(srv, "/api/team/room?id=" + rid)[1]["room"]["status"] == "paused"
    assert J(srv, "/api/team/rooms/resume", {"id": rid})[1]["ok"] is True
    assert J(srv, "/api/team/rooms/pause", {"id": rid})[1]["ok"] is True
    assert J(srv, "/api/team/rooms/archive", {"id": rid})[1]["ok"] is True
    assert J(srv, "/api/team/room?id=" + rid)[1]["room"]["status"] == "archived"


def test_fleet_endpoints_no_secrets(srv):
    srv.team.quota.mark_exhausted("kiro", "quota exceeded", datetime.now())
    code, fl = J(srv, "/api/team/fleet")
    assert code == 200 and fl["fleet"]["kiro"]["quota"]["state"] != "ok" and "kiro" in fl["catalog"]
    assert J(srv, "/api/team/fleet/probe", {"seat": "kiro"})[1]["ok"] is True
    assert wait(lambda: srv.probes == ["kiro"], 10)
    assert wait(lambda: J(srv, "/api/team/fleet")[1]["fleet"]["kiro"]["quota"]["state"] == "ok", 10)
    srv.team.quota.mark_exhausted("agy", "quota exceeded", datetime.now())
    assert J(srv, "/api/team/fleet/reset", {"seat": "agy"})[1]["ok"] is True
    assert J(srv, "/api/team/fleet")[1]["fleet"]["agy"]["quota"]["state"] == "ok"
    assert J(srv, "/api/team/fleet/probe", {"seat": "nope"})[0] == 400
    assert J(srv, "/api/team/fleet/reset", {"seat": "nope"})[0] == 400
    assert _req(srv, "/api/team/fleet/reset", {"seat": "agy"}, token=False)[0] == 401
    J(srv, "/api/team/demo", {})
    for path in ("/api/team/state", "/api/team/fleet", "/api/team/projects"):
        raw = _req(srv, path)[1].decode("utf-8")
        assert GLM_KEY not in raw and D1_KEY not in raw
    rid = J(srv, "/api/team/state")[1]["rooms"][0]["id"]
    for path in ("/api/team/room?id=" + rid, "/api/team/scan?path=" + urllib.request.quote(srv.team.base_dir),
                 "/api/state"):
        raw = _req(srv, path)[1].decode("utf-8")
        assert GLM_KEY not in raw and D1_KEY not in raw


def test_sse_team_event(srv):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/stream",
                                 headers={"Host": f"127.0.0.1:{srv.port}", "Cookie": "aea_t=" + TOKEN})
    seen = []
    with urllib.request.urlopen(req, timeout=15) as r:
        def read_until(name):
            for raw in r:
                line = raw.decode("utf-8").rstrip("\n")
                if line.startswith("event: "):
                    seen.append(line[7:])
                if line.startswith("data: ") and seen and seen[-1] == name:
                    return json.loads(line[6:])
        first = read_until("team")
        assert first is not None and "rooms" in first
        J(srv, "/api/team/demo", {})
        second = read_until("team")
        assert second is not None and len(second["rooms"]) == 1


def test_team_settings_fields(srv):
    code, res = J(srv, "/api/settings/ai", {"team_max_running": 99, "team_cost_weight": -1,
                                            "team_context_rollover": 0.5, "team_plugin_dirs": "C:/a;C:/b"})
    assert res["ok"] is True
    assert srv.cfg.team_max_running == 16 and srv.cfg.team_cost_weight == 0.0
    assert srv.cfg.team_context_rollover == 0.5 and srv.cfg.team_plugin_dirs == "C:/a;C:/b"
    assert srv.team.max_running_fn() == 16
    J(srv, "/api/settings/ai", {"team_plugin_dirs": ""})
    assert srv.cfg.team_plugin_dirs == ""


@pytest.mark.parametrize("bad", ["/\\server\\share", "\\/server\\share", "//server/share",
                                 "\\\\server\\share\\x", "relative/dir", "https://x.invalid/a", "x" * 1100])
def test_path_guard_scan_and_create(srv, bad):
    code, res = J(srv, "/api/team/scan?path=" + urllib.request.quote(bad))
    assert code == 400 and "完整路徑" in res["text"]
    code, res = J(srv, "/api/team/rooms", {"repo": bad, "goal": "g"})
    assert code == 400 and "完整路徑" in res["text"]


def test_string_fields_and_no_repr(srv):
    assert J(srv, "/api/team/send", {"id": "r-x", "text": None})[0] in (400, 404)
    rid = J(srv, "/api/team/demo", {})[1]["room"]["id"]
    assert J(srv, "/api/team/send", {"id": rid, "text": None})[0] == 400
    assert J(srv, "/api/team/send", {"id": rid, "text": 5})[0] == 400
    code, res = J(srv, "/api/team/answer", {"item_id": "x", "answer": None})
    assert code in (400, 404)
    srv.team.pause = lambda r: (_ for _ in ()).throw(RuntimeError("/secret/path boom"))
    code, res = J(srv, "/api/team/rooms/pause", {"id": rid})
    assert code == 500 and "secret" not in res["text"] and "RuntimeError" not in res["text"]


def test_sse_survives_snapshot_error_and_dedupes(srv):
    real = srv.team.snapshot
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real()
    srv.team.snapshot = flaky
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/stream",
                                 headers={"Host": f"127.0.0.1:{srv.port}", "Cookie": "aea_t=" + TOKEN})
    with urllib.request.urlopen(req, timeout=4) as r:
        team_events = 0
        end = time.time() + 6
        try:
            for raw in r:
                if raw.decode("utf-8").startswith("event: team"):
                    team_events += 1
                if time.time() > end:
                    break
                if team_events == 1:
                    srv.hub.bump()
        except (TimeoutError, OSError):
            pass
    assert calls["n"] >= 2 and team_events == 1


def test_team_snapshot_cached_per_version(srv):
    real = srv.team.snapshot
    n = {"c": 0}
    srv.team.snapshot = lambda: (n.__setitem__("c", n["c"] + 1), real())[1]
    srv.team_snapshot()
    srv.team_snapshot()
    assert n["c"] == 1
    srv.hub.bump()
    srv.team_snapshot()
    assert n["c"] == 2


def test_probe_now_false_after_stop(srv):
    srv.team.stop()
    assert srv.team.probe_now("kiro") is False


def test_pause_all(srv, tmp_path):
    srv.team.max_running_fn = lambda: 0  # 房間停在排隊中
    ids = []
    for n in ("pa1", "pa2"):
        repo = str(tmp_path / n)
        git_repo(repo)
        ids.append(J(srv, "/api/team/rooms", {"repo": repo, "goal": "g", "source": "goal"})[1]["room"]["id"])
    assert _req(srv, "/api/team/pause_all", {}, token=False)[0] == 401
    code, res = J(srv, "/api/team/pause_all", {})
    assert code == 200 and res["ok"] is True and res["paused"] == 2
    rooms = {r["id"]: r for r in J(srv, "/api/team/state")[1]["rooms"]}
    assert all(rooms[i]["status"] == "paused" for i in ids)
    code, res = J(srv, "/api/team/pause_all", {})
    assert res["ok"] is True and res["paused"] == 0 and "沒有" in res["text"]


def test_usage_endpoint_after_demo(srv):
    code, res = J(srv, "/api/team/demo", {})
    rid = res["room"]["id"]

    def done():
        room = next(r for r in J(srv, "/api/team/state")[1]["rooms"] if r["id"] == rid)
        if room["status"] == "error":
            pytest.fail(room["error"])
        return room if room["status"] == "done" else None
    room = wait(done, 60)
    assert room is not None and "inflight" in room and room["tokens"]
    assert _req(srv, "/api/team/usage", cookie=False)[0] == 401
    code, u = J(srv, "/api/team/usage")
    assert code == 200 and u["models_ready"] is True and u["glm_model"] == "glm-5.3"
    s = u["session"]
    assert s["models"] and all(m["model"] == "scripted" and m["label"].startswith("示範（假模型，代 ") for m in s["models"])
    assert s["cli"] and all(c["demo"] for c in s["cli"])
    assert s["rooms"][0]["room_id"] == rid and s["rooms"][0]["model_calls"] > 0
    assert {r["role"] for r in s["roles"]} >= {"policy", "po", "reviewer"}
    raw = json.dumps(u)
    assert GLM_KEY not in raw and D1_KEY not in raw
    # 示範紀錄不寫進真正的用量帳
    path = srv.team.usage.file_for(datetime.now().date().isoformat())
    assert not os.path.exists(path) or "scripted" not in open(path, encoding="utf-8").read()
    evs = J(srv, "/api/team/room?id=" + rid)[1]["events"]
    assert any(e["kind"] == "model" for e in evs) and any(e["kind"] == "cli_usage" for e in evs)
    a = next(e for e in evs if e["kind"] == "assign")
    assert a["by"] == "user" and a["reason"] == "使用者指定"  # 示範房指定 Kiro／Antigravity


# ---------------------------------------------------------------- Task 18：使用者選模型
KIRO_IDS = ["auto", "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "gpt-5.6-sol", "gpt-5.6-terra",
            "gpt-5.6-luna", "grok-4.7", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4.5"]


@pytest.fixture()
def msrv(tmp_path, monkeypatch):
    """有 kiro／claude 的伺服器（路徑不存在，房間會失敗，不影響模型 API）；偵測用注入的假函式。"""
    from team.clis import CliInfo
    store = SessionStore(root=str(tmp_path / "sessions"))
    monkeypatch.setattr(automation, "SessionStore", lambda: store)
    with open(tmp_path / "litellm.local.json", "w", encoding="utf-8") as f:
        json.dump({"base_url": "http://models.invalid", "key": GLM_KEY, "decision1_key": D1_KEY}, f)
    gate = threading.Event()
    calls = []

    def detector(seat):
        calls.append(seat)
        gate.wait(10)
        return {"models": [{"modelId": m, "name": m.upper(), "description": "d"} for m in KIRO_IDS],
                "current": "auto"}
    clis = {"kiro": CliInfo("kiro", "Kiro", "/nonexistent/kiro-cli", "acp"),
            "claude": CliInfo("claude", "Claude", "/nonexistent/claude", "headless")}
    app = server.AssistantServer(
        config_path=str(tmp_path / "config.json"), secrets_path=str(tmp_path / "s.json"),
        detector=NoWins(), start_threads=False, token=TOKEN,
        team_root=str(tmp_path / "team"), team_base_dir=str(tmp_path),
        team_opts={"clis_fn": lambda: dict(clis), "runner_kw": {"turn_timeout": 30},
                   "prober": lambda seat: True, "probe_interval": 3600, "model_detector": detector})
    app.gate, app.detect_calls = gate, calls
    app.team.start()
    httpd = app.serve(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app
    gate.set()
    app.team.stop(timeout=10)
    httpd.shutdown()
    httpd.server_close()


def test_models_api_detect_save_and_room_model(msrv, tmp_path):
    app = msrv
    # 啟動時偵測一次（kiro 已安裝；agy 沒裝 → 不偵測）；偵測中再按不會開第二個
    assert wait(lambda: app.detect_calls == ["kiro"], 10)
    code, v = J(app, "/api/team/models")
    assert code == 200 and v["seats"]["kiro"]["detecting"] is True and v["seats"]["kiro"]["known"] is False
    code, r = J(app, "/api/team/models/detect", {"seat": "kiro"})
    assert r["ok"] is True and r["started"] == [] and "偵測中" in r["text"]
    assert J(app, "/api/team/models/detect", {"seat": "agy"})[1]["ok"] is False
    app.gate.set()
    assert wait(lambda: not J(app, "/api/team/models")[1]["seats"]["kiro"]["detecting"], 10)
    assert app.detect_calls == ["kiro"]
    v = J(app, "/api/team/models")[1]
    kiro = v["seats"]["kiro"]
    assert [m["id"] for m in kiro["models"]] == KIRO_IDS and kiro["current"] == "auto" and kiro["detected_at"]
    assert kiro["source"] == "detected" and v["seats"]["claude"]["source"] == "catalog"
    assert v["seats"]["glm"]["editable"] is False and v["purposes"]["permission"] == "d1+glm"
    assert {o["id"] for o in v["purpose_options"]["notes"]["options"]} == {"glm_low", "glm_normal", "off"}
    assert os.path.exists(tmp_path / "team" / "models_cache.json")
    # 存全域設定
    code, r = J(app, "/api/team/models", {"seats": {"kiro": "claude-sonnet-5.5", "claude": "opus"},
                                          "purposes": {"judge": "glm"},
                                          "models": {"kiro": [{"id": "claude-haiku-5.5", "tier": "economy", "cost": 1}]}})
    assert r["ok"] is True, r
    assert r["models"]["seats"]["kiro"]["pref"] == "claude-sonnet-5.5"
    assert [m for m in r["models"]["seats"]["kiro"]["models"] if m["id"] == "claude-haiku-5.5"][0]["tier"] == "economy"
    code, r = J(app, "/api/team/models", {"seats": {"kiro": "gpt-9"}})
    assert r["ok"] is False and "不在" in r["text"]
    saved = (tmp_path / "team_models.json").read_text(encoding="utf-8")
    assert GLM_KEY not in saved and D1_KEY not in saved and "gpt-9" not in saved
    assert app.team._router(False).purposes is app.team.prefs  # 真的路由器依設定頁的用途選模型
    assert app.team._router(False)._purpose("judge") == "glm"
    # 房間：建立時指定座位＋模型；中途改模型
    repo = str(tmp_path / "repo")
    git_repo(repo)
    code, r = J(app, "/api/team/rooms", {"repo": repo, "goal": "加入 mul", "impl_cli": "kiro", "impl_model": "gpt-9"})
    assert r["ok"] is False and "不在" in r["text"]
    code, r = J(app, "/api/team/rooms", {"repo": repo, "goal": "加入 mul", "impl_cli": "kiro",
                                         "impl_model": "claude-haiku-5.5", "review_cli": "claude"})
    assert r["ok"] is True, r
    rid = r["room"]["id"]
    assert r["room"]["impl_pref"] == {"seat": "kiro", "model": "claude-haiku-5.5"}
    assert r["room"]["review_pref"] == {"seat": "claude", "model": ""}
    code, r = J(app, "/api/team/rooms/model", {"id": rid, "role": "impl", "seat": "kiro", "model": "claude-sonnet-5.5"})
    assert r["ok"] is True and r["room"]["impl_pref"]["model"] == "claude-sonnet-5.5"
    ev = J(app, "/api/team/room?id=" + rid)[1]["events"]
    assert any(e["kind"] == "prefs" and "你把實作模型改成 claude-sonnet-5.5" in e["text"] for e in ev)
    for bad, why in (({"role": "impl", "seat": "kiro", "model": "gpt-9"}, "不在"),
                     ({"role": "impl", "seat": "claude"}, "不同的座位"),
                     ({"role": "impl", "seat": "auto", "model": "opus"}, "先選座位"),
                     ({"role": "boss", "seat": "kiro"}, "role")):
        code, r = J(app, "/api/team/rooms/model", dict(bad, id=rid))
        assert r["ok"] is False and why in r["text"], (bad, r)
    code, r = J(app, "/api/team/rooms/model", {"id": rid, "role": "review", "seat": "auto", "model": ""})
    assert r["ok"] is True and r["room"]["review_pref"] == {} and r["room"]["review_cli"] == "auto"
    assert J(app, "/api/team/rooms/model", {"id": "r00000000", "role": "impl"})[0] == 404


def test_models_endpoints_need_token(msrv):
    for path, body in (("/api/team/models", {"purposes": {"notes": "off"}}), ("/api/team/models/detect", {}),
                       ("/api/team/rooms/model", {"id": "r00000000", "role": "impl"})):
        assert _req(msrv, path, body, token=False)[0] == 401
    assert J(msrv, "/api/team/models")[1]["purposes"]["notes"] == "glm_low"


def test_room_delegate_endpoint(srv):
    rid = J(srv, "/api/team/demo", {})[1]["room"]["id"]
    code, r = J(srv, "/api/team/rooms/delegate", {"id": rid, "on": True})
    assert code == 200 and r["ok"] is True and r["room"]["delegate"] is True and r["text"] == "已開啟代管"
    assert any(x["id"] == rid and x["delegate"] for x in J(srv, "/api/team/state")[1]["rooms"])
    assert J(srv, "/api/team/rooms/delegate", {"id": rid, "on": "yes"})[0] == 400
    assert J(srv, "/api/team/rooms/delegate", {"id": "r00000000", "on": True})[0] == 404
    assert _req(srv, "/api/team/rooms/delegate", {"id": rid, "on": False}, token=False)[0] == 401
    code, r = J(srv, "/api/team/rooms/delegate", {"id": rid, "on": False})
    assert r["ok"] is True and r["room"]["delegate"] is False
    ev = J(srv, "/api/team/room?id=" + rid)[1]["events"]
    assert {"你開啟代管", "你關閉代管"} <= {e["text"] for e in ev if e["kind"] == "delegate"}
    raw = json.dumps(ev, ensure_ascii=False)
    assert GLM_KEY not in raw and D1_KEY not in raw


def test_legacy_docs_api(srv, tmp_path):
    repo = str(tmp_path / "repo_legacy")
    git_repo(repo)
    old = os.path.join(repo, "docs", "ai-team", "s", "spec.md")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with open(old, "w", encoding="utf-8") as f:
        f.write("# old")
    srv.team.store.create(name="p", repo=repo, slug="s", goal="g", source="goal", impl_cli="auto", review_cli="auto")
    code, res = J(srv, "/api/team/legacy_docs")
    assert code == 200 and {"repo": repo, "dirs": ["docs/ai-team"]} in res["repos"]
    code, rm = J(srv, "/api/team/legacy_docs/remove", {"repo": repo})
    assert code == 200 and rm["ok"] is True and rm["moved"] == ["docs/ai-team"]
    assert not os.path.exists(os.path.join(repo, "docs", "ai-team"))
    assert J(srv, "/api/team/legacy_docs")[1]["repos"] == []


def test_prompts_api_get_save_reset(srv):
    code, v = J(srv, "/api/team/prompts")
    assert code == 200 and "qa" in v["roles"] and v["roles"]["qa"]["version"] == "default"
    code, bad = J(srv, "/api/team/prompts", {"role": "qa", "scope": "global", "sections": {"principles": "看 {unknown_ph}"}})
    assert code == 200 and bad["ok"] is False and "unknown_ph" in bad["text"]
    code, saved = J(srv, "/api/team/prompts", {"role": "qa", "scope": "global", "sections": {"principles": "新原則 {goal}"}})
    assert code == 200 and saved["ok"] is True and saved["version"] == "global:v1"
    code, rst = J(srv, "/api/team/prompts/reset", {"role": "qa", "scope": "global"})
    assert code == 200 and rst["ok"] is True and rst["prompts"]["roles"]["qa"]["version"] == "default"
    raw = json.dumps(rst, ensure_ascii=False)
    assert GLM_KEY not in raw and D1_KEY not in raw


