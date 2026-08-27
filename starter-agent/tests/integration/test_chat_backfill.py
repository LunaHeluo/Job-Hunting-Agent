from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from starter_agent.delegation.backfill import ChatBackfillService
from starter_agent.delegation.models import BudgetLimits, ParentRun
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.delegation.store import OutboxMessage
from starter_agent.infrastructure.session_store import SQLiteSessionStore


def _limits(value: int = 0) -> BudgetLimits:
    return BudgetLimits(
        tokens=value, cost_microunits=value, wall_clock_ms=value,
        model_calls=value, tool_calls=value,
    )


def _completed_parent(session_id, now: datetime) -> ParentRun:
    return ParentRun(
        id="parent:backfill:001", session_id=str(session_id), origin_turn_id="turn:backfill:001",
        principal="user:alice", coordinator_spec_version="v1", runtime_revision="v1",
        status="succeeded", phase="merged", available_at=now,
        deadline_at=now + timedelta(minutes=5), budget_total=_limits(10),
        budget_reserved=_limits(), budget_consumed=_limits(), result_version=1,
        merge_report_id="merge:backfill:001", route="delegated_job_research",
        created_at=now, started_at=now, completed_at=now, updated_at=now,
    )


def test_chat_backfill_publishes_one_assistant_message_only_for_successful_merge(tmp_path) -> None:
    now = datetime.now(UTC)
    session_id = uuid4()
    run_store = SQLiteRunStore("sqlite:///backfill-runs.db", tmp_path)
    session_store = SQLiteSessionStore("sqlite:///backfill-chat.db", tmp_path)
    session_store.ensure_session(session_id)
    parent = _completed_parent(session_id, now)
    run_store.create_parent(parent)
    service = ChatBackfillService(run_store=run_store, session_store=session_store)

    first = service.publish_once(parent.id, result_version=1, message_kind="delegation.final")
    second = service.publish_once(parent.id, result_version=1, message_kind="delegation.final")

    assert first == second
    messages = session_store.list_messages(session_id)
    assert len(messages) == 1
    assert messages[0].role == "assistant"
    assert run_store.get_parent(parent.id).backfill_status == "completed"

    failed = parent.model_copy(update={"id": "parent:backfill:failed", "status": "failed", "phase": "failed", "merge_report_id": None})
    run_store.create_parent(failed)
    assert service.publish_once(failed.id, result_version=1, message_kind="delegation.final") is None
    assert len(session_store.list_messages(session_id)) == 1


def test_chat_backfill_consumer_delivers_merge_outbox_once(tmp_path) -> None:
    now = datetime.now(UTC)
    session_id = uuid4()
    runs = SQLiteRunStore("sqlite:///backfill-outbox.db", tmp_path)
    chat = SQLiteSessionStore("sqlite:///backfill-outbox-chat.db", tmp_path)
    chat.ensure_session(session_id)
    parent = _completed_parent(session_id, now)
    runs.create_parent(parent)
    message = OutboxMessage(
        id="outbox:backfill:001", topic="chat.backfill_requested", aggregate_id=parent.id,
        idempotency_key="backfill:parent:backfill:001:1", payload={"result_version": 1, "message_kind": "delegation.final"}, created_at=now,
    )
    runs.enqueue_outbox(message)
    service = ChatBackfillService(run_store=runs, session_store=chat)

    assert service.consume_pending() == 1
    assert service.consume_pending() == 0
    assert len(chat.list_messages(session_id)) == 1


def test_chat_backfill_leaves_failed_parent_outbox_pending(tmp_path) -> None:
    now = datetime.now(UTC)
    session_id = uuid4()
    runs = SQLiteRunStore("sqlite:///backfill-failed.db", tmp_path)
    chat = SQLiteSessionStore("sqlite:///backfill-failed-chat.db", tmp_path)
    chat.ensure_session(session_id)
    failed = _completed_parent(session_id, now).model_copy(update={
        "id": "parent:backfill:failed-outbox", "status": "failed", "phase": "failed",
        "merge_report_id": None,
    })
    runs.create_parent(failed)
    message = OutboxMessage(
        id="outbox:backfill:failed", topic="chat.backfill_requested", aggregate_id=failed.id,
        idempotency_key="backfill:failed:1", payload={"result_version": 1, "message_kind": "delegation.final"}, created_at=now,
    )
    runs.enqueue_outbox(message)

    assert ChatBackfillService(run_store=runs, session_store=chat).consume_pending() == 0
    assert runs.list_outbox(limit=10).items[0].status == "pending"
    assert chat.list_messages(session_id) == []
