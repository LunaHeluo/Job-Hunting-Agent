from datetime import UTC, datetime, timedelta
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.capabilities.models import PolicyRule
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.coordinator import Coordinator, EnvelopeValidation
from starter_agent.delegation.models import BudgetLimits, ParentRun, RunSpec
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.service import DelegationService
from starter_agent.delegation.service import CoordinatorTaskContract
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.delegation.tools import DelegateTaskTool
from starter_agent.domain.models import Message, ModelResponse, ToolCall, ToolResult
from starter_agent.providers.base import Provider
from starter_agent.settings import RuntimeConfig
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry


NOW = datetime(2026, 8, 12, 10, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _limits(value: int) -> BudgetLimits:
    return BudgetLimits(tokens=value, cost_microunits=value, wall_clock_ms=value, model_calls=value, tool_calls=value)


def _parent() -> ParentRun:
    return ParentRun(id="parent:resume:1", session_id="session:1", origin_turn_id="turn:1", principal="user:1",
        coordinator_spec_version="1", runtime_revision="runtime:1", status="running", phase="planning", started_at=NOW,
        available_at=NOW, deadline_at=NOW + timedelta(hours=1), budget_total=_limits(10000), budget_reserved=_limits(0),
        budget_consumed=_limits(0), route="delegation", created_at=NOW, updated_at=NOW)


def _registry() -> SpecialistRegistry:
    registry = SpecialistRegistry(ROOT / "config" / "specialists", project_root=ROOT, dependency_resolver=lambda _: True)
    registry.reload()
    return registry


def _delegate_arguments(number: int) -> dict:
    return {
        "specialist_id": "job_web_researcher",
        "task_contract": {
            "goal": f"Research Sydney jobs batch {number}",
            "inputs": {"query": f"Agent engineer Sydney {number}", "target_fields": ["title"], "max_pages": 1, "stop_conditions": {}, "output_schema_version": "job-web-output-v1"},
            "constraints": {}, "requested_allowed_tools": ["search_jobs_serpapi"],
            "requested_deadline": (NOW + timedelta(minutes=10)).isoformat(),
            "requested_budget": _limits(10).model_dump(mode="json"),
            "failure_behavior": "allow_partial", "idempotency_key": f"delegate:web:{number}", "contract_version": "1",
        },
    }


def _accept_authority(authority):
    return EnvelopeValidation(
        authorized=True,
        parent_run_id=authority.parent_run_id,
        task_id=authority.task_id,
        child_run_id=authority.child_run_id,
        envelope_ref=authority.envelope_ref,
        result_hash=authority.result_hash,
        principal=authority.principal,
        trace_ref=authority.trace_ref,
    )


class _Provider(Provider):
    name = "fixture"
    def __init__(self) -> None: self.calls = 0
    async def complete(self, messages, model, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(provider=self.name, model=model, usage={"total_tokens": 1, "cost_microunits": 1}, tool_calls=[
                ToolCall(id="d1", name="delegate_task", arguments=_delegate_arguments(1)),
                ToolCall(id="d2", name="delegate_task", arguments=_delegate_arguments(2)),
            ])
        return ModelResponse(provider=self.name, model=model, content="must not run", usage={"total_tokens": 1, "cost_microunits": 1})
    async def health(self, model): return True, model


@pytest.mark.asyncio
async def test_entire_delegate_batch_persists_then_parent_suspends_before_next_model_call() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    delegate = DelegateTaskTool(DelegationService(store=store, registry=_registry(), now=lambda: NOW))
    tools = ToolRegistry([]); tools._tools = {delegate.name: delegate}
    provider = _Provider()
    runtime = AgentRuntime(tools, ToolPolicy(["read", "write"]), RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=5), provider_resolver=lambda _: provider)
    capability = runtime.gate.registry.resolve_execution("delegate_task")
    assert capability is not None
    runtime.gate.store.create_policy_rule(PolicyRule(id="allow-delegate", server_id="builtin", tool_name="delegate_task", effect="allowlist_auto", roles=("coordinator",), schema_hash=capability.schema_hash, created_by="test"))
    coordinator = Coordinator(store=store, now=lambda: NOW)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1",
        messages=[Message(role="user", content="research")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3),
        trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture",
        system_prompt_ref="prompt:coordinator:1", output_schema_ref="schema:coordinator:1", allowed_tools=("delegate_task",), max_steps=3, runtime_revision="runtime:1")

    async def approve(event: dict) -> None:
        if event.get("type") != "confirmation_required":
            return
        confirmation = runtime.turn_coordinator.confirmations.get(event["confirmation_id"])
        runtime.turn_coordinator.confirmations.decide(
            confirmation.id,
            expected_revision=confirmation.revision,
            idempotency_key=f"approve:{confirmation.call_id}",
            decision="once",
        )

    outcome = await coordinator.run(runtime=runtime, spec=spec, context=context, on_tool_event=approve)

    assert provider.calls == 1
    assert outcome.disposition == "suspended" and outcome.status == "waiting_children"
    persisted = store.get_parent(_parent().id)
    assert persisted is not None and persisted.status == "waiting_children" and persisted.phase == "waiting_children"
    checkpoint = store.get_coordinator_checkpoint(_parent().id)
    assert checkpoint is not None
    assert checkpoint.payload["messages"][-2]["role"] == "tool"
    assert checkpoint.payload["messages"][-1]["role"] == "tool"
    assert "suspension_probe" not in checkpoint.payload
    tree = store.get_run_tree(_parent().id)
    assert len(tree.child_tasks) == len(tree.child_runs) == 2
    assert all(run.status == "queued" for run in tree.child_runs)
    batch = store.get_delegate_batch(_parent().id)
    assert batch is not None and batch.completed_call_ids == ("d1", "d2")
    assert context.suspension_probe is None
    assert context.suspension_checkpoint_ref == outcome.checkpoint_ref


def test_resume_only_loads_validated_envelopes_and_refs_and_profile_depends_by_reference() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW, envelope_validator=_accept_authority)
    checkpoint_context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1",
        messages=[Message(role="user", content="research")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3),
        trace_context=RunTraceContext(parent_run_id=_parent().id))
    coordinator.persist_checkpoint(checkpoint_context)
    service = DelegationService(store=store, registry=_registry(), now=lambda: NOW)
    receipt = service.delegate_task(parent_run_id=_parent().id, specialist_id="job_web_researcher", task_contract=__import__("starter_agent.delegation.service", fromlist=["CoordinatorTaskContract"]).CoordinatorTaskContract.model_validate(_delegate_arguments(9)["task_contract"]))
    queued = store.get_run_tree(_parent().id).child_runs[0]
    running = store.claim_child_run(queued.id, worker_id="worker:1", lease_token="lease:1", expected_version=queued.version, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(minutes=1))
    assert running is not None
    result_hash = "a" * 64
    store.complete_child_run(running.id, target_status="succeeded", result_envelope_ref="artifact:env:web", result_hash=result_hash, worker_id="worker:1", lease_token="lease:1", expected_version=running.version, completed_at=NOW + timedelta(seconds=2))
    task = store.get_run_tree(_parent().id).child_tasks[0]
    store._accept_child_result(running.id, result_envelope_ref="artifact:env:web", result_hash=result_hash, expected_task_version=task.version, accepted_at=NOW + timedelta(seconds=3))

    restored = coordinator.resume_context(_parent().id)
    profile = coordinator.profile_dependency_inputs(normalized_job_requirements_ref="artifact:env:web", web_task_id="task:web", knowledge_scope={"type": "resume", "user_id": "user:1", "project_id": "project:1", "knowledge_base_id": "kb:1"}, candidate_chunk_ids=("chunk:1",), top_k=3)

    assert restored is not checkpoint_context
    assert restored.working_memory["validated_child_results"] == [{"task_id": receipt.task_id, "child_run_id": receipt.child_run_id, "envelope_ref": "artifact:env:web", "trace_ref": f"trace:child-run:{receipt.child_run_id}"}]
    assert restored.artifact_refs == ["artifact:env:web", f"trace:child-run:{receipt.child_run_id}"]
    assert "raw-html" not in repr(restored.to_checkpoint())
    assert profile == {"normalized_job_requirements_ref": "artifact:env:web", "depends_on_task_id": "task:web", "knowledge_scope": {"type": "resume", "user_id": "user:1", "project_id": "project:1", "knowledge_base_id": "kb:1"}, "candidate_chunk_ids": ["chunk:1"], "top_k": 3, "output_schema_version": "profile-evidence-output-v1"}
    assert "jobs" not in profile


def test_woken_parent_must_transition_queued_to_running_before_resume() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    parent = _parent().model_copy(update={"status": "queued", "phase": "children_terminal"})
    store.create_parent(parent)
    coordinator = Coordinator(store=store, now=lambda: NOW)

    running = coordinator.begin_resumed_attempt(parent.id)

    assert running.status == "running"
    assert running.phase == "validating"
    assert running.version == parent.version + 1


def test_resume_fails_closed_without_envelope_validator() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1",
        messages=[Message(role="user", content="research")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    coordinator.persist_checkpoint(context)

    restored = coordinator.resume_context(_parent().id)

    assert restored.working_memory["validated_child_results"] == []
    assert restored.artifact_refs == []


@pytest.mark.asyncio
async def test_reconcile_crash_after_child_creation_uses_persisted_planning_checkpoint() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1",
        messages=[Message(role="user", content="research")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    service = DelegationService(store=store, registry=_registry(), now=lambda: NOW)
    class CrashingRuntime:
        async def run(self, *, spec, context):
            assert store.get_coordinator_checkpoint(context.parent_run_id) is not None
            service.delegate_task(parent_run_id=context.parent_run_id, specialist_id="job_web_researcher", task_contract=CoordinatorTaskContract.model_validate(_delegate_arguments(31)["task_contract"]))
            raise KeyboardInterrupt("crash after durable child creation")
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=1, runtime_revision="r")
    with pytest.raises(KeyboardInterrupt):
        await coordinator.run(runtime=CrashingRuntime(), spec=spec, context=context)
    with pytest.raises(Exception) as caught:
        coordinator.reconcile_interrupted_planning(_parent().id)

    assert getattr(caught.value, "code", None) == "delegate_batch_ledger_missing"
    assert store.get_parent(_parent().id).status == "running"


def test_invalid_checkpoint_schema_fails_closed_with_stable_code() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    coordinator.persist_checkpoint(context)
    checkpoint = store.get_coordinator_checkpoint(_parent().id)
    assert checkpoint is not None
    from sqlalchemy import text
    with store.engine.begin() as connection:
        raw = checkpoint.model_dump(mode="json"); raw["schema_version"] = "0"
        import json
        connection.execute(text("UPDATE delegation_coordinator_checkpoints SET payload_json=:payload WHERE parent_run_id=:parent"), {"payload": json.dumps(raw), "parent": _parent().id})

    with pytest.raises(Exception) as caught:
        coordinator.resume_context(_parent().id)
    assert getattr(caught.value, "code", None) == "coordinator_checkpoint_invalid"


@pytest.mark.asyncio
async def test_planning_checkpoint_alone_does_not_mark_successful_parent_suspended() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    provider = _Provider()
    async def complete_without_delegation(*args, **kwargs):
        provider.calls += 1
        return ModelResponse(provider="fixture", model="fixture", content="done", usage={"total_tokens": 1, "cost_microunits": 1})
    provider.complete = complete_without_delegation
    runtime = AgentRuntime(ToolRegistry([]), ToolPolicy(["read"]), RuntimeConfig(max_model_calls=1, max_tool_calls=0, max_seconds=5), provider_resolver=lambda _: provider)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=[], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=1, tool_calls=0), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=(), max_steps=1, runtime_revision="r")

    outcome = await Coordinator(store=store, now=lambda: NOW).run(runtime=runtime, spec=spec, context=context)

    assert outcome.status == "succeeded"
    assert outcome.disposition == "completed"


@pytest.mark.asyncio
async def test_child_finishing_inside_delegate_batch_still_suspends_same_attempt() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    registry = _registry(); service = DelegationService(store=store, registry=registry, now=lambda: NOW)
    delegate = DelegateTaskTool(service); tools = ToolRegistry([]); tools._tools = {delegate.name: delegate}
    provider = _Provider(); runtime = AgentRuntime(tools, ToolPolicy(["read", "write"]), RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=5), provider_resolver=lambda _: provider)
    capability = runtime.gate.registry.resolve_execution("delegate_task"); assert capability
    runtime.gate.store.create_policy_rule(PolicyRule(id="allow-fast-delegate", server_id="builtin", tool_name="delegate_task", effect="allowlist_auto", roles=("coordinator",), schema_hash=capability.schema_hash, created_by="test"))
    async def finish_on_second(event):
        if event.get("type") == "confirmation_required":
            confirmation = runtime.turn_coordinator.confirmations.get(event["confirmation_id"])
            runtime.turn_coordinator.confirmations.decide(confirmation.id, expected_revision=confirmation.revision, idempotency_key=f"approve:{confirmation.call_id}", decision="once")
        if event.get("type") == "tool_completed" and event.get("call_id") == "d2":
            for queued in store.get_run_tree(_parent().id).child_runs:
                claimed = store.claim_child_run(queued.id, worker_id="fast", lease_token=f"lease:{queued.id}", expected_version=queued.version, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(minutes=1)); assert claimed
                store.complete_child_run(claimed.id, target_status="failed", result_envelope_ref=f"artifact:failed:{claimed.id}", result_hash="c" * 64, worker_id="fast", lease_token=f"lease:{queued.id}", expected_version=claimed.version, completed_at=NOW + timedelta(seconds=2))
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="research")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=3, runtime_revision="r")

    outcome = await Coordinator(store=store, now=lambda: NOW).run(runtime=runtime, spec=spec, context=context, on_tool_event=finish_on_second)

    assert outcome.disposition == "suspended"
    assert provider.calls == 1
    assert store.get_parent(_parent().id).status == "queued"


@pytest.mark.asyncio
async def test_delegate_batch_ledger_replays_only_missing_call_after_crash() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    calls = (
        ToolCall(id="batch:1", name="delegate_task", arguments=_delegate_arguments(41)),
        ToolCall(id="batch:2", name="delegate_task", arguments=_delegate_arguments(42)),
    )
    coordinator.record_delegate_batch(_parent().id, calls)
    coordinator.mark_delegate_call_completed(_parent().id, "batch:1")
    replayed = []
    class ReplayRuntime:
        async def replay_persisted_delegate_call(self, *, spec, context, call, on_tool_event=None):
            replayed.append(call.id)
            return ToolResult(ok=True, data={"child_run_id": "child:replayed"})
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=1, runtime_revision="r")

    outcome = await coordinator.replay_incomplete_delegate_batch(_parent().id, runtime=ReplayRuntime(), spec=spec, context=context)

    assert replayed == ["batch:2"]
    batch = store.get_delegate_batch(_parent().id)
    assert batch is not None and batch.completed_call_ids == ("batch:1", "batch:2")
    assert outcome.disposition == "suspended"
    assert [message.tool_call_id for message in context.messages if message.role == "tool"] == ["batch:1", "batch:2"]


@pytest.mark.asyncio
async def test_repeating_completed_batch_replay_is_message_idempotent() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    calls = (ToolCall(id="repeat:1", name="delegate_task", arguments=_delegate_arguments(43)),)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=1, tool_calls=1), trace_context=RunTraceContext(parent_run_id=_parent().id))
    coordinator.record_delegate_batch(_parent().id, calls, context_checkpoint=context.to_checkpoint())
    class ReplayRuntime:
        async def replay_persisted_delegate_call(self, *, spec, context, call, on_tool_event=None):
            context.budget.consume(tool_calls=1)
            return ToolResult(ok=False, error_code="controlled")
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="x", model="x", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=1, runtime_revision="r")
    await coordinator.replay_incomplete_delegate_batch(_parent().id, runtime=ReplayRuntime(), spec=spec, context=context)
    restored = coordinator.resume_context(_parent().id)

    second = await coordinator.replay_incomplete_delegate_batch(_parent().id, runtime=ReplayRuntime(), spec=spec, context=restored)

    assert second.disposition == "suspended"
    persisted = store.get_coordinator_checkpoint(_parent().id); assert persisted
    assert [m["tool_call_id"] for m in persisted.payload["messages"] if m["role"] == "tool"] == ["repeat:1"]


@pytest.mark.asyncio
async def test_real_runtime_crash_after_first_receipt_reopens_from_batch_checkpoint_without_second_model_call() -> None:
    root = ROOT / ".session-task9-replay" / uuid4().hex; root.mkdir(parents=True)
    database = root / "runs.db"
    store = SQLiteRunStore(f"sqlite:///{database}", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    delegate = DelegateTaskTool(DelegationService(store=store, registry=_registry(), now=lambda: NOW)); tools = ToolRegistry([]); tools._tools = {delegate.name: delegate}
    provider = _Provider()
    runtime = AgentRuntime(tools, ToolPolicy(["read", "write"]), RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=5), provider_resolver=lambda _: provider)
    capability = runtime.gate.registry.resolve_execution("delegate_task"); assert capability
    runtime.gate.store.create_policy_rule(PolicyRule(id="allow-crash-delegate", server_id="builtin", tool_name="delegate_task", effect="allowlist_auto", roles=("coordinator",), schema_hash=capability.schema_hash, created_by="test"))
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=3, runtime_revision="r")
    original_mark = coordinator.mark_delegate_call_completed
    crashed = False
    def crash_after_first_receipt(parent_run_id, call_id, receipt=None, *, context_checkpoint=None):
        nonlocal crashed
        batch = original_mark(parent_run_id, call_id, receipt, context_checkpoint=context_checkpoint)
        if not crashed:
            crashed = True
            raise KeyboardInterrupt("simulated process crash after durable receipt")
        return batch
    coordinator.mark_delegate_call_completed = crash_after_first_receipt
    async def approve(event):
        if event.get("type") == "confirmation_required":
            confirmation = runtime.turn_coordinator.confirmations.get(event["confirmation_id"])
            runtime.turn_coordinator.confirmations.decide(confirmation.id, expected_revision=confirmation.revision, idempotency_key=f"approve:{confirmation.call_id}", decision="once")
    with pytest.raises(KeyboardInterrupt):
        await asyncio.wait_for(coordinator.run(runtime=runtime, spec=spec, context=context, on_tool_event=approve), timeout=5)
    persisted_before_restart = store.get_delegate_batch(_parent().id)
    assert persisted_before_restart is not None
    assert persisted_before_restart.completed_call_ids == ("d1",)
    assert persisted_before_restart.context_checkpoint["budget_consumed"]["model_calls"] == 1
    assert persisted_before_restart.context_checkpoint["budget_consumed"]["tool_calls"] == 1
    assert [m["tool_call_id"] for m in persisted_before_restart.context_checkpoint["messages"] if m["role"] == "tool"] == ["d1"]
    store.close()

    reopened = SQLiteRunStore(f"sqlite:///{database}", ROOT)
    delegate = DelegateTaskTool(DelegationService(store=reopened, registry=_registry(), now=lambda: NOW)); tools = ToolRegistry([]); tools._tools = {delegate.name: delegate}
    replay_runtime = AgentRuntime(tools, ToolPolicy(["read", "write"]), RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=5), provider_resolver=lambda _: (_ for _ in ()).throw(AssertionError("replay must not request model")))
    capability = replay_runtime.gate.registry.resolve_execution("delegate_task"); assert capability
    replay_runtime.gate.store.create_policy_rule(PolicyRule(id="allow-replay-delegate", server_id="builtin", tool_name="delegate_task", effect="allowlist_auto", roles=("coordinator",), schema_hash=capability.schema_hash, created_by="test"))
    restored = Coordinator(store=reopened, now=lambda: NOW).resume_context(_parent().id)
    resumed = Coordinator(store=reopened, now=lambda: NOW)
    async def approve_replay(event):
        if event.get("type") == "confirmation_required":
            confirmation = replay_runtime.turn_coordinator.confirmations.get(event["confirmation_id"])
            replay_runtime.turn_coordinator.confirmations.decide(confirmation.id, expected_revision=confirmation.revision, idempotency_key=f"approve:{confirmation.call_id}", decision="once")
    outcome = await asyncio.wait_for(resumed.replay_incomplete_delegate_batch(_parent().id, runtime=replay_runtime, spec=spec, context=restored, on_tool_event=approve_replay), timeout=5)

    tree = reopened.get_run_tree(_parent().id)
    assert outcome.disposition == "suspended"
    assert len(tree.child_runs) == 2
    assert reopened.get_delegate_batch(_parent().id).completed_call_ids == ("d1", "d2")
    checkpoint = reopened.get_coordinator_checkpoint(_parent().id); assert checkpoint
    assert checkpoint.payload["budget_consumed"]["model_calls"] == 1
    assert checkpoint.payload["budget_consumed"]["tool_calls"] == 2
    assert [m["tool_call_id"] for m in checkpoint.payload["messages"] if m["role"] == "tool"] == ["d1", "d2"]


def test_same_parent_multiple_batches_and_cross_parent_same_calls_are_independent() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    other = _parent().model_copy(update={"id": "parent:batch:other", "session_id": "s:other", "origin_turn_id": "t:other"}); store.create_parent(other)
    coordinator = Coordinator(store=store, now=lambda: NOW)
    calls = (ToolCall(id="same:1", name="delegate_task", arguments=_delegate_arguments(71)),)

    first = coordinator.record_delegate_batch(_parent().id, calls, model_request_id="model:1", response_hash="a" * 64)
    coordinator.mark_delegate_call_completed(_parent().id, "same:1")
    second = coordinator.record_delegate_batch(_parent().id, calls, model_request_id="model:2", response_hash="b" * 64)
    foreign = coordinator.record_delegate_batch(other.id, calls, model_request_id="model:1", response_hash="a" * 64)

    assert len({first.batch_id, second.batch_id, foreign.batch_id}) == 3
    assert store.get_delegate_batch(_parent().id, batch_id=first.batch_id).batch_id == first.batch_id
    assert store.get_delegate_batch(_parent().id).batch_id == second.batch_id


def test_delegate_batch_rejects_duplicate_call_ids_and_replacing_incomplete_active_batch() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    duplicate = (ToolCall(id="dup", name="delegate_task", arguments=_delegate_arguments(91)), ToolCall(id="dup", name="delegate_task", arguments=_delegate_arguments(92)))
    with pytest.raises(ValueError, match="delegate_batch_invalid"):
        coordinator.record_delegate_batch(_parent().id, duplicate)
    coordinator.record_delegate_batch(_parent().id, (ToolCall(id="old", name="delegate_task", arguments=_delegate_arguments(93)),), model_request_id="old")
    with pytest.raises(Exception) as caught:
        coordinator.record_delegate_batch(_parent().id, (ToolCall(id="new", name="delegate_task", arguments=_delegate_arguments(94)),), model_request_id="new")
    assert getattr(caught.value, "code", None) == "delegate_batch_active_incomplete"


def test_duplicate_receipt_with_different_payload_is_rejected() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    coordinator.record_delegate_batch(_parent().id, (ToolCall(id="receipt", name="delegate_task", arguments=_delegate_arguments(95)),))
    coordinator.mark_delegate_call_completed(_parent().id, "receipt", {"ok": True, "data": {"child_run_id": "a"}})
    with pytest.raises(Exception) as caught:
        coordinator.mark_delegate_call_completed(_parent().id, "receipt", {"ok": True, "data": {"child_run_id": "b"}})
    assert getattr(caught.value, "code", None) == "delegate_batch_receipt_conflict"


@pytest.mark.asyncio
async def test_replay_rejects_mixed_parent_spec_context_and_principal() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    coordinator.record_delegate_batch(_parent().id, (ToolCall(id="x:1", name="delegate_task", arguments=_delegate_arguments(72)),), model_request_id="m:1", response_hash="e" * 64)
    bad = RunContext(run_id="parent:other", parent_run_id="parent:other", session_id=uuid4(), turn_id=uuid4(), principal="attacker", messages=[], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=1, tool_calls=1), trace_context=RunTraceContext(parent_run_id="parent:other"))
    spec = RunSpec(run_id="parent:other", run_kind="parent", role="coordinator", provider="x", model="x", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=1, runtime_revision="r")
    with pytest.raises(Exception) as caught:
        await coordinator.replay_incomplete_delegate_batch(_parent().id, runtime=object(), spec=spec, context=bad)
    assert getattr(caught.value, "code", None) == "delegate_batch_authority_mismatch"


@pytest.mark.asyncio
async def test_replay_rejects_tampered_receipt_hash_before_runtime_side_effect() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    coordinator = Coordinator(store=store, now=lambda: NOW)
    batch = coordinator.record_delegate_batch(_parent().id, (ToolCall(id="hash:1", name="delegate_task", arguments=_delegate_arguments(96)),))
    coordinator.mark_delegate_call_completed(_parent().id, "hash:1", {"ok": True})
    from sqlalchemy import text
    import json
    raw = store.get_delegate_batch(_parent().id).model_dump(mode="json"); raw["receipts"][0]["outcome_hash"] = "0" * 64
    with store.engine.begin() as connection:
        connection.execute(text("UPDATE delegation_delegate_batches SET payload_json=:payload WHERE batch_id=:batch"), {"payload": json.dumps(raw), "batch": batch.batch_id})
    class NeverRuntime:
        calls = 0
        async def replay_persisted_delegate_call(self, **kwargs): self.calls += 1
    runtime = NeverRuntime()
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=1, tool_calls=1), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="x", model="x", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=1, runtime_revision="r")
    with pytest.raises(Exception) as caught:
        await coordinator.replay_incomplete_delegate_batch(_parent().id, runtime=runtime, spec=spec, context=context)
    assert getattr(caught.value, "code", None) == "delegate_batch_receipt_invalid"
    assert runtime.calls == 0


def test_profile_dependency_can_only_delegate_from_accepted_web_reference() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    registry = _registry(); service = DelegationService(store=store, registry=registry, now=lambda: NOW)
    coordinator = Coordinator(store=store, now=lambda: NOW, envelope_validator=_accept_authority)
    web = service.delegate_task(parent_run_id=_parent().id, specialist_id="job_web_researcher", task_contract=CoordinatorTaskContract.model_validate(_delegate_arguments(11)["task_contract"]))
    queued = store.get_run_tree(_parent().id).child_runs[0]
    running = store.claim_child_run(queued.id, worker_id="worker:1", lease_token="lease:1", expected_version=queued.version, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(minutes=1)); assert running
    result_hash = "b" * 64
    store.complete_child_run(running.id, target_status="succeeded", result_envelope_ref="artifact:env:web", result_hash=result_hash, worker_id="worker:1", lease_token="lease:1", expected_version=running.version, completed_at=NOW + timedelta(seconds=2))
    web_task = store.get_run_tree(_parent().id).child_tasks[0]
    store._accept_child_result(running.id, result_envelope_ref="artifact:env:web", result_hash=result_hash, expected_task_version=web_task.version, accepted_at=NOW + timedelta(seconds=3))
    profile_contract = CoordinatorTaskContract(
        goal="Analyze authorized resume evidence against accepted jobs",
        inputs={"normalized_job_requirements_ref": "artifact:env:web", "depends_on_task_id": web.task_id, "knowledge_scope": {"type": "resume", "user_id": "user:1", "project_id": "project:1", "knowledge_base_id": "kb:1"}, "candidate_chunk_ids": ["chunk:1"], "top_k": 5, "output_schema_version": "profile-evidence-output-v1"},
        requested_allowed_tools=("retrieve_resume_evidence",), requested_deadline=NOW + timedelta(minutes=10), requested_budget=_limits(10), failure_behavior="allow_partial", idempotency_key="delegate:profile:11",
    )

    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="profile")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    coordinator.advance_phase(_parent().id, __import__("starter_agent.delegation.coordinator", fromlist=["CoordinatorPhase"]).CoordinatorPhase.VALIDATING)
    receipt = coordinator.delegate_profile_after_web(service=service, web_task_id=web.task_id, task_contract=profile_contract, context=context)

    profile_task = next(task for task in store.get_run_tree(_parent().id).child_tasks if task.id == receipt.task_id)
    assert profile_task.specialist_id == "profile_evidence_analyst"
    assert profile_task.inputs_ref_json["normalized_job_requirements_ref"] == "artifact:env:web"
    assert "normalized_job_requirements" not in profile_task.inputs_ref_json
    parent = store.get_parent(_parent().id)
    assert parent is not None and parent.status == parent.phase == "waiting_children"
    assert store.get_coordinator_checkpoint(_parent().id).parent_version == parent.version


def test_profile_delegation_rejects_legacy_inputs_before_creating_child_or_reserving_budget() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    registry = _registry(); service = DelegationService(store=store, registry=registry, now=lambda: NOW)
    coordinator = Coordinator(store=store, now=lambda: NOW, envelope_validator=_accept_authority)
    web = service.delegate_task(parent_run_id=_parent().id, specialist_id="job_web_researcher", task_contract=CoordinatorTaskContract.model_validate(_delegate_arguments(12)["task_contract"]))
    queued = store.get_run_tree(_parent().id).child_runs[0]
    running = store.claim_child_run(queued.id, worker_id="worker:1", lease_token="lease:1", expected_version=queued.version, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(minutes=1)); assert running
    result_hash = "c" * 64
    store.complete_child_run(running.id, target_status="succeeded", result_envelope_ref="artifact:env:web-legacy", result_hash=result_hash, worker_id="worker:1", lease_token="lease:1", expected_version=running.version, completed_at=NOW + timedelta(seconds=2))
    web_task = store.get_child_task(web.task_id); assert web_task
    store._accept_child_result(running.id, result_envelope_ref="artifact:env:web-legacy", result_hash=result_hash, expected_task_version=web_task.version, accepted_at=NOW + timedelta(seconds=3))
    coordinator.advance_phase(_parent().id, __import__("starter_agent.delegation.coordinator", fromlist=["CoordinatorPhase"]).CoordinatorPhase.VALIDATING)
    legacy_contract = CoordinatorTaskContract(
        goal="Analyze profile",
        inputs={"normalized_job_requirements_ref": "artifact:env:web-legacy", "knowledge_scope": {"scope_type": "resume", "scope_id": "resume:user:1"}, "candidate_chunk_ids": ["chunk:1"], "top_k": 5, "output_schema_version": "profile-evidence-output-v1"},
        requested_allowed_tools=("retrieve_resume_evidence",), requested_deadline=NOW + timedelta(minutes=10), requested_budget=_limits(10), failure_behavior="allow_partial", idempotency_key="delegate:profile:legacy",
    )
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="profile")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    before_tree = store.get_run_tree(_parent().id)
    before_parent = store.get_parent(_parent().id); assert before_parent

    with pytest.raises(ValueError, match="profile_input_schema_invalid"):
        coordinator.delegate_profile_after_web(service=service, web_task_id=web.task_id, task_contract=legacy_contract, context=context)

    after_tree = store.get_run_tree(_parent().id)
    after_parent = store.get_parent(_parent().id); assert after_parent
    assert len(after_tree.child_tasks) == len(before_tree.child_tasks) == 1
    assert len(after_tree.child_runs) == len(before_tree.child_runs) == 1
    assert after_parent.budget_reserved == before_parent.budget_reserved
    assert store.get_delegate_batch(_parent().id) is None


def test_profile_child_created_before_checkpoint_is_reconciled_from_durable_intent_after_restart() -> None:
    recovery_root = ROOT / ".session-task9-replay" / uuid4().hex; recovery_root.mkdir(parents=True)
    database = recovery_root / "profile-recovery.db"
    store = SQLiteRunStore(f"sqlite:///{database}", ROOT); store.create_parent(_parent())
    registry = _registry(); service = DelegationService(store=store, registry=registry, now=lambda: NOW)
    coordinator = Coordinator(store=store, now=lambda: NOW, envelope_validator=_accept_authority)
    web = service.delegate_task(parent_run_id=_parent().id, specialist_id="job_web_researcher", task_contract=CoordinatorTaskContract.model_validate(_delegate_arguments(81)["task_contract"]))
    queued = store.get_run_tree(_parent().id).child_runs[0]
    running = store.claim_child_run(queued.id, worker_id="worker:p", lease_token="lease:p", expected_version=queued.version, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(minutes=1)); assert running
    store.complete_child_run(running.id, target_status="succeeded", result_envelope_ref="artifact:env:profile-recovery", result_hash="f" * 64, worker_id="worker:p", lease_token="lease:p", expected_version=running.version, completed_at=NOW + timedelta(seconds=2))
    task = store.get_child_task(web.task_id); assert task
    store._accept_child_result(running.id, result_envelope_ref="artifact:env:profile-recovery", result_hash="f" * 64, expected_task_version=task.version, accepted_at=NOW + timedelta(seconds=3))
    coordinator.advance_phase(_parent().id, __import__("starter_agent.delegation.coordinator", fromlist=["CoordinatorPhase"]).CoordinatorPhase.VALIDATING)
    profile_contract = CoordinatorTaskContract(goal="Analyze profile", inputs={"normalized_job_requirements_ref": "artifact:env:profile-recovery", "depends_on_task_id": web.task_id, "knowledge_scope": {"type": "resume", "user_id": "user:1", "project_id": "project:1", "knowledge_base_id": "kb:1"}, "candidate_chunk_ids": ["chunk:1"], "top_k": 5, "output_schema_version": "profile-evidence-output-v1"}, requested_allowed_tools=("retrieve_resume_evidence",), requested_deadline=NOW + timedelta(minutes=10), requested_budget=_limits(10), failure_behavior="allow_partial", idempotency_key="delegate:profile:recovery")
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="profile")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    original_mark = coordinator.mark_delegate_call_completed
    coordinator.mark_delegate_call_completed = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt("crash after child creation"))
    with pytest.raises(KeyboardInterrupt):
        coordinator.delegate_profile_after_web(service=service, web_task_id=web.task_id, task_contract=profile_contract, context=context)
    assert len(store.get_run_tree(_parent().id).child_runs) == 2
    batch = store.get_delegate_batch(_parent().id); assert batch and batch.completed_call_ids == ()
    store.close()

    reopened = SQLiteRunStore(f"sqlite:///{database}", ROOT)
    recovered = Coordinator(store=reopened, now=lambda: NOW, envelope_validator=_accept_authority)
    receipt = recovered.reconcile_profile_intent(service=DelegationService(store=reopened, registry=registry, now=lambda: NOW), parent_run_id=_parent().id)

    assert receipt.child_run_id
    assert len(reopened.get_run_tree(_parent().id).child_runs) == 2
    assert reopened.get_delegate_batch(_parent().id).completed_call_ids == (reopened.get_delegate_batch(_parent().id).calls[0]["id"],)
    parent = reopened.get_parent(_parent().id); assert parent and parent.status == parent.phase == "waiting_children"


def test_profile_delegation_rejects_queued_parent() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent().model_copy(update={"status": "queued", "phase": "children_terminal"}))
    coordinator = Coordinator(store=store, now=lambda: NOW, envelope_validator=_accept_authority)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    with pytest.raises(ValueError, match="coordinator_parent_status_invalid"):
        coordinator.delegate_profile_after_web(service=None, web_task_id="task:none", task_contract=None, context=context)


def test_profile_delegation_rejects_cross_parent_context() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT)
    store.create_parent(_parent())
    other = _parent().model_copy(update={"id": "parent:other", "session_id": "session:other", "origin_turn_id": "turn:other", "principal": "user:other", "phase": "validating"})
    store.create_parent(other)
    service = DelegationService(store=store, registry=_registry(), now=lambda: NOW)
    web = service.delegate_task(parent_run_id=_parent().id, specialist_id="job_web_researcher", task_contract=CoordinatorTaskContract.model_validate(_delegate_arguments(51)["task_contract"]))
    queued = store.get_run_tree(_parent().id).child_runs[0]
    running = store.claim_child_run(queued.id, worker_id="worker:x", lease_token="lease:x", expected_version=queued.version, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(minutes=1)); assert running
    store.complete_child_run(running.id, target_status="succeeded", result_envelope_ref="artifact:env:foreign", result_hash="d" * 64, worker_id="worker:x", lease_token="lease:x", expected_version=running.version, completed_at=NOW + timedelta(seconds=2))
    task = store.get_child_task(web.task_id); assert task
    store._accept_child_result(running.id, result_envelope_ref="artifact:env:foreign", result_hash="d" * 64, expected_task_version=task.version, accepted_at=NOW + timedelta(seconds=3))
    context = RunContext(run_id=other.id, parent_run_id=other.id, session_id=uuid4(), turn_id=uuid4(), principal="user:other", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=other.id))
    coordinator = Coordinator(store=store, now=lambda: NOW, envelope_validator=_accept_authority)
    with pytest.raises(ValueError, match="accepted_web_result_required"):
        coordinator.delegate_profile_after_web(service=service, web_task_id=web.task_id, task_contract=None, context=context)


def test_loose_boolean_or_mismatched_hash_validator_cannot_inject_envelope() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ROOT); store.create_parent(_parent())
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="research")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=3, tool_calls=3), trace_context=RunTraceContext(parent_run_id=_parent().id))
    loose = Coordinator(store=store, now=lambda: NOW, envelope_validator=lambda _authority: True)
    loose.persist_checkpoint(context)
    assert loose.resume_context(_parent().id).working_memory["validated_child_results"] == []


@pytest.mark.asyncio
async def test_parent_runtime_rejects_forged_hidden_specialist_tool_call() -> None:
    class HiddenTool(Tool):
        name = "search_jobs_serpapi"; description = "hidden"; risk_level = "read"; input_schema = {"type": "object"}
        def __init__(self): self.calls = 0
        async def execute(self, arguments, context): self.calls += 1; return ToolResult(ok=True)
    class VisibleTool(HiddenTool):
        name = "delegate_task"
    hidden = HiddenTool(); visible = VisibleTool(); tools = ToolRegistry([]); tools._tools = {hidden.name: hidden, visible.name: visible}
    provider = _Provider()
    schemas = []
    async def forged(*args, **kwargs):
        schemas.append({item["function"]["name"] for item in args[2]})
        provider.calls += 1
        return ModelResponse(provider="fixture", model="fixture", usage={"total_tokens": 1, "cost_microunits": 1}, tool_calls=[ToolCall(id="forged", name=hidden.name, arguments={"query": "Agent engineer Sydney"})]) if provider.calls == 1 else ModelResponse(provider="fixture", model="fixture", content="done", usage={"total_tokens": 1, "cost_microunits": 1})
    provider.complete = forged
    runtime = AgentRuntime(tools, ToolPolicy(["read"]), RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=5), provider_resolver=lambda _: provider)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=["delegate_task"], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=2, tool_calls=2), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=("delegate_task",), max_steps=2, runtime_revision="r")

    await runtime.run(spec=spec, context=context)

    assert hidden.calls == 0


@pytest.mark.asyncio
async def test_mixed_delegate_batch_is_rejected_before_any_tool_side_effect() -> None:
    class CountingTool(Tool):
        name = "delegate_task"; description = "count"; risk_level = "read"; input_schema = {"type": "object"}
        def __init__(self): self.calls = 0
        async def execute(self, arguments, context): self.calls += 1; return ToolResult(ok=True)
    class OtherTool(CountingTool): name = "inspect_delegated_results"
    delegate, other = CountingTool(), OtherTool(); tools = ToolRegistry([]); tools._tools = {delegate.name: delegate, other.name: other}
    provider = _Provider()
    async def mixed(messages, model, tools, **kwargs):
        return ModelResponse(provider="fixture", model="fixture", usage={"total_tokens": 1, "cost_microunits": 1}, tool_calls=[ToolCall(id="m1", name="delegate_task", arguments={}), ToolCall(id="m2", name=other.name, arguments={})])
    provider.complete = mixed
    runtime = AgentRuntime(tools, ToolPolicy(["read"]), RuntimeConfig(max_model_calls=1, max_tool_calls=2, max_seconds=5), provider_resolver=lambda _: provider)
    context = RunContext(run_id=_parent().id, parent_run_id=_parent().id, session_id=uuid4(), turn_id=uuid4(), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=[delegate.name, other.name], budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=10000, model_calls=1, tool_calls=2), trace_context=RunTraceContext(parent_run_id=_parent().id))
    spec = RunSpec(run_id=_parent().id, run_kind="parent", role="coordinator", provider="fixture", model="fixture", system_prompt_ref="p", output_schema_ref="s", allowed_tools=(delegate.name, other.name), max_steps=1, runtime_revision="r")
    with pytest.raises(ValueError, match="mixed_delegate_batch_forbidden"):
        await runtime.run(spec=spec, context=context)
    assert delegate.calls == other.calls == 0
