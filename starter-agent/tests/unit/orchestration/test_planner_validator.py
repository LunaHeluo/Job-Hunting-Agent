from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from starter_agent.domain.models import Message, ModelResponse
from starter_agent.orchestration.models import (
    BudgetAmounts,
    BudgetSnapshot,
    DoneWhenRule,
    ModelCandidate,
    ModelDecision,
    ModelRequirements,
    Plan,
    PlanStep,
    RouteDecision,
)
from starter_agent.orchestration.planner import Planner, PlannerRequest
from starter_agent.orchestration.plan_validator import (
    PlanValidationContext,
    PlanValidator,
)
from starter_agent.providers.base import Provider


NOW = datetime(2026, 8, 14, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=10)


def amounts(value: int = 100, **changes: int) -> BudgetAmounts:
    values = {name: value for name in BudgetAmounts.model_fields}
    values.update(changes)
    return BudgetAmounts(**values)


def budget_snapshot(value: int = 100) -> BudgetSnapshot:
    return BudgetSnapshot(
        budget_snapshot_id="budget:1",
        parent_run_id="parent:1",
        phase="preflight",
        limit=amounts(value),
        reserved=BudgetAmounts(),
        consumed=BudgetAmounts(),
        released=BudgetAmounts(),
        remaining=amounts(value),
        overage=BudgetAmounts(),
        cost_status="actual",
        created_at=NOW,
    )


def route(route_name: str = "plan_delegation") -> RouteDecision:
    return RouteDecision(
        route_decision_id="route:1",
        session_id="session:1",
        turn_id="turn:1",
        route=route_name,
        confidence=1,
        reason_code="fixture",
        reason_summary="fixture",
        required_capabilities=("planner",),
        risk_level="medium",
        fallback={"route": "human_review", "condition_code": "invalid"},
        capability_snapshot_revision="cap:1",
        policy_revision="policy:1",
        created_at=NOW,
    )


def model_decision() -> ModelDecision:
    candidate = ModelCandidate(
        provider="fixture",
        model="planner-model",
        capabilities=("structured_output",),
        latency_class="background",
        health="healthy",
    )
    return ModelDecision(
        model_decision_id="model:1",
        purpose="planner",
        requirements=ModelRequirements(
            capabilities=("structured_output",),
            complexity="complex",
            latency_class="background",
            context_tokens=100,
            risk_policy="normal",
        ),
        candidates=(candidate,),
        selected_provider="fixture",
        selected_model="planner-model",
        reason_code="fixture",
        reason_summary="fixture",
        config_revision="config:1",
        status="selected",
        created_at=NOW,
    )


class SequenceProvider(Provider):
    name = "fixture"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | None = None,
        context_revision: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        del on_delta, tool_choice, context_revision, max_output_tokens
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        return ModelResponse(content=self.outputs.pop(0), provider=self.name, model=model)

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def valid_plan_json() -> str:
    return """{
      "plan_id":"plan:1","parent_run_id":"parent:1","goal":"compare jobs",
      "steps":[{
        "step_id":"step:1","plan_id":"plan:1","ordinal":1,"goal":"read JD",
        "capabilities":["job_description_reader"],
        "done_when":[{"rule_id":"schema:jd","type":"schema","expected":{"valid":true}}],
        "risk":"low","budget_limit":{"steps":1,"tokens":10,"cost_microunits":10,"wall_clock_ms":10,"tool_calls":1,"model_calls":1},
        "deadline_at":"2026-08-14T00:05:00Z","execution":"tool_loop",
        "output_contract_ref":"schema:jd"
      }],
      "join_policy":"all_required",
      "budget_total":{"steps":2,"tokens":20,"cost_microunits":20,"wall_clock_ms":20,"tool_calls":2,"model_calls":2},
      "deadline_at":"2026-08-14T00:10:00Z",
      "created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:00:00Z"
    }"""


async def test_planner_only_accepts_complex_route_and_never_exposes_tools() -> None:
    provider = SequenceProvider([valid_plan_json()])
    request = PlannerRequest(
        parent_run_id="parent:1",
        goal="compare jobs",
        capability_summary=("job_description_reader",),
        budget_total=amounts(),
        deadline_at=DEADLINE,
    )
    plan = await Planner().create_plan(
        request,
        route_decision=route(),
        model_decision=model_decision(),
        provider=provider,
    )
    assert plan.plan_id == "plan:1"
    assert provider.calls[0]["tools"] == []
    assert provider.calls[0]["model"] == "planner-model"
    assert plan.steps[0].done_when[0].rule_id == "schema:jd"

    with pytest.raises(ValueError, match="planner_route_not_allowed"):
        await Planner().create_plan(
            request,
            route_decision=route("direct"),
            model_decision=model_decision(),
            provider=SequenceProvider([valid_plan_json()]),
        )


def step(step_id: str, *, depends_on=(), capabilities=("reader",), risk="low", budget=10, done=True) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        plan_id="plan:test",
        ordinal=int(step_id.split(":")[-1]),
        goal=f"goal {step_id}",
        capabilities=capabilities,
        done_when=(DoneWhenRule(rule_id=f"rule:{step_id}", type="schema", expected={"ok": True}),) if done else (),
        risk=risk,
        budget_limit=amounts(budget),
        deadline_at=DEADLINE - timedelta(minutes=1),
        depends_on=depends_on,
        execution="tool_loop",
        output_contract_ref=f"schema:{step_id}",
    )


def plan(*steps: PlanStep, budget=100) -> Plan:
    return Plan(
        plan_id="plan:test",
        parent_run_id="parent:1",
        goal="fixture",
        steps=steps,
        budget_total=amounts(budget),
        deadline_at=DEADLINE,
        created_at=NOW,
        updated_at=NOW,
    )


def context(**changes: object) -> PlanValidationContext:
    values: dict[str, object] = {
        "capability_snapshot_revision": "cap:1",
        "policy_revision": "policy:1",
        "enabled_capabilities": ("reader",),
        "authorized_capabilities": ("reader",),
        "healthy_capabilities": ("reader",),
        "budget_snapshot": budget_snapshot(),
        "run_deadline_at": DEADLINE,
    }
    values.update(changes)
    return PlanValidationContext(**values)


def test_validator_accepts_valid_plan_before_execution() -> None:
    result = PlanValidator().validate(
        plan(step("step:1")),
        context=context(),
        validation_result_id="validation:1",
        validated_at=NOW,
    )
    assert result.valid is True
    assert result.decision == "execute"
    assert result.issues == ()


@pytest.mark.parametrize(
    ("candidate", "expected_code"),
    [
        (plan(step("step:1", depends_on=("step:2",)), step("step:2", depends_on=("step:1",))), "plan_cycle"),
        (plan(step("step:1", depends_on=("step:missing",))), "dependency_missing"),
        (plan(step("step:1", done=False)), "done_when_missing"),
        (plan(step("step:1", capabilities=("disabled",))), "capability_disabled"),
    ],
)
def test_validator_rejects_cycle_missing_dependency_done_when_and_disabled_capability(candidate: Plan, expected_code: str) -> None:
    result = PlanValidator().validate(
        candidate,
        context=context(),
        validation_result_id=f"validation:{expected_code}",
        validated_at=NOW,
    )
    assert result.valid is False
    assert expected_code in {issue.code for issue in result.issues}
    assert result.decision in {"revise", "stop"}


def test_validator_rejects_budget_overallocation() -> None:
    result = PlanValidator().validate(
        plan(step("step:1", budget=60), step("step:2", budget=60)),
        context=context(),
        validation_result_id="validation:budget",
        validated_at=NOW,
    )
    assert result.decision == "stop"
    assert "budget_exceeded" in {issue.code for issue in result.issues}


def test_high_risk_step_without_approval_goes_to_human_review() -> None:
    result = PlanValidator().validate(
        plan(step("step:1", risk="high")),
        context=context(),
        validation_result_id="validation:risk",
        validated_at=NOW,
    )
    assert result.valid is False
    assert result.decision == "human_review"
    assert "approval_required" in {issue.code for issue in result.issues}

