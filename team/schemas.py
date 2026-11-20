"""強制結構化輸出契約（Pydantic v2 + OpenAI response_format json_schema）。"""
import json
from typing import Any, Dict, List, Literal, Type, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecondOpinion(_StrictModel):
    ok: bool
    reason: str = Field(..., min_length=1, max_length=500)


class DiagnosticVerdict(_StrictModel):
    category: Literal["test_or_check", "code", "data_or_env", "assistant_system"]
    summary: str = Field(..., min_length=1, max_length=500)
    next_action: str = Field(..., min_length=1, max_length=500)


class LessonEntry(_StrictModel):
    category: str = Field(..., min_length=1, max_length=40)
    fingerprint: str = Field(default="", max_length=64)
    root_cause: str = Field(default="", max_length=300)
    fix: str = Field(default="", max_length=300)
    lesson: str = Field(..., min_length=1, max_length=200)


class SpecBreakdown(_StrictModel):
    criteria: List[str] = Field(..., min_length=1)
    notes: str = Field(default="", max_length=1000)


class AutotunePatch(_StrictModel):
    identity: str = Field(default="")
    principles: str = Field(default="")
    checklist: str = Field(default="")
    rationale: str = Field(default="", max_length=500)


def _inline_schema(schema_cls: Type[BaseModel]) -> Dict[str, Any]:
    raw = schema_cls.model_json_schema()
    raw.setdefault("additionalProperties", False)
    return raw


def response_format_for(schema_cls: Type[BaseModel], strict: bool = True) -> Dict[str, Any]:
    """產生 OpenAI 相容 /v1/chat/completions 的 response_format（json_schema）。"""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema_cls.__name__,
            "strict": bool(strict),
            "schema": _inline_schema(schema_cls),
        },
    }


def validate_schema(raw_text: str, schema_cls: Type[T]) -> T:
    """嚴格驗證 JSON 字串並轉換為 Pydantic 模型；不允許非 JSON 前後綴或正則硬撈。"""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("模型回覆為空")
    data = json.loads(raw_text.strip())
    return schema_cls.model_validate(data)
