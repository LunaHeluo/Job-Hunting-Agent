from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from starter_agent.cv_workbench.contracts import (
    BusinessOperation,
    ContentReference,
    Job,
    JobSnapshot,
    MatchAnalysis,
    MergeDecisionType,
    MergeProposal,
    MergeProposalStatus,
    OperationStatus,
    Resume,
    ResumeBranch,
    ResumeBranchType,
    ResumeDraft,
    ResumeNodeType,
    ResumeVersion,
    ResumeVersionStatus,
    Suggestion,
    SuggestionStatus,
    Workspace,
)
from starter_agent.cv_workbench.merging import MergeStaleError, ResumeMergeService
from starter_agent.cv_workbench.store import ObjectNotFoundError, SQLiteWorkbenchStore
from starter_agent.cv_workbench.versioning import (
    BlockPatch,
    ResumeVersionService,
    StaleContentError,
)
from starter_agent.cv_workbench.suggestions import (
    SuggestionCommand,
    SuggestionService,
    SuggestionServiceError,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


@dataclass
class FakeContentRepository:
    values: dict[str, str] = field(default_factory=dict)

    def read(self, reference, *, principal, workspace_id):
        return self.values[reference.artifact_id]

    def write_draft(
        self,
        *,
        draft_id,
        revision,
        markdown,
        content_sha256,
        principal,
        workspace_id,
    ):
        ref = f"artifact:draft:{draft_id}:{revision}"
        self.values[ref] = markdown
        return ContentReference(content_sha256=content_sha256, artifact_id=ref)

    def publish_version(
        self,
        *,
        version_id,
        markdown,
        content_sha256,
        principal,
        workspace_id,
    ):
        ref = f"artifact:version:{version_id}"
        self.values[ref] = markdown
        return ContentReference(content_sha256=content_sha256, artifact_id=ref)


def setup_service(tmp_path: Path):
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'versioning.db').as_posix()}", tmp_path
    )
    payloads = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )
    workspace = payloads["workspace"]
    workspace["revision"] = 1
    store.create(Workspace.model_validate(workspace), principal=PRINCIPAL)
    resume_payload = payloads["resume"]
    resume_payload.update(latest_version_id="rv_master_v1", revision=1)
    resume = Resume.model_validate(resume_payload)
    # latest reference is attached after the root exists.
    resume = Resume.model_validate(resume.model_dump() | {"latest_version_id": None})
    store.create(resume, principal=PRINCIPAL)
    branch = ResumeBranch.model_validate(
        {
            "branch_id": "rb_master",
            "resume_id": "res_demo",
            "name": "master",
            "branch_type": "master",
            "base_version_id": "rv_master_v1",
            "job_snapshot_id": None,
            "archived": False,
            "revision": 1,
            "created_at": "2026-08-17T08:00:00+08:00",
            "updated_at": "2026-08-17T08:00:00+08:00",
            "allowed_actions": ("create_version",),
        }
    )
    store.create(branch, principal=PRINCIPAL)
    repository = FakeContentRepository(
        {"artifact:version:rv_master_v1": "# Summary\n\nPython API\n"}
    )
    root = ResumeVersion.model_validate(
        {
            "version_id": "rv_master_v1",
            "resume_id": "res_demo",
            "branch_id": "rb_master",
            "parent_version_id": None,
            "branch_base_version_id": "rv_master_v1",
            "node_type": ResumeNodeType.BASE,
            "version_number": 1,
            "label": "Master v1",
            "content": {
                "content_sha256": "7a428a5a0c83ee617323e04fa0cc4057fb081845ba801f0f114369062a218e41",
                "artifact_id": "artifact:version:rv_master_v1",
            },
            "status": ResumeVersionStatus.CONFIRMED,
            "job_snapshot_id": None,
            "upstream_changes_available": False,
            "revision": 1,
            "created_by": PRINCIPAL,
            "created_at": "2026-08-17T08:01:00+08:00",
            "confirmed_at": "2026-08-17T08:01:00+08:00",
            "allowed_actions": ("compare",),
        }
    )
    # Use the real digest of the normalized body.
    normalized = ResumeVersionService(store=store, content=repository).normalizer.normalize(
        repository.values[root.content.artifact_id]
    )
    root = ResumeVersion.model_validate(
        root.model_dump()
        | {"content": root.content.model_dump() | {"content_sha256": normalized.content_sha256}}
    )
    store.create(root, principal=PRINCIPAL)
    current = store.get(Resume, "res_demo", principal=PRINCIPAL)
    store.update(
        Resume.model_validate(
            current.model_dump()
            | {"latest_version_id": root.version_id, "revision": 2}
        ),
        principal=PRINCIPAL,
        expected_revision=1,
    )
    store.link_to_workspace("ws_demo", "res_demo", principal=PRINCIPAL)
    return ResumeVersionService(store=store, content=repository), store, repository


def setup_suggestion_service(tmp_path: Path):
    versions, store, repository = setup_service(tmp_path)
    payloads = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )
    job = Job.model_validate(payloads["job"])
    snapshot = JobSnapshot.model_validate(payloads["job_snapshot"])
    store.create(job, principal=PRINCIPAL)
    store.create(snapshot, principal=PRINCIPAL)
    store.link_to_workspace("ws_demo", job.job_id, principal=PRINCIPAL)
    analysis_payload = json.loads(
        (FIXTURES / "analysis-validated.json").read_text(encoding="utf-8")
    )
    root = store.get(ResumeVersion, "rv_master_v1", principal=PRINCIPAL)
    analysis_payload.update(
        analysis_id="ma_suggestions",
        resume_version_id=root.version_id,
        resume_content_sha256=root.content.content_sha256,
        job_snapshot_id=snapshot.snapshot_id,
        job_content_sha256=snapshot.content.content_sha256,
    )
    analysis = MatchAnalysis.model_validate(analysis_payload)
    store.create(analysis, principal=PRINCIPAL)
    versions.create_branch(
        branch_id="rb_suggestions",
        resume_id="res_demo",
        name="Suggestions",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id=root.version_id,
        principal=PRINCIPAL,
    )
    draft = versions.create_draft(
        draft_id="rd_suggestions",
        workspace_id="ws_demo",
        base_version_id=root.version_id,
        branch_id="rb_suggestions",
        principal=PRINCIPAL,
    )
    return SuggestionService(store=store, versions=versions), versions, store, repository, analysis, draft


def test_draft_patch_pending_confirmation_and_diff(tmp_path: Path) -> None:
    service, store, repository = setup_service(tmp_path)
    service.create_branch(
        branch_id="rb_backend",
        resume_id="res_demo",
        name="Backend",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id="rv_master_v1",
        principal=PRINCIPAL,
    )
    draft = service.create_draft(
        draft_id="rd_backend_1",
        workspace_id="ws_demo",
        base_version_id="rv_master_v1",
        branch_id="rb_backend",
        principal=PRINCIPAL,
    )
    normalized = service.normalizer.normalize(
        repository.read(draft.content, principal=PRINCIPAL, workspace_id="ws_demo")
    )
    paragraph = next(item for item in normalized.blocks if item.kind == "paragraph")
    patched = service.apply_patch(
        draft.draft_id,
        BlockPatch(
            block_id=paragraph.block_id,
            expected_sha256=paragraph.content_sha256,
            replacement="Python and FastAPI services",
        ),
        workspace_id="ws_demo",
        principal=PRINCIPAL,
        expected_revision=1,
        expected_content_sha256=draft.content.content_sha256,
    )

    with pytest.raises(StaleContentError):
        service.apply_patch(
            draft.draft_id,
            BlockPatch(
                block_id=paragraph.block_id,
                expected_sha256=paragraph.content_sha256,
                replacement="stale edit",
            ),
            workspace_id="ws_demo",
            principal=PRINCIPAL,
            expected_revision=1,
            expected_content_sha256=draft.content.content_sha256,
        )

    pending = service.save_pending_version(
        draft.draft_id,
        workspace_id="ws_demo",
        version_id="rv_backend_v1",
        label="Backend v1",
        principal=PRINCIPAL,
        expected_draft_revision=patched.revision,
    )
    assert pending.status == ResumeVersionStatus.PENDING_CONFIRMATION
    assert store.get(Resume, "res_demo", principal=PRINCIPAL).latest_version_id == "rv_master_v1"

    confirmed = service.confirm_version(
        pending.version_id, principal=PRINCIPAL, expected_revision=1
    )
    difference = service.diff(
        "rv_master_v1",
        confirmed.version_id,
        workspace_id="ws_demo",
        principal=PRINCIPAL,
    )

    assert confirmed.status == ResumeVersionStatus.CONFIRMED
    assert store.get(Resume, "res_demo", principal=PRINCIPAL).latest_version_id == confirmed.version_id
    assert difference.common_ancestor_version_id == "rv_master_v1"
    assert any(item.change == "modified" for item in difference.blocks)
    assert any("FastAPI" in line for line in difference.unified)


def test_version_map_marks_downstream_when_master_advances(tmp_path: Path) -> None:
    service, _, _ = setup_service(tmp_path)
    service.create_branch(
        branch_id="rb_backend",
        resume_id="res_demo",
        name="Backend",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id="rv_master_v1",
        principal=PRINCIPAL,
    )
    direction_draft = service.create_draft(
        draft_id="rd_direction",
        workspace_id="ws_demo",
        base_version_id="rv_master_v1",
        branch_id="rb_backend",
        principal=PRINCIPAL,
    )
    direction = service.save_pending_version(
        direction_draft.draft_id,
        workspace_id="ws_demo",
        version_id="rv_direction_v1",
        label="Direction v1",
        principal=PRINCIPAL,
        expected_draft_revision=1,
    )
    service.confirm_version(direction.version_id, principal=PRINCIPAL, expected_revision=1)
    master_draft = service.create_draft(
        draft_id="rd_master_2",
        workspace_id="ws_demo",
        base_version_id="rv_master_v1",
        branch_id="rb_master",
        principal=PRINCIPAL,
    )
    master_saved = service.autosave(
        master_draft.draft_id,
        "# Summary\n\nPython API with observability\n",
        workspace_id="ws_demo",
        principal=PRINCIPAL,
        expected_revision=1,
        expected_content_sha256=master_draft.content.content_sha256,
    )
    master_v2 = service.save_pending_version(
        master_draft.draft_id,
        workspace_id="ws_demo",
        version_id="rv_master_v2",
        label="Master v2",
        principal=PRINCIPAL,
        expected_draft_revision=master_saved.revision,
    )
    service.confirm_version(master_v2.version_id, principal=PRINCIPAL, expected_revision=1)

    version_map = service.version_map("res_demo", principal=PRINCIPAL)
    direction_node = next(
        item for item in version_map.nodes if item.version_id == "rv_direction_v1"
    )

    assert direction_node.upstream_changes_available is True
    assert len(version_map.edges) == 2


def test_block_reorder_and_reference_based_undo_redo(tmp_path: Path) -> None:
    service, _, repository = setup_service(tmp_path)
    draft = service.create_draft(
        draft_id="rd_history",
        workspace_id="ws_demo",
        base_version_id="rv_master_v1",
        branch_id="rb_master",
        principal=PRINCIPAL,
    )
    initial = service.normalizer.normalize(
        repository.read(draft.content, principal=PRINCIPAL, workspace_id="ws_demo")
    )
    reordered = service.reorder_blocks(
        draft.draft_id,
        tuple(item.block_id for item in reversed(initial.blocks)),
        workspace_id="ws_demo",
        principal=PRINCIPAL,
        expected_revision=1,
        expected_content_sha256=draft.content.content_sha256,
    )
    reordered_text = repository.read(
        reordered.content, principal=PRINCIPAL, workspace_id="ws_demo"
    )
    assert reordered_text.startswith("Python API")

    undone = service.undo(
        draft.draft_id, principal=PRINCIPAL, expected_revision=reordered.revision
    )
    assert undone.content == draft.content
    redone = service.redo(
        draft.draft_id, principal=PRINCIPAL, expected_revision=undone.revision
    )
    assert redone.content == reordered.content


def _save_changed(
    service,
    *,
    draft_id,
    branch_id,
    base_version_id,
    version_id,
    body,
):
    draft = service.create_draft(
        draft_id=draft_id,
        workspace_id="ws_demo",
        base_version_id=base_version_id,
        branch_id=branch_id,
        principal=PRINCIPAL,
    )
    saved = service.autosave(
        draft.draft_id,
        f"# Summary\n\n{body}\n",
        workspace_id="ws_demo",
        principal=PRINCIPAL,
        expected_revision=1,
        expected_content_sha256=draft.content.content_sha256,
    )
    pending = service.save_pending_version(
        draft.draft_id,
        workspace_id="ws_demo",
        version_id=version_id,
        label=version_id,
        principal=PRINCIPAL,
        expected_draft_revision=saved.revision,
    )
    return service.confirm_version(
        pending.version_id, principal=PRINCIPAL, expected_revision=1
    )


def _committing_operation(store, operation_id="op_merge_1"):
    operation = BusinessOperation.model_validate(
        {
            "operation_id": operation_id,
            "workspace_id": "ws_demo",
            "operation_type": "commit_merge",
            "idempotency_key": operation_id,
            "input_sha256": "d" * 64,
            "expected_revision": 1,
            "status": OperationStatus.COMMITTING,
            "parent_run_id": "local-merge",
            "task_id": None,
            "result_object_id": None,
            "error_code": None,
            "retryable": False,
            "revision": 1,
            "created_at": "2026-08-17T13:00:00+08:00",
            "updated_at": "2026-08-17T13:00:00+08:00",
        }
    )
    store.create_or_get_operation(operation, principal=PRINCIPAL)
    return operation


def test_three_way_merge_requires_decision_and_only_adds_target_child(
    tmp_path: Path,
) -> None:
    versions, store, repository = setup_service(tmp_path)
    versions.create_branch(
        branch_id="rb_backend",
        resume_id="res_demo",
        name="Backend",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id="rv_master_v1",
        principal=PRINCIPAL,
    )
    target = _save_changed(
        versions,
        draft_id="rd_target",
        branch_id="rb_backend",
        base_version_id="rv_master_v1",
        version_id="rv_backend_v1",
        body="Python backend services",
    )
    upstream = _save_changed(
        versions,
        draft_id="rd_upstream",
        branch_id="rb_master",
        base_version_id="rv_master_v1",
        version_id="rv_master_v2",
        body="Python platform services",
    )
    merges = ResumeMergeService(store=store, content=repository)
    proposal = merges.create_proposal(
        proposal_id="mp_backend_sync",
        workspace_id="ws_demo",
        target_branch_id="rb_backend",
        base_version_id="rv_master_v1",
        upstream_version_id=upstream.version_id,
        target_version_id=target.version_id,
        principal=PRINCIPAL,
    )

    assert proposal.status == MergeProposalStatus.CONFLICTED
    unresolved = next(
        item for item in proposal.decisions if item.decision == MergeDecisionType.UNRESOLVED
    )
    ready = merges.decide(
        proposal.proposal_id,
        block_id=unresolved.block_id,
        decision=MergeDecisionType.MANUAL,
        manual_content="Python platform and backend services",
        principal=PRINCIPAL,
        expected_revision=proposal.revision,
    )
    assert ready.status == MergeProposalStatus.READY
    operation = _committing_operation(store)
    result = merges.commit(
        ready.proposal_id,
        operation_id=operation.operation_id,
        workspace_id="ws_demo",
        result_version_id="rv_backend_merge_v2",
        label="Backend merged v2",
        principal=PRINCIPAL,
        expected_revision=ready.revision,
    )

    committed = store.get(MergeProposal, ready.proposal_id, principal=PRINCIPAL)
    assert committed.status == MergeProposalStatus.COMMITTED
    assert committed.result_version_id == result.version_id
    assert result.parent_version_id == target.version_id
    assert result.branch_id == "rb_backend"
    assert result.status == ResumeVersionStatus.CONFIRMED
    assert "platform and backend" in repository.read(
        result.content, principal=PRINCIPAL, workspace_id="ws_demo"
    )
    assert store.get(
        ResumeVersion, target.version_id, principal=PRINCIPAL
    ).content == target.content
    assert store.get(
        ResumeVersion, upstream.version_id, principal=PRINCIPAL
    ).content == upstream.content


def test_merge_operation_commits_once_and_replays_committed_result(
    tmp_path: Path,
) -> None:
    versions, store, repository = setup_service(tmp_path)
    versions.create_branch(
        branch_id="rb_ops",
        resume_id="res_demo",
        name="Operations",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id="rv_master_v1",
        principal=PRINCIPAL,
    )
    target = _save_changed(
        versions,
        draft_id="rd_ops_target",
        branch_id="rb_ops",
        base_version_id="rv_master_v1",
        version_id="rv_ops_v1",
        body="Python services",
    )
    upstream = _save_changed(
        versions,
        draft_id="rd_ops_upstream",
        branch_id="rb_master",
        base_version_id="rv_master_v1",
        version_id="rv_ops_master_v2",
        body="Python services with tests",
    )
    merges = ResumeMergeService(store=store, content=repository)
    proposal = merges.create_proposal(
        proposal_id="mp_operation",
        workspace_id="ws_demo",
        target_branch_id="rb_ops",
        base_version_id="rv_master_v1",
        upstream_version_id=upstream.version_id,
        target_version_id=target.version_id,
        principal=PRINCIPAL,
    )
    for item in proposal.decisions:
        if item.decision == MergeDecisionType.UNRESOLVED:
            proposal = merges.decide(
                proposal.proposal_id,
                block_id=item.block_id,
                decision=MergeDecisionType.ACCEPT_UPSTREAM,
                principal=PRINCIPAL,
                expected_revision=proposal.revision,
            )

    operation = merges.commit_proposal(
        proposal.proposal_id,
        operation_id="op_merge_boundary",
        idempotency_key="merge-boundary",
        workspace_id="ws_demo",
        principal=PRINCIPAL,
    )
    replay = merges.commit_proposal(
        proposal.proposal_id,
        operation_id="op_merge_boundary",
        idempotency_key="merge-boundary",
        workspace_id="ws_demo",
        principal=PRINCIPAL,
    )

    assert operation.status == OperationStatus.COMMITTED
    assert replay == operation
    assert len(
        [
            item
            for item in store.lineage("res_demo", principal=PRINCIPAL)
            if item.parent_version_id == target.version_id
        ]
    ) == 1


def test_merge_rejects_changed_target_tip_and_marks_proposal_stale(
    tmp_path: Path,
) -> None:
    versions, store, repository = setup_service(tmp_path)
    versions.create_branch(
        branch_id="rb_backend",
        resume_id="res_demo",
        name="Backend",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id="rv_master_v1",
        principal=PRINCIPAL,
    )
    target = _save_changed(
        versions,
        draft_id="rd_target_stale",
        branch_id="rb_backend",
        base_version_id="rv_master_v1",
        version_id="rv_backend_stale_v1",
        body="Python API",
    )
    upstream = _save_changed(
        versions,
        draft_id="rd_upstream_stale",
        branch_id="rb_master",
        base_version_id="rv_master_v1",
        version_id="rv_master_stale_v2",
        body="Python API with tests",
    )
    merges = ResumeMergeService(store=store, content=repository)
    proposal = merges.create_proposal(
        proposal_id="mp_stale_tip",
        workspace_id="ws_demo",
        target_branch_id="rb_backend",
        base_version_id="rv_master_v1",
        upstream_version_id=upstream.version_id,
        target_version_id=target.version_id,
        principal=PRINCIPAL,
    )
    if proposal.status == MergeProposalStatus.CONFLICTED:
        unresolved = next(
            item
            for item in proposal.decisions
            if item.decision == MergeDecisionType.UNRESOLVED
        )
        proposal = merges.decide(
            proposal.proposal_id,
            block_id=unresolved.block_id,
            decision=MergeDecisionType.KEEP_TARGET,
            principal=PRINCIPAL,
            expected_revision=proposal.revision,
        )
    _save_changed(
        versions,
        draft_id="rd_target_stale_2",
        branch_id="rb_backend",
        base_version_id=target.version_id,
        version_id="rv_backend_stale_v2",
        body="Python API changed after proposal",
    )
    operation = _committing_operation(store, "op_merge_stale")

    with pytest.raises(MergeStaleError):
        merges.commit(
            proposal.proposal_id,
            operation_id=operation.operation_id,
            workspace_id="ws_demo",
            result_version_id="rv_merge_should_not_exist",
            label="stale",
            principal=PRINCIPAL,
            expected_revision=proposal.revision,
        )

    assert store.get(
        MergeProposal, proposal.proposal_id, principal=PRINCIPAL
    ).status == MergeProposalStatus.STALE
    with pytest.raises(ObjectNotFoundError):
        store.get(
            ResumeVersion, "rv_merge_should_not_exist", principal=PRINCIPAL
        )


def _suggestion_command(analysis, draft, block, suggestion_id, markdown):
    lines = markdown.rstrip("\n").splitlines()
    original_text = "\n".join(lines[block.start_line - 1 : block.end_line])
    return SuggestionCommand(
        suggestion_id=suggestion_id,
        analysis_id=analysis.analysis_id,
        target_version_id=analysis.resume_version_id,
        target_draft_id=draft.draft_id,
        target_draft_revision=draft.revision,
        block_id=block.block_id,
        original_text=original_text,
        proposed_text=f"{original_text}\n\nEvidence-backed improvement.",
        change_type="strengthen_evidence",
        reason="Use existing resume evidence for a positive JD requirement.",
        resume_evidence=analysis.requirements[0].evidence,
        requirement_ids=(analysis.requirements[0].requirement_id,),
    )


def test_suggestion_accept_updates_only_draft_and_invalidates_siblings(tmp_path: Path) -> None:
    service, versions, store, repository, analysis, draft = setup_suggestion_service(tmp_path)
    normalized = versions.normalizer.normalize(repository.read(draft.content, principal=PRINCIPAL, workspace_id="ws_demo"))
    paragraph = next(item for item in normalized.blocks if item.kind == "paragraph")
    first = service.create(_suggestion_command(analysis, draft, paragraph, "sg_first", normalized.markdown), workspace_id="ws_demo", principal=PRINCIPAL)
    sibling = service.create(_suggestion_command(analysis, draft, paragraph, "sg_sibling", normalized.markdown), workspace_id="ws_demo", principal=PRINCIPAL)
    version_count = len(store.list(ResumeVersion, principal=PRINCIPAL).items)

    updated = service.accept(first.suggestion_id, workspace_id="ws_demo", principal=PRINCIPAL, edited_text="Python and FastAPI services with measured latency.")

    assert updated.revision == draft.revision + 1
    assert "measured latency" in repository.read(updated.content, principal=PRINCIPAL, workspace_id="ws_demo")
    assert store.get(Suggestion, first.suggestion_id, principal=PRINCIPAL).status == SuggestionStatus.ACCEPTED
    assert store.get(Suggestion, sibling.suggestion_id, principal=PRINCIPAL).status == SuggestionStatus.INVALIDATED
    assert len(store.list(ResumeVersion, principal=PRINCIPAL).items) == version_count


def test_missing_requirement_cannot_be_used_to_invent_resume_content(tmp_path: Path) -> None:
    service, versions, _, repository, analysis, draft = setup_suggestion_service(tmp_path)
    normalized = versions.normalizer.normalize(repository.read(draft.content, principal=PRINCIPAL, workspace_id="ws_demo"))
    paragraph = next(item for item in normalized.blocks if item.kind == "paragraph")
    requested = _suggestion_command(analysis, draft, paragraph, "sg_invent", normalized.markdown)
    requested = SuggestionCommand(**(requested.__dict__ | {"requirement_ids": (analysis.requirements[1].requirement_id,)}))

    with pytest.raises(SuggestionServiceError, match="suggestion_requires_positive_job_requirements"):
        service.create(requested, workspace_id="ws_demo", principal=PRINCIPAL)


def test_reject_and_stale_suggestion_do_not_change_draft(tmp_path: Path) -> None:
    service, versions, store, repository, analysis, draft = setup_suggestion_service(tmp_path)
    normalized = versions.normalizer.normalize(repository.read(draft.content, principal=PRINCIPAL, workspace_id="ws_demo"))
    paragraph = next(item for item in normalized.blocks if item.kind == "paragraph")
    rejected = service.create(_suggestion_command(analysis, draft, paragraph, "sg_reject", normalized.markdown), workspace_id="ws_demo", principal=PRINCIPAL)
    before = draft.content
    service.reject(rejected.suggestion_id, principal=PRINCIPAL)
    assert store.get(ResumeDraft, draft.draft_id, principal=PRINCIPAL).content == before

    stale = service.create(_suggestion_command(analysis, draft, paragraph, "sg_stale", normalized.markdown), workspace_id="ws_demo", principal=PRINCIPAL)
    versions.autosave(draft.draft_id, "# Summary\n\nChanged elsewhere\n", workspace_id="ws_demo", principal=PRINCIPAL, expected_revision=draft.revision, expected_content_sha256=draft.content.content_sha256)
    assert service.refresh(stale.suggestion_id, principal=PRINCIPAL).status == SuggestionStatus.INVALIDATED
    with pytest.raises(SuggestionServiceError, match="suggestion_not_pending"):
        service.accept(stale.suggestion_id, workspace_id="ws_demo", principal=PRINCIPAL)


def test_batch_apply_is_explicit_and_updates_multiple_blocks_once(tmp_path: Path) -> None:
    service, versions, store, repository, analysis, draft = setup_suggestion_service(tmp_path)
    normalized = versions.normalizer.normalize(repository.read(draft.content, principal=PRINCIPAL, workspace_id="ws_demo"))
    heading = next(item for item in normalized.blocks if item.kind == "heading")
    paragraph = next(item for item in normalized.blocks if item.kind == "paragraph")
    suggestions = tuple(
        service.create(_suggestion_command(analysis, draft, block, suggestion_id, normalized.markdown), workspace_id="ws_demo", principal=PRINCIPAL)
        for block, suggestion_id in ((heading, "sg_batch_h"), (paragraph, "sg_batch_p"))
    )
    assert store.get(ResumeDraft, draft.draft_id, principal=PRINCIPAL).revision == draft.revision

    result = service.apply_batch(
        tuple(item.suggestion_id for item in suggestions),
        workspace_id="ws_demo",
        principal=PRINCIPAL,
        edited_text_by_id={
            "sg_batch_p": "Python and FastAPI services tailored to the verified role evidence."
        },
    )

    assert result.draft.revision == draft.revision + 1
    assert result.accepted_ids == ("sg_batch_h", "sg_batch_p")
    assert "tailored to the verified role evidence" in repository.read(
        result.draft.content,
        principal=PRINCIPAL,
        workspace_id="ws_demo",
    )
    assert all(store.get(Suggestion, item.suggestion_id, principal=PRINCIPAL).status == SuggestionStatus.ACCEPTED for item in suggestions)
