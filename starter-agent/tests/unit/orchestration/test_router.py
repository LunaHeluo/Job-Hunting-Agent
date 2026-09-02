from datetime import UTC, datetime

import pytest

from starter_agent.orchestration.models import BudgetAmounts
from starter_agent.orchestration.budget import OrchestrationBudgetManager
from starter_agent.orchestration.router import (
    ExecutionRouter,
    ModelRouteClassification,
    RoutingCapabilitySnapshot,
    RoutingRequest,
)


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def capabilities(*, disabled: tuple[str, ...] = ()) -> RoutingCapabilitySnapshot:
    all_capabilities = {
        "job_weekly_report",
        "job_description_reader",
        "planner",
        "delegation",
        "resume_evidence_search",
        "send_email",
    }
    return RoutingCapabilitySnapshot(
        revision="cap:1",
        policy_revision="policy:1",
        enabled=tuple(sorted(all_capabilities - set(disabled))),
        disabled=disabled,
    )


def budget(*, stopped: bool = False):
    manager = OrchestrationBudgetManager()
    snapshot = manager.initial_snapshot(
        snapshot_id="budget:1",
        parent_run_id="parent:1",
        limit=BudgetAmounts(
            steps=20,
            tokens=20_000,
            cost_microunits=1_000_000,
            wall_clock_ms=300_000,
            tool_calls=20,
            model_calls=10,
        ),
        created_at=NOW,
    )
    return snapshot.model_copy(update={"phase": "stopped", "stop_dimension": "steps"}) if stopped else snapshot


def decide(text: str, **request_changes: object):
    values: dict[str, object] = {
        "session_id": "session:1",
        "turn_id": "turn:1",
        "user_text": text,
    }
    values.update(request_changes)
    return ExecutionRouter().decide(
        RoutingRequest(**values),
        capability_snapshot=capabilities(),
        budget_snapshot=budget(),
        decision_id="route:1",
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("text", "expected_route"),
    [
        ("请解释什么是 STAR 面试法", "direct"),
        ("生成本周固定求职周报", "workflow"),
        ("读取这个 JD：https://jobs.example.test/1", "tool_loop"),
        ("后台批量调研北京的 Agent 岗位", "plan_delegation"),
        ("并行读取这三个独立 JD 并比较", "plan_delegation"),
        ("并行搜集 JD 与我的简历证据，汇合后排序", "plan_delegation"),
    ],
)
def test_fixed_routes_cover_minimum_execution_paths(text: str, expected_route: str) -> None:
    decision = decide(text)
    assert decision.route == expected_route
    assert decision.reason_summary
    assert decision.fallback.condition_code


def test_high_risk_send_email_overrides_explicit_direct_request() -> None:
    decision = decide(
        "直接发送求职邮件给 hr@example.test",
        explicit_route="direct",
        provided_inputs={"recipient": "hr@example.test", "content_ref": "draft:1"},
    )
    assert decision.route == "human_review"
    assert decision.risk_level == "high"
    assert "rule:high_risk_external_write" in decision.matched_rules
    assert "rule:user_explicit_direct" in decision.conflicting_rules


def test_missing_high_risk_input_is_reported_and_never_guessed() -> None:
    decision = decide("帮我发送求职邮件")
    assert decision.route == "human_review"
    assert decision.status == "clarification_required"
    assert set(decision.missing_inputs) == {"recipient", "content_ref"}


def test_low_confidence_model_classification_requires_clarification() -> None:
    decision = ExecutionRouter(confidence_threshold=0.7).decide(
        RoutingRequest(
            session_id="session:1",
            turn_id="turn:1",
            user_text="帮我处理一下这个",
        ),
        capability_snapshot=capabilities(),
        budget_snapshot=budget(),
        model_classification=ModelRouteClassification(
            route="tool_loop",
            confidence=0.42,
            reason_code="ambiguous_object",
            required_capabilities=("job_description_reader",),
        ),
        decision_id="route:2",
        model_decision_id="model:2",
        created_at=NOW,
    )
    assert decision.route == "human_review"
    assert decision.status == "clarification_required"
    assert decision.model_decision_id == "model:2"


def test_disabled_tool_fails_closed_with_specific_missing_capability() -> None:
    decision = ExecutionRouter().decide(
        RoutingRequest(
            session_id="session:1",
            turn_id="turn:1",
            user_text="读取这个 JD：https://jobs.example.test/1",
        ),
        capability_snapshot=capabilities(disabled=("job_description_reader",)),
        budget_snapshot=budget(),
        decision_id="route:3",
        created_at=NOW,
    )
    assert decision.route == "human_review"
    assert decision.reason_code == "required_capability_unavailable"
    assert decision.missing_inputs == ("capability:job_description_reader",)


def test_exhausted_budget_does_not_route_into_tool_or_plan() -> None:
    decision = ExecutionRouter().decide(
        RoutingRequest(
            session_id="session:1",
            turn_id="turn:1",
            user_text="并行读取这三个 JD",
        ),
        capability_snapshot=capabilities(),
        budget_snapshot=budget(stopped=True),
        decision_id="route:4",
        created_at=NOW,
    )
    assert decision.route == "human_review"
    assert decision.reason_code == "budget_unavailable"


def test_router_is_pure_and_hard_rule_does_not_need_model_decision() -> None:
    router = ExecutionRouter()
    assert not hasattr(router, "tool_registry")
    decision = decide("请解释一下岗位职级")
    assert decision.model_decision_id is None
    assert decision.required_capabilities == ()

