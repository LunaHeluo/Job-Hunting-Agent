from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from starter_agent.delegation.models import (
    BudgetAllocation,
    BudgetLimits,
    BudgetUsage,
    ChildRun,
    ChildTask,
    DelegationModelError,
    MergeReport,
    ParentRun,
    ResultEnvelope,
    RunOutcome,
    RunSpec,
    TaskContract,
    ensure_idempotency_compatible,
    transition_run,
    validate_child_result_acceptance,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)


def _limits(**changes: int) -> BudgetLimits:
    values = {
        "tokens": 20_000,
        "cost_microunits": 500_000,
        "wall_clock_ms": 120_000,
        "model_calls": 8,
        "tool_calls": 20,
    }
    values.update(changes)
    return BudgetLimits(**values)


def _contract(**changes: object) -> TaskContract:
    values: dict[str, object] = {
        "task_id": "task:web:001",
        "parent_run_id": "parent:001",
        "specialist_id": "job_web_researcher",
        "goal": "调研悉尼 Agent 工程师岗位",
        "inputs": {
            "query": "Agent engineer Sydney",
            "artifact_ids": ["artifact:query:001"],
        },
        "constraints": {"max_pages": 5, "target_fields": ["title", "company"]},
        "requested_allowed_tools": (
            "mcp__playwright__browser_snapshot",
            "search_jobs_serpapi",
        ),
        "requested_deadline": DEADLINE,
        "requested_budget": _limits(),
        "failure_behavior": "allow_partial",
        "idempotency_key": "delegate:parent:001:web:001",
    }
    values.update(changes)
    return TaskContract(**values)


def _child_run(**changes: object) -> ChildRun:
    values: dict[str, object] = {
        "id": "child-run:001",
        "child_task_id": "task:web:001",
        "parent_run_id": "parent:001",
        "attempt": 1,
        "status": "running",
        "phase": "model_loop",
        "version": 3,
        "deadline_at": DEADLINE,
        "created_at": NOW,
        "started_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ChildRun(**values)


def _parent_run(**changes: object) -> ParentRun:
    values: dict[str, object] = {
        "id": "parent:001",
        "session_id": "session:001",
        "origin_turn_id": "turn:001",
        "principal": "user:001",
        "coordinator_spec_version": "coordinator-v1",
        "runtime_revision": "runtime-v1",
        "status": "running",
        "phase": "delegating",
        "version": 1,
        "priority": 100,
        "available_at": NOW,
        "deadline_at": DEADLINE,
        "budget_total": _limits(),
        "budget_reserved": _limits(
            tokens=5_000,
            cost_microunits=100_000,
            wall_clock_ms=30_000,
            model_calls=2,
            tool_calls=5,
        ),
        "budget_consumed": _limits(
            tokens=1_000,
            cost_microunits=10_000,
            wall_clock_ms=5_000,
            model_calls=1,
            tool_calls=1,
        ),
        "route": "delegation",
        "legacy_path_used": False,
        "created_at": NOW,
        "started_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ParentRun(**values)


def _envelope(**changes: object) -> ResultEnvelope:
    values: dict[str, object] = {
        "status": "succeeded",
        "output": {"jobs": [{"title": "Agent Engineer", "source_url": "https://example.test/jobs/1"}]},
        "evidence": ({"source_url": "https://example.test/jobs/1", "field": "title"},),
        "missing": (),
        "conflicts": (),
        "usage": BudgetUsage(
            tokens=1_200,
            cost_microunits=20_000,
            wall_clock_ms=4_000,
            model_calls=2,
            tool_calls=3,
            estimated=False,
            cost_status="actual",
            price_version="prices-2026-08",
            usage_source="provider",
        ),
        "child_run_id": "child-run:001",
        "task_id": "task:web:001",
        "trace_ref": "trace:child-run:001",
        "idempotency_key": "result:child-run:001:attempt:1",
    }
    values.update(changes)
    return ResultEnvelope(**values)


def test_task_contract_is_frozen_rejects_extra_fields_and_has_stable_hash() -> None:
    first = _contract(
        inputs={"query": "Agent engineer Sydney", "artifact_ids": ["artifact:query:001"]}
    )
    reordered = _contract(
        inputs={"artifact_ids": ["artifact:query:001"], "query": "Agent engineer Sydney"}
    )

    assert first.canonical_hash == reordered.canonical_hash
    assert len(first.canonical_hash) == 64
    assert first.model_json_schema()["additionalProperties"] is False

    with pytest.raises(ValidationError):
        first.goal = "被篡改"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TaskContract(**{**first.model_dump(), "system_prompt": "越权覆盖"})


def test_same_idempotency_key_with_different_payload_is_a_stable_conflict() -> None:
    first = _contract(goal="调研岗位 A")
    conflicting = _contract(goal="调研岗位 B")

    with pytest.raises(DelegationModelError) as captured:
        ensure_idempotency_compatible(first, conflicting)

    assert captured.value.code == "idempotency_payload_conflict"


def test_budget_allocation_enforces_hard_accounting_invariants() -> None:
    allocation = BudgetAllocation(
        dimension="tokens",
        limit=20_000,
        requested=8_000,
        reserved=8_000,
        consumed=5_000,
        released=3_000,
        estimated=False,
        usage_source="provider",
        version=2,
    )
    assert allocation.consumed + allocation.released == allocation.reserved

    with pytest.raises(ValidationError, match="requested cannot exceed limit"):
        BudgetAllocation(
            dimension="tokens",
            limit=1_000,
            requested=1_001,
            reserved=0,
            consumed=0,
            released=0,
        )
    with pytest.raises(ValidationError, match="consumed and released cannot exceed reserved"):
        BudgetAllocation(
            dimension="tokens",
            limit=1_000,
            requested=900,
            reserved=900,
            consumed=700,
            released=300,
        )
    with pytest.raises(ValidationError):
        BudgetAllocation(**{**allocation.model_dump(), "version": 2**63})


def test_transition_requires_expected_version_and_terminal_state_is_immutable() -> None:
    running = _child_run()

    succeeded = transition_run(
        running,
        "succeeded",
        expected_version=3,
        occurred_at=NOW + timedelta(seconds=5),
    )
    assert succeeded.status == "succeeded"
    assert succeeded.version == 4
    assert succeeded.completed_at == NOW + timedelta(seconds=5)

    with pytest.raises(DelegationModelError) as stale:
        transition_run(running, "failed", expected_version=2, occurred_at=NOW)
    assert stale.value.code == "run_version_conflict"

    with pytest.raises(DelegationModelError) as terminal:
        transition_run(succeeded, "failed", expected_version=4, occurred_at=NOW)
    assert terminal.value.code == "terminal_state_immutable"


def test_transition_records_child_cancellation_and_rejects_naive_event_time() -> None:
    running = _child_run()
    cancelling = transition_run(
        running,
        "cancelling",
        expected_version=3,
        occurred_at=NOW + timedelta(seconds=1),
    )
    cancelled = transition_run(
        cancelling,
        "cancelled",
        expected_version=4,
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert cancelled.cancelled_at == NOW + timedelta(seconds=2)
    assert cancelled.completed_at == NOW + timedelta(seconds=2)

    with pytest.raises(ValidationError, match="UTC offset"):
        transition_run(
            running,
            "failed",
            expected_version=3,
            occurred_at=datetime(2026, 8, 10, 12, 0),
        )


def test_first_running_transition_records_start_once_and_resume_preserves_it() -> None:
    queued = _child_run(
        status="queued",
        phase="queued",
        version=1,
        started_at=None,
    )
    running = transition_run(
        queued,
        "running",
        expected_version=1,
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert running.started_at == NOW + timedelta(seconds=1)

    waiting = transition_run(
        running,
        "waiting_for_user",
        expected_version=2,
        occurred_at=NOW + timedelta(seconds=2),
    )
    requeued = transition_run(
        waiting,
        "queued",
        expected_version=3,
        occurred_at=NOW + timedelta(seconds=3),
    )
    resumed = transition_run(
        requeued,
        "running",
        expected_version=4,
        occurred_at=NOW + timedelta(seconds=4),
    )
    assert resumed.started_at == NOW + timedelta(seconds=1)


def test_parent_only_transition_and_invalid_child_transition_are_rejected() -> None:
    child = _child_run()
    with pytest.raises(DelegationModelError) as captured:
        transition_run(child, "waiting_children", expected_version=3, occurred_at=NOW)
    assert captured.value.code == "invalid_run_status_transition"

    parent = _parent_run()
    waiting = transition_run(parent, "waiting_children", expected_version=1, occurred_at=NOW)
    assert waiting.status == "waiting_children"


def test_late_or_mismatched_child_result_is_rejected_without_mutating_run() -> None:
    cancelled = _child_run(
        status="cancelled",
        cancelled_at=NOW,
        completed_at=NOW,
        version=4,
    )

    with pytest.raises(DelegationModelError) as late:
        validate_child_result_acceptance(cancelled, _envelope(), expected_version=4)
    assert late.value.code == "late_child_result_rejected"

    with pytest.raises(DelegationModelError) as mismatched:
        validate_child_result_acceptance(
            _child_run(),
            _envelope(child_run_id="child-run:other"),
            expected_version=3,
        )
    assert mismatched.value.code == "child_result_identity_mismatch"


@pytest.mark.parametrize("status", ["created", "queued", "waiting_for_user"])
def test_child_result_is_only_accepted_while_attempt_is_running(status: str) -> None:
    run = _child_run(status=status)
    with pytest.raises(DelegationModelError) as captured:
        validate_child_result_acceptance(run, _envelope(), expected_version=3)
    assert captured.value.code == "child_result_not_acceptable"


def test_matching_running_child_result_is_accepted() -> None:
    assert validate_child_result_acceptance(
        _child_run(),
        _envelope(),
        expected_version=3,
    ) is None


def test_run_models_reject_incomplete_terminal_and_out_of_order_timestamps() -> None:
    with pytest.raises(ValidationError, match="started_at"):
        _child_run(status="running", started_at=None)
    with pytest.raises(ValidationError, match="terminal runs require completed_at"):
        _child_run(status="succeeded", completed_at=None)
    with pytest.raises(ValidationError, match="cancelled runs require cancelled_at"):
        _child_run(status="cancelled", completed_at=NOW, cancelled_at=None)
    with pytest.raises(ValidationError, match="updated_at cannot precede created_at"):
        _child_run(updated_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="completed_at cannot precede started_at"):
        _parent_run(
            status="failed",
            completed_at=NOW - timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="completed_at cannot follow updated_at"):
        _child_run(
            status="failed",
            completed_at=NOW + timedelta(seconds=2),
            updated_at=NOW + timedelta(seconds=1),
        )
    with pytest.raises(ValidationError, match="available_at cannot follow deadline_at"):
        _parent_run(available_at=DEADLINE + timedelta(seconds=1))


def test_child_lease_fields_must_form_a_complete_running_lease() -> None:
    with pytest.raises(ValidationError, match="lease fields"):
        _child_run(lease_owner="worker:a")
    with pytest.raises(ValidationError, match="running child lease"):
        _child_run(
            status="queued",
            lease_owner="worker:a",
            lease_token="lease:a",
            lease_expires_at=DEADLINE,
            heartbeat_at=NOW,
        )
    with pytest.raises(ValidationError, match="lease expiry"):
        _child_run(
            lease_owner="worker:a",
            lease_token="lease:a",
            lease_expires_at=NOW - timedelta(seconds=1),
            heartbeat_at=NOW,
        )


@pytest.mark.parametrize(
    ("reserved_change", "consumed_change"),
    [
        ({"tokens": 20_001}, {}),
        ({}, {"tool_calls": 6}),
    ],
)
def test_parent_budget_summary_rejects_reserved_or_consumed_overflow(
    reserved_change: dict[str, int],
    consumed_change: dict[str, int],
) -> None:
    reserved = {
        "tokens": 5_000,
        "cost_microunits": 100_000,
        "wall_clock_ms": 30_000,
        "model_calls": 2,
        "tool_calls": 5,
        **reserved_change,
    }
    consumed = {
        "tokens": 1_000,
        "cost_microunits": 10_000,
        "wall_clock_ms": 5_000,
        "model_calls": 1,
        "tool_calls": 1,
        **consumed_change,
    }
    with pytest.raises(ValidationError, match="consumed <= reserved <= total"):
        _parent_run(
            budget_reserved=BudgetLimits(**reserved),
            budget_consumed=BudgetLimits(**consumed),
        )


def test_sqlite_backed_integer_fields_reject_values_above_signed_64_bit() -> None:
    with pytest.raises(ValidationError):
        _limits(tokens=2**63)
    with pytest.raises(ValidationError):
        _child_run(version=2**63)
    with pytest.raises(ValidationError):
        _child_run(attempt=2**63)
    with pytest.raises(ValidationError):
        BudgetAllocation(
            dimension="tokens",
            limit=1,
            requested=1,
            reserved=1,
            consumed=0,
            released=0,
            version=2**63,
        )


@pytest.mark.parametrize(
    ("run_kind", "role"),
    [("parent", "specialist"), ("child", "coordinator")],
)
def test_run_spec_rejects_role_that_does_not_match_run_kind(
    run_kind: str,
    role: str,
) -> None:
    with pytest.raises(ValidationError, match="run_kind and role"):
        RunSpec(
            run_id="run:001",
            run_kind=run_kind,
            role=role,
            provider="provider",
            model="model",
            system_prompt_ref="prompt:v1",
            output_schema_ref="schema:v1",
            max_steps=4,
            runtime_revision="runtime-v1",
        )


@pytest.mark.parametrize(
    ("disposition", "status"),
    [
        ("completed", "running"),
        ("suspended", "succeeded"),
        ("failed", "waiting_children"),
        ("cancelled", "cancelling"),
    ],
)
def test_run_outcome_rejects_disposition_status_mismatch(
    disposition: str,
    status: str,
) -> None:
    with pytest.raises(ValidationError, match="disposition does not match status"):
        RunOutcome(
            disposition=disposition,
            run_id="run:001",
            status=status,
        )


def test_result_envelope_hash_is_stable_and_schema_forbids_extra_fields() -> None:
    first = _envelope(output={"jobs": [], "errors": [{"code": "blocked"}]})
    reordered = _envelope(output={"errors": [{"code": "blocked"}], "jobs": []})

    assert first.canonical_hash == reordered.canonical_hash
    assert first.model_json_schema()["additionalProperties"] is False
    assert set(first.model_json_schema()["required"]) >= {
        "status",
        "output",
        "evidence",
        "missing",
        "conflicts",
        "usage",
        "child_run_id",
        "task_id",
        "trace_ref",
        "idempotency_key",
    }


def test_result_envelope_idempotency_rejects_a_different_callback_payload() -> None:
    first = _envelope(output={"jobs": []})
    duplicate = _envelope(output={"jobs": []})
    conflict = _envelope(output={"jobs": [{"title": "different"}]})

    assert ensure_idempotency_compatible(first, duplicate) is first
    with pytest.raises(DelegationModelError) as captured:
        ensure_idempotency_compatible(first, conflict)
    assert captured.value.code == "idempotency_payload_conflict"


def test_domain_models_express_child_task_run_spec_outcome_and_merge_report() -> None:
    contract = _contract()
    task = ChildTask.from_contract(
        contract,
        specialist_snapshot_id="specialist-snapshot:001",
        output_schema_version="job-web-output-v1",
        created_at=NOW,
    )
    assert task.contract_hash == contract.canonical_hash
    assert task.status == "created"

    spec = RunSpec(
        run_id="child-run:001",
        run_kind="child",
        role="specialist",
        provider="openai-compatible",
        model="test-model",
        system_prompt_ref="prompt:job-web-researcher:v1",
        output_schema_ref="schema:job-web-output:v1",
        allowed_tools=("search_jobs_serpapi",),
        max_steps=12,
        runtime_revision="runtime-v1",
    )
    assert spec.allowed_tools == ("search_jobs_serpapi",)

    outcome = RunOutcome(
        disposition="suspended",
        run_id="parent:001",
        status="waiting_children",
        checkpoint_ref="artifact:checkpoint:001",
    )
    assert outcome.disposition == "suspended"

    envelope = _envelope()
    report = MergeReport(
        id="merge:001",
        parent_run_id="parent:001",
        result_version=1,
        input_envelope_refs=("envelope:001",),
        input_hashes=(envelope.canonical_hash,),
        accepted=({"task_id": envelope.task_id, "child_run_id": envelope.child_run_id},),
        rejected=(),
        dedup_groups=(),
        missing=(),
        conflicts=(),
        source_validation=({"source_url": "https://example.test/jobs/1", "valid": True},),
        evidence_validation=({"task_id": envelope.task_id, "valid": True},),
        ranking_features={"quality": 1.0},
        deterministic_order=(envelope.task_id,),
        semantic_synthesis_version="disabled",
        final_output_ref="artifact:final:001",
        final_output_hash="0" * 64,
        created_at=NOW,
    )
    assert report.input_hashes == (envelope.canonical_hash,)


def test_field_size_and_timezone_boundaries_are_enforced() -> None:
    with pytest.raises(ValidationError):
        _contract(goal="x" * 4_001)
    with pytest.raises(ValidationError):
        _contract(requested_deadline=datetime(2026, 8, 10, 12, 5))
    with pytest.raises(ValidationError):
        _contract(requested_allowed_tools=tuple(f"tool:{index}" for index in range(65)))
