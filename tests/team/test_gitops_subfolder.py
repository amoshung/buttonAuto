import os
import subprocess
import pytest

from team import checks, gitops, notes, policy, scan
from team.events import PermissionRequest
from team.fakes.fake_models import ScriptedRouter

OPTS = [
    {"optionId": "a", "name": "always", "kind": "allow_always"},
    {"optionId": "o", "name": "once", "kind": "allow_once"},
    {"optionId": "r", "name": "no", "kind": "reject_once"},
]


def test_scan_parent_folder_lists_subdirs(tmp_path):
    parent_a = tmp_path / "workspace_A"
    parent_a.mkdir()
    (parent_a / "proj_B").mkdir()
    (parent_a / "ref_C").mkdir()
    (parent_a / "ref_D").mkdir()
    (parent_a / ".hidden").mkdir()
    (parent_a / "node_modules").mkdir()

    res = scan.scan_project(str(parent_a))
    assert res["subdirs"] == ["proj_B", "ref_C", "ref_D"]


def test_subfolder_b_without_git_initializes_in_place_worktree_without_touching_a_or_c(tmp_path):
    parent_a = tmp_path / "workspace_A"
    parent_a.mkdir()
    target_b = parent_a / "proj_B"
    target_b.mkdir()
    (target_b / "main.py").write_text("print('hello')\n", encoding="utf-8")
    ref_c = parent_a / "ref_C"
    ref_c.mkdir()
    (ref_c / "spec.md").write_text("# Read-only reference\n", encoding="utf-8")

    # Neither A nor B has .git initially
    created = gitops.ensure_local_git_repo(str(target_b))
    assert created is True
    assert gitops.has_own_git_repo(str(target_b)) is True
    assert not (parent_a / ".git").exists()

    # Worktree and notes_dir are created in-place inside B/.ai-team-worktrees
    wt = gitops.create_worktree(str(target_b), "feat-sub", in_place=True)
    nd = notes.notes_dir(str(target_b), "feat-sub", in_place=True)

    assert wt.path.startswith(os.path.abspath(str(target_b)) + os.sep)
    assert nd.startswith(os.path.abspath(str(target_b)) + os.sep)
    assert not (parent_a / ".ai-team-worktrees").exists()
    assert sorted(os.listdir(parent_a)) == ["proj_B", "ref_C"]

    # Inside B and wt, uncommitted and changed_files ignore .ai-team-worktrees
    assert gitops.uncommitted(wt) == []
    assert gitops.changed_files(wt) == []


def test_subfolder_b_under_git_parent_a_creates_isolated_git_in_b(tmp_path):
    parent_a = tmp_path / "monorepo_A"
    parent_a.mkdir()
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", "init", "-q"], cwd=str(parent_a), check=True)
    (parent_a / "root.txt").write_text("root", encoding="utf-8")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", "add", "-A"], cwd=str(parent_a), check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "-m", "root"], cwd=str(parent_a), check=True)

    target_b = parent_a / "sub_B"
    target_b.mkdir()
    (target_b / "app.py").write_text("x = 1\n", encoding="utf-8")

    # Before ensure_local_git_repo, B does not have its own top-level git repo
    assert gitops.has_own_git_repo(str(target_b)) is False
    created = gitops.ensure_local_git_repo(str(target_b))
    assert created is True
    assert gitops.has_own_git_repo(str(target_b)) is True

    wt = gitops.create_worktree(str(target_b), "isolated", in_place=True)
    assert os.path.isdir(wt.path)
    assert not (parent_a / ".ai-team-worktrees").exists()


def test_policy_allows_read_in_reference_root_and_rejects_writes_outside_b(tmp_path):
    parent_a = tmp_path / "A"
    target_b = parent_a / "B"
    ref_c = parent_a / "C"
    target_b.mkdir(parents=True)
    ref_c.mkdir(parents=True)
    c_file = ref_c / "ref.py"
    c_file.write_text("CONST = 42\n", encoding="utf-8")

    wt = gitops.create_worktree(str(target_b), "scope-test", in_place=True)
    router = ScriptedRouter()

    # 1. Reading C/ref.py inside A is allowed
    read_req = PermissionRequest("t1", "read ref", "read", {"path": str(c_file)}, OPTS)
    d_read = policy.decide(
        read_req,
        goal="build B",
        scope=wt.path,
        deny_patterns=[],
        router=router,
        read_only=False,
        reference_root=str(parent_a),
    )
    assert d_read.outcome == "allow"

    # 2. Editing C/ref.py is rejected immediately
    edit_req = PermissionRequest("t2", "edit ref", "edit", {"path": str(c_file)}, OPTS)
    d_edit = policy.decide(
        edit_req,
        goal="build B",
        scope=wt.path,
        deny_patterns=[],
        router=router,
        read_only=False,
        reference_root=str(parent_a),
    )
    assert d_edit.outcome == "reject"
    assert "唯讀參考區" in d_edit.reason

    # 3. Shell output redirection into C/ref.py is rejected even if kind="read" or "execute"
    redir_req = PermissionRequest("t3", "bash", "execute", {"command": f"echo hacked > {c_file}"}, OPTS)
    d_redir = policy.decide(
        redir_req,
        goal="build B",
        scope=wt.path,
        deny_patterns=[],
        router=router,
        read_only=False,
        reference_root=str(parent_a),
    )
    assert d_redir.outcome == "reject"

    # 4. Reference snapshot detects any out-of-band modification to C/ref.py
    snap = checks.snapshot_reference_root(str(parent_a), str(target_b))
    assert checks.verify_reference_untouched(snap, str(parent_a), str(target_b)) == []
    c_file.write_text("MODIFIED\n", encoding="utf-8")
    violated = checks.verify_reference_untouched(snap, str(parent_a), str(target_b))
    assert violated == ["C/ref.py"]
