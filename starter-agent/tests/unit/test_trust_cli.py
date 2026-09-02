import json
from pathlib import Path
from uuid import uuid4

from typer.testing import CliRunner

from starter_agent.interfaces.cli import app
from starter_agent.trust.store import TrustStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_ONLY_ROOT = PROJECT_ROOT / ".session-only-trust-cli-tests"


def test_trust_fixture_baseline_cli_writes_report() -> None:
    # The database is isolated per invocation, so a fixed ID proves the CLI
    # carries caller-supplied IDs through to its durable report.
    run_id = "cli-delegation-fixture-v4"
    db_path = SESSION_ONLY_ROOT / uuid4().hex / "agent.db"
    report_dir = SESSION_ONLY_ROOT / "reports"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "trust",
            "fixture-baseline",
            "--project-root",
            str(PROJECT_ROOT),
            "--database-url",
            f"sqlite:///{db_path}",
            "--report-dir",
            str(report_dir),
            "--run-id",
            run_id,
        ],
    )

    assert result.exit_code == 0, result.output
    assert run_id in result.output
    report_path = report_dir / f"{run_id}.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["run_id"] == run_id
    assert report["run_type"] == "fixture"
    assert report["fixture_manifest_hash"]
    case_ids = {item["case_id"] for item in report["case_results"]}
    assert {
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
    }.issubset(case_ids)


def test_trust_delegation_compare_cli_persists_hash_bound_decision(tmp_path) -> None:
    versions = {"delegation-dual-success": "v4"}
    hashes = {"delegation-dual-success": "a" * 64}
    def report(run_id: str, success: float) -> dict:
        values = {
            "Task Success": success, "Source Completeness": 0.8,
            "Evidence Fidelity": 0.8, "Failure Complexity": 1.0,
            "Latency P95": 100.0, "Total Tokens": 1000.0,
            "Cost per Successful Task": 1.0,
        }
        return {
            "run_id": run_id, "fixture_manifest_hash": "b" * 64,
            "case_versions": versions, "case_hashes": hashes,
            "case_results": [], "failure_clusters": [],
            "gate": {"status": "passed", "safety_blocking": False},
            "metrics": {name: {"value": value, "missing": False} for name, value in values.items()},
        }
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(report("baseline", 0.7)), encoding="utf-8")
    candidate_path.write_text(json.dumps(report("candidate", 0.8)), encoding="utf-8")
    db_path = tmp_path / "trust.db"

    result = CliRunner().invoke(app, [
        "trust", "delegation-compare", "--baseline-report", str(baseline_path),
        "--candidate-report", str(candidate_path), "--decision-id", "release:test",
        "--expires-at", "2026-08-15T00:00:00+00:00", "--project-root", str(PROJECT_ROOT),
        "--database-url", f"sqlite:///{db_path}",
    ])

    assert result.exit_code == 0, result.output
    decision = TrustStore(f"sqlite:///{db_path}", PROJECT_ROOT).get_delegation_release_decision("release:test")
    assert decision is not None
    assert decision["status"] == "passed"
    assert decision["baseline_report_hash"] and decision["candidate_report_hash"]


def test_real_smoke_cli_returns_nonzero_for_environment_block(monkeypatch, tmp_path) -> None:
    import starter_agent.interfaces.cli as cli_module

    async def blocked(**_kwargs):
        return {
            "run_id": "smoke:blocked", "status": "blocked",
            "source_url": "https://jobs.example.test/public",
            "report_path": str(tmp_path / "smoke.json"),
        }

    monkeypatch.setattr(cli_module, "run_job_research_real_smoke", blocked)
    result = CliRunner().invoke(app, [
        "trust", "real-smoke", "--run-id", "smoke:blocked",
        "--source-url", "https://jobs.example.test/public",
        "--report-dir", str(tmp_path),
    ])

    assert result.exit_code == 2
    assert "status=blocked" in result.output
