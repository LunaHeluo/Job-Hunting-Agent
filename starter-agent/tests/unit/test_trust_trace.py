from datetime import UTC, datetime

from starter_agent.capabilities.models import AuditEvent, canonical_json_sha256
from starter_agent.trust.store import TrustStore
from starter_agent.trust.trace import TraceContext, TrustTraceRecorder


def test_trace_recorder_persists_full_eval_correlation_chain() -> None:
    store = TrustStore("sqlite:///:memory:", ".")
    context = TraceContext(
        eval_run_id="run-1",
        case_id="case-1",
        session_id="session-1",
        turn_id="turn-1",
        model_request_id="model-request-1",
        tool_call_id="tool-call-1",
        policy_decision_id="policy-decision-1",
        approval_id="approval-1",
        child_run_id="child-run-1",
    )

    event = TrustTraceRecorder(store).record(
        id="trace-1",
        context=context,
        event_type="Tool",
        status="completed",
        summary={"tool_name": "search_jobs_serpapi"},
        payload={"tool_name": "search_jobs_serpapi", "arguments": {"limit": 3}},
    )

    assert event.eval_run_id == "run-1"
    assert event.case_id == "case-1"
    assert event.session_id == "session-1"
    assert event.turn_id == "turn-1"
    assert event.model_request_id == "model-request-1"
    assert event.tool_call_id == "tool-call-1"
    assert event.policy_decision_id == "policy-decision-1"
    assert event.approval_id == "approval-1"
    assert event.child_run_id == "child-run-1"
    assert event.payload_hash == canonical_json_sha256(
        {"tool_name": "search_jobs_serpapi", "arguments": {"limit": 3}}
    )
    assert store.list_trace_events(eval_run_id="run-1") == [event]


def test_trace_chain_keeps_parent_task_and_access_metadata_out_of_public_body() -> None:
    store = TrustStore("sqlite:///:memory:", ".")
    event = TrustTraceRecorder(store).record(
        id="trace-web-1",
        context=TraceContext(parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1", tool_call_id="tool:1", policy_decision_id="policy:1", approval_id="approval:1", principal="user:1", access_level="child_restricted"),
        event_type="Tool", status="completed",
        summary={"tool_name": "browser_snapshot", "artifact_ref": "tool:web:1"},
        payload={"raw_html": "<html>private</html>", "artifact_ref": "tool:web:1"},
    )
    assert event.parent_run_id == "parent:1"
    assert store.list_trace_events(parent_run_id="parent:1", child_task_id="task:1") == [event]
    assert "raw_html" not in event.summary


def test_trace_recorder_explains_missing_nodes_when_bridging_audit_event() -> None:
    store = TrustStore("sqlite:///:memory:", ".")
    audit_event = AuditEvent(
        event_id="audit-1",
        actor="agent",
        action="gate.evaluated",
        target="tool:search_jobs_serpapi",
        decision="allow",
        reason_code="allowlist_auto",
        created_at=datetime.now(UTC),
        payload={
            "session_id": "session-1",
            "turn_id": "turn-1",
            "tool_name": "search_jobs_serpapi",
        },
    )

    event = TrustTraceRecorder(store).from_audit_event(
        audit_event,
        context=TraceContext(eval_run_id="run-1", case_id="case-1"),
    )

    assert event.id == "audit-1"
    assert event.event_type == "Policy"
    assert event.policy_decision_id is None
    assert "missing_nodes" in event.summary
    assert event.summary["missing_nodes"]["policy_decision_id"] == (
        "not present in source audit event"
    )
    assert event.summary["tool_name"] == "search_jobs_serpapi"


def test_active_child_runtime_audit_context_reaches_trust_recorder() -> None:
    from starter_agent.agent.runtime import _ACTIVE_RUN_CONTEXT
    from starter_agent.delegation.context import RunContext, RunTraceContext
    from starter_agent.delegation.models import BudgetLimits
    from starter_agent.domain.models import Message
    from uuid import UUID
    context = RunContext(run_id="child:1", parent_run_id="parent:1", child_task_id="task:1", session_id=UUID("00000000-0000-0000-0000-000000000001"), turn_id=UUID("00000000-0000-0000-0000-000000000002"), principal="user:1", messages=[Message(role="user", content="x")], effective_tool_view=[], budget_limits=BudgetLimits(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=1), trace_context=RunTraceContext(parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1", policy_decision_id="policy:1", approval_id="approval:1"))
    token = _ACTIVE_RUN_CONTEXT.set(context)
    try:
        audit = AuditEvent(event_id="audit-child-1", actor="agent", action="tool.completed", target="tool:browser_snapshot", decision="allow", reason_code="ok", created_at=datetime.now(UTC), payload={"tool_name": "browser_snapshot", "raw_html": "<html>private</html>"})
        event = TrustTraceRecorder(TrustStore("sqlite:///:memory:", ".")).from_audit_event(audit, context=TraceContext())
    finally:
        _ACTIVE_RUN_CONTEXT.reset(token)
    assert (event.parent_run_id, event.child_task_id, event.child_run_id, event.principal, event.access_level) == ("parent:1", "task:1", "child:1", "user:1", "child_restricted")
    assert "raw_html" not in event.summary
