"""測試三角色協作強化（結構化審查清單、主管決策共享＋動態驗收同步、ce-compound 回合中途注入、審查員每輪全新 Context）。"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from team import ce_sync, personas
from team.lessons import LessonBook
from team.rooms import RoomStore
from team.runner import RoomRunner


class _FakeRouter:
    glm_name = "glm-5.3"
    decision_name = "Decision-1"

    def write(self, prompt, effort="normal", ctx=None):
        return '```json\n{"blocking":[],"suggestions":[]}\n```'

    def probability(self, state, question, ctx=None):
        return 0.9


class CollaborationEnhancementsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo, exist_ok=True)
        self.store = RoomStore(os.path.join(self.root, "rooms"))
        self.room = self.store.create(
            name="協作測試房",
            slug="collab-test",
            source="local",
            impl_cli="claude",
            review_cli="claude",
            repo=self.repo,
            goal="實作三角色協作強化",
            criteria=[{"id": "C1", "text": "初始條件", "check": "pytest -q"}],
        )
        self.lessons = LessonBook(os.path.join(self.root, "lessons"))
        self.runner = RoomRunner(
            room_id=self.room.id,
            store=self.store,
            router=_FakeRouter(),
            clis={},
            deny_patterns=[],
            on_change=lambda: None,
            lessons_book=self.lessons,
        )

    def tearDown(self):
        self.runner.kb.close()
        self._tmp.cleanup()

    def test_1_structured_review_blocking_parsing(self):
        reply = (
            "審查完成。\n"
            "```json\n"
            '{"blocking":['
            '{"severity":"Critical","location":"team/runner.py:42","issue":"空指標未防呆","fix":"先檢查 None"},'
            '{"severity":"Important","file":"team/office.py","line":88,"message":"缺少授權檢查"},'
            '"舊格式純字串問題"'
            '],"suggestions":["可補註解"]}\n'
            "```"
        )
        parsed = RoomRunner._parse_review(reply)
        self.assertEqual(len(parsed), 3)
        self.assertIn("[Critical] team/runner.py:42 — 空指標未防呆（修法：先檢查 None）", parsed[0])
        self.assertIn("[Important] team/office.py:88 — 缺少授權檢查", parsed[1])
        self.assertEqual(parsed[2], "舊格式純字串問題")

    def test_2_supervisor_decision_shared_and_live_criteria_sync(self):
        # 1) 主管決策自動寫入三方共享紀錄與 progress.md
        self.runner._decision("核准補上快取上限", reason="避免記憶體膨脹")
        self.assertTrue(any("核准補上快取上限" in d for d in self.runner._supervisor_decisions))

        md = self.runner._build_progress_markdown()
        self.assertIn("## 主管決策紀錄（最新在上）", md)
        self.assertIn("核准補上快取上限", md)

        # 2) 中途動態更新 store 內的驗收條件，_live_criteria() 應即時同步最新條件
        updated_criteria = [
            {"id": "C1", "text": "初始條件", "check": "pytest -q"},
            {"id": "C2", "text": "動態新增的效能驗收條件", "check": "pytest -k perf"},
        ]
        self.store.update(self.room.id, lambda r: setattr(r, "criteria", updated_criteria))
        live = self.runner._live_criteria()
        self.assertEqual(len(live), 2)
        self.assertEqual(live[1]["id"], "C2")
        self.assertEqual(self.runner.room.criteria[1]["text"], "動態新增的效能驗收條件")

        # 3) 審查提示詞包含主管決策與最新驗收條件
        prompt = personas.review(
            self.room.goal,
            live,
            "1 file changed",
            "+x = 1",
            supervisor_decisions=self.runner._supervisor_decisions,
        )
        self.assertIn("高層主管決策紀錄（三方共享）", prompt)
        self.assertIn("核准補上快取上限", prompt)
        self.assertIn("動態新增的效能驗收條件", prompt)
        self.assertIn('"severity":"Critical"', prompt)

    def test_3_compound_learnings_mid_round_injection(self):
        self.lessons.add(self.repo, "review", "修改 runner 時不可漏掉鎖保護 self._lock", self.room.id)
        self.runner.kb.query = MagicMock(return_value="[Wiki] 過去解法：請在 _review_once 確保關閉舊 driver")

        learnings = self.runner._compound_learnings("審查發現 _lock 競態與舊 driver 未關閉")
        self.assertIn("self._lock", learnings)
        self.assertIn("確保關閉舊 driver", learnings)

        fix_prompt = personas.fix(
            "reviewer",
            ["[Critical] team/runner.py:100 — 競態問題"],
            goal=self.room.goal,
            compound_learnings=learnings,
        )
        self.assertIn("專案歷史經驗（ce-compound，避免重複踩坑）", fix_prompt)
        self.assertIn("self._lock", fix_prompt)

    def test_4_reviewer_fresh_context_per_round(self):
        self.runner._wt = SimpleNamespace(path=self.repo, ssh_host="")
        captured_tmpdirs = []

        def fake_seat_launch(seat, clis, cfg, plugin_dirs, wt_path, tmpdir, reference_root="", ssh_host=""):
            captured_tmpdirs.append(tmpdir)
            return SimpleNamespace(name="claude", label="Claude", protocol="headless"), {}, []

        fake_drv_1 = MagicMock()
        fake_drv_2 = MagicMock()
        with patch("team.runner.seat_launch", side_effect=fake_seat_launch), \
             patch("team.runner.make_driver", side_effect=[fake_drv_1, fake_drv_2]):
            d1 = self.runner._open("claude", "", read_only=True, fresh_context=True)
            d2 = self.runner._open("claude", "", read_only=True, fresh_context=True)

        self.assertIs(d1, fake_drv_1)
        self.assertIs(d2, fake_drv_2)
        self.assertEqual(len(captured_tmpdirs), 2)
        self.assertNotEqual(captured_tmpdirs[0], captured_tmpdirs[1])
        self.assertTrue(captured_tmpdirs[0].endswith("seat-fresh-1"))
        self.assertTrue(captured_tmpdirs[1].endswith("seat-fresh-2"))

    def test_5_rules_include_static_fallback_and_db_safety(self):
        from team.prompt_defaults import SAFETY

        for text in (
            SAFETY,
            personas.build("slug", self.room.criteria, [], goal=self.room.goal),
            personas.review(self.room.goal, self.room.criteria, "1 file", "+pass"),
        ):
            self.assertIn("[環境限制-靜態驗證]", text)
            self.assertIn("WHERE", text)
            self.assertIn("dry-run", text)

    def test_6_review_circuit_breaker_on_repeated_blocking(self):
        self.runner._wt = SimpleNamespace(path=self.repo, ssh_host="")
        same_issue_r1 = ["[Critical] team/runner.py:42 — 空指標未防呆（修法：先檢查 None）"]
        same_issue_r2 = ["[Critical] team/runner.py:48 — 空指標未防呆（修法：先檢查 None）"]
        self.assertEqual(
            RoomRunner._blocking_fingerprint(same_issue_r1[0]),
            RoomRunner._blocking_fingerprint(same_issue_r2[0]),
        )

        with patch("team.runner.gitops.diff_stat", return_value="1 file changed"), \
             patch.object(self.runner, "_role_note", return_value=""), \
             patch.object(self.runner, "_review_once", side_effect=[(same_issue_r1, "claude"), (same_issue_r2, "claude")]):
            fix1 = self.runner._review_stage()
            self.assertNotIn("高階主管斷路器介入", fix1)
            fix2 = self.runner._review_stage()
            self.assertIn("高階主管斷路器介入", fix2)
            self.assertTrue(any("防互踢斷路器" in d for d in self.runner._supervisor_decisions))

    def test_7_static_fallback_audit_trail_and_executive_summary(self):
        self.runner._wt = SimpleNamespace(path=self.repo, ssh_host="")
        self.runner.room.criteria = [
            {"id": "C1", "text": "核心邏輯正確", "check": "pytest -q"},
            {"id": "C2", "text": "內網 DB 寫入驗證", "check": "manual"},
        ]
        self.runner._record_supervisor_decision("核准 C2 改採靜態審查", actor="高階主管")

        fake_gate = SimpleNamespace(results=[])
        with patch("team.runner.gitops.git", return_value="abc1234"):
            self.runner._write_evidence(fake_gate, "無法驗證：無內網 DB 連線")

        ev_path = os.path.join(self.runner._notes_dir(), self.runner._doc_rel("evidence.md"))
        with open(ev_path, encoding="utf-8") as f:
            ev_text = f.read()
        self.assertIn("## [環境限制-靜態驗證] 審計軌跡", ev_text)
        self.assertIn("`C2`", ev_text)
        self.assertIn("無內網 DB 連線", ev_text)

        summary = self.runner._build_executive_summary("abc1234 feat", "1 file", ["C2"], ["可加索引"])
        self.assertIn("## 結案一頁式驗收摘要", summary)
        self.assertIn("已自動實測通過條件：C1", summary)
        self.assertIn("環境受限／靜態驗證項目：C2", summary)
        self.assertIn("核准 C2 改採靜態審查", summary)


if __name__ == "__main__":
    unittest.main()
