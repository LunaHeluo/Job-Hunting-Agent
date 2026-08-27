from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.delegation.context import RunContext, RunTraceContext
from starter_agent.delegation.models import BudgetLimits, RunSpec
from starter_agent.domain.models import Message, ModelResponse, ToolCall, ToolResult
from starter_agent.providers.base import Provider
from starter_agent.settings import RuntimeConfig
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry


class _EvidenceTool(Tool):
    name = "retrieve_resume_evidence"
    description = "fixture RAG"
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    risk_level = "read"

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, _arguments: dict, _context: ToolContext) -> ToolResult:
        self.calls += 1
        return ToolResult(
            ok=True,
            data={"evidence": [{
                "chunk_id": "chunk:authorized",
                "document_id": "document:resume",
                "source_ref": "resume.md#L1-L3",
                "quote": "Built governed retrieval systems.",
            }]},
        )


class _Provider(Provider):
    name = "profile-fixture"

    def __init__(self, final: dict) -> None:
        self.final = final
        self.requests: list[tuple[list[Message], list[dict]]] = []

    async def complete(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        **_kwargs,
    ) -> ModelResponse:
        self.requests.append((list(messages), list(tools)))
        if len(self.requests) == 1:
            return ModelResponse(
                tool_calls=[ToolCall(
                    id="rag:1", name="retrieve_resume_evidence",
                    arguments={"query": "governed retrieval", "top_k": 1},
                )],
                provider=self.name,
                model=model,
                usage={"total_tokens": 1, "cost_microunits": 1},
            )
        return ModelResponse(
            content=json.dumps(self.final), provider=self.name, model=model,
            usage={"total_tokens": 1, "cost_microunits": 1},
        )

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def _context() -> RunContext:
    return RunContext(
        run_id="child:profile:1", parent_run_id="parent:1", child_task_id="task:profile:1",
        session_id=uuid4(), turn_id=uuid4(), principal="user:1",
        messages=[Message(role="system", content="evidence only"), Message(role="user", content="refs only")],
        effective_tool_view=["retrieve_resume_evidence"],
        budget_limits=BudgetLimits(tokens=1000, cost_microunits=1000, wall_clock_ms=30_000, model_calls=3, tool_calls=3),
        trace_context=RunTraceContext(parent_run_id="parent:1", child_task_id="task:profile:1", child_run_id="child:profile:1"),
        user_id="user:1", project_id="project:1", knowledge_base_id=uuid4(), knowledge_scope="resume",
    )


def _spec() -> RunSpec:
    return RunSpec(
        run_id="child:profile:1", run_kind="child", role="specialist",
        provider="profile-fixture", model="fixture", system_prompt_ref="prompt:profile:v1",
        output_schema_ref="profile-evidence-output-v1", allowed_tools=("retrieve_resume_evidence",),
        max_steps=3, runtime_revision="runtime:v1",
    )


def _runtime(final: dict) -> tuple[AgentRuntime, _Provider, _EvidenceTool]:
    provider, tool = _Provider(final), _EvidenceTool()
    registry = ToolRegistry([])
    registry._tools = {tool.name: tool}
    runtime = AgentRuntime(
        registry, ToolPolicy(["read"]),
        RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=10),
        provider_resolver=lambda _name: provider,
    )
    from starter_agent.capabilities.models import PolicyRule

    capability = runtime.gate.registry.resolve_execution(tool.name)
    runtime.gate.store.create_policy_rule(PolicyRule(
        id="profile-rag-allow", server_id="builtin", tool_name=tool.name,
        effect="allowlist_auto", actions=("read",), schema_hash=capability.schema_hash,
        created_by="test",
    ))
    return runtime, provider, tool


def _inputs(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "normalized_job_requirements_ref": "artifact:web:accepted",
        "depends_on_task_id": "task:web:1",
        "knowledge_scope": {"type": "resume"},
        "candidate_chunk_ids": ["chunk:authorized"],
        "top_k": 1,
        "output_schema_version": "profile-evidence-output-v1",
    }
    value.update(updates)
    return value


@pytest.mark.asyncio
async def test_profile_provider_gets_only_authorized_rag_schema_and_reference_inputs() -> None:
    from starter_agent.delegation.specialists.profile_evidence_analyst import ProfileEvidenceAnalyst

    final = {"matches": [{
        "requirement_ref": "req:rag", "match_status": "matched", "evidence_strength": "strong",
        "evidence": [{"chunk_id": "chunk:authorized", "source_ref": "resume.md#L1-L3"}],
    }], "missing": [], "conflicts": []}
    runtime, provider, tool = _runtime(final)
    context = _context()
    context.messages[-1] = Message(
        role="user",
        content=json.dumps({"inputs": _inputs(), "context_refs": ["artifact:web:accepted"]}),
    )

    result = await ProfileEvidenceAnalyst(runtime).run(_spec(), context, _inputs())

    assert result.outcome.status == "succeeded"
    assert [{item["function"]["name"] for item in tools} for _messages, tools in provider.requests] == [{"retrieve_resume_evidence"}] * 2
    sent = "\n".join(message.content for messages, _tools in provider.requests for message in messages)
    assert "artifact:web:accepted" in sent
    assert "raw web" not in sent
    assert tool.calls == 1
    assert result.output["matches"][0]["evidence"][0]["chunk_id"] == "chunk:authorized"


@pytest.mark.asyncio
async def test_profile_rejects_positive_match_without_authorized_retrieved_chunk() -> None:
    from starter_agent.delegation.specialists.profile_evidence_analyst import ProfileEvidenceAnalyst

    final = {"matches": [{
        "requirement_ref": "req:rag", "match_status": "matched", "evidence_strength": "strong",
        "evidence": [{"chunk_id": "chunk:forged", "source_ref": "resume.md#L1-L3"}],
    }], "missing": [], "conflicts": []}
    runtime, _provider, tool = _runtime(final)

    result = await ProfileEvidenceAnalyst(runtime).run(_spec(), _context(), _inputs())

    assert result.outcome.status == "failed"
    assert result.outcome.error_code == "profile_evidence_unbound"
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_profile_allows_a_retrieved_scope_authorized_chunk_when_candidate_refs_are_omitted() -> None:
    from starter_agent.delegation.specialists.profile_evidence_analyst import ProfileEvidenceAnalyst

    final = {"matches": [{
        "requirement_ref": "req:rag", "match_status": "matched", "evidence_strength": "strong",
        "evidence": [{"chunk_id": "chunk:authorized", "source_ref": "resume.md#L1-L3"}],
    }], "missing": [], "conflicts": []}
    runtime, _provider, tool = _runtime(final)
    inputs = _inputs()
    inputs.pop("candidate_chunk_ids")

    result = await ProfileEvidenceAnalyst(runtime).run(_spec(), _context(), inputs)

    assert result.outcome.status == "succeeded"
    assert tool.calls == 1
