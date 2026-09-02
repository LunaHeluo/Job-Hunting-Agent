from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from starter_agent.delegation.dispatcher import (
    Dispatcher,
    DispatcherConfig,
    RunQueueOverloaded,
)
from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, RunOutcome, TaskContract
from starter_agent.delegation.store import RunStoreError, SQLiteRunStore


NOW = datetime(2026, 8, 12, 9, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _limits(value: int = 100) -> BudgetLimits:
    return BudgetLimits(tokens=value, cost_microunits=value, wall_clock_ms=value, model_calls=value, tool_calls=value)


def _parent(run_id: str, *, priority: int = 100, **changes: object) -> ParentRun:
    values = dict(
        id=run_id, session_id=f"session:{run_id}", origin_turn_id=f"turn:{run_id}",
        principal="user:001", coordinator_spec_version="1", runtime_revision="runtime:1",
        priority=priority, available_at=NOW, deadline_at=NOW + timedelta(minutes=10),
        budget_total=_limits(), budget_reserved=_limits(0), budget_consumed=_limits(0),
        route="delegation", created_at=NOW, updated_at=NOW,
    )
    values.update(changes)
    return ParentRun(**values)


def _queue(store: SQLiteRunStore, *, parent_id: str, suffix: str, created_at: datetime = NOW) -> str:
    contract = TaskContract(
        task_id=f"task:{suffix}", parent_run_id=parent_id, specialist_id="job_web_researcher",
        goal="Research jobs", inputs={"query": suffix}, requested_allowed_tools=("search_jobs_serpapi",),
        requested_deadline=NOW + timedelta(minutes=5), requested_budget=_limits(10),
        failure_behavior="allow_partial", idempotency_key=f"delegate:{suffix}",
    )
    run = ChildRun(
        id=f"child:{suffix}", child_task_id=contract.task_id, parent_run_id=parent_id,
        attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline,
        created_at=created_at, updated_at=created_at,
    )
    parent = store.get_parent(parent_id)
    assert parent is not None
    store.create_child_task_and_run(
        contract=contract, child_run=run, specialist_snapshot_id="snapshot:1",
        output_schema_version="1", expected_parent_version=parent.version, created_at=created_at,
    )
    return run.id


def test_claim_next_is_atomic_and_fair_by_priority_available_created_and_id() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:low", priority=200))
    store.create_parent(_parent("parent:high", priority=10))
    _queue(store, parent_id="parent:low", suffix="low")
    _queue(store, parent_id="parent:high", suffix="z", created_at=NOW + timedelta(seconds=1))
    _queue(store, parent_id="parent:high", suffix="a", created_at=NOW + timedelta(seconds=1))
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: NOW + timedelta(seconds=2))

    first = dispatcher.claim_next(worker_id="worker:1")
    second = dispatcher.claim_next(worker_id="worker:2")

    assert first is not None and first.run.id == "child:a"
    assert second is not None and second.run.id == "child:z"
    assert first.run.lease_owner == "worker:1"
    assert first.parent_cancellation_version == 0


def test_queue_capacity_has_stable_backpressure_error_and_high_watermark() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1"))
    _queue(store, parent_id="parent:1", suffix="1")
    dispatcher = Dispatcher(
        store,
        config=DispatcherConfig(queue_high_watermark=1, queue_hard_capacity=1),
        now=lambda: NOW,
    )

    assert dispatcher.queue_pressure().high_watermark_reached is True
    with pytest.raises(RunQueueOverloaded) as captured:
        dispatcher.ensure_capacity()
    assert captured.value.code == "run_queue_overloaded"


def test_parent_cancellation_atomically_blocks_queued_claim_and_preserves_event() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1"))
    _queue(store, parent_id="parent:1", suffix="1")
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: NOW + timedelta(seconds=1))

    parent = dispatcher.cancel_parent("parent:1", reason="user_requested")

    assert parent.status == "cancelled"
    assert parent.cancellation_version == 1
    assert dispatcher.claim_next(worker_id="worker:1") is None
    tree = store.get_run_tree("parent:1")
    assert tree.child_runs[0].status == "cancelled"
    assert any(event.event_type == "parent.cancellation_requested" for event in tree.events)


def test_parent_cancellation_blocks_new_child_creation_atomically() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1"))
    Dispatcher(store, config=DispatcherConfig(), now=lambda: NOW).cancel_parent("parent:1", reason="user_requested")

    with pytest.raises(RunStoreError) as captured:
        _queue(store, parent_id="parent:1", suffix="late")

    assert captured.value.code == "parent_cancelling"


def test_reaper_requeues_retryable_lease_then_terminates_at_attempt_limit() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1"))
    _queue(store, parent_id="parent:1", suffix="1")
    dispatcher = Dispatcher(
        store,
        config=DispatcherConfig(lease_ttl=timedelta(seconds=1), max_attempts=2),
        now=lambda: NOW,
    )
    claimed = dispatcher.claim_next(worker_id="worker:1")
    assert claimed is not None

    recovered = dispatcher.reap_expired(now=NOW + timedelta(seconds=2))
    assert recovered[0].status == "queued"
    claimed_again = dispatcher.claim_next(worker_id="worker:2", now=NOW + timedelta(seconds=3))
    assert claimed_again is not None
    terminated = dispatcher.reap_expired(now=NOW + timedelta(seconds=5))

    assert terminated[0].status == "failed"
    assert terminated[0].error_code == "run_lease_expired"
    events = store.get_run_tree("parent:1").events
    assert any(event.event_type == "child.lease_recovered" for event in events)
    assert any(event.event_type == "child.retry_exhausted" for event in events)


def test_retry_backoff_blocks_reclaim_until_child_available_at() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1"))
    _queue(store, parent_id="parent:1", suffix="1")
    dispatcher = Dispatcher(
        store,
        config=DispatcherConfig(retry_base_delay=timedelta(seconds=4)),
        now=lambda: NOW,
    )
    first = dispatcher.claim_next(worker_id="worker:1")
    assert first is not None

    retried = dispatcher.retry(first, error_code="worker_execution_timeout", now=NOW + timedelta(seconds=1))

    assert retried.attempt == 2
    assert retried.available_at == NOW + timedelta(seconds=5)
    assert dispatcher.claim_next(worker_id="worker:2", now=NOW + timedelta(seconds=4)) is None
    assert dispatcher.claim_next(worker_id="worker:2", now=NOW + timedelta(seconds=5)) is not None


def test_deadline_expired_queued_child_is_timed_out_and_not_claimed() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1"))
    _queue(store, parent_id="parent:1", suffix="1")
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: NOW + timedelta(minutes=6))

    assert dispatcher.claim_next(worker_id="worker:1") is None
    child = store.get_run_tree("parent:1").child_runs[0]
    assert child.status == "timed_out"
    assert child.error_code == "run_deadline_exceeded"


def test_last_terminal_child_wakes_waiting_parent_without_coordinator_logic() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent("parent:1", status="running", started_at=NOW))
    parent = store.get_parent("parent:1")
    assert parent is not None
    store.transition("parent:1", "waiting_children", expected_version=parent.version, occurred_at=NOW)
    _queue(store, parent_id="parent:1", suffix="1")
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: NOW + timedelta(seconds=1))
    claim = dispatcher.claim_next(worker_id="worker:1")
    assert claim is not None

    dispatcher.finish(claim, RunOutcome(disposition="completed", run_id=claim.run.id, status="succeeded", output_ref="output:1"))

    assert store.get_parent("parent:1").status == "queued"
