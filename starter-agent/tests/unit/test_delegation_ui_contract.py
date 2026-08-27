from pathlib import Path


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))


def test_chat_and_trust_render_durable_delegation_run_state_without_raw_artifacts() -> None:
    """Task18 uses the persisted Run API; SSE is only an event-sequence accelerator."""
    for contract in (
        'id="delegationTaskCards"',
        'id="delegationRunDetail"',
        "renderDelegationTaskCard",
        "loadDelegationRun",
        "loadDelegationRunForSession",
        "if (!delegationRunIsTerminal(run.parent?.status))",
        "startDelegationEventStream",
        "applyDelegationRunEvent",
        "/v1/runs/${encodeURIComponent(parentRunId)}",
        "/events?after_seq=",
        "/events/stream?after_seq=",
        "cancelDelegationRun",
        "resumeDelegationRun",
        "expected_version",
        "idempotency_key",
        "children_completed",
        "budget_consumed",
        "merge_reports",
        "source_validation",
        "evidence_validation",
        "message.metadata?.parent_run_id",
        "delegationReconnectTimers",
        "state.delegationStreams.delete(parentRunId)",
        "/artifacts",
        "renderOrchestrationDebug",
        "orchestration-debug-section",
        "Plan 依赖 DAG",
        "Join Decision",
        "Runtime Verify",
        "预算进度",
        "Model Router",
        "stop_reason",
    ):
        assert contract in HTML

    for forbidden in (
        "raw_html",
        "hidden_reasoning",
        "child_messages",
        "tool_result_raw",
    ):
        assert forbidden not in HTML
