from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from starter_agent.cv_workbench.contracts import MatchAnalysis, ResumeVersion
from starter_agent.cv_workbench.runtime import create_workbench_runtime
from starter_agent.interfaces.capabilities_api import (
    ManagementPrincipal,
    get_management_principal,
)
from starter_agent.interfaces.workbench_api import (
    create_workbench_router,
    install_workbench_error_handlers,
)
from starter_agent.knowledge.models import KnowledgeScope


RESUME_MARKDOWN = """# 项目经历

参与 AI 研究课程，完成金融论文和模型实验。

Academic Conference Program Committee Recommendation System：使用 React 完成前端开发，对接推荐 API 进行系统联调，并落地前端 AI 应用。
"""
JOB_MARKDOWN = """# 前端开发工程师

负责 React 前端开发、系统联调和前端 AI 应用
"""


class NeverCalledTailoringGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        raise AssertionError("legacy analysis must be rejected before provider call")


def make_client(tmp_path: Path, *, tailoring_generator=None):
    runtime = create_workbench_runtime(
        f"sqlite:///evidence-v2-{uuid4().hex}.db",
        tmp_path,
        tailoring_generator=tailoring_generator,
    )
    api = FastAPI()
    install_workbench_error_handlers(api)
    api.include_router(create_workbench_router(lambda: runtime))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(
        subject="local-user",
        role="admin",
    )
    return TestClient(api), runtime


def create_match_fixture(client: TestClient):
    workspace = client.post(
        "/v1/workbench/workspaces",
        json={
            "workspace_id": "ws_evidence_v2",
            "name": "Evidence v2",
            "target_roles": ["Frontend Engineer"],
            "keywords": ["React"],
        },
    )
    assert workspace.status_code == 201, workspace.text
    imported = client.post(
        "/v1/workbench/resumes/imports",
        json={
            "operation_id": "op_import_evidence_v2",
            "idempotency_key": "import-evidence-v2",
            "workspace_id": "ws_evidence_v2",
            "resume_id": "res_evidence_v2",
            "branch_id": "rb_evidence_v2",
            "version_id": "rv_evidence_v2",
            "resume_name": "Evidence Resume",
            "filename": "resume.md",
            "content": RESUME_MARKDOWN,
            "confirmed_authorized": True,
        },
    )
    assert imported.status_code == 201, imported.text
    candidate = client.post(
        "/v1/workbench/job-candidates",
        json={
            "candidate_id": "jc_evidence_v2",
            "workspace_id": "ws_evidence_v2",
            "title": "Frontend Engineer",
            "company": "Example",
            "filename": "frontend.md",
            "content": JOB_MARKDOWN,
            "confirmed_authorized": True,
        },
    )
    assert candidate.status_code == 201, candidate.text
    retained = client.post(
        "/v1/workbench/job-candidates/retain",
        json={
            "candidate_id": "jc_evidence_v2",
            "workspace_id": "ws_evidence_v2",
            "operation_id": "op_retain_evidence_v2",
            "idempotency_key": "retain-evidence-v2",
        },
    )
    assert retained.status_code == 200, retained.text
    return retained.json()["snapshot_id"]


def evaluate(client: TestClient, snapshot_id: str, *, suffix: str):
    return client.post(
        "/v1/workbench/match-analyses/evaluate",
        json={
            "analysis_id": f"ma_evidence_v2_{suffix}",
            "operation_id": f"op_evidence_v2_{suffix}",
            "idempotency_key": f"evaluate-evidence-v2-{suffix}",
            "workspace_id": "ws_evidence_v2",
            "resume_version_id": "rv_evidence_v2",
            "job_snapshot_id": snapshot_id,
        },
    )


def test_evaluate_selects_react_chunk_and_reuses_unchanged_v2_analysis(
    tmp_path: Path,
) -> None:
    client, runtime = make_client(tmp_path)
    try:
        snapshot_id = create_match_fixture(client)
        version = runtime.store.get(
            ResumeVersion,
            "rv_evidence_v2",
            principal="local-user",
        )
        scope = KnowledgeScope(
            user_id="local-user",
            project_id="ws_evidence_v2",
        )
        resume_text = runtime.versions.content.read(
            version.content,
            principal="local-user",
            workspace_id="ws_evidence_v2",
        )
        selection = runtime.evidence_selector.select(
            "负责 React 前端开发、系统联调和前端 AI 应用",
            scope=scope,
            knowledge_base_id=UUID(version.content.knowledge_base_id),
            document_id=UUID(version.content.document_id),
            markdown=resume_text,
        )
        assert selection.evidence

        first = evaluate(client, snapshot_id, suffix="first")
        second = evaluate(client, snapshot_id, suffix="second")

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        first_value = first.json()
        second_value = second.json()
        assert first_value["rule_version"] == "match-rule.v2"
        assert first_value["validator_version"] == "match-result-validator.v2"
        assert first_value["reused"] is False
        assert second_value["reused"] is True
        assert second_value["analysis_id"] == first_value["analysis_id"]
        positive = next(
            item
            for item in first_value["requirements"]
            if item["verdict"] in {"matched", "partial"}
        )
        assert "React" in positive["evidence"][0]["quote"]
        assert "金融论文" not in positive["evidence"][0]["quote"]
        assert positive["evidence"][0]["source_ref"].startswith(
            "knowledge-chunk://"
        )
        analyses = runtime.store.list(MatchAnalysis, principal="local-user").items
        assert len(analyses) == 1
    finally:
        runtime.close()


def test_tailoring_endpoint_requires_one_time_upgrade_for_v1_analysis(
    tmp_path: Path,
) -> None:
    generator = NeverCalledTailoringGenerator()
    client, runtime = make_client(tmp_path, tailoring_generator=generator)
    try:
        snapshot_id = create_match_fixture(client)
        current = evaluate(client, snapshot_id, suffix="current")
        assert current.status_code == 201, current.text
        v2 = runtime.store.get(
            MatchAnalysis,
            current.json()["analysis_id"],
            principal="local-user",
        )
        v1 = v2.model_copy(
            update={
                "analysis_id": "ma_evidence_legacy_v1",
                "rule_version": "match-rule.v1",
                "validator_version": "match-result-validator.v1",
            }
        )
        runtime.store.create(v1, principal="local-user")
        draft = client.post(
            "/v1/workbench/resume-versions/rv_evidence_v2/drafts",
            json={
                "draft_id": "rd_evidence_legacy_v1",
                "workspace_id": "ws_evidence_v2",
                "branch_id": "rb_evidence_v2",
            },
        )
        assert draft.status_code == 201, draft.text

        response = client.post(
            "/v1/workbench/match-analyses/ma_evidence_legacy_v1/tailored-resume-candidates",
            json={
                "workspace_id": "ws_evidence_v2",
                "draft_id": "rd_evidence_legacy_v1",
            },
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == (
            "tailoring_analysis_upgrade_required"
        )
        assert response.json()["error"]["recovery_action"] == (
            "reanalyze_with_current_rule"
        )
        assert generator.calls == 0
    finally:
        runtime.close()
