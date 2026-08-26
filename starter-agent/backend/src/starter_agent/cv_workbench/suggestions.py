"""Evidence-bound suggestion candidates and explicit Draft approval workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from starter_agent.cv_workbench.contracts import (
    EvidenceReference,
    MatchAnalysis,
    MatchStatus,
    RequirementVerdict,
    ResumeDraft,
    ResumeDraftStatus,
    Suggestion,
    SuggestionStatus,
)
from starter_agent.cv_workbench.resume_import import ResumeMarkdownNormalizer
from starter_agent.cv_workbench.store import SQLiteWorkbenchStore
from starter_agent.cv_workbench.versioning import ResumeVersionService


class SuggestionServiceError(RuntimeError):
    code = "suggestion_service_error"


@dataclass(frozen=True)
class SuggestionCommand:
    suggestion_id: str
    analysis_id: str
    target_version_id: str
    target_draft_id: str
    target_draft_revision: int
    block_id: str
    original_text: str
    proposed_text: str
    change_type: str
    reason: str
    resume_evidence: tuple[EvidenceReference, ...]
    requirement_ids: tuple[str, ...]
    risk: str | None = None
    allow_partial_analysis: bool = False


@dataclass(frozen=True)
class BatchApplyResult:
    draft: ResumeDraft
    accepted_ids: tuple[str, ...]
    invalidated_ids: tuple[str, ...]


@dataclass(frozen=True)
class _SuggestionBlock:
    block_id: str
    start_line: int
    end_line: int
    content_sha256: str
    text: str


class SuggestionService:
    def __init__(
        self,
        *,
        store: SQLiteWorkbenchStore,
        versions: ResumeVersionService,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.versions = versions
        self.clock = clock
        self.normalizer = ResumeMarkdownNormalizer()

    def create(
        self,
        command: SuggestionCommand,
        *,
        workspace_id: str,
        principal: str,
    ) -> Suggestion:
        analysis = self.store.get(
            MatchAnalysis, command.analysis_id, principal=principal
        )
        if analysis.status == MatchStatus.PARTIAL and not command.allow_partial_analysis:
            raise SuggestionServiceError("partial_analysis_requires_explicit_acceptance")
        if analysis.status not in {MatchStatus.VALIDATED, MatchStatus.PARTIAL}:
            raise SuggestionServiceError("analysis_not_eligible_for_suggestions")
        if analysis.resume_version_id != command.target_version_id:
            raise SuggestionServiceError("suggestion_target_version_mismatch")
        draft = self.store.get(
            ResumeDraft, command.target_draft_id, principal=principal
        )
        self.store.assert_entity_in_workspace(
            draft.draft_id, workspace_id, principal=principal
        )
        if draft.status != ResumeDraftStatus.ACTIVE:
            raise SuggestionServiceError("suggestion_target_draft_not_active")
        if draft.revision != command.target_draft_revision:
            raise SuggestionServiceError("suggestion_target_revision_stale")
        positive = {
            item.requirement_id: item
            for item in analysis.requirements
            if item.verdict in {RequirementVerdict.MATCHED, RequirementVerdict.PARTIAL}
        }
        if not command.requirement_ids or any(
            item not in positive for item in command.requirement_ids
        ):
            raise SuggestionServiceError("suggestion_requires_positive_job_requirements")
        allowed_evidence = {
            (ref.source_ref, ref.content_sha256)
            for requirement_id in command.requirement_ids
            for ref in positive[requirement_id].evidence
        }
        if not command.resume_evidence or any(
            (ref.source_ref, ref.content_sha256) not in allowed_evidence
            for ref in command.resume_evidence
        ):
            raise SuggestionServiceError("suggestion_resume_evidence_not_in_analysis")
        block = self._block(draft, command.block_id, workspace_id, principal)
        if self._clean(command.original_text) != self._clean(block.text):
            raise SuggestionServiceError("suggestion_original_text_mismatch")
        now = self.clock()
        suggestion = Suggestion(
            suggestion_id=command.suggestion_id,
            analysis_id=analysis.analysis_id,
            target_version_id=command.target_version_id,
            target_draft_id=draft.draft_id,
            target_draft_revision=draft.revision,
            block_id=command.block_id,
            original_text=block.text,
            proposed_text=command.proposed_text,
            change_type=command.change_type,
            reason=command.reason,
            resume_evidence=command.resume_evidence,
            requirement_ids=command.requirement_ids,
            risk=command.risk,
            status=SuggestionStatus.PENDING,
            revision=1,
            created_at=now,
            decided_at=None,
        )
        stored = self.store.create(
            suggestion, principal=principal, workspace_id=workspace_id
        )
        self._event(stored, principal, "suggestion_created", {})
        return stored

    def generate_safe_candidates(
        self,
        analysis_id: str,
        draft_id: str,
        *,
        workspace_id: str,
        principal: str,
    ) -> tuple[Suggestion, ...]:
        """Generate conservative de-cliché candidates from already cited resume text."""
        analysis = self.store.get(MatchAnalysis, analysis_id, principal=principal)
        draft = self.store.get(ResumeDraft, draft_id, principal=principal)
        self.store.assert_entity_in_workspace(draft_id, workspace_id, principal=principal)
        if draft.base_version_id != analysis.resume_version_id:
            raise SuggestionServiceError("suggestion_draft_base_mismatch")
        existing = tuple(
            item
            for item in self._all_suggestions(principal)
            if item.analysis_id == analysis_id
            and item.target_draft_id == draft_id
            and item.target_draft_revision == draft.revision
        )
        if existing:
            return existing
        markdown = self.versions.content.read(
            draft.content, principal=principal, workspace_id=workspace_id
        )
        normalized = self.normalizer.normalize(markdown)
        blocks = tuple(
            _SuggestionBlock(
                item.block_id,
                item.start_line,
                item.end_line,
                item.content_sha256,
                self._projection_text(normalized.markdown, item),
            )
            for item in normalized.blocks
        )
        created: list[Suggestion] = []
        used_blocks: set[str] = set()
        for requirement in analysis.requirements:
            if requirement.verdict not in {RequirementVerdict.MATCHED, RequirementVerdict.PARTIAL}:
                continue
            for evidence in requirement.evidence:
                quote = self._clean(evidence.quote or "")
                block = next(
                    (
                        item
                        for item in blocks
                        if item.block_id not in used_blocks
                        and quote
                        and quote in self._clean(item.text)
                    ),
                    None,
                )
                if block is None:
                    continue
                proposed = block.text
                change_type = None
                for weak, strong in (("负责", "实现"), ("参与", "协作完成"), ("Responsible for", "Delivered"), ("Participated in", "Contributed to")):
                    if weak in proposed:
                        proposed = proposed.replace(weak, strong, 1)
                        change_type = "remove_cliche"
                        break
                if change_type is None or self._clean(proposed) == self._clean(block.text):
                    continue
                digest = sha256(
                    f"{analysis_id}\0{draft_id}\0{draft.revision}\0{block.block_id}\0{requirement.requirement_id}".encode()
                ).hexdigest()[:24]
                created.append(
                    self.create(
                        SuggestionCommand(
                            suggestion_id=f"sg_{digest}",
                            analysis_id=analysis_id,
                            target_version_id=analysis.resume_version_id,
                            target_draft_id=draft_id,
                            target_draft_revision=draft.revision,
                            block_id=block.block_id,
                            original_text=block.text,
                            proposed_text=proposed,
                            change_type=change_type,
                            reason=f"去除套话，并保留要求“{requirement.original_text[:120]}”对应的已验证事实。",
                            resume_evidence=(evidence,),
                            requirement_ids=(requirement.requirement_id,),
                            risk="仅替换弱动词；请人工确认语气和职责边界。",
                        ),
                        workspace_id=workspace_id,
                        principal=principal,
                    )
                )
                used_blocks.add(block.block_id)
                break
        return tuple(created)

    def _all_suggestions(self, principal: str) -> tuple[Suggestion, ...]:
        values: list[Suggestion] = []
        cursor = None
        while True:
            page = self.store.list(Suggestion, principal=principal, cursor=cursor)
            values.extend(page.items)
            if page.next_cursor is None:
                return tuple(values)
            cursor = page.next_cursor

    def accept(
        self,
        suggestion_id: str,
        *,
        workspace_id: str,
        principal: str,
        edited_text: str | None = None,
    ) -> ResumeDraft:
        suggestion = self._pending(suggestion_id, principal)
        draft = self.store.get(
            ResumeDraft, suggestion.target_draft_id, principal=principal
        )
        if draft.revision != suggestion.target_draft_revision:
            self._invalidate(suggestion, principal, "draft_revision_changed")
            raise SuggestionServiceError("suggestion_stale")
        block = self._block(draft, suggestion.block_id, workspace_id, principal)
        if self._clean(block.text) != self._clean(suggestion.original_text):
            self._invalidate(suggestion, principal, "target_block_changed")
            raise SuggestionServiceError("suggestion_stale")
        replacement = edited_text if edited_text is not None else suggestion.proposed_text
        updated = self.versions.apply_patch(
            draft.draft_id,
            patch=self._patch(block, replacement),
            workspace_id=workspace_id,
            principal=principal,
            expected_revision=draft.revision,
            expected_content_sha256=draft.content.content_sha256,
        )
        self._decide(suggestion, SuggestionStatus.ACCEPTED, principal, edited_text)
        self._invalidate_pending_for_revision(
            draft.draft_id,
            draft.revision,
            principal=principal,
            exclude={suggestion.suggestion_id},
        )
        return updated

    def reject(self, suggestion_id: str, *, principal: str) -> Suggestion:
        suggestion = self._pending(suggestion_id, principal)
        return self._decide(suggestion, SuggestionStatus.REJECTED, principal, None)

    def apply_batch(
        self,
        suggestion_ids: tuple[str, ...],
        *,
        workspace_id: str,
        principal: str,
    ) -> BatchApplyResult:
        if not suggestion_ids or len(set(suggestion_ids)) != len(suggestion_ids):
            raise SuggestionServiceError("invalid_suggestion_batch")
        suggestions = tuple(self._pending(item, principal) for item in suggestion_ids)
        target = suggestions[0]
        if any(
            item.target_draft_id != target.target_draft_id
            or item.target_draft_revision != target.target_draft_revision
            for item in suggestions
        ):
            raise SuggestionServiceError("suggestion_batch_target_mismatch")
        if len({item.block_id for item in suggestions}) != len(suggestions):
            raise SuggestionServiceError("suggestion_batch_duplicate_block")
        draft = self.store.get(ResumeDraft, target.target_draft_id, principal=principal)
        if draft.revision != target.target_draft_revision:
            for item in suggestions:
                self._invalidate(item, principal, "draft_revision_changed")
            raise SuggestionServiceError("suggestion_stale")
        markdown = self.versions.content.read(
            draft.content, principal=principal, workspace_id=workspace_id
        )
        normalized = self.normalizer.normalize(markdown)
        blocks = {
            item.block_id: _SuggestionBlock(
                item.block_id,
                item.start_line,
                item.end_line,
                item.content_sha256,
                self._projection_text(normalized.markdown, item),
            )
            for item in normalized.blocks
        }
        lines = normalized.markdown.rstrip("\n").splitlines()
        replacements = []
        for item in suggestions:
            block = blocks.get(item.block_id)
            if block is None or self._clean(block.text) != self._clean(item.original_text):
                for candidate in suggestions:
                    self._invalidate(candidate, principal, "target_block_changed")
                raise SuggestionServiceError("suggestion_stale")
            replacements.append((block.start_line, block.end_line, item.proposed_text))
        for start, end, replacement in sorted(replacements, reverse=True):
            lines[start - 1 : end] = replacement.strip("\n").splitlines()
        updated = self.versions.autosave(
            draft.draft_id,
            "\n".join(lines) + "\n",
            workspace_id=workspace_id,
            principal=principal,
            expected_revision=draft.revision,
            expected_content_sha256=draft.content.content_sha256,
        )
        for item in suggestions:
            self._decide(item, SuggestionStatus.ACCEPTED, principal, None)
        invalidated = self._invalidate_pending_for_revision(
            draft.draft_id,
            draft.revision,
            principal=principal,
            exclude=set(suggestion_ids),
        )
        return BatchApplyResult(updated, suggestion_ids, invalidated)

    def refresh(self, suggestion_id: str, *, principal: str) -> Suggestion:
        suggestion = self.store.get(Suggestion, suggestion_id, principal=principal)
        if suggestion.status != SuggestionStatus.PENDING:
            return suggestion
        analysis = self.store.get(
            MatchAnalysis, suggestion.analysis_id, principal=principal
        )
        draft = self.store.get(
            ResumeDraft, suggestion.target_draft_id, principal=principal
        )
        if analysis.status == MatchStatus.STALE:
            return self._invalidate(suggestion, principal, "analysis_stale")
        if draft.revision != suggestion.target_draft_revision:
            return self._invalidate(suggestion, principal, "draft_revision_changed")
        return suggestion

    def _pending(self, suggestion_id: str, principal: str) -> Suggestion:
        suggestion = self.store.get(Suggestion, suggestion_id, principal=principal)
        if suggestion.status != SuggestionStatus.PENDING:
            raise SuggestionServiceError("suggestion_not_pending")
        return suggestion

    def _block(self, draft, block_id, workspace_id, principal):
        markdown = self.versions.content.read(
            draft.content, principal=principal, workspace_id=workspace_id
        )
        normalized = self.normalizer.normalize(markdown)
        block = next((item for item in normalized.blocks if item.block_id == block_id), None)
        if block is None:
            raise SuggestionServiceError("suggestion_block_not_found")
        return _SuggestionBlock(
            block.block_id,
            block.start_line,
            block.end_line,
            block.content_sha256,
            self._projection_text(normalized.markdown, block),
        )

    @staticmethod
    def _patch(block, replacement):
        from starter_agent.cv_workbench.versioning import BlockPatch

        return BlockPatch(block.block_id, block.content_sha256, replacement)

    def _decide(self, suggestion, status, principal, edited_text):
        updated = suggestion.model_copy(
            update={
                "status": status,
                "revision": suggestion.revision + 1,
                "decided_at": self.clock(),
            }
        )
        stored = self.store.update(
            updated, principal=principal, expected_revision=suggestion.revision
        )
        payload = {"status": status.value}
        if edited_text is not None:
            payload["applied_text_sha256"] = sha256(edited_text.encode()).hexdigest()
        self._event(stored, principal, "suggestion_decided", payload)
        return stored

    def _invalidate(self, suggestion, principal, reason):
        if suggestion.status != SuggestionStatus.PENDING:
            return suggestion
        updated = suggestion.model_copy(
            update={
                "status": SuggestionStatus.INVALIDATED,
                "revision": suggestion.revision + 1,
                "decided_at": None,
            }
        )
        stored = self.store.update(
            updated, principal=principal, expected_revision=suggestion.revision
        )
        self._event(stored, principal, "suggestion_invalidated", {"reason": reason})
        return stored

    def _invalidate_pending_for_revision(self, draft_id, revision, *, principal, exclude):
        invalidated = []
        cursor = None
        while True:
            page = self.store.list(Suggestion, principal=principal, cursor=cursor)
            for item in page.items:
                if (
                    item.suggestion_id not in exclude
                    and item.target_draft_id == draft_id
                    and item.target_draft_revision == revision
                    and item.status == SuggestionStatus.PENDING
                ):
                    self._invalidate(item, principal, "draft_revision_changed")
                    invalidated.append(item.suggestion_id)
            if page.next_cursor is None:
                return tuple(invalidated)
            cursor = page.next_cursor

    def _event(self, suggestion, principal, event_type, payload):
        self.store.append_event(
            suggestion.suggestion_id,
            principal=principal,
            event_type=event_type,
            payload=payload,
            occurred_at=self.clock(),
        )

    @staticmethod
    def _clean(value: str) -> str:
        return "\n".join(line.rstrip() for line in value.strip().splitlines())

    @staticmethod
    def _projection_text(markdown: str, block) -> str:
        lines = markdown.rstrip("\n").splitlines()
        return "\n".join(lines[block.start_line - 1 : block.end_line])
