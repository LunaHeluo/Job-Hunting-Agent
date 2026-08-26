from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from starter_agent.cv_workbench.contracts import (
    ContentReference,
    Job,
    JobSnapshot,
    MatchAnalysis,
    Resume,
    ResumeBranch,
    ResumeBranchType,
    ResumeNodeType,
    ResumeVersion,
    ResumeVersionStatus,
    Workspace,
)
from starter_agent.cv_workbench.store import SQLiteWorkbenchStore
from starter_agent.cv_workbench.suggestions import SuggestionService
from starter_agent.cv_workbench.versioning import ResumeVersionService
from starter_agent.domain.models import ModelResponse


PRINCIPAL = "local-user"


class SequencedProvider:
    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[object]] = []

    async def complete(self, messages, model, tools, **_kwargs):
        self.calls.append(messages)
        return ModelResponse(
            content=self.responses.pop(0),
            provider=self.name,
            model=model,
        )


def tailoring_module():
    return importlib.import_module("starter_agent.cv_workbench.tailoring")


def generation_request(module):
    return module.TailoringGenerationRequest(
        analysis_id="ma_1",
        draft_id="rd_1",
        draft_revision=1,
        requirements=(
            {
                "requirement_id": "req_1",
                "original_text": "需要 Python API 经验",
            },
        ),
        blocks=(
            module.TailoringBlock(
                block_id="b1",
                original_text="负责 Python API",
            ),
        ),
        evidence=(
            module.TailoringEvidence(
                evidence_id="ev_1",
                requirement_id="req_1",
                quote="负责 Python API",
            ),
        ),
    )


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


class StaticGenerator:
    def __init__(self) -> None:
        self.result = None
        self.calls = []

    async def generate(self, request):
        self.calls.append(request)
        return self.result


def setup_tailoring_service(tmp_path):
    module = tailoring_module()
    now = datetime(2026, 8, 26, tzinfo=UTC)
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'tailoring.db').as_posix()}",
        tmp_path,
    )
    store.create(
        Workspace(
            workspace_id="ws_demo",
            owner_id=PRINCIPAL,
            name="Demo",
            revision=1,
            created_at=now,
            updated_at=now,
        ),
        principal=PRINCIPAL,
    )
    store.create(
        Resume(
            resume_id="res_demo",
            owner_id=PRINCIPAL,
            name="Resume",
            latest_version_id=None,
            revision=1,
            created_at=now,
            updated_at=now,
        ),
        principal=PRINCIPAL,
    )
    store.create(
        ResumeBranch(
            branch_id="rb_master",
            resume_id="res_demo",
            name="master",
            branch_type=ResumeBranchType.MASTER,
            base_version_id="rv_master_v1",
            revision=1,
            created_at=now,
            updated_at=now,
        ),
        principal=PRINCIPAL,
    )
    markdown = "# 工作经历\n\n使用 Python 与 FastAPI 构建服务\n"
    normalized_sha = sha256(markdown.encode("utf-8")).hexdigest()
    repository = FakeContentRepository(
        {"artifact:version:rv_master_v1": markdown}
    )
    version = ResumeVersion(
        version_id="rv_master_v1",
        resume_id="res_demo",
        branch_id="rb_master",
        parent_version_id=None,
        branch_base_version_id="rv_master_v1",
        node_type=ResumeNodeType.BASE,
        version_number=1,
        label="Master v1",
        content=ContentReference(
            content_sha256=normalized_sha,
            artifact_id="artifact:version:rv_master_v1",
        ),
        status=ResumeVersionStatus.CONFIRMED,
        revision=1,
        created_by=PRINCIPAL,
        created_at=now,
        confirmed_at=now,
    )
    store.create(version, principal=PRINCIPAL)
    store.link_to_workspace("ws_demo", "res_demo", principal=PRINCIPAL)
    job = Job(
        job_id="job_demo",
        owner_id=PRINCIPAL,
        title="Backend Engineer",
        company="Example",
        user_status="saved",
        revision=1,
        created_at=now,
        updated_at=now,
    )
    store.create(job, principal=PRINCIPAL)
    snapshot = JobSnapshot(
        snapshot_id="js_demo",
        job_id=job.job_id,
        title=job.title,
        company=job.company,
        content=ContentReference(
            content_sha256="b" * 64,
            artifact_id="artifact:job:js_demo",
        ),
        verified=True,
        captured_at=now,
        verified_at=now,
    )
    store.create(snapshot, principal=PRINCIPAL)
    store.link_to_workspace("ws_demo", job.job_id, principal=PRINCIPAL)
    analysis = MatchAnalysis.model_validate(
        {
            "analysis_id": "ma_demo",
            "workspace_id": "ws_demo",
            "resume_version_id": version.version_id,
            "resume_content_sha256": version.content.content_sha256,
            "job_snapshot_id": snapshot.snapshot_id,
            "job_content_sha256": snapshot.content.content_sha256,
            "status": "validated",
            "rule_version": "match-rule.v1",
            "validator_version": "validator.v1",
            "total_score": 50.0,
            "requirements": [
                {
                    "requirement_id": "req_python",
                    "original_text": "具备 Python 后端开发经验",
                    "category": "required",
                    "importance": 5,
                    "verdict": "matched",
                    "evidence": [
                        {
                            "chunk_id": "chunk-python",
                            "source_ref": "resume.md@v1#L3-L3",
                            "content_sha256": "c" * 64,
                            "quote": "使用 Python 与 FastAPI 构建服务",
                        }
                    ],
                    "explanation": "有直接证据",
                },
                {
                    "requirement_id": "req_kubernetes",
                    "original_text": "熟悉 Kubernetes",
                    "category": "preferred",
                    "importance": 2,
                    "verdict": "missing",
                    "evidence": [],
                    "explanation": "没有证据",
                },
            ],
            "revision": 1,
            "created_at": now,
        }
    )
    store.create(analysis, principal=PRINCIPAL)
    versions = ResumeVersionService(store=store, content=repository)
    versions.create_branch(
        branch_id="rb_tailored",
        resume_id="res_demo",
        name="Tailored",
        branch_type=ResumeBranchType.DIRECTION,
        base_version_id=version.version_id,
        principal=PRINCIPAL,
    )
    draft = versions.create_draft(
        draft_id="rd_tailored",
        workspace_id="ws_demo",
        base_version_id=version.version_id,
        branch_id="rb_tailored",
        principal=PRINCIPAL,
    )
    suggestions = SuggestionService(store=store, versions=versions)
    fake = StaticGenerator()
    service = module.TailoredResumeService(
        store=store,
        versions=versions,
        suggestions=suggestions,
        generator=fake,
    )
    normalized = versions.normalizer.normalize(markdown)
    paragraph = next(block for block in normalized.blocks if block.kind == "paragraph")
    return module, service, fake, analysis, draft, store, paragraph.block_id


@pytest.mark.asyncio
async def test_provider_generator_runs_reflection_before_writer() -> None:
    module = tailoring_module()
    provider = SequencedProvider(
        [
            '{"issues_found":0,"issues":[],"notes":"证据可用"}',
            (
                '{"candidates":[{"block_id":"b1",'
                '"proposed_text":"交付 Python API","reason":"贴合岗位",'
                '"requirement_ids":["req_1"],"evidence_ids":["ev_1"],'
                '"risk":"请确认职责边界"}]}'
            ),
        ]
    )
    generator = module.ProviderTailoredResumeGenerator(
        provider_resolver=lambda _name: provider,
        provider_name="fake",
        model="fake-model",
    )

    result = await generator.generate(generation_request(module))

    assert len(provider.calls) == 2
    assert "反思" in provider.calls[0][0].content
    assert "撰写" in provider.calls[1][0].content
    assert result.reflection.notes == "证据可用"
    assert result.candidates[0].block_id == "b1"


@pytest.mark.asyncio
async def test_provider_generator_rejects_non_json_writer_output() -> None:
    module = tailoring_module()
    provider = SequencedProvider(
        [
            '{"issues_found":0,"issues":[],"notes":"证据可用"}',
            "not-json",
        ]
    )
    generator = module.ProviderTailoredResumeGenerator(
        provider_resolver=lambda _name: provider,
        provider_name="fake",
        model="fake-model",
    )

    with pytest.raises(
        module.TailoringServiceError,
        match="tailoring_output_invalid",
    ):
        await generator.generate(generation_request(module))


@pytest.mark.asyncio
async def test_tailoring_creates_only_positive_evidence_bound_suggestions(
    tmp_path,
) -> None:
    module, service, fake, analysis, draft, store, block_id = (
        setup_tailoring_service(tmp_path)
    )
    fake.result = module.TailoringGenerationResult(
        reflection=module.ReflectionResult(notes="证据可用"),
        candidates=(
            module.GeneratedTailoringCandidate(
                block_id=block_id,
                proposed_text="交付 Python 与 FastAPI 服务",
                reason="贴合岗位要求",
                requirement_ids=("req_python",),
                evidence_ids=("ev_1",),
                risk="请确认职责边界",
            ),
        ),
    )

    result = await service.generate_candidates(
        module.TailoringCommand(
            workspace_id="ws_demo",
            analysis_id=analysis.analysis_id,
            draft_id=draft.draft_id,
        ),
        principal=PRINCIPAL,
    )

    assert [item["requirement_id"] for item in fake.calls[0].requirements] == [
        "req_python"
    ]
    assert len(result.items) == 1
    assert result.items[0].change_type == "ai_tailor_v1"
    assert result.items[0].resume_evidence == analysis.requirements[0].evidence
    assert store.get(
        type(draft), draft.draft_id, principal=PRINCIPAL
    ).revision == draft.revision


@pytest.mark.asyncio
async def test_tailoring_rejects_unknown_evidence_and_new_numeric_claim(
    tmp_path,
) -> None:
    module, service, fake, analysis, draft, _store, block_id = (
        setup_tailoring_service(tmp_path)
    )
    fake.result = module.TailoringGenerationResult(
        reflection=module.ReflectionResult(notes="证据可用"),
        candidates=(
            module.GeneratedTailoringCandidate(
                block_id=block_id,
                proposed_text="交付 Python 服务，性能提升 99%",
                reason="贴合岗位要求",
                requirement_ids=("req_python",),
                evidence_ids=("unknown",),
            ),
        ),
    )

    result = await service.generate_candidates(
        module.TailoringCommand(
            workspace_id="ws_demo",
            analysis_id=analysis.analysis_id,
            draft_id=draft.draft_id,
        ),
        principal=PRINCIPAL,
    )

    assert result.items == ()
    assert set(result.rejected_reasons) == {
        "new_numeric_claim",
        "unknown_evidence",
    }


@pytest.mark.asyncio
async def test_tailoring_reuses_candidates_for_same_draft_revision(
    tmp_path,
) -> None:
    module, service, fake, analysis, draft, _store, block_id = (
        setup_tailoring_service(tmp_path)
    )
    fake.result = module.TailoringGenerationResult(
        reflection=module.ReflectionResult(notes="证据可用"),
        candidates=(
            module.GeneratedTailoringCandidate(
                block_id=block_id,
                proposed_text="交付 Python 与 FastAPI 服务",
                reason="贴合岗位要求",
                requirement_ids=("req_python",),
                evidence_ids=("ev_1",),
            ),
        ),
    )
    command = module.TailoringCommand(
        workspace_id="ws_demo",
        analysis_id=analysis.analysis_id,
        draft_id=draft.draft_id,
    )

    first = await service.generate_candidates(command, principal=PRINCIPAL)
    second = await service.generate_candidates(command, principal=PRINCIPAL)

    assert len(first.items) == 1
    assert second.items == first.items
    assert second.reused is True
    assert len(fake.calls) == 1
