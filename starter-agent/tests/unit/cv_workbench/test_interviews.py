from datetime import datetime

import pytest

from starter_agent.cv_workbench.contracts import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Job,
    JobSnapshot,
    JobUserStatus,
    Resume,
    ResumeBranch,
    ResumeBranchType,
    ResumeNodeType,
    ResumeStatus,
    ResumeVersion,
    ResumeVersionStatus,
    Workspace,
    WorkspaceStatus,
)
from starter_agent.cv_workbench.interviews import (
    InterviewConfirmationRequiredError,
    InterviewReviewService,
    RoundCommand,
)
from starter_agent.cv_workbench.analytics import ApplicationAnalyticsService
from starter_agent.cv_workbench.exports import AtsCleanRenderer, AtsCompactRenderer
from starter_agent.cv_workbench.store import ObjectNotFoundError, SQLiteWorkbenchStore


NOW = datetime.fromisoformat("2026-08-17T10:00:00+08:00")
PRINCIPAL = "local-user"


def setup_service(tmp_path):
    store = SQLiteWorkbenchStore(f"sqlite:///{(tmp_path / 'interviews.db').as_posix()}", tmp_path)
    store.create(Workspace(workspace_id="ws_demo", owner_id=PRINCIPAL, name="Demo", status=WorkspaceStatus.ACTIVE, revision=1, created_at=NOW, updated_at=NOW), principal=PRINCIPAL)
    store.create(Resume(resume_id="res_demo", owner_id=PRINCIPAL, name="Resume", status=ResumeStatus.ACTIVE, revision=1, created_at=NOW, updated_at=NOW), principal=PRINCIPAL)
    store.create(ResumeBranch(branch_id="rb_master", resume_id="res_demo", name="Master", branch_type=ResumeBranchType.MASTER, base_version_id="rv_master", revision=1, created_at=NOW, updated_at=NOW), principal=PRINCIPAL)
    store.create(ResumeVersion(version_id="rv_master", resume_id="res_demo", branch_id="rb_master", branch_base_version_id="rv_master", node_type=ResumeNodeType.BASE, version_number=1, label="Master", content={"content_sha256": "a" * 64, "knowledge_base_id": "kb", "document_id": "doc", "document_version_id": "docv"}, status=ResumeVersionStatus.CONFIRMED, revision=1, created_by=PRINCIPAL, created_at=NOW, confirmed_at=NOW), principal=PRINCIPAL)
    store.create(Job(job_id="job_demo", owner_id=PRINCIPAL, title="Engineer", company="Acme", user_status=JobUserStatus.SAVED, revision=1, created_at=NOW, updated_at=NOW), principal=PRINCIPAL, workspace_id="ws_demo")
    store.create(JobSnapshot(snapshot_id="js_demo", job_id="job_demo", title="Engineer", company="Acme", content={"content_sha256": "b" * 64, "knowledge_base_id": "kb", "document_id": "jobdoc", "document_version_id": "jobdocv"}, verified=True, captured_at=NOW, verified_at=NOW), principal=PRINCIPAL, workspace_id="ws_demo")
    store.create(Application(application_id="app_demo", workspace_id="ws_demo", job_snapshot_id="js_demo", resume_version_id="rv_master", current_status=ApplicationStatus.INTERVIEW, events=(ApplicationEvent(event_id="ae_demo", from_status=None, to_status=ApplicationStatus.INTERVIEW, confirmed_by=PRINCIPAL, occurred_at=NOW),), revision=1, created_at=NOW, updated_at=NOW), principal=PRINCIPAL, workspace_id="ws_demo")
    return store, InterviewReviewService(store=store, clock=lambda: NOW)


def test_review_round_summary_candidate_and_user_decision(tmp_path) -> None:
    _store, service = setup_service(tmp_path)
    review = service.create("ir_demo", "app_demo", principal=PRINCIPAL)
    command = RoundCommand(expected_revision=1, round_id="round_technical", round_type="技术一面", occurred_at=NOW, questions=("如何设计缓存？",), feedback=("需要补充一致性策略",), result="进入下一轮", improvement_items=("复习缓存一致性",), user_confirmed=True)
    review = service.add_round(review.review_id, command, principal=PRINCIPAL)
    replay = service.add_round(review.review_id, command, principal=PRINCIPAL)
    assert replay == review

    review = service.propose_summary(review.review_id, "is_demo", expected_revision=2, principal=PRINCIPAL)
    candidate = review.summary_candidates[0]
    assert candidate.status == "pending"
    assert candidate.cited_round_ids == ("round_technical",)
    assert "需要补充一致性策略" in candidate.text
    assert "音频" not in candidate.text

    accepted = service.decide_summary(review.review_id, "is_demo", expected_revision=3, decision="accepted", principal=PRINCIPAL)
    assert accepted.summary_candidates[0].status == "accepted"


def test_confirmation_is_required_and_delete_is_private(tmp_path) -> None:
    _store, service = setup_service(tmp_path)
    review = service.create("ir_demo", "app_demo", principal=PRINCIPAL)
    with pytest.raises(InterviewConfirmationRequiredError):
        service.add_round(review.review_id, RoundCommand(expected_revision=1, round_id="round_one", round_type="一面", occurred_at=NOW), principal=PRINCIPAL)
    with pytest.raises(ObjectNotFoundError):
        service.for_application("app_demo", principal="other-user")
    service.delete(review.review_id, principal=PRINCIPAL)
    with pytest.raises(ObjectNotFoundError):
        service.for_application("app_demo", principal=PRINCIPAL)


def test_funnel_uses_events_and_reminder_is_a_read_only_projection(tmp_path) -> None:
    store, _service = setup_service(tmp_path)
    application = store.get(Application, "app_demo", principal=PRINCIPAL)
    reminded = Application.model_validate(application.model_dump() | {
        "next_action": "准备系统设计题",
        "remind_at": NOW,
        "revision": 2,
        "updated_at": NOW,
    })
    store.update(reminded, principal=PRINCIPAL, expected_revision=1)
    analytics = ApplicationAnalyticsService(store=store, clock=lambda: NOW)

    funnel = analytics.funnel("ws_demo", principal=PRINCIPAL)
    interview = next(item for item in funnel["stages"] if item["status"] == "interview")
    assert funnel["definition_version"] == "application-funnel.v1"
    assert interview == {"status": "interview", "reached": 1, "current": 1}
    assert funnel["event_count"] == 1

    reminders = analytics.reminders("ws_demo", principal=PRINCIPAL)
    assert reminders["items"][0]["status"] == "due"
    assert reminders["external_messages_sent"] == 0
    assert store.get(Application, "app_demo", principal=PRINCIPAL).current_status == ApplicationStatus.INTERVIEW


def test_export_templates_change_layout_without_changing_resume_content() -> None:
    markdown = "# 张三\n\n## 经历\n\n- 构建 API"
    clean = AtsCleanRenderer().render_docx(markdown, {"title": "简历"})
    compact = AtsCompactRenderer().render_docx(markdown, {"title": "简历"})
    assert clean.startswith(b"PK") and compact.startswith(b"PK")
    assert clean != compact
    assert AtsCleanRenderer.template_version == AtsCompactRenderer.template_version == "1.0.0"
