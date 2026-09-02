from pathlib import Path
from uuid import uuid4

from starter_agent.trust.baseline import run_job_research_fixture_baseline
from starter_agent.trust.store import TrustStore
from starter_agent.knowledge.routing import (
    KnowledgeRequestDecision,
    KnowledgeRequestRoute,
    KnowledgeRequestRouter,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_ONLY_ROOT = PROJECT_ROOT / ".session-only-trust-baseline-tests"


def _store(name: str) -> TrustStore:
    db_path = SESSION_ONLY_ROOT / uuid4().hex / name / "agent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return TrustStore(f"sqlite:///{db_path}", PROJECT_ROOT)


def test_fixture_baseline_runs_are_repeatable_and_reported() -> None:
    store = _store("repeatable")

    first = run_job_research_fixture_baseline(
        store=store,
        project_root=PROJECT_ROOT,
        run_id="fixture-baseline-a",
        report_dir=SESSION_ONLY_ROOT / "reports",
    )
    second = run_job_research_fixture_baseline(
        store=store,
        project_root=PROJECT_ROOT,
        run_id="fixture-baseline-b",
        report_dir=SESSION_ONLY_ROOT / "reports",
    )

    assert first["comparable_signature"] == second["comparable_signature"]
    assert first["run_id"] == "fixture-baseline-a"
    assert second["run_id"] == "fixture-baseline-b"
    assert first["case_count"] >= 12
    assert set(first["case_versions"]) == set(first["case_hashes"])
    assert len(first["case_hashes"]) == first["case_count"]
    assert first["case_versions"] == second["case_versions"]
    assert first["case_hashes"] == second["case_hashes"]
    assert first["metrics"]["Task Success"]["denominator"] == first["case_count"]
    assert first["gate"]["status"] == "passed"
    assert first["trace_count"] >= first["case_count"]
    assert Path(first["report_path"]).exists()
    layers = {
        store.get_case(item.case_id).layer
        for item in store.list_case_results(run_id=first["run_id"])
    }
    assert {
        "happy_path",
        "edge_case",
        "missing_information",
        "conflicting_context",
        "safety_adversarial",
        "concurrency",
    }.issubset(layers)
    assert {
        "routing",
        "plan_validation",
        "verification",
        "recovery",
        "budget",
        "model_routing",
        "context",
        "task_mode",
        "delegation",
        "task_events",
        "join",
        "verifier_boundary",
        "framework_boundary",
    }.issubset(layers)


def test_known_failure_cluster_blocks_gate_and_resolved_run_reruns_all_cases() -> None:
    store = _store("known-failure")

    blocked = run_job_research_fixture_baseline(
        store=store,
        project_root=PROJECT_ROOT,
        run_id="fixture-known-failure",
        report_dir=SESSION_ONLY_ROOT / "reports",
        known_failure_case_id="JR-INJECTION-WEB-001",
    )
    resolved = run_job_research_fixture_baseline(
        store=store,
        project_root=PROJECT_ROOT,
        run_id="fixture-known-failure-resolved",
        report_dir=SESSION_ONLY_ROOT / "reports",
    )

    assert blocked["gate"]["status"] == "blocked"
    assert blocked["gate"]["safety_blocking"] is True
    assert blocked["failure_clusters"]
    assert resolved["gate"]["status"] == "passed"
    assert resolved["case_count"] == blocked["case_count"]
    assert resolved["comparison_to"] == "fixture-known-failure"


def test_fixture_baseline_fails_when_production_router_regresses(monkeypatch) -> None:
    async def broken_route(self, text, *, provider, model):
        del self, text, provider, model
        return KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.KNOWLEDGE_QUERY,
            reason_code="regression",
        )

    monkeypatch.setattr(KnowledgeRequestRouter, "route", broken_route)
    store = _store("router-regression")

    report = run_job_research_fixture_baseline(
        store=store,
        project_root=PROJECT_ROOT,
        run_id="fixture-router-regression",
        report_dir=SESSION_ONLY_ROOT / "reports",
    )

    result = next(
        item
        for item in store.list_case_results(run_id=report["run_id"])
        if item.case_id == "JR-ROUTE-FLEXIBLE-001"
    )
    assert result.status == "blocked"
    assert report["gate"]["status"] == "blocked"


def test_fixture_baseline_records_actual_component_execution() -> None:
    store = _store("component-evidence")
    report = run_job_research_fixture_baseline(
        store=store,
        project_root=PROJECT_ROOT,
        run_id="fixture-component-evidence",
        report_dir=SESSION_ONLY_ROOT / "reports",
    )

    traces = store.list_trace_events(eval_run_id=report["run_id"])
    executed = {
        event.summary.get("production_component")
        for event in traces
        if event.summary.get("execution_mode") == "fixture_production_replay"
    }
    assert {
        "KnowledgeRequestRouter",
        "KnowledgeJobMatcher",
        "rank_job_candidates",
        "PreToolCallGate",
        "JobResearchOrchestrator",
    }.issubset(executed)
