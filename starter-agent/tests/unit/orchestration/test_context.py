from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, BudgetUsage, ResultEnvelope
from starter_agent.domain.models import Message
from starter_agent.orchestration.context import OrchestrationContextManager
from starter_agent.orchestration.models import ExecutionState, RouteDecision


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def limits(value: int = 100) -> BudgetLimits:
    return BudgetLimits(
        steps=value,
        tokens=value,
        cost_microunits=value,
        wall_clock_ms=value,
        model_calls=value,
        tool_calls=value,
    )


def run_context() -> RunContext:
    return RunContext(
        run_id="parent:1",
        parent_run_id="parent:1",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:1",
        messages=[Message(role="user", content="full private parent conversation")],
        effective_tool_view=[],
        budget_limits=limits(),
        trace_context=RunTraceContext(parent_run_id="parent:1"),
        todo_plan=[{"id": "todo:1", "status": "ready"}],
        working_memory={"temporary": "may be trimmed"},
    )


def state(context: RunContext) -> ExecutionState:
    return ExecutionState(
        run_id=context.run_id,
        parent_run_id=context.parent_run_id,
        session_id=str(context.session_id),
        turn_id=str(context.turn_id),
        goal="Compare jobs safely",
        route=RouteDecision(
            route_decision_id="route:1",
            run_id=context.run_id,
            session_id=str(context.session_id),
            turn_id=str(context.turn_id),
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
    )


def test_run_context_checkpoint_round_trip_preserves_orchestration_control_state() -> None:
    context = run_context()
    attached = OrchestrationContextManager().attach_state(context, state(context))
    restored = RunContext.from_checkpoint(attached.to_checkpoint())
    assert restored.orchestration_state == attached.orchestration_state
    assert restored.orchestration_state.goal == "Compare jobs safely"
    assert restored.todo_plan == [{"id": "todo:1", "status": "ready"}]


def test_parent_projection_keeps_goal_policy_plan_budget_and_todo_outside_chat_summary() -> None:
    context = run_context()
    OrchestrationContextManager().attach_state(context, state(context))
    projection = OrchestrationContextManager().parent_projection(
        context,
        safety_policy_refs=("policy:1",),
        confirmed_facts=("location=Shanghai",),
        chat_summary="trimmed chat only",
        memory_refs=("memory:preference:1",),
    )
    assert projection.goal == "Compare jobs safely"
    assert projection.safety_policy_refs == ("policy:1",)
    assert projection.todo == ({"id": "todo:1", "status": "ready"},)
    assert projection.chat_summary == "trimmed chat only"
    assert "full private parent conversation" not in projection.model_dump_json()


def test_child_result_projection_never_copies_child_context_or_scratchpad() -> None:
    envelope = ResultEnvelope(
        status="partial",
        output={
            "summary": "two jobs found",
            "messages": ["private child message"],
            "scratchpad": "private reasoning",
            "working_memory": {"secret": "not for parent"},
        },
        evidence=({"artifact_ref": "artifact:evidence:1", "source_url": "https://jobs.example.test/1"},),
        missing=("salary",),
        conflicts=(),
        usage=BudgetUsage(
            steps=1,
            tokens=10,
            cost_microunits=1,
            wall_clock_ms=10,
            model_calls=1,
            tool_calls=1,
            cost_status="actual",
            usage_source="fixture",
        ),
        child_run_id="child:1",
        task_id="task:1",
        trace_ref="trace:child:1",
        idempotency_key="result:1",
    )
    projection = OrchestrationContextManager().project_child_result(
        envelope,
        result_envelope_ref="result-envelope:1",
        artifact_refs=("artifact:evidence:1",),
    )
    serialized = projection.model_dump_json()
    assert projection.result_envelope_ref == "result-envelope:1"
    assert projection.artifact_refs == ("artifact:evidence:1",)
    assert "private child message" not in serialized
    assert "private reasoning" not in serialized
    assert "working_memory" not in serialized


def test_attach_state_rejects_cross_run_or_cross_session_state() -> None:
    context = run_context()
    mismatched = state(context).model_copy(update={"run_id": "other:run"})
    with pytest.raises(ValueError, match="orchestration_state_run_mismatch"):
        OrchestrationContextManager().attach_state(context, mismatched)

