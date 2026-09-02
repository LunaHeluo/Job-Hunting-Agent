"""Evidence-validated, deterministic resume/JD match analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import re
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from starter_agent.cv_workbench.bindings import (
    BindEvidenceCommand,
    EvidenceBindingService,
    EvidenceSourceReader,
)
from starter_agent.cv_workbench.contracts import (
    EvidenceReference,
    JobSnapshot,
    MatchAnalysis,
    MatchStatus,
    RequirementResult,
    RequirementVerdict,
    ResumeVersion,
    ResumeVersionStatus,
    ScoreDimension,
)
from starter_agent.cv_workbench.operations import (
    BusinessOperationService,
    CommitReceipt,
    OperationCommand,
    RunBinding,
    RunOutcome,
    SafetyDecision,
    ValidationDecision,
)
from starter_agent.cv_workbench.store import ObjectNotFoundError, SQLiteWorkbenchStore
from starter_agent.cv_workbench.tailoring_evidence import ResumeEvidenceSelector
from starter_agent.knowledge.models import KnowledgeScope


RULE_VERSION = "match-rule.v2"
VALIDATOR_VERSION = "match-result-validator.v2"
CATEGORY_WEIGHTS = {
    "required": Decimal("0.60"),
    "responsibility": Decimal("0.25"),
    "preferred": Decimal("0.15"),
}
VERDICT_FACTORS = {
    RequirementVerdict.MATCHED: Decimal("1"),
    RequirementVerdict.PARTIAL: Decimal("0.5"),
    RequirementVerdict.MISSING: Decimal("0"),
    RequirementVerdict.CONFLICT: Decimal("0"),
}


class MatchServiceError(RuntimeError):
    code = "match_service_error"


def deterministic_requirements(
    resume_text: str,
    job_text: str,
    *,
    selector: ResumeEvidenceSelector,
    scope: KnowledgeScope,
    knowledge_base_id: UUID,
    document_id: UUID,
) -> tuple[CandidateRequirement, ...]:
    """Build requirements whose positive verdicts have scoped chunk evidence."""
    output: list[CandidateRequirement] = []
    seen: set[str] = set()
    for raw in job_text.splitlines():
        text = re.sub(r"^[\s#>*+\-\d.)、]+", "", raw).strip()
        if len(text) < 4 or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        folded = text.casefold()
        category = (
            "preferred"
            if any(word in folded for word in ("preferred", "plus", "优先", "加分"))
            else "responsibility"
            if any(word in folded for word in ("responsib", "负责", "职责"))
            else "required"
        )
        selection = selector.select(
            text,
            scope=scope,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            markdown=resume_text,
        )
        covered_count = sum(
            term in selection.covered_terms for term in selection.query_terms
        )
        coverage = covered_count / max(1, len(selection.query_terms))
        if not selection.evidence:
            verdict = RequirementVerdict.MISSING
            refs: tuple[EvidenceReference, ...] = ()
            explanation = (
                "未在当前简历的已授权证据中找到可验证内容；"
                "该缺口不会自动写入简历。"
            )
        else:
            verdict = (
                RequirementVerdict.MATCHED
                if coverage >= 0.60
                else RequirementVerdict.PARTIAL
            )
            refs = selection.evidence
            explanation = (
                f"当前简历的 {len(refs)} 条证据覆盖核心词 "
                f"{covered_count}/{len(selection.query_terms)}。"
            )
        output.append(
            CandidateRequirement(
                original_text=text[:5000],
                category=category,
                importance=3 if category == "required" else 2,
                verdict=verdict,
                evidence=refs,
                explanation=explanation,
            )
        )
        if len(output) >= 30:
            break
    if not output:
        raise MatchServiceError("job_requirements_not_detected")
    return tuple(output)


class CandidateRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    original_text: str = Field(min_length=1, max_length=5000)
    category: str = Field(pattern="^(responsibility|required|preferred)$")
    importance: int = Field(ge=1, le=5)
    verdict: RequirementVerdict
    evidence: tuple[EvidenceReference, ...] = ()
    explanation: str = Field(min_length=1, max_length=5000)


class MatchCandidateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    analysis_id: str
    workspace_id: str
    resume_version_id: str
    resume_content_sha256: str
    job_snapshot_id: str
    job_content_sha256: str
    complete: bool
    requirements: tuple[CandidateRequirement, ...]
    parent_run_id: str | None = None
    child_run_ids: tuple[str, ...] = ()

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def content_sha256(self) -> str:
        return sha256(self.canonical_json.encode()).hexdigest()


@dataclass(frozen=True)
class StoredMatchCandidate:
    artifact_ref: str
    content_sha256: str


class MatchCandidateRepository(Protocol):
    def write(
        self, candidate: MatchCandidateEnvelope, *, principal: str
    ) -> StoredMatchCandidate: ...

    def read(self, artifact_ref: str, *, principal: str) -> MatchCandidateEnvelope: ...


@dataclass(frozen=True)
class AnalyzeCommand:
    analysis_id: str
    operation_id: str
    idempotency_key: str
    workspace_id: str
    resume_version_id: str
    job_snapshot_id: str
    requirements: tuple[CandidateRequirement, ...]
    complete: bool = True
    parent_run_id: str | None = None
    child_run_ids: tuple[str, ...] = ()


class MatchService:
    def __init__(
        self,
        *,
        store: SQLiteWorkbenchStore,
        candidates: MatchCandidateRepository,
        evidence_reader: EvidenceSourceReader,
        evidence_bindings: EvidenceBindingService,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.candidates = candidates
        self.evidence_reader = evidence_reader
        self.evidence_bindings = evidence_bindings
        self.clock = clock

    def analyze(self, command: AnalyzeCommand, *, principal: str) -> MatchAnalysis:
        resume = self.store.get(
            ResumeVersion, command.resume_version_id, principal=principal
        )
        job = self.store.get(JobSnapshot, command.job_snapshot_id, principal=principal)
        self.store.assert_entity_in_workspace(
            resume.version_id, command.workspace_id, principal=principal
        )
        self.store.assert_entity_in_workspace(
            job.snapshot_id, command.workspace_id, principal=principal
        )
        if resume.status != ResumeVersionStatus.CONFIRMED:
            raise MatchServiceError("analysis_requires_confirmed_resume_version")
        candidate = MatchCandidateEnvelope(
            analysis_id=command.analysis_id,
            workspace_id=command.workspace_id,
            resume_version_id=resume.version_id,
            resume_content_sha256=resume.content.content_sha256,
            job_snapshot_id=job.snapshot_id,
            job_content_sha256=job.content.content_sha256,
            complete=command.complete,
            requirements=command.requirements,
            parent_run_id=command.parent_run_id,
            child_run_ids=command.child_run_ids,
        )
        if not candidate.requirements:
            raise MatchServiceError("analysis_requirements_empty")
        operation_service = self._operations(principal)
        operation, _ = operation_service.create(
            OperationCommand(
                operation_id=command.operation_id,
                workspace_id=command.workspace_id,
                operation_type="create_match_analysis",
                idempotency_key=command.idempotency_key,
                input_sha256=self._input_hash(candidate),
            ),
            principal=principal,
        )
        if operation.status == "committed" and operation.result_object_id is not None:
            return self.store.get(
                MatchAnalysis, operation.result_object_id, principal=principal
            )
        stored = self.candidates.write(candidate, principal=principal)
        if stored.content_sha256 != candidate.content_sha256:
            raise MatchServiceError("analysis_candidate_hash_mismatch")
        if operation.status == "commit_failed":
            operation = operation_service.retry_commit(
                command.operation_id, principal=principal
            )
        elif operation.status != "committed":
            run_id = (
                operation.parent_run_id
                or command.parent_run_id
                or f"local-match:{command.operation_id}"
            )
            if operation.status == "created":
                operation_service.bind_run(
                    command.operation_id,
                    RunBinding(parent_run_id=run_id),
                    principal=principal,
                )
            operation = operation_service.process_run_outcome(
                command.operation_id,
                RunOutcome(
                    parent_run_id=run_id,
                    status="succeeded" if command.complete else "partial",
                    result_ref=stored.artifact_ref,
                    result_sha256=stored.content_sha256,
                ),
                principal=principal,
            )
        if operation.status != "committed" or operation.result_object_id is None:
            raise MatchServiceError(operation.error_code or "analysis_commit_failed")
        return self.store.get(
            MatchAnalysis, operation.result_object_id, principal=principal
        )

    def refresh_staleness(
        self,
        analysis_id: str,
        *,
        current_resume_version_id: str,
        current_job_snapshot_id: str,
        principal: str,
    ) -> MatchAnalysis:
        analysis = self.store.get(MatchAnalysis, analysis_id, principal=principal)
        reasons = []
        if analysis.resume_version_id != current_resume_version_id:
            reasons.append("resume_version_changed")
        if analysis.job_snapshot_id != current_job_snapshot_id:
            reasons.append("job_snapshot_changed")
        if not reasons or analysis.status == MatchStatus.STALE:
            return analysis
        stale = analysis.model_copy(
            update={
                "status": MatchStatus.STALE,
                "stale_reason": ",".join(reasons),
                "allowed_actions": ("explain", "reanalyze"),
                "revision": analysis.revision + 1,
            }
        )
        return self.store.update(
            stale, principal=principal, expected_revision=analysis.revision
        )

    def _operations(self, principal: str) -> BusinessOperationService:
        validator = _MatchValidator(self, principal=principal)
        return BusinessOperationService(
            store=self.store,
            validator=validator,
            safety_gate=_MatchSafetyGate(),
            committer=_MatchCommitter(self, principal=principal),
            clock=self.clock,
        )

    def _validated_requirements(
        self, candidate: MatchCandidateEnvelope, *, principal: str
    ) -> tuple[RequirementResult, ...]:
        results = []
        seen: set[str] = set()
        now = self.clock()
        for item in candidate.requirements:
            requirement_id = self._requirement_id(item.category, item.original_text)
            if requirement_id in seen:
                raise MatchServiceError("duplicate_normalized_requirement")
            seen.add(requirement_id)
            evidence = []
            for ref in item.evidence:
                snapshot = self.evidence_reader.inspect(
                    ref.source_ref,
                    principal=principal,
                    workspace_id=candidate.workspace_id,
                    now=now,
                )
                if (
                    snapshot.exists
                    and snapshot.authorized
                    and not snapshot.expired
                    and snapshot.content_sha256 == ref.content_sha256
                ):
                    evidence.append(ref)
            verdict = item.verdict
            explanation = item.explanation
            if verdict in {RequirementVerdict.MATCHED, RequirementVerdict.PARTIAL} and not evidence:
                verdict = RequirementVerdict.MISSING
                explanation = "授权简历证据不足，按 missing 处理。"
            if verdict in {RequirementVerdict.MISSING, RequirementVerdict.CONFLICT}:
                evidence = []
            results.append(
                RequirementResult(
                    requirement_id=requirement_id,
                    original_text=" ".join(item.original_text.split()),
                    category=item.category,
                    importance=item.importance,
                    verdict=verdict,
                    evidence=tuple(evidence),
                    explanation=explanation,
                )
            )
        return tuple(results)

    @staticmethod
    def _score(requirements: tuple[RequirementResult, ...]):
        present = tuple(
            category
            for category in ("required", "responsibility", "preferred")
            if any(item.category == category for item in requirements)
        )
        weight_total = sum(CATEGORY_WEIGHTS[item] for item in present)
        dimensions = []
        total = Decimal("0")
        for category in present:
            items = tuple(item for item in requirements if item.category == category)
            importance = sum(Decimal(item.importance) for item in items)
            points = sum(
                Decimal(item.importance) * VERDICT_FACTORS[item.verdict]
                for item in items
            )
            score = Decimal("100") * points / importance
            weight = CATEGORY_WEIGHTS[category] / weight_total
            score_value = float(score.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))
            dimensions.append(
                ScoreDimension(name=f"{category}_requirements", weight=float(weight), score=score_value)
            )
            total += weight * score
        return (
            float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            tuple(dimensions),
        )

    @staticmethod
    def _requirement_id(category: str, text: str) -> str:
        normalized = " ".join(text.casefold().split())
        digest = sha256(f"{category}\0{normalized}".encode()).hexdigest()[:20]
        return f"req_{digest}"

    @staticmethod
    def _input_hash(candidate: MatchCandidateEnvelope) -> str:
        value = f"{RULE_VERSION}\0{candidate.content_sha256}"
        return sha256(value.encode()).hexdigest()


class _MatchValidator:
    def __init__(self, service: MatchService, *, principal: str) -> None:
        self.service = service
        self.principal = principal

    def validate(self, operation, outcome):
        try:
            candidate = self.service.candidates.read(
                str(outcome.result_ref), principal=self.principal
            )
            requirements = self.service._validated_requirements(
                candidate, principal=self.principal
            )
            accepted = (
                candidate.workspace_id == operation.workspace_id
                and outcome.result_sha256 == candidate.content_sha256
                and operation.input_sha256 == self.service._input_hash(candidate)
                and bool(requirements)
            )
        except Exception:
            candidate, requirements, accepted = None, (), False
        return ValidationDecision(
            accepted=accepted,
            validator_version=VALIDATOR_VERSION,
            result_ref=outcome.result_ref,
            result_sha256=outcome.result_sha256,
            evidence_refs=tuple(
                ref.source_ref for item in requirements for ref in item.evidence
            ),
            partial=bool(candidate is not None and not candidate.complete),
            error_code=None if accepted else "match_candidate_validation_failed",
        )


class _MatchSafetyGate:
    def evaluate(self, operation, decision):
        return SafetyDecision(
            allowed=decision.accepted,
            summary={"evidence_count": len(decision.evidence_refs), "deterministic_score": True},
            error_code=None if decision.accepted else "match_evidence_rejected",
        )


class _MatchCommitter:
    def __init__(self, service: MatchService, *, principal: str) -> None:
        self.service = service
        self.principal = principal

    def commit(self, operation, checkpoint):
        candidate = self.service.candidates.read(
            checkpoint.result_ref, principal=self.principal
        )
        requirements = self.service._validated_requirements(
            candidate, principal=self.principal
        )
        score, dimensions = self.service._score(requirements)
        status = (
            MatchStatus.PARTIAL
            if checkpoint.partial
            or any(item.verdict == RequirementVerdict.CONFLICT for item in requirements)
            else MatchStatus.VALIDATED
        )
        analysis = MatchAnalysis(
            analysis_id=candidate.analysis_id,
            workspace_id=candidate.workspace_id,
            resume_version_id=candidate.resume_version_id,
            resume_content_sha256=candidate.resume_content_sha256,
            job_snapshot_id=candidate.job_snapshot_id,
            job_content_sha256=candidate.job_content_sha256,
            status=status,
            rule_version=RULE_VERSION,
            validator_version=VALIDATOR_VERSION,
            total_score=score,
            dimensions=dimensions,
            requirements=requirements,
            parent_run_id=candidate.parent_run_id,
            child_run_ids=candidate.child_run_ids,
            revision=1,
            created_at=self.service.clock(),
            stale_reason=None,
            allowed_actions=(
                ("explain", "generate_suggestions", "reanalyze")
                if status == MatchStatus.VALIDATED
                else ("explain", "reanalyze")
            ),
        )
        try:
            existing = self.service.store.get(
                MatchAnalysis, analysis.analysis_id, principal=self.principal
            )
            identity = (
                existing.workspace_id,
                existing.resume_version_id,
                existing.resume_content_sha256,
                existing.job_snapshot_id,
                existing.job_content_sha256,
                existing.rule_version,
                existing.validator_version,
                existing.total_score,
                existing.dimensions,
                existing.requirements,
            )
            proposed = (
                analysis.workspace_id,
                analysis.resume_version_id,
                analysis.resume_content_sha256,
                analysis.job_snapshot_id,
                analysis.job_content_sha256,
                analysis.rule_version,
                analysis.validator_version,
                analysis.total_score,
                analysis.dimensions,
                analysis.requirements,
            )
            if identity != proposed:
                raise MatchServiceError("analysis_id_conflict")
        except ObjectNotFoundError:
            self.service.store.create(analysis, principal=self.principal)
        for requirement in analysis.requirements:
            for evidence in requirement.evidence:
                self.service.evidence_bindings.bind(
                    BindEvidenceCommand(
                        workspace_id=analysis.workspace_id,
                        subject_id=analysis.analysis_id,
                        source_kind="chunk",
                        source_ref=evidence.source_ref,
                        expected_sha256=evidence.content_sha256,
                    ),
                    principal=self.principal,
                )
        return CommitReceipt(result_object_id=analysis.analysis_id)
