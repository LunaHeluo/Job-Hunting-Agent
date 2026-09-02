from __future__ import annotations

import asyncio
import ipaddress
from types import SimpleNamespace

import pytest

from starter_agent.mcp.config import McpServerConfig
from starter_agent.mcp.network_guard import PlaywrightNetworkGuard


async def _public_resolver(_hostname: str):
    return [ipaddress.ip_address("93.184.216.34")]


@pytest.mark.asyncio
async def test_playwright_guard_binds_proxy_and_attests_https_targets() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        attestation = await guard(
            SimpleNamespace(
                arguments={"url": "https://jobs.example/role"}
            )
        )

        assert attestation.targets == ("https://jobs.example/role",)
        assert attestation.dns_pinned is True
        assert attestation.redirects_enforced is True
        assert attestation.peer_verified is True
        assert guard.proxy_url.startswith("http://127.0.0.1:")
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_playwright_guard_attests_public_http_targets() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        attestation = await guard(
            SimpleNamespace(arguments={"url": "http://jobs.example/role"})
        )

        assert attestation.targets == ("http://jobs.example/role",)
        assert attestation.dns_pinned is True
        assert attestation.redirects_enforced is True
        assert attestation.peer_verified is False
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_playwright_guard_allows_targetless_snapshot_only_after_navigation() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    snapshot = SimpleNamespace(
        tool_name="mcp__playwright__browser_snapshot",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={},
    )
    try:
        with pytest.raises(
            RuntimeError, match="browser_network_target_required"
        ):
            await guard(snapshot)

        navigate = SimpleNamespace(
            call_id="navigate-1",
            tool_name="mcp__playwright__browser_navigate",
            server_id="playwright",
            session_id="session-a",
            snapshot_id="snapshot-1",
            arguments={"url": "https://jobs.example/role"},
        )
        guard.activate_generation("playwright", "snapshot-1", 1)
        await guard(navigate)
        with pytest.raises(
            RuntimeError, match="browser_network_target_required"
        ):
            await guard(snapshot)

        guard._record_connection_target("jobs.example", 443)
        assert await guard.commit_navigation(
            navigate,
            lease_generation=1,
            final_url="https://jobs.example/role",
        ) is True
        attestation = await guard(snapshot)

        assert attestation.targets == ("https://jobs.example/role",)
        assert attestation.dns_pinned is True
        assert attestation.redirects_enforced is True
        assert attestation.peer_verified is True
        wait = await guard(
            SimpleNamespace(
                tool_name="mcp__playwright__browser_wait_for",
                server_id="playwright",
                session_id="session-a",
                snapshot_id="snapshot-1",
                arguments={"time": 1},
            )
        )
        assert wait.targets == ("https://jobs.example/role",)
        click = await guard(
            SimpleNamespace(
                tool_name="mcp__playwright__browser_click",
                server_id="playwright",
                session_id="session-a",
                snapshot_id="snapshot-1",
                arguments={"ref": "e42"},
            )
        )
        assert click.targets == ("https://jobs.example/role",)
        with pytest.raises(
            RuntimeError, match="browser_network_target_required"
        ):
            await guard(
                SimpleNamespace(
                    tool_name="mcp__playwright__browser_snapshot",
                    server_id="playwright",
                    session_id="session-b",
                    snapshot_id="snapshot-1",
                    arguments={},
                )
            )
        with pytest.raises(
            RuntimeError, match="browser_network_target_required"
        ):
            await guard(
                SimpleNamespace(
                    tool_name="mcp__playwright__browser_snapshot",
                    server_id="playwright",
                    session_id="session-a",
                    snapshot_id="snapshot-2",
                    arguments={},
                )
            )
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_exact_navigation_can_reuse_an_already_guarded_connection() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 1)
    guard._record_connection_target("jobs.example", 443)
    navigate = SimpleNamespace(
        call_id="navigate-reused",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/role"},
    )
    try:
        await guard(navigate)

        assert await guard.commit_navigation(
            navigate,
            lease_generation=1,
            final_url="https://jobs.example/role",
        ) is True
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_exact_cached_navigation_authorizes_only_the_staged_target() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 1)
    navigate = SimpleNamespace(
        call_id="navigate-cache-hit",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/role"},
    )
    snapshot = SimpleNamespace(
        tool_name="mcp__playwright__browser_snapshot",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={},
    )
    try:
        await guard(navigate)

        assert await guard.commit_navigation(
            navigate,
            lease_generation=1,
            final_url="https://jobs.example/role",
        ) is True
        assert (await guard(snapshot)).targets == (
            "https://jobs.example/role",
        )
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_failed_or_stale_navigation_never_authorizes_snapshot() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 7)
    failed = SimpleNamespace(
        call_id="failed",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/failed"},
    )
    snapshot = SimpleNamespace(
        call_id="snapshot",
        tool_name="mcp__playwright__browser_snapshot",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={},
    )
    try:
        succeeded = SimpleNamespace(
            call_id="succeeded",
            tool_name=failed.tool_name,
            server_id=failed.server_id,
            session_id=failed.session_id,
            snapshot_id=failed.snapshot_id,
            arguments={"url": "https://jobs.example/old-page"},
        )
        await guard(succeeded)
        guard._record_connection_target("jobs.example", 443)
        assert await guard.commit_navigation(
            succeeded,
            lease_generation=7,
            final_url="https://jobs.example/old-page",
        ) is True
        assert (await guard(snapshot)).targets == (
            "https://jobs.example/old-page",
        )

        await guard(failed)
        guard._record_connection_target("jobs.example", 443)
        guard.discard_navigation(failed)
        with pytest.raises(RuntimeError, match="browser_network_target_required"):
            await guard(snapshot)

        old = SimpleNamespace(
            call_id="old",
            tool_name=failed.tool_name,
            server_id=failed.server_id,
            session_id=failed.session_id,
            snapshot_id=failed.snapshot_id,
            arguments={"url": "https://jobs.example/old"},
        )
        await guard(old)
        guard.activate_generation("playwright", "snapshot-1", 8)
        guard._record_connection_target("jobs.example", 443)
        assert await guard.commit_navigation(
            old,
            lease_generation=7,
            final_url="https://jobs.example/old",
        ) is False
        with pytest.raises(RuntimeError, match="browser_network_target_required"):
            await guard(snapshot)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_older_navigation_completion_cannot_overwrite_newer_success() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 3)
    older = SimpleNamespace(
        call_id="older",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/older"},
    )
    newer = SimpleNamespace(
        call_id="newer",
        tool_name=older.tool_name,
        server_id=older.server_id,
        session_id=older.session_id,
        snapshot_id=older.snapshot_id,
        arguments={"url": "https://jobs.example/newer"},
    )
    snapshot = SimpleNamespace(
        call_id="snapshot",
        tool_name="mcp__playwright__browser_snapshot",
        server_id=older.server_id,
        session_id=older.session_id,
        snapshot_id=older.snapshot_id,
        arguments={},
    )
    try:
        await guard(older)
        guard._record_connection_target("jobs.example", 443)
        await guard(newer)
        guard._record_connection_target("jobs.example", 443)
        assert await guard.commit_navigation(
            newer,
            lease_generation=3,
            final_url="https://jobs.example/newer",
        ) is True
        assert await guard.commit_navigation(
            older,
            lease_generation=3,
            final_url="https://jobs.example/older",
        ) is False
        attestation = await guard(snapshot)
        assert attestation.targets == ("https://jobs.example/newer",)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_redirect_rejects_connection_evidence_from_before_preflight() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 4)
    guard._record_connection_target("jobs.example", 443)
    navigate = SimpleNamespace(
        call_id="navigate",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="new-session",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/role"},
    )
    try:
        await guard(navigate)
        assert await guard.commit_navigation(
            navigate,
            lease_generation=4,
            final_url="https://jobs.example/redirected",
        ) is False
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_new_session_navigation_supersedes_old_session_stage() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 5)
    old = SimpleNamespace(
        call_id="old",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="old-session",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/old"},
    )
    new = SimpleNamespace(
        call_id="new",
        tool_name=old.tool_name,
        server_id=old.server_id,
        session_id="new-session",
        snapshot_id=old.snapshot_id,
        arguments={"url": "https://jobs.example/new"},
    )
    try:
        await guard(old)
        await guard(new)
        guard._record_connection_target("jobs.example", 443)
        assert await guard.commit_navigation(
            old,
            lease_generation=5,
            final_url="https://jobs.example/old",
        ) is False
        assert await guard.commit_navigation(
            new,
            lease_generation=5,
            final_url="https://jobs.example/new",
        ) is True
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_navigation_without_final_url_fails_closed() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 2)
    navigate = SimpleNamespace(
        call_id="navigate",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/requested"},
    )
    snapshot = SimpleNamespace(
        tool_name="mcp__playwright__browser_snapshot",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={},
    )
    try:
        await guard(navigate)
        assert await guard.commit_navigation(
            navigate, lease_generation=2, final_url=None
        ) is False
        with pytest.raises(RuntimeError, match="browser_network_target_required"):
            await guard(snapshot)
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_redirect_final_url_requires_proxy_connection_history() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    guard.activate_generation("playwright", "snapshot-1", 2)
    navigate = SimpleNamespace(
        call_id="navigate",
        tool_name="mcp__playwright__browser_navigate",
        server_id="playwright",
        session_id="session-a",
        snapshot_id="snapshot-1",
        arguments={"url": "https://jobs.example/requested"},
    )
    try:
        await guard(navigate)
        assert await guard.commit_navigation(
            navigate,
            lease_generation=2,
            final_url="https://redirect.example/final",
        ) is False
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_playwright_guard_close_cancels_active_proxy_connections() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    _, port = guard.address
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"CONNECT jobs.example:443 HTTP/1.1\r\n")
    await writer.drain()
    await asyncio.sleep(0)

    await asyncio.wait_for(guard.close(), timeout=0.5)

    assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_playwright_guard_proxy_rejects_private_connect_targets() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        _, port = guard.address
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n"
            b"Host: 127.0.0.1:443\r\n\r\n"
        )
        await writer.drain()

        response = await reader.read(4096)

        assert response.startswith(b"HTTP/1.1 403 Forbidden")
        writer.close()
        await writer.wait_closed()
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_playwright_guard_proxy_rejects_mutating_http_methods(
    method: str,
) -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        _, port = guard.address
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"{method} http://jobs.example/role HTTP/1.1\r\n"
            "Host: jobs.example\r\n\r\n".encode("ascii")
        )
        await writer.drain()

        response = await reader.read(4096)

        assert response.startswith(b"HTTP/1.1 403 Forbidden")
        writer.close()
        await writer.wait_closed()
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_playwright_guard_proxy_rejects_private_http_target() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        _, port = guard.address
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET http://127.0.0.1/private HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n\r\n"
        )
        await writer.drain()

        response = await reader.read(4096)

        assert response.startswith(b"HTTP/1.1 403 Forbidden")
        writer.close()
        await writer.wait_closed()
    finally:
        await guard.close()


def test_http_proxy_request_preserves_host_and_removes_hop_by_hop_headers() -> None:
    request, hostname, port = PlaywrightNetworkGuard._prepare_http_request(
        b"GET http://jobs.example/role?q=ml HTTP/1.1\r\n"
        b"Host: jobs.example\r\n"
        b"Proxy-Authorization: Basic secret\r\n"
        b"Proxy-Connection: keep-alive\r\n"
        b"Connection: keep-alive, X-Remove\r\n"
        b"X-Remove: secret\r\n"
        b"Accept: text/html\r\n\r\n"
    )

    assert (hostname, port) == ("jobs.example", 80)
    assert request.startswith(b"GET /role?q=ml HTTP/1.1\r\n")
    assert b"Host: jobs.example\r\n" in request
    assert b"Accept: text/html\r\n" in request
    assert b"secret" not in request
    assert b"Proxy-" not in request
    assert b"Connection: close\r\n" in request


@pytest.mark.parametrize(
    "header",
    (
        b"POST http://127.0.0.1:43127/v1/capabilities/x HTTP/1.1\r\n"
        b"Host: 127.0.0.1:43127\r\nContent-Type: application/json\r\n"
        b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n",
        b"POST http://127.0.0.1:43127/v1/capabilities/x HTTP/1.1\r\n"
        b"Host: 127.0.0.1:43127\r\nContent-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n\r\n",
        b"GET http://jobs.example/ HTTP/1.1\r\n"
        b"Host: jobs.example\r\nHost: jobs.example\r\n\r\n",
        b"GET http://jobs.example/ HTTP/2\r\nHost: jobs.example\r\n\r\n",
        b"GET http://jobs.example/ HTTP/1.1\r\nHost: jobs.example\r\n"
        b"Bad Header: value\r\n\r\n",
        b"GET http://jobs.example/ HTTP/1.1\r\nHost: jobs.example\r\n"
        b"\tcontinued\r\n\r\n",
    ),
)
def test_http_proxy_rejects_request_smuggling_variants(header: bytes) -> None:
    controls = frozenset({("http", "127.0.0.1", 43127)})
    with pytest.raises(ValueError):
        PlaywrightNetworkGuard._prepare_http_request(header, controls)


def test_control_proxy_forwards_one_canonical_content_length() -> None:
    request, _hostname, _port = PlaywrightNetworkGuard._prepare_http_request(
        b"POST http://127.0.0.1:43127/v1/capabilities/x HTTP/1.1\r\n"
        b"Host: 127.0.0.1:43127\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 0002\r\n\r\n",
        frozenset({("http", "127.0.0.1", 43127)}),
    )
    assert request.count(b"Content-Length:") == 1
    assert b"Content-Length: 2\r\n" in request


def test_playwright_guard_hardens_runtime_launch_without_replacing_package() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    try:
        secured = guard.secure_config(
            McpServerConfig(
                command="npx",
                args=("@playwright/mcp@latest",),
            )
        )

        assert secured.command == "npx"
        assert secured.args[0] == "@playwright/mcp@latest"
        assert "--proxy-server" in secured.args
        assert guard.proxy_url in secured.args
        assert ("--proxy-bypass", "<-loopback>") == secured.args[-2:]
        assert "--isolated" in secured.args
        assert "--block-service-workers" in secured.args
        assert "--headless" in secured.args
        assert "--ignore-https-errors" not in secured.args
    finally:
        guard.dispose()


@pytest.mark.asyncio
async def test_playwright_guard_re_resolves_every_target_to_block_dns_rebinding() -> None:
    resolutions = iter(
        [
            [ipaddress.ip_address("93.184.216.34")],
            [ipaddress.ip_address("127.0.0.1")],
        ]
    )

    async def rebinding_resolver(_hostname: str):
        return next(resolutions)

    guard = PlaywrightNetworkGuard(resolver=rebinding_resolver)
    await guard.start()
    request = SimpleNamespace(
        arguments={"url": "https://jobs.example/role"}
    )
    try:
        await guard(request)
        with pytest.raises(ValueError, match="browser_private_target"):
            await guard(request)
    finally:
        await guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/role",
        "https://127.0.0.1/role",
        "https://[::1]/role",
        "https://169.254.169.254/latest/meta-data",
        "http://localhost/role",
        "http://169.254.169.254/latest/meta-data",
    ],
)
async def test_playwright_guard_rejects_local_and_private_targets(
    url: str,
) -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        with pytest.raises(
            ValueError,
            match="browser_(?:private_target|web_target_required)",
        ):
            await guard(SimpleNamespace(arguments={"url": url}))
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_control_origin_allows_only_exact_loopback_ip_and_port() -> None:
    guard = PlaywrightNetworkGuard(
        resolver=_public_resolver,
        control_origins=("http://127.0.0.1:43127",),
    )
    await guard.start()
    try:
        attestation = await guard(
            SimpleNamespace(
                arguments={"url": "http://127.0.0.1:43127/#/capabilities/mcp-servers"}
            )
        )
        assert attestation.targets == (
            "http://127.0.0.1:43127/#/capabilities/mcp-servers",
        )
        assert attestation.peer_verified is False

        for url in (
            "http://127.0.0.1:43128/",
            "http://localhost:43127/",
            "http://192.168.1.10:43127/",
            "https://127.0.0.1:43127/",
        ):
            with pytest.raises(
                ValueError,
                match="browser_(?:private_target|web_target_required)",
            ):
                await guard(SimpleNamespace(arguments={"url": url}))
    finally:
        await guard.close()


@pytest.mark.parametrize(
    "origin",
    (
        "http://localhost:43127",
        "http://0.0.0.0:43127",
        "http://192.168.1.10:43127",
        "https://127.0.0.1:43127",
        "http://127.0.0.1",
        "http://127.0.0.1:43127/path",
    ),
)
def test_control_origin_configuration_rejects_broad_or_ambiguous_scope(
    origin: str,
) -> None:
    with pytest.raises(ValueError, match="control_origin_invalid"):
        PlaywrightNetworkGuard(
            resolver=_public_resolver,
            control_origins=(origin,),
        )


@pytest.mark.asyncio
async def test_control_origin_proxy_allows_bounded_json_post_but_rejects_wrong_port() -> None:
    guard = PlaywrightNetworkGuard(
        resolver=_public_resolver,
        control_origins=("http://127.0.0.1:43127",),
    )
    await guard.start()
    try:
        _, proxy_port = guard.address
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", proxy_port
        )
        writer.write(
            b"POST http://127.0.0.1:43127/v1/capabilities/servers/"
            b"playwright/refresh HTTP/1.1\r\n"
            b"Host: 127.0.0.1:43127\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        await writer.drain()
        response = await reader.read(4096)
        assert response.startswith(b"HTTP/1.1 502 Bad Gateway")
        writer.close()
        await writer.wait_closed()

        for request in (
            b"POST http://127.0.0.1:43127/v1/memories HTTP/1.1\r\n"
            b"Host: 127.0.0.1:43127\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}",
            b"POST http://127.0.0.1:43127/refresh HTTP/1.1\r\n"
            b"Host: 127.0.0.1:43127\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 2\r\n\r\n{}",
            b"POST http://127.0.0.1:43127/refresh HTTP/1.1\r\n"
            b"Host: 127.0.0.1:43127\r\n"
            b"Proxy-Authorization: Basic secret\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}",
            b"GET http://127.0.0.1:43128/ HTTP/1.1\r\n"
            b"Host: 127.0.0.1:43128\r\n\r\n",
        ):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", proxy_port
            )
            writer.write(request)
            await writer.drain()
            response = await reader.read(4096)
            assert response.startswith(b"HTTP/1.1 403 Forbidden")
            writer.close()
            await writer.wait_closed()
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_proxy_rechecks_each_redirect_connection_target() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        _, port = guard.address
        for authority in ("127.0.0.1:443", "[::1]:443"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                f"CONNECT {authority} HTTP/1.1\r\n"
                f"Host: {authority}\r\n\r\n".encode("ascii")
            )
            await writer.drain()
            response = await reader.read(4096)
            assert response.startswith(b"HTTP/1.1 403 Forbidden")
            writer.close()
            await writer.wait_closed()
        assert guard.connection_targets == ()
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_proxy_rejects_unstaged_public_redirect_before_network_side_effect() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver)
    await guard.start()
    try:
        request = SimpleNamespace(call_id="navigate-1", tool_name="browser_navigate", server_id="playwright", session_id="s", snapshot_id="snap", arguments={"url":"https://jobs.example/role"})
        guard.activate_generation("playwright", "snap", 1)
        await guard(request)
        _host, port = guard.address
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT redirect.example:443 HTTP/1.1\r\nHost: redirect.example:443\r\n\r\n")
        await writer.drain(); response = await reader.read(4096)
        assert response.startswith(b"HTTP/1.1 403 Forbidden")
        assert guard.connection_targets == ()
        writer.close(); await writer.wait_closed()
    finally:
        await guard.close()


@pytest.mark.asyncio
async def test_proxy_does_not_share_authority_across_active_sessions() -> None:
    guard = PlaywrightNetworkGuard(resolver=_public_resolver); await guard.start()
    try:
        guard.activate_generation("playwright", "snap", 1)
        for session, call, url in (("a","a1","https://a.example/x"),("b","b1","https://b.example/x")):
            await guard(SimpleNamespace(call_id=call, tool_name="browser_navigate", server_id="playwright", session_id=session, snapshot_id="snap", arguments={"url":url}))
        _host, port = guard.address
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT b.example:443 HTTP/1.1\r\nHost: b.example:443\r\n\r\n"); await writer.drain()
        assert (await reader.read(4096)).startswith(b"HTTP/1.1 403 Forbidden")
        assert guard.connection_targets == ()
        writer.close(); await writer.wait_closed()
    finally:
        await guard.close()
