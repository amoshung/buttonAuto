"""Task 4：基準（baseline）、git 外環境資料偵測與連結、test_gate 去重與 gate logs、quick_check。"""
import os
import subprocess
import sys

from team import checks, gitops
from team.baseline import Baseline, detect_env_dirs, link_env_dirs, run_baseline

PY = sys.executable


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


def make_repo_with_old_failure(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "test_x.py").write_text(
        "from calc import add\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_old():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text("__pycache__/\n.pytest_cache/\nreference/\nbuild/\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init with old failure")
    return path


def test_baseline_records_preexisting_failures(tmp_path):
    repo = make_repo_with_old_failure(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "feat")
    cmd = '"%s" -m pytest -q -p no:cacheprovider' % PY
    b = run_baseline(wt, cmd, [])
    assert b.parsed is True
    assert b.failed == ["test_x.py::test_old"]
    assert b.exit_code != 0 and b.commit == wt.base
    assert Baseline.from_dict(b.to_dict()) == b


def test_gate_only_new_failures_are_issues(tmp_path):
    repo = make_repo_with_old_failure(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "feat")
    cmd = '"%s" -m pytest -q -p no:cacheprovider' % PY
    b = run_baseline(wt, cmd, [])
    # 代理加入 mul，但寫了一個失敗的 test_new
    with open(os.path.join(wt.path, "calc.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    with open(os.path.join(wt.path, "test_x.py"), "w", encoding="utf-8") as f:
        f.write(
            "from calc import add, mul\n\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_old():\n    assert 1 == 2\n\n\n"
            "def test_new():\n    assert mul(2, 3) == 999\n"
        )
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-q", "-m", "add mul with broken test_new")
    crit = [{"id": "C1", "text": "mul", "check": '"%s" -c "from calc import mul; assert mul(2,3)==6"' % PY}]
    g1 = checks.test_gate(wt, cmd, crit, baseline=b)
    assert not g1.ok
    assert g1.new_failures == ["test_x.py::test_new"]
    assert g1.env_failures == ["test_x.py::test_old"]
    assert g1.criteria_passed == ["C1"]
    assert any("test_new" in i for i in g1.issues)
    assert not any("test_old" in i for i in g1.issues)

    # 修好 test_new，只剩原本就壞的 test_old → gate.ok 為 True（且通過換回原狀檢查）
    with open(os.path.join(wt.path, "test_x.py"), "w", encoding="utf-8") as f:
        f.write(
            "from calc import add, mul\n\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_old():\n    assert 1 == 2\n\n\n"
            "def test_new():\n    assert mul(2, 3) == 6\n"
        )
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-q", "-m", "fix test_new")
    g2 = checks.test_gate(wt, cmd, crit, baseline=b)
    assert g2.ok is True, (g2.issues, g2.unverified)
    assert g2.new_failures == [] and g2.env_failures == ["test_x.py::test_old"]


def test_env_dirs_detected_and_linked(tmp_path):
    repo = make_repo_with_old_failure(tmp_path / "proj")
    ref = repo / "reference"
    ref.mkdir()
    (ref / "a.pdf").write_bytes(b"%PDF-1.4")
    bld = repo / "build"
    bld.mkdir()
    (bld / "out.pdf").write_bytes(b"%PDF-1.4")

    dirs = detect_env_dirs(str(repo), [])
    assert dirs == ["reference"]
    wt = gitops.create_worktree(str(repo), "feat2")
    res = link_env_dirs(str(repo), wt.path, dirs)
    assert res == [("reference", "")]
    assert os.path.isfile(os.path.join(wt.path, "reference", "a.pdf"))


def test_same_cmd_runs_once(tmp_path):
    repo = make_repo_with_old_failure(tmp_path / "proj")
    (repo / "test_x.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "green")
    wt = gitops.create_worktree(str(repo), "dedup")
    with open(os.path.join(wt.path, "calc.py"), "w", encoding="utf-8") as f:
        f.write("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    with open(os.path.join(wt.path, "test_x.py"), "a", encoding="utf-8") as f:
        f.write("\nfrom calc import mul\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-q", "-m", "mul")

    counter = tmp_path / "count.txt"
    cmd = '"%s" -c "open(r\'%s\', \'a\').write(\'1\\n\')" && "%s" -m pytest -q -p no:cacheprovider' % (
        PY,
        counter,
        PY,
    )
    crit = [{"id": "C1", "text": "pass tests", "check": cmd}]
    gate = checks.test_gate(wt, cmd, crit)
    assert gate.ok is True
    assert len(gate.results) == 1
    assert gate.criteria_passed == ["C1"]


def test_gate_logs_written_and_masked(tmp_path):
    repo = make_repo_with_old_failure(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "logs")
    log_dir = tmp_path / "logs"
    cmd = '"%s" -c "print(\'api_key=sk-zzzzzzzzzzzz\'); import sys; sys.exit(1)"' % PY
    gate = checks.test_gate(wt, cmd, [], log_dir=str(log_dir), gate_no=3, secrets=["sk-zzzzzzzzzzzz"])
    assert gate.logs and os.path.isfile(gate.logs[0])
    content = open(gate.logs[0], encoding="utf-8").read()
    assert "api_key=***" in content and "sk-zzzzzzzzzzzz" not in content


def test_quick_check_skips_manual_env_dependent_and_test_cmd(tmp_path):
    repo = make_repo_with_old_failure(tmp_path / "proj")
    wt = gitops.create_worktree(str(repo), "qc")
    test_cmd = '"%s" -m pytest -q' % PY
    crit = [
        {"id": "C1", "text": "ok", "check": '"%s" -c "assert 1 == 1"' % PY},
        {"id": "C2", "text": "fail", "check": '"%s" -c "assert 1 == 2"' % PY},
        {"id": "C3", "text": "manual", "check": "manual"},
        {"id": "C4", "text": "env", "check": '"%s" -c "assert 1 == 1"' % PY, "env_dependent": True},
        {"id": "C5", "text": "same as test_cmd", "check": test_cmd},
    ]
    passed = checks.quick_check(wt, crit, test_cmd)
    assert passed == ["C1"]
