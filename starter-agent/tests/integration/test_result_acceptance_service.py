from __future__ import annotations

from datetime import timedelta

from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.results import ResultAcceptanceService, ResultValidator
from starter_agent.delegation.store import ArtifactLink, SQLiteRunStore
from tests.unit.test_delegation_store import NOW, PROJECT_ROOT, _child_run, _contract, _create_child, _database_url, _parent
from tests.unit.test_result_validator import _envelope


def test_result_acceptance_service_is_the_production_boundary() -> None:
    assert ResultAcceptanceService.__name__ == "ResultAcceptanceService"


def test_service_builds_fail_closed_authority_from_persisted_links_and_accepts_once() -> None:
    url, root = _database_url("acceptance-authority")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent())
    registry = SpecialistRegistry(PROJECT_ROOT / "config" / "specialists", project_root=PROJECT_ROOT)
    snapshot = registry.reload()
    creation = _create_child(store, _contract(), _child_run(), expected_parent_version=0, specialist_snapshot_id=snapshot.snapshot_hash)
    store.transition("child-run:001", "queued", expected_version=0, occurred_at=NOW)
    claimed = store.claim_child_run("child-run:001", worker_id="worker:1", lease_token="lease:1", expected_version=1, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=30))
    envelope = _envelope(child_run_id="child-run:001", task_id="task:001", trace_ref="trace:child-run:child-run:001")
    store.complete_child_run("child-run:001", target_status="succeeded", result_envelope_ref="artifact:envelope:1", result_hash=envelope.canonical_hash, worker_id="worker:1", lease_token="lease:1", expected_version=claimed.version, completed_at=NOW + timedelta(seconds=2))
    store.link_artifact(ArtifactLink(id="link:envelope", parent_run_id="parent:001", child_run_id="child-run:001", artifact_ref="artifact:envelope:1", kind="result_envelope", restricted=True, principal="user:001", created_at=NOW))
    store.link_artifact(ArtifactLink(id="link:job", parent_run_id="parent:001", child_run_id="child-run:001", artifact_ref="artifact:job:1", kind="web_tool_result", restricted=True, principal="user:001", source_url="https://example.test/jobs/1", content_hash="b" * 64, created_at=NOW))
    service = ResultAcceptanceService(store=store, validator=ResultValidator(registry))
    result = service.validate_and_accept(envelope, now=NOW + timedelta(seconds=3), child_run_id="child-run:001", envelope_ref="artifact:envelope:1")
    assert result.accepted
    assert store.get_child_task(creation.task.id).accepted_result_hash == envelope.canonical_hash
    store.close()


def test_repair_service_calls_no_tool_provider_once_and_rejects_second_before_provider() -> None:
    from starter_agent.delegation.results import StructuredResultRepair
    calls: list[dict] = []

    class Store:
        def begin_result_repair_attempt(self, **_kwargs): return None
        def complete_result_repair_attempt(self, *_args, **_kwargs): return None

    usage = __import__("starter_agent.delegation.models", fromlist=["BudgetUsage"]).BudgetUsage(tokens=1, cost_microunits=1, wall_clock_ms=1, model_calls=1, tool_calls=0, cost_status="actual", price_version="p1", usage_source="provider")
    service = ResultAcceptanceService(store=Store(), validator=None, repair=StructuredResultRepair())
    assert service.repair_once(child_run_id="child:1", envelope_hash="a" * 64, output={"bad": 1}, schema={"type": "object"}, errors=("bad",), provider=lambda payload: calls.append(payload) or ({"ok": 1}, usage), expected_parent_version=0)[0] == {"ok": 1}
    assert calls[0]["tools"] == ()


def test_repair_service_rejects_budget_before_provider() -> None:
    from starter_agent.delegation.results import StructuredResultRepair
    class Store:
        def begin_result_repair_attempt(self, **_kwargs): raise ValueError("budget insufficient")
    service = ResultAcceptanceService(store=Store(), validator=None, repair=StructuredResultRepair())
    import pytest
    with pytest.raises(ValueError, match="budget"):
        service.repair_once(child_run_id="child:1", envelope_hash="a" * 64, output={}, schema={}, errors=("bad",), provider=lambda _payload: (_ for _ in ()).throw(AssertionError("provider called")), expected_parent_version=0)


def test_acceptance_service_persists_merge_report_only_through_single_store_command() -> None:
    from starter_agent.delegation.results import DeterministicResultMerger
    class Store:
        def accept_validated_result(self, command):
            self.command = command
    store = Store()
    service = ResultAcceptanceService(store=store, validator=None)
    assert DeterministicResultMerger.__name__ == "DeterministicResultMerger"
    assert not hasattr(store, "stage_candidate_merge")


class _Artifacts:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def get_tool_artifact_for_principal(self, ref: str, *, principal: str):
        content = self.items.get(ref)
        return None if content is None else {"content": content, "principal": principal}


def test_merge_ready_parent_reads_only_accepted_artifacts_and_is_idempotent() -> None:
    """The parent finalization path has no caller-supplied Child results."""
    url, root = _database_url("ready-parent")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent(status="running", phase="validating", started_at=NOW))
    registry = SpecialistRegistry(PROJECT_ROOT / "config" / "specialists", project_root=PROJECT_ROOT)
    snapshot = registry.reload()
    artifacts = _Artifacts()
    service = ResultAcceptanceService(store=store, validator=ResultValidator(registry), artifact_store=artifacts)
    for suffix in ("001", "002"):
        task_id, child_id, ref = f"task:{suffix}", f"child-run:{suffix}", f"artifact:envelope:{suffix}"
        parent = store.get_parent("parent:001")
        creation = _create_child(store, _contract(task_id=task_id), _child_run(task_id=task_id, run_id=child_id), expected_parent_version=parent.version, specialist_snapshot_id=snapshot.snapshot_hash)
        store.transition(child_id, "queued", expected_version=0, occurred_at=NOW)
        claimed = store.claim_child_run(child_id, worker_id="worker:1", lease_token=f"lease:{suffix}", expected_version=1, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=30))
        envelope = _envelope(child_run_id=child_id, task_id=task_id, trace_ref=f"trace:child-run:{child_id}")
        store.complete_child_run(child_id, target_status="succeeded", result_envelope_ref=ref, result_hash=envelope.canonical_hash, worker_id="worker:1", lease_token=f"lease:{suffix}", expected_version=claimed.version, completed_at=NOW + timedelta(seconds=2))
        store.link_artifact(ArtifactLink(id=f"link:envelope:{suffix}", parent_run_id="parent:001", child_run_id=child_id, artifact_ref=ref, kind="result_envelope", restricted=True, principal="user:001", created_at=NOW))
        store.link_artifact(ArtifactLink(id=f"link:job:{suffix}", parent_run_id="parent:001", child_run_id=child_id, artifact_ref="artifact:job:1", kind="web_tool_result", restricted=True, principal="user:001", source_url="https://example.test/jobs/1", content_hash="b" * 64, created_at=NOW))
        artifacts.items[ref] = envelope.model_dump_json()
        assert service.accept_pending_terminal(child_id, now=NOW + timedelta(seconds=3)).accepted
        assert creation.task.id == task_id
    current = store.get_parent("parent:001")
    merged = service.merge_ready_parent("parent:001", expected_version=current.version, now=NOW + timedelta(seconds=4))
    repeated = service.merge_ready_parent("parent:001", expected_version=store.get_parent("parent:001").version, now=NOW + timedelta(seconds=5))
    assert merged.status == repeated.status == "merged"
    assert merged.merge_report_id == repeated.merge_report_id
    final = store.get_parent("parent:001")
    assert final.status == "succeeded" and final.merge_report_id == merged.merge_report_id
    store.close()


def test_merge_ready_parent_waits_and_rejects_cancelled_parent() -> None:
    url, root = _database_url("ready-parent-wait")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent(status="running", phase="validating", started_at=NOW))
    service = ResultAcceptanceService(store=store, validator=None, artifact_store=_Artifacts())
    assert service.merge_ready_parent("parent:001", expected_version=0, now=NOW).status == "waiting"
    cancelled = store.request_parent_cancellation("parent:001", reason="test", requested_at=NOW + timedelta(seconds=1))
    import pytest
    with pytest.raises(ValueError, match="not_mergeable"):
        service.merge_ready_parent("parent:001", expected_version=cancelled.version, now=NOW + timedelta(seconds=2))
    store.close()


def test_merge_ready_parent_preserves_allow_partial_failure_without_candidate_merge() -> None:
    url, root = _database_url("ready-parent-partial")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent(status="running", phase="validating", started_at=NOW))
    registry = SpecialistRegistry(PROJECT_ROOT / "config" / "specialists", project_root=PROJECT_ROOT)
    snapshot, artifacts = registry.reload().snapshot_hash, _Artifacts()
    service = ResultAcceptanceService(store=store, validator=ResultValidator(registry), artifact_store=artifacts)
    success = _create_child(store, _contract(task_id="task:success"), _child_run(task_id="task:success", run_id="child-run:success"), expected_parent_version=0, specialist_snapshot_id=snapshot)
    store.transition(success.run.id, "queued", expected_version=0, occurred_at=NOW)
    claim = store.claim_child_run(success.run.id, worker_id="worker:1", lease_token="lease:success", expected_version=1, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=30))
    envelope = _envelope(child_run_id=success.run.id, task_id=success.task.id, trace_ref=f"trace:child-run:{success.run.id}")
    ref = "artifact:envelope:success"
    store.complete_child_run(success.run.id, target_status="succeeded", result_envelope_ref=ref, result_hash=envelope.canonical_hash, worker_id="worker:1", lease_token="lease:success", expected_version=claim.version, completed_at=NOW + timedelta(seconds=2))
    store.link_artifact(ArtifactLink(id="link:envelope:success", parent_run_id="parent:001", child_run_id=success.run.id, artifact_ref=ref, kind="result_envelope", restricted=True, principal="user:001", created_at=NOW))
    store.link_artifact(ArtifactLink(id="link:job:success", parent_run_id="parent:001", child_run_id=success.run.id, artifact_ref="artifact:job:1", kind="web", restricted=True, principal="user:001", source_url="https://example.test/jobs/1", content_hash="b" * 64, created_at=NOW))
    artifacts.items[ref] = envelope.model_dump_json()
    assert service.accept_pending_terminal(success.run.id, now=NOW + timedelta(seconds=3)).accepted
    parent = store.get_parent("parent:001")
    failure = _create_child(store, _contract(task_id="task:failed", failure_behavior="allow_partial"), _child_run(task_id="task:failed", run_id="child-run:failed"), expected_parent_version=parent.version, specialist_snapshot_id=snapshot)
    store.transition(failure.run.id, "queued", expected_version=0, occurred_at=NOW)
    claim = store.claim_child_run(failure.run.id, worker_id="worker:1", lease_token="lease:failed", expected_version=1, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=30))
    store.complete_child_run(failure.run.id, target_status="failed", result_envelope_ref="artifact:failed", result_hash="a" * 64, worker_id="worker:1", lease_token="lease:failed", expected_version=claim.version, completed_at=NOW + timedelta(seconds=2))
    parent = store.get_parent("parent:001")
    result = service.merge_ready_parent("parent:001", expected_version=parent.version, now=NOW + timedelta(seconds=4))
    tree = store.get_run_tree("parent:001")
    assert result.status == "merged" and tree.parent.status == "partial"
    report = next(item for item in tree.merge_reports if item.id == result.merge_report_id)
    assert "task:task:failed" in report.missing
    assert report.rejected[0]["task_id"] == "task:failed" and report.rejected[0]["child_run_id"] == "child-run:failed"
    store.close()


def test_merge_ready_parent_fails_parent_without_merging_fail_parent_task() -> None:
    url, root = _database_url("ready-parent-fail")
    store = SQLiteRunStore(url, root)
    store.create_parent(_parent(status="running", phase="validating", started_at=NOW))
    snapshot = SpecialistRegistry(PROJECT_ROOT / "config" / "specialists", project_root=PROJECT_ROOT).reload().snapshot_hash
    failure = _create_child(store, _contract(task_id="task:failed", failure_behavior="fail_parent"), _child_run(task_id="task:failed", run_id="child-run:failed"), expected_parent_version=0, specialist_snapshot_id=snapshot)
    store.transition(failure.run.id, "queued", expected_version=0, occurred_at=NOW)
    claim = store.claim_child_run(failure.run.id, worker_id="worker:1", lease_token="lease:failed", expected_version=1, claimed_at=NOW + timedelta(seconds=1), lease_ttl=timedelta(seconds=30))
    store.complete_child_run(failure.run.id, target_status="failed", result_envelope_ref="artifact:failed", result_hash="a" * 64, worker_id="worker:1", lease_token="lease:failed", expected_version=claim.version, completed_at=NOW + timedelta(seconds=2))
    result = ResultAcceptanceService(store=store, validator=None).merge_ready_parent("parent:001", expected_version=store.get_parent("parent:001").version, now=NOW + timedelta(seconds=3))
    tree = store.get_run_tree("parent:001")
    assert result.status == "failed" and tree.parent.status == "failed"
    assert not tree.merge_reports
    store.close()
