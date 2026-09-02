from datetime import UTC, datetime, timedelta

from starter_agent.trust.release_gate import (
    DelegationCandidateGate,
    DelegationReleaseDecisionService,
)
from starter_agent.trust.store import TrustStore


CASE_VERSIONS = {
    "delegation-dual-success": "v4:dual",
    "delegation-single-agent-better": "v4:single-agent",
}


def _report(*, run_id: str, metrics: dict[str, float | None], single_agent_better: bool = False, safety_blocking: bool = False) -> dict:
    return {
        "run_id": run_id,
        "fixture_manifest_hash": "a" * 64,
        "case_versions": CASE_VERSIONS,
        "case_hashes": {
            case_id: f"{index}" * 64
            for index, case_id in enumerate(CASE_VERSIONS, start=1)
        },
        "case_evidence_hashes": {
            case_id: (("c" if run_id.startswith("single") else "d") + str(index)) * 32
            for index, case_id in enumerate(CASE_VERSIONS, start=1)
        },
        "case_results": [
            {
                "case_id": "delegation-single-agent-better",
                "outcome_summary": {
                    "error_code": "single_agent_preferred" if single_agent_better else None,
                },
            }
        ],
        "metrics": {
            name: {"value": value, "missing": value is None}
            for name, value in metrics.items()
        },
        "failure_clusters": [],
        "gate": {"status": "blocked" if safety_blocking else "passed", "safety_blocking": safety_blocking},
    }


def _metric_values(report: dict) -> dict[str, float | None]:
    return {name: item["value"] for name, item in report["metrics"].items()}


def test_delegation_candidate_gate_requires_comparable_quality_gain_and_persists_decision_evidence() -> None:
    baseline = _report(
        run_id="single-v1",
        metrics={
            "Task Success": 0.70,
            "Source Completeness": 0.70,
            "Evidence Fidelity": 0.80,
            "Failure Complexity": 2.0,
            "Latency P95": 100.0,
            "Total Tokens": 1000.0,
            "Cost per Successful Task": 2.0,
        },
    )
    candidate = _report(
        run_id="multi-v1",
        metrics={
            "Task Success": 0.80,
            "Source Completeness": 0.70,
            "Evidence Fidelity": 0.80,
            "Failure Complexity": 1.0,
            "Latency P95": 190.0,
            "Total Tokens": 1300.0,
            "Cost per Successful Task": 2.8,
        },
    )

    decision = DelegationCandidateGate().decide(
        baseline_report=baseline, candidate_report=candidate
    )

    assert decision["status"] == "passed"
    assert decision["default_route_enabled"] is True
    assert decision["route_config"] == {
        "delegated_job_research_enabled": True,
        "legacy_job_research_enabled": False,
    }
    assert decision["quality_improvements"]["Task Success"] == 0.1
    assert decision["cost_ratio"] == 1.4
    assert decision["p95_ratio"] == 1.9
    assert decision["baseline_report_hash"] and decision["candidate_report_hash"]
    assert decision["decision_hash"]

    for degraded in (
        _report(run_id="multi-single-better", metrics=_metric_values(candidate), single_agent_better=True),
        _report(run_id="multi-unknown-cost", metrics={**_metric_values(candidate), "Cost per Successful Task": None}),
        _report(run_id="multi-safety", metrics=_metric_values(candidate), safety_blocking=True),
        _report(run_id="multi-version", metrics=_metric_values(candidate)),
    ):
        if degraded["run_id"] == "multi-version":
            degraded["case_versions"] = {"delegation-dual-success": "v5:different"}
        result = DelegationCandidateGate().decide(
            baseline_report=baseline, candidate_report=degraded
        )
        assert result["status"] == "blocked"
        assert result["default_route_enabled"] is False
        assert result["route_config"]["legacy_job_research_enabled"] is False


def test_delegation_release_decision_is_durable_and_only_enables_current_passing_reports(tmp_path) -> None:
    baseline = _report(run_id="single-persist", metrics={
        "Task Success": 0.7, "Source Completeness": 0.7,
        "Evidence Fidelity": 0.8, "Failure Complexity": 2.0,
        "Latency P95": 100.0, "Total Tokens": 1000.0,
        "Cost per Successful Task": 2.0,
    })
    candidate = _report(run_id="multi-persist", metrics={
        "Task Success": 0.8, "Source Completeness": 0.7,
        "Evidence Fidelity": 0.8, "Failure Complexity": 1.0,
        "Latency P95": 150.0, "Total Tokens": 1200.0,
        "Cost per Successful Task": 2.5,
    })
    now = datetime(2026, 8, 14, tzinfo=UTC)
    store = TrustStore(f"sqlite:///{tmp_path / 'trust.db'}", tmp_path)
    service = DelegationReleaseDecisionService(store)

    decision = service.compare_and_persist(
        baseline_report=baseline, candidate_report=candidate,
        decision_id="delegation-release:v1", created_at=now,
        expires_at=now + timedelta(days=1),
    )

    assert store.get_delegation_release_decision("delegation-release:v1") == decision
    assert service.route_config(
        decision_id="delegation-release:v1", now=now,
        baseline_report_hash=decision["baseline_report_hash"],
        candidate_report_hash=decision["candidate_report_hash"],
    ) == {"delegated_job_research_enabled": True, "legacy_job_research_enabled": False}
    assert service.route_config(
        decision_id="delegation-release:v1", now=now + timedelta(days=2),
        baseline_report_hash=decision["baseline_report_hash"],
        candidate_report_hash=decision["candidate_report_hash"],
    ) == {"delegated_job_research_enabled": False, "legacy_job_research_enabled": False}
