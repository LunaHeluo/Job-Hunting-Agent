from __future__ import annotations

from importlib import import_module
import ipaddress

import pytest


def _policy_module():
    try:
        return import_module("starter_agent.capabilities.policy")
    except ModuleNotFoundError:
        assert False, "Task 7 browser scope policy module is missing"


async def _public_resolver(_host: str):
    return [ipaddress.ip_address("93.184.216.34")]


async def _mixed_resolver(_host: str):
    return [
        ipaddress.ip_address("93.184.216.34"),
        ipaddress.ip_address("127.0.0.1"),
    ]


async def test_browser_scope_reuses_fail_closed_url_and_redirect_validation() -> None:
    policy_module = _policy_module()
    public = policy_module.BrowserScopePolicy(resolver=_public_resolver)
    validated = await public.validate_url("https://jobs.example.com/opening")
    assert validated.hostname == "jobs.example.com"

    mixed = policy_module.BrowserScopePolicy(resolver=_mixed_resolver)
    unsafe_urls = (
        "file:///etc/passwd",
        "https://user:password@jobs.example.com/opening",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/admin",
    )
    for url in unsafe_urls:
        with pytest.raises(policy_module.ScopeDenied, match="unsafe_url"):
            await public.validate_url(url)
    with pytest.raises(policy_module.ScopeDenied, match="unsafe_url"):
        await mixed.validate_url("https://jobs.example.com/opening")
    with pytest.raises(policy_module.ScopeDenied, match="unsafe_redirect"):
        await public.validate_redirects(
            "https://jobs.example.com/opening",
            ("http://127.0.0.1/admin",),
        )


async def test_every_nested_url_and_second_target_is_validated() -> None:
    policy_module = _policy_module()
    policy = policy_module.BrowserScopePolicy(resolver=_public_resolver)
    arguments = {
        "url": "https://jobs.example.com/opening",
        "nested": {
            "callback_uri": "http://127.0.0.1/admin",
        },
    }

    targets = policy_module.extract_url_targets(arguments)
    assert targets == (
        "https://jobs.example.com/opening",
        "http://127.0.0.1/admin",
    )
    with pytest.raises(policy_module.ScopeDenied, match="unsafe_url"):
        await policy.validate_all(targets)


async def test_browser_execution_guard_rechecks_redirect_and_dns_rebinding() -> None:
    policy_module = _policy_module()
    resolutions = 0

    async def rebinding_resolver(_host: str):
        nonlocal resolutions
        resolutions += 1
        if resolutions == 1:
            return [ipaddress.ip_address("93.184.216.34")]
        return [ipaddress.ip_address("127.0.0.1")]

    policy = policy_module.BrowserScopePolicy(resolver=rebinding_resolver)
    await policy.validate_all(("https://jobs.example.com/opening",))
    with pytest.raises(policy_module.ScopeDenied, match="unsafe_url"):
        await policy.validate_all(("https://jobs.example.com/opening",))

    redirect_policy = policy_module.BrowserScopePolicy(resolver=_public_resolver)
    with pytest.raises(policy_module.ScopeDenied, match="unsafe_redirect"):
        await redirect_policy.validate_redirects(
            "https://jobs.example.com/opening",
            ("http://127.0.0.1/final",),
        )


async def test_browser_scope_control_origin_is_exact_and_opt_in() -> None:
    policy_module = _policy_module()
    policy = policy_module.BrowserScopePolicy(
        resolver=_public_resolver,
        control_origins=("http://127.0.0.1:43127",),
    )

    assert await policy.validate_url(
        "http://127.0.0.1:43127/#/capabilities/mcp-servers"
    )
    for url in (
        "http://127.0.0.1:43128/",
        "http://localhost:43127/",
        "http://192.168.1.10:43127/",
        "https://127.0.0.1:43127/",
    ):
        with pytest.raises(policy_module.ScopeDenied, match="unsafe_url"):
            await policy.validate_url(url)


def test_browser_sensitive_outbound_and_serpapi_fields_are_denied() -> None:
    policy_module = _policy_module()
    browser = policy_module.BrowserScopePolicy(resolver=_public_resolver)

    with pytest.raises(policy_module.ScopeDenied, match="sensitive_outbound"):
        browser.validate_outbound(("resume",), 100)
    with pytest.raises(policy_module.ScopeDenied, match="outbound_budget"):
        browser.validate_outbound(("job_keywords",), 10_001, max_bytes=10_000)

    policy_module.validate_serpapi_payload(
        {"keywords": "AI product manager", "location": "Shanghai"},
        ("job_keywords", "location"),
    )
    policy_module.validate_serpapi_payload(
        {"query": "上海的ai agent jobs", "limit": 5},
        (),
    )
    with pytest.raises(policy_module.ScopeDenied, match="serpapi_fields"):
        policy_module.validate_serpapi_payload(
            {"keywords": "AI PM", "resume": "private resume"},
            ("job_keywords", "resume"),
        )


def test_browser_click_accepts_only_structured_playwright_reference() -> None:
    policy_module = _policy_module()
    policy_module.validate_browser_payload(
        "click", {"element": "Refresh", "ref": "e42"}
    )
    with pytest.raises(policy_module.ScopeDenied, match="browser_payload"):
        policy_module.validate_browser_payload(
            "click", {"element": "Refresh", "ref": "e42", "script": "submit()"}
        )


def test_browser_wait_for_accepts_only_bounded_official_schema_fields() -> None:
    policy_module = _policy_module()
    policy_module.validate_browser_payload("read", {"time": 1})
    policy_module.validate_browser_payload("read", {"text": "Job details"})
    with pytest.raises(policy_module.ScopeDenied, match="browser_payload"):
        policy_module.validate_browser_payload("read", {"time": 36})
    with pytest.raises(policy_module.ScopeDenied, match="browser_payload"):
        policy_module.validate_browser_payload("read", {"time": 1, "script": "submit()"})
