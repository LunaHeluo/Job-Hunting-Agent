from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from starter_agent.cv_workbench.tailoring_evidence import ResumeEvidenceSelector
from starter_agent.knowledge.models import KnowledgeChunk, KnowledgeScope, RetrievalMatch


KB_ID = UUID("00000000-0000-0000-0000-000000000101")
ACTIVE_DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000201")
OTHER_DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000202")
VERSION_ID = UUID("00000000-0000-0000-0000-000000000301")
REACT_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000401")
AI_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000402")
OTHER_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000403")
UNMAPPABLE_CHUNK_ID = UUID("00000000-0000-0000-0000-000000000404")

REACT_PROJECT_BLOCK = (
    "Academic Conference Program Committee Recommendation System："
    "使用 React 完成前端开发，对接推荐 API 进行系统联调，并落地前端 AI 应用。"
)
AI_RESEARCH_BLOCK = "参与 AI 研究课程，完成金融论文和模型实验。"
RESUME_MARKDOWN = f"""# 项目经历

{AI_RESEARCH_BLOCK}

{REACT_PROJECT_BLOCK}
"""


class RecordingRetriever:
    def __init__(self, matches: list[RetrievalMatch]) -> None:
        self.matches = matches
        self.calls: list[dict[str, object]] = []

    def retrieve(
        self,
        scope,
        knowledge_base_id,
        question,
        *,
        top_k,
        document_ids=None,
        document_types=None,
        filenames=None,
        versions=None,
    ):
        self.calls.append(
            {
                "scope": scope,
                "knowledge_base_id": knowledge_base_id,
                "question": question,
                "top_k": top_k,
                "document_ids": document_ids,
                "document_types": document_types,
                "filenames": filenames,
                "versions": versions,
            }
        )
        return self.matches


class ChunkReader:
    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self.chunks = {item.id: (item, "resume") for item in chunks}

    def get_chunks_by_ids(self, scope, knowledge_base_id, chunk_ids):
        return {
            chunk_id: self.chunks[chunk_id]
            for chunk_id in chunk_ids
            if chunk_id in self.chunks
        }


def chunk(
    chunk_id: UUID,
    text: str,
    *,
    document_id: UUID = ACTIVE_DOCUMENT_ID,
    ordinal: int = 0,
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=chunk_id,
        document_id=document_id,
        version_id=VERSION_ID,
        knowledge_base_id=KB_ID,
        user_id="local-user",
        project_id="ws_demo",
        version=1,
        filename="resume.md",
        section_path=["项目经历"],
        start_line=1,
        end_line=1,
        ordinal=ordinal,
        text=text,
        search_text=" ".join(text.casefold().split()),
        content_sha256=f"{chunk_id.int:064x}"[-64:],
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
    )


def match(item: KnowledgeChunk, *, rank: int) -> RetrievalMatch:
    return RetrievalMatch(
        chunk_id=item.id,
        document_id=item.document_id,
        document_type="resume",
        filename=item.filename,
        version=item.version,
        section_path=item.section_path,
        start_line=item.start_line,
        end_line=item.end_line,
        preview=item.text,
        source_ref=item.source_ref,
        matched_terms=[],
        rank=rank,
        created_at=item.created_at,
    )


def test_selector_scopes_retrieval_and_prefers_the_relevant_react_block() -> None:
    react = chunk(REACT_CHUNK_ID, REACT_PROJECT_BLOCK, ordinal=2)
    unrelated_ai = chunk(AI_CHUNK_ID, AI_RESEARCH_BLOCK, ordinal=1)
    other_resume = chunk(
        OTHER_CHUNK_ID,
        "React 前端开发 系统联调 前端 AI 应用",
        document_id=OTHER_DOCUMENT_ID,
    )
    retriever = RecordingRetriever(
        [
            match(other_resume, rank=1),
            match(unrelated_ai, rank=2),
            match(react, rank=3),
        ]
    )
    selector = ResumeEvidenceSelector(
        retriever=retriever,
        chunk_reader=ChunkReader([react, unrelated_ai, other_resume]),
    )

    selection = selector.select(
        requirement_text="负责前端开发、系统联调和前端 AI 应用，熟悉 React",
        scope=KnowledgeScope(user_id="local-user", project_id="ws_demo"),
        knowledge_base_id=KB_ID,
        document_id=ACTIVE_DOCUMENT_ID,
        markdown=RESUME_MARKDOWN,
    )

    assert retriever.calls == [
        {
            "scope": KnowledgeScope(user_id="local-user", project_id="ws_demo"),
            "knowledge_base_id": KB_ID,
            "question": "react",
            "top_k": 5,
            "document_ids": [ACTIVE_DOCUMENT_ID],
            "document_types": ["resume"],
            "filenames": None,
            "versions": None,
        }
    ]
    assert selection.evidence[0].quote == REACT_PROJECT_BLOCK
    assert selection.evidence[0].content_sha256 == react.content_sha256
    assert selection.evidence[0].source_ref == (
        f"knowledge-chunk://{KB_ID}/{REACT_CHUNK_ID}"
    )
    assert {"react", "前端开发", "系统联调"}.issubset(selection.covered_terms)
    assert all(ref.chunk_id != str(OTHER_CHUNK_ID) for ref in selection.evidence)


def test_selector_uses_a_skill_anchor_instead_of_requiring_english_action_words():
    python_api = chunk(
        UUID("00000000-0000-0000-0000-000000000405"),
        "负责 Python API engineering",
    )
    retriever = RecordingRetriever([match(python_api, rank=1)])
    selector = ResumeEvidenceSelector(
        retriever=retriever,
        chunk_reader=ChunkReader([python_api]),
    )

    selection = selector.select(
        requirement_text="Build Python APIs",
        scope=KnowledgeScope(user_id="local-user", project_id="ws_demo"),
        knowledge_base_id=KB_ID,
        document_id=ACTIVE_DOCUMENT_ID,
        markdown="负责 Python API engineering\n",
    )

    assert retriever.calls[0]["question"] == "python"
    assert selection.query_terms == ("python", "api")
    assert selection.covered_terms == {"python", "api"}
    assert selection.evidence[0].quote == "负责 Python API engineering"


def test_selector_rejects_unmapped_chunks_and_limits_distinct_blocks() -> None:
    block_texts = [
        "React 前端项目一，完成系统联调。",
        "React 前端项目二，完成组件开发。",
        "React 前端项目三，完成 AI 功能。",
        "React 前端项目四，完成性能优化。",
    ]
    markdown = "\n\n".join(block_texts) + "\n"
    block_chunks = [
        chunk(UUID(int=500 + index), text, ordinal=index)
        for index, text in enumerate(block_texts, start=1)
    ]
    unmappable = chunk(UNMAPPABLE_CHUNK_ID, "React 内容来自旧版本", ordinal=0)
    duplicate = match(block_chunks[0], rank=6).model_copy(update={"rank": 5})
    retriever = RecordingRetriever(
        [
            match(unmappable, rank=1),
            *(match(item, rank=index + 2) for index, item in enumerate(block_chunks)),
            duplicate,
        ]
    )
    selector = ResumeEvidenceSelector(
        retriever=retriever,
        chunk_reader=ChunkReader([unmappable, *block_chunks]),
    )

    selection = selector.select(
        requirement_text="React 前端开发",
        scope=KnowledgeScope(user_id="local-user", project_id="ws_demo"),
        knowledge_base_id=KB_ID,
        document_id=ACTIVE_DOCUMENT_ID,
        markdown=markdown,
    )

    assert len(selection.evidence) == 3
    assert len({item.quote for item in selection.evidence}) == 3
    assert all(item.chunk_id != str(UNMAPPABLE_CHUNK_ID) for item in selection.evidence)
