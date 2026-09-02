from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from starter_agent.agent.runtime import AgentRuntime
from starter_agent.capabilities.models import PolicyRule
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.delegation.context import ChildContextBuilder, RuntimeContextAuthority
from starter_agent.delegation.models import BudgetLimits, RunSpec, TaskContract
from starter_agent.delegation.profile_knowledge import ProfileKnowledgeBindings
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.specialists.profile_evidence_analyst import ProfileEvidenceAnalyst
from starter_agent.domain.models import ModelResponse, ToolCall
from starter_agent.knowledge.service import KnowledgeApplicationService
from starter_agent.knowledge.store import SQLiteKnowledgeStore
from starter_agent.providers.base import Provider
from starter_agent.settings import RuntimeConfig, load_settings
from starter_agent.tools.builtin.knowledge import RetrieveResumeEvidenceTool
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry


class _Provider(Provider):
    name = "profile-binding-fixture"

    def __init__(self) -> None:
        self.chunk_id = ""
        self.calls = 0

    async def complete(self, messages, model, tools, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=[ToolCall(id="rag:1", name="retrieve_resume_evidence", arguments={"query": "governed retrieval", "top_k": 1})],
                provider=self.name, model=model, usage={"total_tokens": 1, "cost_microunits": 1},
            )
        return ModelResponse(
            content=json.dumps({"matches": [{"requirement_ref": "req:rag", "match_status": "matched", "evidence_strength": "strong", "evidence": [{"chunk_id": self.chunk_id, "source_ref": "resume.md@v1#L1-L1"}]}], "missing": [], "conflicts": []}),
            provider=self.name, model=model, usage={"total_tokens": 1, "cost_microunits": 1},
        )

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


@pytest.mark.asyncio
async def test_profile_binding_builds_real_rag_context_for_parent_principal_and_authorized_chunk() -> None:
    root = Path(__file__).parents[2]
    settings = load_settings("config/config.example.yaml")
    settings.project_root = root
    settings.app.database_url = "sqlite+pysqlite:///:memory:"
    knowledge = KnowledgeApplicationService(settings, SQLiteKnowledgeStore(settings.app.database_url, root))
    uploaded = knowledge.upload(
        knowledge_base_id=knowledge.default_knowledge_base_id,
        filename="resume.md", content=b"Built governed retrieval systems.",
        document_type="resume", confirmed_authorized=True,
    )
    chunk = knowledge.store.list_chunks(
        knowledge.scope, knowledge.default_knowledge_base_id, uploaded.document.id,
        after_ordinal=-1, limit=1,
    )[0]
    registry = SpecialistRegistry(root / "config/specialists", project_root=root, dependency_resolver=lambda _dependency: True)
    registry.reload()
    definition = registry.resolve("profile_evidence_analyst")
    now = datetime.now(UTC)
    contract = TaskContract(
        task_id="task:profile", parent_run_id="parent:profile", specialist_id=definition.specialist_id,
        goal="Match only resume evidence", inputs={
            "normalized_job_requirements_ref": "artifact:web:1", "depends_on_task_id": "task:web",
            "knowledge_scope": {"type": "resume", "user_id": knowledge.scope.user_id, "project_id": knowledge.scope.project_id, "knowledge_base_id": str(knowledge.default_knowledge_base_id)},
            "candidate_chunk_ids": [str(chunk.id)], "top_k": 1, "output_schema_version": "profile-evidence-output-v1",
        }, requested_allowed_tools=("retrieve_resume_evidence",), requested_deadline=now + timedelta(minutes=2),
            requested_budget=BudgetLimits(tokens=20_000, cost_microunits=1000, wall_clock_ms=60_000, model_calls=3, tool_calls=3), failure_behavior="allow_partial", idempotency_key="profile:binding",
    )
    claim = SimpleNamespace(
        parent=SimpleNamespace(id="parent:profile", principal=knowledge.scope.user_id, session_id="session:profile", origin_turn_id="turn:profile", deadline_at=now + timedelta(minutes=2)),
        task=SimpleNamespace(id=contract.task_id, specialist_id=contract.specialist_id, inputs_ref_json=contract.inputs),
        run=SimpleNamespace(id="child:profile", deadline_at=now + timedelta(minutes=2)),
    )
    bindings = ProfileKnowledgeBindings(knowledge)
    user_id, project_id, knowledge_base_id = bindings.authority_values(claim)
    tool = RetrieveResumeEvidenceTool(knowledge)
    raw_tools = ToolRegistry([])
    raw_tools._tools = {tool.name: tool}
    tools = UnifiedToolRegistry(raw_tools)
    provider = _Provider(); provider.chunk_id = str(chunk.id)
    runtime = AgentRuntime(tools, ToolPolicy(["read"]), RuntimeConfig(max_model_calls=3, max_tool_calls=3, max_seconds=10), provider_resolver=lambda _name: provider)
    capability = runtime.gate.registry.resolve_execution(tool.name)
    runtime.gate.store.create_policy_rule(PolicyRule(id="profile-binding-rag", server_id="builtin", tool_name=tool.name, effect="allowlist_auto", actions=("read",), schema_hash=capability.schema_hash, created_by="test"))
    authority = RuntimeContextAuthority(
        parent_run_id=claim.parent.id, child_task_id=contract.task_id, child_run_id=claim.run.id,
        session_id=uuid4(), turn_id=uuid4(), principal=claim.parent.principal, now=now,
        parent_deadline=claim.parent.deadline_at, policy_deadline=claim.parent.deadline_at,
        parent_remaining_budget=contract.requested_budget, policy_budget=contract.requested_budget,
        scenario_tools=frozenset({tool.name}), policy_tools=frozenset({tool.name}),
        allowed_artifact_types=frozenset({"profile_evidence_result"}), allowed_knowledge_scope_types=frozenset({"resume"}),
        knowledge_user_id=user_id, knowledge_project_id=project_id, knowledge_base_id=knowledge_base_id,
        runtime_revision="test", provider=provider.name, model="fixture", tool_registry=tools,
    )

    built = ChildContextBuilder(bindings).build(contract, definition, authority, references=bindings.references(claim))
    result = await ProfileEvidenceAnalyst(runtime).run(built.spec, built.context, contract.inputs)

    assert result.outcome.status == "succeeded"
    assert result.output["matches"][0]["evidence"][0]["chunk_id"] == str(chunk.id)
