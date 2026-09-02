from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from starter_agent.delegation.models import (
    BudgetLimits,
    BudgetUsage,
    ChildRun,
    ChildTask,
    ParentRun,
    ResultEnvelope,
)
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.orchestration.fanout import (
    FanInGateway,
    FanOutBuilder,
    OrchestrationFanOutService,
)
from starter_agent.orchestration.models import BudgetAmounts, Plan, PlanStep


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


def parent(**changes: object) -> ParentRun:
    values: dict[str, object] = {
        "id": "parent:1",
        "run_type": "job_application_orchestration",
        "session_id": "session:1",
        "origin_turn_id": "turn:1",
        "principal": "user:1",
        "coordinator_spec_version": "v1",
        "runtime_revision": "v1",
        "available_at": NOW,
        "deadline_at": DEADLINE,
        "budget_total": limits(100),
        "budget_reserved": limits(0),
        "budget_consumed": limits(0),
        "route": "plan_delegation",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ParentRun(**values)


def plan() -> Plan:
    steps = tuple(
        PlanStep(
            step_id=f"step:{index}",
            plan_id="plan:1",
            ordinal=index,
            goal=f"读取 JD {index}",
            input_refs=(f"artifact:jd:{index}",),
            risk="low",
            budget_limit=BudgetAmounts(
                steps=1,
                tokens=10,
                cost_microunits=10,
                wall_clock_ms=10,
                model_calls=1,
                tool_calls=1,
            ),
            deadline_at=DEADLINE,
            execution="child",
            specialist_id="job_web_researcher",
            output_contract_ref="result-envelope:job-web:v1",
            parallel_candidate=True,
            failure_behavior="allow_partial",
        )
        for index in (1, 2)
    )
    return Plan(
        plan_id="plan:1",
        parent_run_id="parent:1",
        status="valid",
        goal="读取两个 JD",
        steps=steps,
        budget_total=BudgetAmounts(
            steps=10,
            tokens=100,
            cost_microunits=100,
            wall_clock_ms=100,
            model_calls=10,
            tool_calls=10,
        ),
        deadline_at=DEADLINE,
        validation_result_id="validation:1",
        created_at=NOW,
        updated_at=NOW,
    )


def build():
    return FanOutBuilder().build(
        parent=parent(),
        plan=plan(),
        selected_step_ids=("step:1", "step:2"),
        requested_tools={
            "step:1": ("browser",),
            "step:2": ("browser",),
        },
        policy_tools=frozenset({"browser"}),
        specialist_tools={"job_web_researcher": frozenset({"browser", "search"})},
        created_at=NOW,
        route_decision_id="route:1",
    )


def test_builder_creates_isolated_stable_minimal_packages() -> None:
    first = build()
    second = build()
    assert first == second
    assert len({item.child_run.id for item in first.packages}) == 2
    for package in first.packages:
        assert package.contract.parent_run_id == "parent:1"
        assert package.contract.requested_allowed_tools == ("browser",)
        assert package.contract.inputs == {
            "input_refs": (f"artifact:jd:{package.step_id[-1]}",),
            "artifact_refs": (f"artifact:jd:{package.step_id[-1]}",),
        }
        serialized = package.model_dump_json().casefold()
        assert "full_chat" not in serialized
        assert "scratchpad" not in serialized
        assert package.child_run.deadline_at <= parent().deadline_at


def test_builder_rejects_tool_authority_expansion() -> None:
    with pytest.raises(ValueError, match="fanout_tool_authority_expansion"):
        FanOutBuilder().build(
            parent=parent(),
            plan=plan(),
            selected_step_ids=("step:1",),
            requested_tools={"step:1": ("send_email",)},
            policy_tools=frozenset({"browser"}),
            specialist_tools={"job_web_researcher": frozenset({"browser"})},
            created_at=NOW,
            route_decision_id="route:1",
        )


def test_fanout_persists_two_children_in_existing_store() -> None:
    root = Path(__file__).resolve().parents[2]
    db = root / ".session-only-orchestration" / str(uuid4()) / "runs.db"
    store = SQLiteRunStore(f"sqlite:///{db}", root)
    store.create_parent(parent())
    created = OrchestrationFanOutService(store).persist(
        build(),
        expected_parent_version=0,
        specialist_snapshot_ids={"job_web_researcher": "snapshot:1"},
        output_schema_versions={"job_web_researcher": "job-web-v1"},
        created_at=NOW,
    )
    tree = store.get_run_tree("parent:1")
    assert len(created) == len(tree.child_runs) == len(tree.child_tasks) == 2
    assert {item.child_task_id for item in tree.child_runs} == {
        item.id for item in tree.child_tasks
    }


class FakeArtifacts:
    def __init__(self, envelope: ResultEnvelope) -> None:
        self.envelope = envelope

    def get_tool_artifact_for_principal(self, artifact_ref: str, *, principal: str):
        assert artifact_ref == "artifact:envelope:1"
        assert principal == "user:1"
        return {"content": self.envelope.model_dump_json()}


def test_fanin_accepts_only_authoritative_result_envelope_projection() -> None:
    envelope = ResultEnvelope(
        status="succeeded",
        output={"jobs": [{"title": "Agent Engineer"}], "scratchpad": "private"},
        evidence=({"artifact_ref": "artifact:evidence:1", "source_url": "https://jobs.test/1"},),
        missing=(),
        conflicts=(),
        usage=BudgetUsage(
            **limits(1).model_dump(), estimated=False, cost_status="actual"
        ),
        child_run_id="child:1",
        task_id="task:1",
        trace_ref="trace:child:1",
        idempotency_key="result:child:1",
    )
    task = ChildTask(
        id="task:1",
        parent_run_id="parent:1",
        specialist_id="job_web_researcher",
        specialist_snapshot_id="snapshot:1",
        goal="读取 JD",
        inputs_ref_json={"input_refs": ["artifact:jd:1"]},
        constraints_json={"step_id": "step:1"},
        output_schema_version="job-web-v1",
        requested_allowed_tools=("browser",),
        requested_deadline=DEADLINE,
        requested_budget=limits(1),
        failure_behavior="allow_partial",
        idempotency_key="fanout:1",
        contract_hash="a" * 64,
        contract_version="orchestration-v1",
        accepted_child_run_id="child:1",
        accepted_result_envelope_ref="artifact:envelope:1",
        accepted_result_hash=envelope.canonical_hash,
        accepted_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    child = ChildRun(
        id="child:1",
        child_task_id="task:1",
        parent_run_id="parent:1",
        attempt=1,
        status="succeeded",
        phase="terminal",
        deadline_at=DEADLINE,
        result_envelope_ref="artifact:envelope:1",
        result_hash=envelope.canonical_hash,
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
        updated_at=NOW,
    )
    fake_store = SimpleNamespace(
        get_run_tree=lambda _parent: SimpleNamespace(
            parent=parent(status="succeeded", started_at=NOW, completed_at=NOW),
            child_tasks=(task,),
            child_runs=(child,),
        )
    )
    result = FanInGateway(
        store=fake_store,
        artifact_reader=FakeArtifacts(envelope),
    ).collect("parent:1")
    assert result[0].step_id == "step:1"
    assert result[0].projection.output_fields == ("jobs",)
    assert "private" not in result[0].projection.model_dump_json()
