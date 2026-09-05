"""Workspace lifecycle and authoritative home aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Protocol, TypeVar

from starter_agent.cv_workbench.contracts import (
    Application,
    ApplicationStatus,
    BusinessOperation,
    ContractModel,
    FeatureAvailability,
    Job,
    JobSnapshot,
    JobUserStatus,
    MatchAnalysis,
    MatchStatus,
    OperationStatus,
    PriorityJobSummary,
    RecentApplicationEventSummary,
    RecentVersionSummary,
    Resume,
    ResumeStatus,
    ResumeVersion,
    WorkbenchHome,
    WorkbenchHomeStats,
    Workspace,
    WorkspaceStatus,
    assert_transition,
)
from starter_agent.cv_workbench.store import Page, SQLiteWorkbenchStore


@dataclass(frozen=True)
class WorkspaceProfile:
    name: str
    target_roles: tuple[str, ...] = ()
    target_cities: tuple[str, ...] = ()
    remote_preference: str | None = None
    seniority: str | None = None
    keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class CreateWorkspaceCommand:
    workspace_id: str
    profile: WorkspaceProfile


class FeatureAvailabilityProvider(Protocol):
    def for_workspace(
        self, workspace: Workspace, *, principal: str
    ) -> FeatureAvailability: ...


class DefaultFeatureAvailabilityProvider:
    """Conservative availability until runtime capabilities are wired in Task 12."""

    def for_workspace(
        self, workspace: Workspace, *, principal: str
    ) -> FeatureAvailability:
        return FeatureAvailability(
            manual_jd=True,
            stable_url=True,
            delegated_research=False,
            export_pdf=False,
            export_docx=False,
            email=False,
            unavailable_reasons={
                "delegated_research": "release_gate_or_runtime_unavailable",
                "export_pdf": "export_service_unavailable",
                "export_docx": "export_service_unavailable",
                "email": "email_integration_unavailable",
            },
        )


class RuntimeFeatureAvailabilityProvider:
    """Expose only capabilities that the composed application can currently use.

    The application factory is deliberately lazy: the workbench can be used on
    its own in tests and the default home route must not require the agent
    runtime until runtime-specific capability information is requested.
    """

    _EMAIL_TOOL_NAMES = frozenset(
        {
            "email_search",
            "email_read",
            "email_create_draft",
            "email_send",
        }
    )

    def __init__(self, application_provider: Callable[[], object]) -> None:
        self._application_provider = application_provider

    def for_workspace(
        self, workspace: Workspace, *, principal: str
    ) -> FeatureAvailability:
        # Workspace and principal are part of the provider contract. Runtime
        # availability is presently application-wide, not workspace-specific.
        del workspace, principal
        application = self._application_provider()
        settings = getattr(application, "settings", None)
        enabled_tools = set(getattr(getattr(settings, "tools", None), "enabled", ()))
        delegated_research = bool(
            getattr(application, "delegation_route_enabled", lambda: False)()
        )
        email = bool(self._EMAIL_TOOL_NAMES.intersection(enabled_tools))
        unavailable_reasons: dict[str, str] = {}
        if not delegated_research:
            unavailable_reasons["delegated_research"] = "release_gate_or_runtime_unavailable"
        if not email:
            unavailable_reasons["email"] = "email_integration_unavailable"
        return FeatureAvailability(
            manual_jd=True,
            stable_url=True,
            delegated_research=delegated_research,
            export_pdf=True,
            export_docx=True,
            email=email,
            unavailable_reasons=unavailable_reasons,
        )


T = TypeVar("T", bound=ContractModel)


class WorkspaceService:
    def __init__(
        self,
        *,
        store: SQLiteWorkbenchStore,
        feature_provider: FeatureAvailabilityProvider | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.feature_provider = (
            feature_provider or DefaultFeatureAvailabilityProvider()
        )
        self.clock = clock

    def create(
        self, command: CreateWorkspaceCommand, *, principal: str
    ) -> Workspace:
        now = self.clock()
        profile = command.profile
        workspace = Workspace.model_validate(
            {
                "workspace_id": command.workspace_id,
                "owner_id": principal,
                "name": profile.name,
                "target_roles": profile.target_roles,
                "target_cities": profile.target_cities,
                "remote_preference": profile.remote_preference,
                "seniority": profile.seniority,
                "keywords": profile.keywords,
                "excluded_keywords": profile.excluded_keywords,
                "status": WorkspaceStatus.ACTIVE,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "allowed_actions": self._allowed_actions(WorkspaceStatus.ACTIVE),
            }
        )
        stored = self.store.create(workspace, principal=principal)
        self._audit(stored, principal, "workspace_created")
        return stored

    def update_profile(
        self,
        workspace_id: str,
        profile: WorkspaceProfile,
        *,
        principal: str,
        expected_revision: int,
    ) -> Workspace:
        current = self.get(workspace_id, principal=principal)
        if current.status == WorkspaceStatus.ARCHIVED:
            raise ValueError("archived_workspace_cannot_be_edited")
        updated = Workspace.model_validate(
            current.model_dump()
            | {
                "name": profile.name,
                "target_roles": profile.target_roles,
                "target_cities": profile.target_cities,
                "remote_preference": profile.remote_preference,
                "seniority": profile.seniority,
                "keywords": profile.keywords,
                "excluded_keywords": profile.excluded_keywords,
                "revision": current.revision + 1,
                "updated_at": self.clock(),
                "allowed_actions": self._allowed_actions(current.status),
            }
        )
        stored = self.store.update(
            updated, principal=principal, expected_revision=expected_revision
        )
        self._audit(stored, principal, "workspace_profile_updated")
        return stored

    def pause(
        self, workspace_id: str, *, principal: str, expected_revision: int
    ) -> Workspace:
        return self._change_status(
            workspace_id,
            WorkspaceStatus.PAUSED,
            principal=principal,
            expected_revision=expected_revision,
        )

    def resume(
        self, workspace_id: str, *, principal: str, expected_revision: int
    ) -> Workspace:
        return self._change_status(
            workspace_id,
            WorkspaceStatus.ACTIVE,
            principal=principal,
            expected_revision=expected_revision,
        )

    def archive(
        self, workspace_id: str, *, principal: str, expected_revision: int
    ) -> Workspace:
        return self._change_status(
            workspace_id,
            WorkspaceStatus.ARCHIVED,
            principal=principal,
            expected_revision=expected_revision,
        )

    def get(self, workspace_id: str, *, principal: str) -> Workspace:
        return self.store.get(Workspace, workspace_id, principal=principal)

    def list(
        self,
        *,
        principal: str,
        limit: int = 50,
        cursor: str | None = None,
        include_archived: bool = False,
    ) -> Page[Workspace]:
        return self.store.list(
            Workspace,
            principal=principal,
            limit=limit,
            cursor=cursor,
            include_archived=include_archived,
        )

    def attach_resume(
        self, workspace_id: str, resume_id: str, *, principal: str
    ) -> None:
        self.store.link_to_workspace(workspace_id, resume_id, principal=principal)

    def replace_active_resume(
        self, workspace_id: str, resume_id: str, *, principal: str
    ) -> tuple[str, ...]:
        """Make one newly imported resume the only active resume in a workspace.

        Older resumes are archived instead of physically deleted so completed
        applications and immutable analyses retain their original references.
        """
        workspace = self.get(workspace_id, principal=principal)
        self.store.assert_entity_in_workspace(
            resume_id, workspace_id, principal=principal
        )
        archived: list[str] = []
        replacements = []
        for resume in self._all_linked(Resume, workspace_id, principal):
            if resume.resume_id == resume_id or resume.status != ResumeStatus.ACTIVE:
                continue
            updated = Resume.model_validate(
                resume.model_dump()
                | {
                    "status": ResumeStatus.ARCHIVED,
                    "revision": resume.revision + 1,
                    "updated_at": self.clock(),
                    "allowed_actions": (),
                }
            )
            replacements.append(updated)
            archived.append(resume.resume_id)
        old_versions = {
            version.version_id
            for old_id in archived
            for version in self.store.lineage(old_id, principal=principal)
        }
        for analysis in self._all_direct(MatchAnalysis, workspace_id, principal):
            if analysis.resume_version_id in old_versions and analysis.status in {MatchStatus.VALIDATED, MatchStatus.PARTIAL}:
                replacements.append(MatchAnalysis.model_validate(analysis.model_dump() | {
                    "status": MatchStatus.STALE, "stale_reason": "当前档案已重新上传，请使用新简历重新匹配。",
                    "revision": analysis.revision + 1, "allowed_actions": (),
                }))
        self.store.replace_resume_projections(replacements, principal=principal)
        self.store.append_event(
            workspace_id,
            principal=principal,
            event_type="workspace_active_resume_replaced",
            payload={"resume_id": resume_id, "archived_resume_ids": archived},
            occurred_at=self.clock(),
        )
        return tuple(archived)

    def attach_job(
        self, workspace_id: str, job_id: str, *, principal: str
    ) -> None:
        self.store.link_to_workspace(workspace_id, job_id, principal=principal)

    def home(self, workspace_id: str, *, principal: str) -> WorkbenchHome:
        workspace = self.get(workspace_id, principal=principal)
        resumes = tuple(
            item
            for item in self._all_linked(Resume, workspace_id, principal)
            if item.status == ResumeStatus.ACTIVE
        )
        jobs = self._all_linked(Job, workspace_id, principal)
        operations = self._all_direct(BusinessOperation, workspace_id, principal)
        applications = self._all_direct(Application, workspace_id, principal)
        versions = self._recent_versions(resumes, principal)
        priority_jobs = self._priority_jobs(jobs, applications, principal)
        active_operations = tuple(
            operation
            for operation in operations
            if operation.status
            not in {
                OperationStatus.COMMITTED,
                OperationStatus.FAILED,
                OperationStatus.REJECTED,
                OperationStatus.CANCELLED,
            }
        )
        application_events = sorted(
            (
                RecentApplicationEventSummary(
                    event_id=event.event_id,
                    application_id=application.application_id,
                    to_status=event.to_status,
                    occurred_at=event.occurred_at,
                )
                for application in applications
                for event in application.events
            ),
            key=lambda item: (item.occurred_at, item.event_id),
            reverse=True,
        )
        todo_count = sum(
            job.user_status == JobUserStatus.TO_ANALYZE for job in jobs
        ) + sum(
            application.current_status
            in {ApplicationStatus.TO_DECIDE, ApplicationStatus.TO_APPLY}
            for application in applications
        )
        return WorkbenchHome(
            workspace=workspace,
            resume_ids=tuple(item.resume_id for item in resumes),
            job_ids=tuple(item.job_id for item in jobs),
            active_operation_ids=tuple(
                item.operation_id for item in active_operations
            ),
            recent_application_ids=tuple(
                item.application_id
                for item in sorted(
                    applications,
                    key=lambda value: (value.updated_at, value.application_id),
                    reverse=True,
                )[:10]
            ),
            stats=WorkbenchHomeStats(
                resume_count=len(resumes),
                job_count=len(jobs),
                todo_count=todo_count,
                active_operation_count=len(active_operations),
                application_count=len(applications),
            ),
            recent_versions=tuple(versions[:10]),
            priority_jobs=tuple(priority_jobs[:10]),
            recent_application_events=tuple(application_events[:20]),
            features=self.feature_provider.for_workspace(
                workspace, principal=principal
            ),
        )

    def _change_status(
        self,
        workspace_id: str,
        target: WorkspaceStatus,
        *,
        principal: str,
        expected_revision: int,
    ) -> Workspace:
        current = self.get(workspace_id, principal=principal)
        assert_transition(current.status, target)
        updated = Workspace.model_validate(
            current.model_dump()
            | {
                "status": target,
                "revision": current.revision + 1,
                "updated_at": self.clock(),
                "allowed_actions": self._allowed_actions(target),
            }
        )
        stored = self.store.update(
            updated, principal=principal, expected_revision=expected_revision
        )
        self._audit(stored, principal, f"workspace_{target.value}")
        return stored

    def _all_linked(
        self, model_type: type[T], workspace_id: str, principal: str
    ) -> tuple[T, ...]:
        items: list[T] = []
        cursor: str | None = None
        while True:
            page = self.store.list_linked(
                model_type,
                workspace_id,
                principal=principal,
                limit=50,
                cursor=cursor,
            )
            items.extend(page.items)
            if page.next_cursor is None:
                return tuple(items)
            cursor = page.next_cursor

    def _all_direct(
        self, model_type: type[T], workspace_id: str, principal: str
    ) -> tuple[T, ...]:
        items: list[T] = []
        cursor: str | None = None
        while True:
            page = self.store.list(
                model_type,
                principal=principal,
                workspace_id=workspace_id,
                limit=50,
                cursor=cursor,
            )
            items.extend(page.items)
            if page.next_cursor is None:
                return tuple(items)
            cursor = page.next_cursor

    def _recent_versions(
        self, resumes: tuple[Resume, ...], principal: str
    ) -> list[RecentVersionSummary]:
        versions: list[ResumeVersion] = []
        for resume in resumes:
            versions.extend(self.store.lineage(resume.resume_id, principal=principal))
        return sorted(
            (
                RecentVersionSummary(
                    version_id=version.version_id,
                    resume_id=version.resume_id,
                    label=version.label,
                    status=version.status,
                    created_at=version.created_at,
                )
                for version in versions
            ),
            key=lambda item: (item.created_at, item.version_id),
            reverse=True,
        )

    def _priority_jobs(
        self,
        jobs: tuple[Job, ...],
        applications: tuple[Application, ...],
        principal: str,
    ) -> list[PriorityJobSummary]:
        priorities = {job.job_id: 0 for job in jobs}
        for application in applications:
            snapshot = self.store.get(
                JobSnapshot, application.job_snapshot_id, principal=principal
            )
            if snapshot.job_id in priorities:
                priorities[snapshot.job_id] = max(
                    priorities[snapshot.job_id], application.priority
                )
        summaries = [
            PriorityJobSummary(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                user_status=job.user_status,
                priority=priorities[job.job_id],
            )
            for job in jobs
        ]
        return sorted(
            summaries,
            key=lambda item: (item.priority, item.job_id),
            reverse=True,
        )

    @staticmethod
    def _allowed_actions(status: WorkspaceStatus) -> tuple[str, ...]:
        if status == WorkspaceStatus.ACTIVE:
            return ("edit", "pause", "archive")
        if status == WorkspaceStatus.PAUSED:
            return ("edit", "resume", "archive")
        return ()

    def _audit(
        self, workspace: Workspace, principal: str, event_type: str
    ) -> None:
        self.store.append_event(
            workspace.workspace_id,
            principal=principal,
            event_type=event_type,
            payload={"revision": workspace.revision, "status": workspace.status.value},
            occurred_at=self.clock(),
        )
