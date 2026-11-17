"""實作者完成時的結構化 JSON 證據契約解析與辦公室安全重跑核對（設計 §5.3.3）。"""
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from team import checks, gitops, headless, personas
from team.gitops import GitError, Worktree

STATUSES = ("DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT")
BLOCK_CATS = ("check", "code", "env", "system")
EVIDENCE_TAIL_LINES = 20


@dataclass
class Report:
    status: str
    criteria: List[Dict[str, Any]] = field(default_factory=list)
    tests: Dict[str, Any] = field(default_factory=dict)
    concerns: List[str] = field(default_factory=list)
    blocker: Dict[str, Any] = field(default_factory=dict)
    recommend: Dict[str, str] = field(default_factory=dict)


def parse_report(text: str) -> Tuple[Optional[Report], str]:
    """解析實作者最後一個 ```json 區塊；失敗回 (None, 中文原因)。"""
    data = personas.parse_json_block(text or "")
    if data is None:
        return None, "回覆缺少可解析的 ```json 證據區塊"
    status = str(data.get("status") or "").strip()
    if status not in STATUSES:
        return None, "status 必須是 %s 之一（實際為 %r）" % (" / ".join(STATUSES), status)
    raw_crit = data.get("criteria", [] if status in ("BLOCKED", "NEEDS_CONTEXT") else None)
    if not isinstance(raw_crit, list):
        return None, "criteria 必須是陣列"
    crit: List[Dict[str, Any]] = []
    for i, item in enumerate(raw_crit):
        if not isinstance(item, dict):
            return None, "criteria 第 %d 項必須是物件" % (i + 1)
        crit.append(dict(item))
    tests = dict(data["tests"]) if isinstance(data.get("tests"), dict) else {}
    concerns = [str(x).strip() for x in (data.get("concerns") or []) if str(x).strip()] if isinstance(
        data.get("concerns"), list
    ) else []
    blocker = dict(data["blocker"]) if isinstance(data.get("blocker"), dict) else {}
    rec_raw = data.get("recommend_capability")
    recommend: Dict[str, str] = {}
    if isinstance(rec_raw, dict) and str(rec_raw.get("name") or "").strip():
        recommend = {
            "name": str(rec_raw.get("name") or "").strip(),
            "reason": str(rec_raw.get("reason") or "").strip(),
        }
    return Report(
        status=status,
        criteria=crit,
        tests=tests,
        concerns=concerns,
        blocker=blocker,
        recommend=recommend,
    ), ""


def rerunnable(
    cmd: str,
    criteria: Sequence[Dict[str, Any]],
    test_cmd: str,
    deny_patterns: Optional[Sequence[str]] = None,
) -> str:
    """回傳不可重跑的原因（空字串＝可安全重跑）。"""
    c = (cmd or "").strip()
    if not c:
        return "沒有提供指令"
    hit = checks.deny_hit(c, list(deny_patterns) if deny_patterns else None)
    if hit:
        return "命中危險規則（%s）" % hit
    gprob = checks.git_problem(c)
    if gprob:
        return gprob
    official_checks = {
        (x.get("check") or "").strip()
        for x in (criteria or ())
        if (x.get("check") or "").strip()
        and (x.get("check") or "").strip().lower() != "manual"
        and not bool(x.get("env_dependent"))
    }
    tcmd = (test_cmd or "").strip()
    if c in official_checks or (tcmd and c == tcmd):
        return ""
    if headless.runner_tool_pattern(c):
        return ""
    return "不在允許的測試工具清單或含串接／管線"


def verify(
    wt: Worktree,
    report: Report,
    criteria: Sequence[Dict[str, Any]],
    test_cmd: str,
    deny_patterns: Optional[Sequence[str]] = None,
    cancel: Optional[threading.Event] = None,
) -> Tuple[List[str], List[str]]:
    """重跑核對實作者自報的證據；回傳 (mismatches, notes)。不可重跑的指令絕不執行，只進 notes。"""
    mismatches: List[str] = []
    notes: List[str] = []
    by_id: Dict[str, Dict[str, Any]] = {}
    ran_cache: Dict[str, checks.CmdResult] = {}

    def _run_once(c_str: str) -> checks.CmdResult:
        if c_str not in ran_cache:
            ran_cache[c_str] = checks.run_cmd(wt.path, c_str, cancel=cancel)
        return ran_cache[c_str]

    for item in report.criteria or []:
        cid = str(item.get("id") or "?").strip()
        by_id[cid] = item
        sha = str(item.get("commit") or "").strip()
        if not sha:
            mismatches.append("條件 %s：證據缺少 commit" % cid)
        else:
            try:
                gitops.git(wt.path, "merge-base", "--is-ancestor", sha, "HEAD")
            except GitError:
                mismatches.append("條件 %s：自報的 commit %s 不存在或不在目前分支" % (cid, sha))

        cmd = str(item.get("cmd") or "").strip()
        rep_exit = item.get("exit")
        why = rerunnable(cmd, criteria, test_cmd, deny_patterns)
        if why:
            notes.append("條件 %s 未重跑（%s）：%s" % (cid, why, cmd))
        else:
            res = _run_once(cmd)
            if res.cancelled:
                break
            if rep_exit is not None:
                try:
                    expected = int(rep_exit)
                except (TypeError, ValueError):
                    expected = None
                if expected is not None and res.exit_code != expected:
                    mismatches.append(
                        "條件 %s：自報 exit %s，但重跑結束碼為 %d（%s）" % (cid, rep_exit, res.exit_code, cmd)
                    )
            if res.exit_code != 0:
                mismatches.append("條件 %s：重跑自報指令未通過（結束碼 %d）：%s" % (cid, res.exit_code, cmd))

    for c in criteria or ():
        cid = str(c.get("id") or "?").strip()
        check = str(c.get("check") or "").strip()
        if not check or check.lower() == "manual" or bool(c.get("env_dependent")):
            continue
        rep_item = by_id.get(cid)
        if rep_item is None:
            mismatches.append("條件 %s：證據中未列出此條件" % cid)
            continue
        why_off = rerunnable(check, criteria, test_cmd, deny_patterns)
        if not why_off:
            res = _run_once(check)
            if res.cancelled:
                break
            if res.exit_code != 0:
                mismatches.append("條件 %s：條件檢查實際未通過（結束碼 %d）：%s" % (cid, res.exit_code, check))
    return mismatches, notes
