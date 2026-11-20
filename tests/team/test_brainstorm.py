"""Goal Brainstorm 對話、多 CLI 降級與生成寫入 docs/plans/ 單元測試。"""
import json
import os
import threading
import urllib.request
from types import SimpleNamespace

import pytest

import automation
import server
from projects import ProjectDetector
from sessions import SessionStore
from team import gitops, office as office_mod
from team.clis import CliInfo
from team.office import Office
from team.rooms import RoomStore


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    gitops.ensure_local_git_repo(str(path))
    return path


def _make_office(tmp_path, clis=None, router=None, with_litellm=True):
    base = tmp_path / "base"
    base.mkdir(parents=True, exist_ok=True)
    if with_litellm:
        (base / "litellm.local.json").write_text(
            json.dumps({"base_url": "http://models.invalid", "key": "k", "decision1_key": "d"}),
            encoding="utf-8",
        )
    store = RoomStore(str(tmp_path / "team"))
    return Office(
        store,
        router_factory=(lambda _demo: router),
        deny_patterns_fn=lambda: [],
        max_running_fn=lambda: 2,
        on_change=lambda: None,
        clis_fn=lambda: dict(clis or {}),
        base_dir=str(base),
        probe_interval=3600,
        detect_on_start=False,
    )


def test_resolve_brainstorm_seat_defaults_glm_and_fallbacks_to_cli(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    off_glm = _make_office(
        tmp_path / "with_glm",
        clis={"claude": CliInfo("claude", "Claude", "claude", "headless")},
        with_litellm=True,
    )
    room1 = off_glm.create_room(repo=str(repo), source="goal", goal="test", impl_cli="auto", review_cli="auto")
    seat, model = off_glm._resolve_brainstorm_seat(room1, "", "")
    assert seat == "glm"
    assert model == "glm-5.3"

    off_no_glm = _make_office(
        tmp_path / "no_glm",
        clis={"kiro": CliInfo("kiro", "Kiro", "kiro-cli", "acp")},
        with_litellm=False,
    )
    room2 = off_no_glm.create_room(repo=str(repo), source="goal", goal="test", impl_cli="auto", review_cli="auto")
    seat2, _ = off_no_glm._resolve_brainstorm_seat(room2, "", "")
    assert seat2 == "kiro"

    with pytest.raises(ValueError, match="GLM 座位不可用"):
        off_no_glm._resolve_brainstorm_seat(room2, "glm", "")


def test_brainstorm_room_glm_and_cli_driver(tmp_path, monkeypatch):
    calls = []

    class FakeRouter:
        def write(self, prompt, effort="low", ctx=None):
            calls.append(("glm", effort, prompt))
            return "建議先釐清目標受眾與驗收條件。"

    repo = _init_repo(tmp_path / "repo")
    off = _make_office(
        tmp_path,
        clis={"claude": CliInfo("claude", "Claude", "claude", "headless")},
        router=FakeRouter(),
        with_litellm=True,
    )
    room = off.create_room(repo=str(repo), source="goal", goal="做一個自動報表工具", impl_cli="auto", review_cli="auto")

    res = off.brainstorm_room(
        room.id,
        messages=[{"role": "user", "content": "我想先做每週報表"}],
        mode="chat",
        seat="",
        goal_draft="做一個自動報表工具",
    )
    assert res["ok"] is True
    assert res["seat"] == "glm"
    assert "釐清目標" in res["reply"]
    assert calls[0][1] == "normal"

    # 測試降級/手動切換到非 glm CLI (claude / kiro)
    driver_events = []

    class FakeDriver:
        def start(self):
            driver_events.append("start")

        def prompt(self, text, timeout=180):
            driver_events.append(("prompt", text))
            return (
                "```markdown\n"
                "---\n"
                'title: "feat: Weekly Report"\n'
                "type: feat\n"
                "---\n\n"
                "## Goal Capsule\n\n"
                "- **Objective:** 自動產出每週報表\n\n"
                "## Implementation Units\n\n"
                "- [ ] 建立報表資料彙整器\n"
                "- [ ] 補齊單元測試\n"
                "```\n"
            )

        def close(self):
            driver_events.append("close")

    monkeypatch.setattr(office_mod, "seat_launch", lambda seat, clis, *a, **kw: (clis[seat], {}, []))
    monkeypatch.setattr(office_mod, "make_driver", lambda *a, **kw: FakeDriver())

    plan_res = off.brainstorm_room(
        room.id,
        messages=[{"role": "user", "content": "我想先做每週報表"}],
        mode="generate_plan",
        seat="claude",
        model="sonnet",
        goal_draft="做一個自動報表工具",
    )
    assert plan_res["ok"] is True
    assert plan_res["seat"] == "claude"
    assert plan_res["filename"].endswith("-plan.md")
    assert not plan_res["content"].startswith("```")
    assert "- **Objective:** 自動產出每週報表" in plan_res["content"]
    assert driver_events[0] == "start"
    assert driver_events[-1] == "close"
    assert not (repo / "docs" / "plans").exists()


def test_save_brainstorm_plan_writes_and_syncs_room(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    off = _make_office(tmp_path, with_litellm=True)
    room = off.create_room(repo=str(repo), source="goal", goal="舊的粗略目標", impl_cli="auto", review_cli="auto")

    with pytest.raises(ValueError, match="不合法"):
        off.save_brainstorm_plan(room.id, "../evil.md", "# bad")

    content = (
        "---\n"
        'title: "feat: Weekly Report"\n'
        "---\n\n"
        "## Goal Capsule\n\n"
        "- **Objective:** 自動產出每週報表並寄送通知\n\n"
        "## Implementation Units\n\n"
        "- [ ] 實作報表彙整\n"
        "- [ ] 實作測試驗證\n"
    )
    saved = off.save_brainstorm_plan(room.id, "2026-11-19-feat-weekly-report-plan.md", content, apply_goal=True)
    assert saved["ok"] is True
    assert saved["plan_rel"] == "docs/plans/2026-11-19-feat-weekly-report-plan.md"
    assert (repo / "docs" / "plans" / "2026-11-19-feat-weekly-report-plan.md").is_file()
    assert saved["room"]["goal"] == "自動產出每週報表並寄送通知"
    assert len(saved["room"]["criteria"]) == 2


def test_server_brainstorm_endpoints(tmp_path, monkeypatch):
    class _NoWins(ProjectDetector):
        def detect(self):
            return []

        def describe_hwnd(self, hwnd):
            return None

    store = SessionStore(root=str(tmp_path / "sessions"))
    monkeypatch.setattr(automation, "SessionStore", lambda: store)
    (tmp_path / "litellm.local.json").write_text(
        json.dumps({"base_url": "http://models.invalid", "key": "k", "decision1_key": "d"}),
        encoding="utf-8",
    )
    token = "T" * 24
    app = server.AssistantServer(
        config_path=str(tmp_path / "config.json"),
        secrets_path=str(tmp_path / "s.json"),
        detector=_NoWins(),
        start_threads=False,
        token=token,
        team_root=str(tmp_path / "team"),
        team_base_dir=str(tmp_path),
        team_opts={"clis_fn": lambda: {}, "probe_interval": 3600},
    )
    app.team.start()
    httpd = app.serve(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        repo = _init_repo(tmp_path / "repo")
        room = app.team.create_room(repo=str(repo), source="goal", goal="粗略目標")
        app.team.store.update(room.id, lambda r: setattr(r, "demo", True))

        def post(path, payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Token": token},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))

        chat_res = post(
            "/api/team/rooms/brainstorm",
            {"id": room.id, "messages": [{"role": "user", "content": "想做登入功能"}], "mode": "chat"},
        )
        assert chat_res["ok"] is True
        assert "示範回應" in chat_res["reply"]

        gen_res = post(
            "/api/team/rooms/brainstorm",
            {"id": room.id, "messages": [{"role": "user", "content": "想做登入功能"}], "mode": "generate_plan"},
        )
        assert gen_res["ok"] is True
        assert gen_res["filename"].endswith("-plan.md")

        save_res = post(
            "/api/team/rooms/brainstorm/save_plan",
            {"id": room.id, "filename": gen_res["filename"], "content": gen_res["content"], "apply_goal": True},
        )
        assert save_res["ok"] is True
        assert save_res["plan_rel"].startswith("docs/plans/")
    finally:
        httpd.shutdown()
        app.team.stop()
