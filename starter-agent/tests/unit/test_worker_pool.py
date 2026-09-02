from __future__ import annotations

import asyncio
import json
import pytest
from datetime import UTC, datetime, timedelta

from starter_agent.delegation.models import RunOutcome
from starter_agent.delegation.worker import ChildRuntimeExecutor, WorkerPool, WorkerPoolConfig


NOW = datetime(2026, 8, 12, 9, tzinfo=UTC)


class _Claim:
    def __init__(self, run_id: str, specialist_id: str = "job_web_researcher") -> None:
        self.run = type("Run", (), {"id": run_id, "version": 1})()
        self.specialist_id = specialist_id
        self.lease_token = f"lease:{run_id}"


class _Dispatcher:
    def __init__(self, claims: list[_Claim]) -> None:
        self.claims = claims
        self.completed: list[tuple[str, str]] = []
        self.retried: list[str] = []
        self.heartbeats = 0
        self.interrupted: list[str] = []

    def claim_next(self, *, worker_id: str, eligible_specialists=None, excluded_specialists=None):
        if not self.claims:
            return None
        for index, claim in enumerate(self.claims):
            if excluded_specialists is None or claim.specialist_id not in excluded_specialists:
                return self.claims.pop(index)
        return None

    def heartbeat(self, claim):
        self.heartbeats += 1
        return claim

    def finish(self, claim, outcome):
        self.completed.append((claim.run.id, outcome.status))

    def retry(self, claim, *, error_code: str):
        self.retried.append(error_code)

    def interrupt(self, claim, *, error_code: str):
        self.interrupted.append(error_code)


def test_waiting_for_user_releases_worker_without_retry_or_busy_wait() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])

    async def execute(claim):
        return RunOutcome(
            disposition="suspended", run_id=claim.run.id, status="waiting_for_user",
            checkpoint_ref="checkpoint:1",
        )

    pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(global_concurrency=1))
    assert asyncio.run(pool.run_once("worker:1")) is True
    assert dispatcher.completed == [("child:1", "waiting_for_user")]
    assert dispatcher.retried == []


def test_retryable_failure_uses_bounded_retry_path() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])

    async def execute(_claim):
        raise TimeoutError("provider timed out")

    pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig())
    assert asyncio.run(pool.run_once("worker:1")) is True
    assert dispatcher.retried == ["worker_execution_timeout"]


def test_global_and_specialist_limits_bound_concurrent_execution() -> None:
    dispatcher = _Dispatcher([_Claim("child:1"), _Claim("child:2")])
    entered = 0
    peak = 0
    release = asyncio.Event()

    async def execute(claim):
        nonlocal entered, peak
        entered += 1
        peak = max(peak, entered)
        await release.wait()
        entered -= 1
        return RunOutcome(disposition="completed", run_id=claim.run.id, status="succeeded", output_ref=f"output:{claim.run.id}")

    async def scenario() -> None:
        pool = WorkerPool(
            dispatcher=dispatcher, execute=execute,
            config=WorkerPoolConfig(global_concurrency=2, specialist_concurrency={"job_web_researcher": 1}),
        )
        first = asyncio.create_task(pool.run_once("worker:1"))
        second = asyncio.create_task(pool.run_once("worker:2"))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert peak == 1


def test_child_runtime_executor_uses_context_builder_output_as_single_budget_authority() -> None:
    built = type("Built", (), {"spec": object(), "context": type("Context", (), {"cancellation_probe": None})()})()
    calls: list[tuple[object, object]] = []

    def assemble(claim):
        assert claim.run.id == "child:1"
        return built

    class Runtime:
        async def run(self, *, spec, context):
            calls.append((spec, context))
            return RunOutcome(disposition="completed", run_id="child:1", status="succeeded", output_ref="output:1")

    executor = ChildRuntimeExecutor(assemble=assemble, runtime=Runtime())
    outcome = asyncio.run(executor(_Claim("child:1")))

    assert outcome.status == "succeeded"
    assert calls == [(built.spec, built.context)]


def test_child_runtime_executor_routes_web_specialist_without_using_context_output_as_result() -> None:
    context = type("Context", (), {"cancellation_probe": None, "output_buffer": []})()
    built = type("Built", (), {"spec": object(), "context": context})()
    claim = _Claim("child:web")
    claim.task = type("Task", (), {"specialist_id": "job_web_researcher", "inputs_ref_json": {"query": "Sydney"}})()

    class WebAdapter:
        async def run(self, spec, context, inputs):
            assert inputs == {"query": "Sydney"}
            return type("Result", (), {
                "outcome": RunOutcome(disposition="completed", run_id="child:web", status="succeeded", output_ref="output:web"),
                "output": {"jobs": [{"title": "Agent Engineer"}], "visited": {"page_count": 1, "step_count": 4}},
            })()

    executor = ChildRuntimeExecutor(
        assemble=lambda _claim: built, runtime=object(), web_researcher=WebAdapter()
    )
    outcome = asyncio.run(executor(claim))

    assert outcome.status == "succeeded"
    assert context.output_buffer == []


def test_child_runtime_executor_routes_profile_specialist_without_using_context_output_as_result() -> None:
    context = type("Context", (), {"cancellation_probe": None, "output_buffer": []})()
    built = type("Built", (), {"spec": object(), "context": context})()
    claim = _Claim("child:profile", specialist_id="profile_evidence_analyst")
    claim.task = type("Task", (), {"specialist_id": "profile_evidence_analyst", "inputs_ref_json": {"knowledge_scope": {"type": "resume"}}})()

    class ProfileAdapter:
        async def run(self, spec, context, inputs, *, on_tool_artifact=None):
            assert inputs == {"knowledge_scope": {"type": "resume"}}
            assert callable(on_tool_artifact) or on_tool_artifact is None
            return type("Result", (), {
                "outcome": RunOutcome(disposition="completed", run_id="child:profile", status="succeeded", output_ref="output:profile"),
                "output": {"matches": [], "missing": ["missing evidence"], "conflicts": []},
            })()

    executor = ChildRuntimeExecutor(
        assemble=lambda _claim: built, runtime=object(), profile_evidence_analyst=ProfileAdapter()
    )
    outcome = asyncio.run(executor(claim))

    assert outcome.status == "succeeded"
    assert context.output_buffer == []


def test_persisted_profile_assembler_requires_explicit_bound_knowledge_references() -> None:
    from starter_agent.delegation.context import ContextBuildError

    claim = _Claim("child:profile", specialist_id="profile_evidence_analyst")
    claim.parent = type("Parent", (), {"id": "parent:1", "principal": "user:1"})()
    claim.task = type("Task", (), {
        "id": "task:profile", "parent_run_id": "parent:1", "specialist_id": "profile_evidence_analyst",
        "inputs_ref_json": {"knowledge_scope": {"type": "resume"}},
    })()
    class ProfileAdapter:
        async def run(self, spec, context, inputs):
            raise AssertionError("must not reach RAG")

    executor = ChildRuntimeExecutor(
        assemble=lambda _claim: (_ for _ in ()).throw(ContextBuildError("profile_knowledge_binding_unavailable", "missing binding")),
        runtime=object(), profile_evidence_analyst=ProfileAdapter(),
    )

    with pytest.raises(ContextBuildError, match="missing binding"):
        asyncio.run(executor(claim))


def test_long_execution_heartbeats_without_holding_database_transaction() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])
    release = asyncio.Event()

    async def execute(claim):
        await release.wait()
        return RunOutcome(disposition="completed", run_id=claim.run.id, status="succeeded", output_ref="output:1")

    async def scenario() -> None:
        pool = WorkerPool(
            dispatcher=dispatcher, execute=execute,
            config=WorkerPoolConfig(heartbeat_interval_seconds=0.001),
        )
        task = asyncio.create_task(pool.run_once("worker:1"))
        async def wait_for_heartbeat() -> None:
            while dispatcher.heartbeats == 0:
                await asyncio.sleep(0)
        await asyncio.wait_for(wait_for_heartbeat(), timeout=0.2)
        release.set()
        await task

    asyncio.run(scenario())
    assert dispatcher.heartbeats >= 1


def test_runtime_executor_injects_durable_cancellation_probe() -> None:
    context = type("Context", (), {"cancellation_probe": None})()
    built = type("Built", (), {"spec": object(), "context": context})()
    probes = 0

    def durable_probe(_claim):
        nonlocal probes
        probes += 1
        return (2, True)

    class Runtime:
        async def run(self, *, spec, context):
            assert context.cancellation_probe() == (2, True)
            return RunOutcome(disposition="cancelled", run_id="child:1", status="cancelled", error_code="run_cancelled")

    executor = ChildRuntimeExecutor(assemble=lambda _claim: built, runtime=Runtime(), cancellation_probe=durable_probe)
    outcome = asyncio.run(executor(_Claim("child:1")))

    assert outcome.status == "cancelled"
    assert probes == 1


def test_heartbeat_lease_loss_cancels_execution_and_rejects_finish() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])
    dispatcher.heartbeat = lambda _claim: (_ for _ in ()).throw(RuntimeError("lease lost"))
    cancelled = asyncio.Event()

    async def execute(_claim):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(heartbeat_interval_seconds=0.001))
        await asyncio.wait_for(pool.run_once("worker:1"), timeout=0.2)

    asyncio.run(scenario())
    assert cancelled.is_set()
    assert dispatcher.completed == []


def test_capacity_is_acquired_before_claim_so_global_limit_does_not_hoard_leases() -> None:
    dispatcher = _Dispatcher([_Claim(f"child:{index}") for index in range(10)])
    release = asyncio.Event()

    async def execute(claim):
        await release.wait()
        return RunOutcome(disposition="completed", run_id=claim.run.id, status="succeeded", output_ref="output:1")

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(global_concurrency=1))
        tasks = [asyncio.create_task(pool.run_once(f"worker:{index}")) for index in range(10)]
        await asyncio.sleep(0)
        assert len(dispatcher.claims) == 9
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_specialist_limit_does_not_hoard_leases() -> None:
    dispatcher = _Dispatcher([_Claim(f"child:{index}") for index in range(6)])
    release = asyncio.Event()

    async def execute(claim):
        await release.wait()
        return RunOutcome(disposition="completed", run_id=claim.run.id, status="succeeded", output_ref="output:1")

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(global_concurrency=4, specialist_concurrency={"job_web_researcher": 1}))
        tasks = [asyncio.create_task(pool.run_once(f"worker:{index}")) for index in range(6)]
        await asyncio.sleep(0)
        assert len(dispatcher.claims) == 5
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(scenario())


def test_unconfigured_specialist_is_not_starved_by_configured_limits() -> None:
    dispatcher = _Dispatcher([_Claim("child:profile", specialist_id="profile_evidence_analyst")])

    async def execute(claim):
        return RunOutcome(disposition="completed", run_id=claim.run.id, status="succeeded", output_ref="output:1")

    pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(specialist_concurrency={"job_web_researcher": 1}))
    assert asyncio.run(pool.run_once("worker:1")) is True
    assert dispatcher.completed == [("child:profile", "succeeded")]


def test_stop_cancels_current_execution_and_records_interruption() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])
    started = asyncio.Event()

    async def execute(_claim):
        started.set()
        await asyncio.Event().wait()

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig())
        task = asyncio.create_task(pool.run_once("worker:1"))
        await started.wait()
        pool.stop()
        await asyncio.wait_for(task, timeout=0.2)

    asyncio.run(scenario())
    assert dispatcher.interrupted == ["worker_interrupted"]


def test_stop_prevents_waiter_from_claiming_after_global_permit_is_released() -> None:
    dispatcher = _Dispatcher([_Claim("child:1"), _Claim("child:2")])
    started = asyncio.Event()

    async def execute(_claim):
        started.set()
        await asyncio.Event().wait()

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(global_concurrency=1))
        first = asyncio.create_task(pool.run_once("worker:1"))
        await started.wait()
        waiter = asyncio.create_task(pool.run_once("worker:2"))
        await asyncio.sleep(0)
        pool.stop()
        assert await asyncio.wait_for(first, timeout=0.2) is True
        assert await asyncio.wait_for(waiter, timeout=0.2) is False

    asyncio.run(scenario())
    assert [claim.run.id for claim in dispatcher.claims] == ["child:2"]


def test_stop_while_waiting_for_claim_lock_prevents_claim() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])

    async def execute(_claim):
        raise AssertionError("execution must not start after stop")

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig(global_concurrency=1))
        await pool._claim_lock.acquire()
        task = asyncio.create_task(pool.run_once("worker:1"))
        await asyncio.sleep(0)
        pool.stop()
        pool._claim_lock.release()
        assert await asyncio.wait_for(task, timeout=0.2) is False

    asyncio.run(scenario())
    assert [claim.run.id for claim in dispatcher.claims] == ["child:1"]


def test_execution_deadline_times_out_without_retry() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])
    dispatcher.claims[0].run.deadline_at = NOW + timedelta(milliseconds=1)
    timed_out: list[str] = []
    dispatcher.timeout = lambda claim, **_kwargs: timed_out.append(claim.run.id)

    async def execute(_claim):
        await asyncio.Event().wait()

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig())
        await asyncio.wait_for(pool.run_once("worker:1"), timeout=0.2)

    asyncio.run(scenario())
    assert timed_out == ["child:1"]
    assert dispatcher.retried == []


def test_cancelled_run_once_releases_lease_as_worker_interrupted() -> None:
    dispatcher = _Dispatcher([_Claim("child:1")])
    started = asyncio.Event()

    async def execute(_claim):
        started.set()
        await asyncio.Event().wait()

    async def scenario() -> None:
        pool = WorkerPool(dispatcher=dispatcher, execute=execute, config=WorkerPoolConfig())
        task = asyncio.create_task(pool.run_once("worker:1"))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert dispatcher.interrupted == ["worker_interrupted"]
