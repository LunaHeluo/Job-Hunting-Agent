from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from starter_agent.delegation.models import BudgetLimits, ParentRun
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.service import (
    CoordinatorTaskContract,
    DelegationService,
)
from starter_agent.delegation.store import (
    IdempotencyPayloadConflictError,
    SQLiteRunStore,
)


ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 12, 8, tzinfo=UTC)


def _limits(value: int) -> BudgetLimits:
    return BudgetLimits(
        tokens=value,
        cost_microunits=value,
        wall_clock_ms=value,
        model_calls=value,
        tool_calls=value,
    )


def _parent() -> ParentRun:
    return ParentRun(
        id="parent:delegate:001",
        session_id="session:001",
        origin_turn_id="turn:001",
        principal="user:001",
        coordinator_spec_version="1",
        runtime_revision="runtime:001",
        available_at=NOW,
        deadline_at=NOW + timedelta(minutes=20),
        budget_total=_limits(500_000),
        budget_reserved=_limits(0),
        budget_consumed=_limits(0),
        route="job_application_delegation",
        created_at=NOW,
        updated_at=NOW,
    )


def _contract(*, query: str = "Agent engineer Sydney") -> CoordinatorTaskContract:
    requested = BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=10, tool_calls=20)
    return CoordinatorTaskContract(
        goal="Research Sydney Agent engineering jobs",
        inputs={
            "query": query,
            "target_fields": ["title", "company", "requirements"],
            "max_pages": 3,
            "stop_conditions": {"target_jobs": 5},
            "output_schema_version": "job-web-output-v1",
        },
        constraints={"region": "Sydney"},
        requested_allowed_tools=("search_jobs_serpapi",),
        requested_deadline=NOW + timedelta(minutes=10),
        requested_budget=requested,
        failure_behavior="allow_partial",
        idempotency_key="delegate:web:001",
    )


def _service(name: str) -> tuple[DelegationService, SQLiteRunStore]:
    test_root = ROOT / ".session-only-delegate-task-tests" / f"{name}-{uuid4().hex}"
    test_root.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(f"sqlite:///{test_root / 'runs.db'}", ROOT)
    store.create_parent(_parent())
    registry = SpecialistRegistry(
        ROOT / "config" / "specialists",
        project_root=ROOT,
        dependency_resolver=lambda _dependency: True,
    )
    registry.reload()
    return DelegationService(store=store, registry=registry, now=lambda: NOW), store


def test_delegate_task_atomically_persists_real_child_and_returns_receipt() -> None:
    service, store = _service("atomic")

    receipt = service.delegate_task(
        parent_run_id="parent:delegate:001",
        specialist_id="job_web_researcher",
        task_contract=_contract(),
    )

    tree = store.get_run_tree("parent:delegate:001")
    assert receipt.status == "queued"
    assert receipt.parent_run_id == tree.parent.id
    assert receipt.task_id == tree.child_tasks[0].id
    assert receipt.child_run_id == tree.child_runs[0].id
    assert receipt.specialist_snapshot_id == tree.child_tasks[0].specialist_snapshot_id
    assert tree.child_runs[0].status == "queued"
    assert tree.parent.budget_reserved == BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=10, tool_calls=20)
    assert len(tree.allocations) == 5
    assert [event.event_type for event in tree.events] == [
        "budget.reserved",
        "child.delegated",
    ]
    outbox = store.list_outbox(limit=10).items
    assert len(outbox) == 1
    assert outbox[0].topic == "delegation.child.queued"
    assert set(receipt.model_dump()) == {
        "receipt_id",
        "parent_run_id",
        "task_id",
        "child_run_id",
        "specialist_id",
        "specialist_snapshot_id",
        "status",
        "created_at",
    }


def test_delegate_task_is_idempotent_and_rejects_changed_payload() -> None:
    service, store = _service("idempotent")

    first = service.delegate_task(
        parent_run_id="parent:delegate:001",
        specialist_id="job_web_researcher",
        task_contract=_contract(),
    )
    repeated = service.delegate_task(
        parent_run_id="parent:delegate:001",
        specialist_id="job_web_researcher",
        task_contract=_contract(),
    )

    assert repeated == first
    tree = store.get_run_tree("parent:delegate:001")
    assert len(tree.child_tasks) == len(tree.child_runs) == 1
    assert [event.event_type for event in tree.events] == [
        "budget.reserved",
        "child.delegated",
    ]
    assert len(store.list_outbox(limit=10).items) == 1

    with pytest.raises(IdempotencyPayloadConflictError):
        service.delegate_task(
            parent_run_id="parent:delegate:001",
            specialist_id="job_web_researcher",
            task_contract=_contract(query="changed payload"),
        )


def test_delegate_task_pins_the_same_registry_snapshot_used_for_validation() -> None:
    specialists = ROOT / ".session-only-delegate-task-tests" / f"snapshot-{uuid4().hex}" / "specialists"
    shutil.copytree(ROOT / "config" / "specialists", specialists)
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    registry = SpecialistRegistry(specialists, project_root=ROOT, dependency_resolver=lambda _: True)
    registry.reload()
    original_resolve = registry.resolve_with_snapshot

    def resolve_then_reload(*args, **kwargs):
        definition, snapshot = original_resolve(*args, **kwargs)
        yaml_path = specialists / "job_web_researcher.yaml"
        yaml_path.write_text(yaml_path.read_text(encoding="utf-8").replace('version: "1.0.0"', 'version: "1.0.1"'), encoding="utf-8")
        registry.reload()
        return definition, snapshot

    registry.resolve_with_snapshot = resolve_then_reload  # type: ignore[method-assign]
    service = DelegationService(store=store, registry=registry, now=lambda: NOW)

    receipt = service.delegate_task(parent_run_id="parent:delegate:001", specialist_id="job_web_researcher", task_contract=_contract())

    persisted = store.get_run_tree(receipt.parent_run_id).child_tasks[0]
    pinned = registry.resolve_pinned(persisted.specialist_id, snapshot_hash=persisted.specialist_snapshot_id)
    event = next(
        item
        for item in store.get_run_tree(receipt.parent_run_id).events
        if item.event_type == "child.delegated"
    )
    assert event.payload["specialist_version"] == pinned.version


def test_delegate_task_hard_capacity_is_atomic_and_idempotent_replay_wins() -> None:
    service, store = _service("hard-capacity")
    service.queue_hard_capacity = 1
    first = service.delegate_task(parent_run_id="parent:delegate:001", specialist_id="job_web_researcher", task_contract=_contract())

    assert service.delegate_task(parent_run_id="parent:delegate:001", specialist_id="job_web_researcher", task_contract=_contract()) == first
    with pytest.raises(Exception) as captured:
        service.delegate_task(
            parent_run_id="parent:delegate:001", specialist_id="job_web_researcher",
            task_contract=_contract(query="second").model_copy(update={"idempotency_key": "delegate:web:002"}),
        )
    assert getattr(captured.value, "code", None) == "run_queue_overloaded"
