from __future__ import annotations

from starter_agent.delegation.models import BudgetLimits, BudgetUsage
from starter_agent.delegation.worker import ChildRuntimeExecutor, build_child_result_envelope


def test_child_adapter_output_becomes_a_bound_result_envelope_not_context_output() -> None:
    usage = BudgetUsage(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=0, cost_status="actual", price_version="p1", usage_source="provider")
    envelope = build_child_result_envelope(task_id="task:1", child_run_id="child:1", status="succeeded", output={"jobs": []}, usage=usage, trace_ref="trace:child-run:child:1")
    assert envelope.task_id == "task:1" and envelope.child_run_id == "child:1"
    assert tuple(envelope.output["jobs"]) == ()


def test_child_runtime_usage_preserves_estimated_price_evidence() -> None:
    context = type("Context", (), {})()
    context.budget = type("Budget", (), {})()
    context.budget.consumed = BudgetLimits(
        tokens=1100,
        cost_microunits=2800,
        wall_clock_ms=10,
        model_calls=1,
        tool_calls=0,
    )
    context.budget.cost_unknown = False
    context.provider_usages = [
        {
            "cost_status": "estimated",
            "cost_estimated": True,
            "price_version": "zhipu-glm-4.7-cny-2026-08-14",
            "usage_source": "provider_tokens+configured_price:https://bigmodel.cn/pricing",
        }
    ]
    executor = ChildRuntimeExecutor(assemble=lambda _claim: None, runtime=object())

    usage = executor._usage(context)

    assert usage.estimated is True
    assert usage.cost_status == "estimated"
    assert usage.price_version == "zhipu-glm-4.7-cny-2026-08-14"
