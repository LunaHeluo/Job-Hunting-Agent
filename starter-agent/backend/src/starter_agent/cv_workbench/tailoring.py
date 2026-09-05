"""Evidence-bound AI tailoring contracts and provider adapter."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal, Protocol
from weakref import WeakValueDictionary
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from starter_agent.domain.models import Message
from starter_agent.providers.base import Provider
from starter_agent.cv_workbench.contracts import (
    EvidenceReference,
    MatchAnalysis,
    MatchStatus,
    RequirementResult,
    RequirementVerdict,
    ResumeDraft,
    ResumeDraftStatus,
    SuggestionStatus,
    Suggestion,
)
from starter_agent.cv_workbench.suggestions import (
    SuggestionCommand,
    SuggestionService,
)
from starter_agent.cv_workbench.versioning import ResumeVersionService


PROMPT_VERSION = "tailored-resume-v4-context-sections"


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
    block_id: str | None = None


class TailoringGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    analysis_id: str = Field(min_length=1, max_length=160)
    draft_id: str = Field(min_length=1, max_length=160)
    draft_revision: int = Field(ge=1)
    requirements: tuple[dict[str, str], ...]
    blocks: tuple[TailoringBlock, ...]
    evidence: tuple[TailoringEvidence, ...]
    resume_context: tuple[TailoringBlock, ...] = ()
    target_requirements: tuple[dict[str, str], ...] = ()
    attempt: int = Field(default=1, ge=1, le=2)
    validation_feedback: tuple[str, ...] = ()


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
    block_failures: dict[str, str] = Field(default_factory=dict)


class TailoredResumeGenerator(Protocol):
    async def generate(
        self,
        request: TailoringGenerationRequest,
    ) -> TailoringGenerationResult: ...


_REFLECTION_PROMPT = """你是简历证据反思审核员。
只审核输入中提供的原文、岗位要求和证据，检测套话、职责升级、新增量化数据、
技能虚构和证据冲突。没有量化数据本身不构成虚构或禁止改写的理由，不要求补造数据。
只针对已有事实提出表达建议，不把缺少业务规模、性能成果视为阻止基础改写的条件。
先阅读 resume_context 全文与 target_requirements 完整岗位要求，在 notes 中给出全文规划：
哪些真实经历优先突出，哪些保留原文，哪些要求缺证据需要用户补充。
未匹配要求不是候选事实，resume_context 只用于理解上下文，不扩大本次可写范围。
不要撰写简历。issues 中每项的 type、description、source 均为字符串。只输出 JSON：
{"issues_found": 0, "issues": [], "notes": ""}
"""


_WRITER_PROMPT = """你是证据约束的简历撰写助手。
输入 block 是本次可改写片段，不一定是整份简历。只返回该片段的改写，系统负责保留并组装其余全文。
只能改写输入中给出的 block，且只能使用给出的 requirement_id 与 evidence_id。
禁止新增输入证据中没有的技能、角色、年限、数字或结果。每个 block 最多一条候选，
本次只有一个可编辑 block，只返回这个 block 的候选。resume_context 是只读全文背景，
不能把其他经历、公司、时期的事实搬到当前段落。保留原文语言，除非输入明确要求翻译。
逐段考虑岗位重点：工作经历突出已有行动，项目突出实际技术应用，
技能突出已证明的使用场景。证据中的 block_id 指定其所属段落，禁止猜测或串用。
允许优化句序、合并重复描述，以凸显岗位相关事实；不得擅自将英文整段翻译成中文。
即使缺少量化数据，也可以进行这些表达优化；禁止把缺量化成果等同于无法改写。
不要为了凑建议扩写；不得把参与、协助升级成主导、独立负责或架构设计。
请参考反思结果并保持职责边界。如果 validation_feedback 非空，
必须修正上次对应问题，并原样使用输入中提供的 ID；只要存在安全改写空间，至少返回一条。
原文已经准确贴合岗位时，可以返回保持原文的候选并说明无需修改；
没有可用素材时才返回空 candidates。candidates 字段必须存在且为数组。只输出 JSON：
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

        candidates = []
        failures = {}
        for block in request.blocks:
            evidence = tuple(item for item in request.evidence
                             if item.block_id == block.block_id or
                             (item.block_id is None and len(request.blocks) == 1))
            requirement_ids = {item.requirement_id for item in evidence}
            scoped = request.model_copy(update={
                "blocks": (block,), "evidence": evidence,
                "requirements": tuple(item for item in request.requirements
                                      if item["requirement_id"] in requirement_ids),
            })
            try:
                writer_response = await provider.complete(
                    [Message(role="system", content=_WRITER_PROMPT),
                     Message(role="user", content=json.dumps({
                         "request": scoped.model_dump(mode="json"),
                         "reflection": reflection.model_dump(mode="json"),
                     }, ensure_ascii=False, separators=(",", ":")))],
                    self.model, tools=[], max_output_tokens=4_000,
                )
                payload = _parse_json_object(writer_response.content)
                if not isinstance(payload.get("candidates"), list):
                    raise TailoringServiceError("tailoring_output_invalid")
                items = tuple(GeneratedTailoringCandidate.model_validate(item)
                              for item in payload["candidates"])
                if len(items) > 1 or any(item.block_id != block.block_id for item in items):
                    raise TailoringServiceError("tailoring_output_invalid")
                allowed_ids = {item.evidence_id for item in evidence}
                if any(not set(item.evidence_ids) <= allowed_ids or
                       not set(item.requirement_ids) <= requirement_ids for item in items):
                    raise TailoringServiceError("tailoring_output_invalid")
                candidates.extend(items)
                if not items:
                    failures[block.block_id] = "empty_model_output"
            except (TypeError, ValidationError, TailoringServiceError) as error:
                if isinstance(error, TailoringServiceError) and error.code != "tailoring_output_invalid":
                    raise
                failures[block.block_id] = "tailoring_output_invalid"
        if not candidates and failures and set(failures.values()) == {"tailoring_output_invalid"}:
            raise TailoringServiceError("tailoring_output_invalid")
        return TailoringGenerationResult(reflection=reflection, candidates=tuple(candidates),
                                        block_failures=failures)


@dataclass(frozen=True)
class TailoringCommand:
    workspace_id: str
    analysis_id: str
    draft_id: str
    generation_id: str | None = None
    block_ids: tuple[str, ...] = ()


class TailoringAttemptDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempt: int
    candidate_count: int
    accepted_count: int
    rejected_reasons: tuple[str, ...] = ()


class TailoringResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    draft_revision: int
    generation_id: str | None = None
    block_ids: tuple[str, ...] = ()
    items: tuple[Suggestion, ...] = ()
    reflection: ReflectionResult
    reused: bool = False
    outcome: Literal["ready", "no_change", "insufficient_evidence", "generation_failed", "validation_failed"] = "ready"
    diagnostics: tuple[TailoringAttemptDiagnostic, ...] = ()
    rejected_reasons: tuple[str, ...] = ()
    attempts: int = Field(default=1, ge=0, le=2)
    unchanged_block_ids: tuple[str, ...] = ()
    block_failures: dict[str, str] = Field(default_factory=dict)


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
        self._locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()

    async def generate_candidates(
        self,
        command: TailoringCommand,
        *,
        principal: str,
    ) -> TailoringResult:
        key = (principal, command.draft_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await self._generate_candidates(command, principal=principal)

    async def _generate_candidates(self, command: TailoringCommand, *, principal: str) -> TailoringResult:
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
        if analysis.rule_version not in {"match-rule.v2", "match-rule.v2.1", "match-rule.v2.2", "match-rule.v2.3"}:
            raise TailoringServiceError("tailoring_analysis_upgrade_required")
        if draft.base_version_id != analysis.resume_version_id:
            raise TailoringServiceError("tailoring_draft_base_mismatch")

        if draft.status != ResumeDraftStatus.ACTIVE:
            raise TailoringServiceError("tailoring_draft_not_active")
        generation_id = command.generation_id
        if generation_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", generation_id):
            raise TailoringServiceError("tailoring_generation_id_invalid")
        block_ids = tuple(sorted(set(command.block_ids)))
        if generation_id:
            for event in self.store.list_events(draft.draft_id, principal=principal):
                if event.event_type == "tailoring_generation_completed" and event.payload.get("generation_id") == generation_id:
                    old = event.payload
                    if (old.get("analysis_id") != analysis.analysis_id or old.get("draft_revision") != draft.revision
                            or tuple(sorted(old.get("block_ids", []))) != block_ids):
                        raise TailoringServiceError("tailoring_generation_input_conflict")
        # Rehydrate a completed request including zero-candidate outcomes. A new
        # generation_id explicitly opts into another bounded model attempt.
        for previous in self.history(draft.draft_id, analysis_id=analysis.analysis_id, principal=principal):
            if (generation_id is None and previous.block_ids == block_ids) or previous.generation_id == generation_id:
                if not previous.items or all(item.status == SuggestionStatus.PENDING for item in previous.items):
                    return previous.model_copy(update={"reused": True})
                if previous.generation_id == generation_id:
                    raise TailoringServiceError("tailoring_generation_already_decided")
                break  # Never fall back to an older completed generation.
        generation_id = generation_id or f"gen_{uuid4().hex}"
        existing = tuple(
            item
            for item in self._all_suggestions(principal)
            if item.analysis_id == analysis.analysis_id
            and item.target_draft_id == draft.draft_id
            and item.target_draft_revision == draft.revision
            and item.change_type == "ai_tailor_v1"
            and item.status == SuggestionStatus.PENDING
            and (not block_ids or item.block_id in block_ids)
        )
        if existing and command.generation_id is None:
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
                        block_id=block.block_id,
                    )
                )
                evidence_by_id[evidence_id] = reference
                aligned_by_id[evidence_id] = block
                used_blocks[block.block_id] = block
        if not request_evidence:
            result = TailoringResult(draft_id=draft.draft_id, draft_revision=draft.revision,
                generation_id=generation_id, block_ids=block_ids, outcome="insufficient_evidence",
                attempts=0, rejected_reasons=("tailoring_no_verified_evidence",),
                reflection=ReflectionResult(notes="请补充与岗位要求相关的真实资料后重新分析"))
            self._record_result(result, analysis_id=analysis.analysis_id, principal=principal,
                                input_block_count=0, input_evidence_count=0)
            return result

        if block_ids:
            if not set(block_ids) <= used_blocks.keys():
                raise TailoringServiceError("tailoring_block_not_eligible")
            used_blocks = {key: value for key, value in used_blocks.items() if key in block_ids}
            request_evidence = [item for item in request_evidence if item.block_id in block_ids]
            requested_evidence_ids = {item.evidence_id for item in request_evidence}
            evidence_by_id = {key: value for key, value in evidence_by_id.items() if key in requested_evidence_ids}
            aligned_by_id = {key: value for key, value in aligned_by_id.items() if key in requested_evidence_ids}
            requirements = tuple(item for item in requirements if item["requirement_id"] in
                                 {reference.requirement_id for reference in request_evidence})
        editable_by_id = {}
        for identifier, block in used_blocks.items():
            editable = block.text
            if len(editable) > 1000:
                quotes = [item.quote for item in request_evidence if item.block_id == identifier
                          and item.quote and block.text.count(item.quote) == 1]
                if not quotes:
                    raise TailoringServiceError("tailoring_edit_scope_ambiguous")
                editable = max(quotes, key=len)
            editable_by_id[identifier] = editable
        request_evidence = [item for item in request_evidence
                            if item.quote in editable_by_id[item.block_id]]
        allowed_evidence_ids = {item.evidence_id for item in request_evidence}
        evidence_by_id = {key: value for key, value in evidence_by_id.items() if key in allowed_evidence_ids}
        base_request = TailoringGenerationRequest(
            analysis_id=analysis.analysis_id,
            draft_id=draft.draft_id,
            draft_revision=draft.revision,
            requirements=requirements,
            resume_context=tuple(TailoringBlock(block_id=item.block_id, original_text=item.text)
                                 for item in blocks),
            target_requirements=tuple({"requirement_id": item.requirement_id,
                                       "original_text": item.original_text,
                                       "verdict": item.verdict.value}
                                      for item in analysis.requirements),
            blocks=tuple(
                TailoringBlock(
                    block_id=item.block_id,
                    original_text=editable_by_id[item.block_id],
                )
                for item in used_blocks.values()
            ),
            evidence=tuple(request_evidence),
        )
        positive_by_id = {item.requirement_id: item for item in positive}
        block_by_id = {item.block_id: item for item in used_blocks.values()}
        request_evidence_by_id = {
            item.evidence_id: item for item in request_evidence
        }
        created: tuple[Suggestion, ...] = ()
        rejected: list[str] = []
        feedback: tuple[str, ...] = ()
        reflection = ReflectionResult()
        attempts = 0
        diagnostics = []
        block_failures = {}
        unchanged_block_ids = []
        outcome = "generation_failed"
        for attempt in (1, 2):
            attempts = attempt
            generated = None
            try:
                generated = await self.generator.generate(
                    base_request.model_copy(update={"attempt": attempt, "validation_feedback": feedback})
                )
            except TailoringServiceError as error:
                if error.code != "tailoring_output_invalid":
                    raise
                attempt_rejected = (error.code,)
            else:
                latest_draft = self.store.get(ResumeDraft, draft.draft_id, principal=principal)
                if latest_draft.revision != draft.revision or latest_draft.status != ResumeDraftStatus.ACTIVE:
                    raise TailoringServiceError("tailoring_draft_changed")
                reflection = generated.reflection
                block_failures = dict(generated.block_failures)
                unchanged_block_ids = []
                created, attempt_rejected = self._validate_and_create(
                    generated=generated, analysis=analysis, draft=draft,
                    workspace_id=command.workspace_id, principal=principal,
                    positive_by_id=positive_by_id, evidence_by_id=evidence_by_id,
                    aligned_by_id=aligned_by_id,
                    request_evidence_by_id=request_evidence_by_id, block_by_id=block_by_id,
                    generation_id=generation_id,
                    editable_by_id=editable_by_id,
                    block_failures=block_failures, unchanged_block_ids=unchanged_block_ids,
                )
                attempt_rejected = (*attempt_rejected, *dict.fromkeys(block_failures.values()))
                if not generated.candidates:
                    attempt_rejected = (*attempt_rejected, "empty_model_output")
            for reason in attempt_rejected:
                if reason not in rejected:
                    rejected.append(reason)
            diagnostics.append(TailoringAttemptDiagnostic(
                attempt=attempt,
                candidate_count=len(generated.candidates) if generated else 0,
                accepted_count=len(created), rejected_reasons=attempt_rejected,
            ))
            if created:
                outcome = "ready"
                break
            # Only a verified, identical candidate proves no change. Model prose or
            # an empty array cannot establish that the existing resume is suitable.
            if generated and generated.candidates and set(attempt_rejected) == {"unchanged_text"}:
                outcome = "no_change"
                break
            outcome = ("generation_failed" if set(attempt_rejected) <=
                       {"empty_model_output", "tailoring_output_invalid"} else "validation_failed")
            feedback = tuple(attempt_rejected) or ("empty_model_output",)

        # Supersede only blocks for which a valid replacement exists. Failed
        # section attempts must not discard other useful pending suggestions.
        replaced_blocks = {item.block_id for item in created}
        created_ids = {item.suggestion_id for item in created}
        for old in existing:
            if old.block_id in replaced_blocks and old.suggestion_id not in created_ids:
                old = self.store.get(Suggestion, old.suggestion_id, principal=principal)
                if old.status != SuggestionStatus.PENDING:
                    continue
                updated = old.model_copy(update={"status": SuggestionStatus.INVALIDATED,
                    "revision": old.revision + 1, "decided_at": None})
                self.store.update(updated, principal=principal, expected_revision=old.revision)
                self.store.append_event(old.suggestion_id, principal=principal,
                    event_type="suggestion_superseded", payload={"generation_id": generation_id},
                    occurred_at=datetime.now(UTC))
        result = TailoringResult(
            draft_id=draft.draft_id, draft_revision=draft.revision, items=created,
            reflection=reflection, rejected_reasons=tuple(rejected), attempts=attempts,
            outcome=outcome, generation_id=generation_id, block_ids=block_ids,
            diagnostics=tuple(diagnostics), block_failures=block_failures,
            unchanged_block_ids=tuple(unchanged_block_ids),
        )
        self._record_result(result, analysis_id=analysis.analysis_id, principal=principal,
                            input_block_count=len(base_request.blocks), input_evidence_count=len(base_request.evidence))
        return result

    def _record_result(self, result: TailoringResult, *, analysis_id: str, principal: str,
                       input_block_count: int, input_evidence_count: int) -> None:
        # Store IDs/counts only. Resume text and raw model output stay out of events.
        self.store.append_event(result.draft_id, principal=principal,
            event_type="tailoring_generation_completed",
            payload={
                "prompt_version": PROMPT_VERSION,
                "reflection": result.reflection.model_dump(mode="json"),
                "block_failures": result.block_failures,
                "unchanged_block_ids": list(result.unchanged_block_ids),
                "attempts": result.attempts, "created_count": len(result.items),
                "rejected_reasons": list(result.rejected_reasons), "outcome": result.outcome,
                "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
                "input_block_count": input_block_count, "input_evidence_count": input_evidence_count,
                "analysis_id": analysis_id, "draft_revision": result.draft_revision,
                "generation_id": result.generation_id, "block_ids": list(result.block_ids),
                "suggestion_ids": [item.suggestion_id for item in result.items],
            }, occurred_at=datetime.now(UTC))

    def _validate_and_create(
        self,
        *,
        generated: TailoringGenerationResult,
        analysis: MatchAnalysis,
        draft: ResumeDraft,
        workspace_id: str,
        principal: str,
        positive_by_id: dict[str, RequirementResult],
        evidence_by_id: dict[str, EvidenceReference],
        aligned_by_id: dict[str, _AlignedBlock],
        request_evidence_by_id: dict[str, TailoringEvidence],
        block_by_id: dict[str, _AlignedBlock],
        generation_id: str,
        editable_by_id: dict[str, str],
        block_failures: dict[str, str],
        unchanged_block_ids: list[str],
    ) -> tuple[tuple[Suggestion, ...], tuple[str, ...]]:
        created = []
        rejected = []
        candidate_blocks = set()
        for candidate in generated.candidates:
            reasons = []
            block = block_by_id.get(candidate.block_id)
            editable = editable_by_id.get(candidate.block_id, "")
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
            if any(
                item in request_evidence_by_id
                and request_evidence_by_id[item].requirement_id
                not in requirement_ids
                for item in candidate.evidence_ids
            ):
                reasons.append("evidence_requirement_mismatch")
            allowed_text = "\n".join(
                [editable]
                + [item.quote or "" for item in selected_evidence]
            )
            if self._numbers(candidate.proposed_text) - self._numbers(allowed_text):
                reasons.append("new_numeric_claim")
            if block is not None and self._clean(candidate.proposed_text) == self._clean(
                editable
            ):
                reasons.append("unchanged_text")
            if reasons:
                if set(reasons) == {"unchanged_text"}:
                    unchanged_block_ids.append(candidate.block_id)
                if candidate.block_id in block_by_id and set(reasons) != {"unchanged_text"}:
                    block_failures[candidate.block_id] = reasons[0]
                rejected.extend(reason for reason in reasons if reason not in rejected)
                continue
            digest = sha256(
                "\0".join(
                    (
                        PROMPT_VERSION,
                        generation_id,
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
                        proposed_text=block.text.replace(editable, candidate.proposed_text, 1),
                        change_type="ai_tailor_v1",
                        reason=candidate.reason,
                        resume_evidence=selected_evidence,
                        requirement_ids=requirement_ids,
                        risk=candidate.risk,
                        allow_partial_analysis=analysis.status == MatchStatus.PARTIAL,
                    ),
                    workspace_id=workspace_id,
                    principal=principal,
                )
            )
            candidate_blocks.add(candidate.block_id)
        for item in created:
            block_failures.pop(item.block_id, None)
        return tuple(created), tuple(rejected)

    def history(self, draft_id: str, *, analysis_id: str, principal: str) -> tuple[TailoringResult, ...]:
        draft = self.store.get(ResumeDraft, draft_id, principal=principal)
        analysis = self.store.get(MatchAnalysis, analysis_id, principal=principal)
        self.store.assert_entity_in_workspace(draft_id, analysis.workspace_id, principal=principal)
        if draft.base_version_id != analysis.resume_version_id:
            raise TailoringServiceError("tailoring_draft_base_mismatch")
        values = []
        for event in reversed(self.store.list_events(draft_id, principal=principal)):
            data = event.payload
            if (event.event_type != "tailoring_generation_completed"
                    or data.get("analysis_id") != analysis_id
                    or data.get("draft_revision") != draft.revision):
                continue
            suggestions = tuple(self.store.get(Suggestion, identifier, principal=principal)
                                for identifier in data.get("suggestion_ids", []))
            values.append(TailoringResult(
                draft_id=draft_id, draft_revision=draft.revision,
                generation_id=data.get("generation_id"), block_ids=tuple(data.get("block_ids", [])), items=suggestions,
                reflection=ReflectionResult.model_validate(data.get("reflection", {"notes": "已恢复保存的生成结果"})),
                block_failures=data.get("block_failures", {}),
                unchanged_block_ids=tuple(data.get("unchanged_block_ids", [])),
                reused=True, outcome=data["outcome"], attempts=data["attempts"],
                rejected_reasons=tuple(data.get("rejected_reasons", [])),
                diagnostics=tuple(TailoringAttemptDiagnostic.model_validate(item)
                                  for item in data.get("diagnostics", [])),
            ))
        return tuple(values)

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
