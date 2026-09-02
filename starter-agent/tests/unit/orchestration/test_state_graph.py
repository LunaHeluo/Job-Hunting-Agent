from datetime import UTC, datetime

import pytest

from starter_agent.orchestration.graph import (
    EdgeCondition,
    StateNode,
    StateTransitionError,
    allowed_targets,
    transition,
)
from starter_agent.orchestration.models import ExecutionState, RouteDecision


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def state(route: str) -> ExecutionState:
    return ExecutionState(
        run_id="run:test",
        session_id="session:test",
        turn_id="turn:test",
        goal="fixture",
        current_node="router",
        route=RouteDecision(
            route_decision_id="route:test",
            session_id="session:test",
            turn_id="turn:test",
            route=route,
            confidence=1,
            reason_code="fixture",
            reason_summary="fixture",
            risk_level="low" if route != "human_review" else "high",
            fallback={"route": "human_review", "condition_code": "uncertain"},
            capability_snapshot_revision="cap:1",
            policy_revision="policy:1",
            created_at=NOW,
        ),
    )


def test_router_has_conditional_targets_instead_of_one_mandatory_chain() -> None:
    targets = allowed_targets(StateNode.ROUTER)
    assert StateNode.EXECUTOR in targets
    assert StateNode.PLANNER in targets
    assert StateNode.HUMAN_REVIEW in targets
    assert StateNode.TASK_MANAGER not in targets


def test_direct_route_cannot_enter_planner_or_task_manager() -> None:
    current = state("direct")
    with pytest.raises(StateTransitionError, match="route_condition_failed"):
        transition(
            current,
            target=StateNode.PLANNER,
            condition=EdgeCondition.ROUTE_PLAN_DELEGATION,
        )


def test_plan_route_can_enter_planner() -> None:
    current = state("plan_delegation")
    updated = transition(
        current,
        target=StateNode.PLANNER,
        condition=EdgeCondition.ROUTE_PLAN_DELEGATION,
    )
    assert updated.current_node == "planner"
    assert updated.state_version == current.state_version + 1


def test_human_review_does_not_execute_tool() -> None:
    current = state("human_review")
    with pytest.raises(StateTransitionError):
        transition(
            current,
            target=StateNode.EXECUTOR,
            condition=EdgeCondition.ROUTE_HUMAN_REVIEW,
        )

