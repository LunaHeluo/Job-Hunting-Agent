import pytest

from starter_agent.delegation.specialists.job_web_error_policy import JobWebErrorPolicy, WebErrorKind


@pytest.mark.asyncio
async def test_real_runtime_access_block_suspends_child_with_safe_checkpoint_and_no_bypass() -> None:
    from tests.unit.test_job_web_researcher import _context, _inputs, _runtime, _spec
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher

    runtime, provider, calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/login"}},
    ])
    navigate = runtime.tools.get("mcp__playwright__browser_navigate")
    original = navigate.execute

    async def blocked(arguments, context):
        result = await original(arguments, context)
        return result.model_copy(update={"ok": False, "error_code": "login_required"})

    navigate.execute = blocked
    context = _context()
    saved = {}

    def save_checkpoint(_ref, payload):
        saved.update(payload=payload)
        return "checkpoint:persisted"

    result = await JobWebResearcher(runtime, checkpoint_sink=save_checkpoint).run(
        _spec(), context, _inputs(urls=["https://jobs.example.test/login"], failure_behavior="wait_for_user")
    )

    assert result.outcome.status == "waiting_for_user"
    assert result.outcome.checkpoint_ref == "checkpoint:persisted"
    assert saved["payload"]["parent_run_id"] == context.parent_run_id
    assert saved["payload"]["child_task_id"] == context.child_task_id
    assert saved["payload"]["child_run_id"] == context.run_id
    assert "html" not in repr(saved["payload"]).lower()
    assert [name for name, _args in calls] == ["mcp__playwright__browser_navigate"]
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_real_runtime_recoverable_failure_reenters_model_gate_tool_and_counts_attempts() -> None:
    from tests.unit.test_job_web_researcher import _context, _inputs, _runtime, _spec
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher

    url = "https://jobs.example.test/retry"
    runtime, provider, calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": url}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": url}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": url}},
        {"final": {"jobs": [], "missing": [], "errors": []}},
    ])
    navigate = runtime.tools.get("mcp__playwright__browser_navigate")
    original = navigate.execute
    attempts = 0

    async def flaky(arguments, context):
        nonlocal attempts
        attempts += 1
        result = await original(arguments, context)
        if attempts <= 2:
            return result.model_copy(update={"ok": False, "error_code": "connection_error", "retryable": True})
        return result

    navigate.execute = flaky
    context = _context()
    result = await JobWebResearcher(runtime).run(_spec(), context, _inputs(urls=[url], target_valid_jobs=1))

    assert attempts == 3
    assert len(provider.calls) == 4
    assert context.budget.consumed.tool_calls == 3
    assert [item["error_code"] for item in result.output["visited"]["attempts"][:2]] == [
        "job_web_connection_failed", "job_web_connection_failed",
    ]


@pytest.mark.asyncio
async def test_recovery_exhaustion_blocks_fourth_tool_before_gate_and_side_effect() -> None:
    from tests.unit.test_job_web_researcher import _context, _inputs, _runtime, _spec
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher

    url = "https://jobs.example.test/retry"
    runtime, provider, calls = _runtime([
        *({"tool": "mcp__playwright__browser_navigate", "arguments": {"url": url}} for _ in range(4)),
    ])
    navigate = runtime.tools.get("mcp__playwright__browser_navigate")
    original = navigate.execute
    async def always_fails(arguments, context):
        result = await original(arguments, context)
        return result.model_copy(update={"ok": False, "error_code": "connection_error", "retryable": True})
    navigate.execute = always_fails
    delays = []

    result = await JobWebResearcher(runtime, sleeper=lambda seconds: delays.append(seconds)).run(
        _spec(), _context(), _inputs(urls=[url])
    )

    assert len(calls) == 3
    assert len(provider.calls) == 3
    assert delays == [1, 2]
    assert result.output["visited"]["stop_reason"] == "candidate_recovery_exhausted"


@pytest.mark.asyncio
async def test_404_status_forbids_original_url_without_retrying_tool() -> None:
    from tests.unit.test_job_web_researcher import _context, _inputs, _runtime, _spec
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher

    url = "https://jobs.example.test/missing"
    runtime, provider, calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": url}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": url}},
    ])
    navigate = runtime.tools.get("mcp__playwright__browser_navigate")
    original = navigate.execute
    async def missing(arguments, context):
        result = await original(arguments, context)
        return result.model_copy(update={"ok": False, "error_code": "http_error", "metadata": {**result.metadata, "status_code": 404}})
    navigate.execute = missing

    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs(urls=[url]))

    assert len(calls) == 1
    assert len(provider.calls) == 1
    assert result.output["visited"]["attempts"][0]["error_code"] == "job_web_not_found"


@pytest.mark.asyncio
async def test_partial_access_block_preserves_previously_verified_incremental_jobs() -> None:
    from tests.unit.test_job_web_researcher import _context, _inputs, _runtime, _spec
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher

    verified = "https://jobs.example.test/verified"; login = "https://jobs.example.test/login"
    runtime, _provider, _calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": verified}},
        {"tool": "mcp__playwright__browser_wait_for", "arguments": {"time": 1}},
        {"tool": "mcp__playwright__browser_snapshot", "arguments": {}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": login}},
    ])
    navigate = runtime.tools.get("mcp__playwright__browser_navigate")
    original = navigate.execute
    async def blocked(arguments, context):
        result = await original(arguments, context)
        return result.model_copy(update={"ok": False, "error_code": "permission_denied"}) if arguments.get("url") == login else result
    navigate.execute = blocked
    snapshot = runtime.tools.get("mcp__playwright__browser_snapshot"); original_snapshot = snapshot.execute
    async def structured(arguments, tool_context):
        result = await original_snapshot(arguments, tool_context)
        return result.model_copy(update={"data": {
        "title": "Agent Engineer", "company": "Example", "location": "Sydney",
        "responsibilities": ["Build agents"], "requirements": ["Python"],
        "source_url": verified, "final_url": verified,
        "retrieved_at": "2026-08-12T00:00:00Z", "validation_state": "verified",
        "content_hash": "a" * 64, "artifact_refs": ["artifact:one"],
        }})
    snapshot.execute = structured
    context = _context()

    result = await JobWebResearcher(runtime).run(
        _spec(), context, _inputs(urls=[verified, login], failure_behavior="allow_partial")
    )

    assert result.outcome.status == "partial"
    assert [job["title"] for job in result.output["jobs"]] == ["Agent Engineer"]
    assert result.output["missing"] and result.output["errors"]


def test_handoff_checkpoint_binds_parent_task_child_and_principal() -> None:
    policy = JobWebErrorPolicy()
    checkpoint = policy.create_handoff_checkpoint(
        parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1",
        principal="user:1", requested_url="https://jobs.example.test/login",
        next_phase="open", created_at="2026-08-12T00:00:00+00:00",
    )

    restored = policy.resume_handoff(
        checkpoint, parent_run_id="parent:1", child_task_id="task:1",
        child_run_id="child:1", principal="user:1", now="2026-08-12T00:05:00+00:00",
    )

    assert restored["next_phase"] == "open"
    assert "messages" not in restored and "html" not in restored


@pytest.mark.parametrize(
    "changed",
    ["parent_run_id", "child_task_id", "child_run_id", "principal"],
)
def test_resume_rejects_cross_authority_checkpoint(changed) -> None:
    policy = JobWebErrorPolicy()
    checkpoint = policy.create_handoff_checkpoint(
        parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1",
        principal="user:1", requested_url="https://jobs.example.test/login",
        next_phase="open", created_at="2026-08-12T00:00:00+00:00",
    )
    authority = dict(parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1", principal="user:1")
    authority[changed] = "other"

    with pytest.raises(ValueError, match="handoff_checkpoint_authority_mismatch"):
        policy.resume_handoff(checkpoint, **authority, now="2026-08-12T00:05:00+00:00")


def test_handoff_timeout_returns_partial_instead_of_resuming_navigation() -> None:
    policy = JobWebErrorPolicy(handoff_timeout_seconds=60)
    checkpoint = policy.create_handoff_checkpoint(
        parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1",
        principal="user:1", requested_url="https://jobs.example.test/login",
        next_phase="open", created_at="2026-08-12T00:00:00+00:00",
    )

    with pytest.raises(TimeoutError, match="job_web_handoff_timeout"):
        policy.resume_handoff(
            checkpoint, parent_run_id="parent:1", child_task_id="task:1",
            child_run_id="child:1", principal="user:1", now="2026-08-12T00:02:00+00:00",
        )


def test_access_block_decision_is_waiting_user_only_when_contract_allows_it() -> None:
    policy = JobWebErrorPolicy()

    waiting = policy.decide(WebErrorKind.ACCESS_BLOCKED, occurrence=1, failure_behavior="wait_for_user")
    partial = policy.decide(WebErrorKind.ACCESS_BLOCKED, occurrence=1, failure_behavior="allow_partial")

    assert waiting.action == "wait_for_user" and waiting.retryable is False
    assert partial.action == "partial" and partial.retryable is False


def test_dispatcher_persists_waiting_checkpoint_and_clears_child_lease() -> None:
    from datetime import UTC, datetime, timedelta
    from starter_agent.delegation.dispatcher import Dispatcher, DispatcherConfig
    from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, RunOutcome, TaskContract
    from starter_agent.delegation.store import SQLiteRunStore

    now = datetime(2026, 8, 12, tzinfo=UTC)
    limits = BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=10, tool_calls=10)
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    parent = ParentRun(
        id="parent:1", session_id="session:1", origin_turn_id="turn:1", principal="user:1",
        coordinator_spec_version="1", runtime_revision="1", available_at=now,
        deadline_at=now + timedelta(hours=1), budget_total=limits, budget_reserved=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0),
        budget_consumed=BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0), route="delegation", created_at=now, updated_at=now,
    )
    store.create_parent(parent)
    contract = TaskContract(
        task_id="task:1", parent_run_id=parent.id, specialist_id="job_web_researcher", goal="research",
        inputs={}, requested_deadline=now + timedelta(minutes=30), requested_budget=limits,
        failure_behavior="wait_for_user", idempotency_key="handoff:1",
    )
    run = ChildRun(id="child:1", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1,
        status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=now, updated_at=now)
    store.create_child_task_and_run(contract=contract, child_run=run, specialist_snapshot_id="snapshot:1", output_schema_version="1", expected_parent_version=0, created_at=now)
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: now)
    claim = dispatcher.claim_next(worker_id="worker:1")
    assert claim is not None
    ref = store.save_child_checkpoint(claim.run.id, {
        "version": "job-web-handoff-v1", "parent_run_id": parent.id, "child_task_id": contract.task_id,
        "child_run_id": claim.run.id, "principal": parent.principal, "requested_url": "https://jobs.example.test/login",
        "next_phase": "open", "created_at": now.isoformat(),
    })

    waiting = dispatcher.finish(claim, RunOutcome(disposition="suspended", run_id=claim.run.id, status="waiting_for_user", checkpoint_ref=ref))

    assert waiting.status == "waiting_for_user"
    assert waiting.run_context_checkpoint_ref == ref
    assert waiting.lease_owner is waiting.lease_token is None


def test_store_resume_child_checkpoint_validates_persisted_authority_and_timeout() -> None:
    from datetime import UTC, datetime, timedelta
    from starter_agent.delegation.store import SQLiteRunStore

    now = datetime(2026, 8, 12, tzinfo=UTC)
    from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, TaskContract
    limits = BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=10, tool_calls=10)
    zero = BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0)
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    parent = ParentRun(id="parent:1", session_id="session:1", origin_turn_id="turn:1", principal="user:1", coordinator_spec_version="1", runtime_revision="1", available_at=now, deadline_at=now + timedelta(hours=1), budget_total=limits, budget_reserved=zero, budget_consumed=zero, route="delegation", created_at=now, updated_at=now)
    store.create_parent(parent)
    contract = TaskContract(task_id="task:1", parent_run_id=parent.id, specialist_id="job_web_researcher", goal="research", inputs={}, requested_deadline=now + timedelta(minutes=30), requested_budget=limits, failure_behavior="wait_for_user", idempotency_key="resume:1")
    run = ChildRun(id="child:1", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=now, updated_at=now)
    store.create_child_task_and_run(contract=contract, child_run=run, specialist_snapshot_id="snapshot:1", output_schema_version="1", expected_parent_version=0, created_at=now)
    dispatcher_module = __import__("starter_agent.delegation.dispatcher", fromlist=["Dispatcher", "DispatcherConfig"])
    dispatcher = dispatcher_module.Dispatcher(store, config=dispatcher_module.DispatcherConfig(), now=lambda: now)
    claim = dispatcher.claim_next(worker_id="worker:1")
    payload = {
        "version": "job-web-handoff-v1", "parent_run_id": "parent:1", "child_task_id": "task:1",
        "child_run_id": "child:1", "principal": "user:1", "requested_url": "https://jobs.example.test/login",
        "next_phase": "open", "created_at": now.isoformat(),
    }
    ref = store.save_child_checkpoint("child:1", payload)
    waiting = dispatcher.finish(claim, __import__("starter_agent.delegation.models", fromlist=["RunOutcome"]).RunOutcome(disposition="suspended", run_id="child:1", status="waiting_for_user", checkpoint_ref=ref))

    resumed = store.resume_child_from_checkpoint(ref, parent_run_id="parent:1", child_task_id="task:1", child_run_id="child:1", principal="user:1", now=now + timedelta(seconds=30), timeout_seconds=60)
    assert resumed.status == "queued" and resumed.phase == "open"
    assert resumed.run_context_checkpoint_ref == ref
    with pytest.raises(ValueError, match="handoff_checkpoint_authority_mismatch"):
        store.resume_child_from_checkpoint(ref, parent_run_id="other", child_task_id="task:1", child_run_id="child:1", principal="user:1", now=now, timeout_seconds=60)
    assert store.get_run_tree("parent:1").child_runs[0].version == resumed.version


def test_expired_persisted_handoff_does_not_change_waiting_child() -> None:
    from datetime import UTC, datetime, timedelta
    from starter_agent.delegation.dispatcher import Dispatcher, DispatcherConfig
    from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, RunOutcome, TaskContract
    from starter_agent.delegation.store import SQLiteRunStore

    now = datetime(2026, 8, 12, tzinfo=UTC)
    limits = BudgetLimits(tokens=10, cost_microunits=10, wall_clock_ms=10, model_calls=10, tool_calls=10)
    zero = BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0)
    store = SQLiteRunStore("sqlite:///:memory:", ".")
    parent = ParentRun(id="parent:e", session_id="session:e", origin_turn_id="turn:e", principal="user:e", coordinator_spec_version="1", runtime_revision="1", available_at=now, deadline_at=now + timedelta(hours=1), budget_total=limits, budget_reserved=zero, budget_consumed=zero, route="delegation", created_at=now, updated_at=now)
    store.create_parent(parent)
    contract = TaskContract(task_id="task:e", parent_run_id=parent.id, specialist_id="job_web_researcher", goal="research", inputs={}, requested_deadline=now + timedelta(minutes=30), requested_budget=limits, failure_behavior="wait_for_user", idempotency_key="resume:e")
    run = ChildRun(id="child:e", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=now, updated_at=now)
    store.create_child_task_and_run(contract=contract, child_run=run, specialist_snapshot_id="snapshot:e", output_schema_version="1", expected_parent_version=0, created_at=now)
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: now)
    claim = dispatcher.claim_next(worker_id="worker:e")
    payload = {"version": "job-web-handoff-v1", "parent_run_id": parent.id, "child_task_id": contract.task_id, "child_run_id": run.id, "principal": parent.principal, "requested_url": "https://jobs.example.test/login", "next_phase": "open", "created_at": now.isoformat()}
    ref = store.save_child_checkpoint(run.id, payload)
    waiting = dispatcher.finish(claim, RunOutcome(disposition="suspended", run_id=run.id, status="waiting_for_user", checkpoint_ref=ref))

    with pytest.raises(TimeoutError, match="job_web_handoff_timeout"):
        store.resume_child_from_checkpoint(ref, parent_run_id=parent.id, child_task_id=contract.task_id, child_run_id=run.id, principal=parent.principal, now=now + timedelta(seconds=61), timeout_seconds=60)

    unchanged = store.get_run_tree(parent.id).child_runs[0]
    assert unchanged.status == "waiting_for_user" and unchanged.version == waiting.version


def test_production_executor_uses_store_backed_checkpoint_sink() -> None:
    from starter_agent.delegation.worker import ChildRuntimeExecutor

    class Store:
        def save_child_checkpoint(self, child_run_id, payload):
            assert child_run_id == "child:1"
            return "checkpoint:stored"

    executor = ChildRuntimeExecutor(assemble=lambda claim: None, runtime=object(), checkpoint_store=Store())
    researcher = executor._web_researcher()

    assert researcher.checkpoint_sink("ignored", {"child_run_id": "child:1"}) == "checkpoint:stored"


@pytest.mark.asyncio
async def test_reopened_worker_restores_checkpoint_context_without_replaying_completed_steps() -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from tests.unit.test_job_web_researcher import TOOLS, _runtime
    from starter_agent.delegation.context import RunContext, RunTraceContext
    from starter_agent.delegation.dispatcher import Dispatcher, DispatcherConfig
    from starter_agent.delegation.models import BudgetLimits, ChildRun, ParentRun, RunOutcome, RunSpec, TaskContract
    from starter_agent.delegation.store import SQLiteRunStore
    from starter_agent.delegation.worker import ChildRuntimeExecutor
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher
    from starter_agent.domain.models import Message, ToolCall

    from pathlib import Path
    from uuid import uuid4
    now = datetime.now(UTC); root = Path(__file__).parents[2]; db = root / ".task11-round2" / f"handoff-{uuid4().hex}.db"; db.parent.mkdir(exist_ok=True)
    limits = BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=100000, model_calls=10, tool_calls=10)
    zero = BudgetLimits(tokens=0, cost_microunits=0, wall_clock_ms=0, model_calls=0, tool_calls=0)
    store = SQLiteRunStore(f"sqlite:///{db}", root)
    parent = ParentRun(id="parent:r", session_id="session:r", origin_turn_id="turn:r", principal="user:r", coordinator_spec_version="1", runtime_revision="1", available_at=now, deadline_at=now+timedelta(hours=1), budget_total=limits, budget_reserved=zero, budget_consumed=zero, route="delegation", created_at=now, updated_at=now)
    store.create_parent(parent)
    next_url = "https://jobs.example.test/next"
    contract = TaskContract(task_id="task:r", parent_run_id=parent.id, specialist_id="job_web_researcher", goal="research", inputs={"urls":[next_url]}, requested_allowed_tools=TOOLS, requested_deadline=now+timedelta(minutes=30), requested_budget=limits, failure_behavior="wait_for_user", idempotency_key="resume:r")
    run = ChildRun(id="child:r", child_task_id=contract.task_id, parent_run_id=parent.id, attempt=1, status="queued", phase="queued", deadline_at=contract.requested_deadline, created_at=now, updated_at=now)
    store.create_child_task_and_run(contract=contract, child_run=run, specialist_snapshot_id="snapshot:r", output_schema_version="1", expected_parent_version=0, created_at=now)
    dispatcher = Dispatcher(store, config=DispatcherConfig(), now=lambda: now); claim = dispatcher.claim_next(worker_id="worker:old")
    context = RunContext(run_id=run.id, parent_run_id=parent.id, child_task_id=contract.task_id, session_id=__import__("uuid").uuid4(), turn_id=__import__("uuid").uuid4(), principal=parent.principal, messages=[Message(role="user", content="research"), Message(role="assistant", content="", tool_calls=[ToolCall(id="search:1", name="search_jobs_serpapi", arguments={"query":"jobs"})]), Message(role="tool", content='{"ok":true}', name="search_jobs_serpapi", tool_call_id="search:1")], effective_tool_view=list(TOOLS), budget_limits=limits, trace_context=RunTraceContext(parent_run_id=parent.id, child_task_id=contract.task_id, child_run_id=run.id), working_memory={"job_web_progress": {"phase": "open", "step_count": 1, "allowed_urls":[next_url]}})
    context.budget.consume(model_calls=1, tool_calls=1)
    payload = {"version":"job-web-handoff-v1","parent_run_id":parent.id,"child_task_id":contract.task_id,"child_run_id":run.id,"principal":parent.principal,"requested_url":"https://jobs.example.test/login","next_phase":"open","created_at":now.isoformat(),"run_context":context.to_checkpoint(),"progress":{"phase":"open","step_count":1}}
    ref = store.save_child_checkpoint(run.id, payload)
    dispatcher.finish(claim, RunOutcome(disposition="suspended", run_id=run.id, status="waiting_for_user", checkpoint_ref=ref)); store.close()

    reopened = SQLiteRunStore(f"sqlite:///{db}", root)
    resumed = __import__("starter_agent.delegation.service", fromlist=["DelegationResumeService"]).DelegationResumeService(store=reopened, now=lambda: now+timedelta(seconds=1)).resume(parent_run_id=parent.id, child_task_id=contract.task_id, child_run_id=run.id, principal=parent.principal, checkpoint_ref=ref)
    assert resumed.status == "queued"
    resumed_claim = Dispatcher(reopened, config=DispatcherConfig(), now=lambda: now+timedelta(seconds=2)).claim_next(worker_id="worker:new")
    runtime, provider, calls = _runtime([
        {"tool":"mcp__playwright__browser_navigate","arguments":{"url":next_url}},
        {"final":{"jobs":[],"missing":[],"errors":[]}},
    ])
    observed = {}

    class RecordingJobWebResearcher(JobWebResearcher):
        async def run(self, spec, restored_context, inputs):
            result = await super().run(spec, restored_context, inputs)
            observed["context"] = restored_context
            observed["step_count"] = result.output["visited"]["step_count"]
            return result

    fresh = SimpleNamespace(spec=RunSpec(run_id=run.id, run_kind="child", role="specialist", provider="scripted-web", model="fixture", system_prompt_ref="p", output_schema_ref="o", allowed_tools=TOOLS, max_steps=10, runtime_revision="1"), context=context, deadline=contract.requested_deadline, tool_view=None)
    executor = ChildRuntimeExecutor(
        assemble=lambda _claim: fresh,
        runtime=runtime,
        checkpoint_store=reopened,
        web_researcher=RecordingJobWebResearcher(runtime),
    )
    await executor(resumed_claim)
    assert len(provider.calls) == 2
    assert [name for name, _args in calls] == ["mcp__playwright__browser_navigate"]
    restored = observed["context"]
    assert restored.budget.consumed.model_calls == 3
    assert restored.budget.consumed.tool_calls == 2
    assert observed["step_count"] == 2
