from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.capabilities.gate import ToolExecutionDenied
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.capabilities.gate import PreToolCallGate
from starter_agent.delegation.models import BudgetLimits, ParentRun
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.service import DelegationService
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.delegation.tools import DelegateTaskTool
from starter_agent.tools.base import ToolContext
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import RunSpec
from starter_agent.domain.models import Message, ModelResponse
from starter_agent.agent.runtime import AgentRuntime
from starter_agent.settings import RuntimeConfig
from starter_agent.tools.policy import ToolPolicy


ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 12, 8, tzinfo=UTC)


def _limits(value: int) -> BudgetLimits:
    return BudgetLimits(tokens=value, cost_microunits=value, wall_clock_ms=value, model_calls=value, tool_calls=value)


def _tool(name: str) -> DelegateTaskTool:
    test_root = ROOT / ".session-only-delegate-task-gate-tests" / f"{name}-{uuid4().hex}"
    test_root.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(f"sqlite:///{test_root / 'runs.db'}", ROOT)
    store.create_parent(ParentRun(
        id="parent:gate:001", session_id="session:001", origin_turn_id="turn:001",
        principal="user:001", coordinator_spec_version="1", runtime_revision="runtime:001",
        available_at=NOW, deadline_at=NOW + timedelta(minutes=20), budget_total=_limits(500_000),
        budget_reserved=_limits(0), budget_consumed=_limits(0), route="job_application_delegation",
        created_at=NOW, updated_at=NOW,
    ))
    registry = SpecialistRegistry(ROOT / "config" / "specialists", project_root=ROOT, dependency_resolver=lambda _: True)
    registry.reload()
    return DelegateTaskTool(DelegationService(store=store, registry=registry, now=lambda: NOW))


def _arguments() -> dict:
    requested = BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=10, tool_calls=20)
    return {
        "specialist_id": "job_web_researcher",
        "task_contract": {
            "goal": "Research Sydney Agent engineering jobs",
            "inputs": {"query": "Agent engineer Sydney", "target_fields": ["title"], "max_pages": 1, "stop_conditions": {}, "output_schema_version": "job-web-output-v1"},
            "constraints": {}, "requested_allowed_tools": ["search_jobs_serpapi"],
            "requested_deadline": (NOW + timedelta(minutes=10)).isoformat(),
            "requested_budget": requested.model_dump(mode="json"),
            "failure_behavior": "allow_partial", "idempotency_key": "delegate:web:gate", "contract_version": "1",
        },
    }


async def test_only_coordinator_parent_context_can_call_delegate_task() -> None:
    tool = _tool("roles")
    coordinator = ToolContext(session_id=uuid4(), turn_id=uuid4(), parent_run_id="parent:gate:001", run_role="coordinator")
    result = await tool.execute(_arguments(), coordinator)
    assert result.ok is True
    assert "output" not in result.data and "jobs" not in result.data

    child = ToolContext(session_id=uuid4(), turn_id=uuid4(), parent_run_id="parent:gate:001", child_task_id="task:evil", child_run_id="child:evil", run_role="specialist")
    ordinary = ToolContext(session_id=uuid4(), turn_id=uuid4())
    for forbidden in (child, ordinary):
        with pytest.raises(ToolExecutionDenied, match="delegate_task_coordinator_only"):
            await tool.execute(_arguments(), forbidden)


async def test_adapter_rejects_runtime_owned_fields_even_for_coordinator() -> None:
    tool = _tool("runtime-owned")
    context = ToolContext(session_id=uuid4(), turn_id=uuid4(), parent_run_id="parent:gate:001", run_role="coordinator")
    injected = _arguments()
    injected["task_contract"]["task_id"] = "forged"
    with pytest.raises(ToolExecutionDenied, match="delegate_task_invalid_arguments"):
        await tool.execute(injected, context)


async def test_pre_tool_call_gate_rejects_non_coordinator_delegate_request() -> None:
    tool = _tool("gate")

    class _BuiltinView:
        email_manager = None

        def list(self):
            return [tool]

    registry = UnifiedToolRegistry(_BuiltinView())
    gate = PreToolCallGate(CapabilityStore("sqlite:///:memory:", project_root=ROOT), registry=registry)
    request = gate.request_for_tool(
        caller="model", session_id="session:001", turn_id="turn:001", call_id="call:001",
        tool_name="delegate_task", arguments=_arguments(), role="specialist",
    )
    decision = await gate.evaluate(request)

    assert decision.outcome == "deny"
    assert decision.reason_code == "delegate_task_coordinator_only"

    coordinator = request.model_copy(update={"role": "coordinator"})
    coordinator_decision = await gate.evaluate(coordinator)
    assert coordinator_decision.reason_code != "delegate_task_coordinator_only"


async def test_runtime_rejects_spec_context_kind_mismatch() -> None:
    class _Provider:
        async def complete(self, **_kwargs):
            return ModelResponse(content="unused", provider="mock", model="mock")

    runtime = AgentRuntime(
        tools=type("EmptyTools", (), {"list": lambda self: [], "get": lambda self, _name: None, "schemas": lambda self: []})(),
        policy=ToolPolicy({"read", "write"}),
        provider_resolver=lambda _name: _Provider(),
        budget=RuntimeConfig(max_model_calls=1, max_tool_calls=1, max_seconds=1, tool_timeout_seconds=1),
    )
    context = RunContext(
        run_id="run:forged", parent_run_id="parent:forged", child_task_id=None,
        session_id=uuid4(), turn_id=uuid4(), principal="user:001", messages=[Message(role="user", content="x")],
        effective_tool_view=[], budget_limits=_limits(10), trace_context=RunTraceContext(parent_run_id="parent:forged"),
    )
    specialist_spec = RunSpec(
        run_id="run:forged", run_kind="child", role="specialist", provider="mock", model="mock",
        system_prompt_ref="prompt:x", output_schema_ref="schema:x", max_steps=1, runtime_revision="runtime:1",
    )

    with pytest.raises(ValueError, match="run kind"):
        await runtime.run(spec=specialist_spec, context=context)
