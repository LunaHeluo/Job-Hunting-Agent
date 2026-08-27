from __future__ import annotations

import pytest

from starter_agent.delegation.budget import (
    BudgetLedgerError,
    reserve_budget,
    settle_budget,
    validate_cost_usage,
)
from starter_agent.delegation.models import (
    BudgetAllocation,
    BudgetLimits,
    BudgetUsage,
)


def _limits(**changes: int) -> BudgetLimits:
    values = {
        "steps": 10,
        "tokens": 10_000,
        "cost_microunits": 1_000_000,
        "wall_clock_ms": 120_000,
        "model_calls": 10,
        "tool_calls": 20,
    }
    values.update(changes)
    return BudgetLimits(**values)


def _usage(**changes: object) -> BudgetUsage:
    values: dict[str, object] = {
        "steps": 2,
        "tokens": 2_000,
        "cost_microunits": 200_000,
        "wall_clock_ms": 20_000,
        "model_calls": 2,
        "tool_calls": 4,
        "estimated": False,
        "cost_status": "actual",
        "usage_source": "provider",
    }
    values.update(changes)
    return BudgetUsage(**values)


def test_reserve_budget_creates_six_dimension_allocations() -> None:
    reservation = reserve_budget(
        total=_limits(),
        reserved=_limits(
            steps=1,
            tokens=1_000,
            cost_microunits=100_000,
            wall_clock_ms=10_000,
            model_calls=1,
            tool_calls=2,
        ),
        consumed=_limits(
            steps=1,
            tokens=500,
            cost_microunits=50_000,
            wall_clock_ms=5_000,
            model_calls=1,
            tool_calls=1,
        ),
        requested=_limits(
            steps=3,
            tokens=3_000,
            cost_microunits=300_000,
            wall_clock_ms=30_000,
            model_calls=3,
            tool_calls=6,
        ),
    )

    assert reservation.updated_reserved == _limits(
        steps=4,
        tokens=4_000,
        cost_microunits=400_000,
        wall_clock_ms=40_000,
        model_calls=4,
        tool_calls=8,
    )
    assert tuple(item.dimension for item in reservation.allocations) == (
        "steps",
        "tokens",
        "cost_microunits",
        "wall_clock_ms",
        "model_calls",
        "tool_calls",
    )
    assert [item.reserved for item in reservation.allocations] == [
        3,
        3_000,
        300_000,
        30_000,
        3,
        6,
    ]


@pytest.mark.parametrize(
    "dimension",
    ["steps", "tokens", "cost_microunits", "wall_clock_ms", "model_calls", "tool_calls"],
)
def test_reserve_budget_rejects_any_exhausted_dimension(dimension: str) -> None:
    requested = _limits(
        steps=1,
        tokens=1,
        cost_microunits=1,
        wall_clock_ms=1,
        model_calls=1,
        tool_calls=1,
    )
    requested = requested.model_copy(update={dimension: 2})
    reserved = _limits(
        steps=9,
        tokens=9_999,
        cost_microunits=999_999,
        wall_clock_ms=119_999,
        model_calls=9,
        tool_calls=19,
    )

    with pytest.raises(BudgetLedgerError) as captured:
        reserve_budget(
            total=_limits(),
            reserved=reserved,
            consumed=_limits(
                steps=1,
                tokens=1,
                cost_microunits=1,
                wall_clock_ms=1,
                model_calls=1,
                tool_calls=1,
            ),
            requested=requested,
        )

    assert captured.value.code == "parent_budget_exhausted"
    assert captured.value.dimension == dimension


def test_settle_budget_consumes_usage_and_releases_unused_reservation() -> None:
    reservation = reserve_budget(
        total=_limits(),
        reserved=_limits(steps=0, tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0),
        consumed=_limits(steps=0, tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0),
        requested=_limits(
            steps=3,
            tokens=3_000,
            cost_microunits=300_000,
            wall_clock_ms=30_000,
            model_calls=3,
            tool_calls=6,
        ),
    )

    settlement = settle_budget(
        parent_reserved=reservation.updated_reserved,
        parent_consumed=_limits(steps=0, tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0),
        allocations=reservation.allocations,
        usage=_usage(),
    )

    assert settlement.updated_reserved == _limits(
        steps=2,
        tokens=2_000,
        cost_microunits=200_000,
        wall_clock_ms=20_000,
        model_calls=2,
        tool_calls=4,
    )
    assert settlement.updated_consumed == settlement.updated_reserved
    assert [item.released for item in settlement.allocations] == [
        1,
        1_000,
        100_000,
        10_000,
        1,
        2,
    ]
    assert all(item.version == 1 for item in settlement.allocations)


def test_settle_budget_rejects_usage_above_reservation() -> None:
    allocation = BudgetAllocation(
        dimension="tokens",
        limit=10_000,
        requested=1_000,
        reserved=1_000,
        consumed=0,
        released=0,
    )
    with pytest.raises(BudgetLedgerError) as captured:
        settle_budget(
            parent_reserved=_limits(
                steps=0,
                tokens=1_000,
                cost_microunits=0,
                wall_clock_ms=0,
                model_calls=0,
                tool_calls=0,
            ),
            parent_consumed=_limits(
                steps=0,
                tokens=0,
                cost_microunits=0,
                wall_clock_ms=0,
                model_calls=0,
                tool_calls=0,
            ),
            allocations=(allocation,),
            usage=_usage(
                steps=0,
                tokens=1_001,
                cost_microunits=0,
                wall_clock_ms=0,
                model_calls=0,
                tool_calls=0,
            ),
        )
    assert captured.value.code == "budget_usage_exceeds_reservation"
    assert captured.value.dimension == "tokens"


def test_unknown_or_unpriced_cost_usage_is_rejected() -> None:
    with pytest.raises(BudgetLedgerError) as unknown:
        validate_cost_usage(_usage(cost_microunits=0, usage_source=None))
    assert unknown.value.code == "cost_budget_unenforceable"

    with pytest.raises(BudgetLedgerError) as unpriced:
        validate_cost_usage(
            _usage(
                estimated=True,
                cost_status="estimated",
                usage_source="token_estimate",
                price_version=None,
            )
        )
    assert unpriced.value.code == "cost_budget_unenforceable"

    with pytest.raises(BudgetLedgerError) as disguised_zero:
        validate_cost_usage(
            _usage(
                cost_microunits=0,
                cost_status="unknown",
                usage_source="provider",
            )
        )
    assert disguised_zero.value.code == "cost_budget_unenforceable"
