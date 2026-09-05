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
_ENGLISH_ACTION_TERMS = {
    "build",
    "building",
    "develop",
    "developing",
    "deliver",
    "delivering",
    "drive",
    "driving",
    "responsible",
    "support",
    "supporting",
}
_ENGLISH_ALIASES = {
    "apis": "api",
}
_MIN_CANDIDATE_COVERAGE = 0.40
_GENERIC_CONCEPTS = frozenset({"设计", "优化", "文档"})
# Keep conjunctions such as "React and Python" together. Punctuation separates
# responsibility clauses without treating every skill as a complete requirement.
_CLAUSE_SEPARATOR = re.compile(r"[，,；;。\n]+")

# Explicit terminology equivalences, not inferred experience. Every match still
# needs a literal quote from the authorized, current resume.
_BILINGUAL_TERMS = {
    "需求分析": ("需求分析", "产品需求", "prd", "product requirements", "requirements analysis"),
    "前端开发": ("前端开发", "frontend development", "front-end development", "developed responsive frontend", "frontend implementation", "front-end implementation"),
    "前端": ("前端场景", "前端", "frontend", "front-end", "front end"),
    "页面开发": ("页面开发", "页面的开发", "page development", "web page development"),
    "后端": ("后端", "backend", "back-end"),
    "ui": ("ui", "user interface", "用户界面"),
    "api": ("api", "apis", "接口"),
    "系统联调": ("系统联调", "功能联调", "integration testing"),
    "系统整合": ("系统整合", "system integration"),
    "应用发布": ("应用发布", "应用部署", "application deployment", "application release"),
    "接口集成": ("接口集成", "接口对接", "api integration", "integrated apis", "integrated api"),
    "响应式布局": ("响应式布局", "响应式设计", "responsive layout", "responsive layouts", "responsive design"),
    "自动化测试": ("自动化测试", "automated testing", "test automation", "automated tests"),
    "持续集成": ("持续集成", "continuous integration"),
    "设计": ("设计", "design", "designed", "designs"),
    "优化": ("优化", "optimized", "optimised", "optimization", "optimisation"),
    "文档": ("文档", "documentation"),
    "嵌入式": ("嵌入式", "embedded"),
}


# Contextual forms bind an action to its object within a bounded sentence span.
# Bare "pages" or "deployment" must never establish these capabilities alone.
# Named capture groups supply literal FTS anchors; quotes remain unmodified.
_CONTEXTUAL_PATTERNS = {
    "页面开发": re.compile(
        r"\b(?:built|developed|implemented|created|delivered|delivering)\b"
        r"(?=[^.!?\n]{0,180}\b(?:responsive|web|frontend|front[ -]end|html)\b)"
        r"[^.!?\n]{0,180}\b(?P<anchor>pages|layouts)\b", re.IGNORECASE,
    ),
    "应用发布": re.compile(
        r"\b(?:vercel|netlify|heroku|applications?|websites?|web apps?)\b"
        r"\s+(?:[\w-]+\s+){0,4}(?P<anchor>deployments?)\b", re.IGNORECASE,
    ),
}
_CHINESE_CONCEPT_PATTERNS = {
    "页面开发": re.compile(r"(?:项目|网页)?页面(?:的)?(?:开发|实现)"),
}


def _literal_pattern(term: str) -> str:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    if term.isascii():
        return rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return escaped


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
        if not query_terms:
            return ResumeEvidenceSelection((), frozenset(), ())
        clause_terms = tuple(
            frozenset(terms) for clause in _CLAUSE_SEPARATOR.split(requirement_text)
            if (terms := self._query_terms(clause))
        )
        matches = list(self.retriever.retrieve(
            scope,
            knowledge_base_id,
            self._retrieval_query(query_terms),
            top_k=5,
            document_ids=[document_id],
            document_types=["resume"],
        ))
        # One absent anchor (e.g. PRD) must not suppress React/UI evidence.
        # Query each additional literal independently: the retriever may require
        # all terms in a combined query. Scope constraints apply to every call.
        anchor = self._retrieval_query(query_terms)
        queries = dict.fromkeys(alias for term in query_terms
                                for alias in _BILINGUAL_TERMS.get(term, (term,)))
        for term in query_terms:
            pattern = _CONTEXTUAL_PATTERNS.get(term)
            if pattern is not None:
                queries.update(dict.fromkeys(match.group("anchor") for match in pattern.finditer(markdown)))
        present_queries = [query for query in queries if query != anchor
                           and re.search(_literal_pattern(query), markdown, re.IGNORECASE)]
        for query in present_queries[:40]:
            matches.extend(self.retriever.retrieve(
                scope, knowledge_base_id, query if query.isascii() else query[:2],
                top_k=5, document_ids=[document_id], document_types=["resume"],
            ))
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
            aligned = self._align_block(chunk, blocks, query_terms, clause_terms)
            if aligned is None:
                continue
            block, covered = aligned
            coverage = len(covered) / max(1, len(query_terms))
            if not self._supports_clause(covered, clause_terms):
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
        terms: list[str] = []
        remaining = requirement_text.casefold()
        for term, pattern in _CHINESE_CONCEPT_PATTERNS.items():
            if pattern.search(remaining):
                terms.append(term)
                remaining = pattern.sub(" ", remaining)
        aliases = sorted(((alias, term) for term, values in _BILINGUAL_TERMS.items()
                          for alias in values), key=lambda pair: -len(pair[0]))
        for alias, term in aliases:
            pattern = _literal_pattern(alias)
            if re.search(pattern, remaining):
                terms.append(term)
                remaining = re.sub(pattern, " ", remaining)
        requirement_text = remaining
        for value in _ENGLISH_TERM.findall(requirement_text):
            normalized = _ENGLISH_ALIASES.get(value.casefold(), value.casefold())
            if normalized not in _ENGLISH_ACTION_TERMS:
                terms.append(normalized)
        for run in _CHINESE_RUN.findall(requirement_text):
            for value in _CHINESE_SEPARATOR.split(run):
                value = re.sub(r"^(?:根据|进行|完成|尝试将|尝试|探索|将|对|的|等|根据)+", "", value)
                cleaned = _TRAILING_BOILERPLATE.sub(
                    "", _LEADING_BOILERPLATE.sub("", value)
                )
                if len(cleaned) >= 2 and cleaned not in _IGNORED_TERMS:
                    terms.append(cleaned)
        return tuple(dict.fromkeys(terms))

    @staticmethod
    def _retrieval_query(query_terms: tuple[str, ...]) -> str:
        english = tuple(
            term for term in query_terms if term.isascii() and term[0].isalpha()
        )
        if english:
            return max(english, key=len)
        longest = max(query_terms, key=len)
        return longest[:2]

    def _blocks(self, markdown: str) -> tuple[_ResumeBlock, ...]:
        normalized = self.normalizer.normalize(markdown)
        lines = normalized.markdown.splitlines()
        excerpts = []
        for item in normalized.blocks:
            text = "\n".join(lines[item.start_line - 1:item.end_line])
            # Imported plain text may have no blank lines. Keep quotes local and
            # avoid counting skills far outside the cited chunk.
            for line in text.splitlines() if len(text) > 1000 else [text]:
                # Overlapping literal windows also handle a long single line;
                # each candidate must still fit wholly inside a stored chunk.
                excerpts.extend([line[start:start + 1000] for start in range(0, len(line), 800)]
                                if len(line) > 1000 else [line])
        return tuple(_ResumeBlock(i, text, self._searchable(text))
                     for i, text in enumerate(excerpts) if text and len(text) <= 1000)

    @classmethod
    def _supports_clause(
        cls, covered: frozenset[str], clause_terms: tuple[frozenset[str], ...],
    ) -> bool:
        # A local quote can substantiate one part of a compound responsibility.
        # The caller still reports coverage against ALL original query terms.
        # Generic actions are modifiers, not independent skill anchors. They
        # neither qualify a quote nor dilute its substantive clause coverage.
        anchors = (terms - _GENERIC_CONCEPTS for terms in clause_terms)
        return any(len(covered & terms) / len(terms) >= _MIN_CANDIDATE_COVERAGE
                   for terms in anchors if terms)

    @classmethod
    def _align_block(
        cls,
        chunk: KnowledgeChunk,
        blocks: tuple[_ResumeBlock, ...],
        query_terms: tuple[str, ...],
        clause_terms: tuple[frozenset[str], ...],
    ) -> tuple[_ResumeBlock, frozenset[str]] | None:
        chunk_text = cls._searchable(chunk.text)
        candidates: list[tuple[_ResumeBlock, frozenset[str]]] = []
        for block in blocks:
            if not block.searchable:
                continue
            if block.searchable not in chunk_text:
                continue
            covered = frozenset(
                term for term in query_terms
                if any(re.search(_literal_pattern(alias), block.text, re.IGNORECASE)
                       for alias in _BILINGUAL_TERMS.get(term, (term,)))
                or (term in _CONTEXTUAL_PATTERNS
                    and _CONTEXTUAL_PATTERNS[term].search(block.text))
            )
            if cls._supports_clause(covered, clause_terms):
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
