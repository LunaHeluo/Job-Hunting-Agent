from datetime import UTC, datetime

from starter_agent.orchestration.model_router import ModelRouter
from starter_agent.orchestration.models import BudgetAmounts, ModelRequirements
from starter_agent.settings import (
    AgentSettings,
    ModelRouteProfile,
    ModelRoutingConfig,
    ProviderConfig,
)


NOW = datetime(2026, 8, 14, tzinfo=UTC)


def settings() -> AgentSettings:
    return AgentSettings(
        providers={
            "mock": ProviderConfig(type="mock", models=["small", "large"]),
            "other": ProviderConfig(type="mock", models=["backup"]),
        },
        model_routing=ModelRoutingConfig(
            config_revision="routes:test",
            profiles=[
                ModelRouteProfile(
                    provider="mock",
                    model="small",
                    purposes=["router", "executor"],
                    capabilities=["structured_output"],
                    complexities=["trivial", "bounded"],
                    latency_class="interactive",
                    max_context_tokens=8_000,
                    priority=10,
                    estimated_cost_microunits=100,
                ),
                ModelRouteProfile(
                    provider="mock",
                    model="large",
                    purposes=["planner", "judge", "recovery"],
                    capabilities=["structured_output", "reasoning"],
                    complexities=["bounded", "complex"],
                    latency_class="background",
                    max_context_tokens=100_000,
                    priority=20,
                    estimated_cost_microunits=500,
                ),
                ModelRouteProfile(
                    provider="other",
                    model="backup",
                    purposes=["planner", "judge", "recovery"],
                    capabilities=["structured_output", "reasoning"],
                    complexities=["bounded", "complex"],
                    latency_class="standard",
                    max_context_tokens=100_000,
                    priority=30,
                    estimated_cost_microunits=600,
                ),
            ],
        ),
    )


def requirements(**changes: object) -> ModelRequirements:
    values: dict[str, object] = {
        "capabilities": ("structured_output", "reasoning"),
        "complexity": "complex",
        "latency_class": "background",
        "context_tokens": 20_000,
        "risk_policy": "high_requires_validation",
    }
    values.update(changes)
    return ModelRequirements(**values)


def test_selects_only_configured_capable_model_and_records_reason() -> None:
    decision = ModelRouter(settings()).decide(
        decision_id="model-decision:1",
        purpose="planner",
        requirements=requirements(),
        remaining_budget=BudgetAmounts(cost_microunits=1_000),
        health={"mock/large": "healthy", "other/backup": "healthy"},
        created_at=NOW,
    )
    assert (decision.selected_provider, decision.selected_model) == ("mock", "large")
    assert decision.reason_code == "best_eligible_candidate"
    assert decision.config_revision == "routes:test"
    assert decision.requirements.risk_policy == "high_requires_validation"


def test_unhealthy_primary_uses_finite_configured_fallback() -> None:
    decision = ModelRouter(settings()).decide(
        decision_id="model-decision:2",
        purpose="planner",
        requirements=requirements(),
        remaining_budget=BudgetAmounts(cost_microunits=1_000),
        health={"mock/large": "unavailable", "other/backup": "healthy"},
        created_at=NOW,
    )
    assert (decision.selected_provider, decision.selected_model) == ("other", "backup")
    assert decision.status == "fallback"
    assert decision.fallback_chain == ("mock/large", "other/backup")


def test_unknown_or_unconfigured_model_profile_is_never_selected() -> None:
    configured = settings()
    configured.model_routing.profiles.append(
        ModelRouteProfile(
            provider="mock",
            model="invented",
            purposes=["planner"],
            capabilities=["reasoning", "structured_output"],
            complexities=["complex"],
            latency_class="background",
            max_context_tokens=100_000,
            priority=1,
            estimated_cost_microunits=1,
        )
    )
    decision = ModelRouter(configured).decide(
        decision_id="model-decision:3",
        purpose="planner",
        requirements=requirements(),
        remaining_budget=BudgetAmounts(cost_microunits=1_000),
        health={},
        created_at=NOW,
    )
    assert decision.selected_model != "invented"
    assert all(candidate.model != "invented" for candidate in decision.candidates)


def test_budget_or_capability_mismatch_returns_unavailable_without_inventing_model() -> None:
    decision = ModelRouter(settings()).decide(
        decision_id="model-decision:4",
        purpose="planner",
        requirements=requirements(capabilities=("vision",)),
        remaining_budget=BudgetAmounts(cost_microunits=10),
        health={},
        created_at=NOW,
    )
    assert decision.status == "unavailable"
    assert decision.selected_model is None
    assert decision.fallback_chain == ()


def test_decision_payload_contains_no_provider_secret() -> None:
    decision = ModelRouter(settings()).decide(
        decision_id="model-decision:5",
        purpose="router",
        requirements=requirements(
            capabilities=("structured_output",),
            complexity="bounded",
            latency_class="interactive",
            context_tokens=100,
        ),
        remaining_budget=None,
        health={},
        created_at=NOW,
    )
    payload = decision.model_dump_json()
    assert "api_key" not in payload.lower()
    assert "base_url" not in payload.lower()

