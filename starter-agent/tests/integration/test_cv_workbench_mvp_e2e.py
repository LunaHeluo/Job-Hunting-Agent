from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from starter_agent.cv_workbench.runtime import create_workbench_runtime
from starter_agent.interfaces.capabilities_api import ManagementPrincipal, get_management_principal
from starter_agent.interfaces.workbench_api import create_workbench_router, install_workbench_error_handlers


def client_for(tmp_path: Path):
    runtime = create_workbench_runtime(f"sqlite:///mvp-{uuid4().hex}.db", tmp_path)
    api = FastAPI()
    install_workbench_error_handlers(api)
    api.include_router(create_workbench_router(lambda: runtime))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(subject="mvp-user", role="admin")
    return TestClient(api), runtime


def test_mvp_real_business_loop_and_lineage(tmp_path: Path) -> None:
    client, runtime = client_for(tmp_path)
    try:
        assert client.post("/v1/workbench/workspaces", json={"workspace_id": "ws_mvp", "name": "Backend Search", "target_roles": ["Backend Engineer"]}).status_code == 201
        imported = client.post("/v1/workbench/resumes/imports", json={
            "operation_id": "op_mvp_import", "idempotency_key": "mvp-import", "workspace_id": "ws_mvp",
            "resume_id": "res_mvp", "branch_id": "rb_mvp_master", "version_id": "rv_mvp_master_v1",
            "resume_name": "Backend Resume", "filename": "resume.md",
            "content": "# Summary\n\n负责 Python FastAPI 服务与测试。\n", "confirmed_authorized": True,
        })
        assert imported.status_code == 201
        assert imported.json()["operation_id"] == "op_mvp_import"
        assert client.get("/v1/workbench/operations/op_mvp_import").json()["status"] == "committed"

        direction = client.post("/v1/workbench/resume-versions/rv_mvp_master_v1/branches", json={
            "branch_id": "rb_mvp_backend", "resume_id": "res_mvp", "name": "Backend", "branch_type": "direction",
        })
        assert direction.status_code == 201
        draft = client.post("/v1/workbench/resume-versions/rv_mvp_master_v1/drafts", json={"draft_id": "rd_mvp_backend", "workspace_id": "ws_mvp", "branch_id": "rb_mvp_backend"}).json()
        pending = client.post("/v1/workbench/drafts/rd_mvp_backend/versions", json={"workspace_id": "ws_mvp", "version_id": "rv_mvp_backend_v1", "label": "Backend v1", "expected_draft_revision": draft["revision"]}).json()
        confirmed_direction = client.post("/v1/workbench/resume-versions/rv_mvp_backend_v1/confirm", json={"workspace_id": "ws_mvp", "expected_revision": pending["revision"]})
        assert confirmed_direction.status_code == 200

        candidate = client.post("/v1/workbench/job-candidates", json={
            "candidate_id": "jc_mvp", "workspace_id": "ws_mvp", "title": "Backend Engineer", "company": "Example Co",
            "filename": "job.md", "content": "# Backend Engineer\n\n- Python FastAPI services\n- Automated testing\n", "confirmed_authorized": True,
        })
        assert candidate.status_code == 201
        promotion = client.post("/v1/workbench/job-candidates/retain", json={"candidate_id": "jc_mvp", "workspace_id": "ws_mvp", "operation_id": "op_mvp_job", "idempotency_key": "mvp-job"})
        assert promotion.status_code == 200
        snapshot_id = promotion.json()["snapshot_id"]
        replay = client.post("/v1/workbench/job-candidates/retain", json={"candidate_id": "jc_mvp", "workspace_id": "ws_mvp", "operation_id": "op_mvp_job", "idempotency_key": "mvp-job"})
        assert replay.status_code == 200
        assert replay.json()["snapshot_id"] == snapshot_id

        company = client.post("/v1/workbench/resume-versions/rv_mvp_backend_v1/branches", json={
            "branch_id": "rb_mvp_company", "resume_id": "res_mvp", "name": "Example Co", "branch_type": "company", "job_snapshot_id": snapshot_id,
        })
        assert company.status_code == 201
        company_draft = client.post("/v1/workbench/resume-versions/rv_mvp_backend_v1/drafts", json={"draft_id": "rd_mvp_company", "workspace_id": "ws_mvp", "branch_id": "rb_mvp_company"}).json()
        company_pending = client.post("/v1/workbench/drafts/rd_mvp_company/versions", json={"workspace_id": "ws_mvp", "version_id": "rv_mvp_company_v1", "label": "Example Co v1", "expected_draft_revision": company_draft["revision"]}).json()
        assert client.post("/v1/workbench/resume-versions/rv_mvp_company_v1/confirm", json={"workspace_id": "ws_mvp", "expected_revision": company_pending["revision"]}).status_code == 200

        version_map = client.get("/v1/workbench/resumes/res_mvp/version-map").json()
        assert {item["node_type"] for item in version_map["nodes"]} == {"base", "direction", "company"}
        assert {(item["parent_version_id"], item["child_version_id"]) for item in version_map["edges"]} >= {
            ("rv_mvp_master_v1", "rv_mvp_backend_v1"), ("rv_mvp_backend_v1", "rv_mvp_company_v1")
        }

        analysis = client.post("/v1/workbench/match-analyses/evaluate", json={
            "analysis_id": "ma_mvp", "operation_id": "op_mvp_match", "idempotency_key": "mvp-match",
            "workspace_id": "ws_mvp", "resume_version_id": "rv_mvp_company_v1", "job_snapshot_id": snapshot_id,
        })
        assert analysis.status_code == 201, analysis.text
        assert analysis.json()["total_score"] is not None
        assert any(item["evidence"] for item in analysis.json()["requirements"] if item["verdict"] in {"matched", "partial"})
        assert all(not item["evidence"] for item in analysis.json()["requirements"] if item["verdict"] in {"missing", "conflict"})

        suggestion_draft = client.post("/v1/workbench/resume-versions/rv_mvp_company_v1/drafts", json={"draft_id": "rd_mvp_suggestions", "workspace_id": "ws_mvp", "branch_id": "rb_mvp_company"}).json()
        suggestions = client.post("/v1/workbench/match-analyses/ma_mvp/suggestion-candidates", json={"workspace_id": "ws_mvp", "draft_id": suggestion_draft["draft_id"]})
        assert suggestions.status_code == 201, suggestions.text
        assert suggestions.json()["items"]
        suggestion = suggestions.json()["items"][0]
        accepted = client.post(f"/v1/workbench/suggestions/{suggestion['suggestion_id']}/decisions", json={"decision": "accept", "workspace_id": "ws_mvp"})
        assert accepted.status_code == 200
        assert accepted.json()["revision"] == suggestion_draft["revision"] + 1
        assert client.get("/v1/workbench/resume-versions/rv_mvp_company_v1").json()["status"] == "confirmed"

        final_pending = client.post("/v1/workbench/drafts/rd_mvp_suggestions/versions", json={"workspace_id": "ws_mvp", "version_id": "rv_mvp_company_v2", "label": "Example Co tailored v2", "expected_draft_revision": accepted.json()["revision"]})
        assert final_pending.status_code == 201
        assert final_pending.json()["status"] == "pending_confirmation"
        final = client.post("/v1/workbench/resume-versions/rv_mvp_company_v2/confirm", json={"workspace_id": "ws_mvp", "expected_revision": final_pending.json()["revision"]})
        assert final.status_code == 200
        assert final.json()["status"] == "confirmed"

        diff = client.get("/v1/workbench/resume-versions/rv_mvp_company_v1/compare/rv_mvp_company_v2?workspace_id=ws_mvp")
        assert diff.status_code == 200
        assert diff.json()["common_ancestor_version_id"] == "rv_mvp_company_v1"
        assert client.get("/v1/workbench/workspaces/ws_mvp/home").json()["stats"] == {
            "resume_count": 1, "job_count": 1, "todo_count": 0,
            "active_operation_count": 0, "application_count": 0,
        }
    finally:
        runtime.close()
