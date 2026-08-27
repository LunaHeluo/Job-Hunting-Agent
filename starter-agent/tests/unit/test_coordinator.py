from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from starter_agent.delegation.coordinator import Coordinator, CoordinatorPhase
from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, TaskContract
from starter_agent.delegation.store import SQLiteRunStore


NOW = datetime(2026, 8, 12, 9, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _limits(value: int) -> BudgetLimits:
    return BudgetLimits(tokens=value, cost_microunits=value, wall_clock_ms=value, model_calls=value, tool_calls=value)


def _parent() -> ParentRun:
    return ParentRun(
        id="parent:coordinator:1", session_id="session:1", origin_turn_id="turn:1",
        principal="user:1", coordinator_spec_version="1", runtime_revision="runtime:1",
        status="running", phase="planning", started_at=NOW, available_at=NOW,
        deadline_at=NOW + timedelta(hours=1), budget_total=_limits(1000),
        budget_reserved=_limits(0), budget_consumed=_limits(0), route="delegation",
        created_at=NOW, updated_at=NOW,
    )


def test_coordinator_tool_view_excludes_specialist_tools() -> None:
    coordinator = Coordinator(store=SQLiteRunStore("sqlite:///:memory:", ROOT), now=lambda: NOW)

    assert coordinator.tool_view == (
        "delegate_task",
        "inspect_delegated_results",
        "merge_delegated_results",
        "request_user_confirmation",
    )
    assert not {"search_jobs_serpapi", "mcp__playwright__browser_navigate", "retrieve_resume_evidence"} & set(coordinator.tool_view)
    with pytest.raises(ValueError, match="coordinator_tool_view_forbidden"):
        coordinator.validate_tool_view(("email_send",))


def test_failure_behavior_is_deterministic_and_never_fills_missing_facts() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)

    partial = coordinator.failure_resolution(
        failures=(("task:web", "allow_partial", "browser_denied"),),
        accepted_envelope_refs=("artifact:envelope:profile",),
    )
    failed = coordinator.failure_resolution(
        failures=(("task:web", "fail_parent", "browser_denied"),),
        accepted_envelope_refs=(),
    )

    assert partial.status == "partial"
    assert partial.missing == ("task:web",)
    assert partial.facts == {}
    assert failed.status == "failed"
    assert failed.facts == {}
    assert CoordinatorPhase.TERMINAL.value == "terminal"


def test_phase_driver_persists_validating_merging_and_terminal() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)

    validating = coordinator.advance_phase("parent:coordinator:1", CoordinatorPhase.VALIDATING)
    merging = coordinator.advance_phase("parent:coordinator:1", CoordinatorPhase.MERGING)
    terminal = coordinator.advance_phase("parent:coordinator:1", CoordinatorPhase.TERMINAL, terminal_status="partial")

    assert (validating.phase, merging.phase, terminal.phase) == ("validating", "merging", "terminal")
    assert terminal.status == "partial"


def test_phase_driver_rejects_skips_reversal_and_wrong_parent_status() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)

    with pytest.raises(ValueError, match="coordinator_phase_transition_invalid"):
        coordinator.advance_phase("parent:coordinator:1", CoordinatorPhase.MERGING)
    coordinator.advance_phase("parent:coordinator:1", CoordinatorPhase.VALIDATING)
    with pytest.raises(ValueError, match="coordinator_phase_transition_invalid"):
        coordinator.advance_phase("parent:coordinator:1", CoordinatorPhase.PLANNING)

    queued = _parent().model_copy(update={"id": "parent:queued", "status": "queued", "phase": "children_terminal"})
    store.create_parent(queued)
    with pytest.raises(ValueError, match="coordinator_parent_status_invalid"):
        coordinator.advance_phase(queued.id, CoordinatorPhase.MERGING)


def test_persisted_failure_behavior_controls_resolution_without_facts() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    contract = TaskContract(
        task_id="task:required", parent_run_id="parent:coordinator:1", specialist_id="job_web_researcher",
        goal="web", inputs={"query": "Sydney"}, requested_deadline=NOW + timedelta(minutes=10),
        requested_budget=_limits(10), failure_behavior="fail_parent", idempotency_key="task:required:key",
    )
    child = ChildRun(id="child:required", child_task_id=contract.task_id, parent_run_id=contract.parent_run_id,
        attempt=1, status="created", phase="created", deadline_at=contract.requested_deadline, created_at=NOW, updated_at=NOW)
    created = store.create_child_task_and_run(contract=contract, child_run=child, specialist_snapshot_id="snapshot:1", output_schema_version="v1", expected_parent_version=0, created_at=NOW)
    running = store.transition(child.id, "queued", expected_version=created.run.version, occurred_at=NOW)
    running = store.transition(child.id, "running", expected_version=running.version, occurred_at=NOW)
    store.transition(child.id, "failed", expected_version=running.version, occurred_at=NOW)

    resolution = Coordinator(store=store, now=lambda: NOW).resolve_persisted_failures("parent:coordinator:1")

    assert resolution.status == "failed"
    assert resolution.missing == ("task:required",)
    assert resolution.facts == {}


def test_wait_for_user_failure_behavior_preserves_waiting_semantics() -> None:
    resolution = Coordinator(store=SQLiteRunStore("sqlite:///:memory:", ROOT), now=lambda: NOW).failure_resolution(
        failures=(("task:login", "wait_for_user", "login_required"),),
        accepted_envelope_refs=(),
    )

    assert resolution.status == "waiting_for_user"
    assert resolution.facts == {}


def test_wait_for_user_failure_persists_parent_suspension() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    contract = TaskContract(task_id="task:login", parent_run_id="parent:coordinator:1", specialist_id="job_web_researcher", goal="web", inputs={"query": "Sydney"}, requested_deadline=NOW + timedelta(minutes=10), requested_budget=_limits(10), failure_behavior="wait_for_user", idempotency_key="task:login:key")
    child = ChildRun(id="child:login", child_task_id=contract.task_id, parent_run_id=contract.parent_run_id, attempt=1, status="created", phase="created", deadline_at=contract.requested_deadline, created_at=NOW, updated_at=NOW)
    created = store.create_child_task_and_run(contract=contract, child_run=child, specialist_snapshot_id="snapshot:1", output_schema_version="v1", expected_parent_version=0, created_at=NOW)
    queued = store.transition(child.id, "queued", expected_version=created.run.version, occurred_at=NOW)
    running = store.transition(child.id, "running", expected_version=queued.version, occurred_at=NOW)
    store.transition(child.id, "failed", expected_version=running.version, occurred_at=NOW)

    coordinator = Coordinator(store=store, now=lambda: NOW)
    from starter_agent.delegation.context import RunContext, RunTraceContext
    from starter_agent.domain.models import Message
    from uuid import uuid4
    coordinator.persist_checkpoint(RunContext(run_id="parent:coordinator:1", parent_run_id="parent:coordinator:1", session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=_limits(100), trace_context=RunTraceContext(parent_run_id="parent:coordinator:1")))
    context = RunContext(run_id="parent:coordinator:1", parent_run_id="parent:coordinator:1", session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=_limits(100), trace_context=RunTraceContext(parent_run_id="parent:coordinator:1"))
    outcome = coordinator.suspend_for_persisted_failures("parent:coordinator:1", context=context)

    parent = store.get_parent("parent:coordinator:1")
    assert outcome.disposition == "suspended" and outcome.status == "waiting_for_user"
    assert parent is not None and parent.status == parent.phase == "waiting_for_user"
    assert store.get_coordinator_checkpoint("parent:coordinator:1") is not None
    assert store.get_coordinator_checkpoint("parent:coordinator:1").parent_version == parent.version
