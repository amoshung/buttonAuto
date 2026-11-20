"""本機唯讀查核：只執行讀取類的 git 指令與讀檔，不修改專案。

用來交叉比對代理的測試報告：commit 是否真的存在、是否在功能分支、改了哪些檔、檔案裡有沒有空殼跡象。
"""
import ast
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple

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


def _py_abstract_not_implemented_lines(text: str) -> Set[int]:
    """回傳屬於 Python 抽象介面／基底類別方法的 raise NotImplementedError 行號，避免誤殺正規多型介面。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    classes: List[ast.ClassDef] = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not classes:
        return set()
    # 建立「基底類別名稱 -> 子類別覆寫過的方法集合」索引
    overridden_by_base: Dict[str, Set[str]] = {}
    for cls in classes:
        methods = {
            n.name for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for base in cls.bases:
            bname = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
            if bname:
                overridden_by_base.setdefault(bname, set()).update(methods)

    exempt: Set[int] = set()
    for cls in classes:
        base_names = {
            (b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else ""))
            for b in cls.bases
        }
        is_abc = bool(base_names & {"ABC", "Protocol"}) or any(
            kw.arg == "metaclass" and (
                (isinstance(kw.value, ast.Name) and kw.value.id == "ABCMeta")
                or (isinstance(kw.value, ast.Attribute) and kw.value.attr == "ABCMeta")
            )
            for kw in cls.keywords
        )
        sub_methods = overridden_by_base.get(cls.name, set())
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            has_abstract_dec = any(
                (isinstance(d, ast.Name) and d.id == "abstractmethod")
                or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
                for d in node.decorator_list
            )
            if not (is_abc or has_abstract_dec or node.name in sub_methods):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and sub.exc is not None:
                    exc = sub.exc.func if isinstance(sub.exc, ast.Call) else sub.exc
                    ename = exc.id if isinstance(exc, ast.Name) else (exc.attr if isinstance(exc, ast.Attribute) else "")
                    if ename == "NotImplementedError":
                        exempt.add(sub.lineno)
    return exempt


def scan_stubs(path: str, files: List[str], max_bytes: int = 400_000) -> List[Dict[str, str]]:
    hits: List[Dict[str, str]] = []
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        if ext not in _TEXT_EXT:
            continue
        full = os.path.join(path, rel)
        try:
            if os.path.getsize(full) > max_bytes:
                continue
            text = open(full, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        exempt_not_impl = _py_abstract_not_implemented_lines(text) if ext == ".py" else set()
        lines = text.splitlines()
        for label, pat in STUB_PATTERNS:
            for m in re.finditer(pat, text, re.M | re.I):
                line_no = text.count("\n", 0, m.start()) + 1
                if label == "NotImplemented" and line_no in exempt_not_impl:
                    continue
                line = lines[line_no - 1].strip() if lines else ""
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
        issues += facts.get("remote_issues") or []
        if not facts.get("is_repo"):
            return issues
    elif not facts.get("is_repo"):
        issues.append("專案路徑不是 git repo，無法查核 commit")
        return issues
    commits = facts.get("commits") or []
    existing = [c for c in commits if c.get("exists")]
    if existing and all(c.get("branches") for c in existing) and \
            all(set(c.get("branches") or []) <= _MAIN_BRANCHES for c in existing):
        issues.append("報告的 commit 都只在主分支上，請改在功能分支上實作與 commit")
    if not commits:
        issues.append("報告沒有提供 commit hash（請用「COMMIT: <hash> <分支>」的格式逐行列出）")
    for c in commits:
        if not c.get("exists") and not remote:  # 遠端的「不存在」已由 parse_remote_git 說明
            issues.append(f"報告提到的 commit `{c['hash']}` 在 repo 裡找不到")
    if facts.get("dirty"):
        issues.append("工作目錄還有未 commit 的變更（例如「故意弄壞」後沒改回來）：" + "、".join(facts["dirty"][:5]))
    for s in (facts.get("stubs") or [])[:8]:
        issues.append(f"`{s['file']}:{s['line']}` 有空殼跡象（{s['kind']}）：{s['text']}")
    return issues


# ---- 遠端專案（D12）：助理讀不到遠端 repo，改要代理貼出固定格式的 git 原始輸出，再檢查彼此是否一致
REMOTE_GIT_BEGIN, REMOTE_STATUS_END, REMOTE_GIT_END = "GIT-BEGIN", "GIT-STATUS-END", "GIT-END"


def remote_git_commands(hashes_hint: str = "<每個 COMMIT 的 hash>") -> str:
    return (f"echo {REMOTE_GIT_BEGIN}\n"
            "git rev-parse --abbrev-ref HEAD\n"
            "git status --porcelain\n"
            f"echo {REMOTE_STATUS_END}\n"
            "git log -n 10 --format=\"GITLOG %H %h %s\"\n"
            f"git show --stat --format=\"GITSHOW %H %s\" {hashes_hint}\n"
            f"echo {REMOTE_GIT_END}")


_GITLOG = re.compile(r"^\s*GITLOG\s+([0-9a-f]{40})\s+([0-9a-f]{4,40})\s*(.*)$")
_GITSHOW = re.compile(r"^\s*GITSHOW\s+([0-9a-f]{40})\s*(.*)$")
_STAT_FILE = re.compile(r"^\s*(\S.*?)\s+\|\s+(\d+|Bin)")
_STAT_SUMMARY = re.compile(r"\d+\s+files?\s+changed")
_COMMIT_BRANCH = re.compile(r"^\s*[-*>]?\s*COMMIT\s*[:：]\s*`?([0-9a-f]{7,40})`?\s+`?([^\s`]+)", re.I | re.M)


def parse_remote_git(report: str) -> Dict[str, Any]:
    """從報告裡的 GIT-BEGIN … GIT-END 區塊整理出和 gather_facts 相同形狀的事實，外加 remote_issues。"""
    text = report or ""
    facts: Dict[str, Any] = {"is_repo": False, "remote": True, "branch": "", "dirty": [], "commits": [], "recent": "",
                             "stubs": [], "remote_issues": []}
    issues: List[str] = facts["remote_issues"]
    b, e = text.rfind(REMOTE_GIT_BEGIN), text.rfind(REMOTE_GIT_END)
    # 指令本身也含有這些字（echo GIT-BEGIN）：取最後一個不是 echo 指令的位置
    lines = text[b:].splitlines() if b >= 0 else []
    if b < 0 or e < b:
        issues.append("遠端專案無法直接查核：請執行下列指令並把原始輸出原封不動貼進報告（不要整理或改寫）：\n"
                      + remote_git_commands())
        return facts
    # 略過終端機回顯的指令行與提示字元，只留下輸出
    block = [ln.rstrip() for ln in lines[1:]
             if not ln.strip().startswith(("echo ", "git ", "$ ", "PS ", "> ", "```"))]
    try:
        end = next(i for i, ln in enumerate(block) if ln.strip() == REMOTE_GIT_END)
        block = block[:end]
    except StopIteration:
        pass
    facts["is_repo"] = True
    head = [ln for ln in block if ln.strip()]
    facts["branch"] = head[0].strip() if head else ""
    try:
        st_end = next(i for i, ln in enumerate(block) if ln.strip() == REMOTE_STATUS_END)
        facts["dirty"] = [ln.strip() for ln in block[:st_end] if ln.strip()][1:31]
    except StopIteration:
        issues.append(f"git 原始輸出缺少「{REMOTE_STATUS_END}」那一行，無法確認工作目錄是否乾淨")
    log: Dict[str, str] = {}
    recent: List[str] = []
    shows: Dict[str, Dict[str, Any]] = {}
    cur: Optional[Dict[str, Any]] = None
    for ln in block:
        m = _GITLOG.match(ln)
        if m:
            full, short, subj = m.group(1), m.group(2), m.group(3).strip()
            if not full.startswith(short):
                issues.append(f"git log 輸出不一致：短 hash `{short}` 不是 `{full}` 的開頭（請貼原始輸出，不要手打）")
            log[full] = subj
            recent.append(f"{short} {subj}")
            cur = None
            continue
        m = _GITSHOW.match(ln)
        if m:
            cur = {"subject": m.group(2).strip(), "files": [], "summary": False}
            shows[m.group(1)] = cur
            continue
        if cur is not None:
            fm = _STAT_FILE.match(ln)
            if fm:
                cur["files"].append(fm.group(1).strip())
            elif _STAT_SUMMARY.search(ln):
                cur["summary"] = True
    facts["recent"] = "\n".join(recent)
    if not log:
        issues.append("沒有看到「GITLOG <完整 hash> <短 hash> <標題>」格式的 git log 原始輸出")
    branches = {h.lower(): br for h, br in _COMMIT_BRANCH.findall(text)}
    for h in claimed_hashes(text):
        full = next((f for f in log if f.startswith(h)), "")
        show_full = next((f for f in shows if f.startswith(h)), "")
        info: Dict[str, Any] = {"hash": h, "exists": bool(full), "subject": log.get(full, ""), "files": [],
                                "branches": [branches[h]] if h in branches else []}
        if not full:
            issues.append(f"COMMIT `{h}` 不在貼出的 git log 原始輸出裡")
        if not show_full:
            issues.append(f"缺少 COMMIT `{h}` 的 `git show --stat` 原始輸出（GITSHOW 開頭那段）")
        else:
            sh = shows[show_full]
            info["files"] = sh["files"][:40]
            if full and sh["subject"] != log[full]:
                issues.append(f"COMMIT `{h}` 在 git log 和 git show 的標題不一致")
            if not sh["summary"]:
                issues.append(f"COMMIT `{h}` 的 git show --stat 輸出缺少「N files changed」統計行")
        facts["commits"].append(info)
    return facts


def facts_summary(facts: Dict[str, Any]) -> str:
    if not facts.get("is_repo"):
        return "（無法在本機查核：不是 git repo 或是遠端專案）"
    lines = (["（遠端專案：以下依代理貼出的 git 原始輸出整理，助理已檢查彼此是否一致）"] if facts.get("remote") else []) + [
             f"目前分支：{facts.get('branch')}",
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
