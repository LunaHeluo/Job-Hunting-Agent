import asyncio
from pathlib import Path

import yaml

from starter_agent.trust.fixture_runtime import execute_fixture_case
from starter_agent.trust.fixtures import JobResearchFixtureLoader
from starter_agent.trust.models import EvalCase


CASE_FILE = Path("evals/job-application-orchestration-cases.yaml")


def cases() -> tuple[EvalCase, ...]:
    payload = yaml.safe_load(CASE_FILE.read_text(encoding="utf-8"))
    return tuple(EvalCase(**item) for item in payload["cases"])


def execute(case: EvalCase):
    root = Path.cwd()
    manifest = JobResearchFixtureLoader(
        root / "evals" / "job-research" / "fixtures"
    ).load_manifest()
    return asyncio.run(execute_fixture_case(case, manifest=manifest, project_root=root))


def test_fixed_orchestration_suite_covers_required_routes_and_boundaries() -> None:
    by_id = {item.id: item for item in cases()}
    required = {
        "orchestration-direct-simple-qa",
        "orchestration-workflow-weekly-report",
        "orchestration-tool-loop-single-jd",
        "orchestration-plan-three-company-match",
        "orchestration-send-email-human-review",
        "orchestration-router-low-confidence",
        "orchestration-validator-tool-disabled",
        "orchestration-validator-cycle",
        "orchestration-validator-duplicate-step",
        "orchestration-validator-unauthorized-action",
        "orchestration-validator-plan-budget",
        "orchestration-verifier-source-url",
        "orchestration-verifier-chunk-id",
        "orchestration-verifier-required-field",
        "orchestration-verifier-business-rule",
        "orchestration-recovery-targeted",
        "orchestration-recovery-limit",
        "orchestration-budget-steps",
        "orchestration-budget-tokens",
        "orchestration-budget-cost",
        "orchestration-budget-time",
        "orchestration-budget-tool-calls",
        "orchestration-model-router-fallback",
        "orchestration-context-trim-preserves-state",
        "orchestration-simple-no-multi-agent",
        "orchestration-foreground-short-task",
        "orchestration-background-batch-task",
        "orchestration-three-jd-parallel",
        "orchestration-serial-input-dependency",
        "orchestration-serial-write-conflict",
        "orchestration-serial-missing-envelope",
        "orchestration-child-isolated-package",
        "orchestration-child-completed-event",
        "orchestration-child-failed-event",
        "orchestration-child-timeout-event",
        "orchestration-child-cancel-event",
        "orchestration-event-idempotency-ordering",
        "orchestration-join-all-required",
        "orchestration-join-partial-allowed",
        "orchestration-join-first-success",
        "orchestration-join-deadline-reached",
        "orchestration-partial-failure-human-review",
        "orchestration-parent-compact-context",
        "orchestration-runtime-verifier-only",
        "orchestration-framework-parity-not-applicable",
    }
    assert required.issubset(by_id)
    assert len(by_id) >= 15
    assert {
        item.expected_outcome["route"] for item in by_id.values()
    } == {"direct", "workflow", "tool_loop", "plan_delegation", "human_review"}
    assert all("orchestration-scenarios-redacted-v1" in item.fixture_ids for item in by_id.values())


def test_orchestration_fixture_is_redacted_schema_checked_and_complete() -> None:
    root = Path.cwd()
    manifest = JobResearchFixtureLoader(
        root / "evals" / "job-research" / "fixtures"
    ).load_manifest()
    fixture = manifest.by_id("orchestration-scenarios-redacted-v1")
    scenario_ids = set(fixture.data["scenarios"])
    case_scenarios = {str(item.input_summary["fixture_state"]) for item in cases()}
    assert case_scenarios.issubset(scenario_ids)
    assert fixture.fixture_type == "orchestration_scenarios"
    assert fixture.record.redaction_summary == {
        "secrets": "none",
        "personal_data": "synthetic_only",
        "source": "deterministic_orchestration_contracts",
    }


def test_budget_join_validation_and_verifier_matrices_are_explicit() -> None:
    actuals = {item.id: execute(item)[0] for item in cases()}
    assert {
        actuals[f"orchestration-budget-{suffix}"]["stop_dimension"]
        for suffix in ("steps", "tokens", "cost", "time", "tool-calls")
    } == {"steps", "tokens", "cost_microunits", "wall_clock_ms", "tool_calls"}
    assert {
        actuals[item]["join_policy"]
        for item in (
            "orchestration-join-all-required",
            "orchestration-join-partial-allowed",
            "orchestration-join-first-success",
            "orchestration-join-deadline-reached",
        )
    } == {"all_required", "partial_allowed", "first_success", "deadline_reached"}
    assert {
        actuals[item]["validation_failure"]
        for item in (
            "orchestration-validator-tool-disabled",
            "orchestration-validator-cycle",
            "orchestration-validator-duplicate-step",
            "orchestration-validator-unauthorized-action",
            "orchestration-validator-plan-budget",
        )
    } == {
        "capability_disabled", "dag_cycle", "duplicate_step_id",
        "approval_required", "plan_budget_exceeds_parent",
    }
    assert {
        actuals[item]["verify_failure"]
        for item in (
            "orchestration-verifier-source-url",
            "orchestration-verifier-chunk-id",
            "orchestration-verifier-required-field",
            "orchestration-verifier-business-rule",
        )
    } == {
        "source_url_missing", "chunk_id_missing", "required_field_missing",
        "application_status_invalid",
    }


def test_orchestration_fixture_actuals_are_offline_stable_and_not_expected_echoes() -> None:
    for case in cases():
        first, first_events = execute(case)
        second, second_events = execute(case)
        assert first == second
        assert first_events == second_events
        assert first["canonical_hash"] == second["canonical_hash"]
        assert first["trace_canonical_hash"] == second["trace_canonical_hash"]
        assert first["network_called"] is False
        assert first["browser_called"] is False
        assert first["provider_called"] is False
        assert first["model_poll_calls"] == 0
        assert first["fixture_execution"] == "offline_orchestration_scenario_adapter"
        assert first["trace_sequence"] == [event["event_type"] for event in first_events]
        if first["route"] in {"direct", "workflow", "tool_loop", "human_review"}:
            assert first["planner_calls"] == 0

    case = next(item for item in cases() if item.id == "orchestration-three-jd-parallel")
    changed = case.model_copy(
        update={"expected_outcome": {"task_success": False, "route": "direct"}}
    )
    assert execute(case) == execute(changed)


def test_runtime_verifier_and_offline_eval_remain_distinct_evidence_paths() -> None:
    source = Path("backend/src/starter_agent/trust/fixture_runtime.py").read_text(encoding="utf-8")
    verifier_source = Path("backend/src/starter_agent/orchestration/verifier.py").read_text(
        encoding="utf-8"
    )
    assert "offline_orchestration_scenario_adapter" in source
    assert "ReleaseGateDecider" not in verifier_source
    assert "EvalRunner" not in verifier_source
