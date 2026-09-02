from __future__ import annotations

from uuid import uuid4
from copy import deepcopy

import pytest

from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits
from starter_agent.domain.models import Message


def _context(run_id: str, *, parent_run_id: str | None = None) -> RunContext:
    return RunContext(
        run_id=run_id,
        parent_run_id=parent_run_id or run_id,
        child_task_id=None if parent_run_id is None else f"task:{run_id}",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:001",
        messages=[Message(role="user", content=f"input:{run_id}")],
        effective_tool_view=["get_current_time"],
        budget_limits=BudgetLimits(
            tokens=10_000,
            cost_microunits=1_000_000,
            wall_clock_ms=60_000,
            model_calls=4,
            tool_calls=4,
        ),
        trace_context=RunTraceContext(
            parent_run_id=parent_run_id or run_id,
            child_task_id=None if parent_run_id is None else f"task:{run_id}",
            child_run_id=None if parent_run_id is None else run_id,
            eval_run_id="eval:001",
        ),
    )


def test_parent_and_children_own_distinct_mutable_run_state() -> None:
    parent = _context("parent:001")
    first = _context("child:001", parent_run_id="parent:001")
    second = _context("child:002", parent_run_id="parent:001")

    assert len({id(parent), id(first), id(second)}) == 3
    mutable_names = (
        "messages",
        "working_memory",
        "todo_plan",
        "effective_tool_view",
        "summary_trim_state",
        "output_buffer",
        "artifact_refs",
        "repeated_calls",
        "provider_usages",
    )
    for name in mutable_names:
        assert len({id(getattr(parent, name)), id(getattr(first, name)), id(getattr(second, name))}) == 3

    first.messages.append(Message(role="assistant", content="child-only"))
    first.working_memory["secret"] = "child-only"
    first.todo_plan.append({"step": "child-only"})
    first.effective_tool_view.append("child-only-tool")
    first.summary_trim_state["summary"] = "child-only"
    first.output_buffer.append("child-only")
    first.artifact_refs.append("artifact:child-only")
    first.repeated_calls["signature"] = 2
    first.provider_usages.append({"total_tokens": 3})
    first.budget.consume(model_calls=1, tokens=3)
    first.cancellation.request("stop child")

    assert len(parent.messages) == 1
    assert parent.working_memory == {}
    assert parent.todo_plan == []
    assert parent.effective_tool_view == ["get_current_time"]
    assert parent.summary_trim_state == {}
    assert parent.output_buffer == []
    assert parent.artifact_refs == []
    assert parent.repeated_calls == {}
    assert parent.provider_usages == []
    assert parent.budget.consumed.model_calls == 0
    assert parent.cancellation.requested is False
    assert second.working_memory == {}


def test_run_context_defensively_copies_caller_owned_containers() -> None:
    messages = [Message(role="user", content="hello")]
    tools = ["get_current_time"]
    context = RunContext(
        run_id="parent:001",
        parent_run_id="parent:001",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:001",
        messages=messages,
        effective_tool_view=tools,
        budget_limits=BudgetLimits(
            tokens=10,
            cost_microunits=10,
            wall_clock_ms=10,
            model_calls=1,
            tool_calls=1,
        ),
        trace_context=RunTraceContext(parent_run_id="parent:001"),
    )

    messages.append(Message(role="user", content="outside"))
    tools.append("outside-tool")
    assert [item.content for item in context.messages] == ["hello"]
    assert context.effective_tool_view == ["get_current_time"]


def test_nested_run_state_is_not_shared_with_caller_or_checkpoint() -> None:
    working = {"nested": {"cursor": [1]}}
    todo = [{"details": {"pages": [1]}}]
    context = _context("child:001", parent_run_id="parent:001")
    context.working_memory = deepcopy(working)
    context.todo_plan = deepcopy(todo)
    restored = RunContext.from_checkpoint(context.to_checkpoint())

    restored.working_memory["nested"]["cursor"].append(2)
    restored.todo_plan[0]["details"]["pages"].append(2)
    assert context.working_memory == working
    assert context.todo_plan == todo


def test_tool_context_contains_run_trace_and_authorized_scope() -> None:
    context = _context("child:001", parent_run_id="parent:001")
    context.user_id = "user:001"
    context.project_id = "project:001"
    context.knowledge_scope = "resume:authorized"
    tool_context = context.tool_context("tool-call:001")

    assert tool_context.parent_run_id == "parent:001"
    assert tool_context.child_task_id == "task:child:001"
    assert tool_context.child_run_id == "child:001"
    assert tool_context.eval_run_id == "eval:001"
    assert tool_context.knowledge_scope == "resume:authorized"
    assert tool_context.user_id == "user:001"
    assert tool_context.project_id == "project:001"


def test_checkpoint_round_trip_creates_fresh_mutable_state() -> None:
    original = _context("child:001", parent_run_id="parent:001")
    original.working_memory["cursor"] = 2
    original.todo_plan.append({"step": "extract"})
    original.output_buffer.append("partial")
    original.budget.consume(tool_calls=1)

    restored = RunContext.from_checkpoint(original.to_checkpoint())

    assert restored is not original
    assert restored.working_memory == original.working_memory
    assert restored.budget.consumed == original.budget.consumed
    assert restored.working_memory is not original.working_memory
    assert restored.todo_plan is not original.todo_plan
    assert restored.output_buffer is not original.output_buffer
    restored.working_memory["cursor"] = 3
    assert original.working_memory["cursor"] == 2


def test_checkpoint_preserves_capped_consumption_and_observed_overage() -> None:
    checkpoint = _context(
        "child:001", parent_run_id="parent:001"
    ).to_checkpoint()
    context = RunContext.from_checkpoint(checkpoint)

    with pytest.raises(ValueError, match="run budget exceeded: tokens"):
        context.budget.consume(tokens=context.budget.limits.tokens + 1)

    restored = RunContext.from_checkpoint(context.to_checkpoint())
    assert restored.budget.consumed.tokens == restored.budget.limits.tokens
    assert restored.budget.overage.tokens == 1


def test_run_context_rejects_conflicting_trace_identity() -> None:
    with pytest.raises(ValueError, match="trace child_run_id"):
        RunContext(
            run_id="child:001",
            parent_run_id="parent:001",
            child_task_id="task:001",
            session_id=uuid4(),
            turn_id=uuid4(),
            principal="user:001",
            messages=[],
            effective_tool_view=[],
            budget_limits=BudgetLimits(
                tokens=1,
                cost_microunits=1,
                wall_clock_ms=1,
                model_calls=1,
                tool_calls=1,
            ),
            trace_context=RunTraceContext(
                parent_run_id="parent:001",
                child_task_id="task:001",
                child_run_id="child:other",
            ),
        )


def test_child_context_requires_child_trace_run_id() -> None:
    with pytest.raises(ValueError, match="child_run_id is required"):
        RunContext(
            run_id="child:001",
            parent_run_id="parent:001",
            child_task_id="task:child:001",
            session_id=uuid4(),
            turn_id=uuid4(),
            principal="user:001",
            messages=[],
            effective_tool_view=[],
            budget_limits=BudgetLimits(
                tokens=1,
                cost_microunits=1,
                wall_clock_ms=1,
                model_calls=1,
                tool_calls=1,
            ),
            trace_context=RunTraceContext(
                parent_run_id="parent:001", child_task_id="task:child:001"
            ),
        )


def test_checkpoint_rejects_consumption_above_limits() -> None:
    checkpoint = _context("child:001", parent_run_id="parent:001").to_checkpoint()
    checkpoint["budget_consumed"]["tokens"] = (
        checkpoint["budget_limits"]["tokens"] + 1
    )

    with pytest.raises(ValueError, match="run budget exceeded: tokens"):
        RunContext.from_checkpoint(checkpoint)
