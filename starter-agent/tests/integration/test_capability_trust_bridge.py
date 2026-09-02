from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from starter_agent.agent.runtime import _ACTIVE_RUN_CONTEXT
from starter_agent.capabilities.models import AuditEvent
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits
from starter_agent.domain.models import Message
from starter_agent.trust.store import TrustStore
from starter_agent.trust.trace import CapabilityAuditTrustBridge


def test_capability_audit_is_automatically_bridged_to_trust_for_active_child(tmp_path) -> None:
    capability = CapabilityStore("sqlite:///capabilities.db", tmp_path)
    trust = TrustStore("sqlite:///trust.db", tmp_path)
    capability.add_audit_sink(CapabilityAuditTrustBridge(trust).record)
    context = RunContext(run_id="child:1", parent_run_id="parent:1", child_task_id="task:1", session_id=UUID("00000000-0000-0000-0000-000000000001"), turn_id=UUID("00000000-0000-0000-0000-000000000002"), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=[], budget_limits=BudgetLimits(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=1), trace_context=RunTraceContext(parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1", policy_decision_id="policy:1", approval_id="approval:1"))
    token = _ACTIVE_RUN_CONTEXT.set(context)
    try:
        capability.append_audit_event(AuditEvent(event_id="audit:1", actor="runtime", action="tool.completed", target="tool:browser_snapshot", decision="allow", reason_code="ok", session_id="session:trusted", turn_id="turn:trusted", call_id="call:trusted", created_at=datetime.now(UTC), payload={"session_id": "session:forged", "turn_id": "turn:forged", "call_id": "call:forged", "policy_decision_id": "policy:payload", "approval_id": "approval:payload", "raw_html": "<html>private</html>"}))
    finally:
        _ACTIVE_RUN_CONTEXT.reset(token)
    [trace] = trust.list_trace_events(parent_run_id="parent:1", child_task_id="task:1")
    assert (trace.session_id, trace.turn_id, trace.tool_call_id) == ("session:trusted", "turn:trusted", "call:trusted")
    assert (trace.parent_run_id, trace.child_task_id, trace.child_run_id, trace.policy_decision_id, trace.approval_id, trace.principal, trace.access_level) == ("parent:1", "task:1", "child:1", "policy:payload", "approval:payload", "user:1", "child_restricted")
    assert "raw_html" not in trace.summary
