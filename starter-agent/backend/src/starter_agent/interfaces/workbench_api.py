"""Stable HTTP boundary for CV Workbench domain services."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Callable, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from starter_agent.cv_workbench.contracts import (
    Application,
    ApplicationStatus,
    BusinessOperation,
    ExportRecord,
    InterviewReview,
    Job,
    JobSnapshot,
    MatchAnalysis,
    MatchStatus,
    MergeDecisionType,
    MergeProposal,
    Resume,
    ResumeBranchType,
    ResumeDraft,
    ResumeVersion,
    Suggestion,
    SuggestionStatus,
    VersionViewPreference,
    Workspace,
)
from starter_agent.cv_workbench.applications import (
    ApplicationDetailsCommand,
    ApplicationEventCommand,
    CreateApplicationCommand,
)
from starter_agent.cv_workbench.exports import ExportArtifactUnavailableError, ExportCommand
from starter_agent.cv_workbench.interviews import RoundCommand
from starter_agent.cv_workbench.jobs import CandidateCommand
from starter_agent.cv_workbench.jd_analysis import extract_job_analysis
from starter_agent.cv_workbench.jd_ingestion import (
    JobDocumentError,
    MinerUOcrParser,
    parse_job_document,
    parse_resume_document,
)
from starter_agent.cv_workbench.matching import (
    AnalyzeCommand,
    CandidateRequirement,
    RULE_VERSION,
    deterministic_requirements,
)
from starter_agent.cv_workbench.resume_import import ResumeImportCommand
from starter_agent.cv_workbench.resume_profile import extract_resume_profile
from starter_agent.cv_workbench.runtime import WorkbenchRuntime
from starter_agent.cv_workbench.store import (
    ForbiddenError,
    ImmutableObjectError,
    ObjectNotFoundError,
    ReferenceConflictError,
    RevisionConflictError,
    WorkbenchStoreError,
)
from starter_agent.cv_workbench.suggestions import SuggestionCommand
from starter_agent.cv_workbench.tailoring import TailoringCommand
from starter_agent.cv_workbench.versioning import BlockPatch, VersioningError
from starter_agent.cv_workbench.workspaces import (
    CreateWorkspaceCommand,
    WorkspaceProfile,
)
from starter_agent.interfaces.capabilities_api import (
    ManagementPrincipal,
    get_management_principal,
)
from starter_agent.knowledge.models import KnowledgeScope


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceBody(ApiModel):
    workspace_id: str
    name: str
    target_roles: tuple[str, ...] = ()
    target_cities: tuple[str, ...] = ()
    remote_preference: str | None = None
    seniority: str | None = None
    keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()

    def profile(self) -> WorkspaceProfile:
        return WorkspaceProfile(**self.model_dump(exclude={"workspace_id"}))


class WorkspacePatch(ApiModel):
    expected_revision: int = Field(ge=1)
    name: str
    target_roles: tuple[str, ...] = ()
    target_cities: tuple[str, ...] = ()
    remote_preference: str | None = None
    seniority: str | None = None
    keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()

    def profile(self) -> WorkspaceProfile:
        return WorkspaceProfile(**self.model_dump(exclude={"expected_revision"}))


class ResumeImportBody(ApiModel):
    operation_id: str
    idempotency_key: str
    workspace_id: str
    resume_id: str
    branch_id: str
    version_id: str
    resume_name: str
    filename: str
    content: str
    confirmed_authorized: bool


class DraftCreateBody(ApiModel):
    draft_id: str
    workspace_id: str
    branch_id: str


class DraftPatchBody(ApiModel):
    workspace_id: str
    expected_revision: int = Field(ge=1)
    expected_content_sha256: str
    markdown: str | None = None
    block_id: str | None = None
    block_sha256: str | None = None
    replacement: str | None = None


class PendingVersionBody(ApiModel):
    workspace_id: str
    version_id: str
    label: str
    expected_draft_revision: int = Field(ge=1)


class ConfirmVersionBody(ApiModel):
    workspace_id: str
    expected_revision: int = Field(ge=1)


class BranchBody(ApiModel):
    branch_id: str
    resume_id: str
    name: str
    branch_type: ResumeBranchType
    job_snapshot_id: str | None = None


class JobCandidateBody(ApiModel):
    candidate_id: str
    workspace_id: str
    source_kind: Literal["text", "stable_url"] = "text"
    title: str = "Untitled role"
    company: str = "Unknown company"
    location: str | None = None
    filename: str = "job.txt"
    content: str | None = None
    url: str | None = None
    confirmed_authorized: bool = False


class JobDocumentAnalyzeBody(ApiModel):
    filename: str = "job.md"
    content: str = Field(min_length=1, max_length=100_000)


class RetainJobBody(ApiModel):
    candidate_id: str
    workspace_id: str
    operation_id: str
    idempotency_key: str


class MatchBody(ApiModel):
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


class MatchEvaluateBody(ApiModel):
    analysis_id: str
    operation_id: str
    idempotency_key: str
    workspace_id: str
    resume_version_id: str
    job_snapshot_id: str


class SuggestionBody(ApiModel):
    suggestion_id: str
    target_version_id: str
    target_draft_id: str
    target_draft_revision: int
    block_id: str
    original_text: str
    proposed_text: str
    change_type: str
    reason: str
    resume_evidence: tuple[dict, ...]
    requirement_ids: tuple[str, ...]
    risk: str | None = None
    allow_partial_analysis: bool = False
    workspace_id: str


class SuggestionDecisionBody(ApiModel):
    decision: Literal["accept", "reject"]
    workspace_id: str | None = None
    edited_text: str | None = None


class SuggestionGenerateBody(ApiModel):
    workspace_id: str
    draft_id: str


class SuggestionBatchDecisionBody(ApiModel):
    workspace_id: str
    accept_ids: tuple[str, ...] = ()
    reject_ids: tuple[str, ...] = ()
    edited_text_by_id: dict[str, str] = Field(default_factory=dict)


class MergeProposalBody(ApiModel):
    proposal_id: str
    workspace_id: str
    target_branch_id: str
    base_version_id: str
    upstream_version_id: str
    target_version_id: str


class MergeDecisionBody(ApiModel):
    block_id: str
    decision: MergeDecisionType
    expected_revision: int = Field(ge=1)
    manual_content: str | None = None


class MergeCommitBody(ApiModel):
    operation_id: str
    idempotency_key: str
    workspace_id: str


class ViewPreferenceBody(ApiModel):
    node_positions: dict[str, tuple[float, float]] = Field(default_factory=dict)
    collapsed_branch_ids: tuple[str, ...] = ()
    viewport_x: float = 0
    viewport_y: float = 0
    viewport_zoom: float = Field(default=1, gt=0, le=8)
    expected_revision: int | None = Field(default=None, ge=1)


class ResearchRunBody(ApiModel):
    workspace_id: str
    query: str = Field(min_length=3, max_length=5000)
    session_id: UUID | None = None
    target_valid_jobs: int = Field(default=3, ge=1, le=5)
    max_pages: int = Field(default=3, ge=1, le=10)


class ResearchRetainBody(ApiModel):
    workspace_id: str
    candidate_id: str
    operation_id: str
    idempotency_key: str


class ExportBody(ApiModel):
    operation_id: str
    idempotency_key: str
    export_id: str
    workspace_id: str
    resume_version_id: str
    format: Literal["pdf", "docx"]
    template_id: str = "ats-clean"
    template_version: str = "1.0.0"
    settings: dict[str, object] = Field(default_factory=dict)


class ApplicationCreateBody(ApiModel):
    operation_id: str
    idempotency_key: str
    application_id: str
    event_id: str
    workspace_id: str
    job_snapshot_id: str
    resume_version_id: str
    initial_status: ApplicationStatus = ApplicationStatus.TO_DECIDE
    priority: int = Field(default=0, ge=0, le=100)
    next_action: str | None = Field(default=None, max_length=500)
    remind_at: datetime | None = None
    note: str | None = Field(default=None, max_length=2000)
    user_confirmed: bool = False


class ApplicationEventBody(ApiModel):
    operation_id: str
    idempotency_key: str
    event_id: str
    workspace_id: str
    expected_revision: int = Field(ge=1)
    to_status: ApplicationStatus
    note: str | None = Field(default=None, max_length=2000)
    next_action: str | None = Field(default=None, max_length=500)
    remind_at: datetime | None = None
    user_confirmed: bool = False


class ApplicationDetailsBody(ApiModel):
    expected_revision: int = Field(ge=1)
    priority: int = Field(ge=0, le=100)
    next_action: str | None = Field(default=None, max_length=500)
    remind_at: datetime | None = None


class InterviewReviewCreateBody(ApiModel):
    review_id: str
    application_id: str


class InterviewRoundBody(ApiModel):
    expected_revision: int = Field(ge=1)
    round_id: str
    round_type: str = Field(min_length=1, max_length=120)
    occurred_at: datetime
    questions: tuple[str, ...] = ()
    answers: tuple[str, ...] = ()
    feedback: tuple[str, ...] = ()
    result: str | None = Field(default=None, max_length=500)
    improvement_items: tuple[str, ...] = ()
    user_confirmed: bool = False


class InterviewSummaryBody(ApiModel):
    expected_revision: int = Field(ge=1)
    summary_id: str


class InterviewSummaryDecisionBody(ApiModel):
    expected_revision: int = Field(ge=1)
    decision: Literal["accepted", "rejected"]


class WorkbenchApiError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        retryable: bool = False,
        authoritative_revision: int | None = None,
        recovery_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.authoritative_revision = authoritative_revision
        self.recovery_action = recovery_action


def install_workbench_error_handlers(api) -> None:
    @api.exception_handler(WorkbenchApiError)
    async def workbench_error_handler(_request: Request, error: WorkbenchApiError):
        payload = {
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        }
        if error.authoritative_revision is not None:
            payload["authoritative_revision"] = error.authoritative_revision
        if error.recovery_action is not None:
            payload["recovery_action"] = error.recovery_action
        return JSONResponse(status_code=error.status_code, content={"error": payload})


def _translate(error: Exception) -> WorkbenchApiError:
    if isinstance(error, (ObjectNotFoundError, ForbiddenError)):
        return WorkbenchApiError("not_found", "Resource not found.", status_code=404)
    if isinstance(error, RevisionConflictError):
        return WorkbenchApiError(
            "revision_conflict",
            "The resource changed; compare and retry.",
            status_code=409,
            retryable=True,
            authoritative_revision=error.authoritative_revision,
            recovery_action="reload_and_compare",
        )
    if isinstance(error, ImmutableObjectError):
        return WorkbenchApiError("immutable_object", str(error), status_code=409)
    if isinstance(error, ReferenceConflictError):
        return WorkbenchApiError("reference_conflict", str(error), status_code=409)
    code = getattr(error, "code", None) or type(error).__name__.casefold()
    if code == "tailoring_analysis_upgrade_required":
        return WorkbenchApiError(
            str(code),
            "Evidence matching has been upgraded; reanalyze once before tailoring.",
            status_code=409,
            recovery_action="reanalyze_with_current_rule",
        )
    return WorkbenchApiError(str(code), str(error), status_code=422)


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except WorkbenchApiError:
        raise
    except (WorkbenchStoreError, VersioningError, ValueError, RuntimeError) as error:
        raise _translate(error) from error


def _all(runtime, model, principal):
    items, cursor = [], None
    while True:
        page = runtime.store.list(model, principal=principal, cursor=cursor)
        items.extend(page.items)
        if page.next_cursor is None:
            return tuple(items)
        cursor = page.next_cursor


def _page_values(values, *, limit: int, cursor: str | None, identity):
    if limit < 1 or limit > 50:
        raise WorkbenchApiError("invalid_page_limit", "limit must be between 1 and 50.")
    ordered = sorted(values, key=identity)
    start = 0
    if cursor is not None:
        positions = [index for index, item in enumerate(ordered) if identity(item) == cursor]
        if not positions:
            raise WorkbenchApiError("invalid_cursor", "Cursor is not valid.")
        start = positions[0] + 1
    page = ordered[start : start + limit]
    next_cursor = identity(page[-1]) if start + limit < len(ordered) and page else None
    return {"items": tuple(page), "next_cursor": next_cursor}


def create_workbench_router(
    runtime_provider: Callable[[], WorkbenchRuntime],
    application_provider: Callable[[], object] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/workbench", tags=["workbench"])

    def principal(actor: ManagementPrincipal) -> str:
        if actor.subject == "anonymous":
            raise WorkbenchApiError("authentication_required", "Authentication required.", status_code=401)
        return actor.subject

    def research_application(actor: ManagementPrincipal):
        subject = principal(actor)
        if application_provider is None:
            raise WorkbenchApiError("delegated_research_unavailable", "Delegated research runtime is unavailable.", status_code=503)
        application = application_provider()
        scope_user = str(getattr(getattr(application.runtime, "knowledge_scope", None), "user_id", ""))
        if scope_user and subject != scope_user:
            raise WorkbenchApiError("delegated_research_not_found", "Delegated research is unavailable.", status_code=404)
        if application.delegation_route_enabled() is not True:
            raise WorkbenchApiError("delegation_release_gate_not_current", "Delegated research release gate is closed.", status_code=409, recovery_action="publish_delegation_release_decision")
        return application

    def research_candidates(parent_run_id: str, actor: ManagementPrincipal):
        application = research_application(actor)
        parent = application.delegation_store.get_parent(parent_run_id)
        subject = principal(actor)
        if parent is None or parent.principal not in {subject, f"user:{subject}"}:
            raise WorkbenchApiError("research_run_not_found", "Research run not found.", status_code=404)
        tree = application.delegation_store.get_run_tree(parent_run_id)
        values = []
        for link in tree.artifact_links:
            if link.kind != "result_envelope":
                continue
            artifact = application.store.get_tool_artifact_for_principal(link.artifact_ref, principal=parent.principal)
            if artifact is None or not isinstance(artifact.get("content"), str):
                continue
            try:
                envelope = json.loads(artifact["content"])
            except (TypeError, ValueError):
                continue
            for index, raw in enumerate((envelope.get("output") or {}).get("jobs") or []):
                if not isinstance(raw, dict):
                    continue
                candidate_id = "jrc_" + sha256(f"{parent_run_id}\0{link.child_run_id}\0{index}\0{raw.get('content_hash', '')}".encode()).hexdigest()[:24]
                refs = [str(item) for item in raw.get("artifact_refs") or [] if isinstance(item, str)]
                state = str(raw.get("validation_state") or "partial_verified")
                values.append({
                    "candidate_id": candidate_id,
                    "parent_run_id": parent_run_id,
                    "child_run_id": link.child_run_id,
                    "title": str(raw.get("title") or ""),
                    "company": str(raw.get("company") or ""),
                    "location": str(raw.get("location") or ""),
                    "responsibilities": tuple(str(item)[:1000] for item in (raw.get("responsibilities") or [])[:30]),
                    "requirements": tuple(str(item)[:1000] for item in (raw.get("requirements") or [])[:30]),
                    "source_url": str(raw.get("source_url") or ""),
                    "final_url": str(raw.get("final_url") or ""),
                    "content_hash": str(raw.get("content_hash") or ""),
                    "artifact_refs": tuple(refs),
                    "evidence_level": "complete" if state == "verified" and refs else "partial",
                    "candidate_only": True,
                })
        return application, tuple(values)

    @router.get("/workspaces")
    def list_workspaces(limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        page = _call(runtime_provider().workspaces.list, principal=principal(actor), limit=limit, cursor=cursor)
        return {"items": page.items, "next_cursor": page.next_cursor}

    @router.post("/workspaces", status_code=201)
    def create_workspace(body: WorkspaceBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().workspaces.create, CreateWorkspaceCommand(body.workspace_id, body.profile()), principal=principal(actor))

    @router.get("/workspaces/{workspace_id}")
    def get_workspace(workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().workspaces.get, workspace_id, principal=principal(actor))

    @router.patch("/workspaces/{workspace_id}")
    def patch_workspace(workspace_id: str, body: WorkspacePatch, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().workspaces.update_profile, workspace_id, body.profile(), principal=principal(actor), expected_revision=body.expected_revision)

    @router.get("/workspaces/{workspace_id}/home")
    def workspace_home(workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().workspaces.home, workspace_id, principal=principal(actor))

    @router.post("/workspaces/{workspace_id}/active-resume/{resume_id}")
    def replace_active_resume(workspace_id: str, resume_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        archived = _call(runtime_provider().workspaces.replace_active_resume, workspace_id, resume_id, principal=principal(actor))
        return {"active_resume_id": resume_id, "archived_resume_ids": archived}

    @router.post("/resumes/imports", status_code=201)
    def import_resume(body: ResumeImportBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        values = body.model_dump(exclude={"content"}) | {"content": body.content.encode()}
        return _call(runtime_provider().resume_imports.import_resume, ResumeImportCommand(**values), principal=principal(actor))

    @router.post("/resumes/imports/upload", status_code=201)
    async def upload_resume_import(
        operation_id: str = Form(), idempotency_key: str = Form(), workspace_id: str = Form(),
        resume_id: str = Form(), branch_id: str = Form(), version_id: str = Form(),
        resume_name: str = Form(), confirmed_authorized: bool = Form(), file: UploadFile = File(),
        actor: ManagementPrincipal = Depends(get_management_principal),
    ):
        """Import an authorized DOCX/PDF resume through the existing commit flow."""
        try:
            settings = getattr(application_provider(), "settings", None) if application_provider else None
            config = getattr(settings, "job_document_parsing", None)
            token = settings.environment_value(getattr(config, "mineru_token_env", None)) if settings else None
            ocr = MinerUOcrParser(token=token, base_url=config.mineru_base_url, timeout_seconds=config.timeout_seconds) if token and config else None
            source_name = file.filename or "resume"
            parsed = parse_resume_document(filename=source_name, content=await file.read(), ocr=ocr)
            profile = extract_resume_profile(parsed.markdown, source_name)
            extracted_name = profile["name"]
            normalized_filename = f"{source_name.rsplit('.', 1)[0]}.md"
            command = ResumeImportCommand(
                operation_id=operation_id, idempotency_key=idempotency_key,
                workspace_id=workspace_id, resume_id=resume_id, branch_id=branch_id,
                version_id=version_id, resume_name=extracted_name,
                filename=normalized_filename, content=parsed.markdown.encode(),
                confirmed_authorized=confirmed_authorized,
            )
            result = _call(runtime_provider().resume_imports.import_resume, command, principal=principal(actor))
            return {"result": result, "resume_name": extracted_name, "profile": profile, "source_filename": source_name, "extraction_method": parsed.extraction_method}
        except JobDocumentError as error:
            raise WorkbenchApiError(str(error), "Resume file could not be parsed.") from error

    @router.get("/resumes/{resume_id}")
    def get_resume(resume_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, Resume, resume_id, principal=principal(actor))

    @router.get("/resumes/{resume_id}/versions")
    def list_versions(resume_id: str, limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.get, Resume, resume_id, principal=subject)
        values = tuple(item for item in _all(runtime, ResumeVersion, subject) if item.resume_id == resume_id)
        return _page_values(values, limit=limit, cursor=cursor, identity=lambda item: item.version_id)

    @router.get("/resume-versions/{version_id}")
    def get_version(version_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, ResumeVersion, version_id, principal=principal(actor))

    @router.post("/resume-versions/{version_id}/drafts", status_code=201)
    def create_draft(version_id: str, body: DraftCreateBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().versions.create_draft, draft_id=body.draft_id, workspace_id=body.workspace_id, base_version_id=version_id, branch_id=body.branch_id, principal=principal(actor))

    @router.patch("/drafts/{draft_id}")
    def patch_draft(draft_id: str, body: DraftPatchBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); service = runtime_provider().versions
        if body.markdown is not None:
            return _call(service.autosave, draft_id, body.markdown, workspace_id=body.workspace_id, principal=subject, expected_revision=body.expected_revision, expected_content_sha256=body.expected_content_sha256)
        if not (body.block_id and body.block_sha256 and body.replacement is not None):
            raise WorkbenchApiError("invalid_draft_patch", "Provide markdown or a complete block patch.")
        return _call(service.apply_patch, draft_id, BlockPatch(body.block_id, body.block_sha256, body.replacement), workspace_id=body.workspace_id, principal=subject, expected_revision=body.expected_revision, expected_content_sha256=body.expected_content_sha256)

    @router.get("/drafts/{draft_id}")
    def get_draft(draft_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, ResumeDraft, draft_id, principal=principal(actor))

    @router.get("/drafts/{draft_id}/content")
    def get_draft_content(draft_id: str, workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        draft = _call(runtime.store.get, ResumeDraft, draft_id, principal=subject)
        _call(runtime.store.assert_entity_in_workspace, draft_id, workspace_id, principal=subject)
        markdown = _call(runtime.versions.content.read, draft.content, principal=subject, workspace_id=workspace_id)
        return {"draft_id": draft_id, "revision": draft.revision, "content_sha256": draft.content.content_sha256, "markdown": markdown}

    @router.get("/resume-versions/{version_id}/content")
    def get_version_content(version_id: str, workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        version = _call(runtime.store.get, ResumeVersion, version_id, principal=subject)
        _call(runtime.store.assert_entity_in_workspace, version_id, workspace_id, principal=subject)
        markdown = _call(runtime.versions.content.read, version.content, principal=subject, workspace_id=workspace_id)
        return {
            "version_id": version_id,
            "revision": version.revision,
            "content_sha256": version.content.content_sha256,
            "markdown": markdown,
            "profile": extract_resume_profile(markdown, version.label),
        }

    @router.post("/drafts/{draft_id}/versions", status_code=201)
    def save_version(draft_id: str, body: PendingVersionBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().versions.save_pending_version, draft_id, workspace_id=body.workspace_id, version_id=body.version_id, label=body.label, principal=principal(actor), expected_draft_revision=body.expected_draft_revision)

    @router.post("/resume-versions/{version_id}/confirm")
    def confirm_version(version_id: str, body: ConfirmVersionBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.assert_entity_in_workspace, version_id, body.workspace_id, principal=subject)
        return _call(runtime.versions.confirm_version, version_id, principal=subject, expected_revision=body.expected_revision)

    @router.get("/resume-versions/{left}/diff/{right}")
    @router.get("/resume-versions/{left}/compare/{right}")
    def version_diff(left: str, right: str, workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().versions.diff, left, right, workspace_id=workspace_id, principal=principal(actor))

    @router.get("/resumes/{resume_id}/version-map")
    def version_map(resume_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().versions.version_map, resume_id, principal=principal(actor))

    @router.post("/resume-versions/{version_id}/branches", status_code=201)
    def create_branch(version_id: str, body: BranchBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().versions.create_branch, branch_id=body.branch_id, resume_id=body.resume_id, name=body.name, branch_type=body.branch_type, base_version_id=version_id, principal=principal(actor), job_snapshot_id=body.job_snapshot_id)

    @router.get("/resume-versions/{version_id}/upstream-changes")
    def upstream_changes(version_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        version = _call(runtime.store.get, ResumeVersion, version_id, principal=subject)
        version_map = _call(runtime.versions.version_map, version.resume_id, principal=subject)
        node = next(item for item in version_map.nodes if item.version_id == version_id)
        return {"version_id": version_id, "upstream_changes_available": node.upstream_changes_available, "revision": version.revision}

    @router.post("/merge-proposals", status_code=201)
    def create_merge(body: MergeProposalBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().merges.create_proposal, **body.model_dump(), principal=principal(actor))

    @router.get("/merge-proposals/{proposal_id}")
    def get_merge(proposal_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, MergeProposal, proposal_id, principal=principal(actor))

    @router.patch("/merge-proposals/{proposal_id}")
    def decide_merge(proposal_id: str, body: MergeDecisionBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().merges.decide, proposal_id, block_id=body.block_id, decision=body.decision, principal=principal(actor), expected_revision=body.expected_revision, manual_content=body.manual_content)

    @router.get("/resumes/{resume_id}/view-preference")
    def get_view_preference(resume_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor)
        return _call(runtime_provider().store.get, VersionViewPreference, f"vvp_{subject}_{resume_id}", principal=subject)

    @router.put("/resumes/{resume_id}/view-preference")
    def put_view_preference(resume_id: str, body: ViewPreferenceBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.get, Resume, resume_id, principal=subject)
        entity_id = f"vvp_{subject}_{resume_id}"
        from datetime import UTC, datetime
        preference = VersionViewPreference(owner_id=subject, resume_id=resume_id, node_positions=body.node_positions, collapsed_branch_ids=body.collapsed_branch_ids, viewport_x=body.viewport_x, viewport_y=body.viewport_y, viewport_zoom=body.viewport_zoom, revision=(body.expected_revision or 0) + 1, updated_at=datetime.now(UTC))
        try:
            existing = runtime.store.get(VersionViewPreference, entity_id, principal=subject)
        except ObjectNotFoundError:
            if body.expected_revision is not None:
                raise WorkbenchApiError("revision_conflict", "View preference does not exist.", status_code=409, authoritative_revision=0, retryable=True)
            return _call(runtime.store.create, preference.model_copy(update={"revision": 1}), principal=subject)
        expected = body.expected_revision if body.expected_revision is not None else existing.revision
        preference = preference.model_copy(update={"revision": expected + 1})
        return _call(runtime.store.update, preference, principal=subject, expected_revision=expected)

    @router.post("/job-candidates", status_code=201)
    def create_job_candidate(body: JobCandidateBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        service = runtime_provider().jobs; subject = principal(actor)
        if body.source_kind == "stable_url":
            if body.url is None:
                raise WorkbenchApiError("stable_url_required", "URL is required.")
            return _call(service.create_url_candidate, candidate_id=body.candidate_id, workspace_id=body.workspace_id, url=body.url, principal=subject)
        if body.content is None:
            raise WorkbenchApiError("job_content_required", "JD content is required.")
        command = CandidateCommand(body.candidate_id, body.workspace_id, body.title, body.company, body.location, body.filename, body.content.encode(), body.confirmed_authorized)
        return _call(service.create_text_candidate, command, principal=subject)

    @router.post("/job-documents/analyze")
    def analyze_job_document(body: JobDocumentAnalyzeBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        del actor
        return {"filename": body.filename, "markdown": body.content, "analysis": extract_job_analysis(body.content), "extraction_method": "text"}

    @router.post("/job-documents/analyze/upload")
    async def analyze_job_document_upload(
        file: UploadFile = File(),
        actor: ManagementPrincipal = Depends(get_management_principal),
    ):
        """Parse a JD source without persisting a candidate until the user edits and confirms it."""
        try:
            settings = getattr(application_provider(), "settings", None) if application_provider else None
            config = getattr(settings, "job_document_parsing", None)
            token = settings.environment_value(getattr(config, "mineru_token_env", None)) if settings else None
            ocr = MinerUOcrParser(token=token, base_url=config.mineru_base_url, timeout_seconds=config.timeout_seconds) if token and config else None
            parsed = parse_job_document(filename=file.filename or "upload", content=await file.read(), ocr=ocr)
            return {"filename": parsed.filename, "markdown": parsed.markdown, "analysis": extract_job_analysis(parsed.markdown), "extraction_method": parsed.extraction_method}
        except JobDocumentError as error:
            raise WorkbenchApiError(str(error), "JD file could not be parsed.") from error

    @router.post("/job-candidates/upload", status_code=201)
    async def upload_job_candidate(
        candidate_id: str = Form(), workspace_id: str = Form(), title: str = Form(),
        company: str = Form(), location: str | None = Form(default=None),
        confirmed_authorized: bool = Form(), file: UploadFile = File(),
        actor: ManagementPrincipal = Depends(get_management_principal),
    ):
        """Parse an authorized JD file into the existing unconfirmed candidate flow."""
        try:
            settings = getattr(application_provider(), "settings", None) if application_provider else None
            config = getattr(settings, "job_document_parsing", None)
            token = settings.environment_value(getattr(config, "mineru_token_env", None)) if settings else None
            ocr = MinerUOcrParser(token=token, base_url=config.mineru_base_url, timeout_seconds=config.timeout_seconds) if token and config else None
            parsed = parse_job_document(filename=file.filename or "upload", content=await file.read(), ocr=ocr)
            command = CandidateCommand(candidate_id, workspace_id, title, company, location, parsed.filename, parsed.markdown.encode(), confirmed_authorized)
            candidate = _call(runtime_provider().jobs.create_text_candidate, command, principal=principal(actor))
            return {"candidate": candidate, "extraction_method": parsed.extraction_method}
        except JobDocumentError as error:
            raise WorkbenchApiError(str(error), "JD file could not be parsed.") from error

    @router.post("/job-candidates/retain")
    def retain_job(body: RetainJobBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().jobs.confirm_candidate, body.candidate_id, workspace_id=body.workspace_id, operation_id=body.operation_id, idempotency_key=body.idempotency_key, principal=principal(actor))

    @router.post("/research-runs", status_code=202)
    async def start_research(body: ResearchRunBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        application = research_application(actor)
        _call(runtime_provider().store.get, Workspace, body.workspace_id, principal=principal(actor))
        try:
            receipt = await application.start_job_research_delegation(
                message=body.query,
                session_id=body.session_id,
                target_valid_jobs=body.target_valid_jobs,
                max_pages=body.max_pages,
            )
        except RuntimeError as error:
            raise WorkbenchApiError(str(error), "Delegated research dependencies are unavailable.", status_code=503) from error
        return {"parent_run_id": receipt.parent_run_id, "child_task_id": receipt.child_task_id, "child_run_id": receipt.child_run_id, "status": receipt.status, "candidate_only": True}

    @router.get("/research-runs/{parent_run_id}/candidates")
    def list_research_candidates(parent_run_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        _application, values = research_candidates(parent_run_id, actor)
        return {"items": values}

    @router.post("/research-runs/{parent_run_id}/retain")
    def retain_research_candidate(parent_run_id: str, body: ResearchRetainBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        _application, values = research_candidates(parent_run_id, actor)
        value = next((item for item in values if item["candidate_id"] == body.candidate_id), None)
        if value is None:
            raise WorkbenchApiError("research_candidate_not_found", "Research candidate not found.", status_code=404)
        if value["evidence_level"] != "complete":
            raise WorkbenchApiError("research_candidate_partial", "Partial candidates cannot be retained until evidence is complete.", status_code=409)
        markdown = "\n".join((
            f"# {value['title']}", "", f"Company: {value['company']}", f"Location: {value['location']}", "",
            "## Responsibilities", *(f"- {item}" for item in value["responsibilities"]), "",
            "## Requirements", *(f"- {item}" for item in value["requirements"]), "",
            f"Source: {value['final_url']}", "",
        ))
        candidate = _call(
            runtime_provider().jobs.create_research_candidate,
            candidate_id=f"jc_{value['candidate_id']}", workspace_id=body.workspace_id,
            title=value["title"], company=value["company"], location=value["location"] or None,
            markdown=markdown, source_url=value["source_url"], final_url=value["final_url"],
            source_artifact_ref=value["artifact_refs"][0], source_content_sha256=value["content_hash"],
            verified=True, principal=principal(actor),
        )
        return _call(runtime_provider().jobs.confirm_candidate, candidate.candidate_id, workspace_id=body.workspace_id, operation_id=body.operation_id, idempotency_key=body.idempotency_key, principal=principal(actor))

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, Job, job_id, principal=principal(actor))

    @router.get("/jobs")
    def list_jobs(workspace_id: str, limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        page = _call(runtime.store.list_linked, Job, workspace_id, principal=subject, limit=limit, cursor=cursor)
        return {"items": page.items, "next_cursor": page.next_cursor}

    @router.get("/jobs/{job_id}/snapshots")
    def list_job_snapshots(job_id: str, limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.get, Job, job_id, principal=subject)
        values = tuple(item for item in _all(runtime, JobSnapshot, subject) if item.job_id == job_id)
        return _page_values(values, limit=limit, cursor=cursor, identity=lambda item: item.snapshot_id)

    @router.get("/job-snapshots/{snapshot_id}")
    def get_job_snapshot(snapshot_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, JobSnapshot, snapshot_id, principal=principal(actor))

    @router.get("/job-snapshots/{snapshot_id}/content")
    def get_job_snapshot_content(snapshot_id: str, workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        snapshot = _call(runtime.store.get, JobSnapshot, snapshot_id, principal=subject)
        _call(runtime.store.assert_entity_in_workspace, snapshot_id, workspace_id, principal=subject)
        markdown = _call(runtime.versions.content.read, snapshot.content, principal=subject, workspace_id=workspace_id)
        return {"snapshot_id": snapshot_id, "content_sha256": snapshot.content.content_sha256, "markdown": markdown}

    @router.post("/match-analyses", status_code=201)
    def create_match(body: MatchBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().matches.analyze, AnalyzeCommand(**body.model_dump()), principal=principal(actor))

    @router.post("/match-analyses/evaluate", status_code=201)
    def evaluate_match(body: MatchEvaluateBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        version = _call(runtime.store.get, ResumeVersion, body.resume_version_id, principal=subject)
        snapshot = _call(runtime.store.get, JobSnapshot, body.job_snapshot_id, principal=subject)
        _call(runtime.store.assert_entity_in_workspace, version.version_id, body.workspace_id, principal=subject)
        _call(runtime.store.assert_entity_in_workspace, snapshot.snapshot_id, body.workspace_id, principal=subject)
        reusable = tuple(
            item
            for item in _all(runtime, MatchAnalysis, subject)
            if item.workspace_id == body.workspace_id
            and item.resume_version_id == version.version_id
            and item.resume_content_sha256 == version.content.content_sha256
            and item.job_snapshot_id == snapshot.snapshot_id
            and item.job_content_sha256 == snapshot.content.content_sha256
            and item.rule_version == RULE_VERSION
            and item.status in {MatchStatus.VALIDATED, MatchStatus.PARTIAL}
        )
        if reusable:
            analysis = max(
                reusable,
                key=lambda item: (item.created_at, item.analysis_id),
            )
            return analysis.model_dump(mode="json") | {
                "reused": True,
                "rule_upgrade_required": False,
            }
        resume_text = _call(runtime.versions.content.read, version.content, principal=subject, workspace_id=body.workspace_id)
        job_text = _call(runtime.versions.content.read, snapshot.content, principal=subject, workspace_id=body.workspace_id)
        if not (version.content.knowledge_base_id and version.content.document_id and version.content.document_version_id):
            raise WorkbenchApiError("resume_evidence_not_published", "Confirmed resume evidence is unavailable.", status_code=409)
        requirements = _call(
            deterministic_requirements,
            resume_text,
            job_text,
            selector=runtime.evidence_selector,
            scope=KnowledgeScope(user_id=subject, project_id=body.workspace_id),
            knowledge_base_id=UUID(version.content.knowledge_base_id),
            document_id=UUID(version.content.document_id),
        )
        command = AnalyzeCommand(**body.model_dump(), requirements=requirements, complete=True)
        analysis = _call(runtime.matches.analyze, command, principal=subject)
        return analysis.model_dump(mode="json") | {
            "reused": False,
            "rule_upgrade_required": False,
        }

    @router.get("/match-analyses")
    def list_matches(workspace_id: str, limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        values = tuple(item for item in _all(runtime, MatchAnalysis, subject) if item.workspace_id == workspace_id)
        return _page_values(values, limit=limit, cursor=cursor, identity=lambda item: item.analysis_id)

    @router.get("/match-analyses/{analysis_id}")
    def get_match(analysis_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, MatchAnalysis, analysis_id, principal=principal(actor))

    @router.post("/match-analyses/{analysis_id}/suggestions", status_code=201)
    def create_suggestion(analysis_id: str, body: SuggestionBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        from starter_agent.cv_workbench.contracts import EvidenceReference
        values = body.model_dump(exclude={"workspace_id", "resume_evidence"}) | {"analysis_id": analysis_id, "resume_evidence": tuple(EvidenceReference.model_validate(item) for item in body.resume_evidence)}
        return _call(runtime_provider().suggestions.create, SuggestionCommand(**values), workspace_id=body.workspace_id, principal=principal(actor))

    @router.get("/match-analyses/{analysis_id}/suggestions")
    def list_suggestions(analysis_id: str, limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.get, MatchAnalysis, analysis_id, principal=subject)
        values = tuple(item for item in _all(runtime, Suggestion, subject) if item.analysis_id == analysis_id)
        return _page_values(values, limit=limit, cursor=cursor, identity=lambda item: item.suggestion_id)

    @router.post("/match-analyses/{analysis_id}/suggestion-candidates", status_code=201)
    def generate_suggestions(analysis_id: str, body: SuggestionGenerateBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        values = _call(
            runtime_provider().suggestions.generate_safe_candidates,
            analysis_id,
            body.draft_id,
            workspace_id=body.workspace_id,
            principal=principal(actor),
        )
        return {"items": values}

    @router.post(
        "/match-analyses/{analysis_id}/tailored-resume-candidates",
        status_code=201,
    )
    async def generate_tailored_resume(
        analysis_id: str,
        body: SuggestionGenerateBody,
        actor: ManagementPrincipal = Depends(get_management_principal),
    ):
        runtime = runtime_provider()
        if runtime.tailoring is None:
            raise WorkbenchApiError(
                "tailoring_provider_unavailable",
                "AI tailoring is not configured.",
                status_code=503,
                recovery_action="configure_model_provider",
            )
        try:
            return await runtime.tailoring.generate_candidates(
                TailoringCommand(
                    workspace_id=body.workspace_id,
                    analysis_id=analysis_id,
                    draft_id=body.draft_id,
                ),
                principal=principal(actor),
            )
        except WorkbenchApiError:
            raise
        except (WorkbenchStoreError, VersioningError, ValueError, RuntimeError) as error:
            raise _translate(error) from error

    @router.post("/suggestions/batch-decisions")
    def decide_suggestions_batch(
        body: SuggestionBatchDecisionBody,
        actor: ManagementPrincipal = Depends(get_management_principal),
    ):
        subject = principal(actor)
        runtime = runtime_provider()
        accept_ids = tuple(body.accept_ids)
        reject_ids = tuple(body.reject_ids)
        all_ids = accept_ids + reject_ids
        if (
            not all_ids
            or len(set(all_ids)) != len(all_ids)
            or not set(body.edited_text_by_id).issubset(accept_ids)
        ):
            raise WorkbenchApiError(
                "invalid_suggestion_batch",
                "Suggestion IDs must be unique and edits may target accepted IDs only.",
            )
        suggestions = tuple(
            _call(runtime.store.get, Suggestion, item, principal=subject)
            for item in all_ids
        )
        if any(item.status != SuggestionStatus.PENDING for item in suggestions):
            raise WorkbenchApiError(
                "suggestion_not_pending",
                "All selected suggestions must still be pending.",
                status_code=409,
                recovery_action="reload_suggestions",
            )
        for item in suggestions:
            _call(
                runtime.store.assert_entity_in_workspace,
                item.target_draft_id,
                body.workspace_id,
                principal=subject,
            )

        batch = None
        if accept_ids:
            batch = _call(
                runtime.suggestions.apply_batch,
                accept_ids,
                workspace_id=body.workspace_id,
                principal=subject,
                edited_text_by_id=body.edited_text_by_id,
                preserve_pending_ids=reject_ids,
            )
        rejected = tuple(
            _call(runtime.suggestions.reject, item, principal=subject)
            for item in reject_ids
        )
        return {
            "draft": batch.draft if batch is not None else None,
            "accepted_ids": batch.accepted_ids if batch is not None else (),
            "rejected_ids": tuple(item.suggestion_id for item in rejected),
            "invalidated_ids": batch.invalidated_ids if batch is not None else (),
        }

    @router.post("/suggestions/{suggestion_id}/decisions")
    def decide_suggestion(suggestion_id: str, body: SuggestionDecisionBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); service = runtime_provider().suggestions
        if body.decision == "reject":
            return _call(service.reject, suggestion_id, principal=subject)
        if body.workspace_id is None:
            raise WorkbenchApiError("workspace_required", "workspace_id is required for acceptance.")
        return _call(service.accept, suggestion_id, workspace_id=body.workspace_id, principal=subject, edited_text=body.edited_text)

    @router.get("/operations/{operation_id}")
    def get_operation(operation_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, BusinessOperation, operation_id, principal=principal(actor))

    @router.get("/operations")
    def list_operations(workspace_id: str, limit: int = 50, cursor: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.get, Workspace, workspace_id, principal=subject)
        values = tuple(item for item in _all(runtime, BusinessOperation, subject) if item.workspace_id == workspace_id)
        return _page_values(values, limit=limit, cursor=cursor, identity=lambda item: item.operation_id)

    @router.post("/operations/{operation_id}/retry-commit")
    def retry_operation(operation_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        principal(actor)
        raise WorkbenchApiError("operation_retry_context_unavailable", "Retry through the owning resource command.", status_code=409, retryable=True, recovery_action="retry_owning_command")

    @router.post("/merge-proposals/{proposal_id}/commit")
    def commit_merge(proposal_id: str, body: MergeCommitBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(
            runtime_provider().merges.commit_proposal,
            proposal_id,
            operation_id=body.operation_id,
            idempotency_key=body.idempotency_key,
            workspace_id=body.workspace_id,
            principal=principal(actor),
        )

    @router.get("/applications")
    def list_applications(workspace_id: str, status: ApplicationStatus | None = None, query: str | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        subject = principal(actor); runtime = runtime_provider()
        _call(runtime.store.get, Workspace, workspace_id, principal=subject)
        return {"items": _call(runtime.applications.list, workspace_id, principal=subject, status=status, query=query)}

    @router.post("/applications", status_code=201)
    def create_application(body: ApplicationCreateBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        result = _call(runtime_provider().applications.create, CreateApplicationCommand(**body.model_dump()), principal=principal(actor))
        return {"operation": result.operation, "application": result.application}

    @router.get("/applications/{application_id}")
    def get_application(application_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, Application, application_id, principal=principal(actor))

    @router.patch("/applications/{application_id}")
    def update_application_details(application_id: str, body: ApplicationDetailsBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().applications.update_details, application_id, ApplicationDetailsCommand(**body.model_dump()), principal=principal(actor))

    @router.post("/applications/{application_id}/events", status_code=201)
    def append_application_event(application_id: str, body: ApplicationEventBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        result = _call(runtime_provider().applications.append_status, application_id, ApplicationEventCommand(**body.model_dump()), principal=principal(actor))
        return {"operation": result.operation, "application": result.application}

    @router.get("/applications/{application_id}/interview-review")
    def get_interview_review(application_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().interviews.for_application, application_id, principal=principal(actor))

    @router.post("/interview-reviews", status_code=201)
    def create_interview_review(body: InterviewReviewCreateBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().interviews.create, body.review_id, body.application_id, principal=principal(actor))

    @router.get("/interview-reviews/{review_id}")
    def get_interview_review_by_id(review_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, InterviewReview, review_id, principal=principal(actor))

    @router.post("/interview-reviews/{review_id}/rounds", status_code=201)
    def add_interview_round(review_id: str, body: InterviewRoundBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().interviews.add_round, review_id, RoundCommand(**body.model_dump()), principal=principal(actor))

    @router.post("/interview-reviews/{review_id}/summary-candidates", status_code=201)
    def propose_interview_summary(review_id: str, body: InterviewSummaryBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().interviews.propose_summary, review_id, body.summary_id, expected_revision=body.expected_revision, principal=principal(actor))

    @router.post("/interview-reviews/{review_id}/summary-candidates/{summary_id}/decision")
    def decide_interview_summary(review_id: str, summary_id: str, body: InterviewSummaryDecisionBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().interviews.decide_summary, review_id, summary_id, expected_revision=body.expected_revision, decision=body.decision, principal=principal(actor))

    @router.delete("/interview-reviews/{review_id}", status_code=204)
    def delete_interview_review(review_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        _call(runtime_provider().interviews.delete, review_id, principal=principal(actor))
        return Response(status_code=204)

    @router.post("/exports", status_code=201)
    def create_export(body: ExportBody, actor: ManagementPrincipal = Depends(get_management_principal)):
        result = _call(runtime_provider().exports.export, ExportCommand(**body.model_dump()), principal=principal(actor))
        return {"operation": result.operation, "export": result.record}

    @router.get("/export-templates")
    def list_export_templates(actor: ManagementPrincipal = Depends(get_management_principal)):
        principal(actor)
        return {"items": runtime_provider().exports.templates()}

    @router.get("/analytics/funnel")
    def application_funnel(workspace_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().analytics.funnel, workspace_id, principal=principal(actor))

    @router.get("/reminders")
    def application_reminders(workspace_id: str, before: datetime | None = None, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().analytics.reminders, workspace_id, principal=principal(actor), before=before)

    @router.get("/exports/{export_id}")
    def get_export(export_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        return _call(runtime_provider().store.get, ExportRecord, export_id, principal=principal(actor))

    @router.get("/exports/{export_id}/download")
    def download_export(export_id: str, actor: ManagementPrincipal = Depends(get_management_principal)):
        try:
            artifact = runtime_provider().exports.download(export_id, principal=principal(actor))
        except ExportArtifactUnavailableError as error:
            code = str(error)
            raise WorkbenchApiError(
                code,
                "Export download is unavailable.",
                status_code=410 if code == "export_download_expired" else 404,
                recovery_action="create_new_export",
            ) from error
        return Response(
            content=artifact.content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{artifact.filename}"',
                "ETag": f'"{artifact.content_sha256}"',
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
