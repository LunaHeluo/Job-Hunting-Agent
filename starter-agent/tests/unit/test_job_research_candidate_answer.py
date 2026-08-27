from starter_agent.domain.models import ToolResult
from types import SimpleNamespace
from uuid import uuid4

from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.interfaces.api import (
    _persist_visible_job_candidates,
    _public_job_search_answer,
)


def _search_result() -> ToolResult:
    return ToolResult(
        ok=True,
        data={
            "results": [],
            "planned_queries": ["北京 AI Agent 招聘", "Beijing AI Agent jobs"],
            "request_count": 4,
            "raw_result_count": 20,
            "deduplicated_count": 12,
            "filtered_collection_count": 3,
            "chinese_title_count": 7,
        },
    )


def _complete_job() -> dict[str, object]:
    return {
        "title": "AI智能体开发工程师",
        "company": "示例科技",
        "location": "北京",
        "responsibilities": [f"职责 {index}" for index in range(1, 5)],
        "requirements": [f"要求 {index}" for index in range(1, 7)],
        "analysis": [
            {"status": "matched", "requirement": "Python"},
            {"status": "matched", "requirement": "RAG"},
            {"status": "gap", "requirement": "LangGraph"},
        ],
        "source_url": "https://careers.example.test/job/42",
        "candidate_id": "550e8400-e29b-41d4-a716-446655440000",
        "selection_status": "PENDING_CONFIRMATION",
        "retrieval_method": "playwright",
    }


def test_complete_jd_uses_selectable_candidate_template_and_all_sections() -> None:
    answer = _public_job_search_answer(
        search_result=_search_result(),
        jd_result=ToolResult(
            ok=True,
            data={
                "jobs": [_complete_job()],
                "partial_jobs": [],
                "candidate_attempts": [],
                "candidate_limit": 10,
                "target_count": 1,
            },
        ),
    )

    assert "# Candidate 1：AI智能体开发工程师" in answer
    assert "## 岗位概览" in answer
    assert "- 公司：示例科技" in answer
    assert "- 岗位：AI智能体开发工程师" in answer
    assert "- 地点：北京" in answer
    assert "- 来源：招聘详情页" in answer
    assert "https://careers.example.test/job/42" in answer
    assert "- 读取状态：已读取完整 JD 核心字段" in answer
    assert "- Candidate ID：`550e8400-e29b-41d4-a716-446655440000`" in answer
    assert "- 状态：`PENDING_CONFIRMATION`" in answer
    assert "## 职责摘录" in answer
    assert "- 职责 4" in answer
    assert "## 任职要求" in answer
    assert "- 要求 6" in answer
    assert "## 简历匹配概览" in answer
    assert "- 匹配项：2" in answer
    assert "- 证据缺口：1" in answer
    assert answer.splitlines()[-1].startswith("请选择 Candidate 编号或 Candidate ID")


def test_missing_company_uses_evidence_aware_labels() -> None:
    complete = _complete_job()
    complete["company"] = ""
    answer = _public_job_search_answer(
        search_result=_search_result(),
        jd_result=ToolResult(
            ok=True,
            data={
                "jobs": [complete],
                "partial_jobs": [
                    {
                        "title": "Partial AI Agent Engineer",
                        "company": "",
                        "location": "北京",
                        "source_url": "https://partial.example.test/job/7",
                        "snippet": (
                            "岗位职责：开发智能体应用。"
                            "任职要求：熟悉 Python 和大模型。"
                        ),
                    }
                ],
                "candidate_attempts": [],
                "candidate_limit": 10,
                "target_count": 2,
            },
        ),
    )

    assert "- 公司：公司未披露" in answer
    assert "Partial AI Agent Engineer · 公司未核实 · 北京" in answer
    assert "未知公司" not in answer


def test_failed_and_partial_urls_are_hidden_when_complete_target_is_met() -> None:
    failed_url = "https://blocked.example.test/job/blocked"
    partial_url = "https://partial.example.test/job/partial"
    answer = _public_job_search_answer(
        search_result=_search_result(),
        jd_result=ToolResult(
            ok=True,
            data={
                "jobs": [_complete_job()],
                "partial_jobs": [{
                    "title": "Partial AI Job",
                    "source_url": partial_url,
                    "snippet": "岗位职责：开发智能体。",
                }],
                "candidate_attempts": [{
                    "source_url": failed_url,
                    "status": "failed",
                    "final_error_code": "access_blocked_403",
                }],
                "candidate_limit": 10,
                "target_count": 1,
            },
        ),
    )

    assert "# Candidate 1：" in answer
    assert partial_url not in answer
    assert failed_url not in answer
    assert "access_blocked_403" not in answer
    assert "无法访问" not in answer


def test_substantive_partial_evidence_only_appears_when_complete_target_unmet() -> None:
    useful_url = "https://partial.example.test/job/useful"
    thin_url = "https://partial.example.test/job/thin"
    failed_url = "https://blocked.example.test/job/blocked"
    answer = _public_job_search_answer(
        search_result=_search_result(),
        jd_result=ToolResult(
            ok=True,
            data={
                "jobs": [],
                "partial_jobs": [
                    {
                        "title": "AI Agent Engineer",
                        "company": "Example",
                        "location": "北京",
                        "source_url": useful_url,
                        "snippet": (
                            "岗位职责：开发智能体应用。"
                            "任职要求：熟悉 Python 和大模型。"
                        ),
                    },
                    {
                        "title": "Jobs",
                        "source_url": thin_url,
                        "snippet": "招聘",
                    },
                ],
                "candidate_attempts": [{
                    "source_url": failed_url,
                    "status": "failed",
                    "final_error_code": "playwright_timeout",
                }],
                "candidate_limit": 10,
                "target_count": 3,
            },
        ),
    )

    assert "未取得完整 JD" in answer
    assert "部分证据（1 个）" in answer
    assert useful_url in answer
    assert "岗位职责：开发智能体应用" in answer
    assert thin_url not in answer
    assert failed_url not in answer


def test_zero_visible_evidence_keeps_message_without_failed_urls() -> None:
    answer = _public_job_search_answer(
        search_result=_search_result(),
        jd_result=ToolResult(
            ok=False,
            data={
                "jobs": [],
                "partial_jobs": [],
                "candidate_attempts": [{
                    "source_url": "https://blocked.example.test/job/1",
                    "status": "failed",
                    "final_error_code": "robots_blocked",
                }],
                "candidate_limit": 10,
                "target_count": 3,
            },
            display="未读取到可用岗位内容。",
            error_code="job_description_unverified",
        ),
    )

    assert "未读取到可用岗位内容" in answer
    assert "https://blocked.example.test/job/1" not in answer
    assert "robots_blocked" not in answer
    assert "搜索：2 个查询变体 · 4 次 SerpAPI 请求" in answer


def test_only_visible_partial_evidence_is_persisted_as_selectable(tmp_path) -> None:
    store = SQLiteSessionStore("sqlite:///sessions.db", tmp_path)
    session_id = store.create_session()
    application = SimpleNamespace(store=store)

    enriched = _persist_visible_job_candidates(
        application,
        session_id=session_id,
        turn_id=uuid4(),
        jd_result=ToolResult(
            ok=True,
            data={
                "jobs": [],
                "partial_jobs": [
                    {
                        "title": "Useful AI Agent Job",
                        "source_url": "https://example.test/job/useful",
                        "snippet": (
                            "岗位职责：开发智能体应用。"
                            "任职要求：熟悉 Python 和大模型。"
                        ),
                    },
                    {
                        "title": "Thin result",
                        "source_url": "https://example.test/job/thin",
                        "snippet": "招聘",
                    },
                ],
            },
        ),
    )

    assert enriched is not None
    assert len(enriched.data["partial_jobs"]) == 1
    stored = store.list_pending_job_candidates(session_id)
    assert [item.title for item in stored] == ["Useful AI Agent Job"]
    assert stored[0].ordinal == 1
