import os
import subprocess

import pytest

from team import gitops
from team.gitops import GitError, Worktree

ID = ["-c", "user.name=t", "-c", "user.email=t@local"]


def run(cwd, *args):
    subprocess.run(["git", *ID, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    p = tmp_path / "@my Proj 專案"
    p.mkdir()
    run(str(p), "init", "-q")
    run(str(p), "checkout", "-q", "-b", "main")
    (p / "a.txt").write_text("a", encoding="utf-8")
    run(str(p), "add", "-A")
    run(str(p), "commit", "-q", "-m", "init")
    return str(p)


def test_repo_problem_cases(tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "不是 git repo" in gitops.repo_problem(str(plain))
    empty = tmp_path / "empty"
    empty.mkdir()
    run(str(empty), "init", "-q")
    assert "沒有任何 commit" in gitops.repo_problem(str(empty))
    assert gitops.repo_problem(repo) == ""
    run(repo, "checkout", "-q", "--detach")
    assert "detached" in gitops.repo_problem(repo)


def test_slugify():
    assert gitops.slugify("加入 CSV 匯出 export") == "csv-export"
    s = gitops.slugify("中文")
    assert s.startswith("task-") and len(s) == 11
    assert len(gitops.slugify("a" * 100)) == 24


def test_create_commit_and_diff(repo):
    wt = gitops.create_worktree(repo, "feat-x")
    assert wt.branch == "ai-team/feat-x"
    assert wt.path == os.path.join(os.path.dirname(repo), ".ai-team-worktrees",
                                   os.path.basename(repo) + "-feat-x")
    assert os.path.isdir(wt.path)
    assert wt.base == gitops.git(repo, "rev-parse", "HEAD").strip()
    os.makedirs(os.path.join(wt.path, "src"))
    open(os.path.join(wt.path, "src", "檔案.py"), "w", encoding="utf-8").write("x")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "加入檔案")
    open(os.path.join(wt.path, "new.md"), "w").write("n")
    open(os.path.join(wt.path, "a.txt"), "w").write("changed")
    assert gitops.changed_files(wt) == ["a.txt", "new.md", "src/檔案.py"]
    assert gitops.uncommitted(wt) == ["a.txt", "new.md"]
    cs = gitops.commits_since(wt)
    assert len(cs) == 1 and cs[0][1] == "加入檔案"
    assert "檔案.py" in gitops.diff_stat(wt)
    assert "diff --git" in gitops.diff_text(wt)
    assert len(gitops.diff_text(wt, max_chars=10)) <= 10 + 50


def test_create_same_slug_resumes_and_branch_conflict(repo):
    wt = gitops.create_worktree(repo, "s")
    assert gitops.create_worktree(repo, "s") == wt
    run(repo, "branch", "ai-team/t")
    wt2 = gitops.create_worktree(repo, "t")
    assert wt2.branch == "ai-team/t-2"


def test_create_failure_leaves_nothing(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError, match="不是 git repo"):
        gitops.create_worktree(str(plain), "x")
    assert not (tmp_path / ".ai-team-worktrees").exists()


def test_outside_allowed():
    assert gitops.outside_allowed(["src/a.py", "tests/t.py", "x.md"], ["src/", "tests/*.py"]) == ["x.md"]
    assert gitops.outside_allowed(["x.md"], []) == []
    assert gitops.outside_allowed(["docs/ai-team/s/spec.md", "y"], ["src/"]) == ["docs/ai-team/s/spec.md", "y"]


def test_merge(repo):
    wt = gitops.create_worktree(repo, "m")
    open(os.path.join(wt.path, "f.txt"), "w").write("f")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "add f")
    open(os.path.join(repo, "a.txt"), "w").write("dirty")
    with pytest.raises(GitError, match="未提交"):
        gitops.merge_into_repo(wt)
    run(repo, "checkout", "--", "a.txt")
    sha = gitops.merge_into_repo(wt)
    assert sha == gitops.git(repo, "rev-parse", "HEAD").strip()
    assert os.path.exists(os.path.join(repo, "f.txt"))
    gitops.remove_worktree(wt)
    assert not os.path.exists(wt.path)
    assert "ai-team/m" in gitops.git(repo, "branch", "--list", "ai-team/m")


def _commit_file(wt, name, text, msg="c"):
    open(os.path.join(wt.path, name), "w").write(text)
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", msg)


def test_merge_untracked_does_not_block(repo):
    wt = gitops.create_worktree(repo, "u")
    _commit_file(wt, "f.txt", "f")
    open(os.path.join(repo, "untracked.txt"), "w").write("u")
    assert gitops.merge_into_repo(wt)


def test_rename_visible(repo):
    wt = gitops.create_worktree(repo, "r")
    os.makedirs(os.path.join(wt.path, "secret"))
    _commit_file(wt, "secret/k.txt", "k")
    wt.base = gitops.git(wt.path, "rev-parse", "HEAD").strip()
    run(wt.path, "mv", "secret/k.txt", "ok.txt")
    files = gitops.changed_files(wt)
    assert "ok.txt" in files and "secret/k.txt" in files
    assert gitops.outside_allowed(files, ["ok.txt"]) == ["secret/k.txt"]
    assert gitops.outside_allowed(["src\\a.py"], ["src\\"]) == []


def test_resume_base_independent_of_main_head(repo):
    wt = gitops.create_worktree(repo, "rs")
    _commit_file(wt, "mine.txt", "m")
    run(repo, "checkout", "-q", "-b", "other")
    open(os.path.join(repo, "o.txt"), "w").write("o")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "other")
    wt2 = gitops.create_worktree(repo, "rs")
    assert wt2.base == wt.base and wt2.target_branch == "main"
    assert gitops.changed_files(wt2) == ["mine.txt"]
    assert [c[1] for c in gitops.commits_since(wt2)] == ["c"]
    assert gitops.create_worktree(repo, "rs", base=wt.base).base == wt.base
    with pytest.raises(GitError, match="切回 main"):
        gitops.merge_into_repo(wt2)


def test_merge_conflict_leaves_clean(repo):
    wt = gitops.create_worktree(repo, "cf")
    _commit_file(wt, "a.txt", "branch")
    open(os.path.join(repo, "a.txt"), "w").write("main")
    run(repo, "commit", "-q", "-am", "main change")
    with pytest.raises(GitError):
        gitops.merge_into_repo(wt)
    assert gitops.git(repo, "status", "--porcelain").strip() == ""
    assert not os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))


def test_merge_nothing_new(repo):
    wt = gitops.create_worktree(repo, "nn")
    with pytest.raises(GitError, match="沒有新的改動"):
        gitops.merge_into_repo(wt)


def test_stale_registration_recovers(repo):
    import shutil
    wt = gitops.create_worktree(repo, "st")
    shutil.rmtree(wt.path)
    wt2 = gitops.create_worktree(repo, "st")
    assert wt2.branch == "ai-team/st" and os.path.isdir(wt2.path)


def test_git_error_drops_progress_and_hints_long_paths(monkeypatch):
    import subprocess as sp
    from team import gitops

    class P:
        returncode = 128
        stdout = b""
        stderr = ("Preparing worktree (new branch 'x')\nUpdating files:  10% (73/678)\r"
                  "Updating files:  11% (75/678)\nerror: unable to create file a/b.md: Filename too long\n"
                  "Updating files: 100% (678/678), done.\nfatal: Could not reset index file to revision 'HEAD'.\n").encode()

    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(gitops.subprocess, "run", fake_run)
    with pytest.raises(gitops.GitError) as ei:
        gitops.git(".", "worktree", "add", "x")
    msg = str(ei.value)
    assert "Updating files" not in msg and "Preparing worktree" not in msg
    assert "Filename too long" in msg and "fatal: Could not reset" in msg
    assert "core.longpaths" in msg and "重試" in msg
    assert "core.longpaths=true" in seen["cmd"]


def test_slugify_is_short():
    from team.gitops import slugify
    assert len(slugify("2026-11-11-1755-feat-bitemporal knowledge base upgrade")) <= 24


# ---- 最終審查 minor：worktree add 逾時、助理 commit 不簽章 ----
def test_worktree_add_uses_long_timeout(repo, monkeypatch):
    seen = []
    real = gitops.git

    def spy(cwd, *args, timeout=60):
        if args[:2] == ("worktree", "add"):
            seen.append(timeout)
        return real(cwd, *args, timeout=timeout)
    monkeypatch.setattr(gitops, "git", spy)
    gitops.create_worktree(repo, "slow")
    assert seen == [900]


def test_identity_disables_gpgsign():
    assert gitops.IDENTITY[-2:] == ["-c", "commit.gpgsign=false"]


def test_merge_ignores_user_gpgsign(repo):
    wt = gitops.create_worktree(repo, "sig")
    _commit_file(wt, "s.txt", "s")
    run(repo, "config", "commit.gpgsign", "true")
    run(repo, "config", "gpg.program", "false")  # 真的去簽章就會失敗
    sha = gitops.merge_into_repo(wt)
    assert sha == gitops.git(repo, "rev-parse", "HEAD").strip()
