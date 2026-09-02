from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from starter_agent.application import ApplicationService


@pytest.mark.asyncio
async def test_application_starts_and_cooperatively_stops_configured_worker_pool() -> None:
    application = object.__new__(ApplicationService)

    class Pool:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.stopped = False

        async def serve(self, worker_id: str) -> None:
            assert worker_id == "delegation-worker:0"
            self.started.set()
            while not self.stopped:
                await asyncio.sleep(0)

        def stop(self) -> None:
            self.stopped = True

    worker = type("Components", (), {"pool": Pool()})()
    application.configure_delegation_worker(worker)
    await application.start_delegation_workers()
    await asyncio.wait_for(worker.pool.started.wait(), timeout=1)
    await application.stop_delegation_workers()
    assert worker.pool.stopped is True


def test_background_tick_resumes_and_merges_children_terminal_parent() -> None:
    application = object.__new__(ApplicationService)
    parent = SimpleNamespace(
        id="parent:ready", status="queued", phase="children_terminal", version=7
    )

    class Store:
        def __init__(self) -> None:
            self.resume_calls: list[dict[str, object]] = []

        def list_parent_run_ids(self, *, limit: int) -> tuple[str, ...]:
            assert limit == 500
            return ("parent:terminal", parent.id)

        def get_parent(self, parent_run_id: str):
            if parent_run_id == "parent:terminal":
                return SimpleNamespace(
                    id=parent_run_id, status="succeeded", phase="terminal", version=3
                )
            return parent

        def resume_parent_for_validation(self, parent_run_id: str, **kwargs):
            self.resume_calls.append({"parent_run_id": parent_run_id, **kwargs})
            return SimpleNamespace(id=parent_run_id, version=8)

    class AcceptanceService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def merge_ready_parent(self, parent_run_id: str, **kwargs):
            self.calls.append({"parent_run_id": parent_run_id, **kwargs})
            return SimpleNamespace(status="merged")

    store = Store()
    acceptance = AcceptanceService()
    application.delegation_store = store
    application.delegation_worker = SimpleNamespace(
        executor=SimpleNamespace(acceptance_service=acceptance)
    )

    assert application._merge_ready_delegation_parents() == 1
    assert store.resume_calls[0]["expected_version"] == 7
    assert isinstance(store.resume_calls[0]["occurred_at"], datetime)
    assert store.resume_calls[0]["occurred_at"].tzinfo is UTC
    assert acceptance.calls[0]["expected_version"] == 8


@pytest.mark.asyncio
async def test_job_research_reconnects_browser_before_creating_child() -> None:
    application = object.__new__(ApplicationService)
    refreshed: list[object] = []
    application.runtime = SimpleNamespace(
        tools=SimpleNamespace(
            refresh_from_manager=lambda manager: refreshed.append(manager)
        )
    )

    class Manager:
        def __init__(self) -> None:
            self.connected = False
            self.discovered: list[str] = []

        def statuses(self):
            return {
                "playwright": SimpleNamespace(
                    enabled=True,
                    connection_state="closed" if not self.connected else "ready",
                    error_code=None,
                )
            }

        async def connect(self, server_id: str):
            assert server_id == "playwright"
            self.connected = True
            return self.statuses()[server_id]

        async def discover(self, server_id: str):
            self.discovered.append(server_id)

    manager = Manager()
    application.mcp_manager = manager

    await application._ensure_job_research_browser_ready()

    assert manager.connected is True
    assert manager.discovered == ["playwright"]
    assert refreshed == [manager]


async def test_job_research_reuses_ready_active_browser_snapshot() -> None:
    application = object.__new__(ApplicationService)
    refreshed: list[object] = []
    application.runtime = SimpleNamespace(
        tools=SimpleNamespace(
            refresh_from_manager=lambda manager: refreshed.append(manager)
        )
    )

    class Manager:
        def __init__(self) -> None:
            self.discovered: list[str] = []

        def statuses(self):
            return {
                "playwright": SimpleNamespace(
                    enabled=True,
                    connection_state="ready",
                    error_code=None,
                )
            }

        def get_snapshot_summary(self, server_id: str):
            assert server_id == "playwright"
            return SimpleNamespace(stale=False)

        async def discover(self, server_id: str):
            self.discovered.append(server_id)

    manager = Manager()
    application.mcp_manager = manager

    await application._ensure_job_research_browser_ready()

    assert manager.discovered == []
    assert refreshed == [manager]


async def test_job_research_reconnects_stale_ready_browser_status() -> None:
    application = object.__new__(ApplicationService)
    refreshed: list[object] = []
    application.runtime = SimpleNamespace(
        tools=SimpleNamespace(
            refresh_from_manager=lambda manager: refreshed.append(manager)
        )
    )

    class Manager:
        def __init__(self) -> None:
            self.live = False
            self.connected = 0
            self.discovered: list[str] = []

        def statuses(self):
            return {
                "playwright": SimpleNamespace(
                    enabled=True,
                    connection_state="ready",
                    error_code=None,
                )
            }

        def session_ready(self, server_id: str) -> bool:
            assert server_id == "playwright"
            return self.live

        def get_snapshot_summary(self, server_id: str):
            assert server_id == "playwright"
            return SimpleNamespace(stale=False)

        async def connect(self, server_id: str):
            assert server_id == "playwright"
            self.connected += 1
            self.live = True
            return self.statuses()[server_id]

        async def discover(self, server_id: str):
            self.discovered.append(server_id)

    manager = Manager()
    application.mcp_manager = manager

    await application._ensure_job_research_browser_ready()

    assert manager.connected == 1
    assert manager.discovered == []
    assert refreshed == [manager]
