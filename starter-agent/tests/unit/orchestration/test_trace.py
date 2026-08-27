from datetime import UTC, datetime, timedelta

from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, TaskContract
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.orchestration.trace import (
    OrchestrationTraceCorrelation,
    OrchestrationTraceProjector,
    OrchestrationTraceRecord,
    TraceChainAuditor,
)
from starter_agent.trust.store import TrustStore
from starter_agent.trust.trace import DelegationEventTrustBridge


NOW = datetime(2026, 8, 15, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=10)


def limits(value: int) -> BudgetLimits:
    return BudgetLimits(
        steps=value,
        tokens=value,
        cost_microunits=value,
        wall_clock_ms=value,
        model_calls=value,
        tool_calls=value,
    )


def store() -> SQLiteRunStore:
    runs = SQLiteRunStore("sqlite:///:memory:", ".")
    runs.create_parent(
        ParentRun(
            id="parent:1",
            run_type="job_application_orchestration",
            session_id="session:1",
            origin_turn_id="turn:1",
            principal="user:1",
            coordinator_spec_version="v1",
            runtime_revision="v1",
            available_at=NOW,
            deadline_at=DEADLINE,
            budget_total=limits(100),
            budget_reserved=limits(0),
            budget_consumed=limits(0),
            route="plan_delegation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    contract = TaskContract(
        task_id="task:1",
        parent_run_id="parent:1",
        specialist_id="researcher",
        goal="read JD",
        inputs={},
        requested_deadline=DEADLINE,
        requested_budget=limits(1),
        failure_behavior="allow_partial",
        idempotency_key="task:1:create",
    )
    runs.create_child_task_and_run(
        contract=contract,
        child_run=ChildRun(
            id="child:1",
            child_task_id="task:1",
            parent_run_id="parent:1",
            attempt=1,
            deadline_at=DEADLINE,
            created_at=NOW,
            updated_at=NOW,
        ),
        specialist_snapshot_id="snapshot:1",
        output_schema_version="v1",
        expected_parent_version=0,
        created_at=NOW,
    )
    return runs


def record(event_type: str, **ids: str) -> OrchestrationTraceRecord:
    return OrchestrationTraceRecord(
        event_type=event_type,
        status="completed",
        correlation=OrchestrationTraceCorrelation(parent_run_id="parent:1", **ids),
        occurred_at=NOW,
        duration_ms=5,
        reason_code="fixture_decision",
        decision="continue",
    )


def test_orchestration_trace_chain_correlates_all_decisions_and_existing_runs() -> None:
    runs = store()
    projector = OrchestrationTraceProjector(runs)
    records = (
        record("route", route_decision_id="route:1"),
        record("plan", route_decision_id="route:1", plan_id="plan:1"),
        record("validation", plan_id="plan:1"),
        record("child_run", plan_id="plan:1", step_id="step:1", child_run_id="child:1"),
        record("task_event", plan_id="plan:1", step_id="step:1", child_run_id="child:1", task_event_id="task-event:1"),
        record("join", plan_id="plan:1", join_decision_id="join:1"),
        record("verify", plan_id="plan:1", verify_id="verify:1"),
        record("recovery", plan_id="plan:1", verify_id="verify:1", recovery_id="recovery:1"),
        record("budget", plan_id="plan:1", budget_snapshot_id="budget:1"),
        record("model_decision", plan_id="plan:1", model_decision_id="model:1"),
    )
    persisted = tuple(projector.record(item) for item in records)
    assert projector.record(records[0]) == persisted[0]
    audit = TraceChainAuditor().audit(
        runs.get_run_tree("parent:1").events,
        required_event_types=tuple(item.event_type for item in records),
    )
    assert audit.complete
    parent = runs.get_parent("parent:1")
    assert parent is not None and parent.status == "created"


def test_existing_trust_bridge_projects_safe_orchestration_correlation_ids() -> None:
    runs = store()
    OrchestrationTraceProjector(runs).record(
        record(
            "verify",
            plan_id="plan:1",
            child_run_id="child:1",
            verify_id="verify:1",
            budget_snapshot_id="budget:1",
            model_decision_id="model:1",
        )
    )
    trust = TrustStore("sqlite:///:memory:", ".")
    DelegationEventTrustBridge(runs, trust).sync_parent("parent:1")
    [trace] = [
        item
        for item in trust.list_trace_events(parent_run_id="parent:1")
        if item.summary.get("delegation_event_type") == "orchestration.verify"
    ]
    assert trace.event_type == "Orchestration"
    assert trace.child_run_id == "child:1"
    assert trace.summary["plan_id"] == "plan:1"
    assert trace.summary["verify_id"] == "verify:1"
    assert trace.summary["budget_snapshot_id"] == "budget:1"
    assert trace.summary["model_decision_id"] == "model:1"
    assert "secret" not in trace.model_dump_json().casefold()


def test_trace_auditor_reports_missing_and_orphan_plan_links() -> None:
    runs = store()
    OrchestrationTraceProjector(runs).record(
        record("verify", plan_id="plan:missing", verify_id="verify:1")
    )
    audit = TraceChainAuditor().audit(
        runs.get_run_tree("parent:1").events,
        required_event_types=("route", "plan", "verify"),
    )
    assert not audit.complete
    assert audit.missing_event_types == ("route", "plan")
    assert audit.orphan_correlation_ids == ("plan:missing",)
