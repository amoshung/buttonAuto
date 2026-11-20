import os
import subprocess
import sys

import pytest

from team import checks, gitops

ID = ["-c", "user.name=t", "-c", "user.email=t@local"]
PY = '"%s"' % sys.executable
TEST_CMD = PY + " -m pytest -q -p no:cacheprovider"
MUL_CHECK = PY + ' -c "import calc; assert calc.mul(2,3)==6"'
CRITERIA = [{"id": "C1", "text": "mul", "check": MUL_CHECK}]


def run(cwd, *args):
    subprocess.run(["git", *ID, *args], cwd=cwd, check=True, capture_output=True)


def write(root, rel, text):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_bytes(root, rel):
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


@pytest.fixture
def wt(tmp_path):
    p = tmp_path / "@my Proj 專案"
    p.mkdir()
    run(str(p), "init", "-q")
    run(str(p), "checkout", "-q", "-b", "main")
    write(str(p), ".gitignore", "__pycache__/\n.pytest_cache/\nnode_modules/\ndata.local\n")
    write(str(p), "calc.py", "def add(a, b):\n    return a + b\n")
    write(str(p), "test_calc.py", "import calc\n\n\ndef test_add():\n    assert calc.add(1, 2) == 3\n")
    run(str(p), "add", "-A")
    run(str(p), "commit", "-q", "-m", "init")
    return gitops.create_worktree(str(p), "mul")


def add_mul(wt, real_test=True):
    write(wt.path, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    body = "    assert calc.mul(2, 3) == 6\n" if real_test else "    assert 1 == 1\n"
    write(wt.path, "test_calc.py", "import calc\n\n\ndef test_add():\n    assert calc.add(1, 2) == 3\n\n\n"
          "def test_mul():\n" + body)
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "mul")


def assert_restored(wt):
    for rel in ("calc.py", "test_calc.py"):
        head = subprocess.run(["git", "show", "HEAD:" + rel], cwd=wt.path, capture_output=True, check=True).stdout
        assert read_bytes(wt.path, rel) == head
    assert gitops.uncommitted(wt) == []
    assert not os.path.exists(os.path.join(os.path.dirname(wt.path), checks.MUTATION_DIR))
    out = subprocess.run(["git", "worktree", "list"], cwd=wt.repo, capture_output=True, check=True).stdout
    assert len(out.decode("utf-8", "replace").strip().splitlines()) == 2  # 暫存 worktree 已清掉


def test_run_cmd_ok_and_fail(tmp_path):
    r = checks.run_cmd(str(tmp_path), PY + ' -c "print(\'hello 你好\')"')
    assert r.exit_code == 0 and not r.timed_out and "hello 你好" in r.tail and r.seconds >= 0
    r = checks.run_cmd(str(tmp_path), PY + ' -c "import sys; sys.exit(3)"')
    assert r.exit_code == 3


def test_run_cmd_tail_limited(tmp_path):
    r = checks.run_cmd(str(tmp_path), PY + ' -c "print(\'x\' * 10000 + \'END\')"')
    assert len(r.tail) <= 4000 and "END" in r.tail


def test_run_cmd_timeout(tmp_path):
    r = checks.run_cmd(str(tmp_path), PY + ' -c "import time; time.sleep(3)"', timeout=1)
    assert r.timed_out and r.exit_code == -1 and r.seconds < 3


def test_gate_passes_with_real_test(wt):
    add_mul(wt)
    gate = checks.test_gate(wt, TEST_CMD, CRITERIA + [{"id": "C2", "text": "人工看", "check": "manual"},
                                                      {"id": "C3", "text": "空", "check": ""}])
    assert gate.ok is True, gate.issues
    assert [r.cmd for r in gate.results] == [TEST_CMD, MUL_CHECK]
    ok, msg = checks.mutation_check(wt, TEST_CMD)
    assert ok is True, msg
    assert_restored(wt)


def test_mutation_detects_fake_test(wt):
    add_mul(wt, real_test=False)
    ok, msg = checks.mutation_check(wt, TEST_CMD)
    assert ok is False and "沒有真正檢查" in msg
    gate = checks.test_gate(wt, TEST_CMD, CRITERIA)
    assert gate.ok is False and any("沒有真正檢查" in i for i in gate.issues)
    assert_restored(wt)


def test_mutation_new_file_moved_away(wt):
    write(wt.path, "util.py", "def tri(x):\n    return 3 * x\n")
    write(wt.path, "tests/test_util.py", "import sys, os\nsys.path.insert(0, os.getcwd())\nimport util\n\n\n"
          "def test_tri():\n    assert util.tri(2) == 6\n")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "util")
    ok, msg = checks.mutation_check(wt, TEST_CMD)
    assert ok is True, msg
    assert os.path.exists(os.path.join(wt.path, "util.py"))
    assert gitops.uncommitted(wt) == []


def test_mutation_skips_when_no_source_change(wt):
    write(wt.path, "docs/ai-team/mul/spec.md", "spec")
    write(wt.path, "test_more.py", "def test_x():\n    assert 1\n")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "docs")
    assert checks.mutation_check(wt, TEST_CMD) == (None, "沒有原始碼改動，無法換回原狀檢查")
    gate = checks.test_gate(wt, TEST_CMD, [])
    assert gate.ok is False and gate.issues == [] and "無法換回原狀檢查" in gate.unverified


def test_mutation_keeps_uncommitted_and_untracked(wt):
    add_mul(wt)
    write(wt.path, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b  # 未提交\n")
    write(wt.path, "scratch.txt", "未追蹤")
    before = (read_bytes(wt.path, "calc.py"), read_bytes(wt.path, "scratch.txt"))
    ok, _ = checks.mutation_check(wt, TEST_CMD)
    assert ok is True
    assert (read_bytes(wt.path, "calc.py"), read_bytes(wt.path, "scratch.txt")) == before


def test_gate_reports_failing_tests_and_criteria(wt):
    write(wt.path, "calc.py", "def add(a, b):\n    return a - b\n")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "break")
    gate = checks.test_gate(wt, TEST_CMD, CRITERIA)
    assert gate.ok is False
    assert any("測試" in i and "assert" in i for i in gate.issues)
    assert any("C1" in i for i in gate.issues)
    assert_restored(wt)


def test_build_issues(wt):
    assert checks.build_issues(wt, []) == ["還沒有 commit"]
    write(wt.path, "calc.py", "def add(a, b):\n    return a + b  # TODO 之後再說\n")
    write(wt.path, "other/x.py", "x = 1\n")
    run(wt.path, "add", "calc.py")
    run(wt.path, "commit", "-q", "-m", "c")
    issues = checks.build_issues(wt, ["calc.py"])
    assert len([i for i in issues if i.startswith("有未提交的改動：")]) == 1
    assert any("other/x.py" in i for i in issues if i.startswith("有未提交的改動："))
    assert len([i for i in issues if i.startswith("改到範圍外的檔案：")]) == 1
    assert any(i.startswith("疑似空殼：calc.py:2") for i in issues)
    assert "還沒有 commit" not in issues


def test_is_test_path():
    for p in ["latest.py", "contest.py", "inspector.py", "src/testing/x.py", "respect.py"]:
        assert not checks.is_test_path(p), p
    for p in ["test_a.py", "a_test.go", "x.test.ts", "y.spec.js", "conftest.py", "tests/u.py", "a/test/b.py",
              "web/__tests__/c.js", "spec/d.rb", "specs/e.rb", "docs/f.md"]:
        assert checks.is_test_path(p), p


def test_gate_rejects_git_in_test_cmd(wt):
    add_mul(wt)
    gate = checks.test_gate(wt, "git stash && " + TEST_CMD, [])
    assert gate.ok is False and any("測試指令不可以用 git" in i for i in gate.issues)
    assert gate.results == []


def test_mutation_unverified_when_test_needs_ignored_file(wt):
    add_mul(wt)
    write(wt.path, "data.local", "x")
    write(wt.path, "test_data.py", "import os\n\n\ndef test_data():\n    assert os.path.exists('data.local')\n")
    run(wt.path, "add", "test_data.py")
    run(wt.path, "commit", "-q", "-m", "data test")
    ok, msg = checks.mutation_check(wt, TEST_CMD)
    assert ok is None and "無法" in msg and "沒有真正檢查" not in msg
    gate = checks.test_gate(wt, TEST_CMD, CRITERIA)
    assert gate.ok is False and gate.issues == [] and gate.unverified
    assert os.path.exists(os.path.join(wt.path, "data.local"))
    assert_restored(wt)


def test_mutation_links_ignored_dependency_dir(wt):
    write(wt.path, "node_modules/dep.py", "VALUE = 6\n")
    write(wt.path, "calc.py", "import dep\n\n\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    write(wt.path, "conftest.py", "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), "
          "'node_modules'))\n")
    write(wt.path, "test_calc.py", "import calc, dep\n\n\ndef test_mul():\n    assert calc.mul(2, 3) == dep.VALUE\n")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "dep")
    ok, msg = checks.mutation_check(wt, TEST_CMD)
    assert ok is True, msg
    assert open(os.path.join(wt.path, "node_modules", "dep.py")).read() == "VALUE = 6\n"
    assert_restored(wt)


def test_build_issues_ignores_preexisting_todo(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    run(str(p), "init", "-q")
    run(str(p), "checkout", "-q", "-b", "main")
    write(str(p), "calc.py", "# TODO 舊的待辦\ndef add(a, b):\n    return a + b\n")
    run(str(p), "add", "-A")
    run(str(p), "commit", "-q", "-m", "init")
    w = gitops.create_worktree(str(p), "x")
    write(w.path, "calc.py", "# TODO 舊的待辦\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    run(w.path, "add", "-A")
    run(w.path, "commit", "-q", "-m", "mul")
    assert checks.build_issues(w, ["calc.py"]) == []
    write(w.path, "calc.py", "# TODO 舊的待辦\ndef add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n"
          "    return a * b  # FIXME 新的\n")
    write(w.path, "new 檔.py", "x = 1  # TODO 新檔\n")
    issues = checks.build_issues(w, [])
    stubs = [i for i in issues if i.startswith("疑似空殼：")]
    assert len(stubs) == 2 and any("calc.py:7" in i for i in stubs) and any("new 檔.py:1" in i for i in stubs)


def test_run_cmd_sets_utf8_env(tmp_path):
    r = checks.run_cmd(str(tmp_path), PY + ' -c "import os; print(os.environ[\'PYTHONUTF8\'], '
                       'os.environ[\'PYTHONIOENCODING\'])"')
    assert "1 utf-8" in r.tail


# ---- Fix round 2


GIT_FLAGGED = ["git diff --quiet HEAD", "/usr/bin/git diff", '"git" diff', "g''it diff", "x=git; $x diff",
               "cd a && git status", 'sh -c "git diff"', "cmd /c git diff", "FOO=1 git diff",
               r'"C:\Program Files\Git\bin\git.exe" diff', "g\\it diff", "pytest && git stash",
               "pytest $(git rev-parse HEAD)", "pytest `git ls-files`", "env A=1 sudo git diff",
               "bash -c 'pytest; git stash'", "powershell -Command \"git diff\"", "(git diff)", "ls | xargs git add"]
GIT_OK = [TEST_CMD, 'pytest -k "not git"', "pytest /home/u/git/proj/tests", r"cd C:\git\proj && pytest",
          "pytest tests/git.py", "npm run git", "pytest $PWD/git/t.py", "pytest $GITHUB_WORKSPACE/tests",
          "cd $GITHUB_WORKSPACE && pytest", "pytest tests/git_utils.py", "npm test", "pytest -k digit",
          "PYTHONPATH=src pytest -q"]


@pytest.mark.parametrize("cmd", GIT_FLAGGED)
def test_git_problem_flags_git_in_command_position(cmd):
    assert checks.git_problem(cmd)
    assert checks.uses_git(cmd)


@pytest.mark.parametrize("cmd", GIT_OK)
def test_git_problem_allows_legitimate_commands(cmd):
    assert checks.git_problem(cmd) == ""


@pytest.mark.parametrize("cmd", ["$RUNNER tests", "%PYTEST% -q", "cd a && $X"])
def test_git_problem_flags_variable_as_command(cmd):
    assert "測試指令不可以用變數當指令" in checks.git_problem(cmd)


def test_gate_rejects_quoted_git(wt):
    add_mul(wt)
    gate = checks.test_gate(wt, '"git" stash; ' + TEST_CMD, [])
    assert any("測試指令不可以用 git" in i for i in gate.issues) and gate.results == []


def test_mutation_one_char_change_reliable(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    run(str(p), "init", "-q")
    run(str(p), "checkout", "-q", "-b", "main")
    write(str(p), ".gitignore", "__pycache__/\n.pytest_cache/\n")
    write(str(p), "calc.py", "def mul(a, b):\n    return a + b\n")
    run(str(p), "add", "-A")
    run(str(p), "commit", "-q", "-m", "init")
    w = gitops.create_worktree(str(p), "x")
    write(w.path, "calc.py", "def mul(a, b):\n    return a * b\n")
    write(w.path, "test_calc.py", "import calc\n\n\ndef test_mul():\n    assert calc.mul(2, 3) == 6\n")
    run(w.path, "add", "-A")
    run(w.path, "commit", "-q", "-m", "fix")
    for _ in range(3):
        ok, msg = checks.mutation_check(w, TEST_CMD)
        assert ok is True, msg


def test_added_lines_with_noprefix_config(wt):
    run(wt.repo, "config", "diff.noprefix", "true")
    run(wt.repo, "config", "diff.mnemonicPrefix", "true")
    write(wt.path, "calc.py", "def add(a, b):\n    return a + b  # TODO 之後\n")
    run(wt.path, "add", "-A")
    run(wt.path, "commit", "-q", "-m", "c")
    assert checks.added_lines(wt) == {"calc.py": {2}}
    assert any(i.startswith("疑似空殼：calc.py:2") for i in checks.build_issues(wt, []))


def test_dep_dirs_exclude_build_outputs():
    assert set(checks.DEP_DIRS) == {"node_modules", ".venv", "venv", "env", "vendor", ".tox"}


def test_mutation_never_writes_through_link(tmp_path):
    p = tmp_path / "repo"
    p.mkdir()
    run(str(p), "init", "-q")
    run(str(p), "checkout", "-q", "-b", "main")
    write(str(p), ".gitignore", "__pycache__/\n.pytest_cache/\n")
    write(str(p), "vendor/lib.py", "OLD = 1\n")
    write(str(p), "calc.py", "def add(a, b):\n    return a + b\n")
    run(str(p), "add", "-A")
    run(str(p), "commit", "-q", "-m", "init")
    w = gitops.create_worktree(str(p), "x")
    run(w.path, "rm", "-q", "--cached", "vendor/lib.py")
    write(w.path, ".gitignore", "__pycache__/\n.pytest_cache/\nvendor/\n")
    write(w.path, "vendor/lib.py", "NEW = 2\n")
    write(w.path, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    write(w.path, "test_calc.py", "import calc\n\n\ndef test_mul():\n    assert calc.mul(2, 3) == 6\n")
    run(w.path, "add", "-A")
    run(w.path, "commit", "-q", "-m", "drop vendor")
    ok, msg = checks.mutation_check(w, TEST_CMD)
    assert ok is None and "無法安全地換回 vendor/lib.py" in msg
    assert read_bytes(w.path, "vendor/lib.py") == b"NEW = 2\n"
    assert not os.path.exists(os.path.join(os.path.dirname(w.path), checks.MUTATION_DIR))


def test_mutation_keeps_copy_when_unlink_fails(wt, monkeypatch):
    os.makedirs(os.path.join(wt.path, "node_modules"))
    write(wt.path, ".gitignore", "__pycache__/\n.pytest_cache/\nnode_modules/\n")
    add_mul(wt)
    monkeypatch.setattr(checks, "_unlink_dir", lambda path: False)
    ok, msg = checks.mutation_check(wt, TEST_CMD)
    assert ok is True
    parent = os.path.join(os.path.dirname(wt.path), checks.MUTATION_DIR)
    assert parent in msg and "手動刪除" in msg
    assert os.path.isdir(os.path.join(wt.path, "node_modules"))
    left = os.listdir(parent)
    assert len(left) == 1  # 連結拆不掉：副本留著，不冒險刪除


def test_run_cmd_cancel_kills_tree(tmp_path):
    import threading
    import time
    ev = threading.Event()
    marker = tmp_path / "child.pid"
    cmd = PY + ' -c "import os,time; open(%r,\'w\').write(str(os.getpid())); time.sleep(30)"' % str(marker)
    threading.Timer(0.5, ev.set).start()
    t0 = time.time()
    r = checks.run_cmd(str(tmp_path), cmd, cancel=ev)
    assert time.time() - t0 < 3
    assert r.cancelled and r.exit_code == -1 and not r.timed_out
    pid = int(marker.read_text())
    time.sleep(0.2)
    assert not _alive(pid)


def _alive(pid):
    """程序還在跑（殭屍程序不算）。"""
    stat = "/proc/%d/stat" % pid
    if os.path.exists("/proc"):
        try:
            with open(stat) as f:
                return f.read().rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_gate_and_mutation_cancel_cleanly(wt):
    import threading
    import time
    add_mul(wt)
    slow = PY + ' -c "import time; time.sleep(8)"'
    ev2 = threading.Event()
    threading.Timer(0.5, ev2.set).start()
    t0 = time.time()
    ok, msg = checks.mutation_check(wt, slow, cancel=ev2)
    assert time.time() - t0 < 3 and ok is None and msg == checks.CANCELLED
    assert_restored(wt)
    ev3 = threading.Event()
    threading.Timer(0.5, ev3.set).start()
    t0 = time.time()
    gate = checks.test_gate(wt, slow, CRITERIA, cancel=ev3)
    assert time.time() - t0 < 3 and gate.cancelled and not gate.ok
    assert_restored(wt)


# ---- 最終審查第 3 項：命中危險規則的驗收指令不執行 ----
def test_gate_refuses_denied_commands(wt, tmp_path):
    add_mul(wt)
    marker = tmp_path / "ran.txt"
    bad = PY + ' -c "open(r\'%s\', \'w\').write(\'x\')" # rm -rf /' % marker
    gate = checks.test_gate(wt, TEST_CMD, [{"id": "C9", "text": "x", "check": bad}],
                            deny_patterns=[r"rm\s+-rf"])
    assert gate.ok is False and not marker.exists()
    assert any("條件 C9" in i and "驗收指令命中危險規則" in i for i in gate.issues)
    gate2 = checks.test_gate(wt, TEST_CMD + " # rm -rf x", [], deny_patterns=[r"rm\s+-rf"])
    assert gate2.ok is False and gate2.results == []
    assert any("驗收指令命中危險規則：rm\\s+-rf" in i for i in gate2.issues)


def test_mutation_worktree_add_uses_long_timeout(wt, monkeypatch):
    add_mul(wt)
    seen = []
    real = gitops.git

    def spy(cwd, *args, timeout=60):
        if args[:2] == ("worktree", "add"):
            seen.append(timeout)
        return real(cwd, *args, timeout=timeout)
    monkeypatch.setattr(gitops, "git", spy)
    checks.mutation_check(wt, TEST_CMD)
    assert seen == [900]
