from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

import starter_agent.interfaces.api as api_module
from starter_agent.domain.models import ChatResult, ToolResult
from starter_agent.knowledge.errors import KnowledgeError
from starter_agent.knowledge.models import RagAnswer, RetrievalMatch
from starter_agent.knowledge.routing import (
    KnowledgeRequestDecision,
    KnowledgeRequestRoute,
)
from starter_agent.infrastructure.session_store import SQLiteSessionStore
from starter_agent.skills.models import SkillRunResult, SkillToolTrace


_FROZEN_SINGLE_AGENT_BASELINE_CASES = {
    "test_public_search_persists_visible_candidate_for_follow_up",
    "test_chat_required_falls_back_to_direct_public_job_tools_without_forcing_model",
    "test_job_research_reports_browser_dependency_without_claiming_jd_attempts",
    "test_chat_distinguishes_invalid_profile_json_from_missing_resume_evidence",
    "test_chat_required_uses_public_search_for_explicit_online_job_query",
    "test_buffered_job_stream_relays_confirmation_before_final_result",
    "test_chat_auto_job_request_uses_unified_job_research_route",
    "test_job_research_uses_knowledge_answer_when_saved_jd_exists",
    "test_job_research_matches_metadata_from_sibling_chunks",
    "test_job_research_falls_back_to_user_request_when_profile_query_misses_saved_jd",
    "test_saved_job_match_survives_invalid_rag_generation_without_web_fallback",
    "test_saved_jd_location_mismatch_uses_prepared_batch_fallback",
    "test_explicit_knowledge_only_job_request_does_not_fall_back_to_web",
    "test_job_research_appends_without_rewriting_existing_history",
    "test_job_research_failure_paths_append_one_turn_with_stable_ids",
}


@pytest.fixture(autouse=True)
def _route_retired_job_workflow_cases_through_the_frozen_baseline(
    request,
    monkeypatch,
):
    """Keep historical assertions as baseline evidence, never as an API fallback."""
    if request.node.name not in _FROZEN_SINGLE_AGENT_BASELINE_CASES:
        return

    async def run_frozen_baseline(
        chat_request,
        *,
        application,
        route,
        on_tool_event=None,
    ):
        del route
        knowledge = (
            api_module.create_knowledge_service()
            if chat_request.knowledge_base_id is not None
            and chat_request.knowledge_mode != "off"
            else None
        )
        return await api_module._chat_with_public_job_search_fallback(
            chat_request,
            application=application,
            knowledge=knowledge,
            on_tool_event=on_tool_event,
        )

    monkeypatch.setattr(api_module, "_dispatch_classified_chat", run_frozen_baseline)


class FakeKnowledge:
    def __init__(self, *, job_matches=None):
        self.calls = []
        self.retrieve_calls = []
        self.job_matches = list(job_matches or [])

    def retrieve(self, knowledge_base_id, question, **kwargs):
        self.retrieve_calls.append((knowledge_base_id, question, kwargs))
        return list(self.job_matches)

    async def answer(self, knowledge_base_id, question, **kwargs):
        self.calls.append((knowledge_base_id, question, kwargs))
        return RagAnswer(
            status="refused",
            answer="知识库中没有足够证据回答该问题。",
            refusal_reason="no_evidence",
        )


class FakeAnsweredKnowledge(FakeKnowledge):

    async def answer(self, knowledge_base_id, question, **kwargs):
        self.calls.append((knowledge_base_id, question, kwargs))
        return RagAnswer(
            status="answered",
            answer="知识库中已有答案。",
            refusal_reason=None,
        )


class FakeFallbackJobKnowledge(FakeAnsweredKnowledge):
    def __init__(self, *, fallback_matches):
        super().__init__(job_matches=[])
        self.fallback_matches = list(fallback_matches)

    def retrieve(self, knowledge_base_id, question, **kwargs):
        self.retrieve_calls.append((knowledge_base_id, question, kwargs))
        if len(self.retrieve_calls) == 1:
            return []
        return list(self.fallback_matches)


class FakeInvalidGeneratedJobKnowledge(FakeAnsweredKnowledge):
    async def answer(self, knowledge_base_id, question, **kwargs):
        self.calls.append((knowledge_base_id, question, kwargs))
        raise KnowledgeError("generation_invalid_output")


class FakeChunkedJobKnowledge(FakeAnsweredKnowledge):
    def __init__(self, *, job_match):
        super().__init__(job_matches=[job_match])
        self.chunk_calls = []

    def list_chunks(
        self,
        knowledge_base_id,
        document_id,
        *,
        after_ordinal,
        limit,
    ):
        self.chunk_calls.append(
            (knowledge_base_id, document_id, after_ordinal, limit)
        )
        if after_ordinal >= 1:
            return []
        return [
            SimpleNamespace(
                ordinal=0,
                text=(
                    "# Python Backend Engineer\n"
                    "- Location: 深圳\n"
                    "- Status: open\n"
                    "- Source URL: https://jobs.example.test/chunked\n"
                ),
            ),
            SimpleNamespace(
                ordinal=1,
                text="## Requirements\nPython backend engineer\n",
            ),
        ]


class FakeStore:
    def __init__(self):
        self.session_id = uuid4()
        self.messages = []

    def ensure_session(self, session_id=None):
        return session_id or self.session_id

    def add_message(self, session_id, turn_id, message):
        self.messages.append((session_id, turn_id, message))
        return uuid4()


class FakeRuntime:
    def __init__(self):
        self.calls = []

    async def execute_tool(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError(kwargs["tool_name"])


class FakeApplication:
    def __init__(self, route=KnowledgeRequestRoute.JOB_RESEARCH):
        self.chat_called = False
        self.chat_calls = []
        self.store = FakeStore()
        self.runtime = FakeRuntime()
        self.job_research_calls = []
        self.job_analysis_calls = []
        self.prepare_calls = []
        self.prepared_search_calls = []
        self.candidate_analysis_calls = []
        self.route = route
        self.route_calls = []

    async def route_knowledge_request(self, **kwargs):
        self.route_calls.append(kwargs)
        return KnowledgeRequestDecision(
            route=self.route,
            reason_code="fixture_route",
        )

    async def search_job_research_from_request(self, **kwargs):
        self.job_research_calls.append(kwargs)
        search_result = ToolResult(
            ok=True,
            data={
                "results": [
                    {
                        "title": "Python Backend Engineer",
                        "company": "Example",
                        "url": "https://jobs.example.com/agent",
                    }
                ]
            },
        )
        return SkillRunResult(
            status="waiting_for_url_selection",
            data={"results": search_result.data["results"]},
            trace=(
                SkillToolTrace(
                    tool_name="retrieve_resume_evidence",
                    call_id="evidence-1",
                    arguments={"query": "resume-backed profile"},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
                SkillToolTrace(
                    tool_name="search_jobs_serpapi",
                    call_id="search-1",
                    arguments={
                        "query": "Python backend engineer",
                        "location": "深圳",
                        "limit": 3,
                    },
                    result=search_result.model_dump(mode="json"),
                    gate_outcome="allow",
                ),
            ),
        )

    async def prepare_job_research_request(self, **kwargs):
        self.prepare_calls.append(kwargs)
        explicit = any(
            marker in kwargs["user_request"]
            for marker in ("最新", "网上", "当前招聘")
        )
        return SkillRunResult(
            status="search_profile_ready",
            data={
                "search_profile": {
                    "query": "Python backend engineer",
                    "location": "深圳",
                    "evidence_refs": ["E1"],
                    "explicit_freshness": explicit,
                    "role_terms": ["Python", "backend", "engineer"],
                },
                "resume_evidence": [
                    {
                        "quote": "Built Python agent systems",
                        "source_ref": "resume.md@v1#L1-L1",
                    }
                ],
            },
            trace=(),
        )

    async def search_prepared_job_research(self, **kwargs):
        self.prepared_search_calls.append(kwargs)
        return await self.search_job_research_from_request(
            user_request="prepared",
            session_id=kwargs["session_id"],
            turn_id=kwargs["turn_id"],
            limit=kwargs["limit"],
        )

    async def analyze_job_research_candidates(self, **kwargs):
        self.candidate_analysis_calls.append(kwargs)
        source_url = kwargs["candidates"][0].url
        job = {
            "title": "AI Agent Engineer",
            "company": "Example",
            "location": "深圳",
            "responsibilities": ["Build AI agents"],
            "requirements": ["Python"],
            "source_url": source_url,
            "retrieved_at": "2026-07-27T00:00:00Z",
        }
        return SkillRunResult(
            status="waiting_for_jd_ingestion_confirmation",
            data={
                "job": job,
                "jobs": [job],
                "analysis": [],
                "candidate_attempts": [
                    {"source_url": source_url, "status": "succeeded"}
                ],
            },
            trace=(
                SkillToolTrace(
                    tool_name="mcp__playwright__browser_navigate",
                    call_id="navigate-batch-1",
                    arguments={"url": source_url},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
                SkillToolTrace(
                    tool_name="mcp__playwright__browser_snapshot",
                    call_id="snapshot-batch-1",
                    arguments={},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
            ),
        )

    async def analyze_job_research(self, **kwargs):
        self.job_analysis_calls.append(kwargs)
        source_url = kwargs["selected_url"]
        return SkillRunResult(
            status="waiting_for_jd_ingestion_confirmation",
            data={
                "job": {
                    "title": "AI Agent Engineer",
                    "company": "Example",
                    "location": "深圳",
                    "responsibilities": ["Build AI agents"],
                    "requirements": ["Python"],
                    "source_url": source_url,
                    "retrieved_at": "2026-07-27T00:00:00Z",
                },
                "analysis": [],
            },
            trace=(
                SkillToolTrace(
                    tool_name="mcp__playwright__browser_navigate",
                    call_id="navigate-1",
                    arguments={"url": source_url},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
                SkillToolTrace(
                    tool_name="mcp__playwright__browser_snapshot",
                    call_id="snapshot-1",
                    arguments={},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
                SkillToolTrace(
                    tool_name="retrieve_resume_evidence",
                    call_id="evidence-2",
                    arguments={"query": kwargs["query"], "top_k": 6},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
            ),
        )
    async def chat(self, **kwargs):
        self.chat_called = True
        self.chat_calls.append(kwargs)
        raise AssertionError("public job fallback must not rely on model tool_choice")

    async def wait_for_background_tasks(self):
        return None


class FakeInvalidProfileApplication(FakeApplication):
    async def prepare_job_research_request(self, **kwargs):
        self.prepare_calls.append(kwargs)
        return SkillRunResult(
            status="search_profile_required",
            error_code="invalid_json",
            trace=(
                SkillToolTrace(
                    tool_name="retrieve_resume_evidence",
                    call_id="evidence-1",
                    arguments={"query": "resume-backed profile"},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
            ),
        )

    async def search_job_research_from_request(self, **kwargs):
        self.job_research_calls.append(kwargs)
        return SkillRunResult(
            status="search_profile_required",
            error_code="invalid_json",
            trace=(
                SkillToolTrace(
                    tool_name="retrieve_resume_evidence",
                    call_id="evidence-1",
                    arguments={"query": "resume-backed profile"},
                    result={"ok": True},
                    gate_outcome="allow",
                ),
            ),
        )


class FakeSearchFailureApplication(FakeApplication):
    async def search_prepared_job_research(self, **kwargs):
        self.prepared_search_calls.append(kwargs)
        return SkillRunResult(
            status="search_failed",
            error_code="serpapi_unavailable",
            data={},
            trace=(),
        )


class FakeNoCandidateApplication(FakeApplication):
    async def search_prepared_job_research(self, **kwargs):
        self.prepared_search_calls.append(kwargs)
        return SkillRunResult(
            status="waiting_for_url_selection",
            data={"results": []},
            trace=(),
        )


class FakeConfirmationJobApplication(FakeApplication):
    async def search_prepared_job_research(self, **kwargs):
        await kwargs["on_tool_event"](
            {
                "type": "confirmation_required",
                "confirmation_id": "confirmation-job-search-1",
                "session_id": str(kwargs["session_id"]),
                "turn_id": str(kwargs["turn_id"]),
                "call_id": "skill-job-search-1",
                "server_id": "builtin",
                "tool_name": "search_jobs_serpapi",
                "arguments_summary": {"query": "[REDACTED]"},
                "risk": "read",
                "destination": "serpapi",
                "expires_at": "2026-07-29T16:00:00Z",
                "revision": 0,
                "status": "pending",
            }
        )
        return await super().search_prepared_job_research(**kwargs)


class FakePlaywrightFailureApplication(FakeApplication):
    async def analyze_job_research_candidates(self, **kwargs):
        self.candidate_analysis_calls.append(kwargs)
        return SkillRunResult(
            status="job_description_unverified",
            error_code="playwright_failed",
            data={
                "jobs": [],
                "candidate_attempts": [
                    {
                        "source_url": kwargs["candidates"][0].url,
                        "status": "failed",
                    }
                ],
            },
            trace=(),
        )


class FakePlaywrightUnavailableApplication(FakeApplication):
    async def analyze_job_research_candidates(self, **kwargs):
        self.candidate_analysis_calls.append(kwargs)
        return SkillRunResult(
            status="dependency_unavailable",
            error_code="dependency_unavailable",
            missing_dependencies=(
                "mcp:mcp__playwright__browser_navigate",
                "mcp:mcp__playwright__browser_snapshot",
            ),
            data={},
            trace=(),
        )


class FakeConversationApplication(FakeApplication):
    def __init__(self):
        super().__init__(route=KnowledgeRequestRoute.CONVERSATION)

    async def chat(self, **kwargs):
        self.chat_called = True
        self.chat_calls.append(kwargs)
        return ChatResult(
            session_id=kwargs.get("session_id") or self.store.session_id,
            turn_id=uuid4(),
            content="你好！有什么可以帮你？",
            provider=kwargs.get("provider_name") or "mock",
            model=kwargs.get("model") or "starter-mock",
        )


def _pending_complete_job(title: str, source_url: str) -> dict[str, object]:
    return {
        "title": title,
        "company": "北京汽车研究总院",
        "location": "北京-大兴区, 北京, CN",
        "responsibilities": ["负责 AI Agent 核心算法研发"],
        "requirements": ["熟悉 Python 和 LangGraph"],
        "analysis": [
            {"status": "matched", "requirement": "Python"},
            {"status": "gap", "requirement": "LangGraph"},
        ],
        "source_url": source_url,
        "evidence_level": "complete",
    }


def test_pending_job_selection_precedes_classifier_and_knowledge(
    monkeypatch,
    tmp_path,
) -> None:
    app_service = FakeApplication(route=KnowledgeRequestRoute.KNOWLEDGE_QUERY)
    app_service.store = SQLiteSessionStore("sqlite:///sessions.db", tmp_path)
    session_id = app_service.store.create_session()
    app_service.store.replace_pending_job_candidates(
        session_id=session_id,
        turn_id=uuid4(),
        candidates=[
            _pending_complete_job(
                "AI智能体开发工程师",
                "https://careers.example.test/jobs/agent-1",
            ),
            _pending_complete_job(
                "Senior AI Engineer",
                "https://careers.example.test/jobs/agent-2",
            ),
        ],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    knowledge = FakeAnsweredKnowledge()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "选择第一个岗位做匹配分析",
                "session_id": str(session_id),
                "knowledge_mode": "auto",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    content = response.json()["content"]
    assert "AI智能体开发工程师" in content
    assert "https://careers.example.test/jobs/agent-1" in content
    assert "Senior AI Engineer" not in content
    assert app_service.route_calls == []
    assert app_service.prepare_calls == []
    assert knowledge.calls == []


def test_missing_pending_selection_does_not_fall_back_to_knowledge(
    monkeypatch,
    tmp_path,
) -> None:
    app_service = FakeApplication(route=KnowledgeRequestRoute.KNOWLEDGE_QUERY)
    app_service.store = SQLiteSessionStore("sqlite:///sessions.db", tmp_path)
    session_id = app_service.store.create_session()
    knowledge = FakeAnsweredKnowledge()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "选择第一个岗位做匹配分析",
                "session_id": str(session_id),
                "knowledge_mode": "auto",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert "候选已失效" in response.json()["content"]
    assert knowledge.calls == []
    assert app_service.route_calls == []


def test_stream_pending_job_selection_precedes_classifier(
    monkeypatch,
    tmp_path,
) -> None:
    app_service = FakeApplication(route=KnowledgeRequestRoute.KNOWLEDGE_QUERY)
    app_service.store = SQLiteSessionStore("sqlite:///sessions.db", tmp_path)
    session_id = app_service.store.create_session()
    app_service.store.replace_pending_job_candidates(
        session_id=session_id,
        turn_id=uuid4(),
        candidates=[
            _pending_complete_job(
                "AI智能体开发工程师",
                "https://careers.example.test/jobs/agent-1",
            )
        ],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        with client.stream(
            "POST",
            "/v1/chat/stream",
            json={
                "message": "选择第一个岗位做匹配分析",
                "session_id": str(session_id),
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "AI智能体开发工程师" in body
    assert "https://careers.example.test/jobs/agent-1" in body
    assert app_service.route_calls == []


def test_public_search_persists_visible_candidate_for_follow_up(
    monkeypatch,
    tmp_path,
) -> None:
    app_service = FakeApplication()
    app_service.store = SQLiteSessionStore("sqlite:///sessions.db", tmp_path)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        search = client.post(
            "/v1/chat",
            json={"message": "根据我的简历搜索北京的岗位"},
        )
        session_id = search.json()["session_id"]
        selection = client.post(
            "/v1/chat",
            json={
                "message": "选择第一个岗位做匹配分析",
                "session_id": session_id,
            },
        )

    assert search.status_code == 200
    assert selection.status_code == 200
    assert "AI Agent Engineer" in selection.json()["content"]
    assert "https://jobs.example.com/agent" in selection.json()["content"]
    assert len(app_service.prepare_calls) == 1


def test_chat_required_falls_back_to_direct_public_job_tools_without_forcing_model(
    monkeypatch,
) -> None:
    app_service = FakeApplication()
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: FakeKnowledge())
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历查询深圳的岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
                "provider": "zhipu",
                "model": "glm-4.7",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["knowledge_mode"] == "required"
    assert body["provider"] == "zhipu"
    assert body["model"] == "glm-4.7"
    assert body["tool_calls"] == 4
    assert "# Candidate 1：AI Agent Engineer" in body["content"]
    assert "AI Agent Engineer" in body["content"]
    assert "https://jobs.example.com/agent" in body["content"]
    assert "## 任职要求" in body["content"]
    assert "- Python" in body["content"]
    assert app_service.chat_called is False
    assert len(app_service.prepare_calls) == 1
    assert app_service.prepare_calls[0]["user_request"] == (
        "根据我的简历查询深圳的岗位"
    )
    assert app_service.prepare_calls[0]["provider_name"] == "zhipu"
    assert app_service.prepare_calls[0]["model"] == "glm-4.7"
    assert len(app_service.prepared_search_calls) == 1
    assert app_service.runtime.calls == []
    assert len(app_service.candidate_analysis_calls) == 1
    assert app_service.candidate_analysis_calls[0]["candidates"][0].url == (
        "https://jobs.example.com/agent"
    )


def test_job_research_reports_browser_dependency_without_claiming_jd_attempts(
    monkeypatch,
) -> None:
    app_service = FakePlaywrightUnavailableApplication()
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: FakeKnowledge())
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历查询成都的岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    content = response.json()["content"]
    assert "dependency_unavailable" in content
    assert "job_description_unverified" not in content
    assert "已检查候选 0" in content


def test_chat_distinguishes_invalid_profile_json_from_missing_resume_evidence(
    monkeypatch,
) -> None:
    app_service = FakeInvalidProfileApplication()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历查询深圳的岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
                "provider": "zhipu",
                "model": "glm-4.7",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["tool_calls"] == 1
    assert "模型未能生成符合结构的岗位搜索条件" in body["content"]
    assert "不代表简历缺失" in body["content"]
    assert "简历证据不足" not in body["content"]
    assert "invalid_json" in body["content"]
    assert app_service.runtime.calls == []


def test_chat_required_does_not_use_public_search_when_knowledge_answers(
    monkeypatch,
) -> None:
    app_service = FakeApplication(route=KnowledgeRequestRoute.KNOWLEDGE_QUERY)
    monkeypatch.setattr(
        api_module,
        "create_knowledge_service",
        lambda: FakeAnsweredKnowledge(),
    )
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "HR 邮箱是什么？",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert response.json()["content"] == "知识库中已有答案。"
    assert response.json()["knowledge_mode"] == "required"
    assert response.json()["tool_calls"] == 0
    assert app_service.chat_called is False
    assert app_service.runtime.calls == []


def test_chat_required_uses_public_search_for_explicit_online_job_query(
    monkeypatch,
) -> None:
    app_service = FakeApplication()
    knowledge = FakeAnsweredKnowledge()
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "还有其他网上的深圳 AI Agent 岗位吗",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert response.json()["tool_calls"] == 4
    assert app_service.chat_called is False
    assert len(app_service.job_research_calls) == 1
    assert app_service.runtime.calls == []
    assert len(app_service.candidate_analysis_calls) == 1
    assert knowledge.calls == []
    assert len(knowledge.retrieve_calls) == 1
    assert knowledge.retrieve_calls[0][2]["document_types"] == [
        "job_description"
    ]


def test_chat_required_greeting_uses_normal_chat_without_knowledge_or_tools(
    monkeypatch,
) -> None:
    app_service = FakeConversationApplication()
    knowledge = FakeKnowledge()
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "你好",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert response.json()["content"] == "你好！有什么可以帮你？"
    assert app_service.chat_called is True
    assert app_service.chat_calls[0]["allow_tools"] is False
    assert knowledge.calls == []
    assert knowledge.retrieve_calls == []
    assert app_service.runtime.calls == []


def test_chat_off_greeting_is_classified_then_runs_without_tools(monkeypatch) -> None:
    app_service = FakeConversationApplication()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={"message": "你好", "knowledge_mode": "off"},
        )

    assert response.status_code == 200
    assert len(app_service.route_calls) == 1
    assert app_service.chat_calls[0]["allow_tools"] is False


def test_stream_off_greeting_uses_the_same_tool_free_route(monkeypatch) -> None:
    app_service = FakeConversationApplication()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        with client.stream(
            "POST",
            "/v1/chat/stream",
            json={"message": "你好", "knowledge_mode": "off"},
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "done"' in body
    assert len(app_service.route_calls) == 1
    assert app_service.chat_calls[0]["allow_tools"] is False


def test_buffered_job_stream_relays_confirmation_before_final_result(
    monkeypatch,
) -> None:
    app_service = FakeConfirmationJobApplication()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        with client.stream(
            "POST",
            "/v1/chat/stream",
            json={"message": "查找上海的岗位", "knowledge_mode": "off"},
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "confirmation_required"' in body
    assert body.index('"type": "confirmation_required"') < body.index(
        '"type": "done"'
    )


def test_profile_failure_answer_exposes_only_bounded_issue_codes() -> None:
    run = SkillRunResult(
        status="search_profile_required",
        error_code="schema_validation_failed",
        data={"profile_issues": ["evidence_refs:list_type"]},
    )

    answer = api_module._public_job_search_failure_answer(run)

    assert "evidence_refs:list_type" in answer
    assert "schema_validation_failed" in answer


def test_chat_auto_job_request_uses_unified_job_research_route(monkeypatch) -> None:
    app_service = FakeApplication()
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={"message": "帮我找深圳 Python 岗位", "knowledge_mode": "auto"},
        )

    assert response.status_code == 200
    assert len(app_service.route_calls) == 1
    assert len(app_service.prepare_calls) == 1
    assert len(app_service.prepared_search_calls) == 1
    assert app_service.chat_called is False


def test_job_research_uses_knowledge_answer_when_saved_jd_exists(
    monkeypatch,
) -> None:
    app_service = FakeApplication()
    knowledge = FakeAnsweredKnowledge(
        job_matches=[_saved_job_match(location="深圳")]
    )
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历查询深圳的岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert "知识库中匹配到 1 个符合条件且仍可使用的 JD" in response.json()["content"]
    assert "saved-job.md@v1#L1-L12" in response.json()["content"]
    assert "知识库中已有答案。" in response.json()["content"]
    assert response.json()["tool_calls"] == 0
    assert len(knowledge.retrieve_calls) == 1
    assert len(knowledge.calls) == 1
    assert app_service.job_research_calls == []
    assert app_service.runtime.calls == []
    saved_turn_ids = {item[1] for item in app_service.store.messages}
    assert saved_turn_ids == {app_service.prepare_calls[0]["turn_id"]}


def test_job_research_matches_metadata_from_sibling_chunks(monkeypatch) -> None:
    app_service = FakeApplication()
    match = _saved_job_match(location="深圳")
    match = match.model_copy(
        update={
            "preview": "## Requirements\nPython backend engineer\n",
            "start_line": 8,
            "end_line": 10,
        }
    )
    knowledge = FakeChunkedJobKnowledge(job_match=match)
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历查询深圳 Python 岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert "知识库中匹配到 1 个符合条件且仍可使用的 JD" in response.json()["content"]
    assert "saved-job.md@v1#L1-L12" in response.json()["content"]
    assert "知识库中已有答案。" in response.json()["content"]
    assert len(knowledge.chunk_calls) >= 1
    assert app_service.prepared_search_calls == []


def test_job_research_falls_back_to_user_request_when_profile_query_misses_saved_jd(
    monkeypatch,
) -> None:
    app_service = FakeApplication()
    original_prepare = app_service.prepare_job_research_request

    async def prepare_ai_agent_profile(**kwargs):
        prepared = await original_prepare(**kwargs)
        prepared.data["search_profile"].update(
            {
                "query": "Python AI Agent",
                "role_terms": ["Python", "AI", "Agent"],
                "location": "深圳",
            }
        )
        return prepared

    app_service.prepare_job_research_request = prepare_ai_agent_profile
    knowledge = FakeFallbackJobKnowledge(
        fallback_matches=[
            _saved_job_match(location="深圳", role="AI Agent Engineer")
        ]
    )
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历在知识库匹配岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert "知识库中匹配到 1 个符合条件且仍可使用的 JD" in response.json()["content"]
    assert "saved-job.md@v1#L1-L12" in response.json()["content"]
    assert "知识库中已有答案。" in response.json()["content"]
    assert [item[1] for item in knowledge.retrieve_calls] == [
        "Python AI Agent",
        "根据我的简历在知识库匹配岗位",
    ]
    assert app_service.prepared_search_calls == []


def test_saved_job_match_survives_invalid_rag_generation_without_web_fallback(
    monkeypatch,
) -> None:
    app_service = FakeApplication()
    original_prepare = app_service.prepare_job_research_request

    async def prepare_ai_agent_profile(**kwargs):
        prepared = await original_prepare(**kwargs)
        prepared.data["search_profile"].update(
            {"query": "AI Agent", "role_terms": ["AI", "Agent"], "location": "深圳"}
        )
        return prepared

    app_service.prepare_job_research_request = prepare_ai_agent_profile
    saved = _saved_job_match(location="深圳", role="字节跳动 AI Agent 研发工程师")
    knowledge = FakeInvalidGeneratedJobKnowledge(job_matches=[saved])
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历在知识库匹配岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert "字节跳动 AI Agent 研发工程师" in body["content"]
    assert saved.source_ref in body["content"]
    assert len(body["citations"]) == 1
    assert body["citations"][0]["document_id"] == str(saved.document_id)
    assert app_service.prepared_search_calls == []
    assert app_service.candidate_analysis_calls == []


def test_non_job_factual_no_evidence_does_not_use_job_tools(monkeypatch) -> None:
    app_service = FakeApplication(route=KnowledgeRequestRoute.KNOWLEDGE_QUERY)
    knowledge = FakeKnowledge()
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "合同的生效日期是什么？",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert response.json()["refusal_reason"] == "no_evidence"
    assert app_service.job_research_calls == []
    assert app_service.runtime.calls == []


def _saved_job_match(*, location: str, role: str = "Python Backend Engineer"):
    return RetrievalMatch(
        chunk_id=uuid4(),
        document_id=uuid4(),
        document_type="job_description",
        filename="saved-job.md",
        version=1,
        section_path=[role],
        start_line=1,
        end_line=12,
        preview=(
            f"# {role}\n\n- Company: Example\n- Location: {location}\n"
            "- Status: open\n- Source URL: https://jobs.example.test/saved\n"
            "## Requirements\n\n- Python\n"
        ),
        source_ref="saved-job.md@v1#L1-L12",
        rank=1,
        created_at=datetime.now(UTC) - timedelta(days=2),
    )


def test_saved_jd_location_mismatch_uses_prepared_batch_fallback(monkeypatch) -> None:
    app_service = FakeApplication()
    knowledge = FakeAnsweredKnowledge(
        job_matches=[_saved_job_match(location="上海")]
    )
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历推荐深圳的 Python 岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert len(app_service.prepare_calls) == 1
    assert len(app_service.prepared_search_calls) == 1
    assert len(app_service.candidate_analysis_calls) == 1
    assert knowledge.calls == []


def test_explicit_knowledge_only_job_request_does_not_fall_back_to_web(
    monkeypatch,
) -> None:
    app_service = FakeApplication()
    knowledge = FakeKnowledge(job_matches=[])
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "根据我的简历在知识库匹配岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert "missing_jd" in response.json()["content"]
    assert app_service.prepared_search_calls == []
    assert app_service.candidate_analysis_calls == []


def test_job_research_appends_without_rewriting_existing_history(monkeypatch) -> None:
    app_service = FakeApplication()
    original = (
        app_service.store.session_id,
        uuid4(),
        api_module.Message(role="assistant", content="既有历史消息"),
    )
    app_service.store.messages.append(original)
    knowledge = FakeKnowledge()
    monkeypatch.setattr(api_module, "create_knowledge_service", lambda: knowledge)
    monkeypatch.setattr(api_module, "create_application", lambda: app_service)

    with TestClient(api_module.create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "session_id": str(app_service.store.session_id),
                "message": "根据我的简历推荐深圳的岗位",
                "knowledge_mode": "required",
                "knowledge_base_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert app_service.store.messages[0] == original
    assert [item[2].role for item in app_service.store.messages[1:]] == [
        "user",
        "assistant",
    ]


def test_job_research_failure_paths_append_one_turn_with_stable_ids(
    monkeypatch,
) -> None:
    for application_type in (
        FakeInvalidProfileApplication,
        FakeSearchFailureApplication,
        FakeNoCandidateApplication,
        FakePlaywrightFailureApplication,
    ):
        app_service = application_type()
        original = (
            app_service.store.session_id,
            uuid4(),
            api_module.Message(role="assistant", content="既有历史消息"),
        )
        app_service.store.messages.append(original)
        monkeypatch.setattr(api_module, "create_application", lambda: app_service)
        monkeypatch.setattr(
            api_module,
            "create_knowledge_service",
            lambda: FakeKnowledge(),
        )

        with TestClient(api_module.create_api()) as client:
            response = client.post(
                "/v1/chat",
                json={
                    "session_id": str(app_service.store.session_id),
                    "message": "根据我的简历推荐深圳的岗位",
                    "knowledge_mode": "required",
                    "knowledge_base_id": str(uuid4()),
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert app_service.store.messages[0] == original
        appended = app_service.store.messages[1:]
        assert [item[2].role for item in appended] == ["user", "assistant"]
        assert {item[0] for item in appended} == {UUID(body["session_id"])}
        assert {item[1] for item in appended} == {UUID(body["turn_id"])}
        request_ids = (UUID(body["session_id"]), UUID(body["turn_id"]))
        assert (
            app_service.prepare_calls[0]["session_id"],
            app_service.prepare_calls[0]["turn_id"],
        ) == request_ids
        for calls in (
            app_service.prepared_search_calls,
            app_service.candidate_analysis_calls,
        ):
            for call in calls:
                assert (call["session_id"], call["turn_id"]) == request_ids
