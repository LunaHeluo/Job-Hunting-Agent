import asyncio
import ipaddress
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from starter_agent.capabilities.gate import (
    NetworkGuardAttestation,
    PreToolCallGate,
    UnifiedToolExecutor,
)
from starter_agent.capabilities.confirmations import (
    ConfirmationService,
    TurnCoordinator,
)
from starter_agent.capabilities.models import (
    PolicyRule,
    Server,
    Snapshot,
    Tool as McpTool,
    canonical_json_sha256,
)
from starter_agent.capabilities.policy import BrowserScopePolicy
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.domain.models import ModelResponse, ToolResult
from starter_agent.skills.job_research import JobResearchOrchestrator
from starter_agent.skills.models import SkillRunResult
from starter_agent.settings import load_settings
from starter_agent.mcp.config import McpConfiguration, McpServerConfig
from starter_agent.mcp.manager import McpManager
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.tools.builtin.knowledge import RetrieveResumeEvidenceTool


async def _public_resolver(_host: str):
    return [ipaddress.ip_address("93.184.216.34")]


class _Builtins:
    email_manager = None

    def __init__(self, tools):
        self._tools = tools

    def list(self):
        return list(self._tools)


class _SearchTool(Tool):
    name = "search_jobs_serpapi"
    description = "Search jobs"
    risk_level = "read"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "location": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "expand_location_aliases": {"type": "boolean"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def execute(self, arguments, context):
        return ToolResult(
            ok=True,
            data={
                "results": [
                    {
                        "title": "AI Agent Engineer",
                        "company": "Example",
                        "location": "Shanghai",
                        "url": "https://jobs.example/agent",
                    }
                ]
            },
        )


class _Knowledge:
    scope = SimpleNamespace(user_id="local-user", project_id="project")

    def retrieve(self, knowledge_base_id, question, **filters):
        return [
            SimpleNamespace(
                chunk_id=uuid4(),
                document_id=uuid4(),
                filename="resume.md",
                version=1,
                section_path=["Projects"],
                start_line=4,
                end_line=4,
                preview="Built a production RAG agent with Python.",
                source_ref="resume.md@v1#L4-L4",
                rank=1,
            )
        ]


class _ProfileProvider:
    name = "profile-test"

    async def complete(self, messages, model, tools, **kwargs):
        return ModelResponse(
            content=(
                '{"query":"AI Agent engineer","location":"Shanghai",'
                '"evidence_refs":["E1"]}'
            ),
            provider=self.name,
            model=model,
        )

    async def health(self, model):
        return True, "ok"


class _InvalidProfileProvider:
    name = "invalid-profile-test"

    async def complete(self, messages, model, tools, **kwargs):
        return ModelResponse(
            content="这不是 JSON 搜索画像。",
            provider=self.name,
            model=model,
        )

    async def health(self, model):
        return True, "ok"


async def test_job_skill_waits_for_confirmation_and_invokes_search_once(tmp_path):
    search = _SearchTool()
    registry = UnifiedToolRegistry(_Builtins([search]))
    store = CapabilityStore("sqlite:///:memory:", tmp_path)
    gate = PreToolCallGate(store, registry=registry)
    executor = UnifiedToolExecutor(store, gate=gate)
    calls = 0

    async def invoke(arguments, context):
        nonlocal calls
        calls += 1
        return await search.execute(dict(arguments), context)

    executor.register_invoker(
        server_id="builtin",
        tool_name=search.name,
        invoker=invoke,
    )
    confirmations = ConfirmationService(
        store,
        gate,
        confirmation_ttl_seconds=2,
    )
    coordinator = TurnCoordinator(
        confirmations,
        confirmation_timeout_seconds=2,
    )
    events = []

    async def on_event(event):
        events.append(event)

    context = ToolContext(
        session_id=uuid4(),
        turn_id=uuid4(),
        on_tool_event=on_event,
    )
    orchestrator = JobResearchOrchestrator(
        registry,
        executor,
        turn_coordinator=coordinator,
    )
    prepared = SkillRunResult(
        status="search_profile_ready",
        data={
            "search_profile": {
                "query": "AI Agent engineer",
                "location": "Shanghai",
            }
        },
    )

    task = asyncio.create_task(
        orchestrator.search_prepared(
            prepared=prepared,
            context=context,
            limit=1,
        )
    )
    for _ in range(100):
        pending = confirmations.list_pending(session_id=str(context.session_id))
        if pending:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("confirmation was not persisted")
    assert calls == 0
    confirmation = pending[0]
    confirmations.decide(
        confirmation.id,
        expected_revision=confirmation.revision,
        idempotency_key="job-search-confirm-once",
        decision="once",
        actor="local-user",
    )

    result = await task

    assert result.status == "waiting_for_url_selection"
    assert calls == 1
    assert [event["type"] for event in events] == [
        "confirmation_required",
        "confirmation_resolved",
    ]


async def test_job_research_calls_every_real_tool_through_gate_and_keeps_trace(tmp_path):
    search = _SearchTool()
    rag = RetrieveResumeEvidenceTool(_Knowledge())
    builtins = _Builtins([search, rag])
    registry = UnifiedToolRegistry(builtins)
    browser_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    }
    browser = McpTool(
        snapshot_id="playwright-snapshot-1",
        server_id="playwright",
        upstream_name="browser_navigate",
        model_alias="mcp__playwright__browser_navigate",
        description="Read a public job page",
        input_schema=browser_schema,
        schema_hash=canonical_json_sha256(browser_schema),
        enabled=True,
        review_state="approved",
        risk_level="read",
        metadata={"browser": True, "action": "navigate"},
    )
    snapshot_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    browser_snapshot = McpTool(
        snapshot_id="playwright-snapshot-1",
        server_id="playwright",
        upstream_name="browser_snapshot",
        model_alias="mcp__playwright__browser_snapshot",
        description="Read the active public job page",
        input_schema=snapshot_schema,
        schema_hash=canonical_json_sha256(snapshot_schema),
        enabled=True,
        review_state="approved",
        risk_level="read",
        metadata={"browser": True, "action": "read"},
    )
    server = Server(
        id="playwright",
        name="playwright",
        config_source="config/mcp.json",
        config_hash="a" * 64,
        enabled=True,
        connection_state="ready",
    )
    store = CapabilityStore("sqlite:///:memory:", tmp_path)
    store.create_server(server)
    store.create_snapshot(
        Snapshot(
            id=browser.snapshot_id,
            server_id=server.id,
            version=1,
            schema_hash=browser.schema_hash,
            discovered_at=datetime.now(UTC),
            tool_count=2,
        ),
        tools=[browser, browser_snapshot],
    )
    active = store.activate_snapshot(server.id, browser.snapshot_id)
    reviewed_browser = store.update_tool(
        active.id,
        browser.upstream_name,
        expected_revision=0,
        review_state="approved",
    )
    reviewed_snapshot = store.update_tool(
        active.id,
        browser_snapshot.upstream_name,
        expected_revision=0,
        review_state="approved",
    )
    registry.refresh_server(
        server,
        [reviewed_browser, reviewed_snapshot],
        snapshot=active,
    )
    for server_id, name, schema_hash in (
        ("builtin", search.name, canonical_json_sha256(search.input_schema)),
        ("builtin", rag.name, canonical_json_sha256(rag.input_schema)),
        ("playwright", browser.upstream_name, browser.schema_hash),
        (
            "playwright",
            browser_snapshot.upstream_name,
            browser_snapshot.schema_hash,
        ),
    ):
        store.create_policy_rule(
            PolicyRule(
                id=f"allow-{server_id}-{name}",
                server_id=server_id,
                tool_name=name,
                effect="allowlist_auto",
                actions=("read", "navigate"),
                schema_hash=schema_hash,
                created_by="test",
            )
        )
    gate = PreToolCallGate(
        store,
        registry=registry,
        browser_policy=BrowserScopePolicy(resolver=_public_resolver),
    )
    executor = UnifiedToolExecutor(store, gate=gate)
    invoked = []

    async def invoke_builtin(arguments, context, *, tool):
        invoked.append(tool.name)
        return await tool.execute(dict(arguments), context)

    for tool in (search, rag):
        executor.register_invoker(
            server_id="builtin",
            tool_name=tool.name,
            invoker=lambda arguments, context, tool=tool: invoke_builtin(
                arguments, context, tool=tool
            ),
            context_factory=lambda request: ToolContext(
                session_id=uuid4(),
                turn_id=uuid4(),
                tool_call_id=request.call_id,
                user_id="local-user",
                project_id="project",
                knowledge_base_id=knowledge_base_id,
            ),
        )

    async def invoke_browser(arguments, _context):
        invoked.append("browser_navigate")
        return ToolResult(ok=True, data={"source_url": arguments["url"]})

    async def invoke_snapshot(_arguments, _context):
        invoked.append("browser_snapshot")
        return ToolResult(
            ok=True,
            data={
                "title": "AI Agent Engineer",
                "company": "Example",
                "location": "Shanghai",
                "responsibilities": ["Build agent systems"],
                "requirements": ["Python", "RAG"],
                "source_url": "https://jobs.example/agent",
                "retrieved_at": "2026-07-23T00:00:00+00:00",
            },
            metadata={"is_untrusted_external_content": True},
        )

    async def attest(request):
        return NetworkGuardAttestation(
            targets=(request.arguments.get("url", "https://jobs.example/agent"),),
            dns_pinned=True,
            redirects_enforced=True,
            peer_verified=True,
        )

    executor.register_invoker(
        server_id="playwright",
        tool_name="browser_navigate",
        invoker=invoke_browser,
        network_guard=attest,
    )
    executor.register_invoker(
        server_id="playwright",
        tool_name="browser_snapshot",
        invoker=invoke_snapshot,
        network_guard=attest,
    )
    orchestrator = JobResearchOrchestrator(registry, executor)
    session_id, turn_id, knowledge_base_id = uuid4(), uuid4(), uuid4()
    context = ToolContext(
        session_id=session_id,
        turn_id=turn_id,
        user_id="local-user",
        project_id="project",
        knowledge_base_id=knowledge_base_id,
    )

    search_result = await orchestrator.search_from_request(
        user_request="根据我的简历搜索上海的岗位",
        provider=_ProfileProvider(),
        model="profile-test-model",
        limit=1,
        context=context,
    )
    result = await orchestrator.analyze(
        query="AI Agent engineer",
        selected_url=search_result.data["results"][0]["url"],
        context=context,
    )

    assert search_result.status == "waiting_for_url_selection"
    assert (
        result.status == "waiting_for_jd_ingestion_confirmation"
    ), result.model_dump()
    assert invoked == [
        "retrieve_resume_evidence",
        "search_jobs_serpapi",
        "browser_navigate",
        "browser_snapshot",
        "retrieve_resume_evidence",
    ]
    assert [item.tool_name for item in (*search_result.trace, *result.trace)] == [
        "retrieve_resume_evidence",
        "search_jobs_serpapi",
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "retrieve_resume_evidence",
    ]
    assert result.data["job"]["source_url"] == "https://jobs.example/agent"
    assert result.data["resume_evidence"][0]["quote"].startswith("Built")
    assert result.data["ingestion"]["status"] == "confirmation_required"
    assert all(item.result is not None for item in result.trace)
    assert len(
        [
            event
            for event in store.list_audit_events()
            if event.action == "tool.invoked" and event.decision == "allow"
        ]
    ) == 5


async def test_missing_dependency_fails_closed_without_tool_invocation(tmp_path):
    registry = UnifiedToolRegistry(_Builtins([]))
    store = CapabilityStore("sqlite:///:memory:", tmp_path)
    gate = PreToolCallGate(store, registry=registry)
    orchestrator = JobResearchOrchestrator(
        registry,
        UnifiedToolExecutor(store, gate=gate),
    )

    result = await orchestrator.search(
        query="AI engineer",
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
    )

    assert result.status == "dependency_unavailable"
    assert result.error_code == "dependency_unavailable"
    assert result.missing_dependencies == ("tool:search_jobs_serpapi",)
    assert result.trace == ()
    assert store.list_audit_events() == []


async def test_invalid_profile_is_audited_without_search_or_sensitive_text(tmp_path):
    search = _SearchTool()
    rag = RetrieveResumeEvidenceTool(_Knowledge())
    registry = UnifiedToolRegistry(_Builtins([search, rag]))
    store = CapabilityStore("sqlite:///:memory:", tmp_path)
    for tool in (search, rag):
        store.create_policy_rule(
            PolicyRule(
                id=f"allow-{tool.name}",
                server_id="builtin",
                tool_name=tool.name,
                effect="allowlist_auto",
                schema_hash=canonical_json_sha256(tool.input_schema),
                created_by="test",
            )
        )
    gate = PreToolCallGate(store, registry=registry)
    executor = UnifiedToolExecutor(store, gate=gate)
    invoked = []
    knowledge_base_id = uuid4()

    async def invoke_builtin(arguments, context, *, tool):
        invoked.append(tool.name)
        return await tool.execute(dict(arguments), context)

    for tool in (search, rag):
        executor.register_invoker(
            server_id="builtin",
            tool_name=tool.name,
            invoker=lambda arguments, context, tool=tool: invoke_builtin(
                arguments,
                context,
                tool=tool,
            ),
            context_factory=lambda request: ToolContext(
                session_id=uuid4(),
                turn_id=uuid4(),
                tool_call_id=request.call_id,
                user_id="local-user",
                project_id="project",
                knowledge_base_id=knowledge_base_id,
            ),
        )
    context = ToolContext(
        session_id=uuid4(),
        turn_id=uuid4(),
        user_id="local-user",
        project_id="project",
        knowledge_base_id=knowledge_base_id,
    )

    result = await JobResearchOrchestrator(registry, executor).search_from_request(
        user_request="根据我的简历查询深圳的岗位",
        context=context,
        provider=_InvalidProfileProvider(),
        model="profile-test-model",
    )

    assert result.status == "search_profile_required"
    assert result.error_code == "invalid_json"
    assert invoked == ["retrieve_resume_evidence"]
    events = [
        event
        for event in store.list_audit_events()
        if event.action == "model.job_search_profile.completed"
    ]
    assert len(events) == 2
    assert all(event.decision == "error" for event in events)
    assert all(event.reason_code == "invalid_json" for event in events)
    assert all(event.session_id == str(context.session_id) for event in events)
    assert all(event.turn_id == str(context.turn_id) for event in events)
    for event in events:
        assert set(event.payload) == {
            "attempt",
            "model_request_id",
            "output_length",
            "fields",
            "error_code",
            "provider",
            "model",
            "issues",
        }
        assert tuple(event.payload["issues"]) == ("$:json_invalid",)
        assert "简历" not in str(event.payload)
        assert "这不是 JSON" not in str(event.payload)


async def test_bootstrap_application_entry_fails_closed_after_real_browser_publish(
    tmp_path,
    monkeypatch,
):
    from starter_agent import bootstrap

    settings = load_settings("config/config.example.yaml")
    settings.project_root = tmp_path
    settings.app.database_url = "sqlite:///agent.db"
    settings.app.identity_path = "agent.md"
    settings.providers["mock"].models = ["starter-mock"]
    (tmp_path / "agent.md").write_text("# Agent", encoding="utf-8")
    prompts = tmp_path / "config" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "system.md").write_text("{identity}", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    bootstrap.create_application.cache_clear()
    application = bootstrap.create_application()
    assert application.job_research is None
    assert application.single_agent_baseline_runner is not None

    executor = application.runtime.executor
    store = application.runtime.gate.store
    manager = McpManager(
        McpConfiguration(
            source_path=tmp_path / "mcp.json",
            servers={"playwright": McpServerConfig(command="npx")},
            config_hash="f" * 64,
        ),
        store=store,
        client_factory=lambda _server_id, _config: object(),
        tool_executor=executor,
    )
    handle = manager._get_handle("playwright")
    current = store.get_server("playwright")
    assert current is not None
    ready = store.update_server(
        "playwright",
        expected_revision=current.revision,
        enabled=True,
        connection_state="ready",
    )
    handle.status = ready
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    }
    browser = McpTool(
        snapshot_id="production-browser-snapshot",
        server_id="playwright",
        upstream_name="browser_navigate",
        model_alias="mcp__playwright__browser_navigate",
        description="Navigate to a public page",
        input_schema=schema,
        schema_hash=canonical_json_sha256(schema),
        metadata={"browser": True, "action": "navigate"},
        risk_level="read",
        outbound_scope=("public_url",),
        enabled=True,
        review_state="approved",
    )
    snapshot = Snapshot(
        id=browser.snapshot_id,
        server_id="playwright",
        version=1,
        schema_hash=browser.schema_hash,
        discovered_at=datetime.now(UTC),
        tool_count=1,
    )
    store.create_snapshot(snapshot, tools=[browser])
    active = store.activate_snapshot("playwright", snapshot.id)
    store.update_tool(
        active.id,
        browser.upstream_name,
        expected_revision=0,
        review_state="approved",
    )
    handle.active.snapshot_id = active.id
    manager._publish_snapshot(handle, active)

    capability = application.runtime.tools.resolve_execution(browser.model_alias)
    assert capability is not None
    assert capability.enabled and capability.connected
    assert executor.has_invoker("playwright", "browser_navigate") is False

    with pytest.raises(RuntimeError, match="legacy_path_forbidden"):
        await application.analyze_job_research(
            query="AI Agent engineer",
            selected_url="https://jobs.example/agent",
            session_id=uuid4(),
        )
    assert executor.has_invoker("playwright", "browser_navigate") is False
    bootstrap.create_application.cache_clear()
