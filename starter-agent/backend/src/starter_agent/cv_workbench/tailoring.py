"""Evidence-bound AI tailoring contracts and provider adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from starter_agent.domain.models import Message
from starter_agent.providers.base import Provider
from starter_agent.cv_workbench.contracts import (
    MatchAnalysis,
    MatchStatus,
    RequirementVerdict,
    ResumeDraft,
    Suggestion,
)
from starter_agent.cv_workbench.suggestions import (
    SuggestionCommand,
    SuggestionService,
)
from starter_agent.cv_workbench.versioning import ResumeVersionService


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


@dataclass(frozen=True)
class TailoringCommand:
    workspace_id: str
    analysis_id: str
    draft_id: str


class TailoringResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    draft_revision: int
    items: tuple[Suggestion, ...] = ()
    reflection: ReflectionResult
    reused: bool = False
    rejected_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class _AlignedBlock:
    block_id: str
    text: str


_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?%?")


class TailoredResumeService:
    def __init__(
        self,
        *,
        store,
        versions: ResumeVersionService,
        suggestions: SuggestionService,
        generator: TailoredResumeGenerator,
    ) -> None:
        self.store = store
        self.versions = versions
        self.suggestions = suggestions
        self.generator = generator

    async def generate_candidates(
        self,
        command: TailoringCommand,
        *,
        principal: str,
    ) -> TailoringResult:
        analysis = self.store.get(
            MatchAnalysis,
            command.analysis_id,
            principal=principal,
        )
        draft = self.store.get(
            ResumeDraft,
            command.draft_id,
            principal=principal,
        )
        self.store.assert_entity_in_workspace(
            draft.draft_id,
            command.workspace_id,
            principal=principal,
        )
        if analysis.workspace_id != command.workspace_id:
            raise TailoringServiceError("tailoring_workspace_mismatch")
        if analysis.status not in {MatchStatus.VALIDATED, MatchStatus.PARTIAL}:
            raise TailoringServiceError("tailoring_analysis_not_ready")
        if draft.base_version_id != analysis.resume_version_id:
            raise TailoringServiceError("tailoring_draft_base_mismatch")

        existing = tuple(
            item
            for item in self._all_suggestions(principal)
            if item.analysis_id == analysis.analysis_id
            and item.target_draft_id == draft.draft_id
            and item.target_draft_revision == draft.revision
            and item.change_type == "ai_tailor_v1"
        )
        if existing:
            return TailoringResult(
                draft_id=draft.draft_id,
                draft_revision=draft.revision,
                items=existing,
                reflection=ReflectionResult(
                    notes="已复用同一 Draft revision 的定制建议"
                ),
                reused=True,
            )

        markdown = self.versions.content.read(
            draft.content,
            principal=principal,
            workspace_id=command.workspace_id,
        )
        normalized = self.versions.normalizer.normalize(markdown)
        lines = normalized.markdown.splitlines()
        blocks = tuple(
            _AlignedBlock(
                block_id=item.block_id,
                text="\n".join(lines[item.start_line - 1 : item.end_line]),
            )
            for item in normalized.blocks
        )
        positive = tuple(
            item
            for item in analysis.requirements
            if item.verdict
            in {RequirementVerdict.MATCHED, RequirementVerdict.PARTIAL}
        )
        requirements = tuple(
            {
                "requirement_id": item.requirement_id,
                "original_text": item.original_text,
            }
            for item in positive
        )
        evidence_by_id = {}
        aligned_by_id = {}
        request_evidence = []
        used_blocks = {}
        for requirement in positive:
            for reference in requirement.evidence:
                quote = self._clean(reference.quote or "")
                block = next(
                    (
                        item
                        for item in blocks
                        if quote and quote in self._clean(item.text)
                    ),
                    None,
                )
                if block is None:
                    continue
                evidence_id = f"ev_{len(request_evidence) + 1}"
                request_evidence.append(
                    TailoringEvidence(
                        evidence_id=evidence_id,
                        requirement_id=requirement.requirement_id,
                        quote=reference.quote or "",
                    )
                )
                evidence_by_id[evidence_id] = reference
                aligned_by_id[evidence_id] = block
                used_blocks[block.block_id] = block
        if not request_evidence:
            raise TailoringServiceError("tailoring_no_verified_evidence")

        generated = await self.generator.generate(
            TailoringGenerationRequest(
                analysis_id=analysis.analysis_id,
                draft_id=draft.draft_id,
                draft_revision=draft.revision,
                requirements=requirements,
                blocks=tuple(
                    TailoringBlock(
                        block_id=item.block_id,
                        original_text=item.text,
                    )
                    for item in used_blocks.values()
                ),
                evidence=tuple(request_evidence),
            )
        )
        positive_by_id = {item.requirement_id: item for item in positive}
        created = []
        rejected = []
        candidate_blocks = set()
        block_by_id = {item.block_id: item for item in used_blocks.values()}
        for candidate in generated.candidates[:8]:
            reasons = []
            block = block_by_id.get(candidate.block_id)
            if block is None:
                reasons.append("unknown_block")
            elif candidate.block_id in candidate_blocks:
                reasons.append("duplicate_block")
            requirement_ids = tuple(dict.fromkeys(candidate.requirement_ids))
            if any(item not in positive_by_id for item in requirement_ids):
                reasons.append("unknown_requirement")
            selected_evidence = tuple(
                evidence_by_id[item]
                for item in candidate.evidence_ids
                if item in evidence_by_id
            )
            if len(selected_evidence) != len(candidate.evidence_ids):
                reasons.append("unknown_evidence")
            if any(
                item in aligned_by_id
                and aligned_by_id[item].block_id != candidate.block_id
                for item in candidate.evidence_ids
            ):
                reasons.append("evidence_block_mismatch")
            request_evidence_by_id = {
                item.evidence_id: item for item in request_evidence
            }
            if any(
                item in request_evidence_by_id
                and request_evidence_by_id[item].requirement_id
                not in requirement_ids
                for item in candidate.evidence_ids
            ):
                reasons.append("evidence_requirement_mismatch")
            allowed_text = "\n".join(
                [block.text if block else ""]
                + [item.quote or "" for item in selected_evidence]
            )
            if self._numbers(candidate.proposed_text) - self._numbers(allowed_text):
                reasons.append("new_numeric_claim")
            if block is not None and self._clean(candidate.proposed_text) == self._clean(
                block.text
            ):
                reasons.append("unchanged_text")
            if reasons:
                rejected.extend(reason for reason in reasons if reason not in rejected)
                continue
            digest = sha256(
                "\0".join(
                    (
                        PROMPT_VERSION,
                        analysis.analysis_id,
                        draft.draft_id,
                        str(draft.revision),
                        candidate.block_id,
                        *requirement_ids,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
            created.append(
                self.suggestions.create(
                    SuggestionCommand(
                        suggestion_id=f"sg_{digest}",
                        analysis_id=analysis.analysis_id,
                        target_version_id=analysis.resume_version_id,
                        target_draft_id=draft.draft_id,
                        target_draft_revision=draft.revision,
                        block_id=candidate.block_id,
                        original_text=block.text,
                        proposed_text=candidate.proposed_text,
                        change_type="ai_tailor_v1",
                        reason=candidate.reason,
                        resume_evidence=selected_evidence,
                        requirement_ids=requirement_ids,
                        risk=candidate.risk,
                        allow_partial_analysis=analysis.status == MatchStatus.PARTIAL,
                    ),
                    workspace_id=command.workspace_id,
                    principal=principal,
                )
            )
            candidate_blocks.add(candidate.block_id)
        return TailoringResult(
            draft_id=draft.draft_id,
            draft_revision=draft.revision,
            items=tuple(created),
            reflection=generated.reflection,
            rejected_reasons=tuple(rejected),
        )

    def _all_suggestions(self, principal: str) -> tuple[Suggestion, ...]:
        values = []
        cursor = None
        while True:
            page = self.store.list(Suggestion, principal=principal, cursor=cursor)
            values.extend(page.items)
            if page.next_cursor is None:
                return tuple(values)
            cursor = page.next_cursor

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())

    @staticmethod
    def _numbers(value: str) -> set[str]:
        return {item.casefold() for item in _NUMBER.findall(value)}
