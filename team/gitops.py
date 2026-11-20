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
    ssh_host: str = ""


def _is_dir(path: str, ssh_host: str = "") -> bool:
    if not path:
        return False
    if not ssh_host:
        return os.path.isdir(path)
    from team.ssh import SshError, remote_fs
    try:
        res = remote_fs(ssh_host, "stat", path)
    except SshError as e:
        raise GitError(
            "SSH 遠端連線失敗（%s），無法存取目標資料夾（若剛重啟服務或需密碼驗證，請於右側重新輸入 SSH 密碼）：%s"
            % (ssh_host, e)
        )
    except Exception:
        return False
    if not res.get("ok", True):
        raise GitError("SSH 遠端檢查資料夾失敗（%s）：%s" % (ssh_host, res.get("error") or "未知錯誤"))
    return bool(res.get("isdir"))


def _exists(path: str, ssh_host: str = "") -> bool:
    if not path:
        return False
    if not ssh_host:
        return os.path.exists(path)
    from team.ssh import SshError, remote_fs
    try:
        res = remote_fs(ssh_host, "stat", path)
    except SshError as e:
        raise GitError(
            "SSH 遠端連線失敗（%s），無法檢查路徑（若剛重啟服務或需密碼驗證，請於右側重新輸入 SSH 密碼）：%s"
            % (ssh_host, e)
        )
    except Exception:
        return False
    if not res.get("ok", True):
        raise GitError("SSH 遠端檢查路徑失敗（%s）：%s" % (ssh_host, res.get("error") or "未知錯誤"))
    return bool(res.get("exists"))


def git(cwd: str, *args: str, timeout: float = 60, ssh_host: str = "") -> str:
    # core.longpaths：Windows 上路徑超過 260 字元的檔案（常見於 reference/ 這類資料夾）才建得出來。
    cmd = ["git", "-c", "core.quotepath=false", "-c", "core.longpaths=true", *args]
    if ssh_host:
        from team.ssh import remote_exec
        res = remote_exec(ssh_host, cwd, cmd, shell=False, timeout=timeout)
        if not res.get("ok"):
            if res.get("error_kind") == "FileNotFoundError":
                raise GitError("遠端找不到 git，請先在遠端安裝 git 並加入 PATH")
            raise GitError("遠端無法執行 git：%s" % res.get("error", ""))
        if res.get("timed_out"):
            raise GitError("git %s 逾時（%s 秒）" % (" ".join(args[:2]), timeout))
        out = str(res.get("stdout") or "")
        if res.get("returncode", 0) != 0:
            err = _clean_err(str(res.get("stderr") or "")) or out.strip()[-1500:]
            raise GitError("git %s 失敗：%s%s" % (" ".join(args[:2]), err, _hint(err)))
        return out
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


def _ok(cwd: str, *args: str, ssh_host: str = "") -> bool:
    try:
        git(cwd, *args, ssh_host=ssh_host)
        return True
    except GitError:
        return False


def has_own_git_repo(path: str, ssh_host: str = "") -> bool:
    """檢查 path 本身是否就是獨立的 Git 儲存庫根目錄（避免誤用上層母資料夾 A 的 .git）。"""
    if not path or not _is_dir(path, ssh_host):
        return False
    try:
        top = git(path, "rev-parse", "--show-toplevel", timeout=10, ssh_host=ssh_host).strip()
    except GitError:
        return False
    if not top:
        return False
    if ssh_host:
        from team.ssh import remote_fs
        r_top = remote_fs(ssh_host, "stat", top).get("realpath") or top
        r_path = remote_fs(ssh_host, "stat", path).get("realpath") or path
        return r_top.rstrip("/\\") == r_path.rstrip("/\\")
    return os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(path))


def _ensure_git_exclude(repo: str, pattern: str = WT_DIR + "/", ssh_host: str = "") -> None:
    """將 .ai-team-worktrees/ 寫入 repo 的 .git/info/exclude，確保建在 B 內部的 worktree 不污染 git status。"""
    try:
        git_dir = git(repo, "rev-parse", "--git-dir", timeout=10, ssh_host=ssh_host).strip()
        if not os.path.isabs(git_dir) and not git_dir.startswith("/"):
            git_dir = (repo.rstrip("/\\") + "/" + git_dir) if ssh_host else os.path.join(repo, git_dir)
        if ssh_host:
            from team.ssh import remote_fs
            remote_fs(ssh_host, "ensure_exclude", git_dir, pattern=pattern)
            return
        info_dir = os.path.join(git_dir, "info")
        os.makedirs(info_dir, exist_ok=True)
        excl = os.path.join(info_dir, "exclude")
        existing = ""
        if os.path.isfile(excl):
            with open(excl, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        lines = [ln.strip() for ln in existing.splitlines()]
        if pattern not in lines and pattern.rstrip("/") not in lines:
            with open(excl, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(pattern + "\n")
    except (GitError, OSError):
        pass


def ensure_local_git_repo(path: str, ssh_host: str = "") -> bool:
    """確保目標資料夾 B 本身擁有獨立的本地 Git 儲存庫與初始 commit（不碰母資料夾 A）。回傳是否新建了 repo。"""
    if not path:
        raise GitError("找不到目標資料夾：%s" % path)
    if ssh_host:
        from team.ssh import SshError, remote_fs
        try:
            st = remote_fs(ssh_host, "stat", path)
        except SshError as e:
            raise GitError(
                "SSH 遠端連線失敗（%s），無法存取目標資料夾（若剛重啟服務或需密碼驗證，請於右側重新輸入 SSH 密碼）：%s"
                % (ssh_host, e)
            )
        if not st.get("ok", True):
            raise GitError("SSH 遠端檢查資料夾失敗（%s）：%s" % (ssh_host, st.get("error") or "未知錯誤"))
        if not st.get("isdir"):
            raise GitError("遠端主機 %s 找不到目標資料夾：%s" % (ssh_host, path))
        abs_path = str(st.get("abspath") or path)
    else:
        if not os.path.isdir(path):
            raise GitError("找不到目標資料夾：%s" % path)
        abs_path = os.path.abspath(path)
    created = False
    if not has_own_git_repo(abs_path, ssh_host=ssh_host):
        if not _ok(abs_path, "init", "-q", "-b", "main", ssh_host=ssh_host):
            git(abs_path, "init", "-q", ssh_host=ssh_host)
            _ok(abs_path, "checkout", "-q", "-b", "main", ssh_host=ssh_host)
        created = True
    if os.name == "nt" and not ssh_host:
        _ok(abs_path, "config", "--local", "core.longpaths", "true", ssh_host=ssh_host)
    _ensure_git_exclude(abs_path, WT_DIR + "/", ssh_host=ssh_host)
    if not _ok(abs_path, "rev-parse", "--verify", "-q", "HEAD", ssh_host=ssh_host):
        git(abs_path, "add", "-A", timeout=120, ssh_host=ssh_host)
        git(abs_path, *IDENTITY, "commit", "-q", "--allow-empty", "-m", "ai-team: initial baseline",
            timeout=120, ssh_host=ssh_host)
    elif not _ok(abs_path, "symbolic-ref", "-q", "HEAD", ssh_host=ssh_host):
        _ok(abs_path, "checkout", "-q", "-B", "main", ssh_host=ssh_host)
    return created


def repo_problem(path: str, ssh_host: str = "") -> str:
    try:
        is_dir = _is_dir(path, ssh_host)
    except GitError as e:
        return str(e)
    if not is_dir or not _ok(path, "rev-parse", "--is-inside-work-tree", ssh_host=ssh_host):
        return "不是 git repo，請先 git init 並提交一次"
    if not _ok(path, "rev-parse", "--verify", "-q", "HEAD", ssh_host=ssh_host):
        return "repo 還沒有任何 commit"
    if not _ok(path, "symbolic-ref", "-q", "HEAD", ssh_host=ssh_host):
        return "目前不在任何分支上（detached HEAD）"
    return ""


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24].strip("-")
    return s or "task-" + uuid.uuid4().hex[:6]


def _branch_exists(repo: str, branch: str, ssh_host: str = "") -> bool:
    return _ok(repo, "show-ref", "--verify", "-q", "refs/heads/" + branch, ssh_host=ssh_host)


def _wt_branch(path: str, ssh_host: str = "") -> str:
    try:
        return git(path, "rev-parse", "--abbrev-ref", "HEAD", ssh_host=ssh_host).strip()
    except GitError:
        return ""


def _cfg_get(repo: str, branch: str, key: str, ssh_host: str = "") -> str:
    try:
        return git(repo, "config", "--local", "--get", "branch.%s.%s" % (branch, key), ssh_host=ssh_host).strip()
    except GitError:
        return ""


def _cur_branch(repo: str, ssh_host: str = "") -> str:
    try:
        return git(repo, "symbolic-ref", "--short", "-q", "HEAD", ssh_host=ssh_host).strip()
    except GitError:
        return ""


def worktree_root(repo: str, in_place: bool = False, ssh_host: str = "") -> str:
    if ssh_host:
        clean = repo.rstrip("/\\")
        if in_place:
            return clean + "/" + WT_DIR
        parent = os.path.dirname(clean) or clean
        return parent.rstrip("/\\") + "/" + WT_DIR
    abs_repo = os.path.abspath(repo)
    if in_place:
        return os.path.join(abs_repo, WT_DIR)
    return os.path.join(os.path.dirname(abs_repo), WT_DIR)


def create_worktree(
    repo: str,
    slug: str,
    base: str = "",
    in_place: bool = False,
    ssh_host: str = "",
    skip_repo_check: bool = False,
) -> Worktree:
    if not skip_repo_check:
        if in_place:
            ensure_local_git_repo(repo, ssh_host=ssh_host)
        prob = repo_problem(repo, ssh_host=ssh_host)
        if prob:
            raise GitError(prob)
    if in_place:
        _ensure_git_exclude(repo, WT_DIR + "/", ssh_host=ssh_host)
    _ok(repo, "worktree", "prune", ssh_host=ssh_host)  # 清掉手動刪除目錄留下的登記
    root = worktree_root(repo, in_place=in_place, ssh_host=ssh_host)
    name = os.path.basename(repo.rstrip("/\\"))
    cur = _cur_branch(repo, ssh_host=ssh_host)
    n = 1
    reuse = False
    while True:
        cand = slug if n == 1 else "%s-%d" % (slug, n)
        branch = "ai-team/" + cand
        path = (root.rstrip("/\\") + f"/{name}-{cand}") if ssh_host else os.path.join(root, "%s-%s" % (name, cand))
        have_branch = _branch_exists(repo, branch, ssh_host=ssh_host)
        ours = have_branch and bool(_cfg_get(repo, branch, "aiTeamBase", ssh_host=ssh_host))
        if _is_dir(path, ssh_host) and _wt_branch(path, ssh_host=ssh_host) == branch:
            target = _cfg_get(repo, branch, "aiTeamTarget", ssh_host=ssh_host) or cur
            b0 = base or _cfg_get(repo, branch, "aiTeamBase", ssh_host=ssh_host)
            if not b0:
                try:
                    b0 = git(repo, "merge-base", target or "HEAD", branch, ssh_host=ssh_host).strip()
                except GitError:
                    b0 = git(repo, "rev-parse", "HEAD", ssh_host=ssh_host).strip()
            return Worktree(repo, path, branch, b0, target, ssh_host=ssh_host)
        if not _exists(path, ssh_host):
            if not have_branch:
                break
            if ours:  # 目錄被手動刪除，分支還在：接回
                reuse = True
                break
        n += 1
    if reuse:
        base_sha = base or _cfg_get(repo, branch, "aiTeamBase", ssh_host=ssh_host)
        target = _cfg_get(repo, branch, "aiTeamTarget", ssh_host=ssh_host) or cur
    else:
        base_sha = git(repo, "rev-parse", "HEAD", ssh_host=ssh_host).strip()
        target = cur
    made_root = not _is_dir(root, ssh_host)
    try:
        if ssh_host:
            from team.ssh import remote_fs
            remote_fs(ssh_host, "makedirs", root)
        else:
            os.makedirs(root, exist_ok=True)
            if os.name == "nt":
                # 讓代理自己在 worktree 裡跑的 git 也能處理長路徑（只寫這個 repo 的設定）
                _ok(repo, "config", "--local", "core.longpaths", "true")
        if reuse:
            git(repo, "worktree", "add", path, branch, timeout=WORKTREE_ADD_TIMEOUT, ssh_host=ssh_host)
        else:
            git(repo, "worktree", "add", "-b", branch, path, base_sha, timeout=WORKTREE_ADD_TIMEOUT, ssh_host=ssh_host)
            git(repo, "config", "--local", "branch.%s.aiTeamBase" % branch, base_sha, ssh_host=ssh_host)
            git(repo, "config", "--local", "branch.%s.aiTeamTarget" % branch, target, ssh_host=ssh_host)
    except (GitError, OSError) as e:
        if ssh_host:
            from team.ssh import remote_fs
            remote_fs(ssh_host, "rmtree", path)
        else:
            shutil.rmtree(path, ignore_errors=True)
        _ok(repo, "worktree", "prune", ssh_host=ssh_host)
        if not reuse and _branch_exists(repo, branch, ssh_host=ssh_host):
            _ok(repo, "branch", "-D", branch, ssh_host=ssh_host)
        if made_root:
            if ssh_host:
                from team.ssh import remote_fs
                remote_fs(ssh_host, "rmdir", root)
            else:
                try:
                    os.rmdir(root)
                except OSError:
                    pass
        raise GitError("建立 worktree 失敗：%s" % e)
    return Worktree(repo, path, branch, base_sha, target, ssh_host=ssh_host)


def _lines(out: str, sep: str = "\n") -> List[str]:
    return [x for x in out.split(sep) if x]


def uncommitted(wt: Worktree) -> List[str]:
    tracked = _lines(git(wt.path, "diff", "--no-renames", "--name-only", "-z", "HEAD", ssh_host=wt.ssh_host), "\0")
    untracked = _lines(git(wt.path, "ls-files", "--others", "--exclude-standard", "-z", ssh_host=wt.ssh_host), "\0")
    wt_prefix = WT_DIR.strip("/") + "/"
    return [
        f for f in sorted(set(tracked) | set(untracked))
        if not f.replace("\\", "/").startswith(wt_prefix)
    ]


def changed_files(wt: Worktree) -> List[str]:
    committed = _lines(
        git(wt.path, "diff", "--no-renames", "--name-only", "-z", wt.base + "..HEAD", ssh_host=wt.ssh_host),
        "\0",
    )
    all_files = sorted(set(committed) | set(uncommitted(wt)))
    prefixes = tuple(d.strip("/") + "/" for d in LEGACY_DOC_DIRS)
    wt_prefix = WT_DIR.strip("/") + "/"
    return [
        f for f in all_files
        if not f.replace("\\", "/").startswith(wt_prefix)
        and not (
            f.replace("\\", "/").startswith(prefixes)
            and not _exists(os.path.join(wt.path, f), wt.ssh_host)
        )
    ]


def commits_since(wt: Worktree) -> List[Tuple[str, str]]:
    out = git(wt.path, "log", "--reverse", "--format=%h%x09%s", wt.base + "..HEAD", ssh_host=wt.ssh_host)
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
    return git(wt.path, "diff", "--no-renames", "--stat=200", wt.base + "..HEAD", ssh_host=wt.ssh_host)


def diff_text(wt: Worktree, max_chars: int = 60000) -> str:
    text = git(wt.path, "diff", "--no-renames", wt.base + "..HEAD", ssh_host=wt.ssh_host)
    if len(text) > max_chars:
        return text[:max_chars] + "\n…（差異過長，已截斷）"
    return text


def merge_into_repo(wt: Worktree) -> str:
    prob = repo_problem(wt.repo, ssh_host=wt.ssh_host)
    if prob:
        raise GitError(prob)
    cur = _cur_branch(wt.repo, ssh_host=wt.ssh_host)
    if wt.target_branch and cur != wt.target_branch:
        raise GitError("主專案目前在 %s 分支，派工時是 %s；請切回 %s 再合併"
                       % (cur, wt.target_branch, wt.target_branch))
    if _ok(wt.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", ssh_host=wt.ssh_host):
        raise GitError("主專案有尚未完成的合併")
    if git(wt.repo, "status", "--porcelain", "--untracked-files=no", ssh_host=wt.ssh_host).strip():
        raise GitError("主專案有未提交的改動，先處理再合併")
    if _ok(wt.repo, "merge-base", "--is-ancestor", wt.branch, "HEAD", ssh_host=wt.ssh_host):
        raise GitError("沒有新的改動可以合併")
    try:
        git(wt.repo, *IDENTITY, "merge", "--no-ff", wt.branch, "-m", "ai-team: " + wt.branch, ssh_host=wt.ssh_host)
    except GitError as e:
        _ok(wt.repo, "merge", "--abort", ssh_host=wt.ssh_host)
        raise GitError("合併失敗，已還原：%s" % e)
    return git(wt.repo, "rev-parse", "HEAD", ssh_host=wt.ssh_host).strip()


def remove_worktree(wt: Worktree) -> None:
    git(wt.repo, "worktree", "remove", "--force", wt.path, ssh_host=wt.ssh_host)
