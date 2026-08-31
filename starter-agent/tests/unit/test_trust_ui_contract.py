from pathlib import Path
import json
import subprocess


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))
TRUST_REQUESTS = WEB / "app/trust-request-state.js"


def test_trust_center_navigation_is_modal_local_and_has_tabs() -> None:
    for contract in (
        'id="trustNavButton"',
        'id="trustView"',
        'openAdvancedWindow("trust", settingsReturnFocus)',
        "function setTrustRoute(route)",
        'advancedWindow.activeType() === "trust"',
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


def test_all_trust_read_loaders_bind_epoch_route_and_abort_ownership() -> None:
    for loader in (
        "loadTrustRunEvidence",
        "loadTrustEvals",
        "loadTrustTraces",
        "loadTrustSafety",
    ):
        start = HTML.index(f"async function {loader}")
        body = HTML[start : HTML.index("\n    }", start) + 6]
        assert "const request = captureTrustRequest(overlayToken);" in body
        assert "const trustRead = captureTrustRead(" in body
        assert "signal: trustState.requestController.signal" in body
        assert "isTrustReadCurrent(trustRead" in body


def test_trust_read_ownership_rejects_same_route_stale_generations_and_selection() -> None:
    module_url = TRUST_REQUESTS.resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createTrustReadOwnership }} from {json.dumps(module_url)};

const ownership = createTrustReadOwnership();
const current = {{ overlay: "trust", overlayEpoch: 3, epoch: 7, route: "evals", apiBase: "http://api" }};
for (const [lane, selection] of Object.entries({{
  "run-evidence": "run-b",
  traces: "case_id=case-b",
  safety: "safety",
  evals: "fixture",
}})) {{
  const first = ownership.capture({{ ...current, lane, selection }});
  const second = ownership.capture({{ ...current, lane, selection }});
  assert.ok(second.generation > first.generation);
  assert.equal(ownership.isCurrent(first, {{ ...current, lane, selection }}), false);
  assert.equal(ownership.isCurrent(second, {{ ...current, lane, selection }}), true);
  assert.equal(ownership.isCurrent(second, {{ ...current, lane, selection: "different" }}), false);
  assert.equal(ownership.isCurrent(second, {{ ...current, overlayEpoch: 4, lane, selection }}), false);
}}
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


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
