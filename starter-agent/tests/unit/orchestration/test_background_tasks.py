from datetime import UTC, datetime, timedelta

import pytest

from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.orchestration.background import (
    BackgroundTaskConflict,
    BackgroundTaskService,
    BackgroundTaskSpec,
)
from starter_agent.orchestration.models import (
    BudgetAmounts,
    BudgetSnapshot,
    ExecutionState,
    RouteDecision,
)


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def state(goal: str = "Batch research") -> ExecutionState:
    budget = BudgetSnapshot(
        budget_snapshot_id="budget:1",
        parent_run_id="placeholder",
        phase="preflight",
        limit=BudgetAmounts(steps=10, tokens=100, cost_microunits=100, wall_clock_ms=100, tool_calls=10, model_calls=10),
        reserved=BudgetAmounts(),
        consumed=BudgetAmounts(),
        released=BudgetAmounts(),
        remaining=BudgetAmounts(steps=10, tokens=100, cost_microunits=100, wall_clock_ms=100, tool_calls=10, model_calls=10),
        overage=BudgetAmounts(),
        cost_status="actual",
        created_at=NOW,
    )
    return ExecutionState(
        run_id="placeholder",
        parent_run_id="placeholder",
        session_id="session:1",
        turn_id="turn:1",
        goal=goal,
        route=RouteDecision(
            route_decision_id="route:1",
            run_id="placeholder",
            session_id="session:1",
            turn_id="turn:1",
            route="plan_delegation",
            confidence=1,
            reason_code="fixture",
            reason_summary="fixture",
            required_capabilities=("planner",),
            risk_level="medium",
            fallback={"route": "human_review", "condition_code": "invalid"},
            capability_snapshot_revision="cap:1",
            policy_revision="policy:1",
            created_at=NOW,
        ),
        budget=budget,
    )


def service(tmp_path) -> BackgroundTaskService:
    return BackgroundTaskService(SQLiteRunStore(f"sqlite:///{tmp_path / 'runs.db'}", tmp_path))


def spec(goal: str = "Batch research") -> BackgroundTaskSpec:
    return BackgroundTaskSpec(
        state=state(goal),
        principal="user:1",
        idempotency_key="background:create:1",
        runtime_revision="runtime:1",
        coordinator_spec_version="orchestration:1",
        deadline_at=NOW + timedelta(minutes=10),
    )


def test_background_creation_is_durable_idempotent_and_immediately_queued(tmp_path) -> None:
    manager = service(tmp_path)
    first = manager.create(spec(), created_at=NOW)
    replay = manager.create(spec(), created_at=NOW)
    assert first == replay
    assert first.task_id.startswith("task:orch:")
    assert first.status == "queued"
    assert manager.get(first.task_id) == first
    parent = manager.store.get_parent(first.parent_run_id)
    assert parent is not None
    assert parent.run_type == "job_application_orchestration"
    assert parent.task_id == first.task_id


def test_same_idempotency_key_with_different_goal_is_conflict(tmp_path) -> None:
    manager = service(tmp_path)
    manager.create(spec(), created_at=NOW)
    with pytest.raises(BackgroundTaskConflict, match="idempotency_payload_conflict"):
        manager.create(spec("Different goal"), created_at=NOW)


def test_public_lifecycle_maps_running_waiting_partial_completed_failed_cancelled(tmp_path) -> None:
    manager = service(tmp_path)
    task = manager.create(spec(), created_at=NOW)
    running = manager.transition(task.task_id, "running", expected_version=task.version, occurred_at=NOW)
    waiting = manager.transition(running.task_id, "waiting", expected_version=running.version, occurred_at=NOW)
    resumed = manager.transition(waiting.task_id, "running", expected_version=waiting.version, occurred_at=NOW)
    partial = manager.transition(resumed.task_id, "partial", expected_version=resumed.version, occurred_at=NOW)
    assert [running.status, waiting.status, resumed.status, partial.status] == [
        "running", "waiting", "running", "partial"
    ]


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_terminal_lifecycle_is_explicit(tmp_path, terminal: str) -> None:
    manager = service(tmp_path)
    task = manager.create(spec(), created_at=NOW)
    running = manager.transition(task.task_id, "running", expected_version=task.version, occurred_at=NOW)
    ended = manager.transition(
        running.task_id,
        terminal,
        expected_version=running.version,
        occurred_at=NOW,
        reason_code=f"fixture_{terminal}",
    )
    assert ended.status == terminal
    assert ended.completed_at == NOW


def test_process_loss_marks_interrupted_without_step_checkpoint_recovery(tmp_path) -> None:
    manager = service(tmp_path)
    task = manager.create(spec(), created_at=NOW)
    running = manager.transition(task.task_id, "running", expected_version=task.version, occurred_at=NOW)
    interrupted = manager.mark_interrupted(
        running.task_id,
        expected_version=running.version,
        occurred_at=NOW,
        reason_code="process_interrupted",
    )
    assert interrupted.status == "interrupted"
    assert interrupted.reason_code == "process_interrupted"
    assert interrupted.completed_at == NOW

