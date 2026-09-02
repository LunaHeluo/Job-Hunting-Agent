import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from starter_agent.bootstrap import create_application, get_settings
from starter_agent.interfaces.api import create_api
from starter_agent.tools.email.models import DraftCreateRequest
from starter_agent.delegation.store import RevisionConflictError
from starter_agent.interfaces.capabilities_api import ManagementPrincipal, get_management_principal


def test_resume_delegated_child_api_returns_controlled_receipt(monkeypatch) -> None:
    from types import SimpleNamespace
    import starter_agent.interfaces.api as api_module

    class Application:
        async def wait_for_background_tasks(self):
            return None

        def resume_delegated_child(self, **authority):
            assert authority == {
                "parent_run_id": "parent:1", "child_task_id": "task:1",
                "child_run_id": "child:1", "checkpoint_ref": "checkpoint:1",
                "principal": "user:1",
            }
            return SimpleNamespace(
                id="child:1", status="queued", phase="open", version=7,
                run_context_checkpoint_ref="checkpoint:1",
            )

    monkeypatch.setattr(api_module, "create_application", lambda: Application())
    monkeypatch.setattr(api_module, "create_mcp_manager", lambda: (_ for _ in ()).throw(RuntimeError("disabled")))
    api = api_module.create_api()
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(subject="user:1", role="operator")
    with TestClient(api) as client:
        response = client.post("/v1/runs/delegated/resume", json={
            "parent_run_id": "parent:1", "child_task_id": "task:1",
            "child_run_id": "child:1", "checkpoint_ref": "checkpoint:1",
        })
    assert response.status_code == 200
    assert response.json() == {
        "child_run_id": "child:1", "status": "queued", "phase": "open",
        "version": 7, "checkpoint_ref": "checkpoint:1",
    }


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (ValueError("handoff_checkpoint_authority_mismatch"), 403, "delegation_resume_unauthorized"),
        (TimeoutError("job_web_handoff_timeout"), 410, "delegation_resume_expired"),
        (RevisionConflictError("conflict"), 409, "delegation_resume_conflict"),
        (RuntimeError("delegation_resume_unavailable"), 409, "delegation_resume_state_invalid"),
    ],
)
def test_resume_delegated_child_api_maps_safe_errors(monkeypatch, error, status_code, code) -> None:
    import starter_agent.interfaces.api as api_module

    class Application:
        async def wait_for_background_tasks(self):
            return None

        def resume_delegated_child(self, **_authority):
            raise error

    monkeypatch.setattr(api_module, "create_application", lambda: Application())
    monkeypatch.setattr(api_module, "create_mcp_manager", lambda: (_ for _ in ()).throw(RuntimeError("disabled")))
    api = api_module.create_api()
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(subject="user:1", role="operator")
    with TestClient(api) as client:
        response = client.post("/v1/runs/delegated/resume", json={
            "parent_run_id": "parent:1", "child_task_id": "task:1",
            "child_run_id": "child:1", "checkpoint_ref": "checkpoint:1",
        })
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code


def test_resume_delegated_child_rejects_anonymous_and_forged_body_principal(monkeypatch) -> None:
    import starter_agent.interfaces.api as api_module

    class Application:
        async def wait_for_background_tasks(self): return None
        def resume_delegated_child(self, **_authority): raise AssertionError("must not resume")

    monkeypatch.setattr(api_module, "create_application", lambda: Application())
    monkeypatch.setattr(api_module, "create_mcp_manager", lambda: (_ for _ in ()).throw(RuntimeError("disabled")))
    api = api_module.create_api()
    api.dependency_overrides[get_management_principal] = lambda: ManagementPrincipal(subject="anonymous", role="viewer")
    with TestClient(api) as client:
        unauthenticated = client.post("/v1/runs/delegated/resume", json={
            "parent_run_id":"parent:1", "child_task_id":"task:1",
            "child_run_id":"child:1", "checkpoint_ref":"checkpoint:1",
        })
        forged = client.post("/v1/runs/delegated/resume", json={
            "parent_run_id":"parent:1", "child_task_id":"task:1",
            "child_run_id":"child:1", "checkpoint_ref":"checkpoint:1",
            "principal":"victim",
        })
    assert unauthenticated.status_code == 401
    assert forged.status_code in {401, 422}


def test_health() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(response.json()["runtime_revision"]) == 64
    assert response.json()["desired_runtime_revision"] == response.json()[
        "runtime_revision"
    ]
    assert response.json()["restart_required"] is False


def test_chat_with_mock() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={"message": "你好", "provider": "mock"},
        )
    assert response.status_code == 200
    assert response.json()["provider"] == "mock"
    assert response.json()["usage"] == {}
    assert response.json()["session_usage"]["total_tokens"] == 0
    assert response.json()["max_total_tokens"] == 128000
    assert response.json()["tool_governance_enabled"] is True


def test_chat_ignores_attempt_to_disable_tool_governance() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "你好",
                "provider": "mock",
                "tool_governance_enabled": False,
            },
        )

    assert response.status_code == 200
    assert response.json()["tool_governance_enabled"] is True


def test_course_ppt_origin_is_allowed() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.options(
            "/v1/chat",
            headers={
                "Origin": "http://127.0.0.1:8001",
                "Access-Control-Request-Method": "POST",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8001"


def test_stream_chat_with_mock() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        with client.stream(
            "POST",
            "/v1/chat/stream",
            json={"message": "你好", "provider": "mock"},
        ) as response:
            body = "".join(response.iter_text())
    assert response.status_code == 200
    assert '"type": "delta"' in body
    assert '"type": "done"' in body
    assert '"session_id"' in body


def test_stream_reports_tool_start_and_completion() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        with client.stream(
            "POST",
            "/v1/chat/stream",
            json={
                "message": "请执行时间工具",
                "provider": "mock",
                "model": "starter-mock",
                "tool": "get_current_time",
            },
        ) as response:
            body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "tool_started"' in body
    assert '"type": "tool_completed"' in body
    assert '"name": "get_current_time"' in body
    assert '"ok": true' in body
    assert body.index('"type": "tool_started"') < body.index(
        '"type": "tool_completed"'
    )
    assert body.index('"type": "tool_completed"') < body.index('"type": "done"')


def test_chat_result_exposes_completed_finish_reason() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={"message": "你好", "provider": "mock"},
        )

    assert response.status_code == 200
    assert response.json()["finish_reason"] == "completed"
    assert response.json()["continuation"] is None


def test_providers_endpoint_returns_configured_providers() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.get("/v1/providers")
    body = response.json()
    assert response.status_code == 200
    assert body["default_provider"]
    assert body["default_model"]
    assert any(provider["name"] == "mock" for provider in body["providers"])
    provider_models = {
        provider["name"]: provider["models"] for provider in body["providers"]
    }
    assert provider_models["zhipu"] == ["glm-4.7", "glm-5.1"]
    assert provider_models["tokenrouter"] == [
        "openai/gpt-5.5",
        "openai/gpt-5.6-terra",
    ]
    assert provider_models["openai"] == [
        "gpt-5.5",
        "gpt-5.6-terra",
    ]
    configured_names = set(get_settings().providers)
    returned_names = {provider["name"] for provider in body["providers"]}
    assert returned_names == configured_names
    assert any(not provider["has_api_key"] for provider in body["providers"])


def test_tools_endpoint_returns_enabled_tools() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.get("/v1/tools")

    assert response.status_code == 200
    tools = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "get_current_time" in tools
    assert "search_jobs_serpapi" in tools
    assert tools["search_jobs_serpapi"]["risk_level"] == "read"
    removed_name = "search_job" + "_description"
    assert removed_name not in tools
    assert tools["read_resume"]["risk_level"] == "read"
    assert tools["list_resume_versions"]["risk_level"] == "read"
    assert tools["save_resume"]["risk_level"] == "write"
    assert tools["compare_resume"]["risk_level"] == "read"
    assert tools["compare_resume_to_jd"]["risk_level"] == "read"
    assert tools["draft_resume_patch"]["risk_level"] == "write"
    assert tools["save_resume_version"]["risk_level"] == "write"
    assert tools["email_search"]["risk_level"] == "read"
    assert tools["email_read"]["risk_level"] == "read"
    assert tools["email_create_draft"]["risk_level"] == "write"
    assert tools["email_send"]["risk_level"] == "external"


def test_email_approval_api_requires_explicit_confirmation() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    application = create_application()
    manager = application.runtime.tools.email_manager
    assert manager is not None
    session_id = application.store.create_session()
    draft = asyncio.run(
        manager.create_draft(
            DraftCreateRequest(
                profile="mock",
                storage_scope="mock",
                to=["hr@example.test"],
                subject="Re: Interview",
                body_text="Confirmed.",
                idempotency_key=f"api-draft-{uuid4()}",
            ),
            session_id=str(session_id),
        )
    )
    with TestClient(create_api()) as client:
        challenge_response = client.post(
            f"/v1/email/drafts/{draft.draft_id}/approval-challenges",
            json={"session_id": str(session_id), "profile": "mock"},
        )
        challenge = challenge_response.json()
        rejected = client.post(
            (
                "/v1/email/approval-challenges/"
                f"{challenge['approval_id']}/confirm"
            ),
            json={"session_id": str(session_id), "confirmed": False},
        )
        accepted = client.post(
            (
                "/v1/email/approval-challenges/"
                f"{challenge['approval_id']}/confirm"
            ),
            json={"session_id": str(session_id), "confirmed": True},
        )

    assert challenge_response.status_code == 200
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["error_code"] == (
        "email_approval_required"
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "approved"


def test_email_approval_send_api_requires_approval_and_is_idempotent() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    application = create_application()
    manager = application.runtime.tools.email_manager
    assert manager is not None
    session_id = application.store.create_session()
    draft = asyncio.run(
        manager.create_draft(
            DraftCreateRequest(
                profile="mock",
                storage_scope="mock",
                to=["hr@example.test"],
                subject="SMTP approval preview",
                body_text="This automated test must remain simulated.",
                idempotency_key=f"api-send-draft-{uuid4()}",
            ),
            session_id=str(session_id),
        )
    )
    with TestClient(create_api()) as client:
        challenge = client.post(
            f"/v1/email/drafts/{draft.draft_id}/approval-challenges",
            json={"session_id": str(session_id), "profile": "mock"},
        ).json()
        rejected = client.post(
            f"/v1/email/approvals/{challenge['approval_id']}/send",
            json={
                "session_id": str(session_id),
                "idempotency_key": "api-send-key-00000001",
            },
        )
        client.post(
            (
                "/v1/email/approval-challenges/"
                f"{challenge['approval_id']}/confirm"
            ),
            json={"session_id": str(session_id), "confirmed": True},
        )
        sent = client.post(
            f"/v1/email/approvals/{challenge['approval_id']}/send",
            json={
                "session_id": str(session_id),
                "idempotency_key": "api-send-key-00000001",
            },
        )
        repeated = client.post(
            f"/v1/email/approvals/{challenge['approval_id']}/send",
            json={
                "session_id": str(session_id),
                "idempotency_key": "api-send-key-00000001",
            },
        )
        changed_replay = client.post(
            f"/v1/email/approvals/{challenge['approval_id']}/send",
            json={
                "session_id": str(session_id),
                "idempotency_key": "api-send-key-changed-0002",
            },
        )

    assert rejected.status_code == 400
    assert rejected.json()["detail"]["error_code"] == (
        "email_approval_required"
    )
    assert sent.status_code == 200
    assert sent.json()["ok"] is True
    assert sent.json()["data"]["status"] == "simulated_sent"
    assert sent.json()["data"]["external_delivery"] is False
    assert repeated.status_code == 200
    assert repeated.json()["data"] == sent.json()["data"]
    assert changed_replay.status_code == 400
    assert changed_replay.json()["detail"]["error_code"] == (
        "email_approval_consumed"
    )


def test_chat_can_force_an_enabled_tool() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "请执行指定工具",
                "provider": "mock",
                "model": "starter-mock",
                "tool": "get_current_time",
            },
        )

    assert response.status_code == 200
    assert response.json()["tool_calls"] == 1


def test_chat_rejects_unknown_forced_tool() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.post(
            "/v1/chat",
            json={
                "message": "请执行不存在的工具",
                "provider": "mock",
                "model": "starter-mock",
                "tool": "missing_tool",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "tool_not_available"
    assert '"api_key":' not in response.text.lower()


def test_sessions_list_and_history_messages() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        chat_response = client.post(
            "/v1/chat",
            json={"message": "请介绍你自己", "provider": "mock"},
        )
        session_id = chat_response.json()["session_id"]

        list_response = client.get("/v1/sessions")
        history_response = client.get(f"/v1/sessions/{session_id}/messages")

    assert list_response.status_code == 200
    sessions = list_response.json()["sessions"]
    assert list_response.json()["total"] >= len(sessions)
    assert list_response.json()["offset"] == 0
    assert list_response.json()["limit"] == 50
    assert any(session["id"] == session_id for session in sessions)
    matching = next(session for session in sessions if session["id"] == session_id)
    assert matching["message_count"] >= 2
    assert matching["title"]

    assert history_response.status_code == 200
    messages = history_response.json()["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "请介绍你自己"
    assert messages[-1]["role"] == "assistant"
    assert history_response.json()["session_usage"]["total_tokens"] == 0
    assert history_response.json()["max_total_tokens"] == 128000


def test_missing_session_returns_404() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    missing = uuid4()
    with TestClient(create_api()) as client:
        response = client.get(f"/v1/sessions/{missing}/messages")
    assert response.status_code == 404


def test_delete_session_removes_history() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        chat_response = client.post(
            "/v1/chat",
            json={"message": "需要删除的会话", "provider": "mock"},
        )
        session_id = chat_response.json()["session_id"]

        delete_response = client.delete(f"/v1/sessions/{session_id}")
        history_response = client.get(f"/v1/sessions/{session_id}/messages")

    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"
    assert history_response.status_code == 404


def test_long_term_memory_crud_and_external_source_rejection() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    payload = {
        "key": "target_city_test",
        "value": "Shanghai",
        "category": "preference",
        "source_type": "user_confirmed",
        "sensitivity": "personal",
        "confirmed": True,
    }
    with TestClient(create_api()) as client:
        created_response = client.post("/v1/memories", json=payload)
        assert created_response.status_code == 201
        created = created_response.json()
        memory_id = created["id"]

        listed = client.get("/v1/memories").json()["memories"]
        assert any(item["id"] == memory_id for item in listed)
        assert created["confidence"] == 1.0
        assert created["source_type"] == "user_confirmed"
        assert created["expires_at"] is not None

        updated_response = client.put(
            f"/v1/memories/{memory_id}",
            json={
                "key": "target_city_test",
                "value": "Shanghai / Shenzhen",
                "category": "preference",
                "sensitivity": "personal",
                "status": "disabled",
                "confirmed": True,
            },
        )
        assert updated_response.status_code == 200
        assert updated_response.json()["status"] == "disabled"

        external = client.post(
            "/v1/memories",
            json={
                **payload,
                "key": "web_content_test",
                "source_type": "external_web",
            },
        )
        assert external.status_code == 400
        assert external.json()["detail"]["code"] == (
            "external_memory_source_not_allowed"
        )

        deleted = client.delete(f"/v1/memories/{memory_id}")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"


def test_long_term_memory_requires_explicit_confirmation() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        response = client.post(
            "/v1/memories",
            json={
                "key": "unconfirmed",
                "value": "Do not save this",
                "category": "constraint",
                "source_type": "user_confirmed",
                "confirmed": False,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "memory_confirmation_required"


def test_stream_can_continue_existing_session() -> None:
    get_settings.cache_clear()
    create_application.cache_clear()
    with TestClient(create_api()) as client:
        chat_response = client.post(
            "/v1/chat",
            json={"message": "第一条", "provider": "mock"},
        )
        session_id = chat_response.json()["session_id"]
        with client.stream(
            "POST",
            "/v1/chat/stream",
            json={
                "message": "第二条",
                "session_id": session_id,
                "provider": "mock",
            },
        ) as stream_response:
            body = "".join(stream_response.iter_text())
        history_response = client.get(f"/v1/sessions/{session_id}/messages")

    assert stream_response.status_code == 200
    assert f'"session_id": "{session_id}"' in body
    user_messages = [
        message["content"]
        for message in history_response.json()["messages"]
        if message["role"] == "user"
    ]
    assert user_messages[-2:] == ["第一条", "第二条"]
