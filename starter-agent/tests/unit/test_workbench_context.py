import asyncio
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
