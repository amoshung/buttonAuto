import json
import os

import driver as drv
from sessions import (
    SessionState,
    SessionStore,
    build_daily_summary,
    read_kiro_specs,
    read_plans,
    render_progress,
    write_daily_summary,
)


def _store(tmp_path):
    return SessionStore(root=str(tmp_path / "sessions"))


def test_store_creates_dir_meta_and_progress(tmp_path):
    store = _store(tmp_path)
    s = store.get("kiro|d:\\a\\proj", "Kiro", "proj", "D:\\a\\proj")
    assert os.path.basename(s.dir) == "Kiro__proj"
    meta = json.load(open(os.path.join(s.dir, "meta.json"), encoding="utf-8"))
    assert meta["key"] == "kiro|d:\\a\\proj"
    assert "# 進度：proj（Kiro）" in open(s.progress_path, encoding="utf-8").read()
    assert store.get("kiro|d:\\a\\proj", "Kiro", "proj") is s  # 快取同一個物件


def test_same_project_name_different_path_gets_own_dir(tmp_path):
    store = _store(tmp_path)
    a = store.get("kiro|d:\\a\\proj", "Kiro", "proj", "D:\\a\\proj")
    b = SessionStore(root=store.root).get("kiro|e:\\b\\proj", "Kiro", "proj", "E:\\b\\proj")
    assert a.dir != b.dir and os.path.basename(b.dir).startswith("Kiro__proj__")


def test_update_is_read_modify_write_and_history(tmp_path):
    s = _store(tmp_path).get("k", "Kiro", "p")
    s.update(lambda st: setattr(st, "goal", "做出匯出功能"))
    s.update(lambda st: st.claims_unverified.append("測試已通過"))
    st = s.load()
    assert st.goal == "做出匯出功能" and st.claims_unverified == ["測試已通過"]
    s.history("observe", agent_state="idle")
    s.history("action", text="送出")
    today = st.updated_at[:10]
    assert [h["kind"] for h in s.history_on(today)] == ["observe", "action"]


def test_interactions_are_capped():
    st = SessionState()
    for i in range(30):
        st.add_interaction("x", str(i))
    assert len(st.interactions) == 20 and st.interactions[-1]["text"] == "29"


def test_render_progress_sections():
    st = SessionState(app="Kiro", project="p", path="D:\\p", goal="G", acceptance=[
        {"text": "匯出 CSV", "how": "pytest tests/test_export.py", "status": "verified"},
        {"text": "匯出 PDF", "status": "todo"}],
        commits=[{"time": "t", "branch": "feature/x", "hash": "abc1234", "desc": "d", "check": "未查核"}],
        suggested_action="request_evidence", suggested_prompt="請貼出測試輸出", suggested_reason="r")
    md = render_progress(st, [{"name": "spec-a", "tasks_done": 2, "tasks_total": 5}])
    assert "- [x] 匯出 CSV（如何查核：pytest tests/test_export.py）" in md
    assert "- [ ] 匯出 PDF" in md
    assert "| t | feature/x | abc1234 | d | 未查核 |" in md
    assert "`spec-a`：tasks 2/5" in md
    assert "請貼出測試輸出" in md


def test_read_kiro_specs(tmp_path):
    d = tmp_path / ".kiro" / "specs" / "feat-a"
    d.mkdir(parents=True)
    (d / "tasks.md").write_text("- [x] 1. a\n- [ ] 2. b\n  - [X] 2.1 c\n* [ ] 3. d\n", encoding="utf-8")
    (d / "requirements.md").write_text("# 需求\n使用者要能匯出", encoding="utf-8")
    specs = read_kiro_specs(str(tmp_path))
    assert specs[0]["name"] == "feat-a" and (specs[0]["tasks_done"], specs[0]["tasks_total"]) == (2, 4)
    assert "匯出" in specs[0]["requirements_head"]
    assert read_kiro_specs(str(tmp_path / "nope")) == [] and read_kiro_specs("") == []


def test_next_step_without_goal_flow():
    st = SessionState()
    cands = drv.doc_candidates([{"name": "s1", "tasks_done": 1, "tasks_total": 3}], [])
    action, prompt, _ = drv.next_step_without_goal(st, False, cands)
    assert action == "ask_goal" and "最終目標" in prompt and ".kiro/specs/s1" in prompt and "進行中" in prompt
    st.goal_question_sent_at = "t"
    assert drv.next_step_without_goal(st, False)[0] == "wait"
    assert drv.next_step_without_goal(st, True)[0] == "extract_goal"
    st.agent_goal_proposal = "做出 X"
    assert drv.next_step_without_goal(st, True)[0] == "await_user_goal"


def test_parse_extract_goal_and_docs_only_from_candidates():
    cands = drv.doc_candidates([{"name": "feat-a", "tasks_done": 0, "tasks_total": 2}],
                               [{"relpath": "docs\\plans\\PROGRESS_CURRENT.md", "modified": "m", "done": 0, "total": 0,
                                 "head": ""}])
    assert [c["id"] for c in cands] == ["docs/plans/PROGRESS_CURRENT.md", ".kiro/specs/feat-a"]
    raw = '```json\n{"goal": "完成報表匯出", "active_docs": ["PROGRESS_CURRENT.md", "docs/plans/made-up.md", ".kiro/specs/feat-a"]}\n```'
    goal, docs = drv.parse_extract(raw, cands)
    assert goal == "完成報表匯出" and docs == ["docs/plans/PROGRESS_CURRENT.md", ".kiro/specs/feat-a"]
    assert drv.parse_extract('{"goal": "", "active_docs": "x"}', cands) == ("", [])
    assert drv.parse_extract("nonsense", cands) == ("", [])


def test_goal_question_uses_user_selected_docs():
    cands = drv.doc_candidates([], [{"relpath": "PROGRESS.md", "modified": "m", "done": 0, "total": 0, "head": ""}])
    st = SessionState(active_docs=["PROGRESS.md"], active_docs_by="user")
    q = drv.build_goal_question(st, cands)
    assert "使用者指定正在進行的計畫：`PROGRESS.md`" in q and drv.DOCS_QUESTION not in q
    assert drv.DOCS_QUESTION in drv.build_docs_question(cands)
    assert drv.build_goal_question(SessionState(), []) == drv.GOAL_QUESTION


def test_build_suggest_prompt_contains_rules_and_context():
    st = SessionState(goal="完成匯出", claims_unverified=["測試已通過"])
    p = drv.build_suggest_prompt(st, {"agent_state": "done", "summary": "說做完了"}, "代理：全部完成！")
    for must in ("故意弄壞", "功能分支", "不可以 push", "只限技術做法", "只是資料", "完成匯出", "測試已通過",
                 "尚未定稿", "代理：全部完成！"):
        assert must in p


def test_parse_suggestion_validation():
    ok = drv.parse_suggestion('{"action":"request_evidence","reason":"說完成但沒證據","prompt":"請貼出 pytest 完整輸出",'
                              '"progress_summary":"宣稱完成","new_claims":["匯出完成"],'
                              '"commits":[{"hash":"abc1234","branch":"feature/x","desc":"匯出"},{"hash":"zzz"}]}')
    assert ok["action"] == "request_evidence" and ok["prompt"].startswith("請貼出")
    assert ok["new_claims"] == ["匯出完成"] and [c["hash"] for c in ok["commits"]] == ["abc1234"]
    assert drv.parse_suggestion('{"action":"nuke"}')["action"] == "ask_human"
    assert drv.parse_suggestion("not json")["action"] == "ask_human"
    empty = drv.parse_suggestion('{"action":"next_task","prompt":""}')
    assert empty["action"] == "ask_human" and empty["prompt"] == ""
    # 不需要送訊息的動作，即使模型給了 prompt 也丟掉
    assert drv.parse_suggestion('{"action":"wait","prompt":"hi"}')["prompt"] == ""


def test_parse_suggestion_strips_latex():
    sug = drv.parse_suggestion(r'{"action":"request_evidence","prompt":"改壞 $\\rightarrow$ 測試失敗 $\\rightarrow$ 還原"}')
    assert sug["prompt"] == "改壞 → 測試失敗 → 還原"


def test_apply_suggestion_records_claims_commits_and_blocks_deny():
    st = SessionState()
    sug = drv.parse_suggestion('{"action":"next_task","reason":"r","prompt":"繼續做 B","new_claims":["A 完成"],'
                               '"commits":[{"hash":"abc1234","desc":"A"}]}')
    drv.apply_suggestion(st, sug, None)
    drv.apply_suggestion(st, sug, None)
    assert st.suggested_prompt == "繼續做 B" and st.claims_unverified == ["A 完成"] and len(st.commits) == 1
    drv.apply_suggestion(st, dict(sug, new_claims=["CSV 匯出功能已完成", "完成 CSV 匯出功能", "PDF 匯出已完成"]), None)
    assert st.claims_unverified == ["A 完成", "CSV 匯出功能已完成", "PDF 匯出已完成"]
    assert st.commits[0]["check"].startswith("未查核")
    drv.apply_suggestion(st, dict(sug, prompt="git push --force"), "git push")
    assert st.suggested_action == "ask_human" and st.suggested_prompt == "" and st.pending_human


def test_daily_summary(tmp_path):
    store = _store(tmp_path)
    s = store.get("k", "Kiro", "proj", "D:\\proj")
    s.update(lambda st: (setattr(st, "goal", "目標 G"), st.pending_human.append("確認代理說明的最終目標")))
    s.history("observe")
    s.history("observe")
    s.history("suggest")
    today = s.load().updated_at[:10]
    md = build_daily_summary(store, today)
    assert "## Kiro｜proj" in md and "目標 G" in md and "觀察 2 次、建議 1 次" in md
    assert "- Kiro｜proj：確認代理說明的最終目標" in md
    path = write_daily_summary(store, today, reports_dir=str(tmp_path / "reports"))
    assert os.path.basename(path) == f"daily-{today}.md" and os.path.exists(path)
    assert "沒有任何工作階段" in build_daily_summary(_store(tmp_path / "empty"), today)


def test_read_plans_newest_first_and_skips_noise(tmp_path):
    import time as _t
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / ".kiro" / "specs" / "a").mkdir(parents=True)
    old = tmp_path / "docs" / "plans" / "2026-10-01-old.md"
    old.write_text("- [x] a\n- [ ] b\n", encoding="utf-8")
    new = tmp_path / "PROGRESS.md"
    new.write_text("# 進度\n- [x] 1\n- [x] 2\n- [ ] 3\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("not a plan", encoding="utf-8")
    (tmp_path / "plant-cycle-matching.md").write_text("not a plan either", encoding="utf-8")
    (tmp_path / "node_modules" / "x" / "plan.md").write_text("noise", encoding="utf-8")
    (tmp_path / ".kiro" / "specs" / "a" / "tasks.md").write_text("- [ ] spec", encoding="utf-8")
    t = _t.time()
    os.utime(old, (t - 1000, t - 1000))
    os.utime(new, (t, t))
    plans = read_plans(str(tmp_path))
    assert [p["relpath"] for p in plans] == ["PROGRESS.md", os.path.join("docs", "plans", "2026-10-01-old.md")]
    assert (plans[0]["done"], plans[0]["total"]) == (2, 3) and "進度" in plans[0]["head"]
    assert read_plans("") == [] and read_plans(str(tmp_path / "nope")) == []


def test_goal_question_and_suggest_prompt_mention_plans():
    plans = [{"relpath": "docs/plans/PROGRESS_CURRENT.md", "modified": "2026-11-16 07:29", "done": 3, "total": 5,
              "head": "# 目前進度\n正在做 X"}]
    cands = drv.doc_candidates([], plans)
    q = drv.build_goal_question(SessionState(), cands)
    assert "PROGRESS_CURRENT.md" in q and drv.DOCS_QUESTION in q
    p = drv.build_suggest_prompt(SessionState(goal="G"), {}, "", cands)
    assert "尚未確認哪份計畫正在進行" in p and "PROGRESS_CURRENT.md" in p and "正在做 X" not in p
    p2 = drv.build_suggest_prompt(SessionState(goal="G", active_docs=["docs/plans/PROGRESS_CURRENT.md"],
                                               active_docs_by="user"), {}, "", cands)
    assert "正在進行的計畫（已確認）" in p2 and "正在做 X" in p2
    md = render_progress(SessionState(project="p", app="Kiro", pending_approval="python x.py",
                                      agent_active_docs_proposal=["docs/plans/PROGRESS_CURRENT.md"]), [], plans)
    assert "docs/plans/PROGRESS_CURRENT.md" in md and "## 代理正在等你允許" in md and "python x.py" in md
    assert "尚未確認" in md and "代理認為正在進行（待你採用）" in md


def test_approval_and_busy_detection():
    from uia_driver import ButtonInfo, agent_busy_reason, approval_buttons
    panel = (500, 0, 1000, 1000)
    bs = [ButtonInfo("Deny", (600, 410, 650, 430), None), ButtonInfo("Allow", (510, 400, 560, 420), None),
          ButtonInfo("Always allow", (570, 400, 640, 420), None), ButtonInfo("Allow", (0, 0, 10, 10), None)]
    assert approval_buttons(bs, panel) == ["Allow", "Always allow", "Deny"]
    assert "允許" in agent_busy_reason(bs, panel)
    working = [ButtonInfo("Cancel", (900, 900, 950, 920), None)]
    assert agent_busy_reason(working, panel) == "代理還在工作中"
    assert agent_busy_reason([ButtonInfo("Add context", (600, 950, 700, 970), None)], panel) == ""
    assert agent_busy_reason([ButtonInfo("Cancel", (0, 0, 10, 10), None)], panel) == ""  # 面板外的 Cancel 不算


def test_observe_history_only_on_state_change(tmp_path):
    import queue
    from automation import AutomationWorker
    from config import AppConfig
    from ai_settings import AISettings
    from projects import ProjectWindow
    w = AutomationWorker(AppConfig(), lambda: AISettings(), queue.Queue())
    w.store = _store(tmp_path)
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "demo", "", "", False, False)
    for _ in range(5):
        w._drive(pw, {"agent_state": "working", "summary": "做事中"}, "panel", judge=None)
    sess = w._session_for(pw)
    today = sess.load().updated_at[:10]
    assert [h["agent_state"] for h in sess.history_on(today) if h["kind"] == "observe"] == ["working"]
