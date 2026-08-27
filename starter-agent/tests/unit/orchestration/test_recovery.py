from datetime import UTC, datetime, timedelta

import pytest

from starter_agent.orchestration.budget import OrchestrationBudgetManager
from starter_agent.orchestration.models import (
    BudgetAmounts,
    RecoveryAttempt,
    VerifyFailure,
    VerifyResult,
)
from starter_agent.orchestration.recovery import (
    BoundedRecovery,
    RecoveryPatch,
    RecoveryPolicy,
)


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def amounts(value=10):
    return BudgetAmounts(**{name: value for name in BudgetAmounts.model_fields})


def budget(value=10):
    return OrchestrationBudgetManager().initial_snapshot(
        snapshot_id="budget:before",
        parent_run_id="parent:1",
        limit=amounts(value),
        created_at=NOW,
    )


def failure(rule="citation.complete", path="$.jobs[0].citation", repairable=True):
    return VerifyFailure(
        failure_id="failure:1",
        scope="runtime_output",
        path=path,
        rule_id=rule,
        expected={"source_ref": "present"},
        actual_summary={"source_ref": None},
        severity="error",
        repairable=repairable,
    )


def verify(*failures: VerifyFailure):
    return VerifyResult(
        verify_id="verify:1",
        parent_run_id="parent:1",
        plan_id="plan:1",
        output_ref="artifact:output:1",
        passed=False,
        failures=failures,
        deterministic_result={"citation_failure_count": len(failures)},
        decision="recovery",
        budget_snapshot_id="budget:before",
        created_at=NOW,
    )


def proposal(*, revision=0, prior=(), available_budget=10, risk="low"):
    result = verify(failure())
    decision = BoundedRecovery(policy=RecoveryPolicy(max_attempts=2)).propose(
        result,
        parent_run_id="parent:1",
        revision_count=revision,
        prior_attempts=prior,
        fragment_refs={"failure:1": "artifact:fragment:citation"},
        frozen_item_refs=("artifact:body:passed",),
        budget_snapshot=budget(available_budget),
        recovery_budget=amounts(1),
        deadline_at=NOW + timedelta(minutes=1),
        started_at=NOW,
        risk_level=risk,
    )
    return decision


def test_citation_recovery_receives_only_failed_fragment_and_freezes_body() -> None:
    decision = proposal()
    assert decision.outcome == "execute"
    assert decision.request is not None
    assert decision.request.strategy == "citation_retrieval"
    assert [item.fragment_ref for item in decision.request.targets] == [
        "artifact:fragment:citation"
    ]
    assert decision.attempt is not None
    assert decision.attempt.frozen_item_refs == ("artifact:body:passed",)

    attempt, updated_budget = BoundedRecovery().execute(
        decision,
        repair=lambda _request: RecoveryPatch(
            patch_ref="artifact:patch:1",
            output_ref="artifact:output:2",
            target_paths=("$.jobs[0].citation",),
            usage=amounts(1),
        ),
        budget_snapshot=budget(),
        completed_at=NOW + timedelta(seconds=1),
        budget_snapshot_id="budget:after",
    )
    assert attempt.status == "succeeded"
    assert attempt.patch_ref == "artifact:patch:1"
    assert updated_budget.consumed == amounts(1)


def test_recovery_rejects_full_or_unrelated_rewrite() -> None:
    with pytest.raises(ValueError, match="patch_scope_violation"):
        BoundedRecovery().execute(
            proposal(),
            repair=lambda _request: RecoveryPatch(
                patch_ref="artifact:patch:all",
                output_ref="artifact:output:all",
                target_paths=("$",),
                usage=amounts(1),
            ),
            budget_snapshot=budget(),
            completed_at=NOW,
            budget_snapshot_id="budget:after",
        )


def test_same_failure_limit_budget_and_risk_stop_without_execution() -> None:
    first = proposal().attempt
    assert first is not None
    prior = first.model_copy(update={"status": "failed", "completed_at": NOW})
    assert proposal(revision=1, prior=(prior,)).reason_code == "same_failure_repeated"
    assert proposal(revision=2).reason_code == "recovery_limit_reached"
    assert proposal(available_budget=0).reason_code == "recovery_budget_insufficient"
    assert proposal(risk="high").outcome == "human_review"


def test_non_repairable_failure_never_calls_recovery() -> None:
    result = BoundedRecovery().propose(
        verify(failure(rule="permission.allowed", path="$", repairable=False)),
        parent_run_id="parent:1",
        revision_count=0,
        prior_attempts=(),
        fragment_refs={"failure:1": "artifact:fragment:1"},
        frozen_item_refs=(),
        budget_snapshot=budget(),
        recovery_budget=amounts(1),
        deadline_at=NOW + timedelta(minutes=1),
        started_at=NOW,
    )
    assert result.outcome == "stop"
    assert result.request is None
