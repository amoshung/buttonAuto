"""Task 6：升級階梯（L1→L2→L3→L4→stop）與選擇性預算狀態機。"""
from team.ladder import (
    DIAG_CATS,
    IDLE_TO_ESCALATE,
    LEVELS,
    Ladder,
    budget_state,
    grant,
)


def test_two_idle_rounds_escalate_and_progress_resets():
    assert LEVELS == ("L1", "L2", "L3", "L4", "stop")
    assert IDLE_TO_ESCALATE == 2
    l = Ladder()
    assert l.record(False) is None and l.idle == 1
    assert l.record(False) == ("L1", "L2") and l.level == "L2" and l.idle == 0
    assert len(l.history) == 1 and l.history[0]["from"] == "L1" and l.history[0]["to"] == "L2"

    l2 = Ladder(level="L3")
    assert l2.record(False) is None and l2.idle == 1
    assert l2.record(True) is None and l2.idle == 0


def test_diagnosis_code_returns_once_then_l3():
    l = Ladder(level="L2")
    assert l.after_diagnosis("code") == ("L2", "L1") and l.level == "L1" and l.code_returns == 1
    l.level = "L2"
    assert l.after_diagnosis("code") == ("L2", "L3") and l.level == "L3"


def test_diagnosis_other_categories_stop():
    for c in (*DIAG_CATS, "review", "unclear"):
        if c == "code":
            continue
        assert Ladder(level="L2").after_diagnosis(c) == ("L2", "stop")


def test_l4_exhausted_stops():
    l = Ladder(level="L4")
    assert l.record(False) is None
    assert l.record(False) == ("L4", "stop") and l.level == "stop"


def test_reset_returns_to_l1():
    l = Ladder(level="stop", idle=1)
    l.reset("使用者回答")
    assert l.level == "L1" and l.idle == 0
    assert l.history[-1]["to"] == "L1" and l.history[-1]["reason"] == "使用者回答"


def test_budget_unlimited_by_default():
    assert budget_state({}, {"seconds": 1e9, "rounds": 1e6, "tokens": 1e12}) == ("ok", "")
    assert budget_state({"rounds": None, "seconds": 0}, {"rounds": 999, "seconds": 999}) == ("ok", "")


def test_budget_warn_and_out_and_grant():
    lim = {"rounds": 10}
    assert budget_state(lim, {"rounds": 7}) == ("ok", "")
    assert budget_state(lim, {"rounds": 8}) == ("warn", "rounds")
    assert budget_state(lim, {"rounds": 10}) == ("out", "rounds")
    lim2 = grant(lim, {"rounds": 10})
    assert lim2["rounds"] == 20 and lim2["base"]["rounds"] == 10
    lim3 = grant(lim2, {"rounds": 20})
    assert lim3["rounds"] == 30


def test_ladder_roundtrip():
    l = Ladder(level="L3", idle=1, code_returns=1, history=[{"from": "L1", "to": "L2"}], tried=["修法 A"])
    assert Ladder.from_dict(l.to_dict()) == l
