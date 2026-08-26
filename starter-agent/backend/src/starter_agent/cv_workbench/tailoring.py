"""Evidence-bound AI tailoring contracts and provider adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from starter_agent.domain.models import Message
from starter_agent.providers.base import Provider


PROMPT_VERSION = "tailored-resume-v1"


class TailoringServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TailoringBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    block_id: str = Field(min_length=1, max_length=160)
    original_text: str = Field(min_length=1, max_length=100_000)


class TailoringEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1, max_length=160)
    requirement_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=1, max_length=10_000)


class TailoringGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    analysis_id: str = Field(min_length=1, max_length=160)
    draft_id: str = Field(min_length=1, max_length=160)
    draft_revision: int = Field(ge=1)
    requirements: tuple[dict[str, str], ...]
    blocks: tuple[TailoringBlock, ...]
    evidence: tuple[TailoringEvidence, ...]


class GeneratedTailoringCandidate(BaseModel):
    block_id: str = Field(min_length=1, max_length=160)
    proposed_text: str = Field(min_length=1, max_length=10_000)
    reason: str = Field(min_length=1, max_length=2_000)
    requirement_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    risk: str | None = Field(default=None, max_length=2_000)


class ReflectionResult(BaseModel):
    issues_found: int = Field(default=0, ge=0)
    issues: tuple[dict[str, str], ...] = ()
    notes: str = ""


class TailoringGenerationResult(BaseModel):
    reflection: ReflectionResult
    candidates: tuple[GeneratedTailoringCandidate, ...] = ()


class TailoredResumeGenerator(Protocol):
    async def generate(
        self,
        request: TailoringGenerationRequest,
    ) -> TailoringGenerationResult: ...


_REFLECTION_PROMPT = """你是简历证据反思审核员。
只审核输入中提供的原文、岗位要求和证据，检测套话、职责升级、新增量化数据、
技能虚构和证据冲突。不要撰写简历。只输出 JSON：
{"issues_found": 0, "issues": [], "notes": ""}
"""


_WRITER_PROMPT = """你是证据约束的简历撰写助手。
只能改写输入中给出的 block，且只能使用给出的 requirement_id 与 evidence_id。
禁止新增输入证据中没有的技能、角色、年限、数字或结果。每个 block 最多一条候选，
最多返回 8 条。请参考反思结果并保持职责边界。只输出 JSON：
{"candidates":[{"block_id":"", "proposed_text":"", "reason":"",
"requirement_ids":[""], "evidence_ids":[""], "risk":""}]}
"""


def _parse_json_object(text: str | None) -> dict[str, object]:
    if text is None:
        raise TailoringServiceError("tailoring_output_invalid")
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned[3:-3].strip()
        if cleaned.casefold().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise TailoringServiceError("tailoring_output_invalid") from error
    if not isinstance(value, dict):
        raise TailoringServiceError("tailoring_output_invalid")
    return value


class ProviderTailoredResumeGenerator:
    def __init__(
        self,
        *,
        provider_resolver: Callable[[str], Provider],
        provider_name: str,
        model: str,
    ) -> None:
        self.provider_resolver = provider_resolver
        self.provider_name = provider_name
        self.model = model

    async def generate(
        self,
        request: TailoringGenerationRequest,
    ) -> TailoringGenerationResult:
        provider = self.provider_resolver(self.provider_name)
        request_json = request.model_dump_json()
        reflection_response = await provider.complete(
            [
                Message(role="system", content=_REFLECTION_PROMPT),
                Message(role="user", content=request_json),
            ],
            self.model,
            tools=[],
            max_output_tokens=4_000,
        )
        try:
            reflection = ReflectionResult.model_validate(
                _parse_json_object(reflection_response.content)
            )
        except ValidationError as error:
            raise TailoringServiceError("tailoring_output_invalid") from error

        writer_response = await provider.complete(
            [
                Message(role="system", content=_WRITER_PROMPT),
                Message(
                    role="user",
                    content=json.dumps(
                        {
                            "request": request.model_dump(mode="json"),
                            "reflection": reflection.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            ],
            self.model,
            tools=[],
            max_output_tokens=4_000,
        )
        try:
            payload = _parse_json_object(writer_response.content)
            return TailoringGenerationResult(
                reflection=reflection,
                candidates=tuple(
                    GeneratedTailoringCandidate.model_validate(item)
                    for item in payload.get("candidates", [])
                ),
            )
        except (TypeError, ValidationError) as error:
            raise TailoringServiceError("tailoring_output_invalid") from error
