from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.delegation.models import BudgetLimits, ParentRun
from starter_agent.delegation.store import CandidateMergeWrite, RevisionConflictError, SQLiteRunStore
from starter_agent.delegation.models import MergeReport


NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _database_url(name: str) -> tuple[str, Path]:
    root = Path(__file__).parents[2] / ".session-only-result-merges" / f"{name}-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{root / 'runs.db'}", root


def _parent() -> ParentRun:
    limit = BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=100, tool_calls=100)
    zero = BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0)
    return ParentRun(id="parent:001", session_id="session:001", origin_turn_id="turn:001", principal="user:001", coordinator_spec_version="v1", runtime_revision="r1", status="created", phase="created", available_at=NOW, deadline_at=NOW + timedelta(minutes=1), budget_total=limit, budget_reserved=zero, budget_consumed=zero, route="delegation", created_at=NOW, updated_at=NOW)


def _write(*, key: str = "job:1", digest: str = "a" * 64, version: int = 0) -> CandidateMergeWrite:
    return CandidateMergeWrite(
        id=f"candidate:{key}:{digest[:8]}", parent_run_id="parent:001", candidate_key=key,
        payload_hash=digest, payload={"job_key": key}, idempotency_key=f"merge:{key}",
        expected_parent_version=version, created_at=NOW,
    )


def test_candidate_staging_replays_identical_write_and_rejects_payload_or_version_conflicts() -> None:
    url, root = _database_url("result-staging")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent())
    first = store.stage_candidate_merge(_write())
    assert store.stage_candidate_merge(_write()) == first
    with pytest.raises(RevisionConflictError, match="merge_conflict"):
        store.stage_candidate_merge(_write(digest="b" * 64))
    with pytest.raises(RevisionConflictError, match="merge_conflict"):
        store.stage_candidate_merge(_write(key="job:2", version=0))
    store.close()


def test_concurrent_candidate_writes_do_not_last_write_win() -> None:
    url, root = _database_url("result-staging-concurrent")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent())
    def attempt(key: str):
        try:
            return store.stage_candidate_merge(_write(key=key))
        except RevisionConflictError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("job:1", "job:2")))
    assert sum(item == "merge_conflict" for item in results) == 1
    assert len([item for item in results if item != "merge_conflict"]) == 1
    store.close()


def test_merge_report_is_persisted_with_hashes_and_cancelled_parent_rejects_staging() -> None:
    url, root = _database_url("result-report")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent())
    report = MergeReport(id="merge:1", parent_run_id="parent:001", result_version=1, input_envelope_refs=("artifact:env:1",), input_hashes=("a" * 64,), accepted=(), rejected=(), dedup_groups=(), missing=(), conflicts=(), source_validation=(), evidence_validation=(), ranking_features={}, deterministic_order=(), semantic_synthesis_version="disabled", final_output_ref="artifact:final:1", final_output_hash="b" * 64, created_at=NOW)
    assert store.save_merge_report(report) == report
    assert store.get_run_tree("parent:001").merge_reports == (report,)
    parent = store.get_parent("parent:001")
    assert parent is not None
    store.request_parent_cancellation("parent:001", reason="user", requested_at=NOW)
    with pytest.raises(RevisionConflictError, match="merge_conflict"):
        store.stage_candidate_merge(_write(version=parent.version + 1))
    store.close()
