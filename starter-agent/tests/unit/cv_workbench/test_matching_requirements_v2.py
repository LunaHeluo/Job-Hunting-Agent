from __future__ import annotations

from uuid import UUID

from starter_agent.cv_workbench.contracts import EvidenceReference
from starter_agent.cv_workbench.matching import deterministic_requirements
from starter_agent.cv_workbench.tailoring_evidence import ResumeEvidenceSelection
from starter_agent.knowledge.models import KnowledgeScope


KB_ID = UUID("00000000-0000-0000-0000-000000000101")
DOCUMENT_ID = UUID("00000000-0000-0000-0000-000000000201")
SCOPE = KnowledgeScope(user_id="local-user", project_id="ws_demo")
EVIDENCE = EvidenceReference(
    chunk_id="chunk-react",
    source_ref=f"knowledge-chunk://{KB_ID}/chunk-react",
    content_sha256="a" * 64,
    quote="React 前端项目，完成系统联调。",
)


class FixedSelector:
    def __init__(self, selection: ResumeEvidenceSelection) -> None:
        self.selection = selection
        self.calls: list[dict[str, object]] = []

    def select(self, requirement_text, **context):
        self.calls.append({"requirement_text": requirement_text, **context})
        return self.selection


def selection(*, covered: tuple[str, ...], with_evidence: bool = True):
    return ResumeEvidenceSelection(
        query_terms=("react", "前端开发", "系统联调", "ai"),
        covered_terms=frozenset(covered),
        evidence=(EVIDENCE,) if with_evidence else (),
    )


def build(selector: FixedSelector, *, resume_text: str = "无相关项目"):
    return deterministic_requirements(
        resume_text,
        "负责 React 前端开发、系统联调和 AI 应用",
        selector=selector,
        scope=SCOPE,
        knowledge_base_id=KB_ID,
        document_id=DOCUMENT_ID,
    )[0]


def test_v2_marks_evidence_with_at_least_sixty_percent_core_coverage_matched():
    selector = FixedSelector(
        selection(covered=("react", "前端开发", "系统联调"))
    )

    requirement = build(selector)

    assert requirement.verdict == "matched"
    assert requirement.evidence == (EVIDENCE,)
    assert "3/4" in requirement.explanation
    assert selector.calls == [
        {
            "requirement_text": "负责 React 前端开发、系统联调和 AI 应用",
            "scope": SCOPE,
            "knowledge_base_id": KB_ID,
            "document_id": DOCUMENT_ID,
            "markdown": "无相关项目",
        }
    ]


def test_v2_marks_scoped_evidence_below_sixty_percent_partial():
    selector = FixedSelector(selection(covered=("react",)))

    requirement = build(selector)

    assert requirement.verdict == "partial"
    assert requirement.evidence == (EVIDENCE,)
    assert "1/4" in requirement.explanation


def test_v2_does_not_use_raw_resume_keyword_without_aligned_chunk_evidence():
    selector = FixedSelector(selection(covered=(), with_evidence=False))

    requirement = build(
        selector,
        resume_text="React 前端开发与系统联调项目真实存在，但选择器没有可信 chunk。",
    )

    assert requirement.verdict == "missing"
    assert requirement.evidence == ()
    assert "不会自动写入简历" in requirement.explanation
