from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from starter_agent.cv_workbench.contracts import (
    BusinessOperation,
    CandidateResult,
    CONTRACT_VERSION,
    Application,
    ExportRecord,
    Job,
    JobCandidate,
    JobSnapshot,
    MatchAnalysis,
    MergeDecisionType,
    MergeProposal,
    MergeProposalStatus,
    OperationStatus,
    RequirementResult,
    Resume,
    ResumeBranch,
    ResumeDraft,
    ResumeDraftStatus,
    ResumeNodeType,
    ResumeVersion,
    Suggestion,
    VersionMap,
    VersionMapEdge,
    VersionViewPreference,
    WorkbenchErrorEnvelope,
    WorkbenchHome,
    WorkbenchContext,
    Workspace,
    WorkspaceStatus,
    assert_transition,
    transition_allowed,
)


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("fixture_name", "model_type"),
    [
        ("empty-home.json", WorkbenchHome),
        ("version-map.json", VersionMap),
        ("merge-conflict.json", MergeProposal),
        ("analysis-validated.json", MatchAnalysis),
        ("analysis-partial.json", MatchAnalysis),
        ("analysis-stale.json", MatchAnalysis),
        ("operation-commit-failed.json", BusinessOperation),
        ("error-revision-conflict.json", WorkbenchErrorEnvelope),
    ],
)
def test_fixed_fixtures_validate_and_round_trip(fixture_name, model_type) -> None:
    parsed = model_type.model_validate(load_fixture(fixture_name))

    assert parsed.contract_version == CONTRACT_VERSION
    assert model_type.model_validate_json(parsed.model_dump_json()) == parsed


def test_contracts_reject_unknown_fields() -> None:
    payload = load_fixture("empty-home.json")
    payload["frontend_only_success"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorkbenchHome.model_validate(payload)


@pytest.mark.parametrize(
    ("object_name", "model_type"),
    [
        ("workspace", Workspace),
        ("resume", Resume),
        ("branch", ResumeBranch),
        ("version", ResumeVersion),
        ("draft", ResumeDraft),
        ("job_candidate", JobCandidate),
        ("job", Job),
        ("job_snapshot", JobSnapshot),
        ("suggestion", Suggestion),
        ("application", Application),
        ("export", ExportRecord),
        ("context", WorkbenchContext),
    ],
)
def test_business_object_fixtures_validate_and_round_trip(
    object_name, model_type
) -> None:
    payload = load_fixture("business-objects.json")[object_name]
    parsed = model_type.model_validate(payload)

    assert parsed.contract_version == CONTRACT_VERSION
    assert model_type.model_validate_json(parsed.model_dump_json()) == parsed


def test_positive_requirement_verdict_requires_authorized_evidence() -> None:
    requirement = load_fixture("analysis-validated.json")["requirements"][0]
    requirement["evidence"] = []

    with pytest.raises(
        ValidationError, match="positive_requirement_verdict_requires_evidence"
    ):
        RequirementResult.model_validate(requirement)


def test_missing_requirement_must_not_invent_evidence() -> None:
    requirement = load_fixture("analysis-validated.json")["requirements"][1]
    parsed = RequirementResult.model_validate(requirement)

    assert parsed.verdict.value == "missing"
    assert parsed.evidence == ()


def test_candidate_result_cannot_claim_a_business_object() -> None:
    payload = {
        "contract_version": CONTRACT_VERSION,
        "operation_id": "op_candidate_demo",
        "candidate_kind": "match_analysis",
        "result_sha256": "a" * 64,
        "validator_version": "validator.v1",
        "validated": True,
        "business_object_id": "ma_should_not_exist",
    }

    with pytest.raises(ValidationError):
        CandidateResult.model_validate(payload)


def test_analysis_cannot_expose_score_before_validation() -> None:
    payload = load_fixture("analysis-validated.json")
    payload["status"] = "running"

    with pytest.raises(ValidationError, match="incomplete_analysis_cannot_expose_total_score"):
        MatchAnalysis.model_validate(payload)


def test_stale_analysis_requires_reason() -> None:
    payload = load_fixture("analysis-stale.json")
    payload["stale_reason"] = None

    with pytest.raises(ValidationError, match="stale_analysis_requires_reason"):
        MatchAnalysis.model_validate(payload)


def test_stale_analysis_preserves_historical_score_and_evidence() -> None:
    analysis = MatchAnalysis.model_validate(load_fixture("analysis-stale.json"))

    assert analysis.total_score == 72.5
    assert analysis.requirements[0].evidence
    assert "generate_suggestions" not in analysis.allowed_actions


def version_payload(*, node_type: str, parent: str | None) -> dict[str, object]:
    return {
        "contract_version": CONTRACT_VERSION,
        "version_id": "rv_demo_v1",
        "resume_id": "res_demo",
        "branch_id": "rb_demo",
        "parent_version_id": parent,
        "branch_base_version_id": "rv_demo_v1",
        "node_type": node_type,
        "version_number": 1,
        "label": "Demo v1",
        "content": {
            "content_sha256": "a" * 64,
            "knowledge_base_id": "kb-demo",
            "document_id": "doc-demo",
            "document_version_id": "docv-demo",
            "artifact_id": None,
        },
        "status": "confirmed",
        "job_snapshot_id": None,
        "upstream_changes_available": False,
        "revision": 1,
        "created_by": "local-user",
        "created_at": "2026-08-17T09:00:00+08:00",
        "confirmed_at": "2026-08-17T09:01:00+08:00",
        "allowed_actions": ["compare"],
    }


def test_base_version_is_the_only_parentless_node() -> None:
    base = ResumeVersion.model_validate(version_payload(node_type="base", parent=None))
    assert base.node_type == ResumeNodeType.BASE

    with pytest.raises(ValidationError, match="non_base_version_requires_parent"):
        ResumeVersion.model_validate(version_payload(node_type="direction", parent=None))


def test_base_version_cannot_claim_a_parent() -> None:
    with pytest.raises(ValidationError, match="base_version_cannot_have_parent"):
        ResumeVersion.model_validate(
            version_payload(node_type="base", parent="rv_other")
        )


def test_version_map_edges_must_come_from_known_backend_nodes() -> None:
    payload = load_fixture("version-map.json")
    payload["edges"].append(
        {"parent_version_id": "rv_missing", "child_version_id": "rv_backend_v1"}
    )

    with pytest.raises(ValidationError, match="version_map_edge_references_missing_node"):
        VersionMap.model_validate(payload)


def test_version_map_rejects_self_edges() -> None:
    with pytest.raises(ValidationError, match="version_map_self_edge_is_invalid"):
        VersionMapEdge(
            parent_version_id="rv_same",
            child_version_id="rv_same",
        )


def test_view_preferences_carry_no_lineage_mutation_fields() -> None:
    preference = VersionViewPreference.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "owner_id": "local-user",
            "resume_id": "res_demo",
            "node_positions": {"rv_master_v1": [10.0, 20.0]},
            "collapsed_branch_ids": ["rb_backend"],
            "viewport_x": 2.0,
            "viewport_y": 3.0,
            "viewport_zoom": 1.2,
            "revision": 1,
            "updated_at": "2026-08-17T10:00:00+08:00",
        }
    )

    serialized = preference.model_dump()
    assert "parent_version_id" not in serialized
    assert "content_sha256" not in serialized


def test_conflicted_merge_requires_an_unresolved_item() -> None:
    proposal = MergeProposal.model_validate(load_fixture("merge-conflict.json"))
    assert proposal.status == MergeProposalStatus.CONFLICTED
    assert any(
        item.decision == MergeDecisionType.UNRESOLVED for item in proposal.decisions
    )

    payload = proposal.model_dump(mode="json")
    payload["status"] = "ready"
    with pytest.raises(ValidationError, match="ready_merge_proposal_cannot_have_unresolved_items"):
        MergeProposal.model_validate(payload)


def test_committed_merge_requires_operation_and_result_version() -> None:
    payload = load_fixture("merge-conflict.json")
    payload["decisions"][1] = {
        **payload["decisions"][1],
        "decision": "keep_target",
        "result_sha256": payload["decisions"][1]["target_sha256"],
        "decided_by": "local-user",
        "decided_at": "2026-08-17T10:05:00+08:00",
    }
    payload["status"] = "committed"

    with pytest.raises(ValidationError, match="committed_merge_proposal_requires_result"):
        MergeProposal.model_validate(payload)

    payload["operation_id"] = "op_merge_demo"
    payload["result_version_id"] = "rv_backend_v2"
    committed = MergeProposal.model_validate(payload)
    assert committed.result_version_id == "rv_backend_v2"


def test_committed_operation_is_not_inferred_from_run_success() -> None:
    payload = load_fixture("operation-commit-failed.json")
    payload["status"] = OperationStatus.COMMITTED
    payload["error_code"] = None
    payload["result_object_id"] = None

    with pytest.raises(ValidationError, match="committed_operation_requires_clean_result"):
        BusinessOperation.model_validate(payload)

    payload["result_object_id"] = "ma_demo_validated"
    operation = BusinessOperation.model_validate(payload)
    assert operation.status == OperationStatus.COMMITTED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkspaceStatus.ACTIVE, WorkspaceStatus.PAUSED),
        (ResumeDraftStatus.ACTIVE, ResumeDraftStatus.SAVED),
        (MergeProposalStatus.CONFLICTED, MergeProposalStatus.READY),
        (OperationStatus.COMMIT_FAILED, OperationStatus.COMMITTING),
    ],
)
def test_legal_transitions_are_explicit(current, target) -> None:
    assert transition_allowed(current, target)
    assert_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (WorkspaceStatus.ARCHIVED, WorkspaceStatus.ACTIVE),
        (ResumeDraftStatus.SAVED, ResumeDraftStatus.ACTIVE),
        (MergeProposalStatus.COMMITTED, MergeProposalStatus.DRAFT),
        (OperationStatus.COMMITTED, OperationStatus.RUNNING),
        (WorkspaceStatus.ACTIVE, OperationStatus.RUNNING),
    ],
)
def test_illegal_transitions_are_rejected(current, target) -> None:
    assert not transition_allowed(current, target)
    with pytest.raises(ValueError, match="illegal_transition"):
        assert_transition(current, target)


def test_revision_conflict_fixture_has_stable_recovery_contract() -> None:
    envelope = WorkbenchErrorEnvelope.model_validate(
        load_fixture("error-revision-conflict.json")
    )

    assert envelope.error.code == "revision_conflict"
    assert envelope.error.authoritative_revision == 7
    assert envelope.error.recovery_action == "compare_draft"
