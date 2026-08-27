from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import pytest

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, RunSpec
from starter_agent.domain.models import Message, ModelResponse, ToolResult
from starter_agent.providers.base import Provider
from starter_agent.settings import RuntimeConfig
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry
from starter_agent.delegation.tool_view import build_effective_tool_view
from starter_agent.capabilities.registry import UnifiedToolRegistry


class _Tool(Tool):
    description = "fixture"
    input_schema = {"type": "object", "additionalProperties": False}
    risk_level = "read"

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(ok=True, data={})


class _Provider(Provider):
    name = "capture"

    def __init__(self) -> None:
        self.tool_names: list[set[str]] = []

    async def complete(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | None = None,
        context_revision: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        self.tool_names.append({item["function"]["name"] for item in tools})
        return ModelResponse(
            content="done",
            provider=self.name,
            model=model,
            usage={"total_tokens": 1, "cost_microunits": 1},
        )

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def _context(run_id: str, tools: tuple[str, ...]) -> RunContext:
    return RunContext(
        run_id=run_id,
        parent_run_id="parent:001",
        child_task_id=f"task:{run_id}",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:001",
        messages=[Message(role="user", content="fixture")],
        effective_tool_view=list(tools),
        budget_limits=BudgetLimits(tokens=1000, cost_microunits=100, wall_clock_ms=1000, model_calls=2, tool_calls=2),
        trace_context=RunTraceContext(parent_run_id="parent:001", child_task_id=f"task:{run_id}", child_run_id=run_id),
    )


def _spec(run_id: str, tools: tuple[str, ...]) -> RunSpec:
    return RunSpec(
        run_id=run_id,
        run_kind="child",
        role="specialist",
        provider="capture",
        model="fixture",
        system_prompt_ref="prompt:v1",
        output_schema_ref="schema:v1",
        allowed_tools=tools,
        max_steps=1,
        runtime_revision="runtime:v1",
    )


def _parent_context(tools: tuple[str, ...]) -> RunContext:
    return RunContext(
        run_id="parent:001",
        parent_run_id="parent:001",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:001",
        messages=[Message(role="user", content="coordinate")],
        effective_tool_view=list(tools),
        budget_limits=BudgetLimits(tokens=1000, cost_microunits=100, wall_clock_ms=1000, model_calls=2, tool_calls=2),
        trace_context=RunTraceContext(parent_run_id="parent:001"),
    )


def _parent_spec(tools: tuple[str, ...]) -> RunSpec:
    return RunSpec(
        run_id="parent:001",
        run_kind="parent",
        role="coordinator",
        provider="capture",
        model="fixture",
        system_prompt_ref="coordinator:v1",
        output_schema_ref="coordinator-schema:v1",
        allowed_tools=tools,
        max_steps=1,
        runtime_revision="runtime:v1",
    )


@pytest.mark.asyncio
async def test_each_child_model_request_only_receives_its_effective_tool_schema() -> None:
    registry = ToolRegistry([])
    names = (
        "search_jobs_serpapi",
        "mcp__playwright__browser_navigate",
        "retrieve_resume_evidence",
        "delegate_task",
    )
    registry._tools = {name: _Tool(name) for name in names}
    provider = _Provider()
    runtime = AgentRuntime(
        registry,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=2),
        provider_resolver=lambda _name: provider,
    )
    web_tools = ("search_jobs_serpapi", "mcp__playwright__browser_navigate")
    profile_tools = ("retrieve_resume_evidence",)

    await runtime.run(_spec("child:web", web_tools), _context("child:web", web_tools))
    await runtime.run(_spec("child:profile", profile_tools), _context("child:profile", profile_tools))

    assert provider.tool_names == [set(web_tools), set(profile_tools)]
    assert all("delegate_task" not in item for item in provider.tool_names)


@pytest.mark.asyncio
async def test_child_cannot_execute_tool_omitted_from_effective_view() -> None:
    from starter_agent.domain.models import ToolCall

    class _MaliciousProvider(_Provider):
        async def complete(self, messages, model, tools, **kwargs):
            self.tool_names.append({item["function"]["name"] for item in tools})
            if len(self.tool_names) == 1:
                return ModelResponse(
                    content="",
                    tool_calls=[ToolCall(id="hidden:1", name="delegate_task", arguments={})],
                    provider=self.name,
                    model=model,
                    usage={"total_tokens": 1, "cost_microunits": 1},
                )
            return ModelResponse(
                content="done",
                provider=self.name,
                model=model,
                usage={"total_tokens": 1, "cost_microunits": 1},
            )

    registry = ToolRegistry([])
    visible = _Tool("search_jobs_serpapi")
    hidden = _Tool("delegate_task")
    registry._tools = {visible.name: visible, hidden.name: hidden}
    provider = _MaliciousProvider()
    runtime = AgentRuntime(
        registry,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=2),
        provider_resolver=lambda _name: provider,
    )
    tools = (visible.name,)

    outcome = await runtime.run(
        _spec("child:malicious", tools).model_copy(update={"max_steps": 2}),
        _context("child:malicious", tools),
    )

    assert outcome.status == "succeeded"
    assert hidden.calls == 0
    assert provider.tool_names == [{visible.name}, {visible.name}]


@pytest.mark.asyncio
async def test_run_uses_pinned_filtered_schema_after_shared_registry_changes() -> None:
    registry = ToolRegistry([])
    visible = _Tool("search_jobs_serpapi")
    hidden = _Tool("delegate_task")
    registry._tools = {visible.name: visible, hidden.name: hidden}
    unified = UnifiedToolRegistry(registry)
    view = build_effective_tool_view(
        unified,
        registry_allowed={visible.name},
        contract_requested={visible.name},
        scenario_allowed={visible.name},
        policy_allowed={visible.name},
    )
    context = _context("child:pinned", (visible.name,))
    context.tool_view = view
    provider = _Provider()
    runtime = AgentRuntime(
        unified,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=2),
        provider_resolver=lambda _name: provider,
    )

    unified.set_tool_enabled(visible.name, False)
    outcome = await runtime.run(_spec(context.run_id, (visible.name,)), context)

    assert outcome.status == "failed"
    assert outcome.error_code == "runtime_tool_view_stale"
    assert provider.tool_names == []


@pytest.mark.asyncio
async def test_checkpoint_restore_keeps_pinned_filtered_schema() -> None:
    registry = ToolRegistry([])
    visible = _Tool("search_jobs_serpapi")
    registry._tools = {visible.name: visible}
    unified = UnifiedToolRegistry(registry)
    view = build_effective_tool_view(
        unified,
        registry_allowed={visible.name},
        contract_requested={visible.name},
        scenario_allowed={visible.name},
        policy_allowed={visible.name},
    )
    context = _context("child:restored", (visible.name,))
    context.tool_view = view
    restored = RunContext.from_checkpoint(context.to_checkpoint())
    provider = _Provider()
    runtime = AgentRuntime(
        unified,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=2),
        provider_resolver=lambda _name: provider,
    )

    unified.set_tool_enabled(visible.name, False)
    outcome = await runtime.run(_spec(restored.run_id, (visible.name,)), restored)

    assert outcome.status == "failed"
    assert outcome.error_code == "runtime_tool_view_stale"
    assert provider.tool_names == []


@pytest.mark.asyncio
async def test_coordinator_empty_view_does_not_fall_back_to_full_registry() -> None:
    registry = ToolRegistry([])
    registry._tools = {
        name: _Tool(name)
        for name in ("search_jobs_serpapi", "retrieve_resume_evidence")
    }
    provider = _Provider()
    runtime = AgentRuntime(
        registry,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=2),
        provider_resolver=lambda _name: provider,
    )

    await runtime.run(_parent_spec(()), _parent_context(()))

    assert provider.tool_names == [set()]


@pytest.mark.asyncio
async def test_schema_change_during_model_round_stops_before_tool_execution() -> None:
    from starter_agent.domain.models import ToolCall

    registry = ToolRegistry([])
    visible = _Tool("search_jobs_serpapi")
    registry._tools = {visible.name: visible}
    unified = UnifiedToolRegistry(registry)
    view = build_effective_tool_view(
        unified,
        registry_allowed={visible.name},
        contract_requested={visible.name},
        scenario_allowed={visible.name},
        policy_allowed={visible.name},
    )
    context = _context("child:schema-refresh", (visible.name,))
    context.tool_view = view
    context.tool_schema_snapshot = view.model_snapshot()

    class _RefreshProvider(_Provider):
        async def complete(self, messages, model, tools, **kwargs):
            self.tool_names.append({item["function"]["name"] for item in tools})
            visible.input_schema = {
                "type": "object",
                "properties": {"changed": {"type": "string"}},
                "additionalProperties": False,
            }
            return ModelResponse(
                content="",
                tool_calls=[ToolCall(id="call:refresh", name=visible.name, arguments={})],
                provider=self.name,
                model=model,
                usage={"total_tokens": 1, "cost_microunits": 1},
            )

    provider = _RefreshProvider()
    runtime = AgentRuntime(
        unified,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=2),
        provider_resolver=lambda _name: provider,
    )

    outcome = await runtime.run(
        _spec(context.run_id, (visible.name,)).model_copy(update={"max_steps": 2}),
        context,
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "runtime_tool_view_stale"
    assert visible.calls == 0
