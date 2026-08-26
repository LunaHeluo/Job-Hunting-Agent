"""Deterministic, current-resume-scoped evidence selection for matching."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol
from uuid import UUID

from starter_agent.cv_workbench.contracts import EvidenceReference
from starter_agent.cv_workbench.resume_import import ResumeMarkdownNormalizer
from starter_agent.knowledge.models import KnowledgeChunk, KnowledgeScope, RetrievalMatch
from starter_agent.knowledge.store import SQLiteKnowledgeStore


_ENGLISH_TERM = re.compile(r"[a-z][a-z0-9+#.\-]{1,}", re.IGNORECASE)
_CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_CHINESE_SEPARATOR = re.compile(r"(?:以及|或者|并且|并|和|及|与|或)")
_LEADING_BOILERPLATE = re.compile(
    r"^(?:岗位|职位|任职|工作|主要|相关|要求|职责|负责|参与|具备|具有|熟悉|掌握|能够|可以)+"
)
_TRAILING_BOILERPLATE = re.compile(
    r"(?:经验|能力|要求|职责|工作|优先|加分|者)+$"
)
_IGNORED_TERMS = {
    "岗位",
    "职位",
    "工作",
    "要求",
    "职责",
    "负责",
    "参与",
    "能力",
    "经验",
    "熟悉",
    "掌握",
}
_MIN_CANDIDATE_COVERAGE = 0.40


class ScopedResumeRetriever(Protocol):
    def retrieve(
        self,
        scope: KnowledgeScope,
        knowledge_base_id: UUID,
        question: str,
        *,
        top_k: int,
        document_ids: list[UUID] | None = None,
        document_types: list[str] | None = None,
        filenames: list[str] | None = None,
        versions: list[int] | None = None,
    ) -> list[RetrievalMatch]: ...


@dataclass(frozen=True)
class ResumeEvidenceSelection:
    query_terms: tuple[str, ...]
    covered_terms: frozenset[str]
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class _ResumeBlock:
    ordinal: int
    text: str
    searchable: str


@dataclass(frozen=True)
class _RankedEvidence:
    retrieval_rank: int
    block: _ResumeBlock
    chunk: KnowledgeChunk
    covered_terms: frozenset[str]
    coverage: float


class ResumeEvidenceSelector:
    def __init__(
        self,
        *,
        retriever: ScopedResumeRetriever,
        chunk_reader: SQLiteKnowledgeStore,
    ) -> None:
        self.retriever = retriever
        self.chunk_reader = chunk_reader
        self.normalizer = ResumeMarkdownNormalizer()

    def select(
        self,
        requirement_text: str,
        *,
        scope: KnowledgeScope,
        knowledge_base_id: UUID,
        document_id: UUID,
        markdown: str,
    ) -> ResumeEvidenceSelection:
        query_terms = self._query_terms(requirement_text)
        matches = self.retriever.retrieve(
            scope,
            knowledge_base_id,
            requirement_text,
            top_k=5,
            document_ids=[document_id],
            document_types=["resume"],
        )
        unique_matches = tuple(
            {
                item.chunk_id: item
                for item in sorted(matches, key=lambda value: value.rank)
                if item.document_id == document_id
            }.values()
        )
        chunks = self.chunk_reader.get_chunks_by_ids(
            scope,
            knowledge_base_id,
            [item.chunk_id for item in unique_matches],
        )
        blocks = self._blocks(markdown)
        ranked: list[_RankedEvidence] = []
        for match in unique_matches:
            stored = chunks.get(match.chunk_id)
            if stored is None:
                continue
            chunk, document_type = stored
            if not self._chunk_is_in_scope(
                chunk,
                document_type=document_type,
                scope=scope,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
            ):
                continue
            aligned = self._align_block(chunk, blocks, query_terms)
            if aligned is None:
                continue
            block, covered = aligned
            coverage = len(covered) / max(1, len(query_terms))
            if coverage < _MIN_CANDIDATE_COVERAGE:
                continue
            ranked.append(
                _RankedEvidence(
                    retrieval_rank=match.rank,
                    block=block,
                    chunk=chunk,
                    covered_terms=covered,
                    coverage=coverage,
                )
            )

        ranked.sort(
            key=lambda item: (
                -item.coverage,
                item.retrieval_rank,
                item.block.ordinal,
            )
        )
        selected: list[_RankedEvidence] = []
        used_blocks: set[int] = set()
        for item in ranked:
            if item.block.ordinal in used_blocks:
                continue
            used_blocks.add(item.block.ordinal)
            selected.append(item)
            if len(selected) == 3:
                break

        return ResumeEvidenceSelection(
            query_terms=query_terms,
            covered_terms=frozenset(
                term for item in selected for term in item.covered_terms
            ),
            evidence=tuple(
                EvidenceReference(
                    chunk_id=str(item.chunk.id),
                    source_ref=(
                        f"knowledge-chunk://{knowledge_base_id}/{item.chunk.id}"
                    ),
                    content_sha256=item.chunk.content_sha256,
                    quote=item.block.text[:1000],
                )
                for item in selected
            ),
        )

    @staticmethod
    def _query_terms(requirement_text: str) -> tuple[str, ...]:
        terms: list[str] = [
            value.casefold() for value in _ENGLISH_TERM.findall(requirement_text)
        ]
        for run in _CHINESE_RUN.findall(requirement_text):
            for value in _CHINESE_SEPARATOR.split(run):
                cleaned = _TRAILING_BOILERPLATE.sub(
                    "", _LEADING_BOILERPLATE.sub("", value)
                )
                if len(cleaned) >= 2 and cleaned not in _IGNORED_TERMS:
                    terms.append(cleaned)
        return tuple(dict.fromkeys(terms))

    def _blocks(self, markdown: str) -> tuple[_ResumeBlock, ...]:
        normalized = self.normalizer.normalize(markdown)
        lines = normalized.markdown.splitlines()
        return tuple(
            _ResumeBlock(
                ordinal=item.ordinal,
                text="\n".join(lines[item.start_line - 1 : item.end_line]),
                searchable=self._searchable(
                    "\n".join(lines[item.start_line - 1 : item.end_line])
                ),
            )
            for item in normalized.blocks
        )

    @classmethod
    def _align_block(
        cls,
        chunk: KnowledgeChunk,
        blocks: tuple[_ResumeBlock, ...],
        query_terms: tuple[str, ...],
    ) -> tuple[_ResumeBlock, frozenset[str]] | None:
        chunk_text = cls._searchable(chunk.text)
        candidates: list[tuple[_ResumeBlock, frozenset[str]]] = []
        for block in blocks:
            if not block.searchable:
                continue
            if block.searchable not in chunk_text and chunk_text not in block.searchable:
                continue
            covered = frozenset(
                term for term in query_terms if cls._searchable(term) in block.searchable
            )
            candidates.append((block, covered))
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (-len(item[1]), item[0].ordinal),
        )

    @staticmethod
    def _chunk_is_in_scope(
        chunk: KnowledgeChunk,
        *,
        document_type: str,
        scope: KnowledgeScope,
        knowledge_base_id: UUID,
        document_id: UUID,
    ) -> bool:
        return (
            chunk.user_id == scope.user_id
            and chunk.project_id == scope.project_id
            and chunk.knowledge_base_id == knowledge_base_id
            and chunk.document_id == document_id
            and document_type == "resume"
        )

    @staticmethod
    def _searchable(value: str) -> str:
        return "".join(value.casefold().split())
