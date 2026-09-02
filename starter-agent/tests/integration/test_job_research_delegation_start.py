from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.agent.context import ContextBuilder
from starter_agent.agent.runtime import AgentRuntime
from starter_agent.application import ApplicationService
from starter_agent.delegation.coordinator import Coordinator
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.service import DelegationService
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.settings import load_settings
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.providers.registry import ProviderRegistry
from starter_agent.interfaces.api import ChatRequest, _dispatch_classified_chat
from starter_agent.knowledge.routing import KnowledgeRequestDecision, KnowledgeRequestRoute
from starter_agent.trust.store import TrustStore


ROOT = Path(__file__).parents[2]


def _application(tmp_path):
    settings = load_settings("config/config.example.yaml")
    settings.project_root = tmp_path
    settings.app.database_url = "sqlite:///job-start.db"
    settings.app.identity_path = "identity.md"
    (tmp_path / "identity.md").write_text("identity", encoding="utf-8")
    (tmp_path / "system.md").write_text("system", encoding="utf-8")
    application = ApplicationService(
        settings,
        SQLiteSessionStore(settings.app.database_url, tmp_path),
        ProviderRegistry(settings),
        AgentRuntime(
            UnifiedToolRegistry(ToolRegistry([])),
            ToolPolicy(["read"]), settings.runtime,
        ),
        ContextBuilder(tmp_path / "identity.md", tmp_path / "system.md"),
    )
    store = SQLiteRunStore(settings.app.database_url, tmp_path)
    registry = SpecialistRegistry(
        ROOT / "config" / "specialists", project_root=ROOT,
        dependency_resolver=lambda _dependency: True,
    )
    registry.reload()
    application.configure_job_research_delegation(
        store=store,
        service=DelegationService(store=store, registry=registry),
        coordinator=Coordinator(store=store),
        registry=registry,
    )
    return application, store


@pytest.mark.asyncio
async def test_same_request_creates_one_parent_task_child_and_reservation() -> None:
    tmp_path = ROOT / ".session-only-task15" / uuid4().hex
    tmp_path.mkdir(parents=True, exist_ok=True)
    application, store = _application(tmp_path)
    session_id = uuid4()
    first = await application.start_job_research_delegation(
        message="find backend roles", session_id=session_id
    )
    second = await application.start_job_research_delegation(
        message="find backend roles", session_id=session_id
    )

    assert second == first
    tree = store.get_run_tree(first.parent_run_id)
    assert len(tree.child_tasks) == len(tree.child_runs) == 1
    assert tree.parent.status == "waiting_children"
    assert tree.parent.phase == "waiting_children"
    assert all(len({item.dimension for item in tree.allocations}) == 5 for _ in [0])
    assert tree.parent.budget_reserved == tree.child_tasks[0].requested_budget


@pytest.mark.asyncio
async def test_different_request_creates_independent_delegated_tree() -> None:
    tmp_path = ROOT / ".session-only-task15" / uuid4().hex
    tmp_path.mkdir(parents=True, exist_ok=True)
    application, store = _application(tmp_path)
    session_id = uuid4()
    first = await application.start_job_research_delegation(
        message="find backend roles", session_id=session_id
    )
    second = await application.start_job_research_delegation(
        message="find data roles", session_id=session_id
    )

    assert first.parent_run_id != second.parent_run_id
    assert len(store.get_run_tree(first.parent_run_id).child_runs) == 1
    assert len(store.get_run_tree(second.parent_run_id).child_runs) == 1


@pytest.mark.asyncio
async def test_real_bootstrap_router_creates_one_durable_web_child_without_request_tools(monkeypatch) -> None:
    """The Router submits work only; Search/Browser remain in the Worker."""
    import starter_agent.bootstrap as bootstrap

    database = ROOT / ".session-only-task15" / f"{uuid4().hex}.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings("config/config.example.yaml")
    settings.project_root = ROOT
    settings.app.database_url = f"sqlite:///{database}"
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    bootstrap.create_application.cache_clear()
    application = bootstrap.create_application()
    application.configure_delegation_route_policy(
        lambda: {
            "delegated_job_research_enabled": True,
            "legacy_job_research_enabled": False,
        }
    )
    calls: list[object] = []
    monkeypatch.setattr(
        application.runtime, "execute_tool",
        lambda **kwargs: calls.append(kwargs),
        raising=False,
    )
    request = ChatRequest(
        message="research and compare three backend roles", session_id=uuid4()
    )
    route = KnowledgeRequestDecision(
        route=KnowledgeRequestRoute.JOB_RESEARCH, reason_code="fixture"
    )
    first = await _dispatch_classified_chat(request, application=application, route=route)
    second = await _dispatch_classified_chat(request, application=application, route=route)

    assert first.parent_run_id == second.parent_run_id
    assert first.child_task_id == second.child_task_id
    assert first.child_run_id == second.child_run_id
    assert first.route == "delegated_job_research"
    assert first.legacy_path_used is False
    assert first.contract_hash and first.effective_tool_view_hash
    tree = application.delegation_store.get_run_tree(first.parent_run_id)
    assert len(tree.child_tasks) == len(tree.child_runs) == 1
    assert calls == []
    # Bootstrap registers the durable observer: Trust is populated before a
    # Worker is ever started.
    trust = TrustStore(settings.app.database_url, ROOT)
    assert any(
        event.summary.get("delegation_event_type") == "child.delegated"
        for event in trust.list_trace_events(parent_run_id=first.parent_run_id)
    )
    bootstrap.create_application.cache_clear()
