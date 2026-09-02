from pathlib import Path


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))


def test_trust_center_navigation_routes_and_tabs_exist() -> None:
    for contract in (
        'id="trustNavButton"',
        'id="trustView"',
        "#/trust/evals",
        "#/trust/traces",
        "#/trust/safety",
        'id="trustEvalsTab"',
        'id="trustTracesTab"',
        'id="trustSafetyTab"',
        'id="trustEvalsPanel"',
        'id="trustTracesPanel"',
        'id="trustSafetyPanel"',
        "Trust Center",
    ):
        assert contract in HTML


def test_primary_navigation_keeps_four_top_level_items_aligned() -> None:
    for contract in (
        "grid-template-columns: repeat(4, minmax(0, 1fr));",
        ".primary-nav button",
        "white-space: nowrap;",
        'id="trustNavButton" type="button">信任中心</button>',
    ):
        assert contract in HTML


def test_trust_center_calls_real_backend_endpoints() -> None:
    for endpoint in (
        "/v1/trust/suites",
        "/v1/trust/cases",
        "/v1/trust/runs",
        "/case-results",
        "/metrics",
        "/failure-clusters",
        "/gate",
        "/v1/trust/traces",
        "/v1/trust/safety",
    ):
        assert endpoint in HTML

    for function_name in (
        "loadTrustEvals",
        "startTrustEvalRun",
        "loadTrustRunEvidence",
        "loadTrustTraces",
        "loadTrustSafety",
        "renderTrustSafety",
    ):
        assert f"function {function_name}" in HTML


def test_trust_center_does_not_render_static_success_or_mutate_gate_result() -> None:
    forbidden_static_pass = (
        'trustSafetyGate.textContent = "PASS"',
        "trustSafetyGate.textContent = 'PASS'",
        'trustGateStatus.textContent = "PASS"',
        "trustGateStatus.textContent = 'PASS'",
    )
    for forbidden in forbidden_static_pass:
        assert forbidden not in HTML

    for contract in (
        "gate_status",
        "blocking_reasons",
        "renderTrustSafety",
        "trustSafetyPanel",
    ):
        assert contract in HTML


def test_trust_center_uses_dom_apis_for_external_content() -> None:
    for target in (
        "trustEvalRuns",
        "trustEvalCases",
        "trustFailureClusters",
        "trustTraceEvents",
        "trustSafetyEvidence",
    ):
        assert f"{target}.innerHTML" not in HTML
        assert f'id="{target}"' in HTML

    assert "textContent" in HTML
