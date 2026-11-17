"""房間工作文件（spec.md、plan.md、evidence.md、progress.md）存放在 repo 外，發現 repo 內有舊的 docs/ai-team 就移除。"""
import os
import shutil
from datetime import datetime
from typing import List, Sequence

from team import gitops
from team.gitops import GitError, Worktree

LEGACY_DOC_DIRS: Sequence[str] = ("docs/ai-team",)
PURGE_COMMIT_MSG = "ai-team: 移除助理工作文件"
_NOTE_NAMES = {"spec.md", "plan.md", "evidence.md", "progress.md"}


def _norm_dirs(dirs: Sequence[str]) -> List[str]:
    out: List[str] = []
    for d in dirs or ():
        rel = (d or "").strip().replace("\\", "/").strip("/")
        if rel and rel not in out:
            out.append(rel)
    return out


def notes_dir(repo: str, slug: str) -> str:
    """回傳並建立 repo 外的房間工作文件目錄：<repo 上一層>/.ai-team-worktrees/<repo 名>-<slug>.notes。"""
    abs_repo = os.path.abspath(repo)
    root = os.path.join(os.path.dirname(abs_repo), gitops.WT_DIR)
    name = os.path.basename(abs_repo)
    path = os.path.join(root, "%s-%s.notes" % (name, slug))
    os.makedirs(path, exist_ok=True)
    return path


def mirror(notes: str, room_dir: str) -> None:
    """把 notes 內所有檔案複製到 sessions/team/<room>/notes/。"""
    if not notes or not os.path.isdir(notes) or not room_dir:
        return
    dst_root = (
        room_dir
        if os.path.basename(os.path.normpath(room_dir)) == "notes"
        else os.path.join(room_dir, "notes")
    )
    os.makedirs(dst_root, exist_ok=True)
    for dirpath, _dirnames, filenames in os.walk(notes):
        rel_dir = os.path.relpath(dirpath, notes)
        target_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            dst = os.path.join(target_dir, fn)
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def _copy_tree_into(src_root: str, dst_root: str, notes_root: str = "") -> None:
    if not os.path.exists(src_root):
        return
    if os.path.isfile(src_root):
        os.makedirs(os.path.dirname(dst_root), exist_ok=True)
        shutil.copy2(src_root, dst_root)
        return
    for dirpath, _dirnames, filenames in os.walk(src_root):
        rel = os.path.relpath(dirpath, src_root)
        target_dir = dst_root if rel == "." else os.path.join(dst_root, rel)
        os.makedirs(target_dir, exist_ok=True)
        for fn in filenames:
            s = os.path.join(dirpath, fn)
            d = os.path.join(target_dir, fn)
            shutil.copy2(s, d)
            if notes_root and fn in _NOTE_NAMES:
                top_note = os.path.join(notes_root, fn)
                if not os.path.exists(top_note):
                    shutil.copy2(s, top_note)


def _prune_empty_parents(root: str, rel: str) -> None:
    parent = os.path.dirname(rel.replace("/", os.sep))
    while parent:
        full = os.path.join(root, parent)
        try:
            os.rmdir(full)
        except OSError:
            break
        parent = os.path.dirname(parent)


def _rmtree_force(path: str) -> None:
    if not os.path.exists(path):
        return
    if os.path.isfile(path) or os.path.islink(path):
        try:
            os.chmod(path, 0o700)
            os.remove(path)
        except OSError:
            pass
        return

    def _onexc(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_onexc)


def purge_worktree(
    wt: Worktree,
    notes: str,
    dirs: Sequence[str] = LEGACY_DOC_DIRS,
) -> List[str]:
    """檢查 worktree 是否有舊的助理工作文件目錄（如 docs/ai-team）：
    - 檔案搬到 notes/legacy/
    - 已追蹤 → git rm -r --cached -- <dir> 並只針對該路徑 commit「ai-team: 移除助理工作文件」
    - 未追蹤 → 直接移除
    回傳移除的相對路徑清單。
    """
    if not wt or not wt.path or not os.path.isdir(wt.path):
        return []
    os.makedirs(notes, exist_ok=True)
    legacy_root = os.path.join(notes, "legacy")
    removed: List[str] = []
    for rel in _norm_dirs(dirs):
        full = os.path.join(wt.path, rel.replace("/", os.sep))
        try:
            tracked = gitops.git(wt.path, "ls-files", "--", rel).strip()
        except GitError:
            tracked = ""
        exists = os.path.exists(full)
        if not tracked and not exists:
            continue
        if exists:
            _copy_tree_into(full, legacy_root, notes_root=notes)
        if tracked:
            try:
                gitops.git(wt.path, "rm", "-r", "-f", "--cached", "--", rel)
            except GitError:
                pass
        if os.path.exists(full):
            _rmtree_force(full)
            _prune_empty_parents(wt.path, rel)
        if tracked:
            try:
                staged = gitops.git(wt.path, "diff", "--cached", "--name-only", "--", rel).strip()
                if staged:
                    gitops.git(
                        wt.path,
                        *gitops.IDENTITY,
                        "commit",
                        "-q",
                        "--no-verify",
                        "-m",
                        PURGE_COMMIT_MSG,
                        "--",
                        rel,
                    )
            except GitError:
                pass
        removed.append(rel)
    return removed


def legacy_in_repo(repo: str, dirs: Sequence[str] = LEGACY_DOC_DIRS) -> List[str]:
    """回傳主 repo 工作目錄中存在的舊助理文件目錄清單。"""
    if not repo or not os.path.isdir(repo):
        return []
    out: List[str] = []
    for rel in _norm_dirs(dirs):
        full = os.path.join(repo, rel.replace("/", os.sep))
        if os.path.exists(full):
            out.append(rel)
    return out


def remove_legacy_in_repo(repo: str, dirs: Sequence[str] = LEGACY_DOC_DIRS) -> List[str]:
    """使用者按按鈕時呼叫：把主 repo 的舊助理文件搬到 <repo 上一層>/.ai-team-worktrees/_legacy-<時間>/，
    若已追蹤則 git rm -r --cached，但不自動 commit。"""
    if not repo or not os.path.isdir(repo):
        return []
    found = legacy_in_repo(repo, dirs)
    if not found:
        return []
    abs_repo = os.path.abspath(repo)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(
        os.path.dirname(abs_repo),
        gitops.WT_DIR,
        "_legacy-%s" % stamp,
        os.path.basename(abs_repo),
    )
    moved: List[str] = []
    for rel in found:
        full = os.path.join(abs_repo, rel.replace("/", os.sep))
        dst = os.path.join(backup_root, rel.replace("/", os.sep))
        _copy_tree_into(full, dst)
        try:
            tracked = gitops.git(abs_repo, "ls-files", "--", rel).strip()
        except GitError:
            tracked = ""
        if tracked:
            try:
                gitops.git(abs_repo, "rm", "-r", "-f", "--cached", "--", rel)
            except GitError:
                pass
        _rmtree_force(full)
        _prune_empty_parents(abs_repo, rel)
        moved.append(rel)
    return moved
