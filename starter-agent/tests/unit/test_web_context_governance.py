from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from starter_agent.agent.token_counter import TokenCounter
from starter_agent.infrastructure.session_store import SQLiteSessionStore


def _snapshot_html() -> str:
    return (
        "<html><head><style>.hidden{display:none}</style>"
        "<script>window.cookie='session=TOP-SECRET'</script></head>"
        "<body><nav>Global navigation and careers menu</nav>"
        "<div class='cookie-banner'>Accept cookies session=TOP-SECRET</div>"
        "<main><h1>Agent Engineer</h1><p>Build reliable AI agents in Sydney.</p>"
        "<h2>Requirements</h2><p>Python and Playwright.</p></main>"
        "<footer>Privacy links</footer></body></html>"
    )


def _governor(*, max_result_tokens: int):
    from starter_agent.delegation.web_context import WebContextGovernor

    return WebContextGovernor(
        TokenCounter(safety_ratio=1), max_result_tokens=max_result_tokens
    )


def test_web_observation_stores_raw_artifact_but_injects_only_clean_budgeted_jd_summary() -> None:
    governor = _governor(max_result_tokens=180)
    raw = json.dumps(
        {
            "ok": True,
            "data": {"html": _snapshot_html()},
            "metadata": {
                "source_url": "https://jobs.example.test/agent?token=secret",
                "source_content_sha256": "a" * 64,
            },
        }
    )

    artifact, observation = governor.govern(
        raw,
        tool_name="mcp__playwright__browser_snapshot",
        tool_call_id="call:1",
        raw_source_ref="tool:browser:call:1",
    )

    assert "Agent Engineer" in artifact.content
    assert "Global navigation" in artifact.content
    assert "TOP-SECRET" not in artifact.content
    assert "Global navigation" not in observation.content
    assert "cookie-banner" not in observation.content
    assert "<script" not in observation.content
    assert "Agent Engineer" in observation.content
    payload = json.loads(observation.content)
    assert payload["metadata"]["raw_source_ref"] == "tool:browser:call:1"
    assert payload["metadata"]["source_content_sha256"] == "a" * 64
    assert observation.context_result_tokens <= 180


def test_same_dom_is_not_injected_twice_but_retains_artifact_reference() -> None:
    governor = _governor(max_result_tokens=400)
    raw = json.dumps({"ok": True, "data": {"html": _snapshot_html()}})

    _artifact, first = governor.govern(
        raw, tool_name="mcp__playwright__browser_snapshot", tool_call_id="call:1", raw_source_ref="tool:1"
    )
    _artifact, duplicate = governor.govern(
        raw, tool_name="mcp__playwright__browser_snapshot", tool_call_id="call:2", raw_source_ref="tool:2"
    )

    assert "Agent Engineer" in first.content
    payload = json.loads(duplicate.content)
    assert payload["data"]["duplicate_observation"] is True
    assert payload["metadata"]["raw_source_ref"] == "tool:2"
    assert "Agent Engineer" not in duplicate.content


def test_restricted_web_artifact_requires_owner_and_expires_to_audit_metadata(tmp_path) -> None:
    store = SQLiteSessionStore("sqlite:///web-artifacts.db", tmp_path)
    session_id, turn_id = store.create_session(), uuid4()
    expires_at = datetime.now(UTC) + timedelta(minutes=1)

    store.save_tool_artifact(
        source_ref="tool:web:1",
        session_id=session_id,
        turn_id=turn_id,
        tool_name="mcp__playwright__browser_snapshot",
        content=json.dumps({"html": _snapshot_html()}),
        parent_run_id="parent:1",
        child_task_id="task:1",
        child_run_id="child:1",
        policy_decision_id="policy:1",
        approval_id="approval:1",
        access_level="child_restricted",
        principal="user:owner",
        expires_at=expires_at,
    )

    artifact = store.get_tool_artifact_for_principal("tool:web:1", principal="user:owner")
    assert artifact is not None
    assert artifact["parent_run_id"] == "parent:1"
    assert artifact["child_run_id"] == "child:1"
    assert artifact["access_level"] == "child_restricted"
    assert "Agent Engineer" in artifact["content"]
    assert store.get_tool_artifact_for_principal("tool:web:1", principal="user:other") is None


def test_expired_child_artifact_purges_body_and_legacy_reader_cannot_disclose_it(tmp_path) -> None:
    store = SQLiteSessionStore("sqlite:///expired-web-artifacts.db", tmp_path)
    session_id = store.create_session()
    store.save_tool_artifact(
        source_ref="tool:web:expired", session_id=session_id, turn_id=uuid4(),
        tool_name="mcp__playwright__browser_snapshot", content='{"html":"<main>private</main>"}',
        principal="user:owner", access_level="child_restricted",
        expires_at=datetime.now(UTC) - timedelta(seconds=1), content_sha256="c" * 64,
    )
    assert store.purge_expired_tool_artifacts(now=datetime.now(UTC)) == 1
    legacy = store.get_tool_artifact("tool:web:expired")
    assert legacy is not None and "content" not in legacy and legacy["expired"] is True
    authorized = store.get_tool_artifact_for_principal("tool:web:expired", principal="user:owner")
    assert authorized is not None and "content" not in authorized


@pytest.mark.parametrize("target", ["parent_message", "public_trace", "ordinary_log"])
def test_raw_snapshot_never_appears_in_parent_or_public_payload(target: str) -> None:
    governor = _governor(max_result_tokens=200)
    raw = json.dumps({"ok": True, "data": {"html": _snapshot_html()}})
    _artifact, observation = governor.govern(
        raw, tool_name="mcp__playwright__browser_snapshot", tool_call_id="call:1", raw_source_ref="tool:1"
    )
    public = governor.parent_payload(
        jobs=[{"title": "Agent Engineer", "source_url": "https://jobs.example.test/agent"}],
        missing=[], errors=[], usage={"tool_calls": 1}, trace_ref="trace:child:1", artifact_refs=["tool:1"],
    )

    assert "<html" not in observation.content
    assert "TOP-SECRET" not in observation.content
    serialized = json.dumps(public)
    assert "<html" not in serialized
    assert "TOP-SECRET" not in serialized
    assert set(public) == {"jobs", "missing", "errors", "usage", "trace_ref", "artifact_refs"}
