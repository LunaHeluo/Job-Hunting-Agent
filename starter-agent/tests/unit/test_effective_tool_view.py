from __future__ import annotations

from datetime import UTC, datetime

from starter_agent.capabilities.models import Server, Snapshot, Tool, canonical_json_sha256
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.delegation.tool_view import EffectiveToolView, build_effective_tool_view
from starter_agent.tools.registry import ToolRegistry


def _mcp(name: str, *, browser: bool = True) -> Tool:
    schema = {"type": "object", "additionalProperties": False}
    return Tool(
        snapshot_id="snapshot:1",
        server_id="playwright",
        upstream_name=name.removeprefix("mcp__playwright__"),
        model_alias=name,
        description=f"Schema for {name}",
        input_schema=schema,
        schema_hash=canonical_json_sha256(schema),
        enabled=True,
        review_state="approved",
        reviewed_at=datetime.now(UTC),
        metadata={"browser": browser},
    )


def _registry() -> UnifiedToolRegistry:
    builtins = ToolRegistry(["get_current_time", "search_jobs_serpapi"])
    registry = UnifiedToolRegistry(builtins)
    server = Server(
        id="playwright",
        name="playwright",
        config_source="tests/mcp.json",
        config_hash="a" * 64,
        enabled=True,
        connection_state="ready",
    )
    snapshot = Snapshot(
        id="snapshot:1",
        server_id="playwright",
        version=1,
        schema_hash="b" * 64,
        discovered_at=datetime.now(UTC),
        active=True,
        tool_count=2,
    )
    registry.refresh_server(
        server,
        [
            _mcp("mcp__playwright__browser_navigate"),
            _mcp("mcp__playwright__browser_snapshot"),
        ],
        snapshot=snapshot,
    )
    return registry


def test_effective_view_is_five_way_intersection_and_never_contains_delegate() -> None:
    registry = _registry()
    view = build_effective_tool_view(
        registry,
        registry_allowed={
            "search_jobs_serpapi",
            "mcp__playwright__browser_navigate",
            "delegate_task",
        },
        contract_requested={
            "search_jobs_serpapi",
            "mcp__playwright__browser_navigate",
            "get_current_time",
            "delegate_task",
        },
        scenario_allowed={
            "search_jobs_serpapi",
            "mcp__playwright__browser_navigate",
        },
        policy_allowed={
            "search_jobs_serpapi",
            "mcp__playwright__browser_navigate",
        },
    )

    assert isinstance(view, EffectiveToolView)
    assert view.names == (
        "mcp__playwright__browser_navigate",
        "search_jobs_serpapi",
    )
    assert {item["function"]["name"] for item in view.schemas()} == set(view.names)
    assert view.resolve_execution("get_current_time") is None
    assert view.resolve_execution("delegate_task") is None


def test_filtered_view_is_immutable_snapshot_when_shared_registry_changes() -> None:
    registry = _registry()
    view = build_effective_tool_view(
        registry,
        registry_allowed={"mcp__playwright__browser_navigate"},
        contract_requested={"mcp__playwright__browser_navigate"},
        scenario_allowed={"mcp__playwright__browser_navigate"},
        policy_allowed={"mcp__playwright__browser_navigate"},
    )
    before = view.model_snapshot()

    registry.set_tool_enabled("mcp__playwright__browser_navigate", False)

    assert view.model_snapshot() == before
    assert view.get("mcp__playwright__browser_navigate") is None


def test_uncallable_dependency_is_removed_even_if_all_sets_request_it() -> None:
    registry = _registry()
    registry.set_tool_enabled("mcp__playwright__browser_snapshot", False)

    view = build_effective_tool_view(
        registry,
        registry_allowed={"mcp__playwright__browser_snapshot"},
        contract_requested={"mcp__playwright__browser_snapshot"},
        scenario_allowed={"mcp__playwright__browser_snapshot"},
        policy_allowed={"mcp__playwright__browser_snapshot"},
    )

    assert view.names == ()
    assert view.schemas() == []


def test_mcp_upstream_name_is_canonicalized_to_model_alias() -> None:
    registry = _registry()

    view = build_effective_tool_view(
        registry,
        registry_allowed={"browser_navigate"},
        contract_requested={"browser_navigate"},
        scenario_allowed={"browser_navigate"},
        policy_allowed={"browser_navigate"},
    )

    assert view.names == ("mcp__playwright__browser_navigate",)
    assert view.schemas()[0]["function"]["name"] == view.names[0]
