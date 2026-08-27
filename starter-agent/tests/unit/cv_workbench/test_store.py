from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from starter_agent.cv_workbench.contracts import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    BusinessOperation,
    CONTRACT_VERSION,
    ExportRecord,
    Job,
    JobSnapshot,
    MatchAnalysis,
    MatchStatus,
    MergeDecision,
    MergeProposal,
    MergeProposalStatus,
    OperationStatus,
    Resume,
    ResumeBranch,
    ResumeBranchType,
    ResumeNodeType,
    ResumeVersion,
    ResumeVersionStatus,
    Workspace,
    WorkspaceStatus,
)
from starter_agent.cv_workbench.store import (
    ForbiddenError,
    IdempotencyConflictError,
    ImmutableObjectError,
    LineageConflictError,
    ReferenceConflictError,
    RevisionConflictError,
    SQLiteWorkbenchStore,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_store(tmp_path: Path) -> SQLiteWorkbenchStore:
    database = (tmp_path / "workbench.db").as_posix()
    return SQLiteWorkbenchStore(f"sqlite:///{database}", tmp_path)


def make_workspace(*, workspace_id: str = "ws_demo", revision: int = 1) -> Workspace:
    payload = load_fixture("business-objects.json")["workspace"]
    payload["workspace_id"] = workspace_id
    payload["revision"] = revision
    return Workspace.model_validate(payload)


def make_resume(*, resume_id: str = "res_demo") -> Resume:
    payload = load_fixture("business-objects.json")["resume"]
    payload["resume_id"] = resume_id
    payload["latest_version_id"] = None
    payload["revision"] = 1
    return Resume.model_validate(payload)


def make_branch(
    *,
    resume_id: str = "res_demo",
    branch_id: str = "rb_master",
    base_version_id: str = "rv_master_v1",
    branch_type: ResumeBranchType = ResumeBranchType.MASTER,
) -> ResumeBranch:
    return ResumeBranch.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "branch_id": branch_id,
            "resume_id": resume_id,
            "name": branch_id,
            "branch_type": branch_type,
            "base_version_id": base_version_id,
            "job_snapshot_id": None,
            "archived": False,
            "revision": 1,
            "created_at": "2026-08-17T09:00:00+08:00",
            "updated_at": "2026-08-17T09:00:00+08:00",
            "allowed_actions": ["create_version"],
        }
    )


def make_version(
    *,
    resume_id: str = "res_demo",
    branch_id: str = "rb_master",
    version_id: str = "rv_master_v1",
    parent_version_id: str | None = None,
    branch_base_version_id: str = "rv_master_v1",
    node_type: ResumeNodeType = ResumeNodeType.BASE,
    status: ResumeVersionStatus = ResumeVersionStatus.CONFIRMED,
    revision: int = 1,
    content_hash: str = "a" * 64,
) -> ResumeVersion:
    confirmed_at = (
        "2026-08-17T09:01:00+08:00"
        if status == ResumeVersionStatus.CONFIRMED
        else None
    )
    return ResumeVersion.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "version_id": version_id,
            "resume_id": resume_id,
            "branch_id": branch_id,
            "parent_version_id": parent_version_id,
            "branch_base_version_id": branch_base_version_id,
            "node_type": node_type,
            "version_number": 1,
            "label": version_id,
            "content": {
                "content_sha256": content_hash,
                "knowledge_base_id": "kb-demo",
                "document_id": "doc-demo",
                "document_version_id": f"docv-{version_id}",
                "artifact_id": None,
            },
            "status": status,
            "job_snapshot_id": None,
            "upstream_changes_available": False,
            "revision": revision,
            "created_by": PRINCIPAL,
            "created_at": "2026-08-17T09:00:00+08:00",
            "confirmed_at": confirmed_at,
            "allowed_actions": ["compare"],
        }
    )


def create_root_family(
    store: SQLiteWorkbenchStore,
    *,
    resume_id: str = "res_demo",
    branch_id: str = "rb_master",
    version_id: str = "rv_master_v1",
) -> tuple[Resume, ResumeBranch, ResumeVersion]:
    resume = make_resume(resume_id=resume_id)
    branch = make_branch(
        resume_id=resume_id,
        branch_id=branch_id,
        base_version_id=version_id,
    )
    version = make_version(
        resume_id=resume_id,
        branch_id=branch_id,
        version_id=version_id,
        branch_base_version_id=version_id,
    )
    store.create(resume, principal=PRINCIPAL)
    store.create(branch, principal=PRINCIPAL)
    store.create(version, principal=PRINCIPAL)
    return resume, branch, version


def test_store_enables_foreign_keys_secure_delete_and_schema_version(tmp_path) -> None:
    store = make_store(tmp_path)

    with store.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA secure_delete")).scalar_one() == 1
        assert connection.execute(
            text("SELECT version FROM cv_workbench_schema_migrations ORDER BY version")
        ).scalars().all() == [1, 2, 3, 4, 5]


def test_create_get_reopen_and_principal_isolation(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = make_workspace()
    store.create(workspace, principal=PRINCIPAL)

    assert store.get(Workspace, workspace.workspace_id, principal=PRINCIPAL) == workspace
    with pytest.raises(ForbiddenError):
        store.get(Workspace, workspace.workspace_id, principal="other-user")

    reopened = make_store(tmp_path)
    assert reopened.get(Workspace, workspace.workspace_id, principal=PRINCIPAL) == workspace


def test_payload_owner_cannot_override_trusted_principal(tmp_path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ForbiddenError, match="payload_owner_does_not_match_principal"):
        store.create(make_workspace(), principal="other-user")


def test_workspace_update_uses_cas_and_stable_transitions(tmp_path) -> None:
    store = make_store(tmp_path)
    workspace = make_workspace()
    store.create(workspace, principal=PRINCIPAL)
    paused = Workspace.model_validate(
        workspace.model_dump()
        | {"status": WorkspaceStatus.PAUSED, "revision": 2}
    )

    store.update(paused, principal=PRINCIPAL, expected_revision=1)
    assert store.get(Workspace, "ws_demo", principal=PRINCIPAL).status == WorkspaceStatus.PAUSED

    with pytest.raises(RevisionConflictError) as error:
        store.update(paused, principal=PRINCIPAL, expected_revision=1)
    assert error.value.authoritative_revision == 2


def test_stable_cursor_pagination(tmp_path) -> None:
    store = make_store(tmp_path)
    for index in range(3):
        workspace = make_workspace(workspace_id=f"ws_demo_{index}")
        store.create(workspace, principal=PRINCIPAL)

    first = store.list(Workspace, principal=PRINCIPAL, limit=2)
    second = store.list(
        Workspace, principal=PRINCIPAL, limit=2, cursor=first.next_cursor
    )

    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert len(second.items) == 1
    assert {item.workspace_id for item in first.items + second.items} == {
        "ws_demo_0",
        "ws_demo_1",
        "ws_demo_2",
    }


def test_resume_lineage_is_backend_authoritative_and_cross_resume_parent_fails(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    create_root_family(store)
    direction_branch = make_branch(
        branch_id="rb_backend",
        base_version_id="rv_master_v1",
        branch_type=ResumeBranchType.DIRECTION,
    )
    direction_version = make_version(
        branch_id="rb_backend",
        version_id="rv_backend_v1",
        parent_version_id="rv_master_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DIRECTION,
        content_hash="b" * 64,
    )
    store.create(direction_branch, principal=PRINCIPAL)
    store.create(direction_version, principal=PRINCIPAL)

    assert [item.version_id for item in store.lineage("res_demo", principal=PRINCIPAL)] == [
        "rv_master_v1",
        "rv_backend_v1",
    ]

    store.create(make_resume(resume_id="res_other"), principal=PRINCIPAL)
    store.create(
        make_branch(
            resume_id="res_other",
            branch_id="rb_other",
            base_version_id="rv_master_v1",
            branch_type=ResumeBranchType.DIRECTION,
        ),
        principal=PRINCIPAL,
    )
    invalid = make_version(
        resume_id="res_other",
        branch_id="rb_other",
        version_id="rv_other_v1",
        parent_version_id="rv_master_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DIRECTION,
    )
    with pytest.raises(LineageConflictError, match="cross_resume_parent"):
        store.create(invalid, principal=PRINCIPAL)


def test_pending_version_can_only_confirm_without_changing_content_or_lineage(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    create_root_family(store)
    branch = make_branch(
        branch_id="rb_backend",
        base_version_id="rv_master_v1",
        branch_type=ResumeBranchType.DIRECTION,
    )
    pending = make_version(
        branch_id="rb_backend",
        version_id="rv_backend_pending",
        parent_version_id="rv_master_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DIRECTION,
        status=ResumeVersionStatus.PENDING_CONFIRMATION,
    )
    store.create(branch, principal=PRINCIPAL)
    store.create(pending, principal=PRINCIPAL)
    confirmed = ResumeVersion.model_validate(
        pending.model_dump()
        | {
            "status": "confirmed",
            "revision": 2,
            "confirmed_at": "2026-08-17T10:00:00+08:00",
        }
    )
    store.update(confirmed, principal=PRINCIPAL, expected_revision=1)

    changed = ResumeVersion.model_validate(
        confirmed.model_dump()
        | {
            "revision": 3,
            "content": confirmed.content.model_dump()
            | {"content_sha256": "f" * 64},
        }
    )
    with pytest.raises(ImmutableObjectError):
        store.update(changed, principal=PRINCIPAL, expected_revision=2)


def test_resume_rejects_second_root_and_branch_base_mismatch(tmp_path) -> None:
    store = make_store(tmp_path)
    create_root_family(store)
    second_branch = make_branch(
        branch_id="rb_second_root",
        base_version_id="rv_second_root",
    )
    store.create(second_branch, principal=PRINCIPAL)
    with pytest.raises(LineageConflictError, match="multiple_base_roots"):
        store.create(
            make_version(
                branch_id="rb_second_root",
                version_id="rv_second_root",
                branch_base_version_id="rv_second_root",
            ),
            principal=PRINCIPAL,
        )

    direction = make_branch(
        branch_id="rb_direction_mismatch",
        base_version_id="rv_master_v1",
        branch_type=ResumeBranchType.DIRECTION,
    )
    store.create(direction, principal=PRINCIPAL)
    with pytest.raises(LineageConflictError, match="branch_base_mismatch"):
        store.create(
            make_version(
                branch_id="rb_direction_mismatch",
                version_id="rv_direction_mismatch",
                parent_version_id="rv_master_v1",
                branch_base_version_id="rv_direction_mismatch",
                node_type=ResumeNodeType.DIRECTION,
            ),
            principal=PRINCIPAL,
        )


def test_operation_idempotency_reuses_same_payload_and_rejects_changed_payload(
    tmp_path,
) -> None:
    store = make_store(tmp_path)
    store.create(make_workspace(), principal=PRINCIPAL)
    operation = BusinessOperation.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "operation_id": "op_demo_1",
            "workspace_id": "ws_demo",
            "operation_type": "create_match_analysis",
            "idempotency_key": "analysis-key-1",
            "input_sha256": "a" * 64,
            "expected_revision": 1,
            "status": "created",
            "parent_run_id": None,
            "task_id": None,
            "result_object_id": None,
            "error_code": None,
            "retryable": False,
            "revision": 1,
            "created_at": "2026-08-17T10:00:00+08:00",
            "updated_at": "2026-08-17T10:00:00+08:00",
        }
    )
    first, created = store.create_or_get_operation(operation, principal=PRINCIPAL)
    replay, replay_created = store.create_or_get_operation(
        operation, principal=PRINCIPAL
    )
    assert created is True
    assert replay_created is False
    assert replay == first

    changed = BusinessOperation.model_validate(
        operation.model_dump()
        | {"operation_id": "op_demo_2", "input_sha256": "b" * 64}
    )
    with pytest.raises(IdempotencyConflictError):
        store.create_or_get_operation(changed, principal=PRINCIPAL)


def test_operation_state_update_is_cas_protected(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(make_workspace(), principal=PRINCIPAL)
    operation_payload = {
        "contract_version": CONTRACT_VERSION,
        "operation_id": "op_state_1",
        "workspace_id": "ws_demo",
        "operation_type": "import_resume",
        "idempotency_key": "import-key-1",
        "input_sha256": "c" * 64,
        "expected_revision": None,
        "status": "created",
        "parent_run_id": None,
        "task_id": None,
        "result_object_id": None,
        "error_code": None,
        "retryable": False,
        "revision": 1,
        "created_at": "2026-08-17T10:00:00+08:00",
        "updated_at": "2026-08-17T10:00:00+08:00",
    }
    operation = BusinessOperation.model_validate(operation_payload)
    store.create_or_get_operation(operation, principal=PRINCIPAL)
    running = BusinessOperation.model_validate(
        operation.model_dump() | {"status": "running", "revision": 2}
    )
    store.update(running, principal=PRINCIPAL, expected_revision=1)

    assert store.get(BusinessOperation, "op_state_1", principal=PRINCIPAL).status == OperationStatus.RUNNING


def setup_job_and_application_dependencies(store: SQLiteWorkbenchStore) -> None:
    store.create(make_workspace(), principal=PRINCIPAL)
    create_root_family(store)
    payloads = load_fixture("business-objects.json")
    store.create(Job.model_validate(payloads["job"]), principal=PRINCIPAL)
    store.create(JobSnapshot.model_validate(payloads["job_snapshot"]), principal=PRINCIPAL)


def test_application_and_export_reject_pending_resume_version(tmp_path) -> None:
    store = make_store(tmp_path)
    setup_job_and_application_dependencies(store)
    branch = make_branch(
        branch_id="rb_pending_use",
        base_version_id="rv_master_v1",
        branch_type=ResumeBranchType.DIRECTION,
    )
    pending = make_version(
        branch_id=branch.branch_id,
        version_id="rv_pending_use",
        parent_version_id="rv_master_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DIRECTION,
        status=ResumeVersionStatus.PENDING_CONFIRMATION,
    )
    store.create(branch, principal=PRINCIPAL)
    store.create(pending, principal=PRINCIPAL)
    application_payload = load_fixture("business-objects.json")["application"]
    application_payload["application_id"] = "app_pending_use"
    application_payload["resume_version_id"] = pending.version_id
    export_payload = load_fixture("business-objects.json")["export"]
    export_payload["export_id"] = "exp_pending_use"
    export_payload["resume_version_id"] = pending.version_id

    with pytest.raises(
        ReferenceConflictError,
        match="application_or_export_requires_confirmed_resume_version",
    ):
        store.create(
            Application.model_validate(application_payload), principal=PRINCIPAL
        )
    with pytest.raises(
        ReferenceConflictError,
        match="application_or_export_requires_confirmed_resume_version",
    ):
        store.create(ExportRecord.model_validate(export_payload), principal=PRINCIPAL)


def test_application_events_are_append_only_and_projected_to_event_table(tmp_path) -> None:
    store = make_store(tmp_path)
    setup_job_and_application_dependencies(store)
    payload = load_fixture("business-objects.json")["application"]
    payload["resume_version_id"] = "rv_master_v1"
    application = Application.model_validate(payload)
    store.create(application, principal=PRINCIPAL)
    assert len(store.list_events(application.application_id, principal=PRINCIPAL)) == 2

    next_event = ApplicationEvent.model_validate(
        {
            "event_id": "ae_acme_applied",
            "from_status": "to_apply",
            "to_status": "applied",
            "confirmed_by": PRINCIPAL,
            "occurred_at": "2026-08-17T12:10:00+08:00",
            "note": "已由用户确认投递",
        }
    )
    updated = Application.model_validate(
        application.model_dump()
        | {
            "current_status": ApplicationStatus.APPLIED,
            "events": [*application.events, next_event],
            "revision": 3,
            "updated_at": "2026-08-17T12:10:00+08:00",
        }
    )
    store.update(updated, principal=PRINCIPAL, expected_revision=2)
    assert len(store.list_events(application.application_id, principal=PRINCIPAL)) == 3

    rewritten = Application.model_validate(
        updated.model_dump()
        | {
            "events": [updated.events[1], updated.events[0], updated.events[2]],
            "revision": 4,
        }
    )
    with pytest.raises(ImmutableObjectError, match="application_events_are_append_only"):
        store.update(rewritten, principal=PRINCIPAL, expected_revision=3)


def test_references_and_immutable_versions_block_physical_delete(tmp_path) -> None:
    store = make_store(tmp_path)
    setup_job_and_application_dependencies(store)

    with pytest.raises(ReferenceConflictError):
        store.physical_delete("job_acme_backend", principal=PRINCIPAL)
    with pytest.raises(ImmutableObjectError):
        store.physical_delete("rv_master_v1", principal=PRINCIPAL)


def test_match_analysis_can_be_marked_stale_without_rewriting_result(tmp_path) -> None:
    store = make_store(tmp_path)
    setup_job_and_application_dependencies(store)
    payload = load_fixture("analysis-validated.json")
    payload["resume_version_id"] = "rv_master_v1"
    analysis = MatchAnalysis.model_validate(payload)
    store.create(analysis, principal=PRINCIPAL)
    stale = MatchAnalysis.model_validate(
        analysis.model_dump()
        | {
            "status": MatchStatus.STALE,
            "stale_reason": "resume_version_changed",
            "allowed_actions": ["view", "reanalyze"],
            "revision": 2,
        }
    )
    store.update(stale, principal=PRINCIPAL, expected_revision=1)
    stored = store.get(MatchAnalysis, analysis.analysis_id, principal=PRINCIPAL)
    assert stored.status == MatchStatus.STALE
    assert stored.total_score == analysis.total_score
    assert stored.requirements == analysis.requirements

    rewritten = MatchAnalysis.model_validate(
        stale.model_dump() | {"total_score": 1.0, "revision": 3}
    )
    with pytest.raises(ImmutableObjectError):
        store.update(rewritten, principal=PRINCIPAL, expected_revision=2)


def test_merge_proposal_commits_once_and_references_a_new_target_version(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create(make_workspace(), principal=PRINCIPAL)
    create_root_family(store)
    backend_branch = make_branch(
        branch_id="rb_backend",
        base_version_id="rv_master_v1",
        branch_type=ResumeBranchType.DIRECTION,
    )
    backend_v1 = make_version(
        branch_id="rb_backend",
        version_id="rv_backend_v1",
        parent_version_id="rv_master_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DIRECTION,
        content_hash="b" * 64,
    )
    master_v2 = make_version(
        version_id="rv_master_v2",
        parent_version_id="rv_master_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DERIVED,
        content_hash="c" * 64,
    )
    store.create(backend_branch, principal=PRINCIPAL)
    store.create(backend_v1, principal=PRINCIPAL)
    store.create(master_v2, principal=PRINCIPAL)

    proposal = MergeProposal.model_validate(load_fixture("merge-conflict.json"))
    store.create(proposal, principal=PRINCIPAL)
    resolved_item = MergeDecision.model_validate(
        proposal.decisions[1].model_dump()
        | {
            "decision": "keep_target",
            "result_sha256": proposal.decisions[1].target_sha256,
            "decided_by": PRINCIPAL,
            "decided_at": "2026-08-17T10:05:00+08:00",
        }
    )
    ready = MergeProposal.model_validate(
        proposal.model_dump()
        | {
            "decisions": [proposal.decisions[0], resolved_item],
            "status": MergeProposalStatus.READY,
            "revision": 2,
        }
    )
    store.update(ready, principal=PRINCIPAL, expected_revision=1)
    committing = MergeProposal.model_validate(
        ready.model_dump() | {"status": "committing", "revision": 3}
    )
    store.update(committing, principal=PRINCIPAL, expected_revision=2)

    merge_operation = BusinessOperation.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "operation_id": "op_merge_demo",
            "workspace_id": "ws_demo",
            "operation_type": "commit_merge_proposal",
            "idempotency_key": "merge-demo-1",
            "input_sha256": "d" * 64,
            "expected_revision": 3,
            "status": "created",
            "parent_run_id": None,
            "task_id": None,
            "result_object_id": None,
            "error_code": None,
            "retryable": False,
            "revision": 1,
            "created_at": "2026-08-17T10:06:00+08:00",
            "updated_at": "2026-08-17T10:06:00+08:00",
        }
    )
    store.create_or_get_operation(merge_operation, principal=PRINCIPAL)
    backend_v2 = make_version(
        branch_id="rb_backend",
        version_id="rv_backend_v2",
        parent_version_id="rv_backend_v1",
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.DIRECTION,
        content_hash="e" * 64,
    )
    store.create(backend_v2, principal=PRINCIPAL)
    committed = MergeProposal.model_validate(
        committing.model_dump()
        | {
            "status": "committed",
            "revision": 4,
            "operation_id": "op_merge_demo",
            "result_version_id": "rv_backend_v2",
        }
    )
    store.update(committed, principal=PRINCIPAL, expected_revision=3)

    assert store.get(
        MergeProposal, committed.proposal_id, principal=PRINCIPAL
    ).result_version_id == "rv_backend_v2"
    with pytest.raises(ImmutableObjectError):
        store.update(
            MergeProposal.model_validate(
                committed.model_dump() | {"revision": 5}
            ),
            principal=PRINCIPAL,
            expected_revision=4,
        )
