from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from starter_agent.cv_workbench.contracts import Job, JobCandidate, Resume, ResumeVersion
from starter_agent.cv_workbench.migration import LegacyMigrationService
from starter_agent.cv_workbench.runtime import create_workbench_runtime
from starter_agent.cv_workbench.workspaces import CreateWorkspaceCommand, WorkspaceProfile


PRINCIPAL = "local-user"


def setup(tmp_path: Path):
    runtime = create_workbench_runtime(
        f"sqlite:///{(tmp_path / 'migration.db').as_posix()}", tmp_path
    )
    runtime.workspaces.create(
        CreateWorkspaceCommand("ws_migration", WorkspaceProfile(name="Migration")),
        principal=PRINCIPAL,
    )
    registry = tmp_path / "migration-registry.json"
    return runtime, LegacyMigrationService(runtime, registry_path=registry), registry


def test_dry_run_is_read_only_and_commit_is_resumable_idempotent_and_reversible(tmp_path: Path) -> None:
    runtime, service, registry = setup(tmp_path)
    legacy = tmp_path / "legacy"
    versions = legacy / "versions"
    versions.mkdir(parents=True)
    root_text = "# 张三\n\nPython API\n"
    child_text = "# 张三\n\nPython API\n\n- 延迟降低 30%\n"
    (legacy / "base.md").write_text(root_text, encoding="utf-8")
    (versions / "v1.md").write_text(root_text, encoding="utf-8")
    (versions / "v2.md").write_text(child_text, encoding="utf-8")
    import hashlib
    manifest = [
        {"version_id": "old-v1", "parent_id": None, "source_path": "base.md", "version_path": "versions/v1.md", "sha256": hashlib.sha256((versions / "v1.md").read_bytes()).hexdigest(), "label": "root"},
        {"version_id": "old-v2", "parent_id": "old-v1", "source_path": "base.md", "version_path": "versions/v2.md", "sha256": hashlib.sha256((versions / "v2.md").read_bytes()).hexdigest(), "label": "child"},
        {"version_id": "orphan", "parent_id": "missing", "source_path": "base.md", "version_path": "versions/v2.md", "sha256": hashlib.sha256((versions / "v2.md").read_bytes()).hexdigest(), "label": "orphan"},
    ]
    (legacy / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = {path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()}

    plan = service.scan(resume_root=legacy, include_research_candidates=False)
    assert not registry.exists()
    assert {path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()} == before
    assert plan.counts == {"ready": 2, "manual_review": 1}
    orphan = next(item for item in plan.candidates if item.old_version_id == "orphan")
    assert orphan.reason == "parent_version_evidence_missing"

    committed = service.commit(plan, batch_id="batch-001", workspace_id="ws_migration", principal=PRINCIPAL)
    assert committed["status"] == "committed"
    assert len(runtime.store.list(Resume, principal=PRINCIPAL).items) == 1
    assert len(runtime.store.list(ResumeVersion, principal=PRINCIPAL).items) == 2
    assert service.validate("batch-001", principal=PRINCIPAL)["valid"] is True

    repeated = service.commit(plan, batch_id="batch-001", workspace_id="ws_migration", principal=PRINCIPAL)
    assert repeated == committed
    assert len(runtime.store.list(ResumeVersion, principal=PRINCIPAL).items) == 2

    # Simulate a crash after the child object commit but before its registry checkpoint.
    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    child_key = next(item.source_key for item in plan.candidates if item.old_version_id == "old-v2")
    registry_payload["batches"]["batch-001"]["status"] = "committing"
    registry_payload["batches"]["batch-001"]["items"] = [
        item for item in registry_payload["batches"]["batch-001"]["items"]
        if item["source_key"] != child_key
    ]
    registry_payload["sources"].pop(child_key)
    registry.write_text(json.dumps(registry_payload), encoding="utf-8")
    resumed = service.commit(plan, batch_id="batch-001", workspace_id="ws_migration", principal=PRINCIPAL)
    assert resumed["status"] == "committed"
    assert any(item["source_key"] == child_key for item in resumed["items"])
    assert len(runtime.store.list(ResumeVersion, principal=PRINCIPAL).items) == 2

    rolled_back = service.rollback("batch-001", principal=PRINCIPAL)
    assert rolled_back["status"] == "rolled_back"
    assert runtime.store.list(Resume, principal=PRINCIPAL).items == ()
    assert runtime.store.list(ResumeVersion, principal=PRINCIPAL).items == ()
    assert {path.relative_to(legacy): path.read_bytes() for path in legacy.rglob("*") if path.is_file()} == before
    runtime.close()


def test_research_rows_only_become_candidates_and_knowledge_is_not_guessed(tmp_path: Path) -> None:
    runtime, service, _registry = setup(tmp_path)
    session_id = runtime.artifacts.create_session()
    runtime.artifacts.replace_pending_job_candidates(
        session_id=session_id,
        turn_id=uuid4(),
        candidates=[{
            "title": "Backend Engineer", "company": "Example", "location": "Shanghai",
            "source_url": "https://example.com/jobs/1", "evidence_level": "partial",
        }],
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    plan = service.scan(include_research_candidates=True)
    assert [item.source_kind for item in plan.candidates] == ["research_candidate"]
    service.commit(plan, batch_id="batch-research", workspace_id="ws_migration", principal=PRINCIPAL)
    candidates = runtime.store.list(JobCandidate, principal=PRINCIPAL).items
    assert len(candidates) == 1
    assert candidates[0].candidate_only is True
    assert candidates[0].verified is False
    assert runtime.store.list(Job, principal=PRINCIPAL).items == ()
    runtime.close()


def test_rollback_refuses_to_delete_a_migrated_version_with_downstream_reference(tmp_path: Path) -> None:
    from starter_agent.cv_workbench.contracts import ExportRecord, ExportStatus

    runtime, service, _registry = setup(tmp_path)
    legacy = tmp_path / "legacy-blocked"; legacy.mkdir()
    path = legacy / "resume.md"; path.write_text("# Resume\n\nPython\n", encoding="utf-8")
    plan = service.scan(resume_root=legacy, include_research_candidates=False)
    batch = service.commit(plan, batch_id="batch-blocked", workspace_id="ws_migration", principal=PRINCIPAL)
    version_id = next(target for item in batch["items"] for target in item.get("target_ids", []) if target.startswith("rv_"))
    runtime.store.create(ExportRecord(
        export_id="exp_migration_reference", resume_version_id=version_id, format="pdf",
        template_id="test", template_version="1", settings_sha256="a" * 64,
        status=ExportStatus.AVAILABLE, artifact_id="artifact:test", content_sha256="b" * 64,
        revision=1, created_at=datetime.now(UTC), available_at=datetime.now(UTC),
    ), principal=PRINCIPAL, workspace_id="ws_migration")
    result = service.rollback("batch-blocked", principal=PRINCIPAL)
    assert result["status"] == "rollback_partial"
    assert version_id in result["blocked_target_ids"]
    assert runtime.store.get(ResumeVersion, version_id, principal=PRINCIPAL).version_id == version_id
    runtime.close()
