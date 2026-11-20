"""驗證三角色整合（高層主管／研發工程師／審查工程師）、CE 技能對接、盤中 Decision-1 防偏離審查與跨角色溝通串流。"""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

from team import ce_skills, personas, prompt_defaults
from team.runner import REVIEW_ALIGNMENT_MIN, RoomRunner


def test_three_roles_normalization_and_labels():
    assert personas.normalize_role_key("po") == "supervisor"
    assert personas.normalize_role_key("tech_lead") == "supervisor"
    assert personas.normalize_role_key("scrum") == "supervisor"
    assert personas.normalize_role_key("qa") == "supervisor"
    assert personas.normalize_role_key("delegate") == "supervisor"
    assert personas.normalize_role_key("implementer") == "developer"
    assert personas.normalize_role_key("impl") == "developer"
    assert personas.normalize_role_key("fixer") == "developer"
    assert personas.normalize_role_key("review") == "reviewer"
    assert personas.normalize_role_key("reviewer") == "reviewer"

    assert personas.ROLES["supervisor"] == "高層主管"
    assert personas.ROLES["developer"] == "研發工程師"
    assert personas.ROLES["reviewer"] == "審查工程師"
    for label in prompt_defaults.ROLE_LABELS.values():
        assert "CLI" not in label


def test_ce_skills_three_roles_include_commit_and_no_cli_wording():
    assert "ce-commit" in ce_skills.REQUIRED_CE_SKILLS
    assert "ce-commit" in ce_skills.ROLE_CE_MAP["developer"]["skills"]
    assert "ce-compound" in ce_skills.ROLE_CE_MAP["reviewer"]["skills"]
    assert "ce-plan" in ce_skills.ROLE_CE_MAP["supervisor"]["skills"]
    for role in ("supervisor", "developer", "reviewer"):
        directive = ce_skills.role_ce_directive(role)
        assert "CLI" not in directive
        assert "高層主管" in directive or "研發工程師" in directive or "審查工程師" in directive


def test_review_prompt_includes_concerns_dev_summary_and_drift_alert():
    prompt = personas.review(
        "實作使用者登入 API",
        [{"id": "C1", "text": "支援 JWT 登入", "check": "pytest"}],
        "1 file changed",
        "+def login(): pass",
        concerns=["JWT secret 尚未設過期時間"],
        dev_summary="已完成基本 login 路由與單元測試",
        drift_alert="Decision-1 目標對齊度 55%（輕微偏離）",
    )
    assert "JWT secret 尚未設過期時間" in prompt
    assert "已完成基本 login 路由與單元測試" in prompt
    assert "Decision-1 目標對齊度 55%" in prompt
    assert "drift_locations" in prompt
    assert "compound_learnings" in prompt


def _make_minimal_runner(repo_dir: str) -> RoomRunner:
    room = MagicMock()
    room.id = "room-test-3roles"
    room.name = "3roles"
    room.slug = "3roles"
    room.repo = repo_dir
    room.goal = "完成三角色整合與盤中防偏離審查"
    room.criteria = [{"id": "C1", "text": "完成 API", "check": "pytest"}]
    room.reference_root = ""
    room.target_subdir = ""
    room.target_subdirs = []
    room.role_prefs = {"supervisor": {"seat": "auto", "model": "claude-sonnet-4-5"}}
    room.supervisor_pref = {"seat": "auto", "model": "glm-5.3"}
    room.impl_pref = {}
    room.review_pref = {}
    room.impl_seat = "kiro"
    room.demo = False

    runner = object.__new__(RoomRunner)
    runner.room = room
    runner.room_id = room.id
    runner._lock = MagicMock()
    runner._lock.__enter__ = MagicMock(return_value=None)
    runner._lock.__exit__ = MagicMock(return_value=None)
    runner._role_comms = []
    runner._pending_comm_msgs = []
    runner._supervisor_decisions = []
    runner._concerns = []
    runner._suggestions = []
    runner._last_dev_summary = "已新增 auth.py 並提交 commit"
    runner._last_reply = ""
    runner._last_reviewed_commit_count = 0
    runner._last_drift_alert = ""
    runner._wt = MagicMock(path=repo_dir, ssh_host="")
    runner.lessons = MagicMock()
    runner.kb = MagicMock()
    runner.router = MagicMock()
    runner._events = []
    runner._event = lambda kind, text="", **kw: runner._events.append((kind, text, kw))
    runner._say = lambda role, text: runner._events.append(("say", text, {"role": role}))
    runner._ctx = lambda role, purpose, **kw: {"role": role, "purpose": purpose}
    runner._check_pause = lambda: None
    runner._sync_store_progress = lambda: None
    runner._live_criteria = lambda: room.criteria
    runner._target_subdir_arg = lambda: ""
    runner._compound_learnings = lambda q: ""
    runner._record_supervisor_decision = lambda dec, actor="高層主管": runner._supervisor_decisions.append(f"{actor}：{dec}")
    return runner


def test_role_model_override_and_record_role_comm_three_roles():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_minimal_runner(tmp)
        assert r._role_model_override("supervisor") == "claude-sonnet-4-5"
        assert r._role_model_override("po") == "claude-sonnet-4-5"
        assert r._role_model_override("scrum") == "claude-sonnet-4-5"

        msg = r._record_role_comm("reviewer", "implementer", "auth.py:42 缺少權限驗證", queue_for_impl=True, emit_say=True)
        assert "審查工程師 → 研發工程師" in msg
        assert len(r._pending_comm_msgs) == 1
        assert any(ev[0] == "say" and ev[2].get("comm_from") == "reviewer" and ev[2].get("comm_to") == "developer" for ev in r._events)


def test_checkpoint_review_detects_drift_with_d1_and_triangulates():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_minimal_runner(tmp)
        # 模擬 Decision-1 偵測到目標對齊度低於門檻 (0.45 < 0.70) 並判定為 minor_drift
        r._prob = MagicMock(return_value=0.45)
        r._choose = MagicMock(return_value="minor_drift")
        r.router.write = MagicMock(
            return_value='```json\n' + json.dumps({
                "drift_locations": ["auth.py:42 修改了無關的 billing 邏輯，偏離登入目標"],
                "blocking": [],
                "compound_learnings": ["實作登入功能時不應順手改動 billing 模組"],
            }, ensure_ascii=False) + '\n```'
        )
        with patch("team.runner.gitops.commits_since", return_value=[("abc1234", "feat: update auth")]), \
             patch("team.runner.gitops.diff_stat", return_value="auth.py | 12 +++"), \
             patch("team.runner.gitops.diff_text", return_value="+billing_charge()"):
            fix_prompt = r._checkpoint_review()

        assert fix_prompt is not None
        assert "auth.py:42" in fix_prompt
        r._prob.assert_called_once()
        r._choose.assert_called_once()
        # 確認三角色閉環：審查工程師 → 高層主管 → 研發工程師
        assert any("審查工程師 → 高層主管" in c for c in r._role_comms)
        assert any("高層主管 → 研發工程師" in c for c in r._role_comms)
        assert len(r._supervisor_decisions) == 1
        # 不重複排入 _pending_comm_msgs（因為 fix_prompt 已立即回傳給研發工程師）
        assert len(r._pending_comm_msgs) == 0
        # 確認 compound_learnings 寫入經驗庫與 LLM Wiki
        r.lessons.bump_review_blocking.assert_called_once()
        r.kb.record_room_summary.assert_called_once()


def test_parse_review_captures_drift_locations_when_blocking_empty():
    reply = '```json\n' + json.dumps({
        "blocking": [],
        "drift_locations": ["api/login.py:18 改動了不相關的計費邏輯"],
        "suggestions": [],
        "compound_learnings": [],
    }, ensure_ascii=False) + '\n```'
    parsed = RoomRunner._parse_review(reply)
    assert parsed is not None
    assert len(parsed) == 1
    assert "[目標偏離]" in parsed[0]
    assert "api/login.py:18" in parsed[0]


def test_handle_agent_question_syncs_to_supervisor_decisions():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_minimal_runner(tmp)
        r._delegate_question = MagicMock(return_value=("opt_1", "採用 JWT HS256 並設 1 小時過期"))
        reply = r._handle_agent_question({
            "question": "JWT 應採用哪種簽章演算法？",
            "options": [{"id": "opt_1", "label": "HS256"}, {"id": "opt_2", "label": "RS256"}],
        })
        assert reply is not None
        assert "JWT HS256" in reply
        assert any("JWT 應採用哪種簽章演算法" in d for d in r._supervisor_decisions)
        assert any("高層主管 → 研發工程師" in c for c in r._role_comms)


def test_checkpoint_review_triggers_on_uncommitted_periodic_turn():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_minimal_runner(tmp)
        r._prob = MagicMock(return_value=0.40)
        r._choose = MagicMock(return_value="major_drift")
        r.router.write = MagicMock(
            return_value='```json\n' + json.dumps({
                "drift_locations": ["worker.py:77 尚未 commit 但已偏離目標"],
                "blocking": [],
                "compound_learnings": [],
            }, ensure_ascii=False) + '\n```'
        )
        with patch("team.runner.gitops.commits_since", return_value=[]), \
             patch("team.runner.gitops.diff_stat", return_value="worker.py | 45 ++++"), \
             patch("team.runner.gitops.diff_text", return_value="+unrelated_refactor()"):
            fix_prompt = r._checkpoint_review(turns=3)

        assert fix_prompt is not None
        assert "worker.py:77" in fix_prompt


def test_review_prompt_includes_test_evidence():
    prompt = personas.review(
        "實作登入",
        [{"id": "C1", "text": "pytest 通過", "check": "pytest"}],
        "1 file changed",
        "+def login(): pass",
        test_evidence="- pytest -q -> exit=0 (1.2s)",
    )
    assert "測試關卡實測證據摘要" in prompt
    assert "pytest -q -> exit=0" in prompt


def test_suggestions_event_records_compound_learnings_on_pass():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_minimal_runner(tmp)
        reply = '```json\n' + json.dumps({
            "blocking": [],
            "drift_locations": [],
            "suggestions": ["可補充邊界測試"],
            "compound_learnings": ["JWT 驗簽應強制指定演算法白名單"],
        }, ensure_ascii=False) + '\n```'
        r._suggestions_event(reply)
        assert r._suggestions == ["可補充邊界測試"]
        r.lessons.bump_review_blocking.assert_called_once()
        r.kb.record_room_summary.assert_called_once()


def test_office_send_appends_goal_and_criterion_and_syncs_runner():
    from team.office import Office
    from team.rooms import RoomStore

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "team.db")
        store = RoomStore(db_path)
        repo_dir = os.path.join(tmp, "repo")
        os.makedirs(repo_dir, exist_ok=True)
        room = store.create(
            repo=repo_dir,
            goal="原本目標：遷移服務",
            name="test-room",
            slug="test-room",
            source="main",
            impl_cli="claude",
            review_cli="glm",
        )
        def _init_room(rm):
            rm.status = "running"
            rm.phase = "build"
            rm.criteria = [{"id": "C1", "text": "原本驗收標準", "check": "pytest"}]

        store.update(room.id, _init_room)

        office = Office.__new__(Office)
        office.store = store
        office._lock = __import__("threading").RLock()
        office._runners = {}
        office._sends = {}
        office._waiting = {}
        office._changed = MagicMock()
        office._event = MagicMock()
        office.router = MagicMock()
        office.router.probability = MagicMock(return_value=0.95)

        runner = _make_minimal_runner(tmp)
        runner.room_id = room.id
        runner.store = store
        runner.room = store.get(room.id)
        runner._live_criteria = RoomRunner._live_criteria.__get__(runner, RoomRunner)
        runner.is_alive = MagicMock(return_value=True)
        runner._pause_ev = __import__("threading").Event()
        runner._stopped = False
        runner._user_msgs = []
        runner._supervisor_decisions = []
        office._runners[room.id] = runner

        directive = "目標要再加上一條，當所有計劃看似都完成後，要用decision1模型檢驗是否跟目標有gap需要再整合"
        res = office.send(room.id, directive)

        assert res["ok"] is True
        assert res["goal_updated"] is True
        assert res["added_criterion_id"] == "C2"

        updated = store.get(room.id)
        assert "[使用者追加目標]" in updated.goal
        assert directive in updated.goal
        assert any(c.get("id") == "C2" and directive in c.get("text", "") for c in updated.criteria)

        # Runner 記憶體與三方共享決策均已同步
        assert runner.has_pending_user_msgs() is True
        assert directive in runner.room.goal
        assert any(directive in d for d in runner._supervisor_decisions)

        # 高層主管已送出明確確認事件
        say_calls = [c for c in office._event.call_args_list if len(c.args) >= 2 and c.args[1] == "say"]
        assert say_calls
        assert "C2" in say_calls[0].args[2]


def test_build_refuses_done_when_pending_user_directive_exists():
    with tempfile.TemporaryDirectory() as tmp:
        r = _make_minimal_runner(tmp)
        r.room.goal = "初始目標"
        r.room.criteria = [{"id": "C1", "text": "初始條件", "check": "manual"}]
        r.max_build_turns = 5
        r._phase = MagicMock()
        r._set = MagicMock()
        r._say = MagicMock()
        r._event = MagicMock()
        r._changed = MagicMock()
        r._log_progress = MagicMock()
        r._maybe_checkpoint_review = MagicMock(return_value="")
        r._record_role_comm = MagicMock()
        r._take_role_comms = MagicMock(return_value="")
        r._user_msgs = []
        r._supervisor_decisions = []
        r._turn_paras = []

        # 模擬第一輪剛輸出 DONE 時，使用者同時寫入了新目標
        seen_prompts = []

        def fake_impl_turn(role, text):
            seen_prompts.append(text)
            if len(seen_prompts) == 1:
                r.room.goal = "初始目標\n- [使用者追加目標] 追加 Decision-1 驗收"
                r.room.criteria = [
                    {"id": "C1", "text": "初始條件", "check": "manual"},
                    {"id": "C2", "text": "追加 Decision-1 驗收", "check": "manual"},
                ]
                r._user_msgs.append("追加 Decision-1 驗收")
                r._last_reply = "DONE"
                r._turn_paras = ["DONE"]
                return
            # 第二輪消化掉 _user_msgs 並以最新目標完成
            r._take_user_msgs()
            r._last_reply = "DONE"
            r._turn_paras = ["DONE"]

        r._impl_turn = MagicMock(side_effect=fake_impl_turn)
        r._live_criteria = MagicMock(side_effect=lambda: list(r.room.criteria))

        with patch("team.runner.build_issues", return_value=[]):
            r._build("開始實作")

        assert len(seen_prompts) == 2
        assert "追加 Decision-1 驗收" in seen_prompts[1]
        assert r.has_pending_user_msgs() is False


def test_snapshot_reference_root_skips_codegraph_and_claude_worktrees(tmp_path):
    from team.checks import snapshot_reference_root, verify_reference_untouched

    ref = tmp_path / "MMI_Kernel_Origin"
    target = ref / "bof_knowledge_system"
    (ref / ".codegraph").mkdir(parents=True)
    (ref / "BOF456_Server_Origin" / ".claude" / "worktrees" / "agent-1").mkdir(parents=True)
    target.mkdir(parents=True)
    real_src = ref / "BOF456_Server_Origin" / "real.cpp"
    real_src.write_text("int main() { return 0; }\n", encoding="utf-8")

    snap = snapshot_reference_root(str(ref), str(target), exclude_subdir="bof_knowledge_system")

    # 外部背景工具更新 .codegraph 與 .claude/worktrees 不應觸發誤報
    (ref / ".codegraph" / "codegraph.db-wal").write_text("wal", encoding="utf-8")
    (ref / ".codegraph" / "daemon.log").write_text("log", encoding="utf-8")
    (ref / "BOF456_Server_Origin" / ".claude" / "worktrees" / "agent-1" / "foo.h").write_text("x", encoding="utf-8")
    assert verify_reference_untouched(snap, str(ref), str(target), exclude_subdir="bof_knowledge_system") == []

    # 但真正修改母資料夾內的原始碼仍會被偵測出來
    real_src.write_text("int main() { return 1; }\n", encoding="utf-8")
    violated = verify_reference_untouched(snap, str(ref), str(target), exclude_subdir="bof_knowledge_system")
    assert violated == ["BOF456_Server_Origin/real.cpp"]


def test_scan_stubs_exempts_base_class_interface_but_flags_concrete_stub(tmp_path):
    import verifier

    f = tmp_path / "wiki_linter.py"
    f.write_text(
        "class BaseRule:\n"
        "    def check(self, doc):\n"
        "        raise NotImplementedError\n\n"
        "class SubRuleA(BaseRule):\n"
        "    def check(self, doc):\n"
        "        return []\n\n"
        "def unfinished_helper():\n"
        "    raise NotImplementedError('todo')\n",
        encoding="utf-8",
    )
    hits = verifier.scan_stubs(str(tmp_path), ["wiki_linter.py"])
    lines = [int(h["line"]) for h in hits if h["kind"] == "NotImplemented"]
    assert 3 not in lines  # BaseRule.check 已有子類別實作，不應誤殺
    assert 10 in lines     # concrete 函式的 NotImplementedError 仍應被抓出


def test_adjudicate_outside_allowed_auto_expands_on_decision1_approval():
    r = RoomRunner.__new__(RoomRunner)
    r.room = MagicMock(
        goal="完成 SP3 指標系統",
        allowed_paths=["backend/wiki/linter.py"],
        criteria=[{"id": "C1", "text": "新增 metric run_id 並補測試", "check": "pytest"}],
        phase="build",
        reference_root="",
    )
    r._wt = MagicMock(path="c:/wt", base="HEAD~1", ssh_host="")
    r._last_dev_summary = "已新增 config.py 與測試"
    r._supervisor_decisions = []
    r._ctx = MagicMock(return_value={})
    r._prob = MagicMock(return_value=0.92)
    r._say = MagicMock()
    r._set = MagicMock(side_effect=lambda **kw: setattr(r.room, "allowed_paths", kw["allowed_paths"]))

    with patch("team.runner.gitops.changed_files", return_value=["backend/wiki/linter.py", "backend/config.py"]), \
         patch("team.runner.gitops.outside_allowed", return_value=["backend/config.py"]):
        remaining = r._adjudicate_outside_allowed("完成 SP3 指標系統", ["改到範圍外的檔案：backend/config.py"])

    assert remaining == []
    assert "backend/config.py" in r.room.allowed_paths
    assert r._say.called


def test_resolve_build_deadlock_auto_waives_without_asking_user():
    r = RoomRunner.__new__(RoomRunner)
    r.room = MagicMock(goal="完成任務", criteria=[], phase="build", reference_root="")
    r._last_dev_summary = "wiki_linter.py:312 為基底類別介面，兩個子類別皆已實作"
    r._supervisor_decisions = []
    r._ctx = MagicMock(return_value={})
    r._prob = MagicMock(return_value=0.88)
    r._say = MagicMock()
    r._ask = MagicMock()

    waived = r._resolve_build_deadlock(["疑似空殼：backend/wiki/wiki_linter.py:312 NotImplemented"], "完成任務")
    assert waived is True
    assert not r._ask.called
    assert r._say.called
