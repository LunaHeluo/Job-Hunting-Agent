from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from starter_agent.delegation.models import BudgetLimits, ParentRun
from starter_agent.delegation.store import RunEvent, SQLiteRunStore
from starter_agent.interfaces.capabilities_api import (
    ManagementPrincipal,
    get_management_principal,
)
from starter_agent.interfaces.runs_api import create_runs_router


def _limits() -> BudgetLimits:
    return BudgetLimits(
        tokens=10, cost_microunits=10, wall_clock_ms=10,
        model_calls=1, tool_calls=1,
    )


def _parent(now: datetime) -> ParentRun:
    zero = BudgetLimits(
        tokens=0, cost_microunits=0, wall_clock_ms=0,
        model_calls=0, tool_calls=0,
    )
    return ParentRun(
        id="parent:api:001", session_id="session:api:001", origin_turn_id="turn:api:001",
        principal="user:alice", coordinator_spec_version="v1", runtime_revision="v1",
        status="running", phase="researching", available_at=now,
        deadline_at=now + timedelta(minutes=5), budget_total=_limits(),
        budget_reserved=zero, budget_consumed=zero, route="delegated_job_research",
        created_at=now, started_at=now, updated_at=now,
    )


def test_runs_api_details_events_and_cancel_are_principal_scoped_and_cas(tmp_path) -> None:
    """The durable run API owns state; an SSE/client retry cannot change it."""
    now = datetime.now(UTC)
    store = SQLiteRunStore("sqlite:///runs-api.db", tmp_path)
    parent = _parent(now)
    store.create_parent(parent)
    store.append_event(RunEvent(
        id="event:api:001", parent_run_id=parent.id, event_type="child.delegated",
        status="completed", occurred_at=now, payload={
            "task_id": "task:api:001", "raw_html": "<secret>",
            "child_messages": "secret", "cookie": "secret",
        },
    ))
    api = FastAPI()
    api.include_router(create_runs_router(lambda: SimpleNamespace(delegation_store=store)))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(
        subject="alice", role="operator"
    )

    with TestClient(api) as client:
        details = client.get(f"/v1/runs/{parent.id}")
        assert details.status_code == 200
        assert details.json()["parent"]["id"] == parent.id
        assert "restricted_artifacts" not in details.json()

        events = client.get(f"/v1/runs/{parent.id}/events?after_seq=0&limit=1")
        assert events.status_code == 200
        assert events.json()["events"][0]["event_seq"] == 1
        assert events.json()["events"][0]["payload"] == {"task_id": "task:api:001"}

        streamed = client.get(f"/v1/runs/{parent.id}/events/stream?after_seq=0")
        assert streamed.status_code == 200
        assert '"event_seq": 1' in streamed.text
        assert "<secret>" not in streamed.text

        cancelled = client.post(
            f"/v1/runs/{parent.id}/cancel",
            json={"expected_version": 0, "idempotency_key": "cancel:api:001", "reason": "user"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelling"

        stale = client.post(
            f"/v1/runs/{parent.id}/cancel",
            json={"expected_version": 0, "idempotency_key": "cancel:api:002", "reason": "user"},
        )
        assert stale.status_code == 409


def test_runs_api_resume_is_idempotent_and_rejects_stale_or_changed_key(tmp_path) -> None:
    now = datetime.now(UTC)
    store = SQLiteRunStore("sqlite:///runs-resume.db", tmp_path)
    parent = _parent(now).model_copy(update={"status": "waiting_for_user", "phase": "captcha"})
    store.create_parent(parent)
    api = FastAPI()
    api.include_router(create_runs_router(lambda: SimpleNamespace(delegation_store=store)))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(subject="alice", role="operator")

    with TestClient(api) as client:
        body = {"expected_version": 0, "idempotency_key": "resume:api:001"}
        first = client.post(f"/v1/runs/{parent.id}/resume", json=body)
        second = client.post(f"/v1/runs/{parent.id}/resume", json=body)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert client.post(f"/v1/runs/{parent.id}/resume", json={"expected_version": 0, "idempotency_key": "resume:api:002"}).status_code == 409
