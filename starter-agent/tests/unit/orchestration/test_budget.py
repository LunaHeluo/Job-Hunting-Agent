from datetime import UTC, datetime

import pytest

from starter_agent.orchestration.budget import (
    BudgetPreflightDenied,
    OrchestrationBudgetManager,
)
from starter_agent.orchestration.models import BudgetAmounts


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def amounts(**changes: int) -> BudgetAmounts:
    values = {
        "steps": 10,
        "tokens": 10_000,
        "cost_microunits": 1_000_000,
        "wall_clock_ms": 120_000,
        "tool_calls": 20,
        "model_calls": 10,
    }
    values.update(changes)
    return BudgetAmounts(**values)


def test_preflight_and_consumption_cover_all_six_dimensions() -> None:
    manager = OrchestrationBudgetManager()
    initial = manager.initial_snapshot(
        snapshot_id="budget:0",
        parent_run_id="parent:1",
        limit=amounts(),
        created_at=NOW,
    )
    permit = manager.preflight(initial, amounts(steps=1, tokens=100, cost_microunits=10, wall_clock_ms=20, tool_calls=1, model_calls=1))
    assert permit.allowed is True

    consumed = manager.consume(
        initial,
        operation_id="budget-op:consume:1",
        usage=amounts(steps=1, tokens=100, cost_microunits=10, wall_clock_ms=20, tool_calls=1, model_calls=1),
        snapshot_id="budget:1",
        created_at=NOW,
    )
    assert consumed.consumed.steps == 1
    assert consumed.remaining.steps == 9
    assert consumed.consumed.tool_calls == 1


def test_budget_operation_is_idempotent() -> None:
    manager = OrchestrationBudgetManager()
    initial = manager.initial_snapshot(
        snapshot_id="budget:0",
        parent_run_id="parent:1",
        limit=amounts(),
        created_at=NOW,
    )
    first = manager.consume(
        initial,
        operation_id="budget-op:same",
        usage=amounts(steps=1, tokens=100, cost_microunits=0, wall_clock_ms=0, tool_calls=0, model_calls=0),
        snapshot_id="budget:1",
        created_at=NOW,
    )
    duplicate = manager.consume(
        first,
        operation_id="budget-op:same",
        usage=amounts(steps=1, tokens=100, cost_microunits=0, wall_clock_ms=0, tool_calls=0, model_calls=0),
        snapshot_id="budget:2",
        created_at=NOW,
    )
    assert duplicate is first
    assert duplicate.consumed.steps == 1


def test_fanout_is_atomic_when_required_children_exceed_parent() -> None:
    manager = OrchestrationBudgetManager()
    initial = manager.initial_snapshot(
        snapshot_id="budget:0",
        parent_run_id="parent:1",
        limit=amounts(steps=2, tokens=100),
        created_at=NOW,
    )
    with pytest.raises(BudgetPreflightDenied) as captured:
        manager.reserve_fanout(
            initial,
            operation_id="budget-op:fanout",
            child_requests={
                "child:1": amounts(steps=1, tokens=60, cost_microunits=0, wall_clock_ms=0, tool_calls=0, model_calls=0),
                "child:2": amounts(steps=1, tokens=60, cost_microunits=0, wall_clock_ms=0, tool_calls=0, model_calls=0),
            },
            snapshot_id="budget:1",
            created_at=NOW,
        )
    assert captured.value.dimension == "tokens"
    assert initial.reserved.tokens == 0


def test_settlement_releases_unused_child_budget_and_is_idempotent() -> None:
    manager = OrchestrationBudgetManager()
    initial = manager.initial_snapshot(
        snapshot_id="budget:0",
        parent_run_id="parent:1",
        limit=amounts(),
        created_at=NOW,
    )
    request = amounts(steps=2, tokens=1_000, cost_microunits=100, wall_clock_ms=1_000, tool_calls=2, model_calls=2)
    reserved = manager.reserve_fanout(
        initial,
        operation_id="budget-op:reserve",
        child_requests={"child:1": request},
        snapshot_id="budget:1",
        created_at=NOW,
    ).snapshot
    usage = amounts(steps=1, tokens=400, cost_microunits=40, wall_clock_ms=500, tool_calls=1, model_calls=1)
    settled = manager.settle_reservation(
        reserved,
        operation_id="budget-op:settle",
        reserved_amount=request,
        usage=usage,
        snapshot_id="budget:2",
        created_at=NOW,
    )
    duplicate = manager.settle_reservation(
        settled,
        operation_id="budget-op:settle",
        reserved_amount=request,
        usage=usage,
        snapshot_id="budget:3",
        created_at=NOW,
    )
    assert settled.reserved.tokens == 0
    assert settled.consumed.tokens == 400
    assert settled.released.tokens == 600
    assert duplicate is settled


def test_exhaustion_stops_and_reports_recovery_information() -> None:
    manager = OrchestrationBudgetManager()
    initial = manager.initial_snapshot(
        snapshot_id="budget:0",
        parent_run_id="parent:1",
        limit=amounts(steps=1, tokens=100),
        created_at=NOW,
    )
    stopped = manager.consume(
        initial,
        operation_id="budget-op:over",
        usage=amounts(steps=2, tokens=100, cost_microunits=0, wall_clock_ms=0, tool_calls=0, model_calls=0),
        snapshot_id="budget:1",
        created_at=NOW,
    )
    result = manager.stop_result(
        stopped,
        completed=("step:done",),
        incomplete=("step:blocked",),
    )
    assert stopped.phase == "stopped"
    assert stopped.stop_dimension == "steps"
    assert result.reason_code == "budget_exhausted"
    assert result.recovery_actions == ("increase_budget", "start_new_run")

