from datetime import UTC, datetime

from starter_agent.orchestration.budget import OrchestrationBudgetManager
from starter_agent.orchestration.models import (
    BudgetAmounts,
    JudgeResultSummary,
    ModelCandidate,
    ModelDecision,
    ModelRequirements,
)
from starter_agent.orchestration.verifier import (
    BusinessRuleFailure,
    CitationClaim,
    RuntimeVerifier,
    RuntimeVerifyRequest,
)


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def amounts(value=100):
    return BudgetAmounts(**{name: value for name in BudgetAmounts.model_fields})


def budget():
    return OrchestrationBudgetManager().initial_snapshot(
        snapshot_id="budget:1",
        parent_run_id="parent:1",
        limit=amounts(),
        created_at=NOW,
    )


def request(**changes: object) -> RuntimeVerifyRequest:
    values: dict[str, object] = {
        "parent_run_id": "parent:1",
        "plan_id": "plan:1",
        "output_ref": "artifact:merged:1",
        "output": {"jobs": [{"title": "Agent Engineer"}]},
        "output_schema": {
            "type": "object",
            "required": ["jobs"],
            "properties": {"jobs": {"type": "array"}},
        },
        "permission_allowed": True,
        "source_refs": ("source:1",),
        "authorized_source_refs": ("source:1",),
        "citation_claims": (CitationClaim(path="$.jobs[0]", source_ref="source:1"),),
        "budget_snapshot": budget(),
    }
    values.update(changes)
    return RuntimeVerifyRequest(**values)


def model_decision() -> ModelDecision:
    candidate = ModelCandidate(
        provider="configured-provider",
        model="configured-judge",
        capabilities=("semantic_judge",),
        cost_estimate_microunits=1,
        latency_class="standard",
        health="healthy",
    )
    return ModelDecision(
        model_decision_id="model-decision:1",
        parent_run_id="parent:1",
        purpose="judge",
        requirements=ModelRequirements(
            capabilities=("semantic_judge",),
            complexity="bounded",
            latency_class="standard",
            context_tokens=100,
            risk_policy="deterministic_first",
        ),
        candidates=(candidate,),
        selected_provider=candidate.provider,
        selected_model=candidate.model,
        reason_code="configured_candidate_selected",
        reason_summary="only configured healthy candidate",
        config_revision="test",
        status="selected",
        created_at=NOW,
    )


def test_deterministic_verifier_passes_without_optional_judge() -> None:
    result = RuntimeVerifier().verify(
        request(product_rubric={"clarity": "good"}), created_at=NOW
    )
    assert result.passed
    assert result.decision == "end"
    assert result.judge_result is None
    assert "product_rubric_skipped" in result.verified_items


def test_schema_business_source_citation_and_budget_failures_are_specific() -> None:
    stopped = budget().model_copy(
        update={"phase": "stopped", "stop_dimension": "tokens"}
    )
    result = RuntimeVerifier().verify(
        request(
            output={"jobs": "wrong"},
            permission_allowed=False,
            permission_decision_ref="gate:deny:1",
            source_refs=("source:unauthorized",),
            authorized_source_refs=("source:allowed",),
            citation_claims=(CitationClaim(path="$.jobs[0]", source_ref=None),),
            business_rule_failures=(
                BusinessRuleFailure(
                    rule_id="jobs.non_empty",
                    path="$.jobs",
                    expected={"min": 1},
                    actual_summary={"count": 0},
                ),
            ),
            budget_snapshot=stopped,
        ),
        created_at=NOW,
    )
    codes = {item.rule_id for item in result.failures}
    assert {
        "permission.allowed",
        "schema.valid",
        "jobs.non_empty",
        "source.authorized",
        "citation.complete",
        "budget.consistent",
    }.issubset(codes)
    assert result.decision == "human_review"


def test_judge_cannot_override_deterministic_failure() -> None:
    called = 0

    def judge(_rubric, _output):
        nonlocal called
        called += 1
        return JudgeResultSummary(passed=True, reason_summary="pass")

    result = RuntimeVerifier().verify(
        request(
            output={},
            product_rubric={"clarity": "good"},
        ),
        created_at=NOW,
        judge=judge,
        judge_model_decision=model_decision(),
    )
    assert not result.passed
    assert called == 0
    assert result.judge_result is None


def test_optional_judge_only_adds_semantic_failure() -> None:
    result = RuntimeVerifier().verify(
        request(product_rubric={"clarity": "good"}),
        created_at=NOW,
        judge=lambda _rubric, _output: JudgeResultSummary(
            passed=False,
            rubric_scores={"clarity": 0.2},
            reason_summary="unclear",
        ),
        judge_model_decision=model_decision(),
    )
    assert result.decision == "recovery"
    assert [item.rule_id for item in result.failures] == [
        "rubric.semantic_quality"
    ]
    assert result.judge_model_decision_id == "model-decision:1"


def test_runtime_verifier_has_no_release_gate_authority() -> None:
    verifier = RuntimeVerifier()
    assert not hasattr(verifier, "compare_versions")
    assert not hasattr(verifier, "publish")
    assert not hasattr(verifier, "release")
