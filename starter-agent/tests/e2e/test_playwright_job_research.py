from __future__ import annotations

import asyncio
import json
import re
import socket
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.capabilities.confirmations import (
    ConfirmationService,
    TurnCoordinator,
)
from starter_agent.capabilities.gate import PreToolCallGate, UnifiedToolExecutor
from starter_agent.capabilities.models import PolicyRule
from starter_agent.capabilities.policy import BrowserScopePolicy
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.domain.models import Message, ModelResponse, ToolCall
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.job_research.jd import (
    JobDescriptionIngestionError,
    JobDescriptionIngestionService,
)
from starter_agent.knowledge.service import KnowledgeApplicationService
from starter_agent.knowledge.store import SQLiteKnowledgeStore
from starter_agent.mcp.config import (
    McpConfigLoader,
    McpConfiguration,
    McpServerConfig,
)
from starter_agent.mcp.manager import McpManager
from starter_agent.mcp.network_guard import PlaywrightNetworkGuard
from starter_agent.interfaces.capabilities_api import (
    CapabilityApiServices,
    create_capabilities_router,
    get_capability_services,
)
from starter_agent.providers.base import Provider
from starter_agent.settings import ContextConfig, RuntimeConfig, load_settings
from starter_agent.skills.job_research import JobResearchOrchestrator
from starter_agent.tools.base import ToolContext
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_JD_URL = (
    "https://jobs.lever.co/payugpo/"
    "49975338-7270-422e-a3c1-e2375394cef4"
)


class _LiveJobProvider(Provider):
    name = "task16-live-job"

    def __init__(
        self,
        navigate_name: str,
        snapshot_name: str,
        *,
        target_url: str = PUBLIC_JD_URL,
    ) -> None:
        self.navigate_name = navigate_name
        self.snapshot_name = snapshot_name
        self.target_url = target_url
        self.provider_tool_requests: list[list[dict]] = []

    async def complete(
        self, messages, model, tools, on_delta=None, tool_choice=None
    ):
        del on_delta, tool_choice
        self.provider_tool_requests.append(tools)
        completed = sum(message.role == "tool" for message in messages)
        if completed == 0:
            call = ToolCall(
                id="task16-runtime-navigate",
                name=self.navigate_name,
                arguments={"url": self.target_url},
            )
        elif completed == 1:
            call = ToolCall(
                id="task16-runtime-snapshot",
                name=self.snapshot_name,
                arguments={},
            )
        else:
            return ModelResponse(
                content="JD read complete",
                provider=self.name,
                model=model,
            )
        return ModelResponse(
            provider=self.name,
            model=model,
            tool_calls=[call],
        )

    async def health(self, model):
        return True, f"{model}:ready"


class _CaptureProvider(Provider):
    name = "task16-capture"

    def __init__(self) -> None:
        self.tools: list[dict] = []

    async def complete(
        self, messages, model, tools, on_delta=None, tool_choice=None
    ):
        del messages, on_delta, tool_choice
        self.tools = tools
        return ModelResponse(
            content="captured", provider=self.name, model=model
        )

    async def health(self, model):
        return True, f"{model}:ready"


class _UiControlProvider(Provider):
    name = "task16-ui-controls"

    def __init__(self, navigate: str, snapshot: str, url: str):
        self.navigate = navigate
        self.snapshot = snapshot
        self.url = url
        self.step = 0
        self.polls = 0

    @staticmethod
    def _ref(messages, label: str) -> str:
        payload = json.loads(str(messages[-1].content))
        text = str(payload["data"]["content"][0]["text"])
        match = re.search(
            rf'button\s+"{re.escape(label)}"\s+\[ref=([^\]]+)\]',
            text,
        )
        if match is None:
            raise AssertionError(f"UI button not found: {label}")
        return match.group(1)

    async def complete(
        self, messages, model, tools, on_delta=None, tool_choice=None
    ):
        del tools, on_delta, tool_choice
        actions = (
            (self.navigate, {"url": self.url}),
            (self.snapshot, {}),
        )
        if self.step >= len(actions):
            return ModelResponse(
                content="UI controls exercised",
                provider=self.name,
                model=model,
            )
        name, arguments = actions[self.step]
        if self.step == 1:
            await asyncio.sleep(1)
        if callable(arguments):
            try:
                arguments = arguments()
            except AssertionError as exc:
                raise AssertionError(
                    f"{exc}; snapshot={str(messages[-1].content)[:3000]}"
                ) from exc
        self.step += 1
        return ModelResponse(
            provider=self.name,
            model=model,
            tool_calls=[
                ToolCall(
                    id=f"task16-ui-control-{self.step}",
                    name=name,
                    arguments=arguments,
                )
            ],
        )

    async def health(self, model):
        return True, f"{model}:ready"


async def _wait_pending(
    confirmations: ConfirmationService, session_id: str
):
    for _ in range(900):
        pending = confirmations.list_pending(session_id=session_id)
        if pending:
            return pending[-1]
        await asyncio.sleep(0.1)
    raise AssertionError("confirmation was not persisted")


@pytest.mark.external
@pytest.mark.asyncio
async def test_real_playwright_mcp_initializes_and_discovers(
    tmp_path: Path,
) -> None:
    ui_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ui_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ui_socket.bind(("127.0.0.1", 0))
    ui_socket.listen(socket.SOMAXCONN)
    ui_host, ui_port = ui_socket.getsockname()
    ui_socket.close()
    ui_origin = f"http://{ui_host}:{ui_port}"
    ui_server: uvicorn.Server | None = None
    ui_task: asyncio.Task[None] | None = None
    configuration = McpConfigLoader(PROJECT_ROOT).load("config/mcp.json")
    assert configuration.servers["playwright"].command == "npx"
    assert configuration.servers["playwright"].args == (
        "@playwright/mcp@latest",
    )

    store = CapabilityStore("sqlite:///capabilities.db", tmp_path)
    settings = load_settings("config/config.example.yaml")
    settings = settings.model_copy(
        update={
            "project_root": tmp_path,
            "app": settings.app.model_copy(
                update={"database_url": "sqlite:///knowledge.db"}
            ),
        }
    )
    knowledge = KnowledgeApplicationService(
        settings,
        SQLiteKnowledgeStore(settings.app.database_url, tmp_path),
    )
    resume_upload = knowledge.upload(
        knowledge_base_id=knowledge.default_knowledge_base_id,
        filename="task16-public-resume-fixture.md",
        content=(
            b"# Resume\n\n## Skills\n\n"
            b"Built Python machine learning evaluation pipelines "
            b"and reliable model services.\n"
        ),
        document_type="resume",
        confirmed_authorized=True,
    )
    knowledge.require_upload_succeeded(resume_upload)
    builtins = ToolRegistry(
        ["retrieve_resume_evidence"],
        settings,
        knowledge_service=knowledge,
    )
    registry = UnifiedToolRegistry(
        builtins,
        allowed_risk_levels=["read", "external"],
    )
    gate = PreToolCallGate(
        store,
        registry=registry,
        browser_policy=BrowserScopePolicy(control_origins=(ui_origin,)),
    )
    executor = UnifiedToolExecutor(store, gate=gate)
    rag_tool = builtins.get("retrieve_resume_evidence")
    assert rag_tool is not None

    async def invoke_rag(arguments, context):
        return await rag_tool.execute(dict(arguments), context)

    executor.register_invoker(
        server_id="builtin",
        tool_name=rag_tool.name,
        invoker=invoke_rag,
        context_factory=lambda request: ToolContext(
            session_id=UUID(request.session_id),
            turn_id=UUID(request.turn_id),
            tool_call_id=request.call_id,
            user_id=knowledge.scope.user_id,
            project_id=knowledge.scope.project_id,
            knowledge_base_id=knowledge.default_knowledge_base_id,
        ),
    )
    network_guard = PlaywrightNetworkGuard(control_origins=(ui_origin,))
    manager = McpManager(
        configuration,
        store=store,
        initialize_timeout_seconds=60,
        shutdown_timeout_seconds=15,
        tool_executor=executor,
        browser_network_guard=network_guard,
    )
    try:
        print("stage:mcp_start", flush=True)
        statuses = await asyncio.wait_for(manager.start(), timeout=90)
        status = statuses["playwright"]
        assert status.connection_state == "ready", status.model_dump_json(indent=2)
        assert status.runtime_name != "unknown"
        assert status.runtime_version != "unknown"
        assert status.node_version != "unknown"
        assert status.npx_version != "unknown"

        print("stage:discover", flush=True)
        initial_snapshot = await asyncio.wait_for(
            manager.discover("playwright"), timeout=45
        )
        initial_status = manager.get_status("playwright")
        print("stage:refresh", flush=True)
        snapshot = await asyncio.wait_for(
            manager.refresh_server(
                "playwright",
                expected_revision=initial_status.revision,
            ),
            timeout=90,
        )
        assert snapshot.active is True
        assert snapshot.stale is False
        assert snapshot.tool_count > 0
        assert snapshot.version == initial_snapshot.version + 1
        assert store.get_active_snapshot("playwright").id == snapshot.id
        assert len(store.list_tools(snapshot.id)) == snapshot.tool_count
        assert len(store.list_resources(snapshot.id)) == snapshot.resource_count
        assert len(store.list_prompts(snapshot.id)) == snapshot.prompt_count
        initial_persisted = store.get_snapshot_summary("playwright")
        assert initial_persisted is not None

        navigate = next(
            tool
            for tool in store.list_tools(snapshot.id)
            if tool.upstream_name == "browser_navigate"
        )
        reviewed = store.update_tool(
            snapshot.id,
            navigate.upstream_name,
            expected_revision=navigate.revision,
            review_state="approved",
            enabled=True,
        )
        manager._publish_snapshot(manager._get_handle("playwright"), snapshot)
        capability = registry.resolve_execution(reviewed.model_alias)
        assert capability is not None
        assert capability.enabled is True
        assert capability.review_state == "approved"
        assert capability.schema_hash == reviewed.schema_hash
        assert executor.has_invoker(
            "playwright",
            reviewed.upstream_name,
        )

        snapshot_tool = next(
            tool
            for tool in store.list_tools(snapshot.id)
            if tool.upstream_name == "browser_snapshot"
        )
        snapshot_tool = store.update_tool(
            snapshot.id,
            snapshot_tool.upstream_name,
            expected_revision=snapshot_tool.revision,
            review_state="approved",
            enabled=True,
        )
        manager._publish_snapshot(manager._get_handle("playwright"), snapshot)
        confirmations = ConfirmationService(store, gate)
        runtime = AgentRuntime(
            registry,
            ToolPolicy(["read", "external"]),
            RuntimeConfig(
                max_tool_calls=20,
                max_seconds=240,
                tool_timeout_seconds=120,
            ),
            ContextConfig(
                per_tool_result_tokens=20_000,
                all_tool_results_tokens=40_000,
            ),
            gate=gate,
            executor=executor,
            turn_coordinator=TurnCoordinator(
                confirmations,
                confirmation_timeout_seconds=90,
            ),
            knowledge_scope=knowledge.scope,
            knowledge_base_id=knowledge.default_knowledge_base_id,
        )
        provider = _LiveJobProvider(
            reviewed.model_alias, snapshot_tool.model_alias
        )
        session_id, turn_id = uuid4(), uuid4()
        session_store = SQLiteSessionStore(
            "sqlite:///sessions.db", tmp_path
        )
        events: list[dict] = []

        async def on_event(event: dict) -> None:
            events.append(event)
            if event["type"] == "tool_started":
                print(f"stage:{event['call_id']}", flush=True)

        async def on_artifact(event: dict) -> None:
            session_store.save_tool_artifact(**event)

        runtime_task = asyncio.create_task(
            runtime.run(
                provider=provider,
                model="task16-live",
                messages=[
                    Message(
                        role="user",
                        content="Read the selected public job description.",
                    )
                ],
                session_id=session_id,
                turn_id=turn_id,
                on_tool_event=on_event,
                on_tool_artifact=on_artifact,
            )
        )
        navigate_pending = await _wait_pending(
            confirmations, str(session_id)
        )
        assert navigate_pending.call_id == "task16-runtime-navigate"
        assert not any(
            event.action == "tool.invoked"
            and event.call_id == navigate_pending.call_id
            for event in store.list_audit_events()
        )
        navigate_approved = confirmations.decide(
            navigate_pending.id,
            expected_revision=navigate_pending.revision,
            idempotency_key="task16-runtime-navigate-once",
            decision="once",
            actor="local-user",
        )
        snapshot_pending = await _wait_pending(
            confirmations, str(session_id)
        )
        assert snapshot_pending.call_id == "task16-runtime-snapshot"
        snapshot_approved = confirmations.decide(
            snapshot_pending.id,
            expected_revision=snapshot_pending.revision,
            idempotency_key="task16-runtime-snapshot-once",
            decision="once",
            actor="local-user",
        )
        await asyncio.wait_for(runtime_task, timeout=180)

        snapshot_artifact = session_store.get_tool_artifact(
            f"tool:{snapshot_tool.model_alias}:{turn_id}:"
            "task16-runtime-snapshot"
        )
        assert snapshot_artifact is not None
        assert snapshot_artifact["restricted"] is True
        artifact_envelope = json.loads(snapshot_artifact["content"])
        structured = artifact_envelope["data"]["structured_content"]
        assert structured["title"].startswith("Machine Learning Engineer")
        assert structured["company"] == "PayU GPO"
        assert structured["location"]
        assert structured["responsibilities"]
        assert structured["requirements"]
        assert snapshot_artifact["source_url"] == PUBLIC_JD_URL
        assert snapshot_artifact["server_id"] == "playwright"
        assert snapshot_artifact["snapshot_id"] == snapshot.id
        assert snapshot_artifact["schema_hash"] == snapshot_tool.schema_hash
        completed = next(
            event
            for event in events
            if event.get("call_id") == "task16-runtime-snapshot"
            and event["type"] == "tool_completed"
        )
        assert completed["raw_source_ref"] == snapshot_artifact["source_ref"]
        assert completed["trace_ref"] == (
            f"trace:{session_id}:{turn_id}:task16-runtime-snapshot"
        )
        assert completed["audit_ref"].startswith("audit-")
        assert network_guard.connection_targets

        def provider_names(tools: list[dict]) -> set[str]:
            return {
                str(item.get("name", item.get("function", {}).get("name")))
                for item in tools
            }

        assert snapshot_tool.model_alias in provider_names(
            provider.provider_tool_requests[0]
        )
        current_snapshot_tool = next(
            item
            for item in store.list_tools(snapshot.id)
            if item.upstream_name == "browser_snapshot"
        )
        disabled_snapshot_tool = store.update_tool(
            snapshot.id,
            current_snapshot_tool.upstream_name,
            expected_revision=current_snapshot_tool.revision,
            enabled=False,
        )
        manager._publish_snapshot(manager._get_handle("playwright"), snapshot)
        disabled_capture = _CaptureProvider()
        await runtime.run(
            disabled_capture,
            "task16-live",
            [Message(role="user", content="capture disabled tools")],
            uuid4(),
            uuid4(),
        )
        assert snapshot_tool.model_alias not in provider_names(
            disabled_capture.tools
        )
        restored_snapshot_tool = store.update_tool(
            snapshot.id,
            disabled_snapshot_tool.upstream_name,
            expected_revision=disabled_snapshot_tool.revision,
            enabled=True,
            review_state="approved",
        )
        manager._publish_snapshot(manager._get_handle("playwright"), snapshot)
        enabled_capture = _CaptureProvider()
        await runtime.run(
            enabled_capture,
            "task16-live",
            [Message(role="user", content="capture enabled tools")],
            uuid4(),
            uuid4(),
        )
        assert restored_snapshot_tool.model_alias in provider_names(
            enabled_capture.tools
        )

        resume_matches = knowledge.retrieve(
            knowledge.default_knowledge_base_id,
            "Python machine learning evaluation pipelines",
            document_types=["resume"],
        )
        assert resume_matches
        assert all(match.source_ref for match in resume_matches)
        rag_capability = registry.resolve_execution(rag_tool.name)
        assert rag_capability is not None
        for rule in (
            PolicyRule(
                id="task16-allow-live-navigate",
                server_id="playwright",
                tool_name=reviewed.upstream_name,
                effect="allowlist_auto",
                actions=("navigate",),
                schema_hash=reviewed.schema_hash,
                created_by="task16-confirmed-user",
            ),
            PolicyRule(
                id="task16-allow-live-rag",
                server_id="builtin",
                tool_name=rag_tool.name,
                effect="allowlist_auto",
                actions=("read",),
                schema_hash=rag_capability.schema_hash,
                created_by="task16-confirmed-user",
            ),
            PolicyRule(
                id="task16-allow-live-snapshot",
                server_id="playwright",
                tool_name=snapshot_tool.upstream_name,
                effect="allowlist_auto",
                actions=("read",),
                schema_hash=snapshot_tool.schema_hash,
                created_by="task16-confirmed-user",
            ),
        ):
            store.create_policy_rule(rule)
        orchestrator = JobResearchOrchestrator(registry, executor)
        research = await orchestrator.analyze(
            query="Python machine learning evaluation pipelines",
            selected_url=PUBLIC_JD_URL,
            context=ToolContext(
                session_id=uuid4(),
                turn_id=uuid4(),
                user_id=knowledge.scope.user_id,
                project_id=knowledge.scope.project_id,
                knowledge_base_id=knowledge.default_knowledge_base_id,
            ),
        )
        assert research.status == "waiting_for_jd_ingestion_confirmation", (
            research.model_dump()
        )
        assert [item.tool_name for item in research.trace] == [
            reviewed.model_alias,
            snapshot_tool.model_alias,
            rag_tool.name,
            rag_tool.name,
        ]
        assert research.trace[-1].arguments["query"] == "我的简历匹配这个岗位"
        assert all(item.result for item in research.trace)
        analysis = research.data["analysis"]
        matched = [row for row in analysis if row["status"] == "matched"]
        assert matched
        assert all(
            item["source_ref"]
            for row in matched
            for item in row["evidence"]
        )
        assert all(
            row["evidence"] == []
            for row in analysis
            if row["status"] == "gap"
        )

        ingestion = JobDescriptionIngestionService(
            knowledge, session_store
        )
        challenge = ingestion.prepare(
            source_ref=str(snapshot_artifact["source_ref"]),
            principal="local-user",
            session_id=session_id,
        )
        assert challenge.status == "pending"
        assert not [
            document
            for document in knowledge.list_documents(
                knowledge.default_knowledge_base_id
            )
            if document.document_type == "job_description"
        ]
        with pytest.raises(
            JobDescriptionIngestionError,
            match="confirmation_not_approved",
        ):
            ingestion.ingest(
                challenge.id,
                principal="local-user",
                session_id=session_id,
            )
        ingestion.approve(
            challenge.id,
            principal="local-user",
            session_id=session_id,
        )
        receipt = ingestion.ingest(
            challenge.id,
            principal="local-user",
            session_id=session_id,
        )
        stored_jd = knowledge.get_document(
            knowledge.default_knowledge_base_id, receipt.document_id
        )
        assert stored_jd.document_type == "job_description"
        jd_matches = knowledge.retrieve(
            knowledge.default_knowledge_base_id,
            structured["title"],
            document_types=["job_description"],
        )
        assert jd_matches
        assert all(match.source_ref for match in jd_matches)
        assert receipt.trace.artifact_ref == snapshot_artifact["source_ref"]
        assert receipt.trace.confirmation_id == challenge.id

        services = CapabilityApiServices(
            manager=manager,
            registry=registry,
            skill_registry=SimpleNamespace(),
            confirmations=confirmations,
            store=store,
            application=SimpleNamespace(),
        )
        ui = FastAPI()
        ui.include_router(create_capabilities_router())
        ui.dependency_overrides[get_capability_services] = lambda: services
        html = (PROJECT_ROOT / "frontend/web/index.html").read_text(encoding="utf-8")
        css = (PROJECT_ROOT / "frontend/web/styles/legacy.css").read_text(encoding="utf-8")
        script = (PROJECT_ROOT / "frontend/web/app.js").read_text(encoding="utf-8")
        # The fixture serves a single HTML route; inline split assets while preserving
        # the production module layout exercised by static contract tests.
        script = "\n".join(
            line for line in script.splitlines() if not line.startswith("import ")
        ).replace(
            "window.StarterAgentModules = Object.freeze({ createApiClient, createHashRouter, createStore });",
            "",
        )
        html = html.replace(
            '<link rel="stylesheet" href="./styles/app.css">',
            f"<style>{css}</style>",
        ).replace(
            '<script type="module" src="./app.js"></script>',
            f"<script>{script}</script>",
        )
        html = html.replace(
            'id="apiBase" value="http://127.0.0.1:8000"',
            f'id="apiBase" value="{ui_origin}"',
        )

        @ui.get("/", response_class=HTMLResponse)
        async def capability_ui() -> str:
            return html

        ui_server = uvicorn.Server(
            uvicorn.Config(
                ui,
                host=ui_host,
                port=ui_port,
                log_level="warning",
                lifespan="off",
            )
        )
        ui_task = asyncio.create_task(ui_server.serve())
        for _ in range(100):
            if ui_server.started:
                break
            await asyncio.sleep(0.05)
        assert ui_server.started

        async def fetch_ui(host: str, port: int, target: str) -> bytes:
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                (
                    f"GET {target} HTTP/1.1\r\n"
                    f"Host: {ui_host}:{ui_port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), timeout=10)
            writer.close()
            await writer.wait_closed()
            return response

        direct_ui = await fetch_ui(ui_host, ui_port, "/")
        assert direct_ui.startswith(b"HTTP/1.1 200"), direct_ui[:200]
        proxy_host, proxy_port = network_guard.address
        proxied_ui = await fetch_ui(proxy_host, proxy_port, f"{ui_origin}/")
        assert proxied_ui.startswith(b"HTTP/1.1 200"), (
            proxied_ui[:200],
            network_guard.last_proxy_error,
        )

        ui_provider = _UiControlProvider(
            reviewed.model_alias,
            snapshot_tool.model_alias,
            f"{ui_origin}/#/capabilities/mcp-servers",
        )
        ui_session_id, ui_turn_id = uuid4(), uuid4()
        ui_artifacts: list[dict] = []
        ui_events: list[dict] = []

        async def on_ui_artifact(event: dict) -> None:
            ui_artifacts.append(event)
            session_store.save_tool_artifact(**event)

        async def on_ui_event(event: dict) -> None:
            ui_events.append(event)

        ui_run = asyncio.create_task(
            runtime.run(
                ui_provider,
                "task16-live",
                [Message(role="user", content="Open capability management.")],
                ui_session_id,
                ui_turn_id,
                on_tool_event=on_ui_event,
                on_tool_artifact=on_ui_artifact,
            )
        )
        decision_index = 0
        while not ui_run.done():
            pending_values = confirmations.list_pending(
                session_id=str(ui_session_id)
            )
            for pending in pending_values:
                decision_index += 1
                confirmations.decide(
                    pending.id,
                    expected_revision=pending.revision,
                    idempotency_key=f"task16-ui-control-{decision_index}",
                    decision="once",
                    actor="local-user",
                )
            await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(ui_run, timeout=120)
        except Exception:
            print(
                f"stage:ui-proxy-error:{network_guard.last_proxy_error}",
                flush=True,
            )
            raise
        ui_snapshot_artifact = next(
            artifact
            for artifact in reversed(ui_artifacts)
            if artifact["tool_name"] == snapshot_tool.model_alias
        )
        ui_snapshot_envelope = json.loads(str(ui_snapshot_artifact["content"]))
        ui_snapshot_text = str(
            ui_snapshot_envelope["data"]["content"][0]["text"]
        )
        assert "MCP Servers" in ui_snapshot_text
        assert "playwright" in ui_snapshot_text
        assert "browser_snapshot" in ui_snapshot_text
        assert ui_snapshot_artifact["source_url"] == f"{ui_origin}/"
        assert any(
            event.get("trace_ref")
            == f"trace:{ui_session_id}:{ui_turn_id}:task16-ui-control-2"
            for event in ui_events
        )
        print(
            json.dumps(
                {
                    "runtime": {
                        "name": status.runtime_name,
                        "version": status.runtime_version,
                        "protocol": status.protocol_version,
                        "node": status.node_version,
                        "npx": status.npx_version,
                    },
                    "snapshot": {
                        "id": snapshot.id,
                        "version": snapshot.version,
                        "schema_hash": snapshot.schema_hash,
                        "tool_count": snapshot.tool_count,
                        "resource_count": snapshot.resource_count,
                        "prompt_count": snapshot.prompt_count,
                    },
                    "browser_call": {
                        "url": PUBLIC_JD_URL,
                        "tool": reviewed.upstream_name,
                        "schema_hash": reviewed.schema_hash,
                        "confirmation_id": navigate_approved.id,
                        "snapshot_confirmation_id": snapshot_approved.id,
                        "artifact_ref": snapshot_artifact["source_ref"],
                        "trace_ref": completed["trace_ref"],
                        "content_sha256": snapshot_artifact["content_sha256"],
                        "proxy_connect_count": len(
                            network_guard.connection_targets
                        ),
                        "resume_source_ref": resume_matches[0].source_ref,
                        "jd_document_id": str(receipt.document_id),
                        "jd_source_ref": jd_matches[0].source_ref,
                    },
                },
                ensure_ascii=False,
            )
        )
    finally:
        print("stage:shutdown", flush=True)
        if ui_server is not None:
            ui_server.should_exit = True
        if ui_task is not None:
            await asyncio.wait_for(ui_task, timeout=15)
        elif ui_socket.fileno() != -1:
            ui_socket.close()
        await asyncio.wait_for(manager.shutdown(), timeout=45)


@pytest.mark.external
@pytest.mark.asyncio
async def test_unavailable_mcp_records_authoritative_degraded_state(
    tmp_path: Path,
) -> None:
    store = CapabilityStore("sqlite:///unavailable.db", tmp_path)
    manager = McpManager(
        McpConfiguration(
            source_path=tmp_path / "unavailable-mcp.json",
            servers={
                "unavailable": McpServerConfig(
                    command="starter-agent-task16-command-does-not-exist"
                )
            },
            config_hash="d" * 64,
        ),
        store=store,
        initialize_timeout_seconds=2,
        shutdown_timeout_seconds=2,
    )

    statuses = await asyncio.wait_for(manager.start(), timeout=10)
    status = statuses["unavailable"]
    assert status.connection_state == "failed", (
        status.model_dump()
    )
    assert status.health_state == "unhealthy"
    assert status.error_code
    assert status.last_error
    assert store.get_active_snapshot("unavailable") is None
    assert store.get_snapshot_summary("unavailable") is None
    registry = UnifiedToolRegistry(ToolRegistry([]))
    gate = PreToolCallGate(store, registry=registry)
    orchestrator = JobResearchOrchestrator(
        registry, UnifiedToolExecutor(store, gate=gate)
    )
    result = await orchestrator.analyze(
        query="AI engineer",
        selected_url=PUBLIC_JD_URL,
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
    )
    assert result.status == "dependency_unavailable"
    assert result.error_code == "dependency_unavailable"
    assert result.data.get("analysis", []) == []
    assert result.trace == ()
    settings = load_settings("config/config.example.yaml").model_copy(
        update={
            "project_root": tmp_path,
            "app": load_settings("config/config.example.yaml").app.model_copy(
                update={"database_url": "sqlite:///unavailable-knowledge.db"}
            ),
        }
    )
    knowledge = KnowledgeApplicationService(
        settings,
        SQLiteKnowledgeStore(settings.app.database_url, tmp_path),
    )
    assert not [
        document
        for document in knowledge.list_documents(
            knowledge.default_knowledge_base_id
        )
        if document.document_type == "job_description"
    ]
    await asyncio.wait_for(manager.shutdown(), timeout=10)
    print(
        json.dumps(
            {
                "degraded": {
                    "server_id": status.id,
                    "connection_state": status.connection_state,
                    "health_state": status.health_state,
                    "error_code": status.error_code,
                    "last_error": status.last_error,
                }
            },
            ensure_ascii=False,
        )
    )
