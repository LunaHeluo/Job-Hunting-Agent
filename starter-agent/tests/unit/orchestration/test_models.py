from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from starter_agent.orchestration.models import (
    BackgroundTask,
    BudgetAmounts,
    BudgetSnapshot,
    ExecutionState,
    ModelDecision,
    ModelRequirements,
    PendingAction,
    Plan,
    PlanStep,
    RouteDecision,
)


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def zero_budget() -> BudgetAmounts:
    return BudgetAmounts()


def route(route: str = "direct") -> RouteDecision:
    return RouteDecision(
        route_decision_id="route:test",
        session_id="session:test",
        turn_id="turn:test",
        route=route,
        confidence=1,
        reason_code="fixture",
        reason_summary="fixed fixture",
        risk_level="low",
        fallback={"route": "human_review", "condition_code": "uncertain"},
        capability_snapshot_revision="cap:1",
        policy_revision="policy:1",
        created_at=NOW,
    )


def test_execution_state_round_trip_is_versioned_and_strict() -> None:
    snapshot = BudgetSnapshot(
        budget_snapshot_id="budget:test",
        parent_run_id="parent:test",
        phase="preflight",
        limit=zero_budget(),
        reserved=zero_budget(),
        consumed=zero_budget(),
        released=zero_budget(),
        remaining=zero_budget(),
        overage=zero_budget(),
        cost_status="actual",
        created_at=NOW,
    )
    state = ExecutionState(
        run_id="run:test",
        session_id="session:test",
        turn_id="turn:test",
        goal="Explain the job description",
        route=route(),
        budget=snapshot,
    )

    restored = ExecutionState.model_validate(state.model_dump(mode="json"))

    assert restored == state
    assert restored.schema_version == "1"
    with pytest.raises(ValidationError):
        ExecutionState.model_validate({**state.model_dump(mode="json"), "surprise": True})


def test_direct_state_cannot_hold_plan_or_children() -> None:
    step = PlanStep(
        step_id="step:1",
        plan_id="plan:1",
        ordinal=1,
        goal="read",
        risk="low",
        budget_limit=zero_budget(),
        deadline_at=NOW + timedelta(minutes=1),
        execution="local",
        output_contract_ref="schema:answer",
    )
    plan = Plan(
        plan_id="plan:1",
        parent_run_id="run:test",
        goal="read",
        steps=(step,),
        budget_total=zero_budget(),
        deadline_at=NOW + timedelta(minutes=1),
        created_at=NOW,
        updated_at=NOW,
    )

    with pytest.raises(ValidationError, match="direct route cannot own a plan"):
        ExecutionState(
            run_id="run:test",
            session_id="session:test",
            turn_id="turn:test",
            goal="answer",
            route=route(),
            plan=plan,
        )


def test_human_review_requires_pending_action_before_waiting() -> None:
    human_route = route("human_review")
    with pytest.raises(ValidationError, match="pending action"):
        ExecutionState(
            run_id="run:test",
            session_id="session:test",
            turn_id="turn:test",
            goal="send email",
            route=human_route,
            current_node="human_review",
            execution_status="waiting",
        )

    pending = PendingAction(
        pending_action_id="pending:test",
        parent_run_id="run:test",
        action_type="send_email",
        target_summary="approved test inbox",
        arguments_hash="a" * 64,
        risk_level="high",
        irreversible=True,
        principal="user:test",
        expires_at=NOW + timedelta(minutes=10),
        policy_revision="policy:1",
        gate_decision_id="gate:test",
        created_at=NOW,
    )
    state = ExecutionState(
        run_id="run:test",
        session_id="session:test",
        turn_id="turn:test",
        goal="send email",
        route=human_route,
        current_node="human_review",
        execution_status="waiting",
        pending_action=pending,
    )
    assert state.pending_action is not None


def test_background_task_public_status_is_explicit() -> None:
    task = BackgroundTask(
        task_id="task:test",
        parent_run_id="parent:test",
        session_id="session:test",
        origin_turn_id="turn:test",
        status="interrupted",
        internal_status="worker_lost",
        reason_code="process_interrupted",
        phase="terminal",
        budget_snapshot_id="budget:test",
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    assert task.status == "interrupted"


def test_model_decision_cannot_select_candidate_that_was_not_considered() -> None:
    with pytest.raises(ValidationError, match="selected model must be one of the candidates"):
        ModelDecision(
            model_decision_id="model:test",
            purpose="router",
            requirements=ModelRequirements(
                capabilities=("structured_output",),
                complexity="bounded",
                latency_class="interactive",
                context_tokens=100,
                risk_policy="normal",
            ),
            candidates=(),
            selected_provider="missing",
            selected_model="invented-model",
            reason_code="bad_fixture",
            reason_summary="must fail",
            config_revision="config:1",
            status="selected",
            created_at=NOW,
        )

