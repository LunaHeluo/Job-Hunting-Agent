import asyncio
from pathlib import Path

import yaml

from starter_agent.trust.fixture_runtime import execute_fixture_case
from starter_agent.trust.fixtures import JobResearchFixtureLoader
from starter_agent.trust.models import EvalCase


CASE_FILES = (
    Path("evals/job-application-delegation-cases.yaml"),
)


def test_fixed_delegation_fixture_suite_covers_task19_terminal_and_safety_matrix() -> None:
    """These cases must stay offline and flow through the existing Trust baseline."""
    ids = {
        item["id"]
        for path in CASE_FILES
        for item in yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]
    }
    required = {
        "delegation-dual-success",
        "delegation-child-failure-partial",
        "delegation-child-timeout",
        "delegation-parent-cancelled",
        "delegation-duplicate-callback-idempotent",
        "delegation-invalid-envelope",
        "delegation-source-conflict",
        "delegation-policy-denied",
        "delegation-budget-exhausted",
        "delegation-single-agent-better",
        "delegation-unique-web-subagent-route",
        "delegation-stable-single-url-direct",
        "delegation-three-jd-multistep-loop",
        "delegation-access-restriction-waits",
        "delegation-tool-schema-partition",
        "delegation-profile-no-evidence",
        "delegation-real-child-runtime",
        "delegation-run-context-isolation",
        "delegation-concurrent-sibling-isolation",
        "delegation-recursive-denied",
        "delegation-specialist-not-found",
        "delegation-specialist-disabled",
        "delegation-specialist-version-mismatch",
        "delegation-coordinator-override-denied",
        "delegation-result-envelope-projection",
        "delegation-child-budget-exhausted",
        "delegation-single-multi-comparison",
    }
    assert required.issubset(ids)


def _delegation_cases() -> dict[str, EvalCase]:
    return {
        item["id"]: EvalCase(**item)
        for item in yaml.safe_load(CASE_FILES[0].read_text(encoding="utf-8"))["cases"]
        if item["id"].startswith("delegation-")
    }


def _execute(case: EvalCase):
    root = Path.cwd()
    manifest = JobResearchFixtureLoader(
        root / "evals" / "job-research" / "fixtures"
    ).load_manifest()
    return asyncio.run(execute_fixture_case(case, manifest=manifest, project_root=root))


def test_delegation_fixture_actuals_are_scenario_driven_not_expected_echoes() -> None:
    case = _delegation_cases()["delegation-invalid-envelope"]
    changed_expected = case.model_copy(
        update={"expected_outcome": {"task_success": True, "route": "conversation"}}
    )

    actual, events = _execute(case)
    changed_actual, changed_events = _execute(changed_expected)

    assert actual == changed_actual
    assert events == changed_events
    assert actual["parent_status"] == "failed"
    assert actual["schema_valid"] is False


def test_delegation_fixture_matrix_has_distinct_terminal_evidence_and_stable_hashes() -> None:
    expected = {
        "delegation-dual-success": ("succeeded", ("succeeded", "succeeded"), None),
        "delegation-child-failure-partial": ("partial", ("succeeded", "failed"), "mcp_unavailable"),
        "delegation-child-timeout": ("timed_out", ("timed_out",), "deadline_exceeded"),
        "delegation-parent-cancelled": ("cancelled", ("cancelled",), "parent_cancelled"),
        "delegation-duplicate-callback-idempotent": ("succeeded", ("succeeded",), None),
        "delegation-invalid-envelope": ("failed", ("failed",), "result_envelope_invalid"),
        "delegation-source-conflict": ("partial", ("succeeded", "failed"), "source_conflict"),
        "delegation-policy-denied": ("failed", ("failed",), "policy_denied"),
        "delegation-budget-exhausted": ("budget_exhausted", ("budget_exhausted",), "budget_exhausted"),
        "delegation-single-agent-better": ("succeeded", (), None),
        "delegation-unique-web-subagent-route": ("succeeded", ("succeeded",), None),
    }

    cases = _delegation_cases()
    for case_id, (parent_status, child_statuses, error_code) in expected.items():
        first, first_events = _execute(cases[case_id])
        second, second_events = _execute(cases[case_id])

        assert first["parent_status"] == parent_status
        assert tuple(first["child_statuses"]) == child_statuses
        assert first["error_code"] == error_code
        assert first["canonical_hash"] == second["canonical_hash"]
        assert first["trace_canonical_hash"] == second["trace_canonical_hash"]
        assert first_events == second_events
        assert first["network_called"] is False
        assert first["browser_called"] is False
        assert first["provider_called"] is False


def test_delegation_fixture_is_redacted_complete_and_uses_real_contract_vocabulary() -> None:
    root = Path.cwd()
    manifest = JobResearchFixtureLoader(
        root / "evals" / "job-research" / "fixtures"
    ).load_manifest()
    fixture = manifest.by_id("delegation-scenarios-redacted-v1")
    scenarios = fixture.data["scenarios"]
    cases = _delegation_cases()

    assert fixture.fixture_type == "delegation_scenarios"
    assert fixture.record.redaction_summary["personal_data"] == "synthetic_only"
    assert len(cases) == len(scenarios) >= 12
    assert {
        item.input_summary["fixture_state"] for item in cases.values()
    } == set(scenarios)
    assert all(item.fixture_ids == ("delegation-scenarios-redacted-v1",) for item in cases.values())


def test_multistep_tool_order_context_isolation_and_comparison_are_deterministic() -> None:
    cases = _delegation_cases()
    multi, events = _execute(cases["delegation-three-jd-multistep-loop"])
    tools = [
        event["summary"]["tool_name"]
        for event in events
        if event["event_type"] == "Tool"
    ]
    assert tools == multi["tool_sequence"]
    assert tools[:5] == [
        "search_jobs_serpapi",
        "browser_navigate",
        "browser_wait_for",
        "browser_click",
        "browser_snapshot",
    ]

    isolated, _ = _execute(cases["delegation-run-context-isolation"])
    assert isolated["shared_runtime_path"] is True
    assert isolated["run_context_distinct"] is True
    assert sum(
        isolated[key]
        for key in (
            "message_pollution_count", "memory_pollution_count",
            "plan_pollution_count", "tool_view_pollution_count",
            "budget_pollution_count", "cancellation_pollution_count",
            "trim_pollution_count", "output_pollution_count",
        )
    ) == 0

    comparison, _ = _execute(cases["delegation-single-multi-comparison"])
    assert comparison["multi_agent_latency_ms"] < comparison["single_agent_latency_ms"]
    assert comparison["multi_agent_token_count"] > comparison["single_agent_token_count"]
    assert comparison["multi_agent_cost_units"] > comparison["single_agent_cost_units"]
    assert comparison["multi_agent_quality_score"] > comparison["single_agent_quality_score"]


def test_safety_and_migration_cases_never_create_forbidden_parallel_paths() -> None:
    cases = _delegation_cases()
    stable, _ = _execute(cases["delegation-stable-single-url-direct"])
    assert stable["route"] == "tool_loop"
    assert stable["delegation_count"] == 0

    unique, _ = _execute(cases["delegation-unique-web-subagent-route"])
    assert unique["delegate_task_calls"] == 1
    assert unique["legacy_workflow_calls"] == unique["direct_browser_calls"] == 0

    recursive, _ = _execute(cases["delegation-recursive-denied"])
    assert recursive["delegate_task_in_child_tools"] is False
    assert recursive["recursive_request_rejected"] is True

    partition, _ = _execute(cases["delegation-tool-schema-partition"])
    assert partition["parent_search_schema_count"] == 0
    assert partition["parent_browser_schema_count"] == 0
    assert partition["parent_raw_rag_schema_count"] == 0
