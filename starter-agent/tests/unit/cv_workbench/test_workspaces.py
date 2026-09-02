from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from starter_agent.cv_workbench.contracts import (
    Application,
    BusinessOperation,
    CONTRACT_VERSION,
    Job,
    JobSnapshot,
    OperationStatus,
    Resume,
    ResumeStatus,
    ResumeBranch,
    ResumeBranchType,
    ResumeNodeType,
    ResumeVersion,
    ResumeVersionStatus,
    WorkspaceStatus,
)
from starter_agent.cv_workbench.store import (
    ForbiddenError,
    RevisionConflictError,
    SQLiteWorkbenchStore,
)
from starter_agent.cv_workbench.workspaces import (
    CreateWorkspaceCommand,
    RuntimeFeatureAvailabilityProvider,
    WorkspaceProfile,
    WorkspaceService,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


def fixture(name: str) -> dict:
    return json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )[name]


def make_service(tmp_path: Path) -> tuple[WorkspaceService, SQLiteWorkbenchStore]:
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'workspace-service.db').as_posix()}", tmp_path
    )
    return WorkspaceService(store=store), store


def profile(name: str = "2026 秋招") -> WorkspaceProfile:
    return WorkspaceProfile(
        name=name,
        target_roles=("Backend Engineer",),
        target_cities=("Shanghai",),
        remote_preference="hybrid",
        seniority="mid",
        keywords=("Python", "FastAPI"),
        excluded_keywords=("outsourcing",),
    )


def create_workspace(
    service: WorkspaceService, workspace_id: str = "ws_demo", principal: str = PRINCIPAL
):
    return service.create(
        CreateWorkspaceCommand(workspace_id=workspace_id, profile=profile()),
        principal=principal,
    )


def create_resume_family(store: SQLiteWorkbenchStore) -> None:
    resume_payload = fixture("resume")
    resume_payload.update(latest_version_id=None, revision=1)
    store.create(Resume.model_validate(resume_payload), principal=PRINCIPAL)
    branch = ResumeBranch.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "branch_id": "rb_master",
            "resume_id": "res_demo",
            "name": "master",
            "branch_type": ResumeBranchType.MASTER,
            "base_version_id": "rv_master_v1",
            "job_snapshot_id": None,
            "archived": False,
            "revision": 1,
            "created_at": "2026-08-17T08:10:00+08:00",
            "updated_at": "2026-08-17T08:10:00+08:00",
            "allowed_actions": ("create_version",),
        }
    )
    store.create(branch, principal=PRINCIPAL)
    version = ResumeVersion.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "version_id": "rv_master_v1",
            "resume_id": "res_demo",
            "branch_id": "rb_master",
            "parent_version_id": None,
            "branch_base_version_id": "rv_master_v1",
            "node_type": ResumeNodeType.BASE,
            "version_number": 1,
            "label": "Master v1",
            "content": {
                "content_sha256": "a" * 64,
                "knowledge_base_id": "kb-demo",
                "document_id": "doc-demo",
                "document_version_id": "docv-demo-1",
                "artifact_id": None,
            },
            "status": ResumeVersionStatus.CONFIRMED,
            "job_snapshot_id": None,
            "upstream_changes_available": False,
            "revision": 1,
            "created_by": PRINCIPAL,
            "created_at": "2026-08-17T08:20:00+08:00",
            "confirmed_at": "2026-08-17T08:21:00+08:00",
            "allowed_actions": ("compare",),
        }
    )
    store.create(version, principal=PRINCIPAL)


def create_job_and_application(store: SQLiteWorkbenchStore) -> None:
    store.create(Job.model_validate(fixture("job")), principal=PRINCIPAL)
    store.create(JobSnapshot.model_validate(fixture("job_snapshot")), principal=PRINCIPAL)
    application = fixture("application")
    application["resume_version_id"] = "rv_master_v1"
    store.create(Application.model_validate(application), principal=PRINCIPAL)


def create_running_operation(store: SQLiteWorkbenchStore) -> None:
    operation = BusinessOperation.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "operation_id": "op_home_1",
            "workspace_id": "ws_demo",
            "operation_type": "analyze_match",
            "idempotency_key": "home-analysis-1",
            "input_sha256": "b" * 64,
            "expected_revision": 1,
            "status": OperationStatus.RUNNING,
            "parent_run_id": "run-home-1",
            "task_id": "task-home-1",
            "result_object_id": None,
            "error_code": None,
            "retryable": False,
            "revision": 1,
            "created_at": "2026-08-17T12:10:00+08:00",
            "updated_at": "2026-08-17T12:10:00+08:00",
        }
    )
    store.create_or_get_operation(operation, principal=PRINCIPAL)


def test_workspace_lifecycle_profile_and_allowed_actions(tmp_path: Path) -> None:
    service, store = make_service(tmp_path)
    created = create_workspace(service)
    changed = service.update_profile(
        "ws_demo", profile("Agent 求职"), principal=PRINCIPAL, expected_revision=1
    )
    paused = service.pause(
        "ws_demo", principal=PRINCIPAL, expected_revision=2
    )
    resumed = service.resume(
        "ws_demo", principal=PRINCIPAL, expected_revision=3
    )
    archived = service.archive(
        "ws_demo", principal=PRINCIPAL, expected_revision=4
    )

    assert created.allowed_actions == ("edit", "pause", "archive")
    assert changed.name == "Agent 求职"
    assert paused.status == WorkspaceStatus.PAUSED
    assert resumed.status == WorkspaceStatus.ACTIVE
    assert archived.status == WorkspaceStatus.ARCHIVED
    assert archived.allowed_actions == ()
    assert [event.event_type for event in store.list_events("ws_demo", principal=PRINCIPAL)] == [
        "workspace_created",
        "workspace_profile_updated",
        "workspace_paused",
        "workspace_active",
        "workspace_archived",
    ]


def test_workspace_update_is_cas_protected(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    create_workspace(service)

    with pytest.raises(RevisionConflictError) as error:
        service.update_profile(
            "ws_demo", profile("stale"), principal=PRINCIPAL, expected_revision=99
        )

    assert error.value.authoritative_revision == 1


def test_home_is_authoritative_and_scoped_by_workspace(tmp_path: Path) -> None:
    service, store = make_service(tmp_path)
    create_workspace(service)
    create_workspace(service, "ws_other")
    create_resume_family(store)
    create_job_and_application(store)
    create_running_operation(store)
    service.attach_resume("ws_demo", "res_demo", principal=PRINCIPAL)
    service.attach_job("ws_demo", "job_acme_backend", principal=PRINCIPAL)

    home = service.home("ws_demo", principal=PRINCIPAL)
    other = service.home("ws_other", principal=PRINCIPAL)

    assert home.stats.resume_count == 1
    assert home.stats.job_count == 1
    assert home.stats.application_count == 1
    assert home.stats.active_operation_count == 1
    assert home.stats.todo_count == 1
    assert home.resume_ids == ("res_demo",)
    assert home.job_ids == ("job_acme_backend",)
    assert home.active_operation_ids == ("op_home_1",)
    assert home.recent_versions[0].version_id == "rv_master_v1"
    assert home.priority_jobs[0].priority == 80
    assert home.recent_application_events[0].event_id == "ae_acme_to_apply"
    assert home.features.delegated_research is False
    assert other.stats.resume_count == other.stats.job_count == 0
    assert other.active_operation_ids == ()


def test_replacing_active_resume_archives_old_resume_but_preserves_it(tmp_path: Path) -> None:
    service, store = make_service(tmp_path)
    try:
        create_workspace(service)
        create_resume_family(store)
        service.attach_resume("ws_demo", "res_demo", principal=PRINCIPAL)
        current = store.get(Resume, "res_demo", principal=PRINCIPAL)
        replacement = Resume.model_validate(
            current.model_dump()
            | {
                "resume_id": "res_replacement",
                "name": "Replacement",
                "latest_version_id": None,
                "revision": 1,
            }
        )
        store.create(replacement, principal=PRINCIPAL)
        service.attach_resume("ws_demo", "res_replacement", principal=PRINCIPAL)

        archived = service.replace_active_resume(
            "ws_demo", "res_replacement", principal=PRINCIPAL
        )

        assert archived == ("res_demo",)
        assert store.get(Resume, "res_demo", principal=PRINCIPAL).status == ResumeStatus.ARCHIVED
        assert service.home("ws_demo", principal=PRINCIPAL).resume_ids == ("res_replacement",)
    finally:
        store.close()


def test_runtime_feature_provider_reflects_composed_application() -> None:
    application = SimpleNamespace(
        settings=SimpleNamespace(
            tools=SimpleNamespace(enabled=("email_read", "email_create_draft"))
        ),
        delegation_route_enabled=lambda: True,
    )
    provider = RuntimeFeatureAvailabilityProvider(lambda: application)

    features = provider.for_workspace(
        SimpleNamespace(), principal=PRINCIPAL  # type: ignore[arg-type]
    )

    assert features.delegated_research is True
    assert features.export_pdf is True
    assert features.export_docx is True
    assert features.email is True
    assert features.unavailable_reasons == {}


def test_archiving_workspace_preserves_linked_business_objects(tmp_path: Path) -> None:
    service, store = make_service(tmp_path)
    create_workspace(service)
    create_resume_family(store)
    create_job_and_application(store)
    service.attach_resume("ws_demo", "res_demo", principal=PRINCIPAL)
    service.attach_job("ws_demo", "job_acme_backend", principal=PRINCIPAL)

    service.archive("ws_demo", principal=PRINCIPAL, expected_revision=1)
    home = service.home("ws_demo", principal=PRINCIPAL)

    assert home.workspace.status == WorkspaceStatus.ARCHIVED
    assert home.resume_ids == ("res_demo",)
    assert home.job_ids == ("job_acme_backend",)
    assert store.get(Resume, "res_demo", principal=PRINCIPAL).resume_id == "res_demo"


def test_membership_cannot_cross_principal(tmp_path: Path) -> None:
    service, store = make_service(tmp_path)
    create_workspace(service)
    create_workspace(service, "ws_other_owner", principal="other-user")
    create_resume_family(store)

    with pytest.raises(ForbiddenError):
        service.attach_resume(
            "ws_other_owner", "res_demo", principal="other-user"
        )
    with pytest.raises(ForbiddenError):
        service.home("ws_demo", principal="other-user")


def test_workspace_list_has_stable_pagination_and_archived_filter(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    for index in range(3):
        create_workspace(service, f"ws_page_{index}")
    service.archive("ws_page_1", principal=PRINCIPAL, expected_revision=1)

    first = service.list(principal=PRINCIPAL, limit=1)
    second = service.list(
        principal=PRINCIPAL, limit=1, cursor=first.next_cursor
    )
    all_items = service.list(
        principal=PRINCIPAL, include_archived=True
    ).items

    assert len(first.items) == len(second.items) == 1
    assert first.items[0].workspace_id != second.items[0].workspace_id
    assert {item.workspace_id for item in all_items} == {
        "ws_page_0",
        "ws_page_1",
        "ws_page_2",
    }
