import datetime
import os
import subprocess

import pytest

import driver_p2 as drv2
import verifier
from config import AppConfig
from sessions import SessionState, phase_of, render_progress


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feature/export")
    (tmp_path / "export.py").write_text("def export():\n    pass\n\n# TODO real export\n", encoding="utf-8")
    (tmp_path / "test_export.py").write_text("def test_export():\n    assert True\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "add export")
    h = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "--short", "HEAD"], capture_output=True,
                       text=True).stdout.strip()
    return tmp_path, h


GOOD_REPORT = """# 測試報告
指令：python -m pytest -q
輸出：3 passed in 0.12s
故意弄壞：把 export() 改成回傳空值 → 2 failed；改回後 3 passed；git status 乾淨
分支 feature/export，commit {h}
"""


def test_git_wrapper_rejects_write_commands(tmp_path):
    with pytest.raises(ValueError):
        verifier.git(str(tmp_path), "commit", "-m", "x")
    with pytest.raises(ValueError):
        verifier.git(str(tmp_path), "push")


def test_extract_hashes_ignores_plain_numbers():
    assert verifier.extract_hashes("commit 3f2a9c1 與 abcdef1234567, 1234567 passed") == ["3f2a9c1", "abcdef1234567"]


def test_gather_facts_finds_commit_branch_and_stubs(repo):
    path, h = repo
    facts = verifier.gather_facts(str(path), GOOD_REPORT.format(h=h) + "另外 commit deadbee 也是")
    assert facts["is_repo"] and facts["branch"] == "feature/export" and facts["dirty"] == []
    by_hash = {c["hash"]: c for c in facts["commits"]}
    assert by_hash[h]["exists"] and "export.py" in by_hash[h]["files"]
    assert by_hash["deadbee"]["exists"] is False
    kinds = {s["kind"] for s in facts["stubs"]}
    assert {"TODO / FIXME", "永遠成立的斷言", "只有 pass 的函式"} <= kinds
    issues = verifier.deterministic_issues(facts, GOOD_REPORT.format(h=h), remote=False)
    assert any("deadbee" in x for x in issues) and any("空殼跡象" in x for x in issues)
    assert "目前分支" in verifier.facts_summary(facts)


def test_deterministic_issues_main_branch_dirty_and_missing_sections(repo):
    path, h = repo
    _git(path, "checkout", "-q", "main")
    (path / "app.py").write_text("broken", encoding="utf-8")
    facts = verifier.gather_facts(str(path), "沒有測試輸出也沒有 commit")
    issues = verifier.deterministic_issues(facts, "全部完成了", remote=False)
    text = "\n".join(issues)
    for must in ("沒有看到測試輸出", "故意弄壞", "`main` 分支", "沒有提供 commit hash", "未 commit 的變更"):
        assert must in text
    assert verifier.deterministic_issues({}, "", remote=True) == ["沒有找到測試報告"]
    # 遠端專案只檢查報告內容
    assert verifier.deterministic_issues({}, "3 passed；故意弄壞後 1 failed", remote=True) == []


def test_spec_request_revise_and_parsers():
    st = SessionState(goal="CSV 匯出", active_docs=["docs/plans/x.md"])
    req = drv2.build_spec_request(st, r"D:\a\acceptance_draft.md")
    assert req.startswith(drv2.SPEC_MARK) and "CSV 匯出" in req and "acceptance_draft.md" in req and "x.md" in req
    assert "貼在聊天裡" in drv2.build_spec_request(st, "")
    rev = drv2.build_spec_revise(["第 2 條沒有測試"], "")
    assert rev.startswith(drv2.SPEC_MARK) and "1. 第 2 條沒有測試" in rev
    items = drv2.parse_spec_items('{"items":[{"text":"按匯出可下載 CSV","how":"手動","test":"pytest t.py"},{"text":""}]}')
    assert items == [{"text": "按匯出可下載 CSV", "how": "手動", "test": "pytest t.py", "status": "todo"}]
    assert drv2.parse_spec_items("nonsense") == []
    assert drv2.parse_spec_review('{"ok": true, "issues": [], "needs_user": []}') == (True, [], [])
    ok, issues, needs = drv2.parse_spec_review('{"ok": true, "issues": ["第 1 條不具體"], "needs_user": ["要不要支援 Excel"]}')
    assert ok is False and issues == ["第 1 條不具體"] and needs == ["要不要支援 Excel"]
    assert drv2.parse_spec_review("x")[0] is False


def test_evidence_request_contains_all_required_parts():
    st = SessionState(goal="G", acceptance=[{"text": "A", "test": "pytest a", "status": "verified"},
                                            {"text": "B", "test": "pytest b", "status": "todo"}])
    p = drv2.build_evidence_request(st, r"D:\s\reports\r.md", ["commit 不存在"])
    assert p.startswith(drv2.REPORT_MARK)
    for must in ("- B（測試：pytest b）", "完整輸出", "故意弄壞", "git status", "功能分支", "不要 push", "r.md",
                 "1. commit 不存在"):
        assert must in p
    assert "- A" not in p  # 已查核通過的不再要求


def test_parse_verify_det_issues_block_pass():
    raw = '{"items":[{"index":0,"verified":true,"reason":"有輸出"},{"index":5,"verified":true}],"issues":["補測試"]}'
    results, issues = drv2.parse_verify(raw, 2, [])
    assert results == [(0, True, "有輸出")] and issues == ["補測試"]
    results, issues = drv2.parse_verify(raw, 2, ["commit 不存在"])
    assert results == [(0, False, "有輸出")] and issues[0] == "commit 不存在"
    assert drv2.parse_verify("bad", 2, [])[1][-1].startswith("查核模型回覆無法解析")


def test_read_new_file(tmp_path):
    f = tmp_path / "r.md"
    f.write_text("report", encoding="utf-8")
    future = (datetime.datetime.now() + datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    past = (datetime.datetime.now() - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    assert drv2.read_new_file(str(f), past) == "report"
    assert drv2.read_new_file(str(f), future) == ""  # 要求報告之前就存在的舊檔不算
    assert drv2.read_new_file(str(tmp_path / "none.md"), past) == ""


def test_throttle_and_loop_detection():
    now = datetime.datetime(2026, 11, 16, 12, 0, 0)
    times = ["2026-11-16 11:10:00", "2026-11-16 11:59:00", "2026-11-16 10:00:00", "bad"]
    assert drv2.sends_last_hour(times, now) == 2
    a = "代理：我已完成，請問還有什麼需要？" * 5
    assert drv2.is_looping([a, a], a) is True
    assert drv2.is_looping([a], a) is False
    assert drv2.is_looping([a, "完全不同的內容" * 10], a) is False
    assert drv2.agent_tail("你好 \n\n Ask anything, @ to mention, / for actions") == "你好"


def test_continuation_prompt():
    st = SessionState(goal="G", active_docs=["PROGRESS.md"], last_issues=["補測試"],
                      acceptance=[{"text": "A", "status": "verified"}, {"text": "B", "status": "todo"}],
                      commits=[{"hash": "abc1234", "desc": "A 完成"}])
    p = drv2.build_continuation_prompt(st)
    assert p.startswith(drv2.CONTINUE_MARK)
    for must in ("G", "PROGRESS.md", "1. A", "1. B", "abc1234", "補測試", "不要重做"):
        assert must in p


def test_phase_of_and_render():
    st = SessionState()
    assert phase_of(st) == "goal"
    st.goal = "G"
    assert phase_of(st) == "spec"
    st.acceptance = [{"text": "A", "status": "todo"}]
    st.acceptance_confirmed_at = "t"
    assert phase_of(st) == "build"
    st.acceptance[0]["status"] = "verified"
    assert phase_of(st) == "done"
    st.paused_reason = "要不到證據"
    st.last_issues = ["commit 不存在"]
    md = render_progress(st)
    for must in ("## 階段", "④ 已達成目標", "自動駕駛已暫停：要不到證據", "最近一次查核發現的問題", "commit 不存在"):
        assert must in md


def test_config_blocks_plain_git_push_and_migrates(tmp_path):
    import json
    from automation import check_deny
    cfg = AppConfig()
    assert check_deny(["git push origin feature/x"], cfg.deny_patterns)
    old = tmp_path / "config.json"
    old.write_text(json.dumps({"deny_patterns": [r"\brm\s+-rf"]}), encoding="utf-8")
    loaded = AppConfig.load(str(old))
    assert r"git\s+push\b" in loaded.deny_patterns and r"\brm\s+-rf" in loaded.deny_patterns
