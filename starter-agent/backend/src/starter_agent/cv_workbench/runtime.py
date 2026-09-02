"""Composition root for the local-first CV Workbench services."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from starter_agent.cv_workbench.bindings import EvidenceBindingService
from starter_agent.cv_workbench.analytics import ApplicationAnalyticsService
from starter_agent.cv_workbench.applications import ApplicationService
from starter_agent.cv_workbench.evidence_adapters import (
    ArtifactEvidenceReader,
    KnowledgeEvidenceReader,
)
from starter_agent.cv_workbench.exports import ExportService, RestrictedExportArtifactRepository
from starter_agent.cv_workbench.job_adapters import SessionKnowledgeJobContentRepository
from starter_agent.cv_workbench.jobs import JobService
from starter_agent.cv_workbench.interviews import InterviewReviewService
from starter_agent.cv_workbench.match_adapters import SessionMatchCandidateRepository
from starter_agent.cv_workbench.matching import MatchService
from starter_agent.cv_workbench.merging import ResumeMergeService
from starter_agent.cv_workbench.resume_import import ResumeImportService
from starter_agent.cv_workbench.resume_import_adapters import (
    ScopedKnowledgeResumeImporter,
    SessionResumeArtifactWriter,
)
from starter_agent.cv_workbench.store import SQLiteWorkbenchStore
from starter_agent.cv_workbench.suggestions import SuggestionService
from starter_agent.cv_workbench.tailoring_evidence import ResumeEvidenceSelector
from starter_agent.cv_workbench.tailoring import (
    TailoredResumeGenerator,
    TailoredResumeService,
)
from starter_agent.cv_workbench.version_adapters import (
    SessionKnowledgeVersionContentRepository,
)
from starter_agent.cv_workbench.versioning import ResumeVersionService
from starter_agent.cv_workbench.workspaces import FeatureAvailabilityProvider, WorkspaceService
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.knowledge.mappings import build_query_mapping_catalog
from starter_agent.knowledge.retrieval import KnowledgeRetriever
from starter_agent.knowledge.store import SQLiteKnowledgeStore
from starter_agent.settings import QueryMappingConfig


@dataclass(frozen=True)
class WorkbenchRuntime:
    store: SQLiteWorkbenchStore
    artifacts: SQLiteSessionStore
    knowledge: SQLiteKnowledgeStore
    evidence: EvidenceBindingService
    workspaces: WorkspaceService
    resume_imports: ResumeImportService
    versions: ResumeVersionService
    merges: ResumeMergeService
    jobs: JobService
    evidence_selector: ResumeEvidenceSelector
    matches: MatchService
    suggestions: SuggestionService
    tailoring: TailoredResumeService | None
    exports: ExportService
    applications: ApplicationService
    interviews: InterviewReviewService
    analytics: ApplicationAnalyticsService

    def close(self) -> None:
        """Release SQLite handles for application shutdown and Windows tests."""
        self.store.engine.dispose()
        self.artifacts.engine.dispose()
        self.knowledge.engine.dispose()


def create_workbench_runtime(
    database_url: str,
    project_root: Path,
    *,
    feature_provider: FeatureAvailabilityProvider | None = None,
    tailoring_generator: TailoredResumeGenerator | None = None,
) -> WorkbenchRuntime:
    store = SQLiteWorkbenchStore(database_url, project_root)
    artifacts = SQLiteSessionStore(database_url, project_root)
    knowledge = SQLiteKnowledgeStore(database_url, project_root)
    evidence_selector = ResumeEvidenceSelector(
        retriever=KnowledgeRetriever(
            knowledge,
            build_query_mapping_catalog(QueryMappingConfig()),
        ),
        chunk_reader=knowledge,
    )
    knowledge_reader = KnowledgeEvidenceReader(knowledge)
    evidence = EvidenceBindingService(
        store=store,
        readers={
            "artifact": ArtifactEvidenceReader(artifacts),
            "document_version": knowledge_reader,
            "chunk": knowledge_reader,
        },
    )
    content = SessionKnowledgeVersionContentRepository(artifacts, knowledge)
    versions = ResumeVersionService(store=store, content=content)
    suggestions = SuggestionService(store=store, versions=versions)
    exports = ExportService(
        store=store,
        content=content,
        artifacts=RestrictedExportArtifactRepository(artifacts),
    )
    return WorkbenchRuntime(
        store=store,
        artifacts=artifacts,
        knowledge=knowledge,
        evidence=evidence,
        workspaces=WorkspaceService(store=store, feature_provider=feature_provider),
        resume_imports=ResumeImportService(
            store=store,
            artifact_writer=SessionResumeArtifactWriter(artifacts),
            knowledge_importer=ScopedKnowledgeResumeImporter(knowledge),
            evidence_bindings=evidence,
        ),
        versions=versions,
        merges=ResumeMergeService(store=store, content=content),
        jobs=JobService(
            store=store,
            content=SessionKnowledgeJobContentRepository(artifacts, knowledge),
            evidence=evidence,
        ),
        evidence_selector=evidence_selector,
        matches=MatchService(
            store=store,
            candidates=SessionMatchCandidateRepository(artifacts),
            evidence_reader=knowledge_reader,
            evidence_bindings=evidence,
        ),
        suggestions=suggestions,
        tailoring=(
            TailoredResumeService(
                store=store,
                versions=versions,
                suggestions=suggestions,
                generator=tailoring_generator,
            )
            if tailoring_generator is not None
            else None
        ),
        exports=exports,
        applications=ApplicationService(store=store),
        interviews=InterviewReviewService(store=store),
        analytics=ApplicationAnalyticsService(store=store),
    )
