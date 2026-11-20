"""派工精靈用的專案掃描（只讀不寫）。

- known_projects：Kiro／Antigravity 開過的本機資料夾（workspaceStorage），加上呼叫端給的路徑。
- scan_project：「我看到的現況」：git 狀態、progress.md 的目前任務、最近的計畫／規格檔。
"""
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import projects
from sessions import read_kiro_specs, read_plans
from team import gitops
from team.gitops import GitError

MAX_PROJECTS = 30
MAX_DIRTY = 20
MAX_DOCS = 15
EXCERPT_CHARS = 800
_IGNORE_SUBDIRS = {
    ".git",
    ".ai-team-worktrees",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    "dist",
    "build",
    ".pytest_cache",
}


def _subdirs(path: str) -> List[str]:
    """回傳第一層非隱藏的子資料夾名稱（排序後），供派工精靈選擇工作目標 B。"""
    if not path or not os.path.isdir(path):
        return []
    out: List[str] = []
    try:
        for entry in os.listdir(path):
            if entry.startswith(".") or entry in _IGNORE_SUBDIRS:
                continue
            full = os.path.join(path, entry)
            if os.path.isdir(full):
                out.append(entry)
    except OSError:
        return []
    out.sort(key=lambda s: s.lower())
    return out

_KEYWORDS = r"(?:目前|當前|進行中|current|next|下一步)"
_HEADING = re.compile(r"^(#{1,6})\s*(.+?)\s*#*\s*$")
_KEY_HEADING = re.compile(_KEYWORDS, re.I)
_KEY_LINE = re.compile(r"^\s*(?:[-*+]\s+)?\**" + _KEYWORDS + r"[^：:\n]{0,12}[：:]\**\s*(\S.*)$", re.I)
_TODO = re.compile(r"^\s*[-*+]\s*\[ \]\s*(\S.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]\s+(?:\[[ xX]\]\s*)?|\d+[.)]\s+)")
_PROGRESS_NAME = re.compile(r"^progress[^\\/]*\.md$", re.I)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _local_path(path: str) -> str:
    # projects.parse_folder_uri 一律轉成 Windows 路徑；非 Windows 時換回正斜線
    return path.replace("\\", "/") if os.sep == "/" else path


def known_projects(extra: Iterable[str] = (), appdata: Optional[str] = None) -> List[Dict[str, str]]:
    """呼叫端給的路徑在前（原順序），再來是 IDE 開過的本機資料夾（依最近使用）；去重、最多 30 個。"""
    out: List[Dict[str, str]] = []
    seen = set()

    def add(path: str, source: str) -> None:
        key = _norm(path)
        if key in seen or len(out) >= MAX_PROJECTS:
            return
        seen.add(key)
        name = os.path.basename(os.path.normpath(path)) or path
        out.append({"name": name, "path": path, "repo": path, "source": source})

    for p in extra or ():
        if isinstance(p, str) and p.strip():
            add(p.strip(), "extra")
    entries = []
    for app in projects.APPS:
        try:
            ws = projects.load_workspaces(app.storage_dir, appdata)
        except OSError:
            continue
        for w in ws:
            if w.remote:
                continue  # 遠端專案沒辦法在本機建 worktree
            entries.append((w.mtime, _local_path(w.path), app.label))
    entries.sort(key=lambda e: e[0], reverse=True)
    for _, path, label in entries:
        if os.path.isdir(path):
            add(path, label)
    return out


# ---- progress.md ----
def _find_progress(path: str) -> str:
    """回傳相對路徑；根目錄優先於 docs/，同一處以 progress.md 優先，否則取最近修改的。"""
    for sub in ("", "docs"):
        d = os.path.join(path, sub) if sub else path
        try:
            names = [n for n in os.listdir(d) if _PROGRESS_NAME.match(n) and os.path.isfile(os.path.join(d, n))]
        except OSError:
            continue
        if not names:
            continue
        exact = [n for n in names if n.lower() == "progress.md"]
        pick = exact[0] if exact else max(names, key=lambda n: os.path.getmtime(os.path.join(d, n)))
        return os.path.join(sub, pick) if sub else pick
    return ""


def _sections(lines: List[str]) -> List[Dict[str, Any]]:
    """依標題切段：一段到下一個同級或更高級的標題為止（較深的子標題留在段內）；
    第一個標題之前的內容算一段（標題空字串）。"""
    secs = [{"title": "", "start": 0, "level": 0}]
    in_code = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else _HEADING.match(line)
        if m:
            secs.append({"title": m.group(2), "start": i, "level": len(m.group(1))})
    for i, s in enumerate(secs):
        s["end"] = len(lines)
        for nxt in secs[i + 1:]:
            if s["level"] == 0 or nxt["level"] <= s["level"]:
                s["end"] = nxt["start"]
                break
    return [s for s in secs if s["end"] > s["start"]]


def _clean(line: str) -> str:
    return _BULLET.sub("", line).strip().strip("*").strip()


def progress_task(text: str) -> Dict[str, str]:
    """task：標題含「目前／當前／進行中／Current／Next／下一步」那段的第一個非空行；
    或「目前任務：…」這種行；否則第一個 `- [ ]` 項目。excerpt：那一段的前 800 字。"""
    lines = text.splitlines()
    secs = _sections(lines)

    def excerpt(sec: Dict[str, Any]) -> str:
        return "\n".join(lines[sec["start"]:sec["end"]]).strip()[:EXCERPT_CHARS]

    def section_of(idx: int) -> Dict[str, Any]:
        inside = [s for s in secs if s["start"] <= idx < s["end"]]
        return max(inside, key=lambda s: s["start"]) if inside else {"start": 0, "end": len(lines)}

    for s in secs:
        if s["title"] and _KEY_HEADING.search(s["title"]):
            for line in lines[s["start"] + 1:s["end"]]:
                if _HEADING.match(line):
                    continue
                t = _clean(line)
                if t:
                    return {"task": t[:300], "excerpt": excerpt(s)}
    for i, line in enumerate(lines):
        if _HEADING.match(line):
            continue
        m = _KEY_LINE.match(line)
        if m and m.group(1).strip():
            return {"task": m.group(1).strip().strip("*").strip()[:300], "excerpt": excerpt(section_of(i))}
    for i, line in enumerate(lines):
        m = _TODO.match(line)
        if m:
            return {"task": m.group(1).strip()[:300], "excerpt": excerpt(section_of(i))}
    return {"task": "", "excerpt": text.strip()[:EXCERPT_CHARS]}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(200000)
    except OSError:
        return ""


# ---- 文件 ----
def _title(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(2)[:200]
    return fallback


def _docs(path: str) -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    try:
        plans = read_plans(path, limit=MAX_DOCS)
    except OSError:
        plans = []
    for p in plans:
        docs.append({"relpath": p["relpath"], "title": _title(p.get("head", ""), os.path.basename(p["relpath"])),
                     "modified": p["modified"]})
    try:
        specs = read_kiro_specs(path)
    except OSError:
        specs = []
    for s in specs:
        docs.append({"relpath": os.path.join(".kiro", "specs", s["name"]),
                     "title": _title(s.get("requirements_head", ""), s["name"]),
                     "modified": datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")})
    docs.sort(key=lambda d: d["modified"], reverse=True)
    return docs[:MAX_DOCS]


# ---- git ----
def _git_state(path: str) -> Dict[str, Any]:
    branch = ""
    dirty: List[str] = []
    try:
        branch = gitops.git(path, "symbolic-ref", "--short", "-q", "HEAD", timeout=10).strip()
    except GitError:
        pass
    try:
        out = gitops.git(path, "status", "--porcelain", "--untracked-files=normal", timeout=20)
        for line in out.splitlines():
            if len(dirty) >= MAX_DIRTY:
                break
            name = line[3:].strip()
            if " -> " in name:
                name = name.split(" -> ", 1)[1]
            if name:
                dirty.append(name.strip('"'))
    except GitError:
        pass
    return {"branch": branch, "dirty": dirty}


def _detect_checks(path: str) -> List[str]:
    checks: List[str] = []
    if (
        os.path.isfile(os.path.join(path, "pytest.ini"))
        or os.path.isfile(os.path.join(path, "pyproject.toml"))
        or os.path.isdir(os.path.join(path, "tests"))
    ):
        checks.append("pytest")
    pkg = os.path.join(path, "package.json")
    if os.path.isfile(pkg):
        try:
            import json
            with open(pkg, encoding="utf-8") as f:
                pdata = json.load(f)
            scripts = (pdata.get("scripts") or {}) if isinstance(pdata, dict) else {}
            if "test" in scripts:
                checks.append("npm test")
            if "lint" in scripts:
                checks.append("npm run lint")
        except Exception:
            pass
    if os.path.isfile(os.path.join(path, "Cargo.toml")):
        checks.append("cargo test")
    if os.path.isfile(os.path.join(path, "go.mod")):
        checks.append("go test ./...")
    return checks


def scan_project(path: str) -> Dict[str, Any]:
    path = (path or "").strip()
    full = os.path.abspath(path) if path else ""
    name = os.path.basename(os.path.normpath(full)) if full else ""
    out: Dict[str, Any] = {"path": full, "name": name, "problem": "", "branch": "", "dirty": [],
                           "subdirs": [], "checks": [],
                           "progress": {"file": "", "task": "", "excerpt": ""}, "docs": []}
    if not full or not os.path.isdir(full):
        out["problem"] = "找不到資料夾：%s" % (path or "（空白）")
        return out
    out["subdirs"] = _subdirs(full)
    out["checks"] = _detect_checks(full)
    out["problem"] = gitops.repo_problem(full)
    try:
        is_repo = gitops.git(full, "rev-parse", "--is-inside-work-tree", timeout=10).strip() == "true"
    except GitError:
        is_repo = False
    if is_repo:
        out.update(_git_state(full))
    rel = _find_progress(full)
    if rel:
        info = progress_task(_read(os.path.join(full, rel)))
        out["progress"] = {"file": rel, "task": info["task"], "excerpt": info["excerpt"]}
    out["docs"] = _docs(full)
    return out
