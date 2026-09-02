from datetime import UTC, datetime, timedelta

import pytest

from starter_agent.orchestration.join import JoinChild, JoinEvaluator, JoinPolicyConfig


NOW = datetime(2026, 8, 15, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)


def child(index: int, status: str, *, result=True) -> JoinChild:
    return JoinChild(
        child_run_id=f"child:{index}",
        step_id=f"step:{index}",
        status=status,
        result_envelope_ref=(
            f"artifact:envelope:{index}"
            if result and status in {"succeeded", "partial"}
            else None
        ),
    )


def config(policy: str, **changes: object) -> JoinPolicyConfig:
    values: dict[str, object] = {
        "parent_run_id": "parent:1",
        "plan_id": "plan:1",
        "policy": policy,
        "expected_child_run_ids": ("child:1", "child:2", "child:3"),
        "required_child_run_ids": ("child:1", "child:2"),
        "deadline_at": DEADLINE,
    }
    values.update(changes)
    return JoinPolicyConfig(**values)


def test_all_required_waits_merges_and_fails_explicitly() -> None:
    evaluator = JoinEvaluator()
    waiting = evaluator.evaluate(
        config("all_required"),
        (child(1, "succeeded"), child(2, "running"), child(3, "queued")),
        now=NOW,
        state_version=1,
    )
    assert waiting.next_node == "wait"
    merged = evaluator.evaluate(
        config("all_required"),
        (child(1, "succeeded"), child(2, "succeeded"), child(3, "failed")),
        now=NOW,
        state_version=2,
    )
    assert merged.next_node == "merge"
    assert merged.merge_manifest is not None
    assert merged.merge_manifest.failed == ("child:3",)
    failed = evaluator.evaluate(
        config("all_required"),
        (child(1, "succeeded"), child(2, "failed"), child(3, "cancelled")),
        now=NOW,
        state_version=3,
    )
    assert failed.next_node == "stop"
    assert failed.decision.failed == ("child:2",)
    assert failed.decision.cancelled == ("child:3",)


def test_partial_allowed_uses_minimum_valid_set_and_preserves_failures() -> None:
    result = JoinEvaluator().evaluate(
        config("partial_allowed", minimum_success=2),
        (child(1, "succeeded"), child(2, "partial"), child(3, "timed_out")),
        now=NOW,
        state_version=1,
    )
    assert result.next_node == "merge"
    assert result.decision.partial == ("child:2",)
    assert result.merge_manifest is not None
    assert result.merge_manifest.timed_out == ("child:3",)


def test_first_success_cancels_only_unneeded_pending_children_once() -> None:
    evaluator = JoinEvaluator()
    result = evaluator.evaluate(
        config("first_success"),
        (child(1, "succeeded"), child(2, "running"), child(3, "queued")),
        now=NOW,
        state_version=1,
    )
    assert result.cancel_child_run_ids == ("child:2", "child:3")
    replay = evaluator.evaluate(
        config("first_success"),
        (child(1, "succeeded"), child(2, "cancelled"), child(3, "cancelled")),
        now=NOW + timedelta(seconds=1),
        state_version=2,
        previous_decision=result.decision,
    )
    assert replay.decision == result.decision
    assert replay.cancel_child_run_ids == ()


def test_deadline_policy_waits_then_merges_partial_or_escalates() -> None:
    evaluator = JoinEvaluator()
    waiting = evaluator.evaluate(
        config("deadline_reached"),
        (child(1, "partial"), child(2, "running")),
        now=NOW,
        state_version=1,
    )
    assert waiting.next_node == "wait"
    merged = evaluator.evaluate(
        config("deadline_reached"),
        (child(1, "partial"), child(2, "timed_out")),
        now=DEADLINE,
        state_version=2,
    )
    assert merged.next_node == "merge"
    assert "child:3" in merged.decision.missing
    human = evaluator.evaluate(
        config("deadline_reached", unsatisfied_action="human_review"),
        (child(1, "failed"), child(2, "cancelled"), child(3, "timed_out")),
        now=DEADLINE,
        state_version=3,
    )
    assert human.next_node == "human_review"


@pytest.mark.parametrize("status", ["failed", "timed_out", "cancelled"])
def test_partial_policy_reports_each_terminal_failure(status: str) -> None:
    result = JoinEvaluator().evaluate(
        config("partial_allowed", minimum_success=3),
        (child(1, "succeeded"), child(2, status), child(3, "partial")),
        now=DEADLINE,
        state_version=1,
    )
    assert result.next_node == "stop"
    assert "child:2" in (
        result.decision.failed + result.decision.timed_out + result.decision.cancelled
    )
