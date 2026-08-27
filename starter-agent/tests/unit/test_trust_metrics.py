from starter_agent.trust.metrics import ProgrammaticMetricCalculator
from starter_agent.trust.models import EvalAssertionResult, EvalCaseResult


def _case(
    case_id: str,
    status: str,
    *,
    duration_ms: int | None = None,
    total_tokens: int | None = None,
    cost_usd: float | None = None,
) -> EvalCaseResult:
    summary: dict[str, object] = {}
    if duration_ms is not None:
        summary["duration_ms"] = duration_ms
    if total_tokens is not None:
        summary["token_usage"] = {"total_tokens": total_tokens}
    if cost_usd is not None:
        summary["cost_usd"] = cost_usd
    return EvalCaseResult(
        id=f"result-{case_id}",
        run_id="run-1",
        case_id=case_id,
        status=status,
        outcome_summary=summary,
    )


def _assertion(assertion_id: str, status: str) -> EvalAssertionResult:
    return EvalAssertionResult(
        id=f"assertion-{assertion_id}",
        run_id="run-1",
        case_result_id="result-case-a",
        assertion_id=assertion_id,
        status=status,
        expected_summary={"id": assertion_id},
        actual_summary={"id": assertion_id},
    )


def test_programmatic_metrics_define_denominators_and_failure_costs() -> None:
    case_results = [
        _case("case-a", "passed", duration_ms=100, total_tokens=1000, cost_usd=0.10),
        _case("case-b", "failed", duration_ms=300, total_tokens=2000, cost_usd=0.20),
        _case("case-c", "skipped"),
    ]
    assertions = [
        _assertion("tool_call:search", "passed"),
        _assertion("tool_call:rag", "failed"),
        _assertion("citation:resume", "passed"),
        _assertion("approval:once", "passed"),
        _assertion("approval:timeout", "blocked"),
    ]

    metrics = ProgrammaticMetricCalculator().calculate(
        run_id="run-1",
        case_results=case_results,
        assertion_results=assertions,
    )
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["Task Success"].value == 0.5
    assert by_name["Task Success"].numerator == 1
    assert by_name["Task Success"].denominator == 2
    assert by_name["Tool / Argument Accuracy"].value == 0.5
    assert by_name["Citation Correctness"].value == 1.0
    assert by_name["Approval Compliance"].value == 0.5
    assert by_name["Latency P50"].value == 200
    assert by_name["Latency P95"].value == 290
    assert by_name["Total Tokens"].value == 3000
    assert by_name["Cost per Successful Task"].value == 0.30


def test_programmatic_metrics_mark_missing_cost_without_zero_fallback() -> None:
    metrics = ProgrammaticMetricCalculator().calculate(
        run_id="run-1",
        case_results=[_case("case-a", "failed")],
        assertion_results=[],
    )
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["Cost per Successful Task"].value is None
    assert by_name["Cost per Successful Task"].missing is True
    assert by_name["Task Success"].value == 0.0


def test_programmatic_metrics_include_rule_prefixed_assertions() -> None:
    metrics = ProgrammaticMetricCalculator().calculate(
        run_id="run-1",
        case_results=[_case("case-a", "passed")],
        assertion_results=[
            _assertion("rule:tool_call:search_jobs_serpapi", "passed"),
            _assertion("rule:tool_call:browser_snapshot", "failed"),
            _assertion("rule:citation:resume-chunk-001", "passed"),
            _assertion("rule:source_url:expected", "passed"),
        ],
    )
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["Tool / Argument Accuracy"].value == 0.5
    assert by_name["Tool / Argument Accuracy"].denominator == 2
    assert by_name["Citation Correctness"].value == 1.0
    assert by_name["Citation Correctness"].denominator == 2


def test_programmatic_metrics_include_release_required_quality_and_failure_metrics() -> None:
    result = _case("comparison", "passed").model_copy(
        update={
            "outcome_summary": {
                "source_completeness": 0.82,
                "evidence_fidelity": 0.91,
                "failure_complexity": 1.5,
            }
        }
    )
    metrics = ProgrammaticMetricCalculator().calculate(
        run_id="run-1",
        case_results=[result],
        assertion_results=[],
    )
    by_name = {metric.name: metric for metric in metrics}

    assert by_name["Source Completeness"].value == 0.82
    assert by_name["Evidence Fidelity"].value == 0.91
    assert by_name["Failure Complexity"].value == 1.5
    assert by_name["Source Completeness"].missing is False
