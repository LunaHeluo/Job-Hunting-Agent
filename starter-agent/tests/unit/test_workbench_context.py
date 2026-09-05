import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import starter_agent.interfaces.api as api_module
from starter_agent.cv_workbench.runtime import create_workbench_runtime
from starter_agent.cv_workbench.workspaces import CreateWorkspaceCommand, WorkspaceProfile
from starter_agent.interfaces.api import ChatRequest
from starter_agent.interfaces.capabilities_api import ManagementPrincipal
from starter_agent.cv_workbench.contracts import ResumeVersion


def test_context_reauthorizes_refs_and_never_rewrites_chat_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = create_workbench_runtime(f"sqlite:///context-{uuid4().hex}.db", tmp_path)
    try:
        runtime.workspaces.create(
            CreateWorkspaceCommand("ws_context", WorkspaceProfile(name="Context")),
            principal="local-user",
        )
        monkeypatch.setattr(api_module, "create_cv_workbench_runtime", lambda: runtime)
        request = ChatRequest.model_validate(
            {
                "message": "解释当前结果",
                "provider": "mock",
                "workbench_context": {
                    "workspace_id": "ws_context",
                    "context_epoch": 3,
                },
            }
        )
        api_module._authorize_workbench_context(
            request, ManagementPrincipal(subject="local-user", role="admin")
        )
        assert request.message == "解释当前结果"

        with pytest.raises(HTTPException) as error:
            api_module._authorize_workbench_context(
                request, ManagementPrincipal(subject="other-user", role="admin")
            )
        assert error.value.status_code == 404
    finally:
        runtime.close()


def test_context_rejects_client_supplied_content() -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "message": "hello",
                "workbench_context": {
                    "workspace_id": "ws_context",
                    "context_epoch": 1,
                    "resume_content": "forged content",
                },
            }
        )


def test_authorized_resume_is_passed_ephemerally_to_chat(monkeypatch):
    version = ResumeVersion.model_construct(version_id="rv_current", content=object())
    read = Mock(return_value="# 当前简历\n擅长 Python")
    store = SimpleNamespace(get=lambda model, *args, **kwargs: version if model is ResumeVersion else object(), assert_entity_in_workspace=Mock())
    monkeypatch.setattr(api_module, "create_cv_workbench_runtime", lambda: SimpleNamespace(store=store, versions=SimpleNamespace(content=SimpleNamespace(read=read))))
    request = ChatRequest(message="读取我的简历", workbench_context={"workspace_id": "ws_context", "resume_version_id": "rv_current", "context_epoch": 1})
    api_module._authorize_workbench_context(request, ManagementPrincipal(subject="local-user", role="admin"))
    assert "擅长 Python" in request._resume_context
    assert request.message == "读取我的简历"
    assert "擅长 Python" not in request.model_dump_json()
    app = SimpleNamespace(chat=AsyncMock(return_value="ok"))
    route = SimpleNamespace(route=api_module.KnowledgeRequestRoute.CONVERSATION)
    asyncio.run(api_module._dispatch_classified_chat(request, application=app, route=route))
    assert "擅长 Python" in app.chat.call_args.kwargs["resume_context"]
    assert app.chat.call_args.kwargs["content"] == request.message


def test_resume_context_reaches_runtime_without_persisting(application, monkeypatch):
    run = AsyncMock(wraps=application.runtime.run)
    monkeypatch.setattr(application.runtime, "run", run)
    result = asyncio.run(application.chat(content="读取简历", provider_name="mock", model="starter-mock", allow_tools=False, resume_context="唯一简历正文标记"))
    assert any(message.content == "唯一简历正文标记" for message in run.call_args.kwargs["messages"])
    rows = application.store.list_stored_messages(result.session_id)
    assert not any(row.message.content == "唯一简历正文标记" for row in rows)


def test_match_analysis_context_bypasses_job_research_routing() -> None:
    request = ChatRequest.model_validate(
        {
            "message": "请解释当前匹配分数",
            "workbench_context": {
                "workspace_id": "ws_context",
                "context_epoch": 1,
                "match_analysis_id": "ma_context",
            },
        }
    )
    decision = asyncio.run(api_module._classify_chat_request(request, application=None))
    assert decision.route.value == "conversation"
    assert decision.reason_code == "workbench_match_analysis"
