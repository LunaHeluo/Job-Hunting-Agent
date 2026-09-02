from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, RunOutcome, RunSpec
from starter_agent.orchestration.controller import OrchestrationController
from starter_agent.orchestration.executor import ExecutionResult, UnifiedExecutor
from starter_agent.orchestration.models import (
    BudgetAmounts,
    ExecutionState,
    Plan,
    PlanStep,
    RouteDecision,
)


NOW = datetime(2026, 8, 14, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=10)


class RuntimeSpy:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, *, spec, context):
        self.calls.append((spec, context))
        return RunOutcome(
            disposition="completed",
            run_id=spec.run_id,
            status="succeeded",
            output_ref="output:1",
        )


def context() -> RunContext:
    session_id = uuid4()
    turn_id = uuid4()
    return RunContext(
        run_id="parent:1",
        parent_run_id="parent:1",
        session_id=session_id,
        turn_id=turn_id,
        principal="user:1",
        messages=[],
        effective_tool_view=["reader"],
        budget_limits=BudgetLimits(
            steps=10,
            tokens=100,
            cost_microunits=100,
            wall_clock_ms=100,
            model_calls=10,
            tool_calls=10,
        ),
        trace_context=RunTraceContext(parent_run_id="parent:1"),
    )


def route(name: str, *, session_id: str, turn_id: str) -> RouteDecision:
    return RouteDecision(
        route_decision_id=f"route:{name}",
        run_id="parent:1",
        session_id=session_id,
        turn_id=turn_id,
        route=name,
        confidence=1,
        reason_code="fixture",
        reason_summary="fixture",
        risk_level="low",
        fallback={"route": "human_review", "condition_code": "failure"},
        capability_snapshot_revision="cap:1",
        policy_revision="policy:1",
        created_at=NOW,
    )


def state(name: str) -> tuple[ExecutionState, RunContext]:
    ctx = context()
    return (
        ExecutionState(
            run_id=ctx.run_id,
            parent_run_id=ctx.parent_run_id,
            session_id=str(ctx.session_id),
            turn_id=str(ctx.turn_id),
            goal="fixture",
            current_node="executor",
            execution_status="running",
            route=route(name, session_id=str(ctx.session_id), turn_id=str(ctx.turn_id)),
        ),
        ctx,
    )


def spec(*, tools=()) -> RunSpec:
    return RunSpec(
        run_id="parent:1",
        run_kind="parent",
        role="coordinator",
        provider="fixture",
        model="fixture-model",
        system_prompt_ref="prompt:1",
        output_schema_ref="schema:1",
        allowed_tools=tools,
        max_steps=2,
        runtime_revision="runtime:1",
    )


async def test_direct_uses_unique_runtime_without_tools_or_planner() -> None:
    runtime = RuntimeSpy()
    executor = UnifiedExecutor(runtime=runtime)
    current, ctx = state("direct")
    result = await executor.execute(current, context=ctx, spec=spec())
    assert result.status == "succeeded"
    assert runtime.calls[0][0].allowed_tools == ()
    assert result.output_ref == "output:1"


async def test_direct_rejects_tool_exposure_and_budget_denial_before_runtime() -> None:
    runtime = RuntimeSpy()
    executor = UnifiedExecutor(runtime=runtime)
    current, ctx = state("direct")
    with pytest.raises(ValueError, match="direct_tools_forbidden"):
        await executor.execute(current, context=ctx, spec=spec(tools=("reader",)))
    with pytest.raises(ValueError, match="execution_budget_unavailable"):
        await executor.execute(current, context=ctx, spec=spec(), budget_available=False)
    assert runtime.calls == []


async def test_workflow_and_tool_loop_use_only_their_registered_adapters() -> None:
    runtime = RuntimeSpy()
    workflow_calls = []

    async def weekly(state, context):
        workflow_calls.append((state, context))
        return ExecutionResult(status="succeeded", output_ref="workflow:1")

    executor = UnifiedExecutor(runtime=runtime, workflows={"job_weekly_report": weekly})
    workflow_state, workflow_context = state("workflow")
    workflow_result = await executor.execute(
        workflow_state,
        context=workflow_context,
        workflow_id="job_weekly_report",
    )
    assert workflow_result.output_ref == "workflow:1"
    assert len(workflow_calls) == 1
    assert runtime.calls == []

    tool_state, tool_context = state("tool_loop")
    await executor.execute(tool_state, context=tool_context, spec=spec(tools=("reader",)))
    assert runtime.calls[-1][0].allowed_tools == ("reader",)


async def test_child_step_uses_delegation_adapter_only_after_validated_plan() -> None:
    runtime = RuntimeSpy()
    delegated = []

    async def schedule(state, step, context):
        delegated.append((state.run_id, step.step_id, context.run_id))
        return ExecutionResult(status="scheduled", output_ref="child-task:1")

    step = PlanStep(
        step_id="step:1",
        plan_id="plan:1",
        ordinal=1,
        goal="read",
        risk="low",
        budget_limit=BudgetAmounts(steps=1),
        deadline_at=DEADLINE,
        execution="child",
        specialist_id="job_web_researcher",
        output_contract_ref="result-envelope:job",
    )
    plan = Plan(
        plan_id="plan:1",
        parent_run_id="parent:1",
        status="valid",
        goal="fixture",
        steps=(step,),
        budget_total=BudgetAmounts(steps=1),
        deadline_at=DEADLINE,
        validation_result_id="validation:1",
        created_at=NOW,
        updated_at=NOW,
    )
    current, ctx = state("plan_delegation")
    current = current.model_copy(update={"plan": plan, "current_step": "step:1"})
    result = await UnifiedExecutor(runtime=runtime, delegation_schedule=schedule).execute(
        current,
        context=ctx,
    )
    assert result.status == "scheduled"
    assert delegated == [("parent:1", "step:1", "parent:1")]
    assert runtime.calls == []


def test_controller_routes_to_only_the_needed_node_and_ends_without_verifier() -> None:
    current, _ = state("direct")
    current = current.model_copy(update={"current_node": "router", "execution_status": "created"})
    controller = OrchestrationController()
    executing = controller.enter_route(current)
    assert executing.current_node == "executor"
    ended = controller.after_execution(
        executing,
        ExecutionResult(status="succeeded", output_ref="output:1", requires_verification=False),
    )
    assert ended.current_node == "end"
    assert ended.execution_status == "completed"

