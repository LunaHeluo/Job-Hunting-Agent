from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from starter_agent.cv_workbench.bindings import (
    EvidenceBindingService,
    EvidenceSourceSnapshot,
)
from starter_agent.cv_workbench.contracts import (
    EvidenceReference,
    Job,
    JobSnapshot,
    MatchAnalysis,
    MatchStatus,
    Resume,
    ResumeBranch,
    ResumeVersion,
    Workspace,
)
from starter_agent.cv_workbench.matching import (
    AnalyzeCommand,
    CandidateRequirement,
    MatchCandidateEnvelope,
    MatchService,
    StoredMatchCandidate,
)
from starter_agent.cv_workbench.match_adapters import SessionMatchCandidateRepository
from starter_agent.cv_workbench.store import SQLiteWorkbenchStore
from starter_agent.infrastructure.session_store import SQLiteSessionStore


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


@dataclass
class MemoryCandidates:
    items: dict[str, MatchCandidateEnvelope] = field(default_factory=dict)

    def write(self, candidate, *, principal):
        ref = f"artifact:match:{candidate.analysis_id}"
        self.items[ref] = candidate
        return StoredMatchCandidate(ref, candidate.content_sha256)

    def read(self, artifact_ref, *, principal):
        return self.items[artifact_ref]


class MappingReader:
    def __init__(self, sources):
        self.sources = sources

    def inspect(self, source_ref, *, principal, workspace_id, now):
        return self.sources.get(
            source_ref, EvidenceSourceSnapshot(exists=False, authorized=True)
        )


def make_service(tmp_path: Path):
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'matching.db').as_posix()}", tmp_path
    )
    fixture = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )
    workspace = Workspace.model_validate(fixture["workspace"] | {"revision": 1})
    store.create(workspace, principal=PRINCIPAL)
    resume = Resume.model_validate(
        fixture["resume"] | {"latest_version_id": None, "revision": 1}
    )
    store.create(resume, principal=PRINCIPAL)
    branch = ResumeBranch.model_validate(
        {
            "branch_id": "rb_master",
            "resume_id": resume.resume_id,
            "name": "Master",
            "branch_type": "master",
            "base_version_id": "rv_master_v1",
            "job_snapshot_id": None,
            "archived": False,
            "revision": 1,
            "created_at": "2026-08-17T09:00:00+08:00",
            "updated_at": "2026-08-17T09:00:00+08:00",
            "allowed_actions": (),
        }
    )
    store.create(branch, principal=PRINCIPAL)
    version = ResumeVersion.model_validate(
        {
            "version_id": "rv_master_v1",
            "resume_id": resume.resume_id,
            "branch_id": branch.branch_id,
            "parent_version_id": None,
            "branch_base_version_id": "rv_master_v1",
            "node_type": "base",
            "version_number": 1,
            "label": "Master v1",
            "content": {
                "content_sha256": "a" * 64,
                "knowledge_base_id": "kb-demo",
                "document_id": "doc-resume",
                "document_version_id": "docv-resume-1",
            },
            "status": "confirmed",
            "revision": 1,
            "created_by": PRINCIPAL,
            "created_at": "2026-08-17T09:00:00+08:00",
            "confirmed_at": "2026-08-17T09:01:00+08:00",
        }
    )
    store.create(version, principal=PRINCIPAL)
    job = Job.model_validate(fixture["job"])
    snapshot = JobSnapshot.model_validate(fixture["job_snapshot"])
    store.create(job, principal=PRINCIPAL)
    store.create(snapshot, principal=PRINCIPAL)
    store.link_to_workspace(workspace.workspace_id, resume.resume_id, principal=PRINCIPAL)
    store.link_to_workspace(workspace.workspace_id, job.job_id, principal=PRINCIPAL)
    sources = {
        "knowledge-chunk://kb-demo/chunk-python": EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256="e" * 64,
            metadata={"chunk_id": "chunk-python"},
        ),
        "knowledge-chunk://kb-demo/chunk-api": EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256="f" * 64,
            metadata={"chunk_id": "chunk-api"},
        ),
    }
    reader = MappingReader(sources)
    bindings = EvidenceBindingService(store=store, readers={"chunk": reader})
    service = MatchService(
        store=store,
        candidates=MemoryCandidates(),
        evidence_reader=reader,
        evidence_bindings=bindings,
    )
    return service, store, bindings


def evidence(name: str, digest: str) -> EvidenceReference:
    return EvidenceReference(
        chunk_id=f"chunk-{name}",
        source_ref=f"knowledge-chunk://kb-demo/chunk-{name}",
        content_sha256=digest,
        quote=f"evidence for {name}",
    )


def command(*, analysis_id="ma_match_1", operation_id="op_match_1", complete=True):
    return AnalyzeCommand(
        analysis_id=analysis_id,
        operation_id=operation_id,
        idempotency_key=operation_id,
        workspace_id="ws_demo",
        resume_version_id="rv_master_v1",
        job_snapshot_id="js_acme_v1",
        complete=complete,
        requirements=(
            CandidateRequirement(
                original_text="精通 Python",
                category="required",
                importance=5,
                verdict="matched",
                evidence=(evidence("python", "e" * 64),),
                explanation="存在直接证据",
            ),
            CandidateRequirement(
                original_text="具有 API 设计经验",
                category="required",
                importance=5,
                verdict="partial",
                evidence=(evidence("api", "f" * 64),),
                explanation="证据部分覆盖",
            ),
            CandidateRequirement(
                original_text="熟悉 Kubernetes",
                category="preferred",
                importance=2,
                verdict="missing",
                evidence=(),
                explanation="未找到证据",
            ),
        ),
    )


def test_match_score_is_deterministic_and_operation_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    service, store, bindings = make_service(tmp_path)

    analysis = service.analyze(command(), principal=PRINCIPAL)
    replay = service.analyze(command(), principal=PRINCIPAL)

    assert analysis == replay
    assert analysis.status == MatchStatus.VALIDATED
    assert analysis.total_score == 60.0
    assert [(item.name, item.weight, item.score) for item in analysis.dimensions] == [
        ("required_requirements", 0.8, 75.0),
        ("preferred_requirements", 0.2, 0.0),
    ]
    assert len(store.list(MatchAnalysis, principal=PRINCIPAL).items) == 1
    assert bindings.audit(analysis.analysis_id, principal=PRINCIPAL).traceable == 2


def test_positive_claim_without_authorized_exact_evidence_becomes_missing(
    tmp_path: Path,
) -> None:
    service, _, _ = make_service(tmp_path)
    bad = CandidateRequirement(
        original_text="熟悉 Rust",
        category="required",
        importance=5,
        verdict="matched",
        evidence=(evidence("python", "0" * 64),),
        explanation="模型声称匹配",
    )
    requested = command(analysis_id="ma_bad_evidence", operation_id="op_bad_evidence")

    analysis = service.analyze(
        AnalyzeCommand(**(requested.__dict__ | {"requirements": (bad,)})),
        principal=PRINCIPAL,
    )

    assert analysis.total_score == 0.0
    assert analysis.requirements[0].verdict == "missing"
    assert analysis.requirements[0].evidence == ()


def test_partial_and_conflict_status_and_stale_preserve_historical_result(
    tmp_path: Path,
) -> None:
    service, _, _ = make_service(tmp_path)
    requested = command(
        analysis_id="ma_partial_match",
        operation_id="op_partial_match",
        complete=False,
    )
    conflict = CandidateRequirement(
        original_text="支撑百万流量",
        category="responsibility",
        importance=4,
        verdict="conflict",
        evidence=(),
        explanation="来源描述冲突",
    )
    analysis = service.analyze(
        AnalyzeCommand(**(requested.__dict__ | {"requirements": (*requested.requirements, conflict)})),
        principal=PRINCIPAL,
    )
    original_score = analysis.total_score
    original_requirements = analysis.requirements

    stale = service.refresh_staleness(
        analysis.analysis_id,
        current_resume_version_id="rv_master_v2",
        current_job_snapshot_id="js_acme_v2",
        principal=PRINCIPAL,
    )

    assert analysis.status == MatchStatus.PARTIAL
    assert stale.status == MatchStatus.STALE
    assert stale.stale_reason == "resume_version_changed,job_snapshot_changed"
    assert stale.total_score == original_score
    assert stale.requirements == original_requirements


def test_reanalysis_creates_new_record_instead_of_overwriting(tmp_path: Path) -> None:
    service, store, _ = make_service(tmp_path)
    first = service.analyze(command(), principal=PRINCIPAL)
    second = service.analyze(
        command(analysis_id="ma_match_2", operation_id="op_match_2"),
        principal=PRINCIPAL,
    )

    assert first.analysis_id != second.analysis_id
    assert len(store.list(MatchAnalysis, principal=PRINCIPAL).items) == 2


def test_match_candidate_survives_service_restart_in_restricted_artifact(
    tmp_path: Path,
) -> None:
    artifacts = SQLiteSessionStore("sqlite:///match-artifacts.db", tmp_path)
    repository = SessionMatchCandidateRepository(artifacts)
    requested = command()
    candidate = MatchCandidateEnvelope(
        analysis_id=requested.analysis_id,
        workspace_id=requested.workspace_id,
        resume_version_id=requested.resume_version_id,
        resume_content_sha256="a" * 64,
        job_snapshot_id=requested.job_snapshot_id,
        job_content_sha256="d" * 64,
        complete=requested.complete,
        requirements=requested.requirements,
    )

    stored = repository.write(candidate, principal=PRINCIPAL)
    reopened = SessionMatchCandidateRepository(
        SQLiteSessionStore("sqlite:///match-artifacts.db", tmp_path)
    )

    assert reopened.read(stored.artifact_ref, principal=PRINCIPAL) == candidate
