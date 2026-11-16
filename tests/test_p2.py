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
COMMIT: {h} feature/export
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
    facts = verifier.gather_facts(str(path), GOOD_REPORT.format(h=h) + "COMMIT: deadbee feature/export`n其他專案狀態：buttonAuto f1c131d 有變動")
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
    for must in ("沒有看到測試輸出", "故意弄壞", "沒有提供 commit hash", "未 commit 的變更"):
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


def test_parse_spec_lines_and_latest_segment():
    sent = (drv2.SPEC_MARK + "請提出驗收條件\n格式（每條一行）：\n1. 條件：… | 驗證方式：… | 測試：…\n請直接貼在聊天裡。\n")
    reply = ("好的，以下是驗收條件：\n"
             "1. 條件：slugify 轉小寫 | 驗證方式：比對輸出 | 測試：test_lower；執行 pytest -q\n"
             "2. 條件：合併連字號｜驗證方式：輸入 a--b 得到 a-b｜測試：test_dash\n"
             "說明文字，不是條件\n")
    seg = drv2.latest_spec_segment("舊對話\n" + sent + reply)
    items = drv2.parse_spec_lines(seg)
    assert [i["text"] for i in items] == ["slugify 轉小寫", "合併連字號"]
    assert items[0]["test"] == "test_lower；執行 pytest -q" and items[1]["how"] == "輸入 a--b 得到 a-b"
    assert drv2.parse_spec_lines(sent) == []  # 我們自己送出的格式範例不算


def test_claimed_hashes_and_branch_check(repo):
    path, h = repo
    report = f"測試 3 passed，故意弄壞後失敗\nCOMMIT: {h} feature/export\n其他專案 buttonAuto 最新為 abcdef1，與本次無關"
    assert verifier.claimed_hashes(report) == [h]
    loose = f"我在 feature/export 分支 commit {h}\n其他專案 abcdef1"
    assert verifier.claimed_hashes(loose) == [h]
    facts = verifier.gather_facts(str(path), report)
    assert facts["commits"][0]["branches"] == ["feature/export"]
    assert not any("主分支" in x for x in verifier.deterministic_issues(facts, report, remote=False))
    # 只在 main 上的 commit
    _git(path, "checkout", "-q", "main")
    (path / "only_main.py").write_text("x = 1\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "main only")
    rc = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    facts2 = verifier.gather_facts(str(path), f"COMMIT: {rc} main\n3 passed 故意弄壞")
    assert any("主分支" in x for x in verifier.deterministic_issues(facts2, "3 passed 故意弄壞", remote=False))
    with pytest.raises(ValueError):
        verifier.git(str(path), "branch", "-D", "feature/export")


def test_core_items_and_manual_items_drive_done():
    from sessions import core_items, is_automated
    auto_item = {"text": "slugify 轉小寫", "test": "test_textutils.py::test_lowercase", "status": "todo"}
    manual = {"text": "其他專案沒有被改動", "test": "無（快照比對）", "status": "todo"}
    handcheck = {"text": "README 有說明", "test": "無（手動指令）`git log`", "status": "todo"}
    assert is_automated(auto_item) and not is_automated(manual) and not is_automated(handcheck)
    st = SessionState(goal="G", acceptance_confirmed_at="t", acceptance=[auto_item, manual, handcheck])
    assert core_items(st) == [auto_item]
    assert phase_of(st) == "build"
    auto_item["status"] = "verified"
    assert phase_of(st) == "done"  # 手動項目不擋住完成，最後列給使用者確認
    only_manual = SessionState(goal="G", acceptance_confirmed_at="t", acceptance=[dict(manual)])
    assert core_items(only_manual) == only_manual.acceptance  # 全是手動項目時，全部都要查核


def test_evidence_request_lists_only_core_items_and_latest_reply():
    st = SessionState(goal="G", acceptance=[
        {"text": "核心 A", "test": "test_a", "status": "todo"},
        {"text": "環境 B", "test": "無（快照比對）", "status": "todo"}])
    req = drv2.build_evidence_request(st, "")
    assert "- 核心 A" in req and "- 環境 B" not in req and "1 條手動檢查項目" in req
    assert req.rstrip().endswith(drv2.REQUEST_END)
    chat = "舊回覆：3 passed\n" + req + "\n代理：COMMIT: abc1234 feature/x\n4 passed"
    reply = drv2.latest_reply(chat)
    assert "COMMIT: abc1234" in reply and "舊回覆" not in reply and "核心 A" not in reply
    assert drv2.latest_reply("沒有要求的對話") == "沒有要求的對話"
    verify = drv2.build_verify_prompt(st, "r", "facts", [])
    assert "0. 核心 A" in verify and "環境 B" not in verify


def test_mutation_evidence_remembered():
    assert verifier.has_mutation_evidence("故意弄壞後 2 failed")
    later = verifier.deterministic_issues({}, "補正：4 passed", remote=True, mutation_seen=True)
    assert not any("故意弄壞" in x for x in later)
    assert any("故意弄壞" in x for x in verifier.deterministic_issues({}, "4 passed", remote=True))


def _remote_report(full="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678", short="a1b2c3d", show_subject="add word_count",
                   dirty="", summary=True):
    stat = " wc.py | 12 ++++++++++++\n test_wc.py | 20 ++++++++++++++++++++\n"
    if summary:
        stat += " 2 files changed, 32 insertions(+)\n"
    return ("6 passed\n故意弄壞：失敗 1 個\nCOMMIT: a1b2c3d feature/wc\n```\n"
            "$ echo GIT-BEGIN\nGIT-BEGIN\nfeature/wc\n" + dirty + "GIT-STATUS-END\n"
            f"GITLOG {full} {short} add word_count\n"
            "GITLOG 0000000000000000000000000000000000000abc 0000000 init\n"
            f"GITSHOW {full} {show_subject}\n" + stat + "GIT-END\n```\n")


def test_remote_git_output_consistent_passes():
    from verifier import deterministic_issues, parse_remote_git
    rep = _remote_report()
    f = parse_remote_git(rep)
    assert f["branch"] == "feature/wc" and f["dirty"] == []
    assert f["commits"][0]["exists"] and f["commits"][0]["files"] == ["wc.py", "test_wc.py"]
    assert deterministic_issues(f, rep, remote=True) == []


def test_remote_git_output_inconsistencies_are_issues():
    from verifier import deterministic_issues, parse_remote_git
    bad = _remote_report(short="ffffff1", show_subject="something else", dirty=" M wc.py\n", summary=False)
    issues = deterministic_issues(parse_remote_git(bad), bad, remote=True)
    text = "\n".join(issues)
    assert "不是" in text and "標題不一致" in text and "files changed" in text and "未 commit" in text
    missing = deterministic_issues(parse_remote_git("6 passed\n故意弄壞\nCOMMIT: a1b2c3d feature/wc"),
                                   "6 passed\n故意弄壞", remote=True)
    assert any("GIT-BEGIN" in i for i in missing)
