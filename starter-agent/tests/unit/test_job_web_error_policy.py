import pytest

from starter_agent.delegation.specialists.job_web_error_policy import (
    JobWebErrorPolicy,
    WebErrorKind,
)


@pytest.mark.parametrize(
    ("error_code", "status_code", "kind", "stable_code"),
    [
        ("page_load_failed", None, WebErrorKind.LOAD_FAILURE, "job_web_load_failed"),
        ("connection_error", None, WebErrorKind.CONNECTION_FAILURE, "job_web_connection_failed"),
        (None, 404, WebErrorKind.NOT_FOUND, "job_web_not_found"),
        (None, 410, WebErrorKind.NOT_FOUND, "job_web_not_found"),
        ("redirect", 302, WebErrorKind.REDIRECT, "job_web_redirect"),
        ("tool_timeout", None, WebErrorKind.RENDER_TIMEOUT, "job_web_render_timeout"),
        ("selector_not_found", None, WebErrorKind.SELECTOR_FAILURE, "job_web_selector_failed"),
        ("empty_body", None, WebErrorKind.EMPTY_BODY, "job_web_empty_body"),
        ("duplicate_page", None, WebErrorKind.DUPLICATE, "job_web_duplicate"),
        (
            "sensitive_url_query",
            None,
            WebErrorKind.POLICY_DENIED_CANDIDATE,
            "job_web_candidate_policy_denied",
        ),
        (
            "browser_no_open_page",
            None,
            WebErrorKind.BROWSER_CONTEXT_LOST,
            "job_web_browser_context_lost",
        ),
    ],
)
def test_classification_has_stable_public_codes(error_code, status_code, kind, stable_code) -> None:
    classified = JobWebErrorPolicy().classify(error_code=error_code, status_code=status_code)

    assert classified.kind is kind
    assert classified.code == stable_code


def test_recoverable_retries_are_bounded_and_backoff_is_capped() -> None:
    policy = JobWebErrorPolicy(max_retry_delay_seconds=2)

    decisions = [policy.decide(WebErrorKind.LOAD_FAILURE, occurrence=n) for n in (1, 2, 3)]

    assert [(item.action, item.delay_seconds) for item in decisions] == [
        ("retry", 1), ("retry", 2), ("next_candidate", 0),
    ]


def test_empty_body_only_retries_one_snapshot_and_render_timeout_uses_two_wait_tiers() -> None:
    policy = JobWebErrorPolicy()

    assert policy.decide(WebErrorKind.EMPTY_BODY, occurrence=1).action == "retry_snapshot"
    assert policy.decide(WebErrorKind.EMPTY_BODY, occurrence=2).action == "next_candidate"
    assert [policy.decide(WebErrorKind.RENDER_TIMEOUT, occurrence=n).action for n in (1, 2, 3)] == [
        "retry_wait", "retry_wait", "next_candidate",
    ]


def test_not_found_duplicate_and_three_unrecoverable_candidates_never_retry_original() -> None:
    policy = JobWebErrorPolicy()

    assert policy.decide(WebErrorKind.NOT_FOUND, occurrence=1).action == "next_candidate"
    assert policy.decide(WebErrorKind.DUPLICATE, occurrence=1).action == "deduplicate"
    assert policy.decide(WebErrorKind.NOT_FOUND, occurrence=1, consecutive_unrecoverable=3).action == "stop"


def test_candidate_policy_denial_moves_to_next_candidate_without_retry() -> None:
    policy = JobWebErrorPolicy()
    classified = policy.classify(error_code="sensitive_url_query")

    assert classified.kind is WebErrorKind.POLICY_DENIED_CANDIDATE
    assert policy.decide(classified.kind, occurrence=1).action == "next_candidate"


def test_lost_browser_context_moves_to_next_candidate_without_wait_retry() -> None:
    policy = JobWebErrorPolicy()
    classified = policy.classify(error_code="browser_no_open_page")

    assert classified.kind is WebErrorKind.BROWSER_CONTEXT_LOST
    assert policy.decide(classified.kind, occurrence=1).action == "next_candidate"


@pytest.mark.parametrize("error_code", ["login_required", "captcha", "permission_denied", "robots_denied", "site_denied"])
def test_access_blocks_never_retry_or_bypass(error_code) -> None:
    policy = JobWebErrorPolicy()
    classified = policy.classify(error_code=error_code)

    assert classified.kind is WebErrorKind.ACCESS_BLOCKED
    assert policy.decide(classified.kind, occurrence=1, failure_behavior="wait_for_user").action == "wait_for_user"
    assert policy.decide(classified.kind, occurrence=1, failure_behavior="allow_partial").action == "partial"


def test_redirect_hops_are_bounded() -> None:
    policy = JobWebErrorPolicy(max_redirects=2)

    assert policy.decide(WebErrorKind.REDIRECT, occurrence=1).action == "follow_redirect"
    assert policy.decide(WebErrorKind.REDIRECT, occurrence=2).action == "follow_redirect"
    assert policy.decide(WebErrorKind.REDIRECT, occurrence=3).action == "stop"


@pytest.mark.asyncio
async def test_cross_origin_final_url_without_verifiable_redirect_chain_fails_closed() -> None:
    from starter_agent.delegation.specialists.job_web_researcher import WebResearchLimits, _Progress

    progress = _Progress(WebResearchLimits(10, 30, 35), urls=["https://jobs.example.test/role"])
    await progress.tool_event({
        "type": "tool_completed", "name": "mcp__playwright__browser_navigate", "ok": True,
        "requested_url": "https://jobs.example.test/role", "final_url": "https://other.example.test/role",
        "metadata": {},
    })

    assert progress.stop_reason == "redirect_chain_unverifiable"
    assert progress.page_count == 0


@pytest.mark.asyncio
async def test_redirect_chain_each_hop_is_validated_and_hard_limited() -> None:
    from starter_agent.delegation.specialists.job_web_researcher import WebResearchLimits, _Progress

    validated = []
    async def validate(urls):
        validated.extend(urls)

    progress = _Progress(WebResearchLimits(10, 30, 35), urls=["https://jobs.example.test/role"], redirect_validator=validate, max_redirects=2)
    await progress.tool_event({
        "type": "tool_completed", "name": "mcp__playwright__browser_navigate", "ok": True,
        "requested_url": "https://jobs.example.test/role", "final_url": "https://other.example.test/final",
        "metadata": {"redirect_chain": ["https://redirect.example.test/one", "https://other.example.test/final"]},
    })
    assert validated == ["https://redirect.example.test/one", "https://other.example.test/final"]
    assert progress.stop_reason is None

    await progress.tool_event({
        "type": "tool_completed", "name": "mcp__playwright__browser_navigate", "ok": True,
        "requested_url": "https://jobs.example.test/role", "final_url": "https://third.example.test/final",
        "metadata": {"redirect_chain": ["https://a.example.test/", "https://b.example.test/", "https://third.example.test/final"]},
    })
    assert progress.stop_reason == "redirect_limit"


@pytest.mark.asyncio
async def test_contract_cannot_expand_redirect_hard_limit() -> None:
    from tests.unit.test_job_web_researcher import _context, _inputs, _runtime, _spec
    from starter_agent.delegation.specialists.job_web_researcher import JobWebResearcher

    runtime, _provider, _calls = _runtime([{"final": {"jobs": [], "missing": [], "errors": []}}])
    context = _context(); captured = {}
    original = runtime.run
    async def inspect(*, spec, context, on_tool_event):
        captured["probe"] = context.tool_preflight_probe.__self__.max_redirects
        return await original(spec=spec, context=context, on_tool_event=on_tool_event)
    runtime.run = inspect
    await JobWebResearcher(runtime).run(_spec(), context, _inputs(urls=["https://jobs.example.test/x"], max_redirects=999))
    assert captured["probe"] == 5
