from datetime import UTC, datetime, timedelta

from starter_agent.orchestration.budget import OrchestrationBudgetManager
from starter_agent.orchestration.dag import DAGScheduler, SchedulerLimits
from starter_agent.orchestration.models import (
    BudgetAmounts,
    Plan,
    PlanStep,
)


NOW = datetime(2026, 8, 14, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=10)


def amounts(value: int = 100) -> BudgetAmounts:
    return BudgetAmounts(**{name: value for name in BudgetAmounts.model_fields})


def step(
    index: int,
    *,
    depends_on=(),
    output="result-envelope:jd:v1",
    reads=(),
    writes=(),
    rate_keys=(),
    budget=10,
) -> PlanStep:
    return PlanStep(
        step_id=f"step:{index}",
        plan_id="plan:1",
        ordinal=index,
        goal=f"read JD {index}",
        risk="low",
        budget_limit=amounts(budget),
        deadline_at=DEADLINE,
        depends_on=depends_on,
        execution="child",
        specialist_id="job_web_researcher",
        output_contract_ref=output,
        parallel_candidate=True,
        read_resource_keys=reads,
        write_resource_keys=writes,
        rate_limit_keys=rate_keys,
    )


def plan(*steps: PlanStep) -> Plan:
    return Plan(
        plan_id="plan:1",
        parent_run_id="parent:1",
        status="valid",
        goal="fixture",
        steps=steps,
        budget_total=amounts(),
        deadline_at=DEADLINE,
        validation_result_id="validation:1",
        created_at=NOW,
        updated_at=NOW,
    )


def scheduler(*, budget=100, slots=3, rate_capacity=None) -> DAGScheduler:
    snapshot = OrchestrationBudgetManager().initial_snapshot(
        snapshot_id="budget:1",
        parent_run_id="parent:1",
        limit=amounts(budget),
        created_at=NOW,
    )
    return DAGScheduler(
        budget_snapshot=snapshot,
        limits=SchedulerLimits(
            max_parallel=3,
            available_slots=slots,
            rate_limit_capacity=rate_capacity or {},
        ),
    )


def test_three_independent_jds_are_parallel_ready() -> None:
    decision = scheduler().decide(plan(step(1), step(2), step(3)))
    assert decision.mode == "parallel"
    assert decision.selected_step_ids == ("step:1", "step:2", "step:3")
    assert decision.reason_code == "parallel_eligible"


def test_jd_and_resume_evidence_can_run_in_parallel() -> None:
    decision = scheduler().decide(
        plan(
            step(1, reads=("input:jd",), output="result-envelope:evidence:v1"),
            step(2, reads=("input:resume",), output="result-envelope:evidence:v1"),
        )
    )
    assert decision.mode == "parallel"
    assert len(decision.selected_step_ids) == 2


def test_dependency_blocks_downstream_until_input_is_ready() -> None:
    decision = scheduler().decide(plan(step(1), step(2, depends_on=("step:1",))))
    assert decision.mode == "serial"
    assert decision.selected_step_ids == ("step:1",)
    assert decision.deferred_step_ids == ("step:2",)


def test_shared_write_conflict_forces_serial_execution() -> None:
    decision = scheduler().decide(
        plan(step(1, writes=("external:draft",)), step(2, reads=("external:draft",)))
    )
    assert decision.mode == "serial"
    assert decision.selected_step_ids == ("step:1",)
    assert "shared_write_conflict" in decision.reasons["step:2"]


def test_non_envelope_contract_budget_and_rate_limit_prevent_parallelism() -> None:
    contract = scheduler().decide(
        plan(step(1, output="schema:jd"), step(2, output="schema:jd"))
    )
    assert contract.mode == "serial"
    assert contract.reason_code == "result_envelope_required"

    budget = scheduler(budget=15).decide(plan(step(1, budget=10), step(2, budget=10)))
    assert budget.mode == "serial"
    assert budget.reason_code == "parallel_budget_insufficient"

    rate = scheduler(rate_capacity={"site:jobs": 1}).decide(
        plan(step(1, rate_keys=("site:jobs",)), step(2, rate_keys=("site:jobs",)))
    )
    assert rate.mode == "serial"
    assert rate.reason_code == "rate_limit_serialized"


def test_backpressure_waits_without_starting_steps() -> None:
    decision = scheduler(slots=0).decide(plan(step(1), step(2)))
    assert decision.mode == "waiting"
    assert decision.selected_step_ids == ()
    assert decision.reason_code == "backpressure"

