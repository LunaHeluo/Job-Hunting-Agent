from datetime import UTC, datetime

from starter_agent.capabilities.confirmations import ConfirmationService
from starter_agent.orchestration.pending_action import PendingActionService
from tests.unit.test_tool_confirmations import _confirmation_gate, _request


NOW = datetime(2026, 8, 14, tzinfo=UTC)


async def test_pending_action_reuses_existing_confirmation_and_resumes_with_permit(tmp_path) -> None:
    store, gate = _confirmation_gate(tmp_path)
    confirmations = ConfirmationService(store, gate, confirmation_ttl_seconds=60)
    adapter = PendingActionService(confirmations, gate)
    request = _request()
    gate_decision = await gate.evaluate(request)

    pending = adapter.prepare(
        parent_run_id="parent:1",
        step_id="step:send",
        action_type="browser_navigate",
        request=request,
        gate_decision=gate_decision,
        impact_summary=("external navigation",),
        created_at=NOW,
    )
    assert pending.confirmation_id is not None
    assert pending.arguments_hash == request.confirmation_arguments_hash
    assert pending.status == "pending"

    approved = confirmations.decide(
        pending.confirmation_id,
        expected_revision=0,
        idempotency_key="approve:1",
        decision="once",
    )
    result = await adapter.resume(pending, request=request, budget_available=True)
    assert approved.status == "approved"
    assert result.outcome == "resume"
    assert result.permit_id is not None
    assert result.pending_action.status == "approved"


async def test_changed_arguments_invalidate_old_approval(tmp_path) -> None:
    store, gate = _confirmation_gate(tmp_path)
    confirmations = ConfirmationService(store, gate, confirmation_ttl_seconds=60)
    adapter = PendingActionService(confirmations, gate)
    request = _request()
    pending = adapter.prepare(
        parent_run_id="parent:1",
        action_type="browser_navigate",
        request=request,
        gate_decision=await gate.evaluate(request),
        created_at=NOW,
    )
    confirmations.decide(
        pending.confirmation_id,
        expected_revision=0,
        idempotency_key="approve:changed",
        decision="once",
    )

    changed = request.model_copy(
        update={"arguments": {"url": "https://jobs.example.com/other"}}
    )
    result = await adapter.resume(pending, request=changed, budget_available=True)
    assert result.outcome == "stop"
    assert result.reason_code == "pending_action_arguments_changed"
    assert result.pending_action.status == "invalidated"


async def test_approved_action_rechecks_budget_before_resume(tmp_path) -> None:
    store, gate = _confirmation_gate(tmp_path)
    confirmations = ConfirmationService(store, gate, confirmation_ttl_seconds=60)
    adapter = PendingActionService(confirmations, gate)
    request = _request()
    pending = adapter.prepare(
        parent_run_id="parent:1",
        action_type="browser_navigate",
        request=request,
        gate_decision=await gate.evaluate(request),
        created_at=NOW,
    )
    confirmations.decide(
        pending.confirmation_id,
        expected_revision=0,
        idempotency_key="approve:no-budget",
        decision="once",
    )

    result = await adapter.resume(pending, request=request, budget_available=False)
    assert result.outcome == "stop"
    assert result.reason_code == "budget_unavailable"
    assert result.permit_id is None


async def test_cancelled_confirmation_maps_to_rejected_pending_action(tmp_path) -> None:
    store, gate = _confirmation_gate(tmp_path)
    confirmations = ConfirmationService(store, gate, confirmation_ttl_seconds=60)
    adapter = PendingActionService(confirmations, gate)
    request = _request()
    pending = adapter.prepare(
        parent_run_id="parent:1",
        action_type="browser_navigate",
        request=request,
        gate_decision=await gate.evaluate(request),
        created_at=NOW,
    )
    confirmations.decide(
        pending.confirmation_id,
        expected_revision=0,
        idempotency_key="reject:1",
        decision="cancel",
    )
    result = await adapter.resume(pending, request=request, budget_available=True)
    assert result.outcome == "stop"
    assert result.reason_code == "approval_rejected"
    assert result.pending_action.status == "rejected"

