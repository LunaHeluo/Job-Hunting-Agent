from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from starter_agent.delegation.dispatcher import Dispatcher, DispatcherConfig
from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, RunOutcome, TaskContract
from starter_agent.delegation.store import RevisionConflictError, SQLiteRunStore
from starter_agent.delegation.worker import PersistedChildAssembler, WorkerPoolConfig, compose_delegation_worker
from starter_agent.delegation.context import ChildContextBuilder, ContextBuildError, RuntimeContextAuthority
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.tools.registry import ToolRegistry
from uuid import UUID


NOW = datetime(2026, 8, 12, 9, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _limits(value: int) -> BudgetLimits:
    return BudgetLimits(tokens=value, cost_microunits=value, wall_clock_ms=value, model_calls=value, tool_calls=value)


def test_restart_recovers_expired_lease_and_rejects_late_worker_commit() -> None:
    test_root = ROOT / ".session-only-worker-recovery" / uuid4().hex
    test_root.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{test_root / 'runs.db'}"
    store = SQLiteRunStore(url, ROOT)
    parent = ParentRun(
        id="parent:1", session_id="session:1", origin_turn_id="turn:1", principal="user:1",
        coordinator_spec_version="1", runtime_revision="1", available_at=NOW,
        deadline_at=NOW + timedelta(minutes=5), budget_total=_limits(100),
        budget_reserved=_limits(0), budget_consumed=_limits(0), route="delegation",
        created_at=NOW, updated_at=NOW,
    )
    store.create_parent(parent)
    contract = TaskContract(
        task_id="task:1", parent_run_id=parent.id, specialist_id="job_web_researcher",
        goal="Research", inputs={"query": "Sydney"}, requested_allowed_tools=("search_jobs_serpapi",),
        requested_deadline=NOW + timedelta(minutes=4), requested_budget=_limits(10),
        failure_behavior="allow_partial", idempotency_key="delegate:1",
    )
    run = ChildRun(id="child:1", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=NOW, updated_at=NOW)
    store.create_child_task_and_run(contract=contract, child_run=run, specialist_snapshot_id="snapshot:1", output_schema_version="1", expected_parent_version=0, created_at=NOW)
    dispatcher = Dispatcher(store, config=DispatcherConfig(lease_ttl=timedelta(seconds=1)), now=lambda: NOW)
    old = dispatcher.claim_next(worker_id="worker:old")
    assert old is not None
    store.close()

    reopened = SQLiteRunStore(url, ROOT)
    recovered_dispatcher = Dispatcher(reopened, config=DispatcherConfig(lease_ttl=timedelta(seconds=10)), now=lambda: NOW + timedelta(seconds=2))
    recovered_dispatcher.reap_expired()
    new = recovered_dispatcher.claim_next(worker_id="worker:new")
    assert new is not None

    try:
        recovered_dispatcher.finish(old, RunOutcome(disposition="completed", run_id=old.run.id, status="succeeded", output_ref="output:old"))
    except RevisionConflictError:
        pass
    else:
        raise AssertionError("late lease owner committed after recovery")


def test_persisted_child_assembler_uses_pinned_registry_and_real_context_builder() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    parent = ParentRun(
        id="parent:assembler", session_id="session:1", origin_turn_id="turn:1", principal="user:1",
        coordinator_spec_version="1", runtime_revision="1", available_at=NOW,
        deadline_at=NOW + timedelta(minutes=5), budget_total=_limits(100), budget_reserved=_limits(0),
        budget_consumed=_limits(0), route="delegation", created_at=NOW, updated_at=NOW,
    )
    store.create_parent(parent)
    registry = SpecialistRegistry(ROOT / "config/specialists", project_root=ROOT, dependency_resolver=lambda _dependency: True)
    snapshot = registry.reload()
    definition = registry.resolve("job_web_researcher")
    contract = TaskContract(
        task_id="task:assembler", parent_run_id=parent.id, specialist_id=definition.specialist_id,
        goal="Research", inputs={"query": "Sydney", "target_fields": ["title"], "max_pages": 1, "stop_conditions": {"target_valid_jobs": 1}, "output_schema_version": "job-web-output-v1"},
        requested_allowed_tools=(), requested_deadline=NOW + timedelta(minutes=4), requested_budget=_limits(10),
        failure_behavior="allow_partial", idempotency_key="delegate:assembler",
    )
    run = ChildRun(id="child:assembler", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=NOW, updated_at=NOW)
    created = store.create_child_task_and_run(contract=contract, child_run=run, specialist_snapshot_id=snapshot.snapshot_hash, output_schema_version=definition.schema_version, expected_parent_version=0, created_at=NOW)
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: NOW)
    claim = dispatcher.claim_next(worker_id="worker:assembler")
    assert claim is not None

    class Resolver:
        def load(self, reference, authority):
            raise ContextBuildError("unexpected_reference", "none expected")

    tool_registry = UnifiedToolRegistry(ToolRegistry([]))
    def authority_factory(claim, _specialist):
        return RuntimeContextAuthority(
            parent_run_id=claim.parent.id, child_task_id=claim.task.id, child_run_id=claim.run.id,
            session_id=UUID("00000000-0000-0000-0000-000000000001"), turn_id=UUID("00000000-0000-0000-0000-000000000002"),
            principal=claim.parent.principal, now=NOW, parent_deadline=claim.parent.deadline_at,
            policy_deadline=NOW + timedelta(minutes=3), parent_remaining_budget=_limits(8), policy_budget=_limits(7),
            scenario_tools=frozenset(), policy_tools=frozenset(), allowed_artifact_types=frozenset(),
            allowed_knowledge_scope_types=frozenset(), knowledge_user_id="user:1", knowledge_project_id=None,
            knowledge_base_id=None, runtime_revision="1", provider="fixture", model="fixture", tool_registry=tool_registry,
        )

    built = PersistedChildAssembler(registry=registry, context_builder=ChildContextBuilder(Resolver()), authority_factory=authority_factory)(claim)

    assert built.context.budget.limits == _limits(7)
    assert built.deadline == NOW + timedelta(minutes=3)
    assert built.spec.allowed_tools == ()
    assert built.context.run_id == created.run.id


def test_production_composition_shares_capacity_store_registry_builder_and_runtime(tmp_path) -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    registry = SpecialistRegistry(ROOT / "config/specialists", project_root=ROOT, dependency_resolver=lambda _dependency: True)
    registry.reload()

    class Resolver:
        def load(self, reference, authority):
            raise AssertionError("no references expected")

    class Runtime:
        async def run(self, *, spec, context):
            return RunOutcome(disposition="completed", run_id=spec.run_id, status="succeeded", output_ref="output:1")

    components = compose_delegation_worker(
        store=store, registry=registry, context_builder=ChildContextBuilder(Resolver()), runtime=Runtime(),
        authority_factory=lambda claim, specialist: None,
        dispatcher_config=DispatcherConfig(queue_high_watermark=5, queue_hard_capacity=7),
        worker_config=WorkerPoolConfig(global_concurrency=1),
        artifact_store=SQLiteSessionStore("sqlite:///artifacts.db", tmp_path),
        artifact_retention=timedelta(days=14),
    )

    assert components.service.store is store
    assert components.service.registry is registry
    assert components.service.queue_hard_capacity == 7
    assert components.dispatcher.store is store
    assert components.executor.assemble is components.assembler
