"""Task 2：失敗名單解析、錯誤指紋、RoundStat 與進展判斷。"""
from dataclasses import replace

from team.rounds import (
    ROUNDS_KEEP,
    RoundStat,
    append_round,
    fingerprint,
    first_error_line,
    normalize_line,
    parse_failures,
    progressed,
)

PYTEST_OUT = (
    "...\n"
    "FAILED tests/test_a.py::test_x - AssertionError: 1 != 2\n"
    "ERROR tests/test_b.py::test_y\n"
    "FAILED tests/test_a.py::test_x - duplicate\n"
    "1 failed, 124 passed in 3.2s\n"
)

BASE = RoundStat(
    n=1,
    at="2026-11-17T10:00:00",
    level="L1",
    seat="glm",
    model="glm-5.3",
    prompt_versions={"implementer": "default"},
    criteria_passed=["C1"],
    new_failures=["t1", "t2"],
    env_failures=[],
    fingerprint="aa",
    commits_added=0,
    evidence_mismatch=False,
    tokens=100,
    seconds=5.0,
)


def test_parse_pytest():
    assert parse_failures(PYTEST_OUT) == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]


def test_parse_all_passed():
    assert parse_failures("125 passed in 3.0s") == []


def test_parse_jest_and_go():
    assert parse_failures("  ✕ adds numbers (5 ms)\n") == ["adds numbers"]
    assert parse_failures("  ● math › sub works\n") == ["math › sub works"]
    assert parse_failures("--- FAIL: TestMul (0.00s)\nFAIL\n") == ["TestMul"]


def test_unparseable_output_still_fingerprints():
    assert parse_failures("Traceback ...\nImportError: no module named x\n") is None
    fp = fingerprint(None, "ImportError: no module named x at D:\\a\\b.py:12", 2)
    assert fp and fp == fingerprint(None, "ImportError: no module named x at C:\\z.py:99", 2)


def test_fingerprint_ignores_numbers_paths_addresses():
    a = fingerprint(["t1"], "AssertionError at /tmp/a.py:10 0xdeadbeef", 1)
    b = fingerprint(["t1"], "AssertionError at /var/b.py:77 0x1234", 1)
    assert a == b
    assert fingerprint([], "3 passed", 0) == ""
    assert first_error_line("hello\nAssertionError: bad\nlater") == "AssertionError: bad"
    assert "<addr>" in normalize_line("at 0xdeadbeef")


def test_progressed_rules():
    assert progressed(None, BASE)
    assert not progressed(None, replace(BASE, criteria_passed=[], commits_added=0))
    assert not progressed(None, replace(BASE, evidence_mismatch=True))

    more = replace(BASE, n=2, criteria_passed=["C1", "C2"])
    assert progressed(BASE, more)
    assert not progressed(BASE, replace(more, evidence_mismatch=True))
    assert not progressed(BASE, replace(more, new_failures=["t1", "t2", "t3"]))
    assert progressed(BASE, replace(BASE, n=2, new_failures=["t1"]))
    assert progressed(BASE, replace(BASE, n=2, fingerprint="bb", commits_added=1))
    assert not progressed(BASE, replace(BASE, n=2, fingerprint="bb", commits_added=0))
    assert progressed(replace(BASE, review_blocking=3), replace(BASE, n=2, review_blocking=1))
    assert not progressed(replace(BASE, review_blocking=3), replace(BASE, n=2, review_blocking=3))


def test_roundstat_roundtrip_and_append_keeps_50():
    d = BASE.to_dict()
    assert RoundStat.from_dict(d) == BASE
    rs = []
    for i in range(60):
        rs = append_round(rs, replace(BASE, n=i))
    assert len(rs) == ROUNDS_KEEP == 50 and rs[0]["n"] == 10 and rs[-1]["n"] == 59
