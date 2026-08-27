from datetime import UTC, datetime

import pytest

from starter_agent.orchestration.task_manager import (
    CapacityGovernor,
    CapacitySnapshot,
    ConcurrencyLimits,
    TaskEventConflict,
    TaskEventReducer,
    TaskManagerState,
    make_task_event,
)


NOW = datetime(2026, 8, 15, tzinfo=UTC)


def event(seq: int, kind: str, *, event_id: str | None = None, status="running"):
    return make_task_event(
        task_event_id=event_id or f"event:{seq}",
        event_seq=seq,
        event_type=kind,
        task_id="task:1",
        parent_run_id="parent:1",
        child_run_id="child:1",
        plan_id="plan:1",
        step_id="step:1",
        attempt=1,
        status=status,
        occurred_at=NOW,
        payload_summary={},
        artifact_refs=("artifact:envelope:1",) if kind == "child_completed" else (),
    )


def initial() -> TaskManagerState:
    return TaskManagerState(parent_run_id="parent:1")


def test_six_child_events_drive_structured_snapshot_without_polling() -> None:
    reducer = TaskEventReducer()
    cases = (
        ("child_started", "running"),
        ("child_progress", "waiting"),
        ("child_completed", "succeeded"),
        ("child_failed", "failed"),
        ("child_cancelled", "cancelled"),
        ("child_timed_out", "timed_out"),
    )
    for kind, expected in cases:
        state = initial()
        result = reducer.apply(state, event(1, kind, status=expected))
        assert result.state.child_runs[0].status == expected
    assert not hasattr(reducer, "provider")


def test_duplicate_and_conflicting_event_ids_are_idempotent() -> None:
    reducer = TaskEventReducer()
    applied = reducer.apply(initial(), event(1, "child_started"))
    duplicate = reducer.apply(applied.state, event(1, "child_started"))
    assert duplicate.disposition == "duplicate"
    changed = event(2, "child_failed", event_id="event:1", status="failed")
    with pytest.raises(TaskEventConflict, match="idempotency_conflict"):
        reducer.apply(applied.state, changed)


def test_out_of_order_events_buffer_then_drain_in_sequence() -> None:
    reducer = TaskEventReducer()
    buffered = reducer.apply(initial(), event(2, "child_progress"))
    assert buffered.disposition == "buffered"
    assert buffered.state.child_runs == ()
    drained = reducer.apply(buffered.state, event(1, "child_started"))
    assert drained.applied_event_ids == ("event:1", "event:2")
    assert drained.state.last_event_seq == 2
    assert drained.state.child_runs[0].phase == "child_progress"


def test_late_and_duplicate_completion_cannot_reverse_terminal_state() -> None:
    reducer = TaskEventReducer()
    completed = reducer.apply(initial(), event(1, "child_completed", status="succeeded"))
    late = reducer.apply(completed.state, event(2, "child_progress", status="running"))
    assert late.state.child_runs[0].status == "succeeded"
    assert late.state.child_runs[0].result_envelope_ref == "artifact:envelope:1"


def test_completed_event_preserves_explicit_partial_status() -> None:
    result = TaskEventReducer().apply(
        initial(), event(1, "child_completed", status="partial")
    )
    assert result.state.child_runs[0].status == "partial"


def test_capacity_governor_applies_all_concurrency_dimensions() -> None:
    governor = CapacityGovernor(
        ConcurrencyLimits(
            global_limit=4,
            parent_limit=2,
            specialist_limits={"researcher": 2},
            provider_limits={"provider": 1},
            tool_limits={"browser": 1},
        )
    )
    baseline = CapacitySnapshot()
    assert governor.admit(
        baseline,
        parent_run_id="parent:1",
        specialist_id="researcher",
        provider="provider",
        tools=("browser",),
    ).admitted
    cases = (
        (CapacitySnapshot(global_active=4), "global_backpressure"),
        (CapacitySnapshot(parent_active={"parent:1": 2}), "parent_backpressure"),
        (CapacitySnapshot(specialist_active={"researcher": 2}), "specialist_backpressure"),
        (CapacitySnapshot(provider_active={"provider": 1}), "provider_backpressure"),
        (CapacitySnapshot(tool_active={"browser": 1}), "tool_backpressure"),
    )
    for snapshot, reason in cases:
        decision = governor.admit(
            snapshot,
            parent_run_id="parent:1",
            specialist_id="researcher",
            provider="provider",
            tools=("browser",),
        )
        assert not decision.admitted
        assert decision.reason_code == reason
