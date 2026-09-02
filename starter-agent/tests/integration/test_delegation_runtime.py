from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, RunOutcome, RunSpec
from starter_agent.domain.models import Message, ModelResponse
from starter_agent.providers.base import Provider
from starter_agent.settings import RuntimeConfig
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry


class _RecordingProvider(Provider):
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[int, list[Message], list[dict]]] = []

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
        await asyncio.sleep(0)
        self.calls.append((id(messages), list(messages), list(tools)))
        return ModelResponse(
            content=f"done:{messages[-1].content}",
            provider=self.name,
            model=model,
            usage={"total_tokens": 7, "cost_microunits": 1},
        )

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def _spec(run_id: str, role: str) -> RunSpec:
    return RunSpec(
        run_id=run_id,
        run_kind="parent" if role == "coordinator" else "child",
        role=role,
        provider="recording",
        model="fixture-model",
        system_prompt_ref=f"prompt:{role}:v1",
        output_schema_ref=f"schema:{role}:v1",
        allowed_tools=(),
        max_steps=3,
        runtime_revision="runtime-v1",
    )


def _context(run_id: str, role: str) -> RunContext:
    parent_id = run_id if role == "coordinator" else "parent:001"
    return RunContext(
        run_id=run_id,
        parent_run_id=parent_id,
        child_task_id=None if role == "coordinator" else f"task:{run_id}",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:001",
        messages=[Message(role="user", content=run_id)],
        effective_tool_view=[],
        budget_limits=BudgetLimits(
            tokens=10_000,
            cost_microunits=100,
            wall_clock_ms=10_000,
            model_calls=3,
            tool_calls=3,
        ),
        trace_context=RunTraceContext(
            parent_run_id=parent_id,
            child_task_id=None if role == "coordinator" else f"task:{run_id}",
            child_run_id=None if role == "coordinator" else run_id,
        ),
    )


@pytest.mark.asyncio
async def test_parent_and_children_share_runtime_loop_but_not_context_state() -> None:
    provider = _RecordingProvider()
    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda name: provider if name == "recording" else None,
    )
    parent = _context("parent:001", "coordinator")
    first = _context("child:001", "specialist")
    second = _context("child:002", "specialist")

    outcomes = await asyncio.gather(
        runtime.run(_spec(parent.run_id, "coordinator"), parent),
        runtime.run(_spec(first.run_id, "specialist"), first),
        runtime.run(_spec(second.run_id, "specialist"), second),
    )

    assert all(isinstance(item, RunOutcome) for item in outcomes)
    assert [item.status for item in outcomes] == ["succeeded"] * 3
    assert len({call[0] for call in provider.calls}) == 3
    assert {call[1][-1].content for call in provider.calls} == {
        "parent:001",
        "child:001",
        "child:002",
    }
    assert parent.output_buffer == ["done:parent:001"]
    assert first.output_buffer == ["done:child:001"]
    assert second.output_buffer == ["done:child:002"]
    assert all(context.budget.consumed.model_calls == 1 for context in (parent, first, second))
    assert all(context.budget.consumed.tokens == 7 for context in (parent, first, second))
    assert not (Path(__file__).parents[2] / "backend/src/starter_agent/agent/subagent_loop.py").exists()


@pytest.mark.asyncio
async def test_legacy_chat_signature_still_uses_same_runtime_loop() -> None:
    provider = _RecordingProvider()
    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=2, max_tool_calls=0, max_seconds=30),
    )
    messages = [Message(role="user", content="legacy")]

    response, generated, tool_calls = await runtime.run(
        provider,
        "fixture-model",
        messages,
        uuid4(),
        uuid4(),
        allow_tools=False,
    )

    assert response.content == "done:legacy"
    assert generated == []
    assert tool_calls == 0


@pytest.mark.asyncio
async def test_run_spec_returns_controlled_cancelled_and_budget_outcomes() -> None:
    provider = _RecordingProvider()
    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: provider,
    )
    cancelled = _context("child:cancelled", "specialist")
    cancelled.cancellation.request("parent cancelled")
    cancelled_outcome = await runtime.run(
        _spec(cancelled.run_id, "specialist"), cancelled
    )
    assert cancelled_outcome.disposition == "cancelled"
    assert cancelled_outcome.status == "cancelled"
    assert provider.calls == []

    exhausted = _context("child:exhausted", "specialist")
    exhausted.budget.limits = exhausted.budget.limits.model_copy(
        update={"model_calls": 0}
    )
    exhausted_outcome = await runtime.run(
        _spec(exhausted.run_id, "specialist"), exhausted
    )
    assert exhausted_outcome.disposition == "failed"
    assert exhausted_outcome.status == "budget_exhausted"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_durable_cancellation_after_model_response_stops_before_tool_execution() -> None:
    from starter_agent.domain.models import ToolCall

    provider = _RecordingProvider()
    probe_state = {"cancelled": False}

    async def complete_then_cancel(*_args, **_kwargs):
        probe_state["cancelled"] = True
        return ModelResponse(
            content="", provider=provider.name, model="fixture-model",
            tool_calls=[ToolCall(id="call:1", name="never_runs", arguments={})],
            usage={"total_tokens": 1, "cost_microunits": 1},
        )

    provider.complete = complete_then_cancel
    runtime = AgentRuntime(
        ToolRegistry([]), ToolPolicy(["read"]), RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=30),
        provider_resolver=lambda _name: provider,
    )
    context = _context("child:durable-cancel", "specialist")
    context.cancellation_probe = lambda: (1, probe_state["cancelled"])

    outcome = await runtime.run(_spec(context.run_id, "specialist"), context)

    assert outcome.status == "cancelled"
    assert outcome.error_code == "run_cancelled"
    assert context.budget.consumed.tool_calls == 0


@pytest.mark.asyncio
async def test_execute_tool_checks_durable_cancellation_before_gate() -> None:
    runtime = AgentRuntime(ToolRegistry([]), ToolPolicy(["read"]), RuntimeConfig())
    context = _context("child:gate-cancel", "specialist")
    context.cancellation_probe = lambda: (1, True)
    from starter_agent.agent import runtime as runtime_module

    token = runtime_module._ACTIVE_RUN_CONTEXT.set(context)
    try:
        with pytest.raises(Exception, match="Run cancelled"):
            await runtime.execute_tool(
                tool_name="not_registered", arguments={}, session_id=context.session_id,
                turn_id=context.turn_id, call_id="call:cancel",
            )
    finally:
        runtime_module._ACTIVE_RUN_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_cancellation_after_tool_return_is_not_wrapped_as_tool_execution_error() -> None:
    from starter_agent.domain.models import ToolCall, ToolResult
    from starter_agent.tools.base import Tool, ToolContext

    cancelled = {"value": False}

    class CancellingTool(Tool):
        name = "get_current_time"
        description = "Cancel after execution"
        input_schema = {"type": "object", "additionalProperties": False}
        risk_level = "read"

        async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
            cancelled["value"] = True
            return ToolResult(ok=True, data={"done": True})

    class Provider(_RecordingProvider):
        async def complete(self, messages, model, tools, **kwargs):
            return ModelResponse(
                content="", provider=self.name, model=model,
                tool_calls=[ToolCall(id="call:cancel-after-tool", name="get_current_time", arguments={})],
                usage={"total_tokens": 1, "cost_microunits": 1},
            )

    registry = ToolRegistry([])
    registry._tools = {"get_current_time": CancellingTool()}
    runtime = AgentRuntime(registry, ToolPolicy(["read"]), RuntimeConfig(max_model_calls=2, max_tool_calls=2, max_seconds=30), provider_resolver=lambda _name: Provider())
    context = _context("child:cancel-after-tool", "specialist")
    context.effective_tool_view = ["get_current_time"]
    context.cancellation_probe = lambda: (1, cancelled["value"])
    spec = _spec(context.run_id, "specialist").model_copy(update={"allowed_tools": ("get_current_time",)})

    outcome = await runtime.run(spec, context)

    assert outcome.status == "cancelled"
    assert outcome.error_code == "run_cancelled"
    assert all('tool_execution_error' not in message.content for message in context.messages)

@pytest.mark.asyncio
async def test_provider_usage_updates_run_scoped_token_cost_and_wall_clock_budget() -> None:
    provider = _RecordingProvider()

    async def complete_with_usage(*args, **kwargs):
        response = await _RecordingProvider.complete(provider, *args, **kwargs)
        response.usage = {"total_tokens": 7, "cost_microunits": 11}
        return response

    provider.complete = complete_with_usage
    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: provider,
    )
    context = _context("child:usage", "specialist")
    await runtime.run(_spec(context.run_id, "specialist"), context)

    assert context.budget.consumed.tokens == 7
    assert context.budget.consumed.cost_microunits == 11
    assert context.budget.consumed.model_calls == 1
    assert context.budget.consumed.wall_clock_ms >= 0


@pytest.mark.asyncio
async def test_provider_usage_over_limit_returns_controlled_budget_outcome() -> None:
    provider = _RecordingProvider()

    async def complete_over_budget(*args, **kwargs):
        response = await _RecordingProvider.complete(provider, *args, **kwargs)
        response.usage = {"total_tokens": 1_001, "cost_microunits": 1}
        return response

    provider.complete = complete_over_budget
    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: provider,
    )
    context = _context("child:over-budget", "specialist")
    context.budget.limits = context.budget.limits.model_copy(update={"tokens": 1_000})

    outcome = await runtime.run(_spec(context.run_id, "specialist"), context)

    assert outcome.disposition == "failed"
    assert outcome.status == "budget_exhausted"
    assert outcome.error_code == "runtime_budget_exceeded"
    assert context.budget.consumed.tokens == context.budget.limits.tokens
    assert context.budget.overage.tokens == 1


@pytest.mark.asyncio
async def test_missing_provider_cost_fails_closed_and_is_checkpointed() -> None:
    provider = _RecordingProvider()

    async def complete_without_cost(*args, **kwargs):
        response = await _RecordingProvider.complete(provider, *args, **kwargs)
        response.usage = {"total_tokens": 7}
        return response

    provider.complete = complete_without_cost
    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: provider,
    )
    context = _context("child:unknown-cost", "specialist")

    outcome = await runtime.run(_spec(context.run_id, "specialist"), context)

    assert outcome.status == "budget_exhausted"
    assert context.budget.cost_unknown is True
    assert context.budget.consumed.tokens == 7
    assert RunContext.from_checkpoint(context.to_checkpoint()).budget.cost_unknown is True


@pytest.mark.asyncio
async def test_run_spec_step_exhaustion_returns_outcome_instead_of_loop_exception() -> None:
    from starter_agent.domain.models import ToolCall, ToolResult
    from starter_agent.tools.base import Tool, ToolContext

    class TimeTool(Tool):
        name = "get_current_time"
        description = "Fixture"
        input_schema = {"type": "object", "additionalProperties": False}
        risk_level = "read"

        async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
            return ToolResult(ok=True, data={"now": "fixture"})

    class EndlessToolProvider(_RecordingProvider):
        async def complete(self, messages, model, tools, **kwargs):
            return ModelResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call:{len(messages)}",
                        name="get_current_time",
                        arguments={},
                    )
                ],
                provider=self.name,
                model=model,
                        usage={"total_tokens": 1, "cost_microunits": 1},
            )

    registry = ToolRegistry([])
    registry._tools = {"get_current_time": TimeTool()}
    runtime = AgentRuntime(
        registry,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: EndlessToolProvider(),
    )
    context = _context("child:step-limit", "specialist")
    spec = _spec(context.run_id, "specialist").model_copy(
        update={"allowed_tools": ("get_current_time",), "max_steps": 1}
    )

    outcome = await runtime.run(spec=spec, context=context)

    assert outcome.disposition == "failed"
    assert outcome.status == "budget_exhausted"
    assert outcome.error_code == "runtime_budget_exceeded"
    assert context.budget.consumed.wall_clock_ms > 0
    restored = RunContext.from_checkpoint(context.to_checkpoint())
    assert restored.budget.consumed.wall_clock_ms == context.budget.consumed.wall_clock_ms


@pytest.mark.asyncio
async def test_hung_provider_is_stopped_by_remaining_wall_clock_budget() -> None:
    class HungProvider(_RecordingProvider):
        async def complete(self, messages, model, tools, **kwargs):
            await asyncio.Event().wait()

    runtime = AgentRuntime(
        ToolRegistry([]),
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: HungProvider(),
    )
    context = _context("child:hung", "specialist")
    context.budget.limits = context.budget.limits.model_copy(
        update={"wall_clock_ms": 20}
    )

    outcome = await asyncio.wait_for(
        runtime.run(_spec(context.run_id, "specialist"), context), timeout=1
    )

    assert outcome.disposition == "failed"
    assert outcome.status == "timed_out"
    assert outcome.error_code == "runtime_timeout"


@pytest.mark.asyncio
async def test_concurrent_tool_context_uses_each_runs_trace_identity() -> None:
    from starter_agent.domain.models import ToolCall, ToolResult
    from starter_agent.tools.base import Tool, ToolContext

    seen: list[tuple[str | None, str | None, str | None]] = []

    class CaptureTool(Tool):
        # Reuse an existing auto-approved capability name so this test exercises
        # execution-context propagation rather than the confirmation workflow.
        name = "get_current_time"
        description = "Capture run identity"
        input_schema = {"type": "object", "additionalProperties": False}
        risk_level = "read"

        async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
            seen.append(
                (context.parent_run_id, context.child_task_id, context.child_run_id)
            )
            await asyncio.sleep(0)
            return ToolResult(ok=True, data={"captured": True})

    class ToolProvider(_RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.counts: dict[str, int] = {}

        async def complete(self, messages, model, tools, **kwargs):
            run_id = messages[0].content
            count = self.counts.get(run_id, 0)
            self.counts[run_id] = count + 1
            if count == 0:
                return ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"call:{run_id}", name="get_current_time", arguments={}
                        )
                    ],
                    provider=self.name,
                    model=model,
                    usage={"total_tokens": 1, "cost_microunits": 1},
                )
            return ModelResponse(
                content=f"done:{run_id}",
                provider=self.name,
                model=model,
                usage={"total_tokens": 1, "cost_microunits": 1},
            )

    provider = ToolProvider()
    fixture_registry = ToolRegistry([])
    fixture_registry._tools = {"get_current_time": CaptureTool()}
    runtime = AgentRuntime(
        fixture_registry,
        ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=4, max_tool_calls=4, max_seconds=30),
        provider_resolver=lambda _name: provider,
    )
    first = _context("child:one", "specialist")
    second = _context("child:two", "specialist")
    first.effective_tool_view = ["get_current_time"]
    second.effective_tool_view = ["get_current_time"]
    first_spec = _spec(first.run_id, "specialist").model_copy(
        update={"allowed_tools": ("get_current_time",)}
    )
    second_spec = _spec(second.run_id, "specialist").model_copy(
        update={"allowed_tools": ("get_current_time",)}
    )

    await asyncio.gather(runtime.run(first_spec, first), runtime.run(second_spec, second))

    assert set(seen) == {
        ("parent:001", "task:child:one", "child:one"),
        ("parent:001", "task:child:two", "child:two"),
    }
