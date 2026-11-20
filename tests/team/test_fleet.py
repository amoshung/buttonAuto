import json
import threading
from datetime import datetime, timedelta

import pytest

from team.fleet import (DEFAULT_CATALOG, SEAT_LABELS, SEATS, Assignment, Dispatcher, NoSeatError, QuotaBook,
                        detect_quota, load_catalog, parse_reset)
from team import drivers

NOW = datetime(2026, 11, 17, 10, 0, 0)
COMPLEX_Q = "此任務需要跨多檔重構或深入除錯嗎？"


class FakeRouter:
    def __init__(self, complex_p=None, seat_p=None, default=0.5, down=False):
        self.complex_p = complex_p
        self.seat_p = seat_p or {}
        self.default = default
        self.down = down
        self.questions = []

    def probability(self, state_text, question):
        self.questions.append(question)
        if self.down:
            return None
        if question == COMPLEX_Q:
            return self.complex_p
        for seat, p in self.seat_p.items():
            if question == "%s 適合這個任務嗎？" % SEAT_LABELS[seat]:
                return p
        return self.default


def _disp(tmp_path, router, available=None):
    q = QuotaBook(str(tmp_path))
    avail = set(SEATS) if available is None else available
    return Dispatcher(router, load_catalog(str(tmp_path / "none.json")), q, lambda: set(avail)), q


def test_seats_match_drivers():
    assert SEATS == list(drivers.SEATS)
    assert set(SEAT_LABELS) == set(SEATS)


def test_detect_quota_patterns():
    assert detect_quota("You've hit your usage limit · resets at 3pm")
    assert detect_quota("HTTP 429 Too Many Requests")
    assert detect_quota("本月額度已用完")
    assert not detect_quota("rate of change")
    assert not detect_quota("")


def test_parse_reset():
    assert parse_reset("try again in 2 hours", NOW) == NOW + timedelta(hours=2)
    assert parse_reset("Please try again in 15 minutes.", NOW) == NOW + timedelta(minutes=15)
    assert parse_reset("usage limit · resets at 3pm", NOW) == NOW.replace(hour=15)
    assert parse_reset("resets at 9:30am", NOW) == (NOW + timedelta(days=1)).replace(hour=9, minute=30)
    assert parse_reset("resets at 12am", NOW) == (NOW + timedelta(days=1)).replace(hour=0)
    assert parse_reset("resets at 14:05", NOW) == NOW.replace(hour=14, minute=5)
    assert parse_reset("usage limit reached", NOW) is None


def test_load_catalog(tmp_path):
    assert load_catalog(str(tmp_path / "missing.json")) == DEFAULT_CATALOG
    p = tmp_path / "team_models.json"
    p.write_text(json.dumps({"kiro": [{"id": "claude-sonnet-4.5", "tier": "premium", "cost": 4}]}), encoding="utf-8")
    cat = load_catalog(str(p))
    assert cat["kiro"] == [{"id": "claude-sonnet-4.5", "tier": "premium", "cost": 4}]
    assert cat["claude"] == DEFAULT_CATALOG["claude"]
    p.write_text("{oops", encoding="utf-8")
    assert load_catalog(str(p)) == DEFAULT_CATALOG
    p.write_text(json.dumps({"kiro": [{"id": "x", "tier": "huge", "cost": 1}]}), encoding="utf-8")
    assert load_catalog(str(p)) == DEFAULT_CATALOG
    cat["kiro"].append({})
    assert DEFAULT_CATALOG["kiro"] == [{"id": "", "tier": "standard", "cost": 2}]


def test_quotabook_persist_and_probe(tmp_path):
    q = QuotaBook(str(tmp_path))
    until = q.mark_exhausted("kiro", "You've hit your usage limit · resets at 3pm", NOW)
    assert until == NOW.replace(hour=15)
    until2 = q.mark_exhausted("claude", "usage limit reached", NOW)
    assert until2 == NOW + timedelta(minutes=60)
    assert q.is_exhausted("kiro", NOW) and not q.is_exhausted("agy", NOW)
    assert q.due_probes(NOW) == []
    assert q.due_probes(NOW + timedelta(minutes=61)) == ["claude"]
    assert q.due_probes(NOW.replace(hour=16)) == ["kiro", "claude"]

    q2 = QuotaBook(str(tmp_path))
    assert (tmp_path / "quota.json").exists()
    assert q2.is_exhausted("kiro", NOW)
    snap = q2.snapshot()
    assert snap["kiro"]["state"] == "exhausted"
    assert snap["kiro"]["until"] == NOW.replace(hour=15).isoformat()
    assert snap["kiro"]["since"] == NOW.isoformat()
    assert snap["agy"]["state"] == "ok"
    q2.mark_ok("kiro")
    assert not q2.is_exhausted("kiro", NOW)
    assert not QuotaBook(str(tmp_path)).is_exhausted("kiro", NOW)
    assert QuotaBook(str(tmp_path / "quota.json")).is_exhausted("claude", NOW)


def test_quotabook_threadsafe(tmp_path):
    q = QuotaBook(str(tmp_path))
    ts = [threading.Thread(target=q.mark_exhausted, args=(s, "429", NOW)) for s in SEATS * 5]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(QuotaBook(str(tmp_path)).is_exhausted(s, NOW) for s in SEATS)


def test_quotabook_corrupt_file(tmp_path):
    (tmp_path / "quota.json").write_text("{bad", encoding="utf-8")
    q = QuotaBook(str(tmp_path))
    assert not q.is_exhausted("kiro", NOW)


def test_dispatch_prefers_free_for_simple(tmp_path):
    d, _ = _disp(tmp_path, FakeRouter(complex_p=0.1, default=0.8))
    a = d.pick("build", "把 README 的錯字修好")
    assert isinstance(a, Assignment)
    assert a.seat == "glm" and a.model == "glm-5.3" and a.by == "Decision-1"


def test_dispatch_premium_for_complex(tmp_path):
    d, _ = _disp(tmp_path, FakeRouter(complex_p=0.9, seat_p={"claude": 0.95, "glm": 0.5}, default=0.5))
    a = d.pick("build", "重構整個驅動層並追查競態")
    assert (a.seat, a.model, a.tier, a.by) == ("claude", "opus", "premium", "Decision-1")


def test_dispatch_skips_exhausted_and_excluded(tmp_path):
    router = FakeRouter(complex_p=0.9, seat_p={"claude": 0.95, "kiro": 0.6, "glm": 0.1, "glm_high": 0.1})
    d, q = _disp(tmp_path, router, available={"kiro", "claude", "glm", "glm_high"})
    q.mark_exhausted("claude", "usage limit", datetime.now())
    assert "claude" not in d.feasible()
    assert d.feasible(exclude="kiro") == ["glm", "glm_high"]
    assert d.pick("build", "x").seat == "glm"  # kiro 分數較高也不選：自動只用 Claude／GLM
    assert d.pick("review", "x", exclude="glm").seat == "glm_high"
    q.mark_ok("claude")
    assert d.pick("build", "x").seat == "claude"


def test_dispatch_rule_when_models_down(tmp_path):
    d, _ = _disp(tmp_path, FakeRouter(down=True), available={"kiro", "claude", "glm"})
    a = d.pick("build", "x")
    assert a.by == "rule" and a.seat == "glm"
    d2, _ = _disp(tmp_path, FakeRouter(down=True), available={"kiro", "agy", "claude"})
    a2 = d2.pick("build", "x")
    # p_complex 視為 0.5 → standard；kiro／agy 較便宜也不選 → claude sonnet
    assert (a2.by, a2.seat, a2.tier) == ("rule", "claude", "standard")
    d3, _ = _disp(tmp_path, FakeRouter(down=True), available={"claude"})
    assert (d3.pick("fix", "x").model, d3.pick("fix", "x").tier) == ("haiku", "economy")


def test_user_choice_wins(tmp_path):
    router = FakeRouter(complex_p=0.1, default=0.8)
    d, q = _disp(tmp_path, router)
    a = d.pick("build", "x", user_seat="agy")
    assert a.seat == "agy" and a.by == "user"
    assert router.questions == []
    q.mark_exhausted("agy", "429", datetime.now())
    assert d.pick("build", "x", user_seat="agy").by != "user"


def test_final_review_fixed_glm_high(tmp_path):
    router = FakeRouter(complex_p=0.9, seat_p={"claude": 0.99})
    d, _ = _disp(tmp_path, router)
    a = d.pick("final_review", "x")
    assert a.seat == "glm_high" and router.questions == []


def test_no_seat_and_fallback(tmp_path):
    d, _ = _disp(tmp_path, FakeRouter(), available=set())
    with pytest.raises(NoSeatError) as ei:
        d.pick("build", "x")
    assert "所有座位都不可用" in str(ei.value)
    with pytest.raises(NoSeatError):
        d.fallback("kiro")
    d2, _ = _disp(tmp_path, FakeRouter(), available={"kiro", "glm"})
    f = d2.fallback("kiro")
    assert (f.seat, f.model, f.tier, f.by) == ("glm", "glm-5.3", "standard", "fallback")


class CountingNoneRouter:
    def __init__(self):
        self.calls = 0

    def probability(self, state_text, question):
        self.calls += 1
        return None


def test_dispatch_stops_asking_after_none(tmp_path):
    r = CountingNoneRouter()
    d, _ = _disp(tmp_path, r)
    a = d.pick("build", "x")
    assert a.by == "rule" and r.calls == 1
    r2 = CountingNoneRouter()
    d2, _ = _disp(tmp_path, r2)
    assert d2.pick("review", "x").by == "rule" and r2.calls == 1


def test_quotabook_save_error_swallowed(tmp_path, monkeypatch):
    q = QuotaBook(str(tmp_path))

    def bad_replace(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("team.fleet.os.replace", bad_replace)
    q.mark_exhausted("kiro", "429", NOW)
    assert q.is_exhausted("kiro", NOW)
    q.mark_ok("kiro")
    assert not q.is_exhausted("kiro", NOW)


def test_quotabook_load_aware_timestamps(tmp_path):
    from datetime import timezone
    until = datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc)
    (tmp_path / "quota.json").write_text(json.dumps({"kiro": {
        "state": "exhausted", "until": until.isoformat(), "since": until.isoformat()}}), encoding="utf-8")
    q = QuotaBook(str(tmp_path))
    local = until.astimezone().replace(tzinfo=None)
    assert q.snapshot()["kiro"]["until"] == local.isoformat()
    assert q.due_probes(local) == ["kiro"]
    assert q.due_probes(local - timedelta(seconds=1)) == []


class CtxRouter(FakeRouter):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.ctxs = []

    def probability(self, state_text, question, ctx=None):
        self.ctxs.append(ctx)
        return super().probability(state_text, question)


def test_dispatch_reason_shows_scores_and_ctx(tmp_path):
    router = CtxRouter(complex_p=0.5, seat_p={"kiro": 0.82, "claude": 0.90}, default=0.1)
    d, _ = _disp(tmp_path, router, available={"kiro", "claude"})
    a = d.pick("build", "x", ctx={"room_id": "r12345678"})
    # kiro：0.82 − 0.3×2/15＝0.78；claude（sonnet 3）：0.90 − 0.3×3/15＝0.84 → claude
    assert a.seat == "claude"
    assert a.reason.startswith("Decision-1：難度 0.50 → 標準；")
    assert "Claude 0.90−成本 0.06＝0.84" in a.reason and "Kiro" not in a.reason  # Kiro 不是自動候選
    assert a.reason.endswith("→ 選 Claude（標準）")
    assert all(c["room_id"] == "r12345678" and c["role"] == "dispatcher" for c in router.ctxs)
    assert router.ctxs[0]["purpose"] == "派工：評估任務難度"
    assert router.ctxs[1]["purpose"] == "派工：評估 Claude 適合度"


def test_dispatch_rule_reason_names_rule(tmp_path):
    d, _ = _disp(tmp_path, FakeRouter(down=True), available={"kiro", "claude"})
    a = d.pick("review", "x")
    assert a.by == "rule" and "規則：判斷模型不可用" in a.reason and "成本最低" in a.reason


def test_auto_never_picks_kiro_or_agy(tmp_path):
    from team.fleet import AUTO_SEATS, NO_AUTO_SEAT
    assert AUTO_SEATS == ("claude", "glm", "glm_high")
    router = FakeRouter(complex_p=0.5, seat_p={"kiro": 0.99, "agy": 0.99, "claude": 0.2, "glm": 0.2, "glm_high": 0.2})
    d, _ = _disp(tmp_path, router)
    for phase in ("spec", "build", "fix", "review"):
        a = d.pick(phase, "x")
        assert a.seat in AUTO_SEATS, (phase, a)
        assert d.pick(phase, "x", exclude=a.seat).seat in AUTO_SEATS
    assert not any("Kiro" in q or "Antigravity" in q for q in router.questions)
    # 你指定 kiro／agy 照樣可以
    assert d.pick("build", "x", user_seat="kiro").seat == "kiro"
    assert d.pick("review", "x", user_seat="agy", exclude="kiro").seat == "agy"
    # 只有 kiro／agy 可用 → 自動派工給明確的錯誤
    d2, _ = _disp(tmp_path, FakeRouter(), available={"kiro", "agy"})
    with pytest.raises(NoSeatError) as ei:
        d2.pick("build", "x")
    assert str(ei.value) == NO_AUTO_SEAT
    assert d2.pick("build", "x", user_seat="kiro").seat == "kiro"


def test_simple_fix_routes_to_glm_without_model_call(tmp_path):
    from team.fleet import classify_complexity
    assert classify_complexity("修好 README 錯字", phase="fix", retry_count=0) == "simple"
    assert classify_complexity("修好錯字", phase="build", retry_count=2) == "complex"
    router = FakeRouter(complex_p=0.9, seat_p={"claude": 0.99, "glm": 0.1})
    d, _ = _disp(tmp_path, router)
    a = d.pick("fix", "修好錯字", retry_count=0)
    assert a.seat == "glm" and a.model == "glm-5.3" and a.by == "rule"
    assert router.questions == []


def test_retry_escalation_forces_premium_tier(tmp_path):
    router = FakeRouter(complex_p=0.1, seat_p={"claude": 0.95, "glm": 0.2})
    d, _ = _disp(tmp_path, router)
    a = d.pick("build", "修好錯字", retry_count=2)
    assert a.tier == "premium" and a.seat == "claude"
    assert COMPLEX_Q not in router.questions
