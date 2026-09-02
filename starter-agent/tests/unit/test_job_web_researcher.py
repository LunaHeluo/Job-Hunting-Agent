from __future__ import annotations

import json
import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.agent.runtime import _structured_job_from_snapshot_result
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, RunOutcome, RunSpec
from starter_agent.delegation.specialists.job_web_researcher import (
    JobWebResearcher,
    WebResearchLimits,
    _Progress,
    _parse_model_output,
)
from starter_agent.domain.models import Message, ModelResponse, ToolCall, ToolResult
from starter_agent.providers.base import Provider
from starter_agent.settings import RuntimeConfig
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry


FIXTURE = Path(__file__).parents[1] / "fixtures/job_web_researcher/multi_page.json"
TOOLS = (
    "search_jobs_serpapi",
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_wait_for",
    "mcp__playwright__browser_snapshot",
    "mcp__playwright__browser_click",
)


def test_runtime_projects_mcp_snapshot_wrapper_to_bound_structured_job() -> None:
    projected = _structured_job_from_snapshot_result(
        {
            "content": [{"type": "text", "text": "raw snapshot"}],
            "structured_content": {
                "title": "Agent Engineer",
                "source_url": "https://jobs.example.test/agent",
                "final_url": "https://jobs.example.test/agent",
                "raw_text": "must stay in the Child artifact",
            },
        },
        {"source_content_sha256": "a" * 64},
        "artifact:snapshot:1",
    )

    assert projected is not None
    assert projected["title"] == "Agent Engineer"
    assert projected["content_hash"] == "a" * 64
    assert projected["artifact_refs"] == ["artifact:snapshot:1"]
    assert "content" not in projected
    assert "raw_text" not in projected


def test_model_output_accepts_json_fence_but_keeps_strict_schema() -> None:
    parsed = _parse_model_output(
        '```json\n{"jobs":[],"missing":[],"errors":[]}\n```'
    )
    assert parsed.jobs == []

    with pytest.raises(Exception):
        _parse_model_output(
            '```json\n{"jobs":[],"missing":[],"errors":[],"extra":true}\n```'
        )


class _FixtureTool(Tool):
    description = "Scripted public job fixture"
    input_schema = {"type": "object", "additionalProperties": True}
    risk_level = "read"

    def __init__(self, name: str, calls: list[tuple[str, dict]], state: dict[str, str]) -> None:
        self.name = name
        self.calls = calls
        self.state = state

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        self.calls.append((self.name, arguments))
        url = str(arguments.get("url") or "")
        if self.name == "mcp__playwright__browser_navigate":
            self.state["current_url"] = url.split("?")[0]
        final_url = self.state.get("current_url", "") or url.split("?")[0]
        content_hash = "b" * 64 if final_url.endswith(("agent-2", "/two")) else "a" * 64
        data = {"observed": self.name, "url": url, "artifact_ref": f"artifact:{'two' if content_hash[0] == 'b' else 'one'}"}
        if self.name == "mcp__playwright__browser_snapshot" and self.state.get("snapshot_html"):
            data["html"] = self.state["snapshot_html"]
        if self.name == "search_jobs_serpapi":
            data["results"] = [
                {"url": "https://jobs.example.test/roles/agent-1"},
                {"url": "https://jobs.example.test/roles/agent-2"},
            ]
        return ToolResult(
            ok=True,
            data=data,
            metadata={
                "requested_url": url or None,
                "final_url": final_url or None,
                "source_url": final_url or None,
                "source_content_sha256": content_hash,
                "artifact_ref": f"artifact:{'two' if content_hash[0] == 'b' else 'one'}",
            },
        )


class _ScriptedProvider(Provider):
    name = "scripted-web"

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[tuple[list[Message], list[dict]]] = []

    async def complete(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        **_kwargs,
    ) -> ModelResponse:
        self.calls.append((list(messages), tools))
        step = self.script.pop(0)
        if "final" in step:
            return ModelResponse(
                content=json.dumps(step["final"]), provider=self.name, model=model,
                usage={"total_tokens": 1, "cost_microunits": 1},
            )
        return ModelResponse(
            tool_calls=[ToolCall(id=f"call-{len(self.calls)}", name=step["tool"], arguments=step["arguments"])],
            provider=self.name, model=model,
            usage={"total_tokens": 1, "cost_microunits": 1},
        )

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def _runtime(script: list[dict], *, snapshot_html: str | None = None):
    calls: list[tuple[str, dict]] = []
    provider = _ScriptedProvider(script)
    registry = ToolRegistry([])
    state: dict[str, str] = {} if snapshot_html is None else {"snapshot_html": snapshot_html}
    fixture_tools = {name: _FixtureTool(name, calls, state) for name in TOOLS}
    registry._tools = fixture_tools
    runtime = AgentRuntime(
        registry, ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=20, max_tool_calls=20, max_seconds=60, tool_timeout_seconds=60),
        provider_resolver=lambda name: provider if name == provider.name else None,
    )
    # Fixture-only policy provisioning: production Browser calls still use the
    # same Pre-Tool-Call Gate and must have an explicit current-schema rule.
    from starter_agent.capabilities.models import PolicyRule
    for name in TOOLS:
        capability = runtime.gate.registry.resolve_execution(name)
        runtime.gate.store.create_policy_rule(PolicyRule(
            id=f"fixture-{name}", server_id="builtin", tool_name=name,
            effect="allowlist_auto", actions=("read",), schema_hash=capability.schema_hash,
            created_by="test",
        ))
    return runtime, provider, calls


def _context(*, pages: int = 10, steps: int = 30) -> RunContext:
    return RunContext(
        run_id="child:web:1", parent_run_id="parent:1", child_task_id="task:web:1",
        session_id=uuid4(), turn_id=uuid4(), principal="user:1",
        messages=[Message(role="user", content="research Sydney Agent Engineer roles")],
        effective_tool_view=list(TOOLS),
        budget_limits=BudgetLimits(tokens=10000, cost_microunits=10000, wall_clock_ms=120000, model_calls=steps, tool_calls=60),
        trace_context=RunTraceContext(parent_run_id="parent:1", child_task_id="task:web:1", child_run_id="child:web:1"),
        working_memory={"policy_max_pages": pages, "policy_max_steps": steps, "policy_per_page_timeout_seconds": 35},
    )


def _spec(*, steps: int = 30) -> RunSpec:
    return RunSpec(
        run_id="child:web:1", run_kind="child", role="specialist",
        provider="scripted-web", model="fixture", system_prompt_ref="prompt:web:v1",
        output_schema_ref="job-web-output-v1", allowed_tools=TOOLS,
        max_steps=steps, runtime_revision="runtime-v1",
    )


def _inputs(**updates):
    base = {
        "query": "Agent Engineer Sydney", "target_fields": ["title", "company", "location", "responsibilities", "requirements"],
        "target_valid_jobs": 2, "max_pages": 10, "max_steps": 30,
        "per_page_timeout_seconds": 35, "stop_conditions": {},
        "output_schema_version": "job-web-output-v1",
    }
    base.update(updates)
    return base


@pytest.mark.asyncio
async def test_scripted_multi_page_research_uses_shared_runtime_loop_and_deduplicates_jobs() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    runtime, provider, calls = _runtime(fixture["responses"])
    parent_messages = [Message(role="user", content="parent secret chat")]
    context = _context()

    result = await JobWebResearcher(runtime).run(_spec(), context, _inputs())

    assert result.outcome.status == "succeeded"
    assert len(provider.calls) == 10  # real repeated Model -> Tool -> Observation
    assert len(calls) == 9
    assert [job["final_url"] for job in result.output["jobs"]] == [
        "https://jobs.example.test/roles/agent-1",
        "https://jobs.example.test/roles/agent-2",
    ]
    assert result.output["visited"]["page_count"] == 2
    assert result.output["visited"]["step_count"] == 9
    assert result.output["visited"]["stop_reason"] == "target_reached"
    assert len(result.output["visited"]["attempts"]) == 2
    assert parent_messages == [Message(role="user", content="parent secret chat")]
    assert all("parent secret chat" not in message.content for message in context.messages)
    assert not (Path(__file__).parents[2] / "backend/src/starter_agent/agent/subagent_loop.py").exists()


@pytest.mark.asyncio
async def test_browser_snapshot_html_is_never_injected_into_child_model_messages() -> None:
    snapshot_html = (
        "<html><nav>navigation secret</nav><script>window.cookie='TOP-SECRET'</script>"
        "<main><h1>Agent Engineer</h1><p>Python required</p></main></html>"
    )
    final = {
        "jobs": [{
            "title": "Agent Engineer", "company": "Example", "location": "Sydney",
            "responsibilities": ["Build agents"], "requirements": ["Python"],
            "source_url": "https://jobs.example.test/one", "final_url": "https://jobs.example.test/one",
            "retrieved_at": "2026-08-12T00:00:00Z", "validation_state": "verified",
            "content_hash": "a" * 64, "artifact_refs": ["artifact:one"],
        }], "missing": [], "errors": [],
    }
    runtime, provider, _calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one"}},
        {"tool": "mcp__playwright__browser_wait_for", "arguments": {"time": 1}},
        {"tool": "mcp__playwright__browser_snapshot", "arguments": {}},
        {"final": final},
    ], snapshot_html=snapshot_html)

    result = await JobWebResearcher(runtime).run(
        _spec(), _context(), _inputs(urls=["https://jobs.example.test/one"], target_valid_jobs=1)
    )

    assert result.outcome.status == "succeeded"
    observed = "\n".join(message.content for messages, _tools in provider.calls for message in messages)
    assert "TOP-SECRET" not in observed
    assert "navigation secret" not in observed
    assert "<html" not in observed
    assert "Agent Engineer" in observed


def test_limits_take_contract_registry_policy_parent_minimum_and_hard_caps() -> None:
    limits = WebResearchLimits.resolve(
        contract=_inputs(max_pages=99, max_steps=99, per_page_timeout_seconds=99),
        registry={"max_pages": 8, "max_steps": 28, "per_page_timeout_seconds": 40},
        policy={"max_pages": 6, "max_steps": 24, "per_page_timeout_seconds": 20},
        parent={"max_pages": 7, "max_steps": 21, "per_page_timeout_seconds": 30},
    )

    assert limits.max_pages == 6
    assert limits.max_steps == 21
    assert limits.per_page_timeout_seconds == 20
    assert WebResearchLimits.resolve(contract=_inputs()).max_pages <= 10
    assert WebResearchLimits.resolve(contract=_inputs()).max_steps <= 30
    assert WebResearchLimits.resolve(contract=_inputs()).per_page_timeout_seconds <= 35


@pytest.mark.asyncio
async def test_search_timeout_with_seed_url_falls_back_to_open_instead_of_browser_wait() -> None:
    seed = "https://jobs.example.test/one"
    progress = _Progress(
        WebResearchLimits(max_pages=3, max_steps=10, per_page_timeout_seconds=35),
        urls=[seed],
        require_search=True,
    )

    await progress.tool_event({
        "type": "tool_completed",
        "name": "search_jobs_serpapi",
        "ok": False,
        "error_code": "tool_timeout",
    })

    assert progress.phase == "open"
    assert progress.forced_tool is None
    assert progress.attempts[-1]["recovery_action"] == "fallback_seed_candidates"
    assert progress.preflight(
        ToolCall(id="open-seed", name="mcp__playwright__browser_navigate", arguments={"url": seed}),
        _context(),
    ) is None


@pytest.mark.asyncio
async def test_search_timeout_without_seed_retries_search_not_browser_wait() -> None:
    progress = _Progress(
        WebResearchLimits(max_pages=3, max_steps=10, per_page_timeout_seconds=35),
        require_search=True,
    )

    await progress.tool_event({
        "type": "tool_completed",
        "name": "search_jobs_serpapi",
        "ok": False,
        "error_code": "tool_timeout",
    })

    assert progress.phase == "search"
    assert progress.forced_tool == "search_jobs_serpapi"
    assert progress.attempts[-1]["recovery_action"] == "retry"
    assert progress.preflight(
        ToolCall(id="retry-search", name="search_jobs_serpapi", arguments={"query": "Agent Engineer"}),
        _context(),
    ) is None


@pytest.mark.asyncio
async def test_navigation_timeout_retries_navigation_with_original_url() -> None:
    seed = "https://jobs.example.test/one"
    progress = _Progress(
        WebResearchLimits(max_pages=3, max_steps=10, per_page_timeout_seconds=35),
        urls=[seed],
    )

    await progress.tool_event({
        "type": "tool_completed",
        "name": "mcp__playwright__browser_navigate",
        "ok": False,
        "error_code": "tool_timeout",
        "requested_url": seed,
    })

    assert progress.phase == "open"
    assert progress.forced_tool == "mcp__playwright__browser_navigate"
    assert progress.forced_url == seed
    assert progress.attempts[-1]["recovery_action"] == "retry"
    assert progress.preflight(
        ToolCall(id="retry-open", name="mcp__playwright__browser_navigate", arguments={"url": seed}),
        _context(),
    ) is None


@pytest.mark.asyncio
async def test_wrong_recovery_call_is_skipped_without_clearing_forced_retry() -> None:
    seed = "https://jobs.example.test/one"
    other = "https://jobs.example.test/two"
    progress = _Progress(
        WebResearchLimits(max_pages=3, max_steps=10, per_page_timeout_seconds=35),
        urls=[seed, other],
    )
    await progress.tool_event({
        "type": "tool_completed",
        "name": "mcp__playwright__browser_navigate",
        "ok": False,
        "error_code": "page_load_failed",
        "requested_url": seed,
    })

    assert progress.preflight(
        ToolCall(
            id="wrong-open",
            name="mcp__playwright__browser_navigate",
            arguments={"url": other},
        ),
        _context(),
    ) == (
        "skip",
        "recovery_action_required",
        {
            "required_tool": "mcp__playwright__browser_navigate",
            "required_arguments": {"url": seed},
        },
    )
    assert progress.forced_tool == "mcp__playwright__browser_navigate"
    assert progress.forced_url == seed
    assert progress.preflight(
        ToolCall(
            id="retry-open",
            name="mcp__playwright__browser_navigate",
            arguments={"url": seed},
        ),
        _context(),
    ) is None


@pytest.mark.asyncio
async def test_preflight_corrections_are_bounded() -> None:
    progress = _Progress(
        WebResearchLimits(max_pages=3, max_steps=10, per_page_timeout_seconds=35),
        urls=["https://jobs.example.test/one"],
    )
    wrong = ToolCall(id="wrong", name="mcp__playwright__browser_snapshot", arguments={})

    first = progress.preflight(wrong, _context())
    second = progress.preflight(wrong, _context())
    third = progress.preflight(wrong, _context())

    assert first == (
        "skip",
        "invalid_web_transition",
        {
            "current_phase": "open",
            "required_tools": ["mcp__playwright__browser_navigate"],
            "required_tool": "mcp__playwright__browser_navigate",
            "required_arguments": {"url": "https://jobs.example.test/one"},
        },
    )
    assert second[0] == "skip"
    assert third[0:2] == ("stop", "web_transition_recovery_exhausted")


def test_output_reuses_existing_candidate_ranking_and_jd_completeness_rules() -> None:
    from starter_agent.delegation.specialists.job_web_researcher import normalize_jobs

    jobs = [
        {"title": "Agent jobs in Sydney", "company": "Example", "location": "Sydney", "responsibilities": ["Build"], "requirements": ["Python"], "source_url": "https://jobs.example.test/search", "final_url": "https://jobs.example.test/search", "retrieved_at": "2026-08-12T00:00:00Z", "validation_state": "verified", "content_hash": "d" * 64, "artifact_refs": ["artifact:list"]},
        {"title": "Agent Engineer", "company": "Example", "location": "Sydney", "responsibilities": ["Build"], "requirements": [], "source_url": "https://jobs.example.test/jobs/agent-engineer-123", "final_url": "https://jobs.example.test/jobs/agent-engineer-123", "retrieved_at": "2026-08-12T00:00:00Z", "validation_state": "verified", "content_hash": "e" * 64, "artifact_refs": ["artifact:detail"]},
    ]

    normalized, missing = normalize_jobs(jobs)

    assert [job["title"] for job in normalized] == ["Agent Engineer"]
    assert normalized[0]["validation_state"] == "partial_verified"
    assert missing == [{"source_url": normalized[0]["source_url"], "fields": ["requirements"]}]


@pytest.mark.asyncio
async def test_page_limit_stops_before_opening_another_page() -> None:
    script = [
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one"}},
        {"tool": "mcp__playwright__browser_wait_for", "arguments": {"time": 1}},
        {"tool": "mcp__playwright__browser_snapshot", "arguments": {}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/two"}},
    ]
    runtime, _provider, calls = _runtime(script)

    context = _context(pages=1, steps=5)
    result = await JobWebResearcher(runtime).run(
        _spec(steps=5), context, _inputs(urls=["https://jobs.example.test/one", "https://jobs.example.test/two"], max_pages=2, max_steps=5)
    )

    assert result.outcome.status == "partial"
    assert result.output["visited"]["page_count"] == 1
    assert result.output["visited"]["stop_reason"] == "page_limit"
    assert result.output["missing"] == [{"reason": "page_limit"}]
    assert result.output["errors"] == [{"code": "page_limit"}]
    assert [name for name, _ in calls] == [
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_wait_for",
        "mcp__playwright__browser_snapshot",
    ]
    assert context.messages[-1].role == "tool"
    assert context.messages[-1].tool_call_id == context.messages[-2].tool_calls[0].id


@pytest.mark.asyncio
async def test_cancelled_child_stops_without_calling_model_or_tools() -> None:
    runtime, provider, calls = _runtime([])
    context = _context()
    context.cancellation.requested = True

    result = await JobWebResearcher(runtime).run(_spec(), context, _inputs())

    assert result.outcome.status == "cancelled"
    assert result.output["visited"]["stop_reason"] == "cancelled"
    assert provider.calls == []
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_page_is_skipped_without_tool_side_effect_then_next_candidate_continues() -> None:
    final = {
        "jobs": [{
            "title": "Agent Engineer", "company": "Example", "location": "Sydney",
            "responsibilities": ["Build agents"], "requirements": ["Python"],
            "source_url": "https://jobs.example.test/two", "final_url": "https://jobs.example.test/two",
            "retrieved_at": "2026-08-12T00:00:00Z", "validation_state": "verified",
            "content_hash": "b" * 64, "artifact_refs": ["artifact:two"],
        }], "missing": [], "errors": [],
    }
    script = [
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one"}},
        {"tool": "mcp__playwright__browser_wait_for", "arguments": {"time": 1}},
        {"tool": "mcp__playwright__browser_snapshot", "arguments": {}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one#tracking"}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/two"}},
        {"tool": "mcp__playwright__browser_wait_for", "arguments": {"time": 1}},
        {"tool": "mcp__playwright__browser_snapshot", "arguments": {}},
        {"final": final},
    ]
    runtime, provider, calls = _runtime(script)

    result = await JobWebResearcher(runtime).run(_spec(steps=10), _context(steps=10), _inputs(urls=["https://jobs.example.test/one", "https://jobs.example.test/two"], target_valid_jobs=1, max_steps=10))

    navigated = [args["url"] for name, args in calls if name == "mcp__playwright__browser_navigate"]
    assert navigated == ["https://jobs.example.test/one", "https://jobs.example.test/two"]
    assert len(provider.calls) == 8
    assert result.output["visited"]["page_count"] == 2
    assert any(item["status"] == "duplicate" for item in result.output["visited"]["attempts"])
    assert result.outcome.status == "succeeded"


@pytest.mark.asyncio
async def test_web_policy_rejects_multiple_tool_calls_before_any_tool_side_effect() -> None:
    runtime, _provider, calls = _runtime([])

    class BatchProvider(_ScriptedProvider):
        async def complete(self, messages, model, tools, **kwargs):
            return ModelResponse(
                tool_calls=[
                    ToolCall(id="batch-1", name="search_jobs_serpapi", arguments={"query": "Sydney"}),
                    ToolCall(id="batch-2", name="mcp__playwright__browser_navigate", arguments={"url": "https://jobs.example.test/one"}),
                ], provider=self.name, model=model,
                usage={"total_tokens": 1, "cost_microunits": 1},
            )

    provider = BatchProvider([])
    runtime.provider_resolver = lambda _name: provider
    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs())

    assert result.outcome.status == "partial"
    assert result.output["visited"]["stop_reason"] == "tool_batch_limit"
    assert calls == []


@pytest.mark.asyncio
async def test_state_machine_rejects_navigate_before_required_search_with_zero_side_effect() -> None:
    runtime, _provider, calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one"}}
    ])

    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs())

    assert result.outcome.status == "partial"
    assert result.output["visited"]["stop_reason"] == "invalid_web_transition"
    assert result.output["missing"] == [{"reason": "invalid_web_transition"}]
    assert result.output["errors"] == [{"code": "invalid_web_transition"}]
    assert calls == []


@pytest.mark.asyncio
async def test_seed_url_can_still_require_search_before_browser_side_effect() -> None:
    runtime, _provider, calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one"}}
    ])

    result = await JobWebResearcher(runtime).run(
        _spec(),
        _context(),
        _inputs(urls=["https://jobs.example.test/one"], require_search=True),
    )

    assert result.output["visited"]["stop_reason"] == "invalid_web_transition"
    assert calls == []


@pytest.mark.asyncio
async def test_out_of_order_page_read_is_skipped_then_model_can_correct() -> None:
    runtime, provider, calls = _runtime([
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://jobs.example.test/one"}},
        {"tool": "mcp__playwright__browser_snapshot", "arguments": {}},
        {"tool": "mcp__playwright__browser_wait_for", "arguments": {"time": 1}},
        {"final": {"jobs": [], "missing": [], "errors": []}},
    ])

    result = await JobWebResearcher(runtime).run(
        _spec(), _context(), _inputs(urls=["https://jobs.example.test/one"])
    )

    assert [name for name, _arguments in calls] == [
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_wait_for",
    ]
    assert len(provider.calls) == 4
    assert result.outcome.status == "failed"


@pytest.mark.asyncio
async def test_expired_deadline_stops_before_model_and_tool_side_effects() -> None:
    from datetime import UTC, datetime, timedelta

    runtime, provider, calls = _runtime([])
    context = _context()
    context.deadline_at = datetime.now(UTC) - timedelta(seconds=1)

    result = await JobWebResearcher(runtime).run(_spec(), context, _inputs())

    assert result.outcome.status == "timed_out"
    assert result.output["visited"]["stop_reason"] == "deadline_exhausted"
    assert provider.calls == []
    assert calls == []


@pytest.mark.asyncio
async def test_budget_exhaustion_returns_partial_envelope_payload() -> None:
    class ExhaustedRuntime:
        async def run(self, **_kwargs):
            return RunOutcome(
                disposition="failed",
                run_id="child:web:1",
                status="budget_exhausted",
                error_code="runtime_budget_exceeded",
            )

    result = await JobWebResearcher(ExhaustedRuntime()).run(
        _spec(), _context(), _inputs()
    )

    assert result.outcome.status == "partial"
    assert result.output["visited"]["stop_reason"] == "budget_exhausted"
    assert result.output["missing"] == [{"reason": "budget_exhausted"}]
    assert result.output["errors"] == [{"code": "runtime_budget_exceeded"}]


@pytest.mark.asyncio
async def test_unvisited_or_hash_unbound_job_is_rejected_from_output() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["responses"][-1]["final"]["jobs"][0]["content_hash"] = "f" * 64
    runtime, _provider, _calls = _runtime(fixture["responses"])

    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs())

    assert all(job["content_hash"] != "f" * 64 for job in result.output["jobs"])
    assert any(error["code"] == "job_evidence_unbound" for error in result.output["errors"])


@pytest.mark.asyncio
async def test_invalid_final_schema_returns_specific_partial_failure() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["responses"][-1]["final"]["unexpected"] = True
    runtime, _provider, _calls = _runtime(fixture["responses"])

    result = await JobWebResearcher(runtime).run(
        _spec(), _context(), _inputs()
    )

    assert result.outcome.status == "partial"
    error = result.output["errors"][0]
    assert error["code"] == "job_web_output_schema_invalid"
    assert error["failures"][0]["path"] == "unexpected"
    assert error["failures"][0]["type"] == "extra_forbidden"


@pytest.mark.asyncio
async def test_forged_source_url_is_rejected_even_when_final_hash_and_artifact_are_real() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["responses"][-1]["final"]["jobs"][0]["source_url"] = "https://forged.example.test/job"
    runtime, _provider, _calls = _runtime(fixture["responses"])

    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs())

    assert all(job["source_url"] != "https://forged.example.test/job" for job in result.output["jobs"])
    assert any(error["code"] == "job_source_url_mismatch" for error in result.output["errors"])


def test_shared_jd_validator_rejects_job_when_both_required_sections_are_empty() -> None:
    from starter_agent.delegation.specialists.job_web_researcher import normalize_jobs

    jobs, _missing = normalize_jobs([{
        "title": "Agent Engineer", "company": "Example", "location": "Sydney",
        "responsibilities": [], "requirements": [],
        "source_url": "https://jobs.example.test/jobs/empty",
        "final_url": "https://jobs.example.test/jobs/empty",
        "retrieved_at": "2026-08-12T00:00:00Z", "validation_state": "verified",
        "content_hash": "a" * 64, "artifact_refs": ["artifact:empty"],
    }])

    assert jobs == []


@pytest.mark.asyncio
async def test_search_result_candidates_reject_arbitrary_navigation_without_browser_side_effect() -> None:
    runtime, _provider, calls = _runtime([
        {"tool": "search_jobs_serpapi", "arguments": {"query": "Sydney Agent Engineer"}},
        {"tool": "mcp__playwright__browser_navigate", "arguments": {"url": "https://evil.example.test/job"}},
    ])

    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs())

    assert result.output["visited"]["stop_reason"] == "candidate_url_not_allowed"
    assert [name for name, _args in calls] == ["search_jobs_serpapi"]


@pytest.mark.asyncio
async def test_deadline_bounds_in_flight_provider_wait() -> None:
    runtime, _provider, calls = _runtime([])

    class SlowProvider(_ScriptedProvider):
        async def complete(self, messages, model, tools, **kwargs):
            await asyncio.sleep(0.2)
            return ModelResponse(content="{}", provider=self.name, model=model)

    provider = SlowProvider([])
    runtime.provider_resolver = lambda _name: provider
    context = _context()
    context.deadline_at = datetime.now(UTC) + timedelta(milliseconds=20)

    result = await JobWebResearcher(runtime).run(_spec(), context, _inputs())

    assert result.outcome.status == "timed_out"
    assert calls == []


@pytest.mark.asyncio
async def test_final_response_before_completeness_is_rejected() -> None:
    runtime, _provider, calls = _runtime([
        {"tool": "search_jobs_serpapi", "arguments": {"query": "Sydney Agent Engineer"}},
        {"final": {"jobs": [], "missing": [], "errors": []}},
    ])

    result = await JobWebResearcher(runtime).run(_spec(), _context(), _inputs())

    assert result.outcome.status == "failed"
    assert result.output["visited"]["stop_reason"] == "final_before_completeness"
    assert [name for name, _args in calls] == ["search_jobs_serpapi"]


@pytest.mark.asyncio
async def test_deadline_is_rechecked_before_tool_preflight_and_gate() -> None:
    runtime, _provider, calls = _runtime([])
    context = _context()

    class ExpiringProvider(_ScriptedProvider):
        async def complete(self, messages, model, tools, **kwargs):
            context.deadline_at = datetime.now(UTC) - timedelta(milliseconds=1)
            return ModelResponse(
                tool_calls=[ToolCall(id="expired-tool", name="search_jobs_serpapi", arguments={"query": "Sydney"})],
                provider=self.name, model=model,
            )

    provider = ExpiringProvider([])
    runtime.provider_resolver = lambda _name: provider

    result = await JobWebResearcher(runtime).run(_spec(), context, _inputs())

    assert result.outcome.status == "timed_out"
    assert calls == []
