from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from datetime import UTC, datetime
from zipfile import ZipFile
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient

from starter_agent.cv_workbench.runtime import create_workbench_runtime
from starter_agent.cv_workbench.contracts import Job, ResumeNodeType, ResumeVersion, ResumeVersionStatus
from starter_agent.cv_workbench.tailoring import (
    GeneratedTailoringCandidate,
    ReflectionResult,
    TailoringGenerationResult,
)
from starter_agent.interfaces.capabilities_api import (
    ManagementPrincipal,
    get_management_principal,
)
from starter_agent.interfaces.workbench_api import (
    create_workbench_router,
    install_workbench_error_handlers,
)


class FakeTailoringGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        evidence = request.evidence[0]
        block = next(
            item for item in request.blocks if item.block_id == request.blocks[0].block_id
        )
        return TailoringGenerationResult(
            reflection=ReflectionResult(notes="verified evidence only"),
            candidates=(
                GeneratedTailoringCandidate(
                    block_id=block.block_id,
                    proposed_text=block.original_text.replace("负责", "实现", 1),
                    reason="Align the verified Python API evidence with the role.",
                    requirement_ids=(evidence.requirement_id,),
                    evidence_ids=(evidence.evidence_id,),
                    risk="Review wording before accepting.",
                ),
            ),
        )


def make_client(tmp_path: Path, *, tailoring_generator=None):
    options = {}
    if tailoring_generator is not None:
        options["tailoring_generator"] = tailoring_generator
    runtime = create_workbench_runtime(
        f"sqlite:///api-{uuid4().hex}.db", tmp_path, **options
    )
    api = FastAPI()
    install_workbench_error_handlers(api)
    api.include_router(create_workbench_router(lambda: runtime))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(
        subject="local-user", role="admin"
    )
    return TestClient(api), runtime


def workspace_payload():
    return {
        "workspace_id": "ws_api",
        "name": "API Workspace",
        "target_roles": ["Backend Engineer"],
        "keywords": ["Python"],
    }


def test_workspace_api_has_pagination_home_and_stable_revision_error(
    tmp_path: Path,
) -> None:
    client, runtime = make_client(tmp_path)
    try:
        created = client.post("/v1/workbench/workspaces", json=workspace_payload())
        assert created.status_code == 201
        assert created.json()["revision"] == 1

        listed = client.get("/v1/workbench/workspaces?limit=10")
        assert listed.status_code == 200
        assert [item["workspace_id"] for item in listed.json()["items"]] == [
            "ws_api"
        ]
        home = client.get("/v1/workbench/workspaces/ws_api/home")
        assert home.status_code == 200
        assert "content" not in home.text

        patch = {
            "name": "Changed",
            "expected_revision": 1,
            "target_roles": ["Backend Engineer"],
            "keywords": ["Python"],
        }
        assert client.patch(
            "/v1/workbench/workspaces/ws_api", json=patch
        ).status_code == 200
        conflict = client.patch(
            "/v1/workbench/workspaces/ws_api", json=patch
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "error": {
                "code": "revision_conflict",
                "message": "The resource changed; compare and retry.",
                "retryable": True,
                "authoritative_revision": 2,
                "recovery_action": "reload_and_compare",
            }
        }
    finally:
        runtime.close()


def test_resume_import_and_job_candidate_api_respect_commit_boundary(
    tmp_path: Path,
) -> None:
    tailoring_generator = FakeTailoringGenerator()
    client, runtime = make_client(
        tmp_path,
        tailoring_generator=tailoring_generator,
    )
    try:
        client.post("/v1/workbench/workspaces", json=workspace_payload())
        imported = client.post(
            "/v1/workbench/resumes/imports",
            json={
                "operation_id": "op_api_import",
                "idempotency_key": "api-import",
                "workspace_id": "ws_api",
                "resume_id": "res_api",
                "branch_id": "rb_api_master",
                "version_id": "rv_api_master_v1",
                "resume_name": "API Resume",
                "filename": "resume.md",
                "content": "# Summary\n\n负责 Python API engineering\n",
                "confirmed_authorized": True,
            },
        )
        assert imported.status_code == 201
        assert client.get("/v1/workbench/resumes/res_api").status_code == 200
        operations = client.get("/v1/workbench/operations?workspace_id=ws_api")
        assert operations.status_code == 200
        assert any(item["operation_id"] == "op_api_import" for item in operations.json()["items"])

        candidate = client.post(
            "/v1/workbench/job-candidates",
            json={
                "candidate_id": "jc_api_job",
                "workspace_id": "ws_api",
                "title": "Backend Engineer",
                "company": "Example",
                "filename": "job.txt",
                "content": "# Backend Engineer\n\n- Build Python APIs\n",
                "confirmed_authorized": True,
            },
        )
        assert candidate.status_code == 201
        assert runtime.store.list(Job, principal="local-user").items == ()
        retained = client.post(
            "/v1/workbench/job-candidates/retain",
            json={
                "candidate_id": "jc_api_job",
                "workspace_id": "ws_api",
                "operation_id": "op_api_job",
                "idempotency_key": "api-job",
            },
        )
        assert retained.status_code == 200
        assert client.get(
            f"/v1/workbench/job-snapshots/{retained.json()['snapshot_id']}"
        ).status_code == 200
        assert client.get("/v1/workbench/jobs?workspace_id=ws_api").json()["items"][0]["job_id"] == retained.json()["job_id"]
        snapshot_content = client.get(
            f"/v1/workbench/job-snapshots/{retained.json()['snapshot_id']}/content?workspace_id=ws_api"
        )
        assert snapshot_content.status_code == 200
        analysis = client.post(
            "/v1/workbench/match-analyses/evaluate",
            json={
                "analysis_id": "ma_api_evaluate",
                "operation_id": "op_api_evaluate",
                "idempotency_key": "api-evaluate",
                "workspace_id": "ws_api",
                "resume_version_id": "rv_api_master_v1",
                "job_snapshot_id": retained.json()["snapshot_id"],
            },
        )
        assert analysis.status_code == 201, analysis.text
        assert analysis.json()["requirements"]
        assert any(
            item["verdict"] in {"matched", "partial"}
            for item in analysis.json()["requirements"]
        )
        listed_analyses = client.get(
            "/v1/workbench/match-analyses?workspace_id=ws_api"
        )
        assert listed_analyses.status_code == 200
        assert listed_analyses.json()["items"][0]["analysis_id"] == "ma_api_evaluate"
        draft = client.post(
            "/v1/workbench/resume-versions/rv_api_master_v1/drafts",
            json={
                "draft_id": "rd_api_suggestion",
                "workspace_id": "ws_api",
                "branch_id": "rb_api_master",
            },
        )
        assert draft.status_code == 201
        generated = client.post(
            "/v1/workbench/match-analyses/ma_api_evaluate/suggestion-candidates",
            json={"workspace_id": "ws_api", "draft_id": "rd_api_suggestion"},
        )
        assert generated.status_code == 201, generated.text
        assert len(generated.json()["items"]) == 1
        tailored = client.post(
            "/v1/workbench/match-analyses/ma_api_evaluate/tailored-resume-candidates",
            json={"workspace_id": "ws_api", "draft_id": "rd_api_suggestion"},
        )
        assert tailored.status_code == 201, tailored.text
        assert tailored.json()["reused"] is False
        assert tailoring_generator.calls == 1
        ai_suggestion = tailored.json()["items"][0]
        assert ai_suggestion["change_type"] == "ai_tailor_v1"
        assert ai_suggestion["resume_evidence"]

        reused = client.post(
            "/v1/workbench/match-analyses/ma_api_evaluate/tailored-resume-candidates",
            json={"workspace_id": "ws_api", "draft_id": "rd_api_suggestion"},
        )
        assert reused.status_code == 201, reused.text
        assert reused.json()["reused"] is True
        assert tailoring_generator.calls == 1

        accepted = client.post(
            "/v1/workbench/suggestions/batch-decisions",
            json={
                "workspace_id": "ws_api",
                "accept_ids": [ai_suggestion["suggestion_id"]],
                "reject_ids": [],
                "edited_text_by_id": {
                    ai_suggestion["suggestion_id"]: "实现 Python API engineering"
                },
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["draft"]["revision"] == 2
        draft_content = client.get(
            "/v1/workbench/drafts/rd_api_suggestion/content?workspace_id=ws_api"
        )
        assert draft_content.status_code == 200
        assert "实现 Python API engineering" in draft_content.json()["markdown"]
        version = client.get("/v1/workbench/resume-versions/rv_api_master_v1")
        assert version.status_code == 200
        assert version.json()["revision"] == 1
    finally:
        runtime.close()


def test_uploaded_text_jd_enters_the_existing_candidate_confirmation_flow(
    tmp_path: Path,
) -> None:
    client, runtime = make_client(tmp_path)
    try:
        assert client.post("/v1/workbench/workspaces", json=workspace_payload()).status_code == 201
        uploaded = client.post(
            "/v1/workbench/job-candidates/upload",
            data={
                "candidate_id": "jc_uploaded_jd",
                "workspace_id": "ws_api",
                "title": "Backend Engineer",
                "company": "Example",
                "location": "Shanghai",
                "confirmed_authorized": "true",
            },
            files={"file": ("backend.txt", b"# Backend Engineer\n\nPython and FastAPI", "text/plain")},
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["extraction_method"] == "text"
        assert uploaded.json()["candidate"]["candidate_only"] is True

        retained = client.post(
            "/v1/workbench/job-candidates/retain",
            json={
                "candidate_id": "jc_uploaded_jd",
                "workspace_id": "ws_api",
                "operation_id": "op_uploaded_jd",
                "idempotency_key": "op_uploaded_jd",
            },
        )
        assert retained.status_code == 200
        assert retained.json()["snapshot_id"].startswith("js_")
    finally:
        runtime.close()


def test_confirmed_version_exports_pdf_and_docx_as_immutable_restricted_artifacts(tmp_path: Path) -> None:
    client, runtime = make_client(tmp_path)
    try:
        client.post("/v1/workbench/workspaces", json=workspace_payload())
        imported = client.post(
            "/v1/workbench/resumes/imports",
            json={
                "operation_id": "op_export_import",
                "idempotency_key": "export-import",
                "workspace_id": "ws_api",
                "resume_id": "res_export",
                "branch_id": "rb_export",
                "version_id": "rv_export_v1",
                "resume_name": "张三简历",
                "filename": "resume.md",
                "content": "# 张三\n\n## 工作经历\n\n- 设计 Python API，延迟降低 30%\n- [作品集](https://example.com)\n",
                "confirmed_authorized": True,
            },
        )
        assert imported.status_code == 201

        def create_export(format: str, suffix: str):
            return client.post(
                "/v1/workbench/exports",
                json={
                    "operation_id": f"op_export_{suffix}",
                    "idempotency_key": f"export-rv-v1-{format}",
                    "export_id": f"exp_{suffix}",
                    "workspace_id": "ws_api",
                    "resume_version_id": "rv_export_v1",
                    "format": format,
                    "settings": {"title": "张三简历"},
                },
            )

        pdf = create_export("pdf", "pdf")
        assert pdf.status_code == 201, pdf.text
        assert pdf.json()["operation"]["status"] == "committed"
        pdf_record = pdf.json()["export"]
        downloaded = client.get(f"/v1/workbench/exports/{pdf_record['export_id']}/download")
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"%PDF-")
        assert downloaded.headers["cache-control"] == "private, no-store"

        repeated = create_export("pdf", "pdf_second")
        assert repeated.status_code == 201
        assert repeated.json()["export"]["export_id"] == pdf_record["export_id"]
        assert repeated.json()["export"]["content_sha256"] == pdf_record["content_sha256"]

        docx = create_export("docx", "docx")
        assert docx.status_code == 201, docx.text
        docx_record = docx.json()["export"]
        editable = client.get(f"/v1/workbench/exports/{docx_record['export_id']}/download")
        assert editable.content.startswith(b"PK")
        with ZipFile(BytesIO(editable.content)) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")
            relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "张三" in document_xml
        assert "w:hyperlink" in document_xml
        assert "https://example.com" in relationships

        root = runtime.store.get(ResumeVersion, "rv_export_v1", principal="local-user")
        pending = ResumeVersion.model_validate(root.model_dump() | {
            "version_id": "rv_export_pending",
            "parent_version_id": root.version_id,
            "node_type": ResumeNodeType.DERIVED,
            "version_number": 2,
            "label": "Pending",
            "status": ResumeVersionStatus.PENDING_CONFIRMATION,
            "confirmed_at": None,
            "revision": 1,
            "created_at": datetime.now(UTC),
        })
        runtime.store.create(pending, principal="local-user")
        blocked = client.post("/v1/workbench/exports", json={
            "operation_id": "op_export_blocked", "idempotency_key": "blocked",
            "export_id": "exp_blocked", "workspace_id": "ws_api",
            "resume_version_id": pending.version_id, "format": "pdf",
        })
        assert blocked.status_code == 422
        assert blocked.json()["error"]["code"] == "export_requires_confirmed_version"
    finally:
        runtime.close()


def test_application_events_require_confirmation_and_are_append_only_idempotent(tmp_path: Path) -> None:
    client, runtime = make_client(tmp_path)
    try:
        client.post("/v1/workbench/workspaces", json=workspace_payload())
        assert client.post("/v1/workbench/resumes/imports", json={
            "operation_id": "op_app_import", "idempotency_key": "app-import",
            "workspace_id": "ws_api", "resume_id": "res_app", "branch_id": "rb_app",
            "version_id": "rv_app_v1", "resume_name": "Application Resume",
            "filename": "resume.md", "content": "# Resume\n\nPython API\n", "confirmed_authorized": True,
        }).status_code == 201
        assert client.post("/v1/workbench/job-candidates", json={
            "candidate_id": "jc_app", "workspace_id": "ws_api", "title": "Backend Engineer",
            "company": "Example", "filename": "job.txt", "content": "# Role\n\nPython\n",
            "confirmed_authorized": True,
        }).status_code == 201
        retained = client.post("/v1/workbench/job-candidates/retain", json={
            "candidate_id": "jc_app", "workspace_id": "ws_api", "operation_id": "op_app_job",
            "idempotency_key": "app-job",
        }).json()
        command = {
            "operation_id": "op_app_create", "idempotency_key": "app-create",
            "application_id": "app_backend", "event_id": "ae_applied",
            "workspace_id": "ws_api", "job_snapshot_id": retained["snapshot_id"],
            "resume_version_id": "rv_app_v1", "initial_status": "applied", "priority": 80,
            "next_action": "等待笔试通知", "note": "用户确认已投递", "user_confirmed": False,
        }
        blocked = client.post("/v1/workbench/applications", json=command)
        assert blocked.status_code == 422
        assert client.get("/v1/workbench/applications?workspace_id=ws_api").json()["items"] == []

        command["user_confirmed"] = True
        created = client.post("/v1/workbench/applications", json=command)
        assert created.status_code == 201, created.text
        application = created.json()["application"]
        assert application["current_status"] == "applied"
        assert [event["to_status"] for event in application["events"]] == ["to_decide", "to_apply", "applied"]

        event = {
            "operation_id": "op_app_interview", "idempotency_key": "app-interview",
            "event_id": "ae_interview", "workspace_id": "ws_api",
            "expected_revision": application["revision"], "to_status": "interview",
            "note": "用户确认进入面试", "next_action": "准备系统设计", "user_confirmed": True,
        }
        advanced = client.post("/v1/workbench/applications/app_backend/events", json=event)
        assert advanced.status_code == 201, advanced.text
        updated = advanced.json()["application"]
        assert updated["current_status"] == "interview"
        assert len(updated["events"]) == 4

        event["operation_id"] = "op_app_interview_retry"
        repeated = client.post("/v1/workbench/applications/app_backend/events", json=event)
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["application"]["revision"] == updated["revision"]
        assert len(repeated.json()["application"]["events"]) == 4

        listing = client.get("/v1/workbench/applications?workspace_id=ws_api&status=interview&query=Backend")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["job_snapshot"]["company"] == "Example"
    finally:
        runtime.close()


def test_openapi_exposes_minimum_workbench_resources_and_export_validation_contract(
    tmp_path: Path,
) -> None:
    client, runtime = make_client(tmp_path)
    try:
        paths = client.get("/openapi.json").json()["paths"]
        expected = {
            "/v1/workbench/workspaces",
            "/v1/workbench/workspaces/{workspace_id}/home",
            "/v1/workbench/resumes/imports",
            "/v1/workbench/resumes/{resume_id}/version-map",
            "/v1/workbench/merge-proposals",
            "/v1/workbench/job-candidates",
            "/v1/workbench/match-analyses",
            "/v1/workbench/suggestions/{suggestion_id}/decisions",
            "/v1/workbench/applications",
            "/v1/workbench/exports",
        }
        assert expected <= set(paths)
        invalid = client.post("/v1/workbench/exports", json={})
        assert invalid.status_code == 422
    finally:
        runtime.close()


def test_research_run_is_fail_closed_until_release_gate_is_current(
    tmp_path: Path,
) -> None:
    runtime = create_workbench_runtime(f"sqlite:///research-{uuid4().hex}.db", tmp_path)

    class Application:
        enabled = False
        runtime = SimpleNamespace(knowledge_scope=SimpleNamespace(user_id="local-user"))

        def delegation_route_enabled(self):
            return self.enabled

        async def start_job_research_delegation(self, **values):
            self.values = values
            return SimpleNamespace(
                parent_run_id="parent:research",
                child_task_id="task:research",
                child_run_id="child:research",
                status="queued",
            )

    application = Application()
    api = FastAPI()
    install_workbench_error_handlers(api)
    api.include_router(create_workbench_router(lambda: runtime, lambda: application))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(subject="local-user", role="admin")
    client = TestClient(api)
    try:
        client.post("/v1/workbench/workspaces", json=workspace_payload())
        body = {"workspace_id": "ws_api", "query": "Python backend"}
        blocked = client.post("/v1/workbench/research-runs", json=body)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "delegation_release_gate_not_current"

        application.enabled = True
        started = client.post("/v1/workbench/research-runs", json=body)
        assert started.status_code == 202
        assert started.json()["parent_run_id"] == "parent:research"
        assert started.json()["candidate_only"] is True
    finally:
        runtime.close()
