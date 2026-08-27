from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from starter_agent.cv_workbench.contracts import OperationStatus, Workspace
from starter_agent.cv_workbench.operations import (
    BusinessOperationService,
    CommitReceipt,
    OperationCommand,
    OperationStateError,
    OperationValidationError,
    RunBinding,
    RunOutcome,
    SafetyDecision,
    ValidationDecision,
)
from starter_agent.cv_workbench.store import SQLiteWorkbenchStore

FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"
RESULT_REF = "artifact://runs/run-1/result.json"
RESULT_SHA256 = "b" * 64


def make_workspace() -> Workspace:
    payload = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    return Workspace.model_validate(payload)


@dataclass
class FakeValidator:
    accepted: bool = True
    partial: bool = False
    calls: int = 0

    def validate(self, operation, outcome) -> ValidationDecision:
        self.calls += 1
        return ValidationDecision(
            accepted=self.accepted,
            validator_version="validator-v1",
            result_ref=outcome.result_ref,
            result_sha256=outcome.result_sha256,
            evidence_refs=("chunk-1",),
            partial=self.partial,
            error_code=None if self.accepted else "evidence_missing",
        )


@dataclass
class FakeSafetyGate:
    allowed: bool = True
    calls: int = 0

    def evaluate(self, operation, decision) -> SafetyDecision:
        self.calls += 1
        return SafetyDecision(
            allowed=self.allowed,
            summary={"claims_checked": 1},
            error_code=None if self.allowed else "unsafe_claim",
        )


@dataclass
class FakeCommitter:
    failures_remaining: int = 0
    calls: list[str] = field(default_factory=list)

    def commit(self, operation, checkpoint) -> CommitReceipt:
        self.calls.append(operation.operation_id)
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise OperationValidationError("temporary_commit_failure")
        return CommitReceipt(result_object_id=operation.workspace_id)


@dataclass
class FakeRunController:
    cancelled: list[tuple[str, str]] = field(default_factory=list)

    def cancel(self, parent_run_id: str, *, principal: str) -> None:
        self.cancelled.append((parent_run_id, principal))


def make_service(
    tmp_path: Path,
    *,
    validator: FakeValidator | None = None,
    gate: FakeSafetyGate | None = None,
    committer: FakeCommitter | None = None,
    controller: FakeRunController | None = None,
) -> tuple[
    BusinessOperationService,
    SQLiteWorkbenchStore,
    FakeValidator,
    FakeSafetyGate,
    FakeCommitter,
]:
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'operations.db').as_posix()}", tmp_path
    )
    store.create(make_workspace(), principal=PRINCIPAL)
    validator = validator or FakeValidator()
    gate = gate or FakeSafetyGate()
    committer = committer or FakeCommitter()
    return (
        BusinessOperationService(
            store=store,
            validator=validator,
            safety_gate=gate,
            committer=committer,
            run_controller=controller,
        ),
        store,
        validator,
        gate,
        committer,
    )


def command(operation_id: str = "op_1") -> OperationCommand:
    return OperationCommand(
        operation_id=operation_id,
        workspace_id="ws_demo",
        operation_type="generate_resume",
        idempotency_key="generate-resume-1",
        input_sha256="a" * 64,
        expected_revision=1,
    )


def start(service: BusinessOperationService, operation_id: str = "op_1") -> None:
    service.create(command(operation_id), principal=PRINCIPAL)
    service.bind_run(
        operation_id,
        RunBinding(parent_run_id="run-1", task_id="task-1"),
        principal=PRINCIPAL,
    )


def successful_outcome(status: str = "succeeded") -> RunOutcome:
    return RunOutcome(
        parent_run_id="run-1",
        status=status,
        result_ref=RESULT_REF,
        result_sha256=RESULT_SHA256,
    )


def test_create_is_idempotent_and_run_binding_replay_is_safe(tmp_path: Path) -> None:
    service, store, _, _, _ = make_service(tmp_path)

    first, created = service.create(command(), principal=PRINCIPAL)
    replay, replay_created = service.create(
        command(operation_id="op_different_client_id"), principal=PRINCIPAL
    )
    bound = service.bind_run(
        first.operation_id,
        RunBinding(parent_run_id="run-1", task_id="task-1"),
        principal=PRINCIPAL,
    )
    bound_replay = service.bind_run(
        first.operation_id,
        RunBinding(parent_run_id="run-1", task_id="task-1"),
        principal=PRINCIPAL,
    )

    assert created is True
    assert replay_created is False
    assert replay.operation_id == first.operation_id
    assert bound_replay == bound
    assert [event.event_type for event in store.list_events("op_1", principal=PRINCIPAL)] == [
        "operation_created",
        "run_bound",
    ]


def test_success_is_validated_checkpointed_then_committed(tmp_path: Path) -> None:
    service, store, validator, gate, committer = make_service(tmp_path)
    start(service)

    result = service.process_run_outcome(
        "op_1", successful_outcome(), principal=PRINCIPAL
    )
    checkpoint = store.get_operation_checkpoint("op_1", principal=PRINCIPAL)

    assert result.status == OperationStatus.COMMITTED
    assert result.result_object_id == "ws_demo"
    assert validator.calls == gate.calls == 1
    assert committer.calls == ["op_1"]
    assert checkpoint is not None
    assert checkpoint.result_ref == RESULT_REF
    assert checkpoint.result_sha256 == RESULT_SHA256
    assert checkpoint.commit_attempts == 1
    assert checkpoint.last_commit_error is None
    assert [event.event_type for event in store.list_events("op_1", principal=PRINCIPAL)] == [
        "operation_created",
        "run_bound",
        "validation_started",
        "validation_passed",
        "business_committed",
    ]


@pytest.mark.parametrize("rejection_source", ["validator", "safety"])
def test_rejection_never_reaches_business_commit(
    tmp_path: Path, rejection_source: str
) -> None:
    validator = FakeValidator(accepted=rejection_source != "validator")
    gate = FakeSafetyGate(allowed=rejection_source != "safety")
    service, store, _, _, committer = make_service(
        tmp_path, validator=validator, gate=gate
    )
    start(service)

    result = service.process_run_outcome(
        "op_1", successful_outcome(), principal=PRINCIPAL
    )

    assert result.status == OperationStatus.REJECTED
    assert result.error_code in {"evidence_missing", "unsafe_claim"}
    assert committer.calls == []
    assert store.get_operation_checkpoint("op_1", principal=PRINCIPAL) is None


def test_missing_result_reference_follows_validating_to_rejected(tmp_path: Path) -> None:
    service, store, validator, gate, committer = make_service(tmp_path)
    start(service)

    result = service.process_run_outcome(
        "op_1",
        RunOutcome(parent_run_id="run-1", status="succeeded"),
        principal=PRINCIPAL,
    )

    assert result.status == OperationStatus.REJECTED
    assert result.error_code == "run_result_reference_missing"
    assert validator.calls == gate.calls == 0
    assert committer.calls == []
    assert [event.event_type for event in store.list_events("op_1", principal=PRINCIPAL)][
        -2:
    ] == ["validation_started", "operation_rejected"]


def test_commit_failure_is_recoverable_without_revalidation(tmp_path: Path) -> None:
    committer = FakeCommitter(failures_remaining=1)
    service, store, validator, gate, _ = make_service(tmp_path, committer=committer)
    start(service)

    failed = service.process_run_outcome(
        "op_1", successful_outcome(), principal=PRINCIPAL
    )
    recovered = service.retry_commit("op_1", principal=PRINCIPAL)
    checkpoint = store.get_operation_checkpoint("op_1", principal=PRINCIPAL)

    assert failed.status == OperationStatus.COMMIT_FAILED
    assert failed.retryable is True
    assert recovered.status == OperationStatus.COMMITTED
    assert validator.calls == gate.calls == 1
    assert committer.calls == ["op_1", "op_1"]
    assert checkpoint is not None
    assert checkpoint.commit_attempts == 2
    assert checkpoint.last_commit_error is None


def test_partial_result_must_be_declared_and_can_commit(tmp_path: Path) -> None:
    service, store, _, _, _ = make_service(
        tmp_path, validator=FakeValidator(partial=True)
    )
    start(service)

    result = service.process_run_outcome(
        "op_1", successful_outcome("partial"), principal=PRINCIPAL
    )

    assert result.status == OperationStatus.COMMITTED
    assert store.get_operation_checkpoint("op_1", principal=PRINCIPAL).partial is True
    assert "run_partial" in {
        event.event_type
        for event in store.list_events("op_1", principal=PRINCIPAL)
    }


@pytest.mark.parametrize("status", ["failed", "timed_out", "budget_exhausted"])
def test_run_failure_is_terminal_and_does_not_commit(
    tmp_path: Path, status: str
) -> None:
    service, store, validator, gate, committer = make_service(tmp_path)
    start(service)

    result = service.process_run_outcome(
        "op_1",
        RunOutcome(parent_run_id="run-1", status=status),
        principal=PRINCIPAL,
    )

    assert result.status == OperationStatus.FAILED
    assert validator.calls == gate.calls == 0
    assert committer.calls == []
    assert store.get_operation_checkpoint("op_1", principal=PRINCIPAL) is None


def test_cancel_propagates_to_bound_run_and_is_idempotent(tmp_path: Path) -> None:
    controller = FakeRunController()
    service, _, _, _, _ = make_service(tmp_path, controller=controller)
    start(service)

    cancelled = service.cancel("op_1", principal=PRINCIPAL)
    replay = service.cancel("op_1", principal=PRINCIPAL)

    assert cancelled.status == OperationStatus.CANCELLED
    assert replay == cancelled
    assert controller.cancelled == [("run-1", PRINCIPAL)]


def test_outcome_from_another_run_is_rejected(tmp_path: Path) -> None:
    service, _, _, _, _ = make_service(tmp_path)
    start(service)

    with pytest.raises(OperationStateError, match="run_outcome_binding_mismatch"):
        service.process_run_outcome(
            "op_1",
            RunOutcome(parent_run_id="run-other", status="failed"),
            principal=PRINCIPAL,
        )
