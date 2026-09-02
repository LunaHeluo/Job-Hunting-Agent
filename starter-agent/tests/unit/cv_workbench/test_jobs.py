from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from starter_agent.cv_workbench.bindings import (
    EvidenceBindingService,
    EvidenceSourceSnapshot,
)
from starter_agent.cv_workbench.contracts import ContentReference, Job, JobSnapshot, Workspace
from starter_agent.cv_workbench.jobs import (
    CandidateCommand,
    CandidateContent,
    JobService,
    JobServiceError,
    PublishedJobContent,
    StableUrlResult,
    canonical_job_url,
)
from starter_agent.cv_workbench.job_adapters import (
    ExistingGateStableUrlFetcher,
    SessionKnowledgeJobContentRepository,
)
from starter_agent.cv_workbench.evidence_adapters import (
    ArtifactEvidenceReader,
    KnowledgeEvidenceReader,
)
from starter_agent.cv_workbench.store import SQLiteWorkbenchStore
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.knowledge.store import SQLiteKnowledgeStore


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


@dataclass
class FakeJobContent:
    bodies: dict[str, str] = field(default_factory=dict)
    sources: dict[str, EvidenceSourceSnapshot] = field(default_factory=dict)
    publish_calls: int = 0

    def write_candidate(
        self,
        *,
        candidate_id,
        filename,
        markdown,
        content_sha256,
        principal,
        workspace_id,
    ):
        ref = f"artifact:job-candidate:{candidate_id}"
        self.bodies[ref] = markdown
        self.sources[ref] = EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256=content_sha256,
            metadata={"filename": filename, "content": markdown},
        )
        return CandidateContent(artifact_ref=ref, content_sha256=content_sha256)

    def read_candidate(self, artifact_ref, *, principal, workspace_id):
        return self.bodies[artifact_ref]

    def publish_snapshot(
        self,
        *,
        operation_id,
        filename,
        markdown,
        content_sha256,
        principal,
        workspace_id,
    ):
        self.publish_calls += 1
        suffix = sha256(operation_id.encode()).hexdigest()[:12]
        ref = ContentReference(
            content_sha256=content_sha256,
            knowledge_base_id="00000000-0000-0000-0000-000000000001",
            document_id=f"00000000-0000-0000-0000-{suffix}",
            document_version_id=f"10000000-0000-0000-0000-{suffix}",
        )
        source_ref = (
            f"knowledge://{ref.knowledge_base_id}/{ref.document_id}/"
            f"{ref.document_version_id}"
        )
        self.sources[source_ref] = EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256=content_sha256,
            metadata={"filename": filename, "source_text": markdown},
        )
        return PublishedJobContent(content_ref=ref)


class MappingReader:
    def __init__(self, sources):
        self.sources = sources

    def inspect(self, source_ref, *, principal, workspace_id, now):
        return self.sources.get(
            source_ref, EvidenceSourceSnapshot(exists=False, authorized=True)
        )


@dataclass
class FakeUrlFetcher:
    content: FakeJobContent
    markdown: str = "# Backend Engineer\n\n- Build Python APIs\n"
    calls: list[str] = field(default_factory=list)

    def fetch(self, url, *, principal, workspace_id):
        self.calls.append(url)
        normalized = self.markdown.rstrip() + "\n"
        digest = sha256(normalized.encode()).hexdigest()
        ref = "artifact:stable-url:1"
        self.content.bodies[ref] = normalized
        self.content.sources[ref] = EvidenceSourceSnapshot(
            exists=True, authorized=True, content_sha256=digest
        )
        return StableUrlResult(
            title="Backend Engineer",
            company="Acme",
            location="Shanghai",
            requested_url=url,
            final_url="https://jobs.example.test/backend?utm_source=x&level=mid#apply",
            markdown=normalized,
            source_content_sha256=digest,
            artifact_ref=ref,
            fetched_at=datetime(2026, 8, 17, tzinfo=UTC),
            expires_at=datetime(2026, 8, 18, tzinfo=UTC),
        )


def make_service(tmp_path: Path, *, url: bool = False):
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}", tmp_path
    )
    workspace = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    workspace["revision"] = 1
    store.create(Workspace.model_validate(workspace), principal=PRINCIPAL)
    content = FakeJobContent()
    evidence = EvidenceBindingService(
        store=store,
        readers={
            "artifact": MappingReader(content.sources),
            "document_version": MappingReader(content.sources),
        },
    )
    fetcher = FakeUrlFetcher(content) if url else None
    return (
        JobService(
            store=store,
            content=content,
            evidence=evidence,
            url_fetcher=fetcher,
        ),
        store,
        content,
        fetcher,
    )


def text_command(candidate_id="jc_manual_1", body=None):
    return CandidateCommand(
        candidate_id=candidate_id,
        workspace_id="ws_demo",
        title="Backend Engineer",
        company="Acme",
        location="Shanghai",
        filename="job.txt",
        content=(body or "# Backend Engineer\n\n- Build Python APIs\n").encode(),
        confirmed_authorized=True,
    )


def test_candidate_does_not_create_job_until_confirmed_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    service, store, content, _ = make_service(tmp_path)
    candidate = service.create_text_candidate(text_command(), principal=PRINCIPAL)

    assert store.list(Job, principal=PRINCIPAL).items == ()
    assert store.list(JobSnapshot, principal=PRINCIPAL).items == ()
    promotion = service.confirm_candidate(
        candidate.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_job_confirm_1",
        idempotency_key="job-confirm-1",
        principal=PRINCIPAL,
    )
    replay = service.confirm_candidate(
        candidate.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_job_confirm_1",
        idempotency_key="job-confirm-1",
        principal=PRINCIPAL,
    )

    assert promotion.reused is False
    assert replay.reused is True
    assert replay.job_id == promotion.job_id
    assert len(store.list(Job, principal=PRINCIPAL).items) == 1
    assert len(store.list(JobSnapshot, principal=PRINCIPAL).items) == 1
    assert content.publish_calls == 1
    bindings = store.list_evidence_bindings(
        promotion.snapshot_id, principal=PRINCIPAL
    )
    assert {item.source_kind for item in bindings} == {
        "artifact",
        "document_version",
    }


def test_exact_content_reuses_snapshot_but_same_url_changed_content_conflicts(
    tmp_path: Path,
) -> None:
    service, store, _, _ = make_service(tmp_path)
    first = service.create_text_candidate(text_command(), principal=PRINCIPAL)
    first_result = service.confirm_candidate(
        first.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_job_first",
        idempotency_key="job-first",
        principal=PRINCIPAL,
    )
    duplicate = service.create_text_candidate(
        text_command(candidate_id="jc_manual_duplicate"), principal=PRINCIPAL
    )
    duplicate_result = service.confirm_candidate(
        duplicate.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_job_duplicate",
        idempotency_key="job-duplicate",
        principal=PRINCIPAL,
    )

    assert duplicate_result.snapshot_id == first_result.snapshot_id
    assert len(store.list(JobSnapshot, principal=PRINCIPAL).items) == 1


def test_stable_url_is_canonicalized_and_unavailable_source_keeps_snapshot(
    tmp_path: Path,
) -> None:
    service, store, _, fetcher = make_service(tmp_path, url=True)
    candidate = service.create_url_candidate(
        candidate_id="jc_url_1",
        workspace_id="ws_demo",
        url="HTTPS://JOBS.EXAMPLE.TEST/backend?utm_campaign=x&level=mid#top",
        principal=PRINCIPAL,
    )
    promotion = service.confirm_candidate(
        candidate.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_job_url",
        idempotency_key="job-url",
        principal=PRINCIPAL,
    )
    snapshot = store.get(JobSnapshot, promotion.snapshot_id, principal=PRINCIPAL)
    health = service.source_health(
        snapshot.snapshot_id, principal=PRINCIPAL, available=False
    )

    assert fetcher.calls == ["https://jobs.example.test/backend?level=mid"]
    assert str(candidate.final_url) == "https://jobs.example.test/backend?level=mid"
    assert snapshot.verified is True
    assert health.source_status == "unavailable"
    assert "using_last_legal_snapshot" in health.risk_flags
    assert health.last_legal_content_sha256 == snapshot.content.content_sha256


def test_same_stable_url_changed_content_is_created_as_visible_conflict(
    tmp_path: Path,
) -> None:
    service, store, content, fetcher = make_service(tmp_path, url=True)
    first = service.create_url_candidate(
        candidate_id="jc_url_first",
        workspace_id="ws_demo",
        url="https://jobs.example.test/backend",
        principal=PRINCIPAL,
    )
    first_result = service.confirm_candidate(
        first.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_url_first",
        idempotency_key="url-first",
        principal=PRINCIPAL,
    )
    fetcher.markdown = "# Backend Engineer\n\n- Build Rust services\n"
    second = service.create_url_candidate(
        candidate_id="jc_url_second",
        workspace_id="ws_demo",
        url="https://jobs.example.test/backend",
        principal=PRINCIPAL,
    )
    second_result = service.confirm_candidate(
        second.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_url_second",
        idempotency_key="url-second",
        principal=PRINCIPAL,
    )

    assert second_result.snapshot_id != first_result.snapshot_id
    assert second_result.conflict_snapshot_ids == (first_result.snapshot_id,)
    assert len(store.list(JobSnapshot, principal=PRINCIPAL).items) == 2


def test_url_rejects_credentials_and_sensitive_query() -> None:
    with pytest.raises(JobServiceError, match="stable_url_credentials_forbidden"):
        canonical_job_url("https://user:pass@jobs.example.test/role")
    with pytest.raises(JobServiceError, match="stable_url_sensitive_query"):
        canonical_job_url("https://jobs.example.test/role?token=secret")


def test_real_local_stores_keep_candidate_temporary_then_publish_job(
    tmp_path: Path,
) -> None:
    workbench = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'real-jobs.db').as_posix()}", tmp_path
    )
    workspace = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    workspace["revision"] = 1
    workbench.create(Workspace.model_validate(workspace), principal=PRINCIPAL)
    artifacts = SQLiteSessionStore("sqlite:///job-artifacts.db", tmp_path)
    knowledge = SQLiteKnowledgeStore("sqlite:///job-knowledge.db", tmp_path)
    evidence = EvidenceBindingService(
        store=workbench,
        readers={
            "artifact": ArtifactEvidenceReader(artifacts),
            "document_version": KnowledgeEvidenceReader(knowledge),
        },
    )
    service = JobService(
        store=workbench,
        content=SessionKnowledgeJobContentRepository(artifacts, knowledge),
        evidence=evidence,
    )

    candidate = service.create_text_candidate(text_command(), principal=PRINCIPAL)
    assert workbench.list(Job, principal=PRINCIPAL).items == ()

    promoted = service.confirm_candidate(
        candidate.candidate_id,
        workspace_id="ws_demo",
        operation_id="op_real_job",
        idempotency_key="real-job",
        principal=PRINCIPAL,
    )

    snapshot = workbench.get(
        JobSnapshot, promoted.snapshot_id, principal=PRINCIPAL
    )
    assert snapshot.content.knowledge_base_id is not None
    assert all(
        item.traceable
        for item in evidence.refresh_subject(
            snapshot.snapshot_id, principal=PRINCIPAL
        )
    )


class StubSafeGateway:
    def __init__(self, source_ref: str) -> None:
        self.source_ref = source_ref
        self.calls: list[str] = []

    def fetch_job_artifact(self, url, *, principal, workspace_id):
        self.calls.append(url)
        return self.source_ref


def test_existing_gate_url_fetcher_only_normalizes_authorized_artifact(
    tmp_path: Path,
) -> None:
    artifacts = SQLiteSessionStore("sqlite:///gated-job.db", tmp_path)
    source_ref = "artifact:safe-job:1"
    envelope = json.dumps(
        {
            "ok": True,
            "data": {
                "title": "Agent Engineer",
                "company": "Example Corp",
                "location": "Shanghai",
                "responsibilities": ["Build agent systems"],
                "requirements": ["Python experience"],
                "retrieved_at": "2026-08-17T10:00:00Z",
                "final_url": "https://jobs.example.test/roles/42",
                "content_sha256": "a" * 64,
            },
        }
    )
    namespace = uuid5(NAMESPACE_URL, "safe-job-test")
    artifacts.save_tool_artifact(
        source_ref=source_ref,
        session_id=namespace,
        turn_id=uuid5(namespace, "turn"),
        tool_name="result_envelope",
        content=envelope,
        call_id="call-1",
        server_id="jobs",
        snapshot_id="snapshot-1",
        schema_hash="b" * 64,
        requested_url="https://jobs.example.test/roles/42",
        final_url="https://jobs.example.test/roles/42",
        content_sha256=sha256(envelope.encode()).hexdigest(),
        source_content_sha256="a" * 64,
        access_level="restricted",
        principal=PRINCIPAL,
    )
    gateway = StubSafeGateway(source_ref)
    fetcher = ExistingGateStableUrlFetcher(gateway, artifacts)

    result = fetcher.fetch(
        "https://jobs.example.test/roles/42",
        principal=PRINCIPAL,
        workspace_id="ws_demo",
    )

    assert gateway.calls == ["https://jobs.example.test/roles/42"]
    assert result.artifact_ref == source_ref
    assert result.source_content_sha256 == "a" * 64
    assert "Build agent systems" in result.markdown
