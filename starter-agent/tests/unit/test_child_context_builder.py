from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from starter_agent.delegation.context import (
    ChildContextBuilder,
    ContextBuildError,
    ContextFragment,
    ContextReference,
    RuntimeContextAuthority,
)
from starter_agent.delegation.models import BudgetLimits, TaskContract
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.domain.models import Message
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.domain.models import ToolResult
from starter_agent.tools.base import Tool, ToolContext
from starter_agent.tools.registry import ToolRegistry


NOW = datetime(2026, 8, 12, 6, 0, tzinfo=UTC)


class _Tool(Tool):
    description = "fixture"
    input_schema = {"type": "object", "additionalProperties": False}
    risk_level = "read"

    def __init__(self, name: str) -> None:
        self.name = name

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={})


def _tool_registry() -> UnifiedToolRegistry:
    registry = ToolRegistry([])
    names = {
        "search_jobs_serpapi",
        "mcp__playwright__browser_navigate",
        "retrieve_resume_evidence",
    }
    registry._tools = {name: _Tool(name) for name in names}
    return UnifiedToolRegistry(registry)


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[ContextReference] = []

    def load(self, reference: ContextReference, authority: RuntimeContextAuthority):
        self.calls.append(reference)
        if reference.principal != authority.principal:
            raise ContextBuildError("context_reference_forbidden", "principal mismatch")
        if reference.parent_run_id != authority.parent_run_id:
            raise ContextBuildError("context_reference_forbidden", "run mismatch")
        if reference.expires_at <= authority.now:
            raise ContextBuildError("context_reference_expired", "expired")
        if reference.kind == "artifact" and reference.artifact_type not in authority.allowed_artifact_types:
            raise ContextBuildError("context_reference_forbidden", "artifact type")
        if reference.kind == "knowledge_chunk" and reference.knowledge_scope_type not in authority.allowed_knowledge_scope_types:
            raise ContextBuildError("context_reference_forbidden", "scope type")
        return ContextFragment(
            kind=reference.kind,
            ref_id=reference.ref_id,
            content=f"fragment:{reference.ref_id}",
            artifact_type=reference.artifact_type,
            document_id=reference.document_id,
            chunk_id=reference.chunk_id,
            knowledge_user_id=reference.knowledge_user_id,
            knowledge_project_id=reference.knowledge_project_id,
            knowledge_base_id=reference.knowledge_base_id,
            source_url=reference.source_url,
            content_hash=reference.content_hash,
            untrusted=True,
        )


def _registry() -> SpecialistRegistry:
    root = __import__("pathlib").Path(__file__).parents[2]
    registry = SpecialistRegistry(
        root / "config/specialists",
        project_root=root,
        dependency_resolver=lambda _dependency: True,
    )
    registry.reload()
    return registry


def _contract(specialist_id: str, tools: tuple[str, ...], budget: BudgetLimits) -> TaskContract:
    inputs = (
        {
            "query": "Sydney agent engineer",
            "target_fields": ["title", "requirements"],
            "max_pages": 2,
            "stop_conditions": {"target_valid_jobs": 2},
            "output_schema_version": "job-web-output-v1",
        }
        if specialist_id == "job_web_researcher"
        else {
            "normalized_job_requirements_ref": "artifact:env:web",
            "depends_on_task_id": "task:web",
            "knowledge_scope": {
                "type": "resume",
                "user_id": "user:001",
                "project_id": "project:001",
                "knowledge_base_id": "00000000-0000-0000-0000-000000000003",
            },
            "top_k": 3,
            "output_schema_version": "profile-evidence-output-v1",
        }
    )
    return TaskContract(
        task_id=f"task:{specialist_id}",
        parent_run_id="parent:001",
        specialist_id=specialist_id,
        goal="Research only the assigned evidence",
        inputs=inputs,
        requested_allowed_tools=tools,
        requested_deadline=NOW + timedelta(minutes=30),
        requested_budget=budget,
        failure_behavior="allow_partial",
        idempotency_key=f"idem:{specialist_id}",
    )


def _authority(**updates) -> RuntimeContextAuthority:
    values = dict(
        parent_run_id="parent:001",
        child_task_id="task:job_web_researcher",
        child_run_id="child:001",
        session_id=uuid4(),
        turn_id=uuid4(),
        principal="user:001",
        now=NOW,
        parent_deadline=NOW + timedelta(minutes=20),
        policy_deadline=NOW + timedelta(minutes=15),
        parent_remaining_budget=BudgetLimits(tokens=40_000, cost_microunits=1_000_000, wall_clock_ms=80_000, model_calls=10, tool_calls=30),
        policy_budget=BudgetLimits(tokens=30_000, cost_microunits=900_000, wall_clock_ms=70_000, model_calls=9, tool_calls=20),
        scenario_tools=frozenset({"search_jobs_serpapi", "mcp__playwright__browser_navigate"}),
        policy_tools=frozenset({"search_jobs_serpapi", "mcp__playwright__browser_navigate"}),
        allowed_artifact_types=frozenset({"browser_snapshot"}),
        allowed_knowledge_scope_types=frozenset(),
        knowledge_user_id="user:001",
        knowledge_project_id="project:001",
        knowledge_base_id=None,
        runtime_revision="runtime-v1",
        provider="fixture",
        model="fixture-model",
        tool_registry=_tool_registry(),
    )
    values.update(updates)
    return RuntimeContextAuthority(**values)


def test_builder_uses_owned_fields_and_minimum_deadline_budget() -> None:
    registry = _registry()
    definition = registry.resolve("job_web_researcher")
    requested = BudgetLimits(tokens=35_000, cost_microunits=2_000_000, wall_clock_ms=90_000, model_calls=12, tool_calls=40)
    contract = _contract(
        definition.specialist_id,
        ("search_jobs_serpapi", "mcp__playwright__browser_navigate", "delegate_task"),
        requested,
    )
    resolver = _Resolver()
    authority = _authority()

    built = ChildContextBuilder(resolver).build(contract, definition, authority, references=())

    assert built.spec.system_prompt_ref == definition.system_prompt_ref
    assert built.spec.output_schema_ref.endswith(definition.schema_version)
    assert built.spec.allowed_tools == ("mcp__playwright__browser_navigate", "search_jobs_serpapi")
    assert built.deadline == NOW + timedelta(milliseconds=definition.default_deadline_ms)
    assert built.context.budget.limits == BudgetLimits(tokens=30_000, cost_microunits=900_000, wall_clock_ms=70_000, model_calls=9, tool_calls=20)
    assert built.context.messages[0].role == "system"
    assert built.context.messages[0].content == definition.system_prompt
    assert "full_chat" not in built.context.working_memory
    assert built.context.effective_tool_view == list(built.spec.allowed_tools)


def test_builder_loads_only_authorized_references_as_untrusted_fragments() -> None:
    definition = _registry().resolve("job_web_researcher")
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), definition.default_budget)
    reference = ContextReference(
        kind="artifact",
        ref_id="artifact:001",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:job_web_researcher",
        child_run_id="child:001",
        artifact_type="browser_snapshot",
        expires_at=NOW + timedelta(days=1),
        source_url="https://example.test/job",
        content_hash="a" * 64,
    )

    built = ChildContextBuilder(_Resolver()).build(contract, definition, _authority(), references=(reference,))

    payload = built.context.working_memory["context_fragments"]
    assert payload == [{"kind": "artifact", "ref_id": "artifact:001", "content": "fragment:artifact:001", "source_url": "https://example.test/job", "content_hash": "a" * 64, "untrusted": True}]
    assert all(message.content != "fragment:artifact:001" for message in built.context.messages)


def test_builder_rejects_wrong_principal_reference_and_records_audit() -> None:
    definition = _registry().resolve("job_web_researcher")
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), definition.default_budget)
    reference = ContextReference(
        kind="artifact",
        ref_id="artifact:private",
        parent_run_id="parent:001",
        principal="user:other",
        child_task_id="task:job_web_researcher",
        child_run_id="child:001",
        artifact_type="browser_snapshot",
        expires_at=NOW + timedelta(days=1),
    )
    audit: list[dict] = []

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_Resolver(), audit_sink=audit.append).build(contract, definition, _authority(), references=(reference,))

    assert exc.value.code == "context_reference_forbidden"
    assert audit[-1]["decision"] == "deny"
    assert audit[-1]["ref_id"] == "artifact:private"


def test_profile_context_only_accepts_authorized_resume_chunk_refs() -> None:
    definition = _registry().resolve("profile_evidence_analyst")
    contract = _contract(definition.specialist_id, ("retrieve_resume_evidence", "search_jobs_serpapi"), definition.default_budget)
    authority = _authority(
        child_task_id="task:profile_evidence_analyst",
        scenario_tools=frozenset({"retrieve_resume_evidence"}),
        policy_tools=frozenset({"retrieve_resume_evidence"}),
        allowed_artifact_types=frozenset({"profile_evidence_result"}),
        allowed_knowledge_scope_types=frozenset({"resume"}),
        knowledge_user_id="user:001",
        knowledge_project_id="project:001",
        knowledge_base_id="00000000-0000-0000-0000-000000000003",
    )
    reference = ContextReference(
        kind="knowledge_chunk",
        ref_id="chunk:001",
        document_id="document:001",
        chunk_id="chunk:001",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:profile_evidence_analyst",
        child_run_id="child:001",
        knowledge_scope_type="resume",
        knowledge_user_id="user:001",
        knowledge_project_id="project:001",
        knowledge_base_id="00000000-0000-0000-0000-000000000003",
        expires_at=NOW + timedelta(days=1),
    )

    built = ChildContextBuilder(_Resolver()).build(contract, definition, authority, references=(reference,))

    assert built.spec.allowed_tools == ("retrieve_resume_evidence",)
    assert built.context.knowledge_scope == "resume"
    assert "search_jobs_serpapi" not in repr(built.context.to_checkpoint())


def test_builder_fails_closed_without_callable_tool_registry() -> None:
    definition = _registry().resolve("job_web_researcher")
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), definition.default_budget)

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_Resolver()).build(
            contract,
            definition,
            _authority(tool_registry=None),
            references=(),
        )

    assert exc.value.code == "context_tool_registry_unavailable"


def test_builder_denies_reference_before_resolver_can_read_it() -> None:
    definition = _registry().resolve("job_web_researcher")
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), definition.default_budget)
    resolver = _Resolver()
    reference = ContextReference(
        kind="artifact",
        ref_id="artifact:resume",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:job_web_researcher",
        child_run_id="child:001",
        artifact_type="profile_evidence_result",
        expires_at=NOW + timedelta(days=1),
    )
    audit: list[dict] = []

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(resolver, audit_sink=audit.append).build(
            contract, definition, _authority(), references=(reference,)
        )

    assert exc.value.code == "context_reference_forbidden"
    assert resolver.calls == []
    assert audit[-1]["decision"] == "deny"


def test_builder_rejects_source_fragment_whose_proof_does_not_match_reference() -> None:
    class _SubstitutingResolver(_Resolver):
        def load(self, reference, authority):
            self.calls.append(reference)
            return ContextFragment(
                kind="source",
                ref_id=reference.ref_id,
                content="different page",
                source_url="https://attacker.test/job",
                content_hash="b" * 64,
            )

    definition = _registry().resolve("job_web_researcher")
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), definition.default_budget)
    reference = ContextReference(
        kind="source",
        ref_id="source:001",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:job_web_researcher",
        child_run_id="child:001",
        expires_at=NOW + timedelta(days=1),
        source_url="https://example.test/job",
        content_hash="a" * 64,
    )
    audit: list[dict] = []

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_SubstitutingResolver(), audit_sink=audit.append).build(
            contract, definition, _authority(), references=(reference,)
        )

    assert exc.value.code == "context_reference_invalid"
    assert audit[-1]["decision"] == "deny"


def test_builder_rejects_zero_effective_model_budget_with_domain_error() -> None:
    definition = _registry().resolve("job_web_researcher")
    zero = BudgetLimits(tokens=100, cost_microunits=100, wall_clock_ms=100, model_calls=0, tool_calls=0)
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), zero)

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_Resolver()).build(
            contract, definition, _authority(), references=()
        )

    assert exc.value.code == "context_budget_exhausted"


def test_coordinator_constraints_cannot_inject_chat_memory_prompt_or_tool_schema() -> None:
    definition = _registry().resolve("job_web_researcher")
    contract = _contract(
        definition.specialist_id,
        ("search_jobs_serpapi",),
        definition.default_budget,
    ).model_copy(
        update={
            "constraints": {
                "max_pages": 2,
                "nested": {"full_chat": [{"role": "user", "content": "secret"}]},
            }
        }
    )

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_Resolver()).build(
            contract, definition, _authority(), references=()
        )

    assert exc.value.code == "context_payload_forbidden"


def test_zero_cost_budget_stops_before_child_model_execution() -> None:
    definition = _registry().resolve("job_web_researcher")
    zero_cost = definition.default_budget.model_copy(update={"cost_microunits": 0})
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), zero_cost)

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_Resolver()).build(
            contract, definition, _authority(), references=()
        )

    assert exc.value.code == "context_budget_exhausted"


def test_reference_from_sibling_child_is_denied_before_read() -> None:
    definition = _registry().resolve("job_web_researcher")
    contract = _contract(definition.specialist_id, ("search_jobs_serpapi",), definition.default_budget)
    resolver = _Resolver()
    reference = ContextReference(
        kind="artifact",
        ref_id="artifact:sibling",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:sibling",
        child_run_id="child:sibling",
        artifact_type="browser_snapshot",
        expires_at=NOW + timedelta(days=1),
    )

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(resolver).build(
            contract, definition, _authority(), references=(reference,)
        )

    assert exc.value.code == "context_reference_forbidden"
    assert resolver.calls == []


def test_resolver_cannot_substitute_another_knowledge_chunk() -> None:
    class _SubstituteChunk(_Resolver):
        def load(self, reference, authority):
            self.calls.append(reference)
            return ContextFragment(
                kind=reference.kind,
                ref_id=reference.ref_id,
                content="wrong resume evidence",
                document_id="document:other",
                chunk_id="chunk:other",
            )

    definition = _registry().resolve("profile_evidence_analyst")
    contract = _contract(definition.specialist_id, ("retrieve_resume_evidence",), definition.default_budget)
    authority = _authority(
        child_task_id="task:profile_evidence_analyst",
        scenario_tools=frozenset({"retrieve_resume_evidence"}),
        policy_tools=frozenset({"retrieve_resume_evidence"}),
        allowed_artifact_types=frozenset({"profile_evidence_result"}),
        allowed_knowledge_scope_types=frozenset({"resume"}),
        knowledge_base_id="kb:001",
    )
    reference = ContextReference(
        kind="knowledge_chunk",
        ref_id="chunk:001",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:profile_evidence_analyst",
        child_run_id="child:001",
        knowledge_scope_type="resume",
        knowledge_user_id="user:001",
        knowledge_project_id="project:001",
        knowledge_base_id="kb:001",
        document_id="document:001",
        chunk_id="chunk:001",
        expires_at=NOW + timedelta(days=1),
    )

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(_SubstituteChunk()).build(
            contract, definition, authority, references=(reference,)
        )

    assert exc.value.code == "context_reference_invalid"


def test_knowledge_reference_from_other_project_is_denied_before_read() -> None:
    definition = _registry().resolve("profile_evidence_analyst")
    contract = _contract(definition.specialist_id, ("retrieve_resume_evidence",), definition.default_budget)
    authority = _authority(
        child_task_id="task:profile_evidence_analyst",
        scenario_tools=frozenset({"retrieve_resume_evidence"}),
        policy_tools=frozenset({"retrieve_resume_evidence"}),
        allowed_artifact_types=frozenset({"profile_evidence_result"}),
        allowed_knowledge_scope_types=frozenset({"resume"}),
        knowledge_base_id="kb:001",
    )
    resolver = _Resolver()
    reference = ContextReference(
        kind="knowledge_chunk",
        ref_id="chunk:other-project",
        parent_run_id="parent:001",
        principal="user:001",
        child_task_id="task:profile_evidence_analyst",
        child_run_id="child:001",
        knowledge_scope_type="resume",
        knowledge_user_id="user:001",
        knowledge_project_id="project:other",
        knowledge_base_id="kb:001",
        document_id="document:001",
        chunk_id="chunk:001",
        expires_at=NOW + timedelta(days=1),
    )

    with pytest.raises(ContextBuildError) as exc:
        ChildContextBuilder(resolver).build(
            contract, definition, authority, references=(reference,)
        )

    assert exc.value.code == "context_reference_forbidden"
    assert resolver.calls == []
