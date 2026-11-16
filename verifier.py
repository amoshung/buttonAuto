"""本機唯讀查核：只執行讀取類的 git 指令與讀檔，不修改專案。

用來交叉比對代理的測試報告：commit 是否真的存在、是否在功能分支、改了哪些檔、檔案裡有沒有空殼跡象。
"""
import os
import re
import subprocess
from typing import Any, Dict, List, Tuple

# 只允許讀取類的 git 子指令
_READONLY_GIT = {"rev-parse", "status", "cat-file", "show", "log", "diff", "branch"}
# git branch 只允許查詢用的參數（不允許建立、刪除、改名分支）
_BRANCH_QUERY = {"--contains", "--show-current", "--list", "-a", "--all", "--format=%(refname:short)"}
_MAIN_BRANCHES = {"main", "master", "develop", "trunk"}
_TEXT_EXT = {".py", ".rs", ".cs", ".vb", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".kt", ".c", ".cc", ".cpp",
             ".h", ".hpp", ".rb", ".php", ".swift", ".sql", ".ps1", ".sh"}

# 空殼跡象：(說明, 正規表示式)
STUB_PATTERNS: List[Tuple[str, str]] = [
    ("TODO / FIXME", r"\b(TODO|FIXME|XXX)\b"),
    ("NotImplemented", r"NotImplementedError|NotImplementedException|unimplemented!\(|todo!\(|throw new NotImplemented"),
    ("永遠成立的斷言", r"\bassert\s+True\b|assertTrue\(\s*true\s*\)|Assert\.True\(\s*true\s*\)|assert!\(\s*true\s*\)"),
    ("被略過的測試", r"@pytest\.mark\.skip|@unittest\.skip|\bit\.skip\(|\bxit\(|\[Ignore\]|#\[ignore\]|\.skip\("),
    ("只有 pass 的函式", r"def [A-Za-z_]\w*\([^)]*\)\s*(->\s*[^:]+)?:\s*\n\s+pass\s*$"),
    ("假資料 / mock 回傳", r"return\s+(mock|fake|dummy)_?\w*|#\s*(mock|fake|placeholder)"),
]


def git(path: str, *args: str, timeout: float = 15.0) -> Tuple[int, str]:
    if not args or args[0] not in _READONLY_GIT:
        raise ValueError(f"不允許的 git 指令：{args[:1]}")
    if args[0] == "branch" and not any(a in _BRANCH_QUERY for a in args[1:]):
        raise ValueError("git branch 只允許查詢")
    try:
        p = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr if p.returncode else "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def extract_hashes(text: str, require_letter: bool = True) -> List[str]:
    """報告或聊天裡提到的 commit hash（7~40 個十六進位字元，且至少有一個字母，避免把數字當成 hash）。"""
    out: List[str] = []
    for m in re.finditer(r"(?<![0-9a-fA-F])([0-9a-f]{7,40})(?![0-9a-fA-F])", text or ""):
        h = m.group(1)
        if (not require_letter or re.search(r"[a-f]", h)) and h not in out:
            out.append(h)
    return out[:20]


_CLAIM_LINE = re.compile(r"^\s*[-*>]?\s*COMMIT\s*[:：]", re.I | re.M)
_COMMIT_WORD = re.compile(r"commit|hash|提交|分支|branch", re.I)


def claimed_hashes(report: str) -> List[str]:
    """代理宣稱「這是我的 commit」的 hash。

    優先只看「COMMIT: <hash> <分支>」格式的行；沒有這種行時，才看含有 commit / hash / 分支字樣的行。
    報告中其他地方出現的 hash（例如引用別的專案的 git 狀態）不算，避免把無關的 hash 當成假 commit。
    """
    lines = (report or "").splitlines()
    picked = [ln for ln in lines if _CLAIM_LINE.match(ln)]
    if not picked:
        picked = [ln for ln in lines if _COMMIT_WORD.search(ln)]
    out: List[str] = []
    for ln in picked:
        for h in extract_hashes(ln, require_letter=not _CLAIM_LINE.match(ln)):
            if h not in out:
                out.append(h)
    return out[:20]


def scan_stubs(path: str, files: List[str], max_bytes: int = 400_000) -> List[Dict[str, str]]:
    hits: List[Dict[str, str]] = []
    for rel in files:
        if os.path.splitext(rel)[1].lower() not in _TEXT_EXT:
            continue
        full = os.path.join(path, rel)
        try:
            if os.path.getsize(full) > max_bytes:
                continue
            text = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for label, pat in STUB_PATTERNS:
            for m in re.finditer(pat, text, re.M | re.I):
                line_no = text.count("\n", 0, m.start()) + 1
                line = text.splitlines()[line_no - 1].strip() if text.splitlines() else ""
                hits.append({"file": rel.replace("\\", "/"), "line": str(line_no), "kind": label, "text": line[:120]})
                if len(hits) >= 40:
                    return hits
    return hits


def gather_facts(path: str, report_text: str) -> Dict[str, Any]:
    """收集可獨立查核的事實。path 不是 git repo 時 is_repo=False。"""
    facts: Dict[str, Any] = {"is_repo": False, "branch": "", "dirty": [], "commits": [], "recent": "", "stubs": []}
    if not path or not os.path.isdir(path):
        return facts
    rc, out = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return facts
    facts["is_repo"] = True
    facts["branch"] = out.strip()
    rc, out = git(path, "status", "--porcelain")
    facts["dirty"] = [ln for ln in out.splitlines() if ln.strip()][:30] if rc == 0 else []
    rc, out = git(path, "log", "--oneline", "-n", "15")
    facts["recent"] = out.strip() if rc == 0 else ""
    changed: List[str] = []
    for h in claimed_hashes(report_text):
        rc, _ = git(path, "cat-file", "-e", f"{h}^{{commit}}")
        info: Dict[str, Any] = {"hash": h, "exists": rc == 0, "subject": "", "files": [], "branches": []}
        if rc == 0:
            rc3, out3 = git(path, "branch", "--contains", h, "--format=%(refname:short)")
            info["branches"] = [b.strip() for b in out3.splitlines() if b.strip()] if rc3 == 0 else []
            rc2, out2 = git(path, "show", "--name-only", "--format=%s", h)
            if rc2 == 0:
                lines = [ln for ln in out2.splitlines() if ln.strip()]
                info["subject"] = lines[0][:120] if lines else ""
                info["files"] = lines[1:40]
                changed += info["files"]
        facts["commits"].append(info)
    facts["stubs"] = scan_stubs(path, sorted(set(changed)))
    return facts


_TEST_OUTPUT = re.compile(r"\b\d+\s+(passed|failed|tests?|個測試)|test result:|Tests? run:|PASS|FAIL|OK \(\d+ tests?\)|"
                          r"\d+\s+passing|Passed!|通過", re.I)
_MUTATION = re.compile(r"故意弄壞|mutation|改壞|破壞測試", re.I)


def has_mutation_evidence(report_text: str) -> bool:
    return bool(_MUTATION.search(report_text or ""))


def deterministic_issues(facts: Dict[str, Any], report_text: str, remote: bool, mutation_seen: bool = False) -> List[str]:
    """不需要 AI 就能確定的問題。有任何一項，該次查核就不能算通過。"""
    issues: List[str] = []
    if not report_text.strip():
        return ["沒有找到測試報告"]
    if not _TEST_OUTPUT.search(report_text):
        issues.append("報告裡沒有看到測試輸出（通過／失敗數量），請貼出實際執行的完整輸出")
    if not mutation_seen and not _MUTATION.search(report_text):
        issues.append("報告缺少「故意弄壞」的結果：請把被測的關鍵函式暫時改壞、證明測試會失敗，再改回並證明恢復通過")
    if remote:
        return issues
    if not facts.get("is_repo"):
        issues.append("專案路徑不是 git repo，無法查核 commit")
        return issues
    commits = facts.get("commits") or []
    existing = [c for c in commits if c.get("exists")]
    if existing and all(set(c.get("branches") or []) <= _MAIN_BRANCHES for c in existing):
        issues.append("報告的 commit 都只在主分支上，請改在功能分支上實作與 commit")
    if not commits:
        issues.append("報告沒有提供 commit hash（請用「COMMIT: <hash> <分支>」的格式逐行列出）")
    for c in commits:
        if not c.get("exists"):
            issues.append(f"報告提到的 commit `{c['hash']}` 在 repo 裡找不到")
    if facts.get("dirty"):
        issues.append("工作目錄還有未 commit 的變更（例如「故意弄壞」後沒改回來）：" + "、".join(facts["dirty"][:5]))
    for s in (facts.get("stubs") or [])[:8]:
        issues.append(f"`{s['file']}:{s['line']}` 有空殼跡象（{s['kind']}）：{s['text']}")
    return issues


def facts_summary(facts: Dict[str, Any]) -> str:
    if not facts.get("is_repo"):
        return "（無法在本機查核：不是 git repo 或是遠端專案）"
    lines = [f"目前分支：{facts.get('branch')}",
             f"未 commit 的變更：{len(facts.get('dirty') or [])} 項",
             "報告提到的 commit："]
    for c in facts.get("commits") or []:
        mark = "存在" if c.get("exists") else "不存在"
        lines.append(f"- {c['hash']}（{mark}；所在分支：{', '.join(c.get('branches', [])) or '—'}）{c.get('subject', '')}；"
                     f"檔案：{', '.join(c.get('files', [])[:10])}")
    if not facts.get("commits"):
        lines.append("（無）")
    lines.append("空殼跡象：" + ("；".join(f"{s['file']}:{s['line']} {s['kind']}" for s in facts.get("stubs", [])[:10])
                             or "（無）"))
    lines.append("最近的 commit：\n" + (facts.get("recent") or "（無）"))
    return "\n".join(lines)
