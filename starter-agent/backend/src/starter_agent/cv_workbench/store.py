"""SQLite persistence boundary for CV workbench business objects.

The store owns only workbench projections and references. Knowledge bodies,
Agent messages, Run payloads, and Artifact content stay in their existing
stores. All public reads and writes require a trusted principal argument.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, Iterable, TypeVar

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    event,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.exc import IntegrityError

from starter_agent.cv_workbench.contracts import (
    Application,
    BusinessOperation,
    ContractModel,
    ExportStatus,
    ExportRecord,
    InterviewReview,
    Job,
    JobCandidate,
    JobSnapshot,
    MatchAnalysis,
    MatchStatus,
    MergeProposal,
    MergeProposalStatus,
    OperationStatus,
    Resume,
    ResumeBranch,
    ResumeDraft,
    ResumeDraftStatus,
    ResumeVersion,
    ResumeVersionStatus,
    Suggestion,
    SuggestionStatus,
    VersionViewPreference,
    Workspace,
    assert_transition,
)


SCHEMA_VERSION = 5
T = TypeVar("T", bound=ContractModel)


class WorkbenchStoreError(RuntimeError):
    code = "workbench_store_error"


class ObjectNotFoundError(WorkbenchStoreError):
    code = "not_found"


class ObjectAlreadyExistsError(WorkbenchStoreError):
    code = "already_exists"


class ForbiddenError(WorkbenchStoreError):
    code = "forbidden"


class RevisionConflictError(WorkbenchStoreError):
    code = "revision_conflict"

    def __init__(self, object_id: str, authoritative_revision: int) -> None:
        super().__init__(
            f"revision_conflict:{object_id}:authoritative={authoritative_revision}"
        )
        self.object_id = object_id
        self.authoritative_revision = authoritative_revision


class IdempotencyConflictError(WorkbenchStoreError):
    code = "idempotency_conflict"


class ImmutableObjectError(WorkbenchStoreError):
    code = "immutable_object"


class ReferenceConflictError(WorkbenchStoreError):
    code = "reference_conflict"


class LineageConflictError(WorkbenchStoreError):
    code = "lineage_conflict"


class WorkbenchBase(DeclarativeBase):
    pass


class SchemaMigrationRow(WorkbenchBase):
    __tablename__ = "cv_workbench_schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(40))


class EntityRow(WorkbenchBase):
    __tablename__ = "cv_workbench_entities"
    __table_args__ = (
        Index("ix_cv_workbench_entities_kind_created", "entity_type", "created_at", "id"),
        Index("ix_cv_workbench_entities_owner_workspace", "owner_id", "workspace_id"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(80), index=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    immutable: Mapped[bool] = mapped_column(Boolean)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    updated_at: Mapped[str] = mapped_column(String(40))


class EntityReferenceRow(WorkbenchBase):
    __tablename__ = "cv_workbench_entity_references"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "kind", name="uq_cv_workbench_reference"),
        Index("ix_cv_workbench_reference_target", "target_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[str] = mapped_column(String(40))


class WorkspaceMembershipRow(WorkbenchBase):
    __tablename__ = "cv_workbench_workspace_memberships"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "entity_id", name="uq_cv_workbench_workspace_membership"
        ),
        Index("ix_cv_workbench_membership_workspace_kind", "workspace_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), index=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[str] = mapped_column(String(40))


class ResumeLineageRow(WorkbenchBase):
    __tablename__ = "cv_workbench_resume_lineage"
    __table_args__ = (
        Index("ix_cv_workbench_lineage_resume_branch", "resume_id", "branch_id"),
        Index("ix_cv_workbench_lineage_parent", "parent_version_id"),
    )

    version_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), primary_key=True
    )
    resume_id: Mapped[str] = mapped_column(String(160), index=True)
    branch_id: Mapped[str] = mapped_column(String(160), index=True)
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("cv_workbench_resume_lineage.version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    branch_base_version_id: Mapped[str] = mapped_column(String(160), index=True)
    node_type: Mapped[str] = mapped_column(String(40), index=True)
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)


class OperationKeyRow(WorkbenchBase):
    __tablename__ = "cv_workbench_operation_keys"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "operation_type",
            "idempotency_key",
            name="uq_cv_workbench_operation_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), unique=True
    )
    workspace_id: Mapped[str] = mapped_column(String(160), index=True)
    operation_type: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    input_sha256: Mapped[str] = mapped_column(String(64))


class OperationCheckpointRow(WorkbenchBase):
    __tablename__ = "cv_workbench_operation_checkpoints"

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), primary_key=True
    )
    result_ref: Mapped[str] = mapped_column(String(500))
    result_sha256: Mapped[str] = mapped_column(String(64))
    validator_version: Mapped[str] = mapped_column(String(120))
    evidence_refs_json: Mapped[str] = mapped_column(Text)
    safety_summary_json: Mapped[str] = mapped_column(Text)
    partial: Mapped[bool] = mapped_column(Boolean, default=False)
    commit_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_commit_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40))


class EvidenceBindingRow(WorkbenchBase):
    __tablename__ = "cv_workbench_evidence_bindings"
    __table_args__ = (
        UniqueConstraint(
            "subject_id", "source_kind", "source_ref", name="uq_cv_workbench_evidence_binding"
        ),
        Index("ix_cv_workbench_binding_workspace_subject", "workspace_id", "subject_id"),
    )

    binding_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), index=True
    )
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), index=True
    )
    source_kind: Mapped[str] = mapped_column(String(40))
    source_ref: Mapped[str] = mapped_column(String(500))
    expected_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    safe_summary_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))
    checked_at: Mapped[str] = mapped_column(String(40))


class ResumeImportStagingRow(WorkbenchBase):
    __tablename__ = "cv_workbench_resume_import_staging"

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), primary_key=True
    )
    resume_id: Mapped[str] = mapped_column(String(160))
    branch_id: Mapped[str] = mapped_column(String(160))
    version_id: Mapped[str] = mapped_column(String(160))
    resume_name: Mapped[str] = mapped_column(String(200))
    normalized_sha256: Mapped[str] = mapped_column(String(64))
    raw_artifact_ref: Mapped[str] = mapped_column(String(500))
    raw_sha256: Mapped[str] = mapped_column(String(64))
    knowledge_base_id: Mapped[str] = mapped_column(String(160))
    document_id: Mapped[str] = mapped_column(String(160))
    document_version_id: Mapped[str] = mapped_column(String(160))
    parser_version: Mapped[str] = mapped_column(String(80))
    projection_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class BusinessEventRow(WorkbenchBase):
    __tablename__ = "cv_workbench_business_events"
    __table_args__ = (
        UniqueConstraint("entity_id", "sequence", name="uq_cv_workbench_event_sequence"),
        Index("ix_cv_workbench_events_entity", "entity_id", "sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("cv_workbench_entities.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(120))
    actor: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[str] = mapped_column(String(40))


MODEL_TYPES: dict[str, type[ContractModel]] = {
    model.__name__: model
    for model in (
        Workspace,
        Resume,
        ResumeBranch,
        ResumeVersion,
        ResumeDraft,
        MergeProposal,
        JobCandidate,
        Job,
        JobSnapshot,
        MatchAnalysis,
        Suggestion,
        Application,
        ExportRecord,
        InterviewReview,
        BusinessOperation,
        VersionViewPreference,
    )
}

ID_FIELDS: dict[str, str] = {
    "Workspace": "workspace_id",
    "Resume": "resume_id",
    "ResumeBranch": "branch_id",
    "ResumeVersion": "version_id",
    "ResumeDraft": "draft_id",
    "MergeProposal": "proposal_id",
    "JobCandidate": "candidate_id",
    "Job": "job_id",
    "JobSnapshot": "snapshot_id",
    "MatchAnalysis": "analysis_id",
    "Suggestion": "suggestion_id",
    "Application": "application_id",
    "ExportRecord": "export_id",
    "InterviewReview": "review_id",
    "BusinessOperation": "operation_id",
}


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class StoredEvent:
    sequence: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True)
class OperationCheckpoint:
    operation_id: str
    result_ref: str
    result_sha256: str
    validator_version: str
    evidence_refs: tuple[str, ...]
    safety_summary: dict[str, Any]
    partial: bool
    commit_attempts: int
    last_commit_error: str | None
    updated_at: datetime


@dataclass(frozen=True)
class StoredEvidenceBinding:
    binding_id: str
    workspace_id: str
    subject_id: str
    source_kind: str
    source_ref: str
    expected_sha256: str | None
    status: str
    safe_summary: dict[str, Any]
    created_at: datetime
    checked_at: datetime


@dataclass(frozen=True)
class ResumeImportStaging:
    operation_id: str
    resume_id: str
    branch_id: str
    version_id: str
    resume_name: str
    normalized_sha256: str
    raw_artifact_ref: str
    raw_sha256: str
    knowledge_base_id: str
    document_id: str
    document_version_id: str
    parser_version: str
    projection: dict[str, Any]
    created_at: datetime


def _sqlite_path(database_url: str, project_root: Path) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        return None
    raw = Path(database_url.removeprefix(prefix))
    return raw if raw.is_absolute() else project_root / raw


def _cursor(created_at: str, entity_id: str) -> str:
    raw = json.dumps([created_at, entity_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return str(decoded[0]), str(decoded[1])
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkbenchStoreError("invalid_cursor") from exc


class SQLiteWorkbenchStore:
    def __init__(self, database_url: str, project_root: Path) -> None:
        path = _sqlite_path(database_url, project_root)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(database_url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine, "connect", self._configure_sqlite)
        WorkbenchBase.metadata.create_all(self.engine)
        self._record_schema_version()

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA secure_delete=ON")
        cursor.close()

    def _record_schema_version(self) -> None:
        with Session(self.engine) as db, db.begin():
            for version in range(1, SCHEMA_VERSION + 1):
                if db.get(SchemaMigrationRow, version) is None:
                    db.add(
                        SchemaMigrationRow(
                            version=version,
                            applied_at=datetime.now(UTC).isoformat(),
                        )
                    )

    @staticmethod
    def _identity(model: ContractModel) -> tuple[str, str]:
        entity_type = type(model).__name__
        field = ID_FIELDS.get(entity_type)
        if field is None:
            if isinstance(model, VersionViewPreference):
                return entity_type, f"vvp_{model.owner_id}_{model.resume_id}"
            raise WorkbenchStoreError(f"unsupported_entity_type:{entity_type}")
        return entity_type, str(getattr(model, field))

    @staticmethod
    def _revision(model: ContractModel) -> int:
        revision = getattr(model, "revision", 1)
        return int(revision)

    @staticmethod
    def _status(model: ContractModel) -> str | None:
        status = SQLiteWorkbenchStore._state(model)
        return None if status is None else str(status)

    @staticmethod
    def _state(model: ContractModel):
        for field in ("status", "user_status", "current_status"):
            value = getattr(model, field, None)
            if value is not None:
                return value
        return None

    @staticmethod
    def _is_archived(model: ContractModel) -> bool:
        if getattr(model, "archived", False) is True:
            return True
        state = SQLiteWorkbenchStore._state(model)
        return state is not None and str(state) == "archived"

    @staticmethod
    def _timestamps(model: ContractModel) -> tuple[str, str]:
        now = datetime.now(UTC).isoformat()
        created = getattr(model, "created_at", None)
        updated = getattr(model, "updated_at", None)
        return (
            created.isoformat() if isinstance(created, datetime) else now,
            updated.isoformat() if isinstance(updated, datetime) else now,
        )

    @staticmethod
    def _workspace_id(model: ContractModel, supplied: str | None) -> str | None:
        value = getattr(model, "workspace_id", None)
        if isinstance(model, Workspace):
            return model.workspace_id
        return str(value) if value is not None else supplied

    @staticmethod
    def _validate_owner(model: ContractModel, principal: str) -> None:
        payload_owner = getattr(model, "owner_id", None)
        if payload_owner is not None and str(payload_owner) != principal:
            raise ForbiddenError("payload_owner_does_not_match_principal")

    def create(
        self,
        model: T,
        *,
        principal: str,
        workspace_id: str | None = None,
    ) -> T:
        self._validate_owner(model, principal)
        entity_type, entity_id = self._identity(model)
        with Session(self.engine) as db, db.begin():
            if db.get(EntityRow, entity_id) is not None:
                raise ObjectAlreadyExistsError(entity_id)
            self._create_in_session(db, model, principal, workspace_id)
        return model

    def create_or_get_operation(
        self,
        operation: BusinessOperation,
        *,
        principal: str,
    ) -> tuple[BusinessOperation, bool]:
        self._validate_owner(operation, principal)
        try:
            with Session(self.engine) as db, db.begin():
                key = db.scalar(
                    select(OperationKeyRow).where(
                        OperationKeyRow.workspace_id == operation.workspace_id,
                        OperationKeyRow.operation_type == operation.operation_type,
                        OperationKeyRow.idempotency_key == operation.idempotency_key,
                    )
                )
                if key is not None:
                    row = self._owned_row(db, key.operation_id, principal)
                    if key.input_sha256 != operation.input_sha256:
                        raise IdempotencyConflictError(operation.idempotency_key)
                    return self._parse(row, BusinessOperation), False
                if db.get(EntityRow, operation.operation_id) is not None:
                    raise ObjectAlreadyExistsError(operation.operation_id)
                self._create_in_session(
                    db, operation, principal, operation.workspace_id
                )
                db.flush()
                db.add(
                    OperationKeyRow(
                        operation_id=operation.operation_id,
                        workspace_id=operation.workspace_id,
                        operation_type=operation.operation_type,
                        idempotency_key=operation.idempotency_key,
                        input_sha256=operation.input_sha256,
                    )
                )
        except IntegrityError as exc:
            with Session(self.engine) as db:
                key = db.scalar(
                    select(OperationKeyRow).where(
                        OperationKeyRow.workspace_id == operation.workspace_id,
                        OperationKeyRow.operation_type == operation.operation_type,
                        OperationKeyRow.idempotency_key == operation.idempotency_key,
                    )
                )
                if key is None:
                    raise WorkbenchStoreError("operation_create_integrity_error") from exc
                row = self._owned_row(db, key.operation_id, principal)
                if key.input_sha256 != operation.input_sha256:
                    raise IdempotencyConflictError(operation.idempotency_key) from exc
                return self._parse(row, BusinessOperation), False
        return operation, True

    def _create_in_session(
        self,
        db: Session,
        model: T,
        principal: str,
        workspace_id: str | None,
    ) -> EntityRow:
        entity_type, entity_id = self._identity(model)
        created_at, updated_at = self._timestamps(model)
        row = EntityRow(
            id=entity_id,
            entity_type=entity_type,
            owner_id=principal,
            workspace_id=self._workspace_id(model, workspace_id),
            revision=self._revision(model),
            status=self._status(model),
            immutable=self._is_immutable(model),
            archived=self._is_archived(model),
            payload_json=model.model_dump_json(),
            created_at=created_at,
            updated_at=updated_at,
        )
        db.add(row)
        db.flush()
        if isinstance(model, ResumeVersion):
            self._insert_lineage(db, model, principal)
        self._replace_references(db, row, model, principal)
        if isinstance(model, Application):
            self._append_application_events(db, row, (), model.events, principal)
        return row

    @staticmethod
    def _is_immutable(model: ContractModel) -> bool:
        if isinstance(model, (JobCandidate, JobSnapshot)):
            return True
        if isinstance(model, ResumeDraft):
            return model.status in {
                ResumeDraftStatus.SAVED,
                ResumeDraftStatus.DISCARDED,
            }
        if isinstance(model, ResumeVersion):
            return model.status in {
                ResumeVersionStatus.CONFIRMED,
                ResumeVersionStatus.ARCHIVED,
                ResumeVersionStatus.FAILED,
            }
        if isinstance(model, MergeProposal):
            return model.status in {
                MergeProposalStatus.COMMITTED,
                MergeProposalStatus.STALE,
                MergeProposalStatus.FAILED,
                MergeProposalStatus.CANCELLED,
            }
        if isinstance(model, BusinessOperation):
            return model.status in {
                OperationStatus.COMMITTED,
                OperationStatus.FAILED,
                OperationStatus.REJECTED,
                OperationStatus.CANCELLED,
            }
        if isinstance(model, ExportRecord):
            return model.status in {
                ExportStatus.AVAILABLE,
                ExportStatus.FAILED,
                ExportStatus.CANCELLED,
            }
        if isinstance(model, Suggestion):
            return model.status in {
                SuggestionStatus.ACCEPTED,
                SuggestionStatus.REJECTED,
                SuggestionStatus.INVALIDATED,
            }
        if isinstance(model, MatchAnalysis):
            return model.status in {MatchStatus.STALE, MatchStatus.FAILED}
        return False

    def get(self, model_type: type[T], entity_id: str, *, principal: str) -> T:
        with Session(self.engine) as db:
            row = self._owned_row(db, entity_id, principal)
            return self._parse(row, model_type)

    def delete_owned_unreferenced(
        self, model_type: type[T], entity_id: str, *, principal: str
    ) -> None:
        """Permanently delete a user-owned projection when nothing references it."""
        with Session(self.engine) as db, db.begin():
            row = self._owned_row(db, entity_id, principal)
            if row.entity_type != model_type.__name__:
                raise ObjectNotFoundError(entity_id)
            incoming = db.scalar(
                select(func.count()).select_from(EntityReferenceRow).where(
                    EntityReferenceRow.target_id == entity_id
                )
            )
            if incoming:
                raise ReferenceConflictError(f"entity_is_referenced:{entity_id}")
            db.delete(row)

    def list(
        self,
        model_type: type[T],
        *,
        principal: str,
        workspace_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        include_archived: bool = False,
    ) -> Page[T]:
        if limit < 1 or limit > 50:
            raise WorkbenchStoreError("invalid_page_limit")
        query = select(EntityRow).where(
            EntityRow.entity_type == model_type.__name__,
            EntityRow.owner_id == principal,
        )
        if workspace_id is not None:
            query = query.where(EntityRow.workspace_id == workspace_id)
        if not include_archived:
            query = query.where(EntityRow.archived.is_(False))
        if cursor:
            created_at, entity_id = _decode_cursor(cursor)
            query = query.where(
                (EntityRow.created_at > created_at)
                | ((EntityRow.created_at == created_at) & (EntityRow.id > entity_id))
            )
        query = query.order_by(EntityRow.created_at, EntityRow.id).limit(limit + 1)
        with Session(self.engine) as db:
            rows = list(db.scalars(query))
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            _cursor(page_rows[-1].created_at, page_rows[-1].id)
            if has_more and page_rows
            else None
        )
        return Page(
            items=tuple(self._parse(row, model_type) for row in page_rows),
            next_cursor=next_cursor,
        )

    def link_to_workspace(
        self,
        workspace_id: str,
        entity_id: str,
        *,
        principal: str,
    ) -> None:
        """Idempotently attach a reusable Resume or Job to a Workspace."""

        with Session(self.engine) as db, db.begin():
            workspace = self._owned_row(db, workspace_id, principal)
            entity = self._owned_row(db, entity_id, principal)
            if workspace.entity_type != "Workspace":
                raise WorkbenchStoreError("membership_target_is_not_workspace")
            if entity.entity_type not in {"Resume", "Job"}:
                raise WorkbenchStoreError("unsupported_workspace_membership")
            existing = db.scalar(
                select(WorkspaceMembershipRow).where(
                    WorkspaceMembershipRow.workspace_id == workspace_id,
                    WorkspaceMembershipRow.entity_id == entity_id,
                )
            )
            if existing is None:
                db.add(
                    WorkspaceMembershipRow(
                        workspace_id=workspace_id,
                        entity_id=entity_id,
                        kind=entity.entity_type,
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )

    def list_linked(
        self,
        model_type: type[T],
        workspace_id: str,
        *,
        principal: str,
        limit: int = 50,
        cursor: str | None = None,
        include_archived: bool = False,
    ) -> Page[T]:
        if model_type not in {Resume, Job}:
            raise WorkbenchStoreError("unsupported_workspace_membership_type")
        if limit < 1 or limit > 50:
            raise WorkbenchStoreError("invalid_page_limit")
        query = (
            select(EntityRow)
            .join(
                WorkspaceMembershipRow,
                WorkspaceMembershipRow.entity_id == EntityRow.id,
            )
            .where(
                WorkspaceMembershipRow.workspace_id == workspace_id,
                WorkspaceMembershipRow.kind == model_type.__name__,
                EntityRow.owner_id == principal,
            )
        )
        if not include_archived:
            query = query.where(EntityRow.archived.is_(False))
        if cursor:
            created_at, entity_id = _decode_cursor(cursor)
            query = query.where(
                (EntityRow.created_at > created_at)
                | ((EntityRow.created_at == created_at) & (EntityRow.id > entity_id))
            )
        query = query.order_by(EntityRow.created_at, EntityRow.id).limit(limit + 1)
        with Session(self.engine) as db:
            workspace = self._owned_row(db, workspace_id, principal)
            if workspace.entity_type != "Workspace":
                raise WorkbenchStoreError("membership_target_is_not_workspace")
            rows = list(db.scalars(query))
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = (
            _cursor(page_rows[-1].created_at, page_rows[-1].id)
            if has_more and page_rows
            else None
        )
        return Page(
            items=tuple(self._parse(row, model_type) for row in page_rows),
            next_cursor=next_cursor,
        )

    def assert_entity_in_workspace(
        self, entity_id: str, workspace_id: str, *, principal: str
    ) -> None:
        with Session(self.engine) as db:
            workspace = self._owned_row(db, workspace_id, principal)
            entity = self._owned_row(db, entity_id, principal)
            if workspace.entity_type != "Workspace":
                raise WorkbenchStoreError("binding_workspace_invalid")
            if entity.workspace_id == workspace_id or entity.id == workspace_id:
                return
            linked_id: str | None = None
            if entity.entity_type in {"Resume", "Job"}:
                linked_id = entity.id
            elif entity.entity_type in {"ResumeVersion", "ResumeBranch", "ResumeDraft"}:
                linked_id = str(json.loads(entity.payload_json)["resume_id"])
            elif entity.entity_type == "JobSnapshot":
                linked_id = str(json.loads(entity.payload_json)["job_id"])
            if linked_id is not None:
                membership = db.scalar(
                    select(WorkspaceMembershipRow).where(
                        WorkspaceMembershipRow.workspace_id == workspace_id,
                        WorkspaceMembershipRow.entity_id == linked_id,
                    )
                )
                if membership is not None:
                    return
            raise ForbiddenError("entity_not_in_workspace")

    def save_evidence_binding(
        self,
        *,
        binding_id: str,
        workspace_id: str,
        subject_id: str,
        source_kind: str,
        source_ref: str,
        expected_sha256: str | None,
        status: str,
        safe_summary: dict[str, Any],
        principal: str,
        checked_at: datetime,
    ) -> StoredEvidenceBinding:
        self.assert_entity_in_workspace(subject_id, workspace_id, principal=principal)
        with Session(self.engine) as db, db.begin():
            existing = db.scalar(
                select(EvidenceBindingRow).where(
                    EvidenceBindingRow.subject_id == subject_id,
                    EvidenceBindingRow.source_kind == source_kind,
                    EvidenceBindingRow.source_ref == source_ref,
                )
            )
            if existing is None:
                existing = EvidenceBindingRow(
                    binding_id=binding_id,
                    workspace_id=workspace_id,
                    subject_id=subject_id,
                    source_kind=source_kind,
                    source_ref=source_ref,
                    expected_sha256=expected_sha256,
                    status=status,
                    safe_summary_json=json.dumps(
                        safe_summary, ensure_ascii=False, sort_keys=True
                    ),
                    created_at=checked_at.isoformat(),
                    checked_at=checked_at.isoformat(),
                )
                db.add(existing)
            else:
                if (
                    existing.workspace_id != workspace_id
                    or existing.expected_sha256 != expected_sha256
                ):
                    raise IdempotencyConflictError(f"evidence_binding:{binding_id}")
                existing.status = status
                existing.safe_summary_json = json.dumps(
                    safe_summary, ensure_ascii=False, sort_keys=True
                )
                existing.checked_at = checked_at.isoformat()
            db.flush()
            return self._evidence_binding(existing)

    def list_evidence_bindings(
        self, subject_id: str, *, principal: str
    ) -> tuple[StoredEvidenceBinding, ...]:
        with Session(self.engine) as db:
            self._owned_row(db, subject_id, principal)
            rows = db.scalars(
                select(EvidenceBindingRow)
                .where(EvidenceBindingRow.subject_id == subject_id)
                .order_by(EvidenceBindingRow.created_at, EvidenceBindingRow.binding_id)
            ).all()
            return tuple(self._evidence_binding(row) for row in rows)

    def save_resume_import_staging(
        self, staging: ResumeImportStaging, *, principal: str
    ) -> ResumeImportStaging:
        with Session(self.engine) as db, db.begin():
            operation = self._owned_row(db, staging.operation_id, principal)
            if operation.entity_type != "BusinessOperation":
                raise WorkbenchStoreError("import_staging_target_is_not_operation")
            existing = db.get(ResumeImportStagingRow, staging.operation_id)
            payload = {
                "resume_id": staging.resume_id,
                "branch_id": staging.branch_id,
                "version_id": staging.version_id,
                "resume_name": staging.resume_name,
                "normalized_sha256": staging.normalized_sha256,
                "raw_artifact_ref": staging.raw_artifact_ref,
                "raw_sha256": staging.raw_sha256,
                "knowledge_base_id": staging.knowledge_base_id,
                "document_id": staging.document_id,
                "document_version_id": staging.document_version_id,
                "parser_version": staging.parser_version,
                "projection_json": json.dumps(
                    staging.projection, ensure_ascii=False, sort_keys=True
                ),
            }
            if existing is not None:
                current = self._resume_import_staging(existing)
                if current != staging:
                    raise IdempotencyConflictError(
                        f"resume_import_staging:{staging.operation_id}"
                    )
                return current
            row = ResumeImportStagingRow(
                operation_id=staging.operation_id,
                **payload,
                created_at=staging.created_at.isoformat(),
            )
            db.add(row)
            db.flush()
            return self._resume_import_staging(row)

    def get_resume_import_staging(
        self, operation_id: str, *, principal: str
    ) -> ResumeImportStaging | None:
        with Session(self.engine) as db:
            self._owned_row(db, operation_id, principal)
            row = db.get(ResumeImportStagingRow, operation_id)
            return None if row is None else self._resume_import_staging(row)

    def update(self, model: T, *, principal: str, expected_revision: int) -> T:
        self._validate_owner(model, principal)
        entity_type, entity_id = self._identity(model)
        with Session(self.engine) as db, db.begin():
            row = self._owned_row(db, entity_id, principal)
            if row.entity_type != entity_type:
                raise WorkbenchStoreError("entity_type_mismatch")
            if row.immutable and not isinstance(model, ResumeVersion):
                raise ImmutableObjectError(entity_id)
            if row.revision != expected_revision:
                raise RevisionConflictError(entity_id, row.revision)
            if self._revision(model) != expected_revision + 1:
                raise RevisionConflictError(entity_id, row.revision)
            previous = self._parse(row, type(model))
            self._validate_update(previous, model)
            previous_status = self._state(previous)
            next_status = self._state(model)
            if previous_status is not None and next_status is not None and previous_status != next_status:
                assert_transition(previous_status, next_status)
            if isinstance(model, Application):
                old_events = previous.events
                if model.events[: len(old_events)] != old_events:
                    raise ImmutableObjectError("application_events_are_append_only")
                self._append_application_events(
                    db, row, old_events, model.events[len(old_events) :], principal
                )
            result = db.execute(
                update(EntityRow)
                .where(EntityRow.id == entity_id, EntityRow.revision == expected_revision)
                .values(
                    revision=expected_revision + 1,
                    status=self._status(model),
                    immutable=self._is_immutable(model),
                    archived=self._is_archived(model),
                    payload_json=model.model_dump_json(),
                    updated_at=self._timestamps(model)[1],
                )
            )
            if result.rowcount != 1:
                authoritative = db.get(EntityRow, entity_id)
                raise RevisionConflictError(
                    entity_id,
                    authoritative.revision if authoritative is not None else expected_revision,
                )
            self._replace_references(db, row, model, principal)
        return model

    @staticmethod
    def _validate_update(previous: ContractModel, model: ContractModel) -> None:
        if isinstance(model, ResumeDraft):
            mutable_fields = {
                "content",
                "revision",
                "status",
                "updated_by",
                "updated_at",
            }
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(
                exclude=mutable_fields
            ):
                raise ImmutableObjectError("draft_identity_or_base_changed")
        if isinstance(model, MergeProposal):
            mutable_fields = {
                "decisions",
                "status",
                "revision",
                "operation_id",
                "result_version_id",
                "updated_at",
                "allowed_actions",
            }
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(
                exclude=mutable_fields
            ):
                raise ImmutableObjectError("merge_proposal_inputs_changed")
        if isinstance(model, ResumeVersion):
            mutable_fields = {
                "status",
                "confirmed_at",
                "allowed_actions",
                "revision",
            }
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(
                exclude=mutable_fields
            ):
                raise ImmutableObjectError(
                    "resume_version_content_or_lineage_changed"
                )
        if isinstance(model, MatchAnalysis):
            mutable_fields = {
                "status",
                "stale_reason",
                "allowed_actions",
                "revision",
            }
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(
                exclude=mutable_fields
            ):
                raise ImmutableObjectError("match_analysis_result_changed")
        if isinstance(model, Suggestion):
            mutable_fields = {"status", "revision", "decided_at"}
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(
                exclude=mutable_fields
            ):
                raise ImmutableObjectError("suggestion_candidate_changed")
        if isinstance(model, Application):
            mutable_fields = {
                "current_status",
                "priority",
                "next_action",
                "remind_at",
                "events",
                "revision",
                "updated_at",
                "allowed_actions",
            }
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(exclude=mutable_fields):
                raise ImmutableObjectError("application_binding_changed")
        if isinstance(model, BusinessOperation):
            mutable_fields = {
                "status",
                "parent_run_id",
                "task_id",
                "result_object_id",
                "error_code",
                "retryable",
                "revision",
                "updated_at",
            }
            if previous.model_dump(exclude=mutable_fields) != model.model_dump(
                exclude=mutable_fields
            ):
                raise ImmutableObjectError("operation_input_changed")
            if (
                previous.parent_run_id is not None
                and previous.parent_run_id != model.parent_run_id
            ):
                raise ImmutableObjectError("operation_run_binding_changed")
            if previous.task_id is not None and previous.task_id != model.task_id:
                raise ImmutableObjectError("operation_task_binding_changed")

    def append_event(
        self,
        entity_id: str,
        *,
        principal: str,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime,
    ) -> StoredEvent:
        with Session(self.engine) as db, db.begin():
            self._owned_row(db, entity_id, principal)
            last = db.scalar(
                select(func.max(BusinessEventRow.sequence)).where(
                    BusinessEventRow.entity_id == entity_id
                )
            )
            sequence = int(last or 0) + 1
            db.add(
                BusinessEventRow(
                    entity_id=entity_id,
                    sequence=sequence,
                    event_type=event_type,
                    actor=principal,
                    payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    occurred_at=occurred_at.isoformat(),
                )
            )
        return StoredEvent(sequence, event_type, principal, payload, occurred_at)

    def delete_migration_entity_if_unreferenced(
        self, entity_id: str, *, principal: str
    ) -> bool:
        """Delete one migration-created projection only when no other entity uses it.

        Source files and external Knowledge/Session/Run/Artifact stores are never
        touched. Callers must delete a batch in reverse dependency order.
        """
        with Session(self.engine) as db, db.begin():
            row = self._owned_row(db, entity_id, principal)
            inbound = db.scalar(
                select(func.count())
                .select_from(EntityReferenceRow)
                .where(EntityReferenceRow.target_id == entity_id)
            )
            if int(inbound or 0) > 0:
                return False
            if row.entity_type == "BusinessOperation":
                db.execute(delete(ResumeImportStagingRow).where(ResumeImportStagingRow.operation_id == entity_id))
                db.execute(delete(OperationCheckpointRow).where(OperationCheckpointRow.operation_id == entity_id))
                db.execute(delete(OperationKeyRow).where(OperationKeyRow.operation_id == entity_id))
            if row.entity_type == "ResumeVersion":
                db.execute(delete(ResumeLineageRow).where(ResumeLineageRow.version_id == entity_id))
            db.execute(delete(EvidenceBindingRow).where(EvidenceBindingRow.subject_id == entity_id))
            db.execute(delete(BusinessEventRow).where(BusinessEventRow.entity_id == entity_id))
            db.execute(delete(WorkspaceMembershipRow).where(WorkspaceMembershipRow.entity_id == entity_id))
            db.execute(delete(EntityReferenceRow).where(EntityReferenceRow.source_id == entity_id))
            db.delete(row)
        return True

    def prepare_migration_version_delete(self, version_id: str, *, principal: str) -> bool:
        """Move a Resume latest pointer back only when it is the sole inbound ref."""
        with Session(self.engine) as db, db.begin():
            version_row = self._owned_row(db, version_id, principal)
            if version_row.entity_type != "ResumeVersion":
                raise WorkbenchStoreError("migration_target_is_not_resume_version")
            version = self._parse(version_row, ResumeVersion)
            inbound = list(db.scalars(select(EntityReferenceRow).where(EntityReferenceRow.target_id == version_id)))
            if any(reference.source_id != version.resume_id for reference in inbound):
                return False
            resume_row = self._owned_row(db, version.resume_id, principal)
            resume = self._parse(resume_row, Resume)
            if resume.latest_version_id != version_id:
                return True
            updated = Resume.model_validate(resume.model_dump() | {
                "latest_version_id": version.parent_version_id,
                "revision": resume.revision + 1,
                "updated_at": datetime.now(UTC),
            })
            resume_row.revision = updated.revision
            resume_row.payload_json = updated.model_dump_json()
            resume_row.updated_at = updated.updated_at.isoformat()
            self._replace_references(db, resume_row, updated, principal)
        return True

    def save_operation_checkpoint(
        self,
        operation_id: str,
        *,
        principal: str,
        result_ref: str,
        result_sha256: str,
        validator_version: str,
        evidence_refs: tuple[str, ...],
        safety_summary: dict[str, Any],
        partial: bool,
    ) -> OperationCheckpoint:
        now = datetime.now(UTC)
        with Session(self.engine) as db, db.begin():
            operation = self._owned_row(db, operation_id, principal)
            if operation.entity_type != "BusinessOperation":
                raise WorkbenchStoreError("checkpoint_target_is_not_operation")
            existing = db.get(OperationCheckpointRow, operation_id)
            canonical_evidence = json.dumps(
                list(evidence_refs), ensure_ascii=False, sort_keys=True
            )
            canonical_safety = json.dumps(
                safety_summary, ensure_ascii=False, sort_keys=True
            )
            if existing is not None:
                same = (
                    existing.result_ref == result_ref
                    and existing.result_sha256 == result_sha256
                    and existing.validator_version == validator_version
                    and existing.evidence_refs_json == canonical_evidence
                    and existing.safety_summary_json == canonical_safety
                    and existing.partial == partial
                )
                if not same:
                    raise IdempotencyConflictError(
                        f"operation_checkpoint:{operation_id}"
                    )
                return self._checkpoint(existing)
            row = OperationCheckpointRow(
                operation_id=operation_id,
                result_ref=result_ref,
                result_sha256=result_sha256,
                validator_version=validator_version,
                evidence_refs_json=canonical_evidence,
                safety_summary_json=canonical_safety,
                partial=partial,
                commit_attempts=0,
                last_commit_error=None,
                updated_at=now.isoformat(),
            )
            db.add(row)
            db.flush()
            return self._checkpoint(row)

    def get_operation_checkpoint(
        self, operation_id: str, *, principal: str
    ) -> OperationCheckpoint | None:
        with Session(self.engine) as db:
            self._owned_row(db, operation_id, principal)
            row = db.get(OperationCheckpointRow, operation_id)
            return None if row is None else self._checkpoint(row)

    def record_commit_attempt(
        self,
        operation_id: str,
        *,
        principal: str,
        error: str | None,
    ) -> OperationCheckpoint:
        now = datetime.now(UTC)
        with Session(self.engine) as db, db.begin():
            self._owned_row(db, operation_id, principal)
            row = db.get(OperationCheckpointRow, operation_id)
            if row is None:
                raise ObjectNotFoundError(f"operation_checkpoint:{operation_id}")
            row.commit_attempts += 1
            row.last_commit_error = error
            row.updated_at = now.isoformat()
            db.flush()
            return self._checkpoint(row)

    def list_events(self, entity_id: str, *, principal: str) -> tuple[StoredEvent, ...]:
        with Session(self.engine) as db:
            self._owned_row(db, entity_id, principal)
            rows = db.scalars(
                select(BusinessEventRow)
                .where(BusinessEventRow.entity_id == entity_id)
                .order_by(BusinessEventRow.sequence)
            ).all()
        return tuple(
            StoredEvent(
                sequence=row.sequence,
                event_type=row.event_type,
                actor=row.actor,
                payload=json.loads(row.payload_json),
                occurred_at=datetime.fromisoformat(row.occurred_at),
            )
            for row in rows
        )

    def replace_resume_projections(self, models: list[Resume | MatchAnalysis], *, principal: str) -> None:
        """Archive replaced profiles and expire analyses atomically, retaining historical refs."""
        with Session(self.engine) as db, db.begin():
            for model in models:
                if not isinstance(model, (Resume, MatchAnalysis)):
                    raise WorkbenchStoreError("invalid_replacement_projection")
                _, entity_id = self._identity(model)
                row = self._owned_row(db, entity_id, principal)
                previous = self._parse(row, type(model))
                if row.revision != model.revision - 1:
                    raise RevisionConflictError(entity_id, row.revision)
                self._validate_update(previous, model)
                assert_transition(previous.status, model.status)
                row.payload_json = model.model_dump_json()
                row.revision = model.revision
                row.status = self._status(model)
                row.archived = self._is_archived(model)

    def physical_delete(self, entity_id: str, *, principal: str) -> None:
        with Session(self.engine) as db, db.begin():
            row = self._owned_row(db, entity_id, principal)
            if row.immutable:
                raise ImmutableObjectError(entity_id)
            inbound = db.scalar(
                select(func.count()).select_from(EntityReferenceRow).where(
                    EntityReferenceRow.target_id == entity_id
                )
            )
            children = db.scalar(
                select(func.count()).select_from(ResumeLineageRow).where(
                    ResumeLineageRow.parent_version_id == entity_id
                )
            )
            branch_bases = db.scalar(
                select(func.count()).select_from(ResumeLineageRow).where(
                    ResumeLineageRow.branch_base_version_id == entity_id
                )
            )
            events = db.scalar(
                select(func.count()).select_from(BusinessEventRow).where(
                    BusinessEventRow.entity_id == entity_id
                )
            )
            operation_keys = db.scalar(
                select(func.count()).select_from(OperationKeyRow).where(
                    OperationKeyRow.operation_id == entity_id
                )
            )
            checkpoints = db.scalar(
                select(func.count()).select_from(OperationCheckpointRow).where(
                    OperationCheckpointRow.operation_id == entity_id
                )
            )
            memberships = db.scalar(
                select(func.count()).select_from(WorkspaceMembershipRow).where(
                    (WorkspaceMembershipRow.workspace_id == entity_id)
                    | (WorkspaceMembershipRow.entity_id == entity_id)
                )
            )
            evidence_bindings = db.scalar(
                select(func.count()).select_from(EvidenceBindingRow).where(
                    (EvidenceBindingRow.workspace_id == entity_id)
                    | (EvidenceBindingRow.subject_id == entity_id)
                )
            )
            import_staging = db.scalar(
                select(func.count()).select_from(ResumeImportStagingRow).where(
                    ResumeImportStagingRow.operation_id == entity_id
                )
            )
            if (
                inbound
                or children
                or branch_bases
                or events
                or operation_keys
                or checkpoints
                or memberships
                or evidence_bindings
                or import_staging
            ):
                raise ReferenceConflictError(entity_id)
            db.execute(delete(EntityRow).where(EntityRow.id == entity_id))

    def lineage(self, resume_id: str, *, principal: str) -> tuple[ResumeVersion, ...]:
        with Session(self.engine) as db:
            rows = list(db.execute(
                select(EntityRow)
                .join(ResumeLineageRow, ResumeLineageRow.version_id == EntityRow.id)
                .where(
                    ResumeLineageRow.resume_id == resume_id,
                    EntityRow.owner_id == principal,
                )
                .order_by(ResumeLineageRow.created_at, ResumeLineageRow.version_id)
            ).scalars())
        pending = {row.id: self._parse(row, ResumeVersion) for row in rows}
        ordered: list[ResumeVersion] = []
        emitted: set[str] = set()
        while pending:
            ready = sorted(
                (
                    item
                    for item in pending.values()
                    if item.parent_version_id is None
                    or item.parent_version_id in emitted
                ),
                key=lambda item: (item.created_at, item.version_id),
            )
            if not ready:
                raise LineageConflictError("lineage_cycle_or_missing_parent")
            for item in ready:
                ordered.append(item)
                emitted.add(item.version_id)
                pending.pop(item.version_id)
        return tuple(ordered)

    def _insert_lineage(
        self, db: Session, version: ResumeVersion, principal: str
    ) -> None:
        resume = self._owned_row(db, version.resume_id, principal)
        branch = self._owned_row(db, version.branch_id, principal)
        if resume.entity_type != "Resume" or branch.entity_type != "ResumeBranch":
            raise LineageConflictError("lineage_resume_or_branch_type_invalid")
        branch_model = self._parse(branch, ResumeBranch)
        if branch_model.resume_id != version.resume_id:
            raise LineageConflictError("cross_resume_branch")
        if branch_model.base_version_id != version.branch_base_version_id:
            raise LineageConflictError("branch_base_mismatch")
        if version.parent_version_id is None:
            existing_roots = db.scalar(
                select(func.count()).select_from(ResumeLineageRow).where(
                    ResumeLineageRow.resume_id == version.resume_id,
                    ResumeLineageRow.parent_version_id.is_(None),
                )
            )
            if existing_roots:
                raise LineageConflictError("multiple_base_roots")
        if version.parent_version_id is not None:
            parent = db.get(ResumeLineageRow, version.parent_version_id)
            if parent is None:
                raise LineageConflictError("parent_version_not_found")
            if parent.resume_id != version.resume_id:
                raise LineageConflictError("cross_resume_parent")
        if version.branch_base_version_id != version.version_id:
            base = db.get(ResumeLineageRow, version.branch_base_version_id)
            if base is None or base.resume_id != version.resume_id:
                raise LineageConflictError("branch_base_not_found")
        db.add(
            ResumeLineageRow(
                version_id=version.version_id,
                resume_id=version.resume_id,
                branch_id=version.branch_id,
                parent_version_id=version.parent_version_id,
                branch_base_version_id=version.branch_base_version_id,
                node_type=version.node_type.value,
                content_sha256=version.content.content_sha256,
                created_at=version.created_at.isoformat(),
            )
        )

    def _replace_references(
        self,
        db: Session,
        source: EntityRow,
        model: ContractModel,
        principal: str,
    ) -> None:
        db.execute(
            delete(EntityReferenceRow).where(EntityReferenceRow.source_id == source.id)
        )
        for target_id, kind in self._internal_references(model):
            if target_id == source.id:
                continue
            target = db.get(EntityRow, target_id)
            if target is None:
                # ResumeBranch points to its initial version before that version
                # exists. Lineage.branch_base_version_id still protects it once
                # inserted; all other missing references are invalid.
                if isinstance(model, ResumeBranch) and kind == "branch_base":
                    continue
                raise ReferenceConflictError(f"missing_target:{kind}:{target_id}")
            if target.owner_id != principal:
                raise ForbiddenError("cross_principal_reference")
            if (
                isinstance(model, (Application, ExportRecord))
                and kind == "resume_version"
                and target.status != ResumeVersionStatus.CONFIRMED.value
            ):
                raise ReferenceConflictError(
                    "application_or_export_requires_confirmed_resume_version"
                )
            db.add(
                EntityReferenceRow(
                    source_id=source.id,
                    target_id=target_id,
                    kind=kind,
                    created_at=datetime.now(UTC).isoformat(),
                )
            )

    @staticmethod
    def _internal_references(model: ContractModel) -> Iterable[tuple[str, str]]:
        references: list[tuple[str, str]] = []
        if isinstance(model, Resume):
            if model.latest_version_id:
                references.append((model.latest_version_id, "latest_version"))
        elif isinstance(model, ResumeBranch):
            references.extend(
                [(model.resume_id, "resume"), (model.base_version_id, "branch_base")]
            )
            if model.job_snapshot_id:
                references.append((model.job_snapshot_id, "job_snapshot"))
        elif isinstance(model, ResumeVersion):
            references.extend(
                [(model.resume_id, "resume"), (model.branch_id, "branch")]
            )
            if model.parent_version_id:
                references.append((model.parent_version_id, "parent_version"))
            if model.job_snapshot_id:
                references.append((model.job_snapshot_id, "job_snapshot"))
        elif isinstance(model, ResumeDraft):
            references.extend(
                [
                    (model.resume_id, "resume"),
                    (model.base_version_id, "base_version"),
                    (model.branch_id, "branch"),
                ]
            )
        elif isinstance(model, MergeProposal):
            references.extend(
                [
                    (model.resume_id, "resume"),
                    (model.target_branch_id, "target_branch"),
                    (model.base_version_id, "base_version"),
                    (model.upstream_version_id, "upstream_version"),
                    (model.target_version_id, "target_version"),
                ]
            )
            if model.operation_id:
                references.append((model.operation_id, "operation"))
            if model.result_version_id:
                references.append((model.result_version_id, "result_version"))
        elif isinstance(model, JobSnapshot):
            references.append((model.job_id, "job"))
        elif isinstance(model, MatchAnalysis):
            references.extend(
                [
                    (model.workspace_id, "workspace"),
                    (model.resume_version_id, "resume_version"),
                    (model.job_snapshot_id, "job_snapshot"),
                ]
            )
        elif isinstance(model, Suggestion):
            references.extend(
                [
                    (model.analysis_id, "analysis"),
                    (model.target_version_id, "target_version"),
                    (model.target_draft_id, "target_draft"),
                ]
            )
        elif isinstance(model, Application):
            references.extend(
                [
                    (model.workspace_id, "workspace"),
                    (model.job_snapshot_id, "job_snapshot"),
                    (model.resume_version_id, "resume_version"),
                ]
            )
        elif isinstance(model, InterviewReview):
            references.extend(
                [
                    (model.workspace_id, "workspace"),
                    (model.application_id, "application"),
                ]
            )
        elif isinstance(model, ExportRecord):
            references.append((model.resume_version_id, "resume_version"))
        elif isinstance(model, BusinessOperation):
            references.append((model.workspace_id, "workspace"))
            if model.result_object_id:
                references.append((model.result_object_id, "result_object"))
        return references

    @staticmethod
    def _append_application_events(
        db: Session,
        row: EntityRow,
        old_events: tuple,
        new_events: tuple,
        principal: str,
    ) -> None:
        start = len(old_events)
        for index, item in enumerate(new_events, start=start + 1):
            db.add(
                BusinessEventRow(
                    entity_id=row.id,
                    sequence=index,
                    event_type="application_status_changed",
                    actor=principal,
                    payload_json=item.model_dump_json(),
                    occurred_at=item.occurred_at.isoformat(),
                )
            )

    @staticmethod
    def _parse(row: EntityRow, model_type: type[T]) -> T:
        if row.entity_type != model_type.__name__:
            raise WorkbenchStoreError(
                f"entity_type_mismatch:{row.entity_type}:{model_type.__name__}"
            )
        return model_type.model_validate_json(row.payload_json)

    @staticmethod
    def _owned_row(db: Session, entity_id: str, principal: str) -> EntityRow:
        row = db.get(EntityRow, entity_id)
        if row is None:
            raise ObjectNotFoundError(entity_id)
        if row.owner_id != principal:
            raise ForbiddenError(entity_id)
        return row

    @staticmethod
    def _checkpoint(row: OperationCheckpointRow) -> OperationCheckpoint:
        return OperationCheckpoint(
            operation_id=row.operation_id,
            result_ref=row.result_ref,
            result_sha256=row.result_sha256,
            validator_version=row.validator_version,
            evidence_refs=tuple(json.loads(row.evidence_refs_json)),
            safety_summary=json.loads(row.safety_summary_json),
            partial=row.partial,
            commit_attempts=row.commit_attempts,
            last_commit_error=row.last_commit_error,
            updated_at=datetime.fromisoformat(row.updated_at),
        )

    @staticmethod
    def _evidence_binding(row: EvidenceBindingRow) -> StoredEvidenceBinding:
        return StoredEvidenceBinding(
            binding_id=row.binding_id,
            workspace_id=row.workspace_id,
            subject_id=row.subject_id,
            source_kind=row.source_kind,
            source_ref=row.source_ref,
            expected_sha256=row.expected_sha256,
            status=row.status,
            safe_summary=json.loads(row.safe_summary_json),
            created_at=datetime.fromisoformat(row.created_at),
            checked_at=datetime.fromisoformat(row.checked_at),
        )

    @staticmethod
    def _resume_import_staging(row: ResumeImportStagingRow) -> ResumeImportStaging:
        return ResumeImportStaging(
            operation_id=row.operation_id,
            resume_id=row.resume_id,
            branch_id=row.branch_id,
            version_id=row.version_id,
            resume_name=row.resume_name,
            normalized_sha256=row.normalized_sha256,
            raw_artifact_ref=row.raw_artifact_ref,
            raw_sha256=row.raw_sha256,
            knowledge_base_id=row.knowledge_base_id,
            document_id=row.document_id,
            document_version_id=row.document_version_id,
            parser_version=row.parser_version,
            projection=json.loads(row.projection_json),
            created_at=datetime.fromisoformat(row.created_at),
        )
