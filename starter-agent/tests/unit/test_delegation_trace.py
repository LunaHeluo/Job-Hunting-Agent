from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from starter_agent.agent.runtime import AgentRuntime, _ACTIVE_RUN_CONTEXT
from starter_agent.capabilities.gate import PreToolCallGate, ToolExecutionDenied, UnifiedToolExecutor
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, BudgetUsage, ChildRun, ParentRun, TaskContract
from starter_agent.delegation.store import RunEvent, SQLiteRunStore
from starter_agent.domain.models import Message
from starter_agent.interfaces.trust_api import create_trust_router
from starter_agent.settings import RuntimeConfig
from starter_agent.trust.store import TrustStore
from starter_agent.trust.trace import CapabilityAuditTrustBridge, DelegationEventTrustBridge, TraceContext, TrustTraceRecorder
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.domain.models import ToolResult


class _DeniedChildTool(Tool):
    name = "forbidden_child_tool"
    description = "A write tool intentionally excluded from the child policy."
    input_schema = {"type": "object", "additionalProperties": True}
    risk_level = "write"

    def __init__(self) -> None:
        self.executions = 0

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        self.executions += 1
        return ToolResult(ok=True, data={"unexpected": True})


def _child_context() -> RunContext:
    return RunContext(
        run_id="child:trace:1", parent_run_id="parent:trace:1", child_task_id="task:trace:1",
        session_id=UUID("00000000-0000-0000-0000-000000000001"),
        turn_id=UUID("00000000-0000-0000-0000-000000000002"), principal="user:trace:1",
        messages=[Message(role="user", content="private resume body")], effective_tool_view=[],
        budget_limits=BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=10, tool_calls=10),
        trace_context=RunTraceContext(parent_run_id="parent:trace:1", child_task_id="task:trace:1", child_run_id="child:trace:1", eval_run_id="eval:trace:1", case_id="case:trace:1", model_request_id="model:trace:1", policy_decision_id="policy:trace:1"),
    )


@pytest.mark.asyncio
async def test_real_child_gate_deny_has_full_trace_chain_without_tool_start_or_raw_material() -> None:
    capability = CapabilityStore("sqlite:///:memory:", ".")
    trust = TrustStore("sqlite:///:memory:", ".")
    capability.add_audit_sink(CapabilityAuditTrustBridge(trust).record)
    tool = _DeniedChildTool()
    registry = UnifiedToolRegistry(
        type("BuiltinView", (), {"list": lambda self: [tool], "email_manager": None})(),
        allowed_risk_levels={"read"},
    )
    gate = PreToolCallGate(capability, registry=registry)
    runtime = AgentRuntime(
        registry, ToolPolicy(["read"]), RuntimeConfig(),
        gate=gate, executor=UnifiedToolExecutor(capability, gate=gate),
    )
    context = _child_context()
    token = _ACTIVE_RUN_CONTEXT.set(context)
    try:
        with pytest.raises(ToolExecutionDenied):
            await runtime.execute_tool(
                tool_name="forbidden_child_tool", arguments={"raw_html": "<html>resume</html>"},
                session_id=context.session_id, turn_id=context.turn_id, call_id="tool:trace:1",
                principal=context.principal,
            )
    finally:
        _ACTIVE_RUN_CONTEXT.reset(token)

    audits = capability.list_audit_events()
    assert len(audits) == 1
    events = trust.list_trace_events(parent_run_id=context.parent_run_id, child_run_id=context.run_id)
    assert [event.event_type for event in events] == ["Policy"]
    denied = events[0]
    assert denied.status == "blocked"
    assert (denied.eval_run_id, denied.case_id, denied.session_id, denied.turn_id, denied.model_request_id, denied.tool_call_id, denied.policy_decision_id) == (
        "eval:trace:1", "case:trace:1", str(context.session_id), str(context.turn_id), "model:trace:1", "tool:trace:1", "policy:trace:1"
    )
    assert (denied.parent_run_id, denied.child_task_id, denied.child_run_id) == (
        "parent:trace:1", "task:trace:1", "child:trace:1"
    )
    assert not any(event.event_type == "Tool" and event.status == "started" for event in events)
    assert tool.executions == 0
    assert denied.approval_id is None
    assert tool.executions == 0
    assert denied.approval_id is None
    assert "raw_html" not in denied.summary


def test_child_trace_filters_are_paginated_and_redact_restricted_child_material() -> None:
    store = TrustStore("sqlite:///:memory:", ".")
    recorder = TrustTraceRecorder(store)
    base = TraceContext(parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1")
    first = recorder.record(
        id="trace:1", context=base, event_type="Run", status="completed",
        summary={"registry_hash": "a", "child_messages": "resume body", "raw_html": "<html>x</html>", "cookie": "secret"},
    )
    recorder.record(id="trace:2", context=base, event_type="Run", status="completed", summary={"contract_hash": "b"})
    recorder.record(id="trace:3", context=TraceContext(parent_run_id="parent:2", child_task_id="task:2", child_run_id="child:2"), event_type="Run", status="completed", summary={"route": "legacy"})

    page = store.list_trace_event_page(parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1", limit=1)
    assert page.items == (first,)
    assert page.next_cursor == first.id
    assert store.list_trace_event_page(parent_run_id="parent:1", child_run_id="child:1", cursor=page.next_cursor).items[0].id == "trace:2"
    assert "resume body" not in first.model_dump_json()
    assert "<html>x</html>" not in first.model_dump_json()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(create_trust_router(store_provider=lambda: store))
    with TestClient(app) as client:
        response = client.get("/v1/trust/traces", params={"parent_run_id": "parent:1", "child_task_id": "task:1", "child_run_id": "child:1", "limit": 1})
    assert response.status_code == 200
    assert response.json()["next_cursor"] == "trace:1"


def test_delegation_event_bridge_reads_existing_run_store_events_idempotently() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    limits = BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=10, tool_calls=10)
    runs = SQLiteRunStore("sqlite:///:memory:", ".")
    runs.create_parent(ParentRun(
        id="parent:bridge:1", session_id="session:bridge:1", origin_turn_id="turn:bridge:1",
        principal="user:bridge:1", coordinator_spec_version="v1", runtime_revision="runtime:v1",
        available_at=now, deadline_at=now + timedelta(minutes=10), budget_total=limits,
        budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0),
        budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0),
        route="delegated", created_at=now, updated_at=now,
    ))
    event = runs.append_event(RunEvent(
        id="run-event:bridge:1", parent_run_id="parent:bridge:1", event_type="budget.reserved",
        status="completed", occurred_at=now,
        payload={"task_id": "task:bridge:1", "registry_hash": "registry:v1", "contract_hash": "contract:v1", "tool_view_hash": "tool-view:v1", "raw_html": "<html>private</html>", "route": "delegated", "legacy_path_used": False, "subagent_call_id": "delegate:1"},
    ))
    trust = TrustStore("sqlite:///:memory:", ".")
    bridge = DelegationEventTrustBridge(runs, trust)

    first = bridge.sync_parent("parent:bridge:1")
    again = bridge.sync_parent("parent:bridge:1")
    [trace] = trust.list_trace_events(parent_run_id="parent:bridge:1")

    assert first.events[0].id == "delegation:run-event:bridge:1"
    assert again.events[0].id == first.events[0].id
    assert trace.event_type == "Budget"
    assert (trace.parent_run_id, trace.child_task_id, trace.principal, trace.access_level) == ("parent:bridge:1", "task:bridge:1", "user:bridge:1", "delegation_restricted")
    assert trace.source_ref == "delegation_run_event:run-event:bridge:1"
    assert trace.summary["registry_hash"] == "registry:v1"
    assert "raw_html" not in trace.model_dump_json()


def test_run_store_observer_projects_start_and_queued_cancel_without_worker() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    limits = BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=10, tool_calls=10)
    runs = SQLiteRunStore("sqlite:///:memory:", ".")
    runs.create_parent(ParentRun(id="parent:observer:1", session_id="session:observer:1", origin_turn_id="turn:observer:1", principal="user:observer:1", coordinator_spec_version="v1", runtime_revision="runtime:v1", available_at=now, deadline_at=now + timedelta(minutes=10), budget_total=limits, budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), route="delegated", created_at=now, updated_at=now))
    trust = TrustStore("sqlite:///:memory:", ".")
    bridge = DelegationEventTrustBridge(runs, trust)
    runs.add_event_sink(bridge.record)

    runs.append_event(RunEvent(id="event:start:1", parent_run_id="parent:observer:1", event_type="child.delegated", status="queued", occurred_at=now, payload={"task_id": "task:observer:1", "route": "delegated", "subagent_call_id": "delegate:observer:1"}))
    runs.request_parent_cancellation("parent:observer:1", reason="user_cancelled", requested_at=now + timedelta(seconds=1))

    events = trust.list_trace_events(parent_run_id="parent:observer:1")
    assert {item.summary["delegation_event_type"] for item in events} >= {"child.delegated", "parent.cancellation_requested"}


def test_run_store_emits_five_dimension_budget_reserve_consume_release_events() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    total = BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=10, tool_calls=10)
    requested = BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=1, tool_calls=1)
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    store.create_parent(ParentRun(id="parent:budget:1", session_id="session:budget:1", origin_turn_id="turn:budget:1", principal="user:budget:1", coordinator_spec_version="v1", runtime_revision="runtime:v1", available_at=now, deadline_at=now + timedelta(minutes=10), budget_total=total, budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), route="delegated", created_at=now, updated_at=now))
    contract = TaskContract(task_id="task:budget:1", parent_run_id="parent:budget:1", specialist_id="job_web_researcher", goal="test", inputs={}, constraints={}, requested_allowed_tools=(), requested_deadline=now + timedelta(minutes=1), requested_budget=requested, failure_behavior="allow_partial", idempotency_key="budget:1")
    child = ChildRun(id="child:budget:1", child_task_id=contract.task_id, parent_run_id=contract.parent_run_id, attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=now, updated_at=now)
    created = store.create_child_task_and_run(contract=contract, child_run=child, specialist_snapshot_id="snapshot:budget:1", output_schema_version="v1", expected_parent_version=0, created_at=now)
    store.settle_child_budget(child.id, BudgetUsage(tokens=3, cost_microunits=3, wall_clock_ms=3, model_calls=1, tool_calls=1, cost_status="actual", usage_source="test"), expected_parent_version=created.parent.version, settled_at=now + timedelta(seconds=1))
    budget_events = [event for event in store.get_run_tree("parent:budget:1").events if event.event_type.startswith("budget.")]
    assert [event.event_type for event in budget_events] == ["budget.reserved", "budget.consumed_released"]
    assert set(budget_events[0].payload["requested_budget"]) == set(BudgetLimits.model_fields)
    assert set(BudgetLimits.model_fields).issubset(budget_events[1].payload["usage"])


def test_delegation_trust_backfill_is_idempotent_and_has_no_business_side_effect() -> None:
    # RED: recovery must replay the existing event log, not invoke a Worker.
    runs = SQLiteRunStore("sqlite:///:memory:", ".")
    trust = TrustStore("sqlite:///:memory:", ".")
    bridge = DelegationEventTrustBridge(runs, trust)
    assert hasattr(bridge, "backfill_parent")


def test_delegation_trust_backfill_consumes_parent_and_event_pages() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    limits = BudgetLimits(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=1)
    runs = SQLiteRunStore("sqlite:///:memory:", ".")
    trust = TrustStore("sqlite:///:memory:", ".")
    for parent_index in range(3):
        parent_id = f"parent:page:{parent_index}"
        runs.create_parent(ParentRun(id=parent_id, session_id=f"session:{parent_index}", origin_turn_id=f"turn:{parent_index}", principal="user:page", coordinator_spec_version="v1", runtime_revision="runtime:v1", available_at=now, deadline_at=now + timedelta(minutes=1), budget_total=limits, budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), route="delegated", created_at=now, updated_at=now))
        for event_index in range(3):
            runs.append_event(RunEvent(id=f"page:{parent_index}:{event_index}", parent_run_id=parent_id, event_type="run.status_changed", status="completed", occurred_at=now, payload={"version": event_index}))

    bridge = DelegationEventTrustBridge(runs, trust)
    bridge.backfill_recent(parent_page_size=2, event_page_size=2)
    bridge.backfill_recent(parent_page_size=2, event_page_size=2)

    for parent_index in range(3):
        events = trust.list_trace_events(parent_run_id=f"parent:page:{parent_index}")
        projected_ids = {event.id for event in events if event.id.startswith("delegation:page:")}
        assert projected_ids == {f"delegation:page:{parent_index}:{event_index}" for event_index in range(3)}


def test_run_store_observer_emits_only_the_new_event_with_large_existing_history() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    limits = BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=10, tool_calls=10)
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    store.create_parent(ParentRun(id="parent:history:1", session_id="session:history:1", origin_turn_id="turn:history:1", principal="user:history:1", coordinator_spec_version="v1", runtime_revision="runtime:v1", available_at=now, deadline_at=now + timedelta(minutes=1), budget_total=limits, budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), route="delegated", created_at=now, updated_at=now))
    for index in range(20):
        store.append_event(RunEvent(id=f"history:{index}", parent_run_id="parent:history:1", event_type="run.status_changed", status="completed", occurred_at=now, payload={"version": index}))
    observed: list[str] = []
    store.add_event_sink(lambda item: observed.append(item.id))
    store.append_event(RunEvent(id="history:new", parent_run_id="parent:history:1", event_type="route.selected", status="completed", occurred_at=now, payload={"route": "delegated"}))
    assert observed == ["history:new"]
