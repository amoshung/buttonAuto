"""針對止血修正（_DONE 誤判、強制換手、GLM 逾時與審查 JSON 防呆）與 CE 計畫清空／生成按鈕的回歸測試。"""

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from team.models import GLM_TIMEOUTS
from team.office import Office
from team.rooms import RoomStore
from team.runner import _is_done_reply, _requests_fresh_session, RoomRunner


def _git(cwd: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def sample_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "config", "user.email", "test@example.com")
    _git(str(repo), "config", "user.name", "Test")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(str(repo), "add", "README.md")
    _git(str(repo), "commit", "-m", "init")
    return str(repo)


def test_is_done_reply_rejects_negated_and_context_refusal():
    kiro_refusal = (
        "I haven't started this round, so I'm not replying DONE. "
        "This conversation is still past the 70% context limit, and the rule for "
        "this workspace says to stop rather than start new work. "
        "Please start a **new conversation** (or `/clear`)."
    )
    assert _is_done_reply(kiro_refusal) is False
    assert _is_done_reply("尚未完成，不要回覆 DONE。") is False
    assert _is_done_reply("I cannot say DONE yet.") is False
    assert _is_done_reply("DONE") is True
    assert _is_done_reply("已完成所有條件並提交。\nDONE") is True
    assert _is_done_reply("好，存好了 DONE") is True


def test_requests_fresh_session_detection():
    kiro_refusal = (
        "This conversation is still past the 70% context limit. "
        "Please start a **new conversation** (or `/clear`)."
    )
    assert _requests_fresh_session(kiro_refusal) is True
    assert _requests_fresh_session("請開啟新對話後繼續執行。") is True
    assert _requests_fresh_session("已完成 C1 並 commit。\nDONE") is False


def test_glm_timeouts_raised():
    assert GLM_TIMEOUTS["low"] >= 60.0
    assert GLM_TIMEOUTS["normal"] >= 240.0


def test_parse_review_resilient_and_fail_closed():
    assert RoomRunner._parse_review('```json\n{"blocking": []}\n```') == []
    assert RoomRunner._parse_review('{"issues": ["a.py:10 — missing null check"]}') == [
        "a.py:10 — missing null check"
    ]
    assert RoomRunner._parse_review('{"approved": true, "suggestions": ["looks good"]}') == []
    assert RoomRunner._parse_review("這段純文字沒有任何 JSON") is None


def test_clear_and_generate_plan_from_goal(tmp_path, sample_repo):
    store_root = tmp_path / "sessions"
    fake_router = MagicMock()
    fake_router.write.return_value = (
        "---\n"
        'title: "C++ 轉 Rust 階段計畫"\n'
        "type: feat\n"
        "status: draft\n"
        "date: 2026-11-20\n"
        "---\n\n"
        "## Goal Capsule\n"
        "- **Objective:** 完成 C++ 到 Rust 核心模組遷移\n\n"
        "## Scope\n"
        "- 僅修改目標子資料夾\n\n"
        "## Implementation Units\n"
        "- [ ] C1: 完成 codec 模組接線\n"
        "- [ ] C2: 補齊單元測試並通過 cargo test\n\n"
        "## Verification\n"
        "- `cargo test`\n"
    )

    office = Office(
        RoomStore(str(store_root)),
        deny_patterns_fn=lambda: [],
        max_running_fn=lambda: 2,
        on_change=lambda: None,
        router_factory=lambda _demo=False: fake_router,
        detect_on_start=False,
    )
    office.model_cfg = MagicMock(glm_model="glm-5.3")
    room = office.create_room(
        repo=sample_repo,
        goal="完成 C++ 到 Rust 核心模組遷移",
        source="goal",
    )

    # 先塞入既有條件與進度檔，測試清空按鈕
    office.store.update(
        room.id,
        lambda r: (
            setattr(r, "criteria", [{"id": "C1", "text": "舊條件", "check": "", "done": False}]),
            setattr(r, "attempts", {"test": 3}),
            setattr(r, "pending_fix", "old fix"),
            setattr(r, "graph_node", "implement"),
            setattr(r, "current_unit_id", "C1"),
            setattr(r, "selected_plan_rel", "docs/plans/old.md"),
        ),
    )
    graph_db = os.path.join(str(store_root), room.id, "graph.sqlite")
    with open(graph_db, "wb") as f:
        f.write(b"dummy")
    office.sync_room_progress(room.id, extra_note="舊進度")
    store_prog = os.path.join(str(store_root), room.id, "progress.md")
    assert os.path.isfile(store_prog)

    cleared = office.clear_room_plan_and_progress(room.id)
    assert cleared["ok"] is True
    assert cleared["room"]["criteria"] == []
    assert cleared["room"]["attempts"] == {}
    assert cleared["room"]["pending_fix"] == ""
    assert cleared["room"]["graph_node"] == ""
    assert cleared["room"]["current_unit_id"] == ""
    assert cleared["room"]["selected_plan_rel"] == ""
    assert cleared["room"]["goal"] == "完成 C++ 到 Rust 核心模組遷移"
    assert not os.path.isfile(store_prog)
    assert not os.path.isfile(graph_db)

    # 再測試「根據目標生成計畫」
    generated = office.generate_room_plan_from_goal(room.id)
    assert generated["ok"] is True
    assert generated["plan_rel"].startswith("docs/plans/")
    assert generated["room"]["graph_node"] == "plan"
    assert len(generated["room"]["criteria"]) >= 2
    assert os.path.isfile(os.path.join(sample_repo, generated["plan_rel"]))

    # 再測試「清空 LLM Wiki」
    office.kb.record(sample_repo, room.id, "rooms", "測試知識條目", fingerprint="fp-wiki-1", wait=True)
    assert len(office.kb.wiki_view(sample_repo, sync=False)["pages"]) >= 1
    cleared_wiki = office.clear_room_wiki(room.id)
    assert cleared_wiki["ok"] is True
    assert cleared_wiki["wiki"]["pages"] == []
