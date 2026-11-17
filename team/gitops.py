"""git worktree 與差異：每個房間一個 worktree＋分支 ai-team/<slug>。

- 一律以參數串列呼叫 git（不用 shell），輸出以 UTF-8 解碼；列檔名時關閉 quotepath。
- 不自動合併；merge_into_repo 只在使用者確認後由 office 呼叫，且主 repo 必須乾淨。
- 本模組自己做的 commit 一律帶 ai-team 身分，沒有全域 git 身分的機器也能運作。
"""
import fnmatch
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import List, Tuple

# 助理自己的 commit／merge：不簽章（使用者若設了 commit.gpgsign，沒有金鑰或 pinentry 會卡住或失敗）
IDENTITY = ["-c", "user.name=ai-team", "-c", "user.email=ai-team@local", "-c", "commit.gpgsign=false"]
WORKTREE_ADD_TIMEOUT = 900  # 大 repo checkout（加上防毒掃描）可能要好幾分鐘
LEGACY_DOC_DIRS = ["docs/ai-team"]
WT_DIR = ".ai-team-worktrees"


class GitError(Exception):
    pass


@dataclass
class Worktree:
    repo: str
    path: str
    branch: str
    base: str
    target_branch: str = ""


def git(cwd: str, *args: str, timeout: float = 60) -> str:
    # core.longpaths：Windows 上路徑超過 260 字元的檔案（常見於 reference/ 這類資料夾）才建得出來。
    cmd = ["git", "-c", "core.quotepath=false", "-c", "core.longpaths=true", *args]
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise GitError("找不到 git，請先安裝 git 並加入 PATH")
    except subprocess.TimeoutExpired:
        raise GitError("git %s 逾時（%s 秒）" % (" ".join(args[:2]), timeout))
    except OSError as e:
        raise GitError("無法執行 git：%s" % e)
    out = p.stdout.decode("utf-8", errors="replace")
    if p.returncode != 0:
        err = _clean_err(p.stderr.decode("utf-8", errors="replace")) or out.strip()[-1500:]
        raise GitError("git %s 失敗：%s%s" % (" ".join(args[:2]), err, _hint(err)))
    return out


_NOISE = re.compile(r"^(Updating files:|Checking out files:|Preparing worktree|Updating index|Receiving objects:|Resolving deltas:)")


def _clean_err(text: str) -> str:
    """去掉 git 的進度列（\r 分段），只留真正的錯誤訊息，最多 12 行。"""
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text)]
    keep = [ln for ln in lines if ln and not _NOISE.match(ln)]
    return "\n".join(keep[-12:])


def _hint(err: str) -> str:
    if "Filename too long" in err:
        return ("\n建議：Windows 路徑太長。助理已用 core.longpaths 執行 git；若仍失敗，請在 Windows 開啟長路徑支援"
                "（以系統管理員執行：git config --system core.longpaths true，並在「本機群組原則→系統→檔案系統→啟用 Win32 長路徑」打開），"
                "或把專案移到較短的路徑後按「重試」。")
    return ""


def _ok(cwd: str, *args: str) -> bool:
    try:
        git(cwd, *args)
        return True
    except GitError:
        return False


def repo_problem(path: str) -> str:
    if not os.path.isdir(path) or not _ok(path, "rev-parse", "--is-inside-work-tree"):
        return "不是 git repo，請先 git init 並提交一次"
    if not _ok(path, "rev-parse", "--verify", "-q", "HEAD"):
        return "repo 還沒有任何 commit"
    if not _ok(path, "symbolic-ref", "-q", "HEAD"):
        return "目前不在任何分支上（detached HEAD）"
    return ""


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24].strip("-")
    return s or "task-" + uuid.uuid4().hex[:6]


def _branch_exists(repo: str, branch: str) -> bool:
    return _ok(repo, "show-ref", "--verify", "-q", "refs/heads/" + branch)


def _wt_branch(path: str) -> str:
    try:
        return git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except GitError:
        return ""


def _cfg_get(repo: str, branch: str, key: str) -> str:
    try:
        return git(repo, "config", "--local", "--get", "branch.%s.%s" % (branch, key)).strip()
    except GitError:
        return ""


def _cur_branch(repo: str) -> str:
    try:
        return git(repo, "symbolic-ref", "--short", "-q", "HEAD").strip()
    except GitError:
        return ""


def create_worktree(repo: str, slug: str, base: str = "") -> Worktree:
    prob = repo_problem(repo)
    if prob:
        raise GitError(prob)
    _ok(repo, "worktree", "prune")  # 清掉手動刪除目錄留下的登記
    root = os.path.join(os.path.dirname(os.path.abspath(repo)), WT_DIR)
    name = os.path.basename(os.path.abspath(repo))
    cur = _cur_branch(repo)
    n = 1
    reuse = False
    while True:
        cand = slug if n == 1 else "%s-%d" % (slug, n)
        branch = "ai-team/" + cand
        path = os.path.join(root, "%s-%s" % (name, cand))
        have_branch = _branch_exists(repo, branch)
        ours = have_branch and bool(_cfg_get(repo, branch, "aiTeamBase"))
        if os.path.isdir(path) and _wt_branch(path) == branch:
            target = _cfg_get(repo, branch, "aiTeamTarget") or cur
            b0 = base or _cfg_get(repo, branch, "aiTeamBase")
            if not b0:
                try:
                    b0 = git(repo, "merge-base", target or "HEAD", branch).strip()
                except GitError:
                    b0 = git(repo, "rev-parse", "HEAD").strip()
            return Worktree(repo, path, branch, b0, target)
        if not os.path.exists(path):
            if not have_branch:
                break
            if ours:  # 目錄被手動刪除，分支還在：接回
                reuse = True
                break
        n += 1
    if reuse:
        base_sha = base or _cfg_get(repo, branch, "aiTeamBase")
        target = _cfg_get(repo, branch, "aiTeamTarget") or cur
    else:
        base_sha = git(repo, "rev-parse", "HEAD").strip()
        target = cur
    made_root = not os.path.isdir(root)
    try:
        os.makedirs(root, exist_ok=True)
        if os.name == "nt":
            # 讓代理自己在 worktree 裡跑的 git 也能處理長路徑（只寫這個 repo 的設定）
            _ok(repo, "config", "--local", "core.longpaths", "true")
        if reuse:
            git(repo, "worktree", "add", path, branch, timeout=WORKTREE_ADD_TIMEOUT)
        else:
            git(repo, "worktree", "add", "-b", branch, path, base_sha, timeout=WORKTREE_ADD_TIMEOUT)
            git(repo, "config", "--local", "branch.%s.aiTeamBase" % branch, base_sha)
            git(repo, "config", "--local", "branch.%s.aiTeamTarget" % branch, target)
    except (GitError, OSError) as e:
        shutil.rmtree(path, ignore_errors=True)
        _ok(repo, "worktree", "prune")
        if not reuse and _branch_exists(repo, branch):
            _ok(repo, "branch", "-D", branch)
        if made_root:
            try:
                os.rmdir(root)
            except OSError:
                pass
        raise GitError("建立 worktree 失敗：%s" % e)
    return Worktree(repo, path, branch, base_sha, target)


def _lines(out: str, sep: str = "\n") -> List[str]:
    return [x for x in out.split(sep) if x]


def uncommitted(wt: Worktree) -> List[str]:
    tracked = _lines(git(wt.path, "diff", "--no-renames", "--name-only", "-z", "HEAD"), "\0")
    untracked = _lines(git(wt.path, "ls-files", "--others", "--exclude-standard", "-z"), "\0")
    return sorted(set(tracked) | set(untracked))


def changed_files(wt: Worktree) -> List[str]:
    committed = _lines(git(wt.path, "diff", "--no-renames", "--name-only", "-z", wt.base + "..HEAD"), "\0")
    all_files = sorted(set(committed) | set(uncommitted(wt)))
    prefixes = tuple(d.strip("/") + "/" for d in LEGACY_DOC_DIRS)
    return [
        f for f in all_files
        if not (f.replace("\\", "/").startswith(prefixes) and not os.path.exists(os.path.join(wt.path, f)))
    ]


def commits_since(wt: Worktree) -> List[Tuple[str, str]]:
    out = git(wt.path, "log", "--reverse", "--format=%h%x09%s", wt.base + "..HEAD")
    res = []
    for line in _lines(out):
        sha, _, subj = line.partition("\t")
        if subj == "ai-team: 移除助理工作文件":
            continue
        res.append((sha, subj))
    return res


def outside_allowed(files: List[str], allowed: List[str]) -> List[str]:
    if not allowed:
        return []
    out = []
    for f in files:
        f = f.replace("\\", "/")
        ok = False
        for a in (x.replace("\\", "/") for x in allowed):
            if a.endswith("/"):
                ok = f.startswith(a)
            else:
                ok = fnmatch.fnmatch(f, a)
            if ok:
                break
        if not ok:
            out.append(f)
    return out


def diff_stat(wt: Worktree) -> str:
    return git(wt.path, "diff", "--no-renames", "--stat=200", wt.base + "..HEAD")


def diff_text(wt: Worktree, max_chars: int = 60000) -> str:
    text = git(wt.path, "diff", "--no-renames", wt.base + "..HEAD")
    if len(text) > max_chars:
        return text[:max_chars] + "\n…（差異過長，已截斷）"
    return text


def merge_into_repo(wt: Worktree) -> str:
    prob = repo_problem(wt.repo)
    if prob:
        raise GitError(prob)
    cur = _cur_branch(wt.repo)
    if wt.target_branch and cur != wt.target_branch:
        raise GitError("主專案目前在 %s 分支，派工時是 %s；請切回 %s 再合併"
                       % (cur, wt.target_branch, wt.target_branch))
    if _ok(wt.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD"):
        raise GitError("主專案有尚未完成的合併")
    if git(wt.repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise GitError("主專案有未提交的改動，先處理再合併")
    if _ok(wt.repo, "merge-base", "--is-ancestor", wt.branch, "HEAD"):
        raise GitError("沒有新的改動可以合併")
    try:
        git(wt.repo, *IDENTITY, "merge", "--no-ff", wt.branch, "-m", "ai-team: " + wt.branch)
    except GitError as e:
        _ok(wt.repo, "merge", "--abort")
        raise GitError("合併失敗，已還原：%s" % e)
    return git(wt.repo, "rev-parse", "HEAD").strip()


def remove_worktree(wt: Worktree) -> None:
    git(wt.repo, "worktree", "remove", "--force", wt.path)
