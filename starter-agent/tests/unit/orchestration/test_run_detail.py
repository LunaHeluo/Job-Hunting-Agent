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
from starter_agent.orchestration.models import ExecutionState, RouteDecision, RouteFallback


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def zero():
    return BudgetLimits(
        steps=0,
        tokens=0,
        cost_microunits=0,
        wall_clock_ms=0,
        model_calls=0,
        tool_calls=0,
    )


def test_run_detail_exposes_real_direct_route_without_fake_plan_or_raw_state() -> None:
    route = RouteDecision(
        route_decision_id="route:1",
        run_id="parent:1",
        session_id="session:1",
        turn_id="turn:1",
        route="direct",
        confidence=1,
        reason_code="simple_explanation",
        reason_summary="No tool is required",
        risk_level="low",
        fallback=RouteFallback(route="human_review", condition_code="input_missing"),
        capability_snapshot_revision="cap:v1",
        policy_revision="policy:v1",
        created_at=NOW,
    )
    state = ExecutionState(
        run_id="parent:1",
        parent_run_id="parent:1",
        session_id="session:1",
        turn_id="turn:1",
        goal="explain",
        execution_status="completed",
        current_node="end",
        route=route,
        outputs={"raw_resume": "must not reach the API"},
        stop_reason="direct_completed",
        updated_at=NOW,
    )
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    parent = ParentRun(
        id="parent:1",
        run_type="job_application_orchestration",
        session_id="session:1",
        origin_turn_id="turn:1",
        principal="user:alice",
        coordinator_spec_version="v1",
        runtime_revision="v1",
        status="succeeded",
        phase="terminal",
        available_at=NOW,
        deadline_at=NOW + timedelta(minutes=1),
        budget_total=zero(),
        budget_reserved=zero(),
        budget_consumed=zero(),
        route="direct",
        orchestration_state_version=1,
        orchestration_state=state.model_dump(mode="json"),
        stop_reason_code="direct_completed",
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
        updated_at=NOW,
    )
    store.create_parent(parent)
    store.append_event(
        RunEvent(
            id="event:route:1",
            parent_run_id="parent:1",
            event_type="orchestration.route",
            status="completed",
            occurred_at=NOW,
            payload={"route_decision_id": "route:1", "decision": "direct"},
        )
    )
    api = FastAPI()
    api.include_router(create_runs_router(lambda: SimpleNamespace(delegation_store=store)))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(
        subject="alice", role="operator"
    )
    with TestClient(api) as client:
        response = client.get("/v1/runs/parent:1")
        projection = client.get("/v1/runs/parent:1/orchestration")
    assert response.status_code == projection.status_code == 200
    payload = response.json()
    assert "orchestration_state" not in payload["parent"]
    assert payload["orchestration"]["route"]["route"] == "direct"
    assert payload["orchestration"]["plan"] is None
    assert payload["orchestration"]["child_runs"] == []
    assert "must not reach" not in response.text
    assert projection.json() == payload["orchestration"]


def test_legacy_run_has_no_static_orchestration_placeholder() -> None:
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    store.create_parent(
        ParentRun(
            id="parent:legacy",
            session_id="session:1",
            origin_turn_id="turn:1",
            principal="user:alice",
            coordinator_spec_version="v1",
            runtime_revision="v1",
            available_at=NOW,
            deadline_at=NOW + timedelta(minutes=1),
            budget_total=zero(),
            budget_reserved=zero(),
            budget_consumed=zero(),
            route="legacy",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    api = FastAPI()
    api.include_router(create_runs_router(lambda: SimpleNamespace(delegation_store=store)))
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(
        subject="alice", role="operator"
    )
    with TestClient(api) as client:
        details = client.get("/v1/runs/parent:legacy")
        orchestration = client.get("/v1/runs/parent:legacy/orchestration")
    assert details.json()["orchestration"] is None
    assert orchestration.status_code == 404
