from functools import lru_cache
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from starter_agent.agent.context import ContextBuilder
from starter_agent.agent.runtime import AgentRuntime
from starter_agent.application import ApplicationService
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.capabilities.gate import PreToolCallGate, UnifiedToolExecutor
from starter_agent.capabilities.confirmations import (
    ConfirmationBroker,
    ConfirmationService,
    TurnCoordinator,
)
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.knowledge.service import KnowledgeApplicationService
from starter_agent.knowledge.store import SQLiteKnowledgeStore
from starter_agent.mcp.config import McpConfigLoader
from starter_agent.mcp.manager import McpManager
from starter_agent.mcp.network_guard import PlaywrightNetworkGuard
from starter_agent.observability.logging import configure_logging
from starter_agent.providers.registry import ProviderRegistry
from starter_agent.runtime_revision import RuntimeRevision
from starter_agent.settings import AgentSettings, load_settings
from starter_agent.skills.registry import SkillRegistry
from starter_agent.skills.job_research import JobResearchOrchestrator
from starter_agent.skills.single_agent_baseline import SingleAgentBaselineRunner
from starter_agent.job_research.fallback import JobPageFallback
from starter_agent.skills.selector import SkillSelector
from starter_agent.tools.builtin.knowledge import RetrieveResumeEvidenceTool
from starter_agent.tools.policy import ToolPolicy
from starter_agent.tools.registry import ToolRegistry
from starter_agent.tools.adapters.job_description_extractor import JobDescriptionExtractor
from starter_agent.tools.adapters.safe_web_fetcher import SafeWebFetcher
from starter_agent.delegation.store import SQLiteRunStore
from starter_agent.delegation.context import (
    ChildContextBuilder,
    ContextBuildError,
    RuntimeContextAuthority,
)
from starter_agent.delegation.profile_knowledge import ProfileKnowledgeBindings
from starter_agent.delegation.registry import SpecialistRegistry
from starter_agent.delegation.worker import (
    WorkerPoolConfig,
    compose_delegation_worker,
)
from starter_agent.delegation.dispatcher import DispatcherConfig
from starter_agent.delegation.models import BudgetLimits
from starter_agent.delegation.service import DelegationService
from starter_agent.orchestration.background import BackgroundTaskService
from starter_agent.delegation.coordinator import Coordinator
from starter_agent.delegation.backfill import ChatBackfillService
from starter_agent.trust.store import TrustStore
from starter_agent.trust.trace import CapabilityAuditTrustBridge, DelegationEventTrustBridge
from starter_agent.trust.release_gate import DelegationReleaseDecisionService
from starter_agent.cv_workbench.runtime import (
    WorkbenchRuntime,
    create_workbench_runtime,
)
from starter_agent.cv_workbench.tailoring import ProviderTailoredResumeGenerator


class _FailClosedDelegationReferenceResolver:
    """Task12 deliberately permits no implicit context-reference expansion."""

    def load(self, _reference, _authority):
        raise ContextBuildError(
            "context_reference_unavailable",
            "production reference resolver is not configured",
        )


class _DelegationReferenceResolver:
    def __init__(self, profile_knowledge: ProfileKnowledgeBindings) -> None:
        self.profile_knowledge = profile_knowledge

    def load(self, reference, authority):
        if reference.kind == "knowledge_chunk":
            return self.profile_knowledge.load(reference, authority)
        return _FailClosedDelegationReferenceResolver().load(reference, authority)


def _runtime_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, value)


def _remaining_budget(parent) -> BudgetLimits:
    return BudgetLimits(
        **{
            field: max(
                0,
                getattr(parent.budget_total, field)
                - getattr(parent.budget_consumed, field),
            )
            for field in BudgetLimits.model_fields
        }
    )


@lru_cache
def get_settings() -> AgentSettings:
    return load_settings()


@lru_cache
def create_cv_workbench_runtime() -> WorkbenchRuntime:
    settings = get_settings()
    providers = ProviderRegistry(settings)
    from starter_agent.cv_workbench.workspaces import RuntimeFeatureAvailabilityProvider
    return create_workbench_runtime(
        settings.app.database_url,
        settings.project_root,
        feature_provider=RuntimeFeatureAvailabilityProvider(create_application),
        tailoring_generator=ProviderTailoredResumeGenerator(
            provider_resolver=providers.get,
            provider_name=settings.model.default_provider,
            model=settings.model.default_model,
        ),
    )


@lru_cache
def create_application() -> ApplicationService:
    settings = get_settings()
    configure_logging(settings.resolve_path(settings.app.log_path))
    store = SQLiteSessionStore(settings.app.database_url, settings.project_root)
    providers = ProviderRegistry(settings)
    knowledge = KnowledgeApplicationService(
        settings,
        SQLiteKnowledgeStore(
            settings.app.database_url, settings.project_root
        ),
    )
    enabled_tools = list(settings.tools.enabled)
    if RetrieveResumeEvidenceTool.name not in enabled_tools:
        enabled_tools.append(RetrieveResumeEvidenceTool.name)
    builtin_tools = ToolRegistry(
        enabled_tools,
        settings=settings,
        knowledge_service=knowledge,
    )
    tools = UnifiedToolRegistry(
        builtin_tools,
        allowed_risk_levels=settings.tools.allow_risk_levels,
    )
    policy = ToolPolicy(settings.tools.allow_risk_levels)
    capability_store = CapabilityStore(
        settings.app.database_url,
        settings.project_root,
    )
    trust_store = TrustStore(settings.app.database_url, settings.project_root)
    capability_store.add_audit_sink(CapabilityAuditTrustBridge(trust_store).record)
    for override in capability_store.list_builtin_tool_overrides():
        try:
            tools.set_tool_enabled(override.tool_name, override.enabled)
        except KeyError:
            continue
    gate = PreToolCallGate(capability_store, registry=tools)
    executor = UnifiedToolExecutor(capability_store, gate=gate)
    confirmation_service = ConfirmationService(
        capability_store,
        gate,
        broker=ConfirmationBroker(),
        confirmation_ttl_seconds=settings.mcp.confirmation_timeout_seconds,
        expire_orphans=True,
    )
    turn_coordinator = TurnCoordinator(
        confirmation_service,
        confirmation_timeout_seconds=settings.mcp.confirmation_timeout_seconds,
    )
    runtime = AgentRuntime(
        tools,
        policy,
        settings.runtime,
        settings.context,
        gate=gate,
        executor=executor,
        turn_coordinator=turn_coordinator,
        knowledge_scope=knowledge.scope,
        knowledge_base_id=knowledge.default_knowledge_base_id,
        provider_resolver=providers.get,
    )
    skills = SkillRegistry(
        settings.project_root / "backend/src/starter_agent/skills",
        store=capability_store,
        dependency_resolver=lambda dependency: (
            (
                (capability := tools.resolve_execution(dependency.name))
                is not None
                and capability.enabled
                and capability.connected
                and capability.review_state == "approved"
                and executor.has_invoker(
                    capability.server_id,
                    capability.canonical_name,
                )
            )
            if dependency.kind in {"tool", "mcp"}
            else dependency.name == "job_description_ingestion"
        ),
    )
    skills.reload()
    prompt_path = settings.project_root / "config/prompts/system.md"
    runtime_revision = RuntimeRevision.build(
        code_version=os.environ.get(
            "STARTER_AGENT_CODE_VERSION",
            "workspace",
        ),
        skill_revision=skills.snapshot().revision,
        tool_revision=f"context-{tools.context_revision}",
        prompt_hash=hashlib.sha256(prompt_path.read_bytes()).hexdigest(),
        config_hash=hashlib.sha256(
            json.dumps(
                {
                    "default_provider": settings.model.default_provider,
                    "default_model": settings.model.default_model,
                    "enabled_tools": sorted(settings.tools.enabled),
                    "allowed_risk_levels": sorted(
                        settings.tools.allow_risk_levels
                    ),
                    "mcp_config_path": str(settings.mcp.config_path),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    )
    runtime.runtime_revision = runtime_revision
    context = ContextBuilder(
        settings.resolve_path(settings.app.identity_path),
        settings.project_root / "config/prompts/system.md",
        skill_registry=skills,
        skill_selector=SkillSelector(skills),
    )
    application = ApplicationService(
        settings=settings,
        store=store,
        providers=providers,
        runtime=runtime,
        context=context,
    )
    application.runtime_revision = runtime_revision
    application.configure_job_description_ingestion(knowledge)
    delegation_store = SQLiteRunStore(settings.app.database_url, settings.project_root)
    application.configure_orchestration_tasks(
        BackgroundTaskService(delegation_store)
    )
    application.configure_delegation_resume(store=delegation_store)
    specialist_project_root = settings.project_root
    specialist_definitions = specialist_project_root / "config/specialists"
    if not specialist_definitions.is_dir():
        specialist_project_root = Path(__file__).resolve().parents[3]
        specialist_definitions = specialist_project_root / "config/specialists"
    specialist_registry = SpecialistRegistry(
        specialist_definitions,
        project_root=specialist_project_root,
        dependency_resolver=lambda dependency: (
            dependency in {"service:serpapi", "mcp:playwright", "service:rag"}
            or dependency == "tool:retrieve_resume_evidence"
        ),
    )
    specialist_registry.reload()
    application.configure_job_research_delegation(
        store=delegation_store,
        service=DelegationService(store=delegation_store, registry=specialist_registry),
        coordinator=Coordinator(store=delegation_store),
        registry=specialist_registry,
    )
    release = settings.delegation_release
    decision_service = DelegationReleaseDecisionService(trust_store)
    application.configure_delegation_route_policy(
        lambda: (
            decision_service.route_config(
                decision_id=release.decision_id,
                now=datetime.now(UTC),
                baseline_report_hash=release.baseline_report_hash,
                candidate_report_hash=release.candidate_report_hash,
            )
            if release.decision_id
            and release.baseline_report_hash
            and release.candidate_report_hash
            else {
                "delegated_job_research_enabled": False,
                "legacy_job_research_enabled": False,
            }
        )
    )
    profile_knowledge = ProfileKnowledgeBindings(knowledge)

    def delegation_authority(claim, specialist):
        now = datetime.now(UTC)
        knowledge_user_id = knowledge_project_id = knowledge_base_id = None
        if claim.task.specialist_id == "profile_evidence_analyst":
            knowledge_user_id, knowledge_project_id, knowledge_base_id = profile_knowledge.authority_values(claim)
        return RuntimeContextAuthority(
            parent_run_id=claim.parent.id,
            child_task_id=claim.task.id,
            child_run_id=claim.run.id,
            session_id=_runtime_uuid(claim.parent.session_id),
            turn_id=_runtime_uuid(claim.parent.origin_turn_id),
            principal=claim.parent.principal,
            now=now,
            parent_deadline=claim.parent.deadline_at,
            policy_deadline=claim.parent.deadline_at,
            parent_remaining_budget=_remaining_budget(claim.parent),
            policy_budget=specialist.max_budget,
            scenario_tools=frozenset(specialist.allowed_tools),
            policy_tools=frozenset(specialist.allowed_tools),
            allowed_artifact_types=frozenset(specialist.allowed_artifact_types),
            allowed_knowledge_scope_types=frozenset(
                specialist.allowed_knowledge_scope_types
            ),
            knowledge_user_id=knowledge_user_id,
            knowledge_project_id=knowledge_project_id,
            knowledge_base_id=knowledge_base_id,
            runtime_revision=runtime_revision.id,
            provider=settings.model.default_provider,
            model=settings.model.default_model,
            tool_registry=tools,
        )

    delegation_trace_bridge = DelegationEventTrustBridge(
        delegation_store, trust_store
    )
    delegation_store.add_event_sink(delegation_trace_bridge.record)
    # Recover projections missed while the process was down.  This only reads
    # durable RunStore evidence and cannot execute a Worker or business task.
    delegation_trace_bridge.backfill_recent(parent_page_size=500, event_page_size=500)
    application.configure_delegation_worker(
        compose_delegation_worker(
            store=delegation_store,
            registry=specialist_registry,
            context_builder=ChildContextBuilder(_DelegationReferenceResolver(profile_knowledge)),
            runtime=runtime,
            authority_factory=delegation_authority,
            reference_factory=profile_knowledge.references,
            # Search/Browser providers can hold the asyncio thread during one
            # atomic call for longer than the 30-second test default. Keep the
            # production lease above that bound so the reaper cannot create a
            # duplicate Child attempt while the original call is still live.
            dispatcher_config=DispatcherConfig(lease_ttl=timedelta(minutes=5)),
            worker_config=WorkerPoolConfig(),
            artifact_store=store,
            artifact_retention=timedelta(days=settings.artifacts.retention_days),
            trace_bridge=delegation_trace_bridge,
        )
    )
    application.configure_delegation_background(
        dispatcher=application.delegation_worker.dispatcher,
        chat_backfill=ChatBackfillService(run_store=delegation_store, session_store=store),
    )
    application.configure_single_agent_baseline_runner(
        SingleAgentBaselineRunner(JobResearchOrchestrator(
            tools,
            executor,
            turn_coordinator=runtime.turn_coordinator,
            ingestion_available=lambda: (
                application.job_description_ingestion is not None
            ),
            page_fallback=JobPageFallback(
                SafeWebFetcher.from_config(settings.tools.public_web),
                JobDescriptionExtractor(),
            ),
        ))
    )
    return application


@lru_cache
def create_knowledge_service() -> KnowledgeApplicationService:
    service = create_application().job_description_ingestion
    assert service is not None
    return service.knowledge


@lru_cache
def create_mcp_manager() -> McpManager:
    settings = get_settings()
    configuration = McpConfigLoader(settings.project_root).load(
        settings.mcp.config_path
    )
    store = CapabilityStore(settings.app.database_url, settings.project_root)
    browser_network_guard = PlaywrightNetworkGuard()
    try:
        return McpManager(
            configuration,
            store=store,
            tool_executor=create_application().runtime.executor,
            initialize_timeout_seconds=settings.mcp.initialize_timeout_seconds,
            shutdown_timeout_seconds=settings.mcp.shutdown_timeout_seconds,
            browser_network_guard=browser_network_guard,
        )
    except BaseException:
        browser_network_guard.dispose()
        raise
