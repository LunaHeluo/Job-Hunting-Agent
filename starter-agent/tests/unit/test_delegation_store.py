from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from starter_agent.delegation.budget import BudgetLedgerError
from starter_agent.delegation.models import (
    BudgetLimits,
    BudgetUsage,
    ChildRun,
    MergeReport,
    ParentRun,
    TaskContract,
)
from starter_agent.delegation.store import (
    ArtifactLink,
    IdempotencyPayloadConflictError,
    OutboxMessage,
    RecordAlreadyExistsError,
    RevisionConflictError,
    RunEvent,
    RunStoreError,
    SQLiteRunStore,
    ResultRepairAttempt,
)


NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=10)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_ONLY_ROOT = Path("tests/.session-only-delegation-store-tests")


def _limits(value: int = 100, **changes: int) -> BudgetLimits:
    values = {
        "tokens": value,
        "cost_microunits": value,
        "wall_clock_ms": value,
        "model_calls": value,
        "tool_calls": value,
    }
    values.update(changes)
    return BudgetLimits(**values)


def _zero() -> BudgetLimits:
    return _limits(0)


def _parent(parent_id: str = "parent:001", **changes: object) -> ParentRun:
    values: dict[str, object] = {
        "id": parent_id,
        "session_id": "session:001",
        "origin_turn_id": "turn:001",
        "principal": "user:001",
        "coordinator_spec_version": "coordinator-v1",
        "runtime_revision": "runtime-v1",
        "status": "created",
        "phase": "created",
        "version": 0,
        "priority": 100,
        "available_at": NOW,
        "deadline_at": DEADLINE,
        "budget_total": _limits(),
        "budget_reserved": _zero(),
        "budget_consumed": _zero(),
        "route": "delegation",
        "legacy_path_used": False,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return ParentRun(**values)


def _contract(
    *,
    parent_id: str = "parent:001",
    task_id: str = "task:001",
    request: int = 10,
    **changes: object,
) -> TaskContract:
    values: dict[str, object] = {
        "task_id": task_id,
        "parent_run_id": parent_id,
        "specialist_id": "job_web_researcher",
        "goal": "Research an Agent engineer role",
        "inputs": {"query": "Agent engineer Sydney"},
        "constraints": {"max_pages": 3},
        "requested_allowed_tools": ("search_jobs_serpapi",),
        "requested_deadline": DEADLINE,
        "requested_budget": _limits(request),
        "failure_behavior": "allow_partial",
        "idempotency_key": f"delegate:{parent_id}:{task_id}",
    }
    values.update(changes)
    return TaskContract(**values)


def _child_run(
    *,
    parent_id: str = "parent:001",
    task_id: str = "task:001",
    run_id: str = "child-run:001",
) -> ChildRun:
    return ChildRun(
        id=run_id,
        child_task_id=task_id,
        parent_run_id=parent_id,
        attempt=1,
        status="created",
        phase="created",
        version=0,
        deadline_at=DEADLINE,
        created_at=NOW,
        updated_at=NOW,
    )


def _database_url(name: str) -> tuple[str, Path]:
    directory = SESSION_ONLY_ROOT / f"{name}-{uuid4()}"
    directory.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{directory / 'runs.db'}", PROJECT_ROOT


def _create_child(
    store: SQLiteRunStore,
    contract: TaskContract,
    child_run: ChildRun,
    *,
    expected_parent_version: int,
    specialist_snapshot_id: str = "specialist-snapshot:001",
):
    return store.create_child_task_and_run(
        contract=contract,
        child_run=child_run,
        specialist_snapshot_id=specialist_snapshot_id,
        output_schema_version="job-web-output-v1",
        expected_parent_version=expected_parent_version,
        created_at=NOW,
    )


def _merge_report() -> MergeReport:
    return MergeReport(
        id="merge:001",
        parent_run_id="parent:001",
        result_version=1,
        input_envelope_refs=("envelope:001",),
        input_hashes=("1" * 64,),
        accepted=({"task_id": "task:001"},),
        rejected=(),
        dedup_groups=(),
        missing=(),
        conflicts=(),
        source_validation=(),
        evidence_validation=(),
        ranking_features={"quality": 1},
        deterministic_order=("task:001",),
        semantic_synthesis_version="disabled",
        final_output_ref="artifact:final:001",
        final_output_hash="2" * 64,
        created_at=NOW,
    )


def test_store_recovers_full_run_tree_events_artifacts_merge_and_outbox() -> None:
    url, project_root = _database_url("recovery")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    creation = _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    event = RunEvent(
        id="event:001",
        parent_run_id="parent:001",
        child_run_id="child-run:001",
        event_type="child.created",
        status="completed",
        occurred_at=NOW,
        payload={"task_id": "task:001"},
    )
    artifact = ArtifactLink(
        id="artifact-link:001",
        parent_run_id="parent:001",
        child_run_id="child-run:001",
        artifact_ref="artifact:browser:001",
        kind="browser_snapshot",
        restricted=True,
        created_at=NOW,
    )
    report = _merge_report()
    outbox = OutboxMessage(
        id="outbox:001",
        topic="chat.backfill",
        aggregate_id="parent:001",
        idempotency_key="backfill:parent:001:v1",
        payload={"parent_run_id": "parent:001", "result_version": 1},
        created_at=NOW,
    )
    store.append_event(event)
    store.link_artifact(artifact)
    store.save_merge_report(report)
    store.enqueue_outbox(outbox)
    store.close()

    reopened = SQLiteRunStore(url, project_root)
    tree = reopened.get_run_tree("parent:001")
    assert creation.parent.version == 1
    assert tree.parent.budget_reserved == _limits(10)
    assert tree.child_tasks == (creation.task,)
    assert tree.child_runs == (creation.run,)
    assert len(tree.allocations) == 5
    assert [item.event_type for item in tree.events] == ["budget.reserved", "child.created"]
    assert tree.events[1] == event.model_copy(update={"event_seq": 2})
    assert tree.artifact_links == (artifact,)
    assert tree.merge_reports == (report,)
    assert reopened.list_outbox(limit=10).items == (outbox,)
    reopened.close()


@pytest.mark.parametrize(
    "dimension", ["tokens", "cost_microunits", "wall_clock_ms", "model_calls", "tool_calls"]
)
def test_child_creation_rolls_back_when_any_budget_dimension_is_insufficient(
    dimension: str,
) -> None:
    url, project_root = _database_url(f"insufficient-{dimension}")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    requested = _limits(1).model_copy(update={dimension: 101})
    contract = _contract(request=1, requested_budget=requested)
    with pytest.raises(BudgetLedgerError) as captured:
        _create_child(store, contract, _child_run(), expected_parent_version=0)
    assert captured.value.dimension == dimension
    tree = store.get_run_tree("parent:001")
    assert tree.parent.version == 0
    assert tree.parent.budget_reserved == _zero()
    assert tree.child_tasks == ()
    assert tree.child_runs == ()
    assert tree.allocations == ()
    store.close()


def test_stale_parent_version_cannot_reserve_budget_or_create_second_child() -> None:
    url, project_root = _database_url("concurrency")
    first = SQLiteRunStore(url, project_root)
    second = SQLiteRunStore(url, project_root)
    first.create_parent(_parent())
    barrier = Barrier(2)

    def attempt(store: SQLiteRunStore, suffix: str) -> str:
        contract = _contract(task_id=f"task:{suffix}", request=60)
        child = _child_run(task_id=f"task:{suffix}", run_id=f"child-run:{suffix}")
        barrier.wait()
        try:
            _create_child(store, contract, child, expected_parent_version=0)
            return "created"
        except RevisionConflictError:
            return "revision_conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda args: attempt(*args), ((first, "a"), (second, "b"))))
    assert sorted(results) == ["created", "revision_conflict"]
    tree = first.get_run_tree("parent:001")
    assert tree.parent.version == 1
    assert tree.parent.budget_reserved == _limits(60)
    assert len(tree.child_tasks) == 1
    assert len(tree.child_runs) == 1
    first.close()
    second.close()


def test_task_idempotency_key_is_scoped_to_parent() -> None:
    url, project_root = _database_url("parent-idempotency-scope")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent("parent:a"))
    store.create_parent(_parent("parent:b"))
    shared_key = "delegate:shared"
    first = _create_child(
        store,
        _contract(parent_id="parent:a", task_id="task:a", idempotency_key=shared_key),
        _child_run(parent_id="parent:a", task_id="task:a", run_id="child-run:a"),
        expected_parent_version=0,
    )
    second = _create_child(
        store,
        _contract(parent_id="parent:b", task_id="task:b", idempotency_key=shared_key),
        _child_run(parent_id="parent:b", task_id="task:b", run_id="child-run:b"),
        expected_parent_version=0,
    )
    assert first.task.parent_run_id == "parent:a"
    assert second.task.parent_run_id == "parent:b"
    store.close()


def test_internal_accept_child_result_cas_accepts_only_one_result_with_expected_version() -> None:
    url, project_root = _database_url("accepted-result")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    creation = _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    claimed = store.claim_child_run(
        "child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=1,
        claimed_at=NOW + timedelta(seconds=2), lease_ttl=timedelta(seconds=30),
    )
    assert claimed is not None
    store.complete_child_run(
        "child-run:001", target_status="succeeded", result_envelope_ref="artifact:envelope:001",
        result_hash="a" * 64, worker_id="worker:a", lease_token="lease:a",
        expected_version=claimed.version, completed_at=NOW + timedelta(seconds=3),
    )
    accepted = store._accept_child_result(
        "child-run:001", result_envelope_ref="artifact:envelope:001", result_hash="a" * 64,
        expected_task_version=creation.task.version, accepted_at=NOW + timedelta(seconds=1),
    )
    assert accepted.accepted_child_run_id == "child-run:001"
    assert accepted.accepted_result_hash == "a" * 64
    with pytest.raises(RevisionConflictError):
        store._accept_child_result(
            "child-run:001", result_envelope_ref="artifact:envelope:002", result_hash="b" * 64,
            expected_task_version=creation.task.version, accepted_at=NOW + timedelta(seconds=2),
        )
    store.close()


def test_parent_cancellation_finishes_immediately_when_every_child_is_terminal() -> None:
    url, project_root = _database_url("cancel-terminal-children")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    creation = _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    queued = store.transition(
        creation.run.id, "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1)
    )
    claimed = store.claim_child_run(
        queued.id,
        worker_id="worker:a",
        lease_token="lease:a",
        expected_version=queued.version,
        claimed_at=NOW + timedelta(seconds=2),
        lease_ttl=timedelta(seconds=30),
    )
    assert claimed is not None
    store.complete_child_run(
        claimed.id,
        target_status="partial",
        result_envelope_ref="artifact:partial",
        result_hash="a" * 64,
        worker_id="worker:a",
        lease_token="lease:a",
        expected_version=claimed.version,
        completed_at=NOW + timedelta(seconds=3),
    )
    parent = store.get_parent(creation.parent.id)
    assert parent is not None

    cancelled = store.request_parent_cancellation(
        parent.id,
        reason="user",
        requested_at=NOW + timedelta(seconds=4),
        expected_version=parent.version,
        idempotency_key="cancel-terminal-children",
    )

    assert cancelled.status == "cancelled"
    assert cancelled.phase == "children_terminal"
    assert cancelled.cancelled_at == NOW + timedelta(seconds=4)
    store.close()


def test_result_repair_attempt_is_audited_once_per_child() -> None:
    url, project_root = _database_url("repair-attempt")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    usage = BudgetUsage(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=0, cost_status="actual", price_version="p1", usage_source="provider")
    attempt = ResultRepairAttempt(child_run_id="child-run:001", envelope_hash="a" * 64, attempt=1, error_code="result_schema_invalid", usage=usage, expected_parent_version=1, occurred_at=NOW)
    assert store.record_result_repair_attempt(attempt) == attempt
    assert store.record_result_repair_attempt(attempt) == attempt
    with pytest.raises(RevisionConflictError, match="repair already"):
        store.record_result_repair_attempt(attempt.model_copy(update={"envelope_hash": "b" * 64}))
    assert [event.event_type for event in store.list_events("parent:001").items] == ["budget.reserved", "budget.consumed_released", "result.repair_requested"]
    store.close()


def test_recover_pending_result_repairs_marks_stale_attempt_abandoned_without_releasing_reservation() -> None:
    url, project_root = _database_url("repair-reaper")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent(status="running", phase="validating", started_at=NOW))
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW)
    claimed = store.claim_child_run("child-run:001", worker_id="worker:1", lease_token="lease:1", expected_version=1, claimed_at=NOW, lease_ttl=timedelta(seconds=30))
    store.begin_result_repair_attempt(child_run_id="child-run:001", envelope_hash="a" * 64, expected_parent_version=store.get_parent("parent:001").version)
    reaped_at = datetime.now(UTC) + timedelta(minutes=2)
    assert store.recover_pending_result_repairs(now=reaped_at, stale_after=timedelta(seconds=1)) == ("child-run:001",)
    assert store.recover_pending_result_repairs(now=reaped_at, stale_after=timedelta(seconds=1)) == ()
    with pytest.raises(RevisionConflictError, match="already attempted"):
        store.begin_result_repair_attempt(child_run_id="child-run:001", envelope_hash="a" * 64, expected_parent_version=store.get_parent("parent:001").version)
    store.close()


def test_transition_uses_expected_version_and_persists_event() -> None:
    url, project_root = _database_url("transition")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    queued = store.transition("parent:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    assert queued.status == "queued"
    assert queued.version == 1
    with pytest.raises(RevisionConflictError):
        store.transition("parent:001", "running", expected_version=0, occurred_at=NOW + timedelta(seconds=2))
    tree = store.get_run_tree("parent:001")
    assert [event.event_type for event in tree.events] == ["run.status_changed"]
    store.close()


def test_outbox_is_idempotent_and_rejects_same_key_with_different_payload() -> None:
    url, project_root = _database_url("outbox")
    store = SQLiteRunStore(url, project_root)
    message = OutboxMessage(
        id="outbox:001", topic="chat.backfill", aggregate_id="parent:001",
        idempotency_key="backfill:parent:001:v1", payload={"result_version": 1}, created_at=NOW,
    )
    assert store.enqueue_outbox(message) == message
    assert store.enqueue_outbox(message) == message
    changed = OutboxMessage.model_validate({**message.model_dump(mode="python"), "id": "outbox:002", "payload": {"result_version": 2}})
    with pytest.raises(IdempotencyPayloadConflictError) as captured:
        store.enqueue_outbox(changed)
    assert captured.value.code == "idempotency_payload_conflict"
    assert len(store.list_outbox(limit=10).items) == 1
    wrong_aggregate = OutboxMessage.model_validate({**message.model_dump(mode="python"), "id": "outbox:003", "aggregate_id": "parent:other"})
    with pytest.raises(IdempotencyPayloadConflictError):
        store.enqueue_outbox(wrong_aggregate)
    store.close()


def test_failed_child_insert_rolls_back_parent_budget_task_and_allocations() -> None:
    url, project_root = _database_url("rollback")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    contract = _contract(task_id="task:002")
    duplicate_run = _child_run(task_id="task:002", run_id="child-run:001")
    with pytest.raises(RunStoreError):
        _create_child(store, contract, duplicate_run, expected_parent_version=1)
    tree = store.get_run_tree("parent:001")
    assert tree.parent.version == 1
    assert tree.parent.budget_reserved == _limits(10)
    assert [task.id for task in tree.child_tasks] == ["task:001"]
    assert len(tree.allocations) == 5
    store.close()


def test_settlement_persists_consumption_and_releases_unused_budget() -> None:
    url, project_root = _database_url("settlement")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(request=10), _child_run(), expected_parent_version=0)
    usage = BudgetUsage(
        tokens=6, cost_microunits=6, wall_clock_ms=6, model_calls=6, tool_calls=6,
        cost_status="actual", usage_source="provider",
    )
    settlement = store.settle_child_budget(
        "child-run:001", usage, expected_parent_version=1, settled_at=NOW + timedelta(seconds=1)
    )
    assert settlement.parent.version == 2
    assert settlement.parent.budget_reserved == _limits(6)
    assert settlement.parent.budget_consumed == _limits(6)
    assert all(item.consumed == 6 for item in settlement.allocations)
    assert all(item.released == 4 for item in settlement.allocations)
    reopened = SQLiteRunStore(url, project_root)
    tree = reopened.get_run_tree("parent:001")
    assert all(item.consumed == 6 and item.released == 4 for item in tree.allocations)
    reopened.close()
    store.close()


def test_event_and_artifact_reject_child_owned_by_another_parent() -> None:
    url, project_root = _database_url("ownership")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent("parent:a"))
    store.create_parent(_parent("parent:b"))
    _create_child(store, _contract(parent_id="parent:b"), _child_run(parent_id="parent:b"), expected_parent_version=0)
    with pytest.raises(RunStoreError, match="does not belong"):
        store.append_event(RunEvent(id="event:cross-parent", parent_run_id="parent:a", child_run_id="child-run:001", event_type="child.observed", status="completed", occurred_at=NOW))
    with pytest.raises(RunStoreError, match="does not belong"):
        store.link_artifact(ArtifactLink(id="artifact:cross-parent", parent_run_id="parent:a", child_run_id="child-run:001", artifact_ref="artifact:restricted", kind="browser_snapshot", created_at=NOW))
    store.close()


def test_event_sequence_is_monotonic_and_supports_incremental_cursor() -> None:
    url, project_root = _database_url("event-sequence")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    later = store.append_event(RunEvent(id="event:later-clock", parent_run_id="parent:001", event_type="later", status="completed", occurred_at=NOW + timedelta(seconds=10)))
    earlier = store.append_event(RunEvent(id="event:earlier-clock", parent_run_id="parent:001", event_type="earlier", status="completed", occurred_at=NOW))
    assert (later.event_seq, earlier.event_seq) == (1, 2)
    assert store.list_events("parent:001", after_seq=1).items == (earlier,)
    store.close()


def test_event_retry_without_store_assigned_sequence_is_idempotent() -> None:
    url, project_root = _database_url("event-idempotency")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    event = RunEvent(id="event:retry", parent_run_id="parent:001", event_type="child.created", status="completed", occurred_at=NOW, payload={"child_run_id": "child-run:001"})
    first = store.append_event(event)
    retried = store.append_event(event)
    assert retried == first
    assert first.event_seq == 1
    assert store.list_events("parent:001").items == (first,)
    store.close()


def test_two_workers_cannot_claim_same_child_and_lease_survives_restart() -> None:
    url, project_root = _database_url("lease-competition")
    first = SQLiteRunStore(url, project_root)
    second = SQLiteRunStore(url, project_root)
    first.create_parent(_parent())
    _create_child(first, _contract(), _child_run(), expected_parent_version=0)
    first.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    barrier = Barrier(2)

    def claim(store: SQLiteRunStore, worker: str):
        barrier.wait()
        return store.claim_child_run("child-run:001", worker_id=f"worker:{worker}", lease_token=f"lease:{worker}", expected_version=1, claimed_at=NOW + timedelta(seconds=2), lease_ttl=timedelta(seconds=30))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(lambda args: claim(*args), ((first, "a"), (second, "b"))))
    winners = [item for item in claims if item is not None]
    assert len(winners) == 1
    first.close()
    second.close()
    reopened = SQLiteRunStore(url, project_root)
    persisted = reopened.get_run_tree("parent:001").child_runs[0]
    assert persisted.lease_token == winners[0].lease_token
    with pytest.raises(RevisionConflictError):
        reopened.heartbeat_child_run("child-run:001", worker_id="wrong-worker", lease_token="wrong-token", expected_version=persisted.version, heartbeat_at=NOW + timedelta(seconds=3), lease_ttl=timedelta(seconds=30))
    heartbeat = reopened.heartbeat_child_run("child-run:001", worker_id=persisted.lease_owner, lease_token=persisted.lease_token, expected_version=persisted.version, heartbeat_at=NOW + timedelta(seconds=3), lease_ttl=timedelta(seconds=30))
    assert heartbeat.version == persisted.version + 1
    assert heartbeat.heartbeat_at == NOW + timedelta(seconds=3)
    reopened.close()


def test_claim_rejects_deadline_and_heartbeat_cannot_move_backwards() -> None:
    url, project_root = _database_url("lease-time")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    assert store.claim_child_run("child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=1, claimed_at=DEADLINE, lease_ttl=timedelta(seconds=30)) is None
    claimed = store.claim_child_run("child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=1, claimed_at=NOW + timedelta(seconds=2), lease_ttl=timedelta(seconds=30))
    assert claimed is not None
    with pytest.raises(RevisionConflictError):
        store.heartbeat_child_run("child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=claimed.version, heartbeat_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=30))
    store.close()


def test_terminal_transition_releases_child_lease() -> None:
    url, project_root = _database_url("lease-release")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    claimed = store.claim_child_run("child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=1, claimed_at=NOW + timedelta(seconds=2), lease_ttl=timedelta(seconds=30))
    assert claimed is not None
    with pytest.raises(RevisionConflictError, match="lease credentials"):
        store.transition("child-run:001", "succeeded", expected_version=claimed.version, occurred_at=NOW + timedelta(seconds=3))
    completed = store.complete_child_run("child-run:001", target_status="succeeded", result_envelope_ref="artifact:envelope:001", result_hash="a" * 64, worker_id="worker:a", lease_token="lease:a", expected_version=claimed.version, completed_at=NOW + timedelta(seconds=3))
    assert completed.lease_owner is None
    assert completed.lease_token is None
    assert completed.lease_expires_at is None
    store.close()


def test_expired_worker_cannot_complete_and_claim_requeue_emit_events() -> None:
    url, project_root = _database_url("late-worker")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    claimed = store.claim_child_run("child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=1, claimed_at=NOW + timedelta(seconds=2), lease_ttl=timedelta(seconds=1))
    assert claimed is not None
    with pytest.raises(RevisionConflictError, match="lease"):
        store.complete_child_run("child-run:001", target_status="succeeded", result_envelope_ref="artifact:late", result_hash="a" * 64, worker_id="worker:a", lease_token="lease:a", expected_version=claimed.version, completed_at=NOW + timedelta(seconds=4))
    store.requeue_expired_child_lease("child-run:001", expected_version=claimed.version, recovered_at=NOW + timedelta(seconds=4))
    status_events = [event.payload["to"] for event in store.list_events("parent:001").items if event.event_type == "run.status_changed"]
    assert status_events == ["queued", "running", "queued"]
    store.close()


def test_expired_child_lease_can_be_requeued_after_restart() -> None:
    url, project_root = _database_url("lease-recovery")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent())
    _create_child(store, _contract(), _child_run(), expected_parent_version=0)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW + timedelta(seconds=1))
    claimed = store.claim_child_run("child-run:001", worker_id="worker:a", lease_token="lease:a", expected_version=1, claimed_at=NOW + timedelta(seconds=2), lease_ttl=timedelta(seconds=1))
    assert claimed is not None
    store.close()
    reopened = SQLiteRunStore(url, project_root)
    recovered = reopened.requeue_expired_child_lease("child-run:001", expected_version=claimed.version, recovered_at=NOW + timedelta(seconds=4))
    assert recovered.status == "queued"
    assert recovered.lease_owner is None
    assert recovered.lease_token is None
    assert recovered.lease_expires_at is None
    reclaimed = reopened.claim_child_run("child-run:001", worker_id="worker:b", lease_token="lease:b", expected_version=recovered.version, claimed_at=NOW + timedelta(seconds=5), lease_ttl=timedelta(seconds=30))
    assert reclaimed is not None
    assert reclaimed.lease_owner == "worker:b"
    reopened.close()


def test_parent_listing_is_stably_paginated() -> None:
    url, project_root = _database_url("pagination")
    store = SQLiteRunStore(url, project_root)
    store.create_parent(_parent("parent:001"))
    store.create_parent(_parent("parent:002", created_at=NOW + timedelta(seconds=1), updated_at=NOW + timedelta(seconds=1), available_at=NOW + timedelta(seconds=1)))
    first = store.list_parents(limit=1)
    second = store.list_parents(limit=1, cursor=first.next_cursor)
    assert [parent.id for parent in first.items] == ["parent:001"]
    assert [parent.id for parent in second.items] == ["parent:002"]
    assert second.next_cursor is None
    store.close()
