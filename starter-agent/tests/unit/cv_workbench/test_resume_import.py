from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

import pytest

from starter_agent.cv_workbench.bindings import (
    EvidenceBindingService,
    EvidenceSourceSnapshot,
)
from starter_agent.cv_workbench.contracts import (
    BusinessOperation,
    OperationStatus,
    Resume,
    ResumeVersion,
    Workspace,
)
from starter_agent.cv_workbench.resume_import import (
    KnowledgeImportResult,
    RawArtifact,
    ResumeImportCommand,
    ResumeImportError,
    ResumeImportService,
    ResumeMarkdownNormalizer,
)
from starter_agent.cv_workbench.evidence_adapters import (
    ArtifactEvidenceReader,
    KnowledgeEvidenceReader,
)
from starter_agent.cv_workbench.resume_import_adapters import (
    ScopedKnowledgeResumeImporter,
    SessionResumeArtifactWriter,
)
from starter_agent.cv_workbench.store import (
    IdempotencyConflictError,
    ObjectNotFoundError,
    SQLiteWorkbenchStore,
)
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.knowledge.store import SQLiteKnowledgeStore


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


@dataclass
class FakeArtifactWriter:
    calls: int = 0
    snapshots: dict[str, EvidenceSourceSnapshot] = field(default_factory=dict)

    def write_resume_source(
        self, *, operation_id, filename, content, principal, workspace_id
    ) -> RawArtifact:
        self.calls += 1
        digest = sha256(content).hexdigest()
        source_ref = f"artifact:resume:{operation_id}"
        self.snapshots[source_ref] = EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256=digest,
            metadata={"filename": filename, "content": content.decode("utf-8")},
        )
        return RawArtifact(source_ref=source_ref, content_sha256=digest)


@dataclass
class FakeKnowledgeImporter:
    calls: int = 0
    snapshots: dict[str, EvidenceSourceSnapshot] = field(default_factory=dict)

    def ingest_resume(
        self,
        *,
        operation_id,
        filename,
        normalized_markdown,
        content_sha256,
        principal,
        workspace_id,
    ) -> KnowledgeImportResult:
        self.calls += 1
        result = KnowledgeImportResult(
            knowledge_base_id="00000000-0000-0000-0000-000000000001",
            document_id="00000000-0000-0000-0000-000000000002",
            document_version_id="00000000-0000-0000-0000-000000000003",
            content_sha256=content_sha256,
        )
        self.snapshots[result.source_ref] = EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256=content_sha256,
            metadata={"filename": filename, "source_text": normalized_markdown},
        )
        return result


class MappingReader:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def inspect(self, source_ref, *, principal, workspace_id, now):
        return self.snapshots.get(
            source_ref, EvidenceSourceSnapshot(exists=False, authorized=True)
        )


def make_service(tmp_path: Path):
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'resume-import.db').as_posix()}", tmp_path
    )
    workspace = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    workspace["revision"] = 1
    store.create(Workspace.model_validate(workspace), principal=PRINCIPAL)
    artifacts = FakeArtifactWriter()
    knowledge = FakeKnowledgeImporter()
    bindings = EvidenceBindingService(
        store=store,
        readers={
            "artifact": MappingReader(artifacts.snapshots),
            "document_version": MappingReader(knowledge.snapshots),
        },
    )
    return (
        ResumeImportService(
            store=store,
            artifact_writer=artifacts,
            knowledge_importer=knowledge,
            evidence_bindings=bindings,
        ),
        store,
        artifacts,
        knowledge,
        bindings,
    )


def command(content: bytes | None = None) -> ResumeImportCommand:
    return ResumeImportCommand(
        operation_id="op_import_1",
        idempotency_key="resume-import-1",
        workspace_id="ws_demo",
        resume_id="res_imported",
        branch_id="rb_imported_master",
        version_id="rv_imported_v1",
        resume_name="Backend Resume",
        filename="resume.txt",
        content=content
        or b"Backend Engineer  \r\n\r\n\r\n- Python APIs\r\n- FastAPI\r\n",
        confirmed_authorized=True,
    )


def test_normalizer_is_deterministic_and_projection_has_no_body() -> None:
    normalizer = ResumeMarkdownNormalizer()
    first = normalizer.normalize("# Profile  \r\n\r\n\r\n- Python\r\n")
    second = normalizer.normalize("# Profile\n\n- Python\n")

    assert first == second
    assert first.markdown == "# Profile\n\n- Python\n"
    assert [block.kind for block in first.blocks] == ["heading", "list"]
    assert "Python" not in repr(first.projection())


def test_import_creates_confirmed_version_bindings_and_idempotent_replay(
    tmp_path: Path,
) -> None:
    service, store, artifacts, knowledge, bindings = make_service(tmp_path)

    result = service.import_resume(command(), principal=PRINCIPAL)
    replay = service.import_resume(command(), principal=PRINCIPAL)
    version = store.get(ResumeVersion, result.version_id, principal=PRINCIPAL)
    resume = store.get(Resume, result.resume_id, principal=PRINCIPAL)
    operation = store.get(
        BusinessOperation, result.operation_id, principal=PRINCIPAL
    )

    assert result.reused is False
    assert replay.reused is True
    assert replay.normalized_sha256 == result.normalized_sha256
    assert artifacts.calls == knowledge.calls == 1
    assert version.status.value == "confirmed"
    assert version.content.content_sha256 == result.normalized_sha256
    assert resume.latest_version_id == version.version_id
    assert operation.status == OperationStatus.COMMITTED
    summaries = bindings.summaries(version.version_id, principal=PRINCIPAL)
    assert {item.source_kind for item in summaries} == {
        "artifact",
        "document_version",
    }
    assert all(item.traceable for item in summaries)
    assert "Python APIs" not in repr(summaries)


def test_parse_failure_keeps_raw_artifact_and_creates_no_version(
    tmp_path: Path,
) -> None:
    service, store, artifacts, knowledge, _ = make_service(tmp_path)

    with pytest.raises(ResumeImportError) as error:
        service.import_resume(command(content=b"\n\n"), principal=PRINCIPAL)

    assert error.value.raw_artifact_ref == "artifact:resume:op_import_1"
    assert artifacts.calls == 1
    assert knowledge.calls == 0
    assert store.get(
        BusinessOperation, "op_import_1", principal=PRINCIPAL
    ).status == OperationStatus.FAILED
    with pytest.raises(ObjectNotFoundError):
        store.get(ResumeVersion, "rv_imported_v1", principal=PRINCIPAL)


def test_same_idempotency_key_with_changed_input_is_rejected(tmp_path: Path) -> None:
    service, _, _, _, _ = make_service(tmp_path)
    service.import_resume(command(), principal=PRINCIPAL)

    with pytest.raises(IdempotencyConflictError, match="resume-import-1"):
        service.import_resume(
            command(content=b"Different resume\n"), principal=PRINCIPAL
        )


def test_real_local_stores_complete_import_and_authorized_binding(
    tmp_path: Path,
) -> None:
    workbench = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'real-workbench.db').as_posix()}", tmp_path
    )
    workspace = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    workspace["revision"] = 1
    workbench.create(Workspace.model_validate(workspace), principal=PRINCIPAL)
    artifacts = SQLiteSessionStore("sqlite:///real-artifacts.db", tmp_path)
    knowledge = SQLiteKnowledgeStore("sqlite:///real-knowledge.db", tmp_path)
    bindings = EvidenceBindingService(
        store=workbench,
        readers={
            "artifact": ArtifactEvidenceReader(artifacts),
            "document_version": KnowledgeEvidenceReader(knowledge),
        },
    )
    service = ResumeImportService(
        store=workbench,
        artifact_writer=SessionResumeArtifactWriter(artifacts),
        knowledge_importer=ScopedKnowledgeResumeImporter(knowledge),
        evidence_bindings=bindings,
    )

    result = service.import_resume(command(), principal=PRINCIPAL)

    assert workbench.get(
        BusinessOperation, result.operation_id, principal=PRINCIPAL
    ).status == OperationStatus.COMMITTED
    assert all(
        summary.traceable
        for summary in bindings.refresh_subject(
            result.version_id, principal=PRINCIPAL
        )
    )
