"""基準測試（base commit 乾淨副本）與 git 外環境資料夾偵測／連結。"""
import os
import secrets
import shutil
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from team import checks, gitops, rounds
from team.gitops import GitError, Worktree

BASELINE_DIR = ".ai-team-baseline"
ENV_DIR_NAMES = {"reference", "data", "datasets", "models", "fixtures", "assets", "corpus", "samples"}
EXCLUDED_ENV_DIRS = set(checks.DEP_DIRS) | {
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    ".git",
}
ENV_DATA_EXTS = {".db", ".sqlite", ".sqlite3", ".pdf"}


@dataclass
class Baseline:
    commit: str
    failed: List[str] = field(default_factory=list)
    parsed: bool = False
    exit_code: int = -1
    tail: str = ""
    at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Baseline":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in names}
        data.setdefault("commit", "")
        return cls(**data)


def _has_data_file(folder: str, max_files: int = 500) -> bool:
    seen = 0
    for _root, _dirs, files in os.walk(folder):
        for fn in files:
            seen += 1
            if os.path.splitext(fn)[1].lower() in ENV_DATA_EXTS:
                return True
            if seen >= max_files:
                return False
    return False


def detect_env_dirs(repo: str, extra: Sequence[str] = ()) -> List[str]:
    """掃描主 repo 頂層被 .gitignore 排除的資料夾：名稱符合資料集慣例或內含 .db/.sqlite/.pdf 的列入，再加上 extra。"""
    if not repo or not os.path.isdir(repo):
        return []
    out: List[str] = []
    try:
        raw = gitops.git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        candidates = [x.replace("\\", "/").strip("/") for x in raw.split("\0") if x.strip()]
    except GitError:
        candidates = []
    top_dirs: List[str] = []
    for c in candidates:
        top = c.split("/", 1)[0]
        if top and top not in top_dirs:
            top_dirs.append(top)
    for d in top_dirs:
        if d in EXCLUDED_ENV_DIRS or d.startswith(".ai-team-"):
            continue
        full = os.path.join(repo, d)
        if not os.path.isdir(full):
            continue
        if d.lower() in ENV_DIR_NAMES or _has_data_file(full):
            out.append(d)
    for e in extra or ():
        rel = (e or "").strip().replace("\\", "/").strip("/")
        if rel and rel not in out and os.path.isdir(os.path.join(repo, rel.replace("/", os.sep))):
            out.append(rel)
    return out


def link_env_dirs(repo: str, wt_path: str, dirs: Sequence[str]) -> List[Tuple[str, str]]:
    """把主 repo 的環境資料夾連結進 worktree；回傳 [(dir, 問題)]，問題為空字串代表成功或目標已存在。"""
    res: List[Tuple[str, str]] = []
    for d in dirs or ():
        rel = (d or "").strip().replace("\\", "/").strip("/")
        if not rel:
            continue
        src = os.path.join(repo, rel.replace("/", os.sep))
        dst = os.path.join(wt_path, rel.replace("/", os.sep))
        if os.path.lexists(dst):
            res.append((rel, ""))
            continue
        if not os.path.isdir(src):
            res.append((rel, "找不到來源資料夾：%s" % rel))
            continue
        try:
            os.makedirs(os.path.dirname(dst) or wt_path, exist_ok=True)
            checks._link_dir(src, dst)
            res.append((rel, ""))
        except OSError as e:
            res.append((rel, str(e)))
    return res


def run_baseline(
    wt: Worktree,
    test_cmd: str,
    env_dirs: Sequence[str] = (),
    cancel: Optional[threading.Event] = None,
    deny_patterns: Optional[Sequence[str]] = None,
) -> Baseline:
    """在 wt.base 的乾淨 detached worktree 執行一次 test_cmd，記錄原本就失敗的測試清單。"""
    now = datetime.now().isoformat(timespec="seconds")
    cmd = (test_cmd or "").strip()
    if not cmd or checks.git_problem(cmd) or checks.deny_hit(cmd, list(deny_patterns) if deny_patterns else None):
        return Baseline(commit=wt.base, failed=[], parsed=False, exit_code=-1, tail="", at=now)
    parent = os.path.join(os.path.dirname(os.path.abspath(wt.path)), BASELINE_DIR)
    tmp = os.path.join(parent, "%s-%s" % (os.path.basename(os.path.abspath(wt.path)), secrets.token_hex(4)))
    state: Dict[str, Any] = {"added": False, "links": []}
    try:
        os.makedirs(parent, exist_ok=True)
        gitops.git(wt.path, "worktree", "add", "--detach", tmp, wt.base, timeout=gitops.WORKTREE_ADD_TIMEOUT)
        state["added"] = True
        for name in checks.DEP_DIRS:
            for base_root in (wt.path, wt.repo):
                src = os.path.join(base_root, name)
                dst = os.path.join(tmp, name)
                if os.path.isdir(src) and not os.path.lexists(dst):
                    try:
                        checks._link_dir(src, dst)
                        state["links"].append(dst)
                    except OSError:
                        pass
                    break
        for d in env_dirs or ():
            rel = (d or "").strip().replace("\\", "/").strip("/")
            if not rel:
                continue
            src = os.path.join(wt.repo, rel.replace("/", os.sep))
            dst = os.path.join(tmp, rel.replace("/", os.sep))
            if os.path.isdir(src) and not os.path.lexists(dst):
                try:
                    os.makedirs(os.path.dirname(dst) or tmp, exist_ok=True)
                    checks._link_dir(src, dst)
                    state["links"].append(dst)
                except OSError:
                    pass
        r = checks.run_cmd(tmp, cmd, env=checks.MUT_ENV, cancel=cancel)
    except (GitError, OSError) as e:
        return Baseline(commit=wt.base, failed=[], parsed=False, exit_code=-1, tail=str(e), at=now)
    finally:
        checks._cleanup_copy(wt, parent, tmp, state)
    if r.cancelled:
        return Baseline(commit=wt.base, failed=[], parsed=False, exit_code=-1, tail=checks.CANCELLED, at=now)
    failed = rounds.parse_failures(r.tail)
    return Baseline(
        commit=wt.base,
        failed=failed if failed is not None else [],
        parsed=failed is not None,
        exit_code=r.exit_code,
        tail=r.tail,
        at=now,
    )
