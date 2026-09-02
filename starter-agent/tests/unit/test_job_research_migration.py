from types import SimpleNamespace
import ast
from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.domain.models import ChatResult
from starter_agent.delegation.job_research_compat import (
    LegacyJobResearchCompatibilityAdapter,
)
from starter_agent.interfaces.api import (
    ChatRequest,
    _candidate_search_query,
    _candidate_search_scope,
    _dispatch_classified_chat,
    _requires_delegated_job_research,
    _single_public_job_url,
)
from starter_agent.knowledge.routing import KnowledgeRequestDecision, KnowledgeRequestRoute
from starter_agent.capabilities.gate import ToolExecutionDenied


ROOT = Path(__file__).parents[2]


class _DelegatedApplication:
    def __init__(self) -> None:
        self.called = []
        self.delegation_store = SimpleNamespace(
            get_parent=lambda _run_id: SimpleNamespace(
                status="queued", phase="planning",
                budget_consumed=SimpleNamespace(model_dump=lambda **_kwargs: {}),
                created_at=None, started_at=None,
            ),
            get_run_tree=lambda _run_id: SimpleNamespace(child_runs=()),
        )

    def delegation_route_enabled(self) -> bool:
        return True

    async def start_job_research_delegation(self, **kwargs):
        self.called.append(kwargs)
        return SimpleNamespace(
            parent_run_id="parent:migration:1",
            child_task_id="task:migration:1",
            child_run_id="child-run:migration:1",
            contract_hash="a" * 64,
            effective_tool_view_hash="b" * 64,
        )


class _ExplodingLegacyApplication(_DelegatedApplication):
    async def prepare_job_research_request(self, **_kwargs):
        raise AssertionError("legacy fallback invoked")

    async def search_job_research_from_request(self, **_kwargs):
        raise AssertionError("legacy search invoked")

    async def analyze_job_research_candidates(self, **_kwargs):
        raise AssertionError("legacy browser invoked")

    async def search_job_candidates_once(self, **kwargs):
        self.called.append(kwargs)
        session_id = kwargs["session_id"] or uuid4()
        return session_id, uuid4(), SimpleNamespace(
            ok=True,
            error_code=None,
            data={
                "results": [
                    {
                        "title": "AI Agent Engineer",
                        "company": "Example Co",
                        "location": "Shanghai",
                        "url": "https://jobs.example.test/ai-agent-engineer",
                        "retrieved_at": "2026-08-15T00:00:00Z",
                    }
                ]
            },
        )

    async def read_public_job_page_once(self, **kwargs):
        self.called.append({"single_url": kwargs["url"]})
        return SimpleNamespace(
            session_id=kwargs["session_id"] or uuid4(),
            turn_id=uuid4(),
            requested_url=kwargs["url"],
            source_url=kwargs["url"],
            job={
                "title": "Associate Analytical Lead",
                "company": "Google",
                "location": "Shanghai",
                "responsibilities": ["Analyze customer growth"],
                "requirements": ["Analytical experience"],
                "validation_state": "verified",
            },
            partial=False,
            error_code=None,
            tool_calls=4,
            retrieval_method="playwright",
            attempts=(),
        )


@pytest.mark.asyncio
async def test_job_research_router_creates_only_persisted_web_child() -> None:
    application = _DelegatedApplication()
    request = ChatRequest(
        message="research and compare three backend jobs", session_id=uuid4()
    )
    result = await _dispatch_classified_chat(
        request,
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH,
            reason_code="fixture",
        ),
    )

    assert isinstance(result, ChatResult)
    assert result.parent_run_id == "parent:migration:1"
    assert result.child_task_id == "task:migration:1"
    assert result.child_run_id == "child-run:migration:1"
    assert result.legacy_path_used is False
    assert len(application.called) == 1
    assert application.called[0]["message"] == "research and compare three backend jobs"


@pytest.mark.asyncio
async def test_job_research_router_never_reaches_legacy_workflow() -> None:
    application = _ExplodingLegacyApplication()
    result = await _dispatch_classified_chat(
        ChatRequest(
            message="research and compare three backend jobs", session_id=uuid4()
        ),
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH,
            reason_code="fixture",
        ),
    )

    assert result.legacy_path_used is False
    assert len(application.called) == 1


@pytest.mark.asyncio
async def test_simple_search_uses_one_tool_loop_when_release_is_current() -> None:
    application = _ExplodingLegacyApplication()
    result = await _dispatch_classified_chat(
        ChatRequest(message="搜索深圳 AI Agent 岗位", session_id=uuid4()),
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH, reason_code="fixture",
        ),
    )

    assert result.route == "tool_loop_job_candidates"
    assert result.parent_run_id is None
    assert result.child_run_id is None
    assert result.tool_calls == 1
    assert result.task_card["release_gate_current"] is True
    assert result.task_card["fallback_reason"] is None
    assert result.task_card["delegation_started"] is False


@pytest.mark.asyncio
async def test_single_public_jd_url_uses_browser_tool_loop_without_search_or_child() -> None:
    application = _ExplodingLegacyApplication()
    url = (
        "https://www.google.com/about/careers/applications/jobs/results/"
        "102033425954677446-associate-analytical-lead/"
    )
    result = await _dispatch_classified_chat(
        ChatRequest(message=f"{url}读取JD", session_id=uuid4()),
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH,
            reason_code="fixture",
        ),
    )

    assert _single_public_job_url(f"{url}读取JD") == url
    assert application.called == [{"single_url": url}]
    assert result.route == "tool_loop_single_jd"
    assert result.parent_run_id is None
    assert result.child_run_id is None
    assert result.tool_calls == 4
    assert result.task_card["tool"] == "read_public_job_page"
    assert result.task_card["search_called"] is False
    assert result.task_card["browser_called"] is True
    assert result.task_card["delegation_started"] is False
    assert url in result.content


@pytest.mark.asyncio
async def test_job_research_router_uses_one_governed_candidate_search_without_current_release() -> None:
    application = _ExplodingLegacyApplication()
    application.delegation_route_enabled = lambda: False
    application.chat = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("disabled release must not ask a tool-free model to guess")
    )

    result = await _dispatch_classified_chat(
        ChatRequest(message="find backend jobs", session_id=uuid4()),
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH, reason_code="fixture",
        ),
    )

    assert len(application.called) == 1
    assert application.called[0]["query"] == "find backend jobs"
    assert result.route == "tool_loop_job_candidates"
    assert result.legacy_path_used is False
    assert result.tool_calls == 1
    assert result.task_card["fallback_reason"] == "delegation_release_gate_not_current"
    assert result.task_card["browser_called"] is False
    assert result.task_card["delegation_started"] is False
    assert result.task_card["legacy_path_used"] is False
    assert result.task_card["source_verification"] == "pending"
    assert "https://jobs.example.test/ai-agent-engineer" in result.content


@pytest.mark.asyncio
async def test_job_research_candidate_fallback_stops_when_existing_gate_denies_tool() -> None:
    application = _ExplodingLegacyApplication()
    application.delegation_route_enabled = lambda: False

    async def denied(**_kwargs):
        raise ToolExecutionDenied("tool_disabled")

    application.search_job_candidates_once = denied
    result = await _dispatch_classified_chat(
        ChatRequest(message="find Shanghai AI Agent jobs", session_id=uuid4()),
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH, reason_code="fixture",
        ),
    )

    assert result.route == "single_agent_candidate_disabled"
    assert result.tool_calls == 0
    assert result.task_card["reason"] == "candidate_tool_denied:tool_disabled"
    assert result.task_card["fallback"] == "stop"
    assert "Pre-Tool-Call Gate" in result.content
    assert application.called == []


def test_candidate_search_query_removes_orchestration_words_not_job_intent() -> None:
    assert _candidate_search_query("根据我的简历搜索上海的 AI Agent 相关岗位") == (
        "上海的 AI Agent jobs"
    )
    assert _candidate_search_query("find backend jobs") == "find backend jobs"
    assert _candidate_search_query("根据我的简历搜索上海的ai agent相关岗位") == (
        "上海的 AI Agent jobs"
    )
    assert _candidate_search_scope("根据我的简历搜索上海的ai agent相关岗位") == (
        "AI Agent jobs",
        "上海",
    )


@pytest.mark.parametrize(
    "message",
    (
        "调研并比较这几个岗位，搜索上海 AI Agent 岗位并结合简历排序",
        "搜索上海 AI Agent 岗位并结合我的简历排序",
        "打开前三个岗位的完整 JD",
        "compare and rank three AI Agent jobs against my resume",
        "比较 https://jobs.example.test/one 和 https://jobs.example.test/two",
    ),
)
def test_complex_job_research_requires_delegation(message: str) -> None:
    assert _requires_delegated_job_research(message) is True


@pytest.mark.parametrize(
    "message",
    (
        "搜索上海 AI Agent 岗位",
        "find backend jobs",
        "读取 https://jobs.example.test/one",
    ),
)
def test_simple_candidate_search_or_single_url_does_not_require_delegation(
    message: str,
) -> None:
    assert _requires_delegated_job_research(message) is False


@pytest.mark.asyncio
async def test_complex_job_research_waits_without_starting_search_when_release_is_blocked() -> None:
    application = _ExplodingLegacyApplication()
    application.delegation_route_enabled = lambda: False
    result = await _dispatch_classified_chat(
        ChatRequest(
            message="调研并比较这几个岗位，搜索上海 AI Agent 岗位并结合简历排序",
            session_id=uuid4(),
        ),
        application=application,
        route=KnowledgeRequestDecision(
            route=KnowledgeRequestRoute.JOB_RESEARCH,
            reason_code="fixture",
        ),
    )

    assert application.called == []
    assert result.route == "plan_delegation"
    assert result.run_status == "waiting"
    assert result.tool_calls == 0
    assert result.task_card["status"] == "waiting"
    assert result.task_card["tool_calls_started"] is False
    assert result.task_card["delegation_started"] is False
    assert result.task_card["pending_action"] == (
        "publish_delegation_release_decision"
    )
    assert "没有调用 SerpAPI、Browser 或 Subagent" in result.content


async def _plain_chat_result() -> ChatResult:
    return ChatResult(
        session_id=uuid4(), turn_id=uuid4(), content="plain agent",
        provider="fixture", model="fixture", knowledge_mode="off",
    )


def test_compatibility_adapter_reads_pending_run_without_tools_or_writes() -> None:
    parent = SimpleNamespace(id="parent:1", principal="user:1", route="delegated_job_research")
    task = SimpleNamespace(id="task:1", specialist_id="job_web_researcher", accepted_child_run_id=None, accepted_result_envelope_ref=None)
    store = SimpleNamespace(get_run_tree=lambda value: SimpleNamespace(parent=parent, child_tasks=(task,)))
    artifacts = SimpleNamespace(get_tool_artifact_for_principal=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read missing envelope")))

    result = LegacyJobResearchCompatibilityAdapter(
        run_store=store, artifact_store=artifacts
    ).to_chat_result(
        parent_run_id="parent:1", session_id=uuid4(), provider="mock", model="mock"
    )

    assert result.content == "岗位调研任务仍在处理中。"
    assert result.parent_run_id == "parent:1"
    assert result.child_task_id == "task:1"
    assert result.child_run_id is None
    assert result.tool_calls == 0
    assert result.legacy_path_used is False


def test_compatibility_adapter_keeps_legacy_card_fields_from_accepted_envelope() -> None:
    parent = SimpleNamespace(id="parent:1", principal="user:1", route="delegated_job_research")
    task = SimpleNamespace(id="task:1", specialist_id="job_web_researcher", accepted_child_run_id="child:1", accepted_result_envelope_ref="artifact:1")
    store = SimpleNamespace(get_run_tree=lambda value: SimpleNamespace(parent=parent, child_tasks=(task,)))
    artifacts = SimpleNamespace(get_tool_artifact_for_principal=lambda *_args, **_kwargs: {"content": '{"status":"partial","output":{"jobs":[{}],"missing":["location"]}}'})

    result = LegacyJobResearchCompatibilityAdapter(
        run_store=store, artifact_store=artifacts
    ).to_chat_result(
        parent_run_id="parent:1", session_id=uuid4(), provider="mock", model="mock"
    )

    assert "部分完成" in result.content
    assert "已验证 JD 1 个" in result.content
    assert result.child_run_id == "child:1"
    assert result.route == "delegated_job_research"
    assert result.tool_calls == 0


def test_default_job_research_router_and_frontend_do_not_reference_old_workflow() -> None:
    api_path = ROOT  / "backend" / "src" / "starter_agent" / "interfaces" / "api.py"
    tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
    dispatch = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_classified_chat"
    )
    names = {
        node.id for node in ast.walk(dispatch) if isinstance(node, ast.Name)
    }
    assert "_chat_with_public_job_search_fallback" not in names
    assert "JobResearchOrchestrator" not in names
    for path in (ROOT  / "frontend" / "web").rglob("*.*"):
        if path.suffix not in {".js", ".ts", ".tsx", ".jsx", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "_chat_with_public_job_search_fallback" not in text
        assert "JobResearchOrchestrator" not in text
