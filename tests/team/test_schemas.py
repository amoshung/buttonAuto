import json

import pytest
from pydantic import ValidationError

from team.models import ModelConfig, ModelError, ModelRouter
from team.schemas import (
    AutotunePatch,
    DiagnosticVerdict,
    LessonEntry,
    SecondOpinion,
    SpecBreakdown,
    response_format_for,
    validate_schema,
)


def _cfg():
    return ModelConfig(base_url="http://litellm.local:4000", glm_key="sk-glm", decision1_key="sk-d1")


def test_response_format_for_builds_strict_json_schema():
    rf = response_format_for(SecondOpinion, strict=True)
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "SecondOpinion"
    assert rf["json_schema"]["strict"] is True
    assert "ok" in rf["json_schema"]["schema"]["properties"]


def test_validate_schema_parses_valid_json():
    obj = validate_schema('{"ok": true, "reason": "All checks pass"}', SecondOpinion)
    assert isinstance(obj, SecondOpinion)
    assert obj.ok is True
    assert obj.reason == "All checks pass"


def test_validate_schema_rejects_missing_required_field():
    with pytest.raises((ValueError, ValidationError)):
        validate_schema('{"ok": true}', SecondOpinion)


def test_write_json_happy_path_and_single_repair_retry():
    calls = []

    def fake_glm(prompt, effort, timeout, response_format=None):
        calls.append((prompt, effort, response_format))
        if len(calls) == 1:
            return '{"ok": "not-a-bool"}'
        return '{"ok": false, "reason": "Tests failed"}'

    router = ModelRouter(_cfg(), glm=fake_glm)
    res = router.write_json("Check this", SecondOpinion, effort="low")
    assert isinstance(res, SecondOpinion)
    assert res.ok is False
    assert res.reason == "Tests failed"
    assert len(calls) == 2
    assert calls[0][2]["type"] == "json_schema"
    assert "驗證錯誤" in calls[1][0] or "schema" in calls[1][0].lower()


def test_write_json_fails_closed_after_single_retry():
    calls = []

    def fake_glm(prompt, effort, timeout, response_format=None):
        calls.append(prompt)
        return "not json at all {\"ok\": true}"

    router = ModelRouter(_cfg(), glm=fake_glm)
    with pytest.raises(ModelError):
        router.write_json("Check this", SecondOpinion, effort="low")
    assert len(calls) == 2


def test_write_json_degrades_to_json_object_when_strict_rejected():
    calls = []

    def fake_glm(prompt, effort, timeout, response_format=None):
        calls.append(response_format)
        if response_format and response_format.get("type") == "json_schema":
            raise ModelError("glm-5.3 回應 400")
        return json.dumps({"category": "code", "summary": "Null pointer", "next_action": "Guard None"})

    router = ModelRouter(_cfg(), glm=fake_glm)
    res = router.write_json("Diagnose", DiagnosticVerdict, effort="low")
    assert isinstance(res, DiagnosticVerdict)
    assert res.category == "code"
    assert calls[0]["type"] == "json_schema"
    assert calls[1]["type"] == "json_object"


def test_second_opinion_fails_closed_on_invalid_schema():
    def bad_glm(prompt, effort, timeout, response_format=None):
        return "Sure! {\"ok\": true} looks fine"

    router = ModelRouter(_cfg(), glm=bad_glm)
    ok, reason = router.second_opinion("Approve?", "some diff")
    assert ok is None
    assert "不可用" in reason or "格式" in reason
