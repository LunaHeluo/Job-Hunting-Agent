from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from starter_agent.delegation.context import BuiltChildContext, RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, RunOutcome, RunSpec, TaskContract
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.delegation.worker import ChildRuntimeExecutor
from starter_agent.delegation.results import ResultAcceptanceService, ResultValidator
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.worker import compose_delegation_worker, WorkerPoolConfig
from starter_agent.delegation.dispatcher import DispatcherConfig
from starter_agent.domain.models import Message
from starter_agent.infrastructure.session_store import SQLiteSessionStore


def _limits() -> BudgetLimits:
    return BudgetLimits(tokens=1000, cost_microunits=1000, wall_clock_ms=10_000, model_calls=10, tool_calls=10)


def test_child_executor_persists_and_links_restricted_web_artifact(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    runs = SQLiteRunStore("sqlite:///:memory:", tmp_path)
    artifacts = SQLiteSessionStore("sqlite:///artifacts.db", tmp_path)
    session_id = artifacts.create_session()
    parent = ParentRun(id="parent:web", session_id=str(session_id), origin_turn_id="turn:web", principal="user:owner", coordinator_spec_version="1", runtime_revision="1", available_at=now, deadline_at=now + timedelta(minutes=5), budget_total=_limits(), budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), route="delegation", created_at=now, updated_at=now)
    runs.create_parent(parent)
    contract = TaskContract(task_id="task:web", parent_run_id=parent.id, specialist_id="job_web_researcher", goal="research", inputs={"query": "Sydney"}, requested_allowed_tools=(), requested_deadline=parent.deadline_at, requested_budget=_limits(), failure_behavior="allow_partial", idempotency_key="web:1")
    child = ChildRun(id="child:web", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=parent.deadline_at, created_at=now, updated_at=now)
    runs.create_child_task_and_run(contract=contract, child_run=child, specialist_snapshot_id="snapshot:1", output_schema_version="1", expected_parent_version=0, created_at=now)
    claim = type("Claim", (), {"parent": parent, "task": runs.get_run_tree(parent.id).child_tasks[0], "run": child})()
    context = RunContext(run_id=child.id, parent_run_id=parent.id, child_task_id=contract.task_id, session_id=session_id, turn_id=UUID("00000000-0000-0000-0000-000000000001"), principal=parent.principal, messages=[Message(role="user", content="research")], effective_tool_view=[], budget_limits=_limits(), trace_context=RunTraceContext(parent_run_id=parent.id, child_task_id=contract.task_id, child_run_id=child.id, policy_decision_id="policy:1", approval_id="approval:1"))
    spec = RunSpec(run_id=child.id, run_kind="child", role="specialist", provider="fixture", model="fixture", system_prompt_ref="prompt", output_schema_ref="web", allowed_tools=(), max_steps=1, runtime_revision="1")

    class Runtime:
        async def run(self, *, spec, context, on_tool_event=None, on_tool_artifact=None):
            assert on_tool_artifact is not None
            asyncio.get_running_loop()
            await on_tool_artifact({"source_ref": "tool:web:call", "session_id": session_id, "turn_id": context.turn_id, "tool_name": "mcp__playwright__browser_snapshot", "call_id": "call:web", "content": '{"html":"<main>Agent Engineer</main>"}', "content_sha256": "a" * 64, "source_content_sha256": "b" * 64, "source_url": "https://jobs.example.test/agent"})
            return RunOutcome(disposition="completed", run_id=spec.run_id, status="succeeded", output_ref="output:web")

    executor = ChildRuntimeExecutor(assemble=lambda _claim: BuiltChildContext(spec, context, parent.deadline_at, None), runtime=Runtime(), artifact_store=artifacts, run_store=runs)
    asyncio.run(executor(claim))
    artifact = artifacts.get_tool_artifact_for_principal("tool:web:call", principal="user:owner")
    assert artifact is not None and artifact["child_run_id"] == child.id
    assert artifact["policy_decision_id"] == "policy:1"
    links = runs.get_run_tree(parent.id).artifact_links
    assert len(links) == 2
    assert [item.artifact_ref for item in links if item.kind == "web_tool_result"] == [
        "tool:web:call"
    ]
    assert len([item for item in links if item.kind == "result_envelope"]) == 1


def test_artifact_sink_overrides_untrusted_event_ownership_and_composition_fails_closed(tmp_path: Path) -> None:
    with __import__("pytest").raises(ValueError, match="artifact_store_required"):
        compose_delegation_worker(
            store=object(), registry=object(), context_builder=object(), runtime=object(), authority_factory=lambda *_: None,
            dispatcher_config=DispatcherConfig(), worker_config=WorkerPoolConfig(),
        )


def test_terminal_envelope_can_be_accepted_by_recovery_from_persisted_artifact(tmp_path: Path) -> None:
    from starter_agent.delegation.worker import build_child_result_envelope
    from starter_agent.delegation.store import ArtifactLink
    now = datetime(2026, 8, 12, tzinfo=UTC)
    runs = SQLiteRunStore("sqlite:///:memory:", tmp_path)
    artifacts = SQLiteSessionStore("sqlite:///recovery-artifacts.db", tmp_path)
    session_id = artifacts.create_session()
    zero = BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0)
    parent = ParentRun(id="parent:recover", session_id=str(session_id), origin_turn_id="turn:recover", principal="user:owner", coordinator_spec_version="1", runtime_revision="1", available_at=now, deadline_at=now + timedelta(minutes=5), budget_total=_limits(), budget_reserved=zero, budget_consumed=zero, route="delegation", created_at=now, updated_at=now)
    runs.create_parent(parent)
    registry = SpecialistRegistry(Path(__file__).parents[2] / "config" / "specialists", project_root=Path(__file__).parents[2]); snapshot = registry.reload()
    contract = TaskContract(task_id="task:recover", parent_run_id=parent.id, specialist_id="job_web_researcher", goal="research", inputs={"query":"Sydney"}, requested_allowed_tools=(), requested_deadline=parent.deadline_at, requested_budget=_limits(), failure_behavior="allow_partial", idempotency_key="recover:1")
    child = ChildRun(id="child:recover", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=parent.deadline_at, created_at=now, updated_at=now)
    runs.create_child_task_and_run(contract=contract, child_run=child, specialist_snapshot_id=snapshot.snapshot_hash, output_schema_version="job-web-output-v1", expected_parent_version=0, created_at=now)
    claimed = __import__("starter_agent.delegation.dispatcher", fromlist=["Dispatcher","DispatcherConfig"]).Dispatcher(runs, config=__import__("starter_agent.delegation.dispatcher", fromlist=["DispatcherConfig"]).DispatcherConfig(), now=lambda: now).claim_next(worker_id="worker:1")
    from starter_agent.delegation.models import BudgetUsage
    envelope = build_child_result_envelope(task_id=contract.task_id, child_run_id=child.id, status="succeeded", output={"jobs": [], "missing": [], "errors": [], "visited": {"page_count": 0, "step_count": 0, "attempts": [], "states": [], "stop_reason": "done"}}, usage=BudgetUsage(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0, cost_status="actual", price_version="p1", usage_source="provider"), trace_ref=f"trace:child-run:{child.id}")
    ref = "artifact:result-envelope:recover"
    artifacts.save_tool_artifact(source_ref=ref, session_id=session_id, turn_id=UUID("00000000-0000-0000-0000-000000000002"), tool_name="result_envelope", content=envelope.model_dump_json(), parent_run_id=parent.id, child_task_id=contract.task_id, child_run_id=child.id, principal=parent.principal)
    runs.link_artifact(ArtifactLink(id="link:recover", parent_run_id=parent.id, child_run_id=child.id, artifact_ref=ref, kind="result_envelope", restricted=True, principal=parent.principal, artifact_type="result_envelope", trace_ref=envelope.trace_ref, created_at=now))
    runs.complete_child_run(child.id, target_status="succeeded", result_envelope_ref=ref, result_hash=envelope.canonical_hash, worker_id="worker:1", lease_token=claimed.lease_token, expected_version=claimed.run.version, completed_at=now)
    service = ResultAcceptanceService(store=runs, validator=ResultValidator(registry), artifact_store=artifacts)
    assert service.accept_pending_terminal(child.id, now=now).accepted
    assert runs.get_child_task(contract.task_id).accepted_result_hash == envelope.canonical_hash
