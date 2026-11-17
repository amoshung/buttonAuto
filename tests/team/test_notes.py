"""Task 3：房間工作文件移出 repo（.notes 目錄），發現 docs/ai-team 就移除，主 repo 由按鈕移除。"""
import os
import subprocess

from team import gitops
from team.notes import (
    legacy_in_repo,
    mirror,
    notes_dir,
    purge_worktree,
    remove_legacy_in_repo,
)
from tests.team.test_runner import BUILD_TURN, PLAN_TURN, SPEC_TURN, Env


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@l", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    return path


def test_notes_dir_outside_repo(tmp_path):
    repo = make_repo(tmp_path / "proj")
    d = notes_dir(str(repo), "add-mul")
    assert os.path.isdir(d)
    assert not os.path.abspath(d).startswith(os.path.abspath(str(repo)) + os.sep)
    assert os.path.basename(d) == "proj-add-mul.notes"


def test_mirror_copies_to_room_notes(tmp_path):
    nd = tmp_path / "proj-s.notes"
    nd.mkdir()
    (nd / "spec.md").write_text("# spec", encoding="utf-8")
    room_dir = tmp_path / "r00000001"
    room_dir.mkdir()
    mirror(str(nd), str(room_dir))
    assert (room_dir / "notes" / "spec.md").read_text(encoding="utf-8") == "# spec"


def test_purge_tracked_moves_and_commits_only_that_path(tmp_path):
    repo = make_repo(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "add-mul")
    nd = notes_dir(str(repo), "add-mul")
    plan_file = os.path.join(wt.path, "docs", "ai-team", "x", "plan.md")
    os.makedirs(os.path.dirname(plan_file), exist_ok=True)
    with open(plan_file, "w", encoding="utf-8") as f:
        f.write("# 計畫\n1. C1\n")
    _git(wt.path, "add", "docs/ai-team")
    _git(wt.path, "commit", "-q", "-m", "add ai-team doc")
    # 另有一個未提交的 calc.py 改動
    with open(os.path.join(wt.path, "calc.py"), "a", encoding="utf-8") as f:
        f.write("# dirty\n")

    removed = purge_worktree(wt, nd)
    assert removed == ["docs/ai-team"]
    assert "docs/ai-team" not in _git(wt.path, "ls-files")
    assert not os.path.exists(os.path.join(wt.path, "docs", "ai-team"))
    assert os.path.isfile(os.path.join(nd, "legacy", "x", "plan.md"))
    assert _git(wt.path, "log", "-1", "--format=%s").strip() == "ai-team: 移除助理工作文件"
    changed_in_commit = _git(wt.path, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert all(p.startswith("docs/ai-team/") for p in changed_in_commit if p.strip())
    assert "calc.py" in _git(wt.path, "diff", "--name-only")


def test_purge_untracked_just_moves(tmp_path):
    repo = make_repo(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "add-mul")
    nd = notes_dir(str(repo), "add-mul")
    head_before = _git(wt.path, "rev-parse", "HEAD").strip()
    spec_file = os.path.join(wt.path, "docs", "ai-team", "x", "spec.md")
    os.makedirs(os.path.dirname(spec_file), exist_ok=True)
    with open(spec_file, "w", encoding="utf-8") as f:
        f.write("# spec\n")

    removed = purge_worktree(wt, nd)
    assert removed == ["docs/ai-team"]
    assert not os.path.exists(os.path.join(wt.path, "docs", "ai-team"))
    assert os.path.isfile(os.path.join(nd, "legacy", "x", "spec.md"))
    assert _git(wt.path, "rev-parse", "HEAD").strip() == head_before


def test_merge_tree_excludes_ai_team(tmp_path):
    repo = make_repo(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "add-mul")
    nd = notes_dir(str(repo), "add-mul")
    # 分支歷史先新增 docs/ai-team 與功能改動
    p = os.path.join(wt.path, "docs", "ai-team", "add-mul", "spec.md")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("# spec")
    with open(os.path.join(wt.path, "calc.py"), "a", encoding="utf-8") as f:
        f.write("\ndef mul(a, b):\n    return a * b\n")
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-q", "-m", "feat + legacy doc")

    # 合併前 purge
    purge_worktree(wt, nd)
    gitops.merge_into_repo(wt)
    tree = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
    assert "docs/ai-team" not in tree
    assert "calc.py" in tree


def test_main_repo_untouched_without_button(tmp_path):
    repo = make_repo(tmp_path / "proj")
    old_doc = repo / "docs" / "ai-team" / "old" / "spec.md"
    old_doc.parent.mkdir(parents=True, exist_ok=True)
    old_doc.write_text("# old spec", encoding="utf-8")
    _git(repo, "add", "docs/ai-team")
    _git(repo, "commit", "-q", "-m", "old ai-team in main")

    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    room = env.run()
    assert room.status == "done", room.error
    # worktree 內已清除，但主 repo 工作目錄仍保留直到使用者按鈕
    assert not os.path.exists(os.path.join(room.worktree, "docs", "ai-team"))
    assert old_doc.is_file()
    assert legacy_in_repo(str(repo)) == ["docs/ai-team"]

    head_before = _git(repo, "rev-parse", "HEAD").strip()
    moved = remove_legacy_in_repo(str(repo))
    assert moved == ["docs/ai-team"]
    assert not (repo / "docs" / "ai-team").exists()
    assert legacy_in_repo(str(repo)) == []
    # 主 repo 不自動 commit（留給使用者自己決定）
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
