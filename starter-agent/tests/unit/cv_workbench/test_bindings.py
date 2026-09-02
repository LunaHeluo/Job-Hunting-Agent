from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from starter_agent.cv_workbench.bindings import (
    BindEvidenceCommand,
    EvidenceBindingRejected,
    EvidenceBindingService,
    EvidenceSourceSnapshot,
)
from starter_agent.cv_workbench.contracts import (
    BusinessOperation,
    CONTRACT_VERSION,
    OperationStatus,
    Workspace,
)
from starter_agent.cv_workbench.store import ForbiddenError, SQLiteWorkbenchStore


FIXTURES = Path(__file__).parents[2] / "fixtures" / "cv_workbench"
PRINCIPAL = "local-user"


@dataclass
class FakeReader:
    snapshot: EvidenceSourceSnapshot

    def inspect(self, source_ref, *, principal, workspace_id, now):
        return self.snapshot


def setup_store(tmp_path: Path) -> SQLiteWorkbenchStore:
    store = SQLiteWorkbenchStore(
        f"sqlite:///{(tmp_path / 'bindings.db').as_posix()}", tmp_path
    )
    workspace_payload = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    workspace_payload["revision"] = 1
    store.create(Workspace.model_validate(workspace_payload), principal=PRINCIPAL)
    operation = BusinessOperation.model_validate(
        {
            "contract_version": CONTRACT_VERSION,
            "operation_id": "op_binding_1",
            "workspace_id": "ws_demo",
            "operation_type": "match_analysis",
            "idempotency_key": "binding-1",
            "input_sha256": "a" * 64,
            "expected_revision": 1,
            "status": OperationStatus.CREATED,
            "parent_run_id": None,
            "task_id": None,
            "result_object_id": None,
            "error_code": None,
            "retryable": False,
            "revision": 1,
            "created_at": "2026-08-17T09:00:00+08:00",
            "updated_at": "2026-08-17T09:00:00+08:00",
        }
    )
    store.create_or_get_operation(operation, principal=PRINCIPAL)
    return store


def command(expected_sha256: str | None = "b" * 64) -> BindEvidenceCommand:
    return BindEvidenceCommand(
        workspace_id="ws_demo",
        subject_id="op_binding_1",
        source_kind="artifact",
        source_ref="artifact:run:binding-1",
        expected_sha256=expected_sha256,
    )


def test_bind_persists_only_safe_summary_and_advanced_route(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    reader = FakeReader(
        EvidenceSourceSnapshot(
            exists=True,
            authorized=True,
            content_sha256="b" * 64,
            metadata={
                "tool_name": "browser_snapshot",
                "created_at": "2026-08-17T09:00:00+00:00",
                "content": "restricted resume body",
                "html": "<main>secret</main>",
            },
        )
    )
    service = EvidenceBindingService(store=store, readers={"artifact": reader})

    summary = service.bind(command(), principal=PRINCIPAL)
    replay = service.bind(command(), principal=PRINCIPAL)

    assert replay.binding_id == summary.binding_id
    assert replay.status == summary.status
    assert summary.traceable is True
    assert summary.detail_route.endswith("/advanced")
    assert summary.metadata == {
        "tool_name": "browser_snapshot",
        "created_at": "2026-08-17T09:00:00+00:00",
    }
    serialized = repr(service.summaries("op_binding_1", principal=PRINCIPAL))
    assert "restricted resume body" not in serialized
    assert "<main>secret</main>" not in serialized


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (EvidenceSourceSnapshot(exists=False, authorized=True), "missing"),
        (EvidenceSourceSnapshot(exists=True, authorized=False), "forbidden"),
        (
            EvidenceSourceSnapshot(
                exists=True, authorized=True, expired=True, content_sha256="b" * 64
            ),
            "expired",
        ),
        (
            EvidenceSourceSnapshot(
                exists=True, authorized=True, content_sha256="c" * 64
            ),
            "hash_mismatch",
        ),
    ],
)
def test_invalid_initial_binding_is_rejected(
    tmp_path: Path, snapshot: EvidenceSourceSnapshot, reason: str
) -> None:
    store = setup_store(tmp_path)
    service = EvidenceBindingService(
        store=store, readers={"artifact": FakeReader(snapshot)}
    )

    with pytest.raises(EvidenceBindingRejected, match=reason):
        service.bind(command(), principal=PRINCIPAL)

    assert store.list_evidence_bindings(
        "op_binding_1", principal=PRINCIPAL
    ) == ()


def test_refresh_marks_expired_reference_untraceable_but_keeps_projection(
    tmp_path: Path,
) -> None:
    store = setup_store(tmp_path)
    reader = FakeReader(
        EvidenceSourceSnapshot(
            exists=True, authorized=True, content_sha256="b" * 64,
            metadata={"tool_name": "browser_snapshot"},
        )
    )
    service = EvidenceBindingService(store=store, readers={"artifact": reader})
    service.bind(command(), principal=PRINCIPAL)
    reader.snapshot = EvidenceSourceSnapshot(
        exists=True,
        authorized=True,
        expired=True,
        content_sha256="b" * 64,
        metadata={"tool_name": "browser_snapshot"},
    )

    (expired,) = service.refresh_subject("op_binding_1", principal=PRINCIPAL)
    audit = service.audit("op_binding_1", principal=PRINCIPAL)

    assert expired.status == "expired"
    assert expired.traceable is False
    assert expired.detail_route is None
    assert expired.metadata == {"tool_name": "browser_snapshot"}
    assert audit.total == audit.broken == 1
    assert audit.traceable == 0


def test_subject_must_belong_to_workspace_and_principal(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    workspace_payload = json.loads(
        (FIXTURES / "business-objects.json").read_text(encoding="utf-8")
    )["workspace"]
    workspace_payload.update(workspace_id="ws_other", revision=1)
    store.create(Workspace.model_validate(workspace_payload), principal=PRINCIPAL)
    reader = FakeReader(
        EvidenceSourceSnapshot(
            exists=True, authorized=True, content_sha256="b" * 64
        )
    )
    service = EvidenceBindingService(store=store, readers={"artifact": reader})

    with pytest.raises(ForbiddenError):
        service.bind(command(), principal="other-user")
    with pytest.raises(ForbiddenError):
        service.bind(
            BindEvidenceCommand(
                workspace_id="ws_other",
                subject_id="op_binding_1",
                source_kind="artifact",
                source_ref="artifact:run:binding-1",
                expected_sha256="b" * 64,
            ),
            principal=PRINCIPAL,
        )
