from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp import types

from starter_agent.capabilities.models import (
    Confirmation,
    ExecutionPermit,
    PolicyRule,
)
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.mcp.client import ClientMetadata, McpClientError
from starter_agent.mcp.config import McpConfiguration, McpServerConfig
from starter_agent.mcp.manager import McpManager, McpManagerError


class _Session:
    def __init__(self, schema: dict[str, object]) -> None:
        self.schema = schema

    async def list_tools(self, *, params=None):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="find_jobs",
                    title="Jobs",
                    inputSchema=self.schema,
                )
            ]
        )

    async def list_resources(self, *, params=None):
        return types.ListResourcesResult(resources=[])

    async def list_resource_templates(self, *, params=None):
        return types.ListResourceTemplatesResult(resourceTemplates=[])

    async def list_prompts(self, *, params=None):
        return types.ListPromptsResult(prompts=[])


class _Client:
    def __init__(
        self,
        server_id: str,
        schema: dict[str, object],
        *,
        connect_error: McpClientError | None = None,
    ) -> None:
        self.server_id = server_id
        self._session = _Session(schema)
        self.session = None
        self.stderr_summary = ""
        self.connect_error = connect_error
        self.closed = False

    async def connect(self) -> ClientMetadata:
        if self.connect_error is not None:
            raise self.connect_error
        self.session = self._session
        return ClientMetadata(
            protocol_version="2025-06-18",
            runtime_name=f"fixture-{self.server_id}",
            runtime_version="1.0.0",
            node_version="v22.1.0",
            npx_version="10.8.0",
            started_at=datetime.now(UTC),
        )

    async def run_session_command(self, operation):
        return await operation(self.session)

    async def close(self) -> None:
        self.closed = True
        self.session = None


def _configuration(tmp_path: Path) -> McpConfiguration:
    return McpConfiguration(
        source_path=tmp_path / "mcp.json",
        servers={"alpha": McpServerConfig(command="npx")},
        config_hash="a" * 64,
    )


@pytest.mark.asyncio
async def test_equivalent_live_refresh_keeps_strict_active_snapshot_id(
    tmp_path: Path,
) -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    clients = [_Client("alpha", schema), _Client("alpha", schema)]

    def factory(server_id: str, _config: McpServerConfig) -> _Client:
        client = clients.pop(0)
        assert client.server_id == server_id
        return client

    store = CapabilityStore("sqlite:///:memory:", tmp_path)
    manager = McpManager(
        _configuration(tmp_path),
        store=store,
        client_factory=factory,
        initialize_timeout_seconds=0.2,
        shutdown_timeout_seconds=0.2,
    )
    await manager.start()
    initial = await manager.discover("alpha")

    refreshed = await manager.refresh_server(
        "alpha", manager.get_status("alpha").revision
    )

    assert refreshed.id == initial.id
    assert store.get_active_snapshot("alpha").id == initial.id
    assert store.get_snapshot("alpha-snapshot-2").active is False


@pytest.mark.asyncio
async def test_refresh_swaps_only_valid_candidate_and_invalidates_changed_schema(
    tmp_path: Path,
) -> None:
    old_schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    new_schema = {"type": "object", "properties": {"q": {"type": "number"}}}
    clients: list[_Client] = []
    candidates = [
        _Client("alpha", old_schema),
        _Client("alpha", new_schema),
        _Client(
            "alpha",
            new_schema,
            connect_error=McpClientError(
                "candidate_failed",
                "token=must-not-be-persisted",
                transport_closed=True,
            ),
        ),
    ]

    def factory(server_id: str, _config: McpServerConfig) -> _Client:
        client = candidates[len(clients)]
        clients.append(client)
        assert client.server_id == server_id
        return client

    store = CapabilityStore("sqlite:///:memory:", tmp_path)
    manager = McpManager(
        _configuration(tmp_path),
        store=store,
        client_factory=factory,
        initialize_timeout_seconds=0.2,
        shutdown_timeout_seconds=0.2,
    )
    await manager.start()
    old_snapshot = await manager.discover("alpha")
    old_tool = store.list_tools(old_snapshot.id)[0]
    old_tool = store.update_tool(
        old_snapshot.id,
        old_tool.upstream_name,
        expected_revision=0,
        review_state="approved",
    )
    assert old_tool.reviewed_at is not None
    now = datetime.now(UTC)
    rule = PolicyRule(
        id="auto-find-jobs",
        server_id="alpha",
        tool_name=old_tool.upstream_name,
        effect="allowlist_auto",
        schema_hash=old_tool.schema_hash,
        created_by="test",
    )
    confirmation = Confirmation(
        id="confirmation-find-jobs",
        principal="test",
        session_id="session-1",
        turn_id="turn-1",
        call_id="call-1",
        request_hash="b" * 64,
        server_id="alpha",
        tool_name=old_tool.upstream_name,
        schema_hash=old_tool.schema_hash,
        arguments_summary={"q": "python"},
        risk="external",
        destination="jobs.example",
        decision="once",
        status="approved",
        expires_at=now + timedelta(minutes=5),
        idempotency_key_hash=hashlib.sha256(b"approve").hexdigest(),
        decided_at=now,
    )
    permit = ExecutionPermit(
        id="permit-find-jobs",
        confirmation_id=confirmation.id,
        request_hash=confirmation.request_hash,
        policy_revision=rule.revision,
        expires_at=confirmation.expires_at,
    )
    store.create_policy_rule(rule)
    store.create_confirmation(confirmation)
    store.create_execution_permit(permit)

    revision = manager.get_status("alpha").revision
    refreshed = await manager.refresh_server("alpha", revision)

    assert refreshed.version == old_snapshot.version + 1
    assert refreshed.active is True
    assert manager._get_handle("alpha").client is clients[1]
    assert clients[0].closed is True
    changed_tool = store.list_tools(refreshed.id)[0]
    assert changed_tool.upstream_name == old_tool.upstream_name
    assert changed_tool.schema_hash != old_tool.schema_hash
    assert changed_tool.enabled is False
    assert changed_tool.review_state == "review_required"
    assert changed_tool.reviewed_at is None
    assert store.get_policy_rule(rule.id).enabled is False
    assert store.get_confirmation(confirmation.id).status == "invalidated"
    assert store.get_execution_permit(permit.id).consumed_at is not None

    active_before_failure = store.get_active_snapshot("alpha")
    stale_revision = revision
    with pytest.raises(McpManagerError) as conflict:
        await manager.refresh_server("alpha", stale_revision)
    assert conflict.value.code == "revision_conflict"
    assert len(clients) == 2

    current_revision = manager.get_status("alpha").revision
    with pytest.raises(McpManagerError) as failed:
        await manager.refresh_server("alpha", current_revision)
    assert failed.value.code == "candidate_failed"
    assert manager._get_handle("alpha").client is clients[1]
    assert clients[2].closed is True
    stale = store.get_active_snapshot("alpha")
    assert stale.id == active_before_failure.id
    assert stale.stale is True
    assert stale.error == "candidate_failed"
    assert "must-not-be-persisted" not in (manager.get_status("alpha").last_error or "")
