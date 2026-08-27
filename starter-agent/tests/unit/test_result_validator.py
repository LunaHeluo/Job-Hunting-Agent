from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from starter_agent.delegation.models import BudgetLimits, BudgetUsage, ChildRun, ChildTask, ParentRun, ResultEnvelope
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.results import ResultValidator, ValidationContext


NOW = datetime(2026, 8, 13, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def _limits(value: int = 10) -> BudgetLimits:
    return BudgetLimits(tokens=value, cost_microunits=value, wall_clock_ms=value, model_calls=value, tool_calls=value)


def _context(*, child_status: str = "succeeded", parent_status: str = "running") -> ValidationContext:
    snapshot_id = SpecialistRegistry(ROOT / "config" / "specialists", project_root=ROOT).reload().snapshot_hash
    parent = ParentRun(id="parent:1", session_id="session:1", origin_turn_id="turn:1", principal="user:1", coordinator_spec_version="v1", runtime_revision="r1", status=parent_status, phase="validating", available_at=NOW, deadline_at=NOW + timedelta(minutes=1), budget_total=_limits(100), budget_reserved=_limits(10), budget_consumed=_limits(0), route="delegation", created_at=NOW, started_at=NOW, completed_at=NOW if parent_status == "cancelled" else None, cancel_requested_at=NOW if parent_status == "cancelled" else None, cancelled_at=NOW if parent_status == "cancelled" else None, updated_at=NOW)
    task = ChildTask(id="task:1", parent_run_id=parent.id, specialist_id="job_web_researcher", specialist_snapshot_id=snapshot_id, goal="find a job", inputs_ref_json={}, constraints_json={}, output_schema_version="job-web-output-v1", requested_allowed_tools=(), requested_deadline=parent.deadline_at, requested_budget=_limits(10), failure_behavior="allow_partial", idempotency_key="delegate:1", contract_hash="a" * 64, contract_version="1", created_at=NOW, updated_at=NOW)
    child = ChildRun(id="child:1", child_task_id=task.id, parent_run_id=parent.id, attempt=1, status=child_status, phase=child_status, deadline_at=parent.deadline_at, created_at=NOW, started_at=NOW, completed_at=NOW, updated_at=NOW)
    return ValidationContext(parent=parent, task=task, child=child, envelope_ref="artifact:envelope:1", principal="user:1", authorized_artifact_refs=frozenset({"artifact:job:1"}), authorized_source_urls=frozenset({"https://example.test/jobs/1"}), ledger_limit=_limits(10))


def _envelope(**changes: object) -> ResultEnvelope:
    values: dict[str, object] = {
        "status": "succeeded",
        "output": {"jobs": [{"title": "Agent Engineer", "company": "Example", "location": "Sydney", "responsibilities": [], "requirements": [], "source_url": "https://example.test/jobs/1", "final_url": "https://example.test/jobs/1", "retrieved_at": "2026-08-13T00:00:00Z", "validation_state": "verified", "content_hash": "b" * 64, "artifact_refs": ["artifact:job:1"]}], "missing": [], "errors": [], "visited": {"page_count": 1, "step_count": 1, "attempts": [], "states": [], "stop_reason": "done"}},
        "evidence": ({"source_url": "https://example.test/jobs/1", "artifact_ref": "artifact:job:1", "field": "title"},),
        "missing": (), "conflicts": (), "errors": (),
        "usage": BudgetUsage(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=1, cost_status="actual", price_version="p1", usage_source="provider"),
        "child_run_id": "child:1", "task_id": "task:1", "trace_ref": "trace:child-run:child:1", "idempotency_key": "result:1",
    }
    values.update(changes)
    return ResultEnvelope(**values)


def _validator() -> ResultValidator:
    registry = SpecialistRegistry(ROOT / "config" / "specialists", project_root=ROOT)
    registry.reload()
    return ResultValidator(registry)


def test_validator_accepts_only_pinned_schema_identity_terminal_order_and_authorized_evidence() -> None:
    result = _validator().validate(_envelope(), _context())
    assert result.accepted
    assert result.envelope_hash == _envelope().canonical_hash


def test_validator_ignores_blank_diagnostic_final_url_but_not_untrusted_url() -> None:
    output = dict(_envelope().output)
    output["visited"] = {
        **output["visited"],
        "attempts": [{"status": "failed", "final_url": ""}],
    }
    assert _validator().validate(_envelope(output=output), _context()).accepted

    output["visited"]["attempts"] = [
        {"status": "failed", "final_url": "https://evil.test/jobs/1"}
    ]
    assert (
        _validator().validate(_envelope(output=output), _context()).code
        == "result_source_unauthorized"
    )


def test_validator_rejects_unknown_cost_late_result_identity_schema_and_unauthorized_source() -> None:
    validator = _validator()
    assert validator.validate(_envelope(usage=BudgetUsage(tokens=1, cost_microunits=0, wall_clock_ms=1, model_calls=1, tool_calls=1, cost_status="unknown")), _context()).code == "result_cost_unknown"
    assert validator.validate(_envelope(child_run_id="child:other"), _context()).code == "result_identity_mismatch"
    assert validator.validate(_envelope(output={"not": "the pinned schema"}), _context()).code == "result_schema_invalid"
    assert validator.validate(_envelope(evidence=({"source_url": "https://evil.test/1", "artifact_ref": "artifact:job:1"},)), _context()).code == "result_source_unauthorized"
    assert validator.validate(_envelope(), _context(parent_status="cancelled")).code == "result_parent_not_mergeable"


def test_validator_allows_one_structured_repair_only_for_schema_with_remaining_budget() -> None:
    validator = _validator()
    invalid = _envelope(output={"not": "the pinned schema"})
    first = validator.validate(invalid, _context(), repair_attempt=0)
    second = validator.validate(invalid, _context(), repair_attempt=1)
    assert first.code == "result_schema_invalid" and first.repair_allowed
    assert second.code == "result_schema_invalid" and not second.repair_allowed


def test_schema_repair_attempt_is_persisted_once_and_never_receives_raw_context() -> None:
    from starter_agent.delegation.results import StructuredResultRepair
    repair = StructuredResultRepair()
    payload = repair.request(output={"bad": "shape"}, schema={"type": "object"}, errors=("required jobs",))
    assert payload["tools"] == ()
    assert "parent" not in payload and "messages" not in payload


def test_repair_once_uses_scripted_provider_once_and_rejects_unknown_cost() -> None:
    from starter_agent.delegation.results import StructuredResultRepair
    calls: list[dict] = []
    repair = StructuredResultRepair()
    usage = BudgetUsage(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=0, cost_status="actual", price_version="p1", usage_source="provider")
    assert repair.repair_once(lambda payload: calls.append(payload) or ({"jobs": []}, usage), output={"bad": 1}, schema={"type": "object"}, errors=("bad",))[0] == {"jobs": []}
    assert len(calls) == 1 and calls[0]["tools"] == ()
    unknown = BudgetUsage(tokens=1, cost_microunits=0, wall_clock_ms=1, model_calls=1, tool_calls=0, cost_status="unknown")
    import pytest
    with pytest.raises(ValueError, match="unknown"):
        repair.repair_once(lambda _payload: ({}, unknown), output={}, schema={}, errors=("bad",))


def test_validator_fails_closed_when_required_authority_sets_are_missing() -> None:
    context = _context()
    missing = ValidationContext(
        parent=context.parent, task=context.task, child=context.child,
        envelope_ref=context.envelope_ref, principal=context.principal,
        ledger_limit=context.ledger_limit,
    )
    assert _validator().validate(_envelope(), missing).code == "result_artifact_authority_missing"
