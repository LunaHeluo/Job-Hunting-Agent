from __future__ import annotations

import pytest

from tests.unit.test_profile_evidence_analyst import _context, _inputs, _runtime, _spec


@pytest.mark.asyncio
async def test_profile_rejects_unauthorized_scope_before_provider_or_rag_side_effect() -> None:
    from starter_agent.delegation.specialists.profile_evidence_analyst import ProfileEvidenceAnalyst

    final = {"matches": [], "missing": [], "conflicts": []}
    runtime, provider, tool = _runtime(final)
    context = _context()
    context.knowledge_scope = "job"

    result = await ProfileEvidenceAnalyst(runtime).run(
        _spec(), context, _inputs(knowledge_scope={"type": "job"})
    )

    assert result.outcome.status == "failed"
    assert result.outcome.error_code == "profile_knowledge_scope_forbidden"
    assert provider.requests == []
    assert tool.calls == 0


@pytest.mark.asyncio
async def test_profile_scope_deny_audit_bridges_full_child_identity_to_trust() -> None:
    from starter_agent.trust.store import TrustStore
    from starter_agent.trust.trace import CapabilityAuditTrustBridge
    from starter_agent.delegation.specialists.profile_evidence_analyst import ProfileEvidenceAnalyst

    runtime, provider, tool = _runtime({"matches": [], "missing": [], "conflicts": []})
    trust = TrustStore("sqlite:///:memory:", __import__("pathlib").Path("."))
    runtime.gate.store.add_audit_sink(CapabilityAuditTrustBridge(trust).record)
    context = _context()
    context.knowledge_scope = "job"

    result = await ProfileEvidenceAnalyst(runtime).run(
        _spec(), context, _inputs(knowledge_scope={"type": "job"})
    )

    assert result.outcome.error_code == "profile_knowledge_scope_forbidden"
    [trace] = trust.list_trace_events(parent_run_id="parent:1", child_task_id="task:profile:1")
    assert (trace.parent_run_id, trace.child_task_id, trace.child_run_id, trace.principal, trace.access_level) == (
        "parent:1", "task:profile:1", "child:profile:1", "user:1", "child_restricted"
    )
    assert trace.policy_decision_id == "policy:profile-scope:child:profile:1"
    assert trace.approval_id is None
    assert provider.requests == []
    assert tool.calls == 0
