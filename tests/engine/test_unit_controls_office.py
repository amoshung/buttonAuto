"""測試 v2 房間建立參數（task_mode / d1_fast_review / engine_version）與單一單元控制（重跑／豁免／編輯）。"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from engine.runner_v2 import EngineV2Runner
from team.office import Office
from team.rooms import RoomStore


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "config", "user.email", "test@example.com")
    _git(str(repo), "config", "user.name", "Test")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(str(repo), "add", "README.md")
    _git(str(repo), "commit", "-m", "init")
    return str(repo)


def test_office_v2_room_and_unit_controls(tmp_path, repo_dir):
    office = Office(
        RoomStore(str(tmp_path / "sessions")),
        deny_patterns_fn=lambda: [],
        max_running_fn=lambda: 0,
        on_change=lambda: None,
        router_factory=lambda _demo=False: MagicMock(),
        detect_on_start=False,
    )
    room = office.create_room(
        repo=repo_dir,
        goal="通用多專案測試任務",
        source="goal",
        task_mode="triage",
        d1_fast_review=True,
        engine_version="v2",
    )
    assert room.engine_version == "v2"
    assert room.task_mode == "triage"
    assert room.d1_fast_review is True

    # 預設非 demo 房間應路由至 EngineV2Runner
    runner = office._make_runner(room)
    assert isinstance(runner, EngineV2Runner)

    # 切換 D1 快速審查分流
    toggled = office.set_room_d1_fast_review(room.id, False)
    assert toggled["ok"] is True
    assert toggled["room"]["d1_fast_review"] is False

    # 塞入單元測試 edit / waive / rerun
    office.store.update(
        room.id,
        lambda r: setattr(
            r,
            "criteria",
            [
                {"id": "C1", "text": "第一項", "check": "pytest -k 1", "done": False},
                {"id": "C2", "text": "第二項", "check": "pytest -k 2", "done": True, "status": "verified"},
            ],
        ),
    )

    edited = office.edit_room_unit(room.id, "C1", check_cmd="pytest -k new_1")
    assert edited["ok"] is True
    assert edited["room"]["criteria"][0]["check"] == "pytest -k new_1"

    waived = office.waive_room_unit(room.id, "C1", reason="外部環境限制")
    assert waived["ok"] is True
    assert waived["room"]["criteria"][0]["done"] is True
    assert waived["room"]["criteria"][0]["status"] == "waived"

    rerun = office.rerun_room_unit(room.id, "C2")
    assert rerun["ok"] is True
    assert rerun["room"]["criteria"][1]["done"] is False
    assert rerun["room"]["criteria"][1]["status"] == "pending"
