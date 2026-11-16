"""用假的模型與真的 git repo 跑完整個 P2 流程（不碰任何 IDE 視窗）。"""
import json
import os
import queue
import subprocess

import driver_p2 as drv2
from ai_settings import AISettings
from automation import AutomationWorker
from config import AppConfig
from projects import ProjectWindow
from sessions import SessionStore, now_str, phase_of


class FakeJudge:
    model = "fake"

    def __init__(self, verify_ok=True):
        self.verify_ok = verify_ok
        self.calls = []

    def chat(self, prompt, image_png=None, max_tokens=0, temperature=None):
        self.calls.append(prompt[:40])
        if "代理提出的驗收條件" in prompt:
            return json.dumps({"items": [{"text": "按匯出可下載 CSV", "how": "跑測試", "test": "pytest -q"}]}, ensure_ascii=False)
        if "驗收審查者" in prompt:
            return '{"ok": true, "issues": [], "needs_user": []}'
        if "負責督導" in prompt:
            return json.dumps({"action": "next_task", "reason": "代理說完成了", "prompt": "繼續",
                               "new_claims": ["CSV 匯出完成"], "commits": []}, ensure_ascii=False)
        if "嚴格的驗收查核者" in prompt:
            return json.dumps({"items": [{"index": 0, "verified": self.verify_ok, "reason": "有測試輸出"}],
                               "issues": [] if self.verify_ok else ["測試輸出與條件不對應"]}, ensure_ascii=False)
        return "{}"


def _repo(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(p), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    run("checkout", "-q", "-b", "feature/export")
    (p / "export.py").write_text("def export(rows):\n    return '\\n'.join(','.join(r) for r in rows)\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "export csv")
    h = subprocess.run(["git", "-C", str(p), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    return p, h


def _worker(tmp_path):
    w = AutomationWorker(AppConfig(), lambda: AISettings(), queue.Queue())
    w.store = SessionStore(root=str(tmp_path / "sessions"))
    return w


def _mark_sent(sess, text):
    """模擬「已送出」：和 _do_send 成功後做的狀態更新相同。"""
    def mutate(st):
        if text.startswith(drv2.SPEC_MARK):
            st.spec_requested_at = now_str()
            st.spec_round += 1
        if text.startswith(drv2.REPORT_MARK):
            st.evidence_requested_at = now_str()
            st.evidence_attempts += 1
        st.suggested_action, st.suggested_prompt = "wait", ""
        st.last_panel_hash = ""
    sess.update(mutate)


def test_full_p2_flow_until_done(tmp_path):
    path, h = _repo(tmp_path)
    w = _worker(tmp_path)
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "proj", "", str(path), False, False)
    judge = FakeJudge()
    sess = w._session_for(pw)
    sess.update(lambda st: setattr(st, "goal", "加入 CSV 匯出"))
    stopped = {"agent_state": "waiting_input", "summary": "代理停下來"}

    # ② 先請代理提出驗收 spec
    w._drive(pw, stopped, "panel-1", judge)
    st = sess.load()
    assert phase_of(st) == "spec" and st.suggested_action == "request_spec"
    assert st.suggested_prompt.startswith(drv2.SPEC_MARK) and "acceptance_draft.md" in st.suggested_prompt
    _mark_sent(sess, st.suggested_prompt)

    # 代理回覆 spec → 整理、審查通過 → 等使用者確認（不會自己開始實作）
    w._drive(pw, stopped, "panel-2：1. 條件：按匯出可下載 CSV | 驗證方式：跑測試 | 測試：pytest -q", judge)
    st = sess.load()
    assert st.suggested_action == "await_spec_confirm" and not st.suggested_prompt
    assert st.acceptance[0]["text"] == "按匯出可下載 CSV" and "確認驗收 spec" in st.pending_human
    w._drive(pw, stopped, "panel-3", judge)
    assert sess.load().suggested_action == "await_spec_confirm"  # 沒確認前一直等

    # 使用者確認 → ③ 代理宣稱完成 → 改用固定格式要求測試報告
    sess.update(lambda s: setattr(s, "acceptance_confirmed_at", now_str()))
    w._drive(pw, stopped, "panel-4：我已經完成 CSV 匯出！", judge)
    st = sess.load()
    assert phase_of(st) == "build" and st.suggested_action == "request_evidence"
    assert st.suggested_prompt.startswith(drv2.REPORT_MARK) and "故意弄壞" in st.suggested_prompt
    assert st.report_path.endswith(".md") and os.path.dirname(st.report_path) == sess.reports_dir
    _mark_sent(sess, st.suggested_prompt)

    # 代理寫好報告 → 唯讀查核（commit 存在、在功能分支、沒有空殼）→ 全部通過 → ④ 完成並關閉自動駕駛
    with open(st.report_path, "w", encoding="utf-8") as f:
        f.write(f"python -m pytest -q\n3 passed\n故意弄壞：改壞後 1 failed，改回後 3 passed，git status 乾淨\n"
                f"COMMIT: {h} feature/export\n【報告完畢】\n")
    sess.update(lambda s: setattr(s, "autopilot", True))
    w._drive(pw, stopped, "panel-5：報告已寫入", judge)
    st = sess.load()
    assert st.done_at and phase_of(st) == "done" and st.autopilot is False
    assert st.acceptance[0]["status"] == "verified" and st.evidence
    assert any(c["check"].startswith("✅") for c in st.commits)
    assert "目標已達成，請你驗收" in st.pending_human
    kinds = [x["kind"] for x in sess.history_on(st.updated_at[:10])]
    assert "verify" in kinds and "spec_review" in kinds


def test_fake_commit_and_missing_mutation_are_rejected_then_pause(tmp_path):
    path, _ = _repo(tmp_path)
    w = _worker(tmp_path)
    # 自動駕駛模式（測試中不會真的送出：沒有視窗）
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "proj", "", str(path), False, False)
    judge = FakeJudge(verify_ok=True)  # 模型說通過，但有確定的問題，仍不能算通過
    sess = w._session_for(pw)

    def setup(st):
        st.goal, st.acceptance_confirmed_at = "G", now_str()
        st.acceptance = [{"text": "A", "status": "todo"}]
    sess.update(setup)
    w.cfg.set_mode(pw.key, "auto")
    stopped = {"agent_state": "waiting_input", "summary": "停"}
    for attempt in range(1, 4):
        report = os.path.join(sess.reports_dir, f"r{attempt}.md")
        os.makedirs(sess.reports_dir, exist_ok=True)
        sess.update(lambda s, r=report: (setattr(s, "report_path", r), setattr(s, "evidence_requested_at", now_str()),
                                         setattr(s, "evidence_attempts", attempt)))
        with open(report, "w", encoding="utf-8") as f:
            f.write("3 passed\nCOMMIT: deadbee feature/x\n【報告完畢】")  # 假的 commit、沒有故意弄壞
        w._drive(pw, stopped, f"panel-{attempt}", judge)
        st = sess.load()
        assert st.acceptance[0]["status"] == "todo"
        assert any("deadbee" in x for x in st.last_issues) and any("故意弄壞" in x for x in st.last_issues)
    assert st.paused_reason.startswith("連續 3 次要不到合格的證據")
    assert st.suggested_action == "ask_human"


def test_no_loop_when_assistant_did_not_send(tmp_path):
    path, _ = _repo(tmp_path)
    w = _worker(tmp_path)
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "proj", "", str(path), False, False)
    sess = w._session_for(pw)

    def setup(st):
        st.goal, st.acceptance_confirmed_at = "G", now_str()
        st.acceptance = [{"text": "A", "status": "todo"}]
    sess.update(setup)
    judge = FakeJudge()
    same = "代理：我在等你回覆，請問要繼續嗎？" * 3
    for i in range(5):
        sess.update(lambda s: setattr(s, "last_panel_hash", ""))  # 模擬每輪都重新分析同一個畫面
        w._drive(pw, {"agent_state": "waiting_input", "summary": "停"}, same, judge)
        assert sess.load().suggested_action != "rollover"
    # 助理連續 3 次送出後，代理仍停在幾乎一樣的內容，才算原地打轉
    sess.update(lambda s: (setattr(s, "recent_tails", [drv2.agent_tail(same)] * 3), setattr(s, "last_panel_hash", "")))
    w._drive(pw, {"agent_state": "waiting_input", "summary": "停"}, same, judge)
    assert sess.load().suggested_action == "rollover"


def test_waits_for_report_to_finish_and_followup_is_short(tmp_path):
    path, h = _repo(tmp_path)
    w = _worker(tmp_path)
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "proj", "", str(path), False, False)
    judge = FakeJudge()
    sess = w._session_for(pw)
    report = os.path.join(sess.reports_dir, "r.md")
    os.makedirs(sess.reports_dir, exist_ok=True)

    def setup(st):
        st.goal, st.acceptance_confirmed_at = "G", now_str()
        st.acceptance = [{"text": "A", "test": "test_a", "status": "todo"}]
        st.report_path, st.evidence_requested_at = report, now_str()
    sess.update(setup)
    stopped = {"agent_state": "waiting_input", "summary": "停"}
    with open(report, "w", encoding="utf-8") as f:
        f.write("故意弄壞後 FAILED (failures=5)\n")  # 還在寫，沒有結束標記
    w._drive(pw, stopped, "p1", judge)
    assert not any("嚴格的驗收查核者" in c for c in judge.calls)  # 第一次看到：先等，不查核
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"故意弄壞後 FAILED (failures=5)，改回後 3 passed\nCOMMIT: {h} feature/export\n【報告完畢】\n")
    w._drive(pw, stopped, "p2", judge)
    assert any("嚴格的驗收查核者" in c for c in judge.calls) and sess.load().done_at


def test_paused_session_makes_no_model_calls(tmp_path):
    w = _worker(tmp_path)
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "proj", "", "", False, False)
    sess = w._session_for(pw)
    sess.update(lambda st: (setattr(st, "goal", "G"), setattr(st, "paused_reason", "連續 3 次要不到合格的證據")))
    judge = FakeJudge()
    for i in range(3):
        w._drive(pw, {"agent_state": "waiting_input", "summary": "停"}, f"panel-{i}", judge)
    assert judge.calls == []


def test_followup_lists_only_gaps():
    f = drv2.build_evidence_followup(["補 exit code"], "")
    assert f.startswith(drv2.REPORT_MARK) and "1. 補 exit code" in f and drv2.REPORT_DONE in f
    assert "故意弄壞" not in f and "逐條對照" not in f  # 不重複整份報告格式
    assert len(f) < 200


class InferJudge(FakeJudge):
    def chat(self, prompt, image_png=None, max_tokens=0, temperature=None):
        self.calls.append(prompt[:40])
        if "推測這個專案目前開發的最終目標" in prompt:
            return json.dumps({"goal": "完成 U2 第二段 API", "current_task": "實作 /api/system/settings", "main_doc": ""},
                              ensure_ascii=False)
        return super().chat(prompt, image_png, max_tokens, temperature)


def _done_session(tmp_path, mode):
    w = _worker(tmp_path)
    pw = ProjectWindow(1, "kiro", "Kiro", "t", "proj", "", "", False, False)
    sess = w._session_for(pw)

    def setup(st):
        st.goal, st.acceptance_confirmed_at, st.done_at = "舊目標", now_str(), now_str()
        st.acceptance = [{"text": "A", "test": "t", "status": "verified"}]
    sess.update(setup)
    w.cfg.set_mode(pw.key, mode)
    return w, pw, sess


def test_after_done_new_work_autopilot_starts_new_goal(tmp_path):
    w, pw, sess = _done_session(tmp_path, "auto")
    judge = InferJudge()
    stopped = {"agent_state": "waiting_input", "summary": "代理問下一步"}
    w._drive(pw, stopped, "完成的對話", judge)       # 記下達成時的內容
    w._drive(pw, stopped, "完成的對話", judge)       # 沒有新內容：不動
    assert sess.load().goal == "舊目標"
    w._drive(pw, stopped, "新的對話：U2 第一段完成，要繼續嗎？", judge)
    st = sess.load()
    assert st.goal == "完成 U2 第二段 API" and st.goal_source == "inferred" and not st.done_at
    assert st.current_task.startswith("實作") and any("確認新目標" in p for p in st.pending_human)
    assert os.listdir(os.path.join(sess.dir, "archive"))  # 舊進度有封存


def test_after_done_new_work_suggest_mode_only_offers(tmp_path):
    w, pw, sess = _done_session(tmp_path, "suggest")
    judge = InferJudge()
    stopped = {"agent_state": "waiting_input", "summary": "停"}
    w._drive(pw, stopped, "完成的對話", judge)
    w._drive(pw, stopped, "新的對話", judge)
    st = sess.load()
    assert st.goal == "舊目標" and st.suggested_action == "new_goal" and "U2" in st.agent_goal_proposal


def test_notify_only_new_pending_items_rate_limited(tmp_path, monkeypatch):
    import automation
    sent = []
    monkeypatch.setattr(automation.wu, "notify", lambda t, b, launch="": sent.append(b) or True)
    monkeypatch.setattr(automation.wu, "get_foreground", lambda: 0)
    w = _worker(tmp_path)
    pw = ProjectWindow(7, "kiro", "Kiro", "t", "proj", "", str(tmp_path), False, False)
    sess = w._session_for(pw)
    sess.update(lambda st: st.pending_human.append("舊事項"))
    w._notify_pending(pw)
    assert sent == []  # 啟動時已存在的不通知
    sess.update(lambda st: st.pending_human.append("確認驗收 spec"))
    w._notify_pending(pw)
    assert sent == ["確認驗收 spec"]
    w._notify_pending(pw)
    assert len(sent) == 1  # 同一事項不重複
    sess.update(lambda st: st.pending_human.append("新的第二項"))
    w._notify_pending(pw)
    assert len(sent) == 1  # 60 秒內不再跳
    w._notify_at[pw.key] -= 61
    w._notify_pending(pw)
    assert sent[-1] == "新的第二項"
