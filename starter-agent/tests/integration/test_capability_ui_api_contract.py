from pathlib import Path
import json
import subprocess


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))
LOGIC_START = "/* capability-ui-logic:start */"
LOGIC_END = "/* capability-ui-logic:end */"


def run_capability_logic(expression: str):
    assert LOGIC_START in HTML, "executable capability UI logic block is missing"
    source = HTML.split(LOGIC_START, 1)[1].split(LOGIC_END, 1)[0]
    program = f"{source}\nprocess.stdout.write(JSON.stringify({expression}));"
    result = subprocess.run(
        ["node", "-e", program],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def function_source(name: str, next_name: str) -> str:
    start = HTML.index(f"function {name}(")
    end = HTML.index(f"function {next_name}(", start)
    return HTML[start:end]


def test_capability_ui_calls_only_task11_management_endpoints() -> None:
    for endpoint in (
        "/v1/capabilities/servers",
        "/health-check",
        "/connect",
        "/disconnect",
        "/refresh",
        "/enable",
        "/disable",
        "/v1/capabilities/tools",
        "/schema",
        "/review",
        "/policies",
        "/v1/capabilities/skills",
        "/reload",
        "/raw",
        "/v1/capabilities/confirmations/",
        "/decisions",
    ):
        assert endpoint in HTML


def test_mutations_use_cas_loading_locks_and_authoritative_rereads() -> None:
    for contract in (
        "expected_revision",
        '"If-Match"',
        "capabilityState.pendingOperations",
        "isCapabilityOperationPending",
        "finally",
        "await loadCapabilityServer",
        "await loadCapabilityTool",
        "await loadCapabilitySkills",
        "await loadCapabilitySkill",
    ):
        assert contract in HTML


def test_dangerous_operations_use_management_confirmation_decisions() -> None:
    for contract in (
        "renderCapabilityConfirmation",
        "decideCapabilityConfirmation",
        "confirmation.revision",
        "idempotency_key",
        'decision: "once"',
        'decision: "cancel"',
    ):
        assert contract in HTML


def test_single_server_refresh_is_scoped_to_the_selected_server() -> None:
    assert "async function refreshCapabilityServer" in HTML
    assert "await loadCapabilityServer(serverId)" in HTML
    assert "loadCapabilityServers()" not in HTML[
        HTML.index("async function refreshCapabilityServer") :
        HTML.index("async function refreshCapabilityServer") + 700
    ]


def test_primary_routes_are_resolved_entirely_from_hashes() -> None:
    resolved = run_capability_logic(
        """[
          CapabilityUiLogic.resolvePrimaryRoute(""),
          CapabilityUiLogic.resolvePrimaryRoute("#/chat"),
          CapabilityUiLogic.resolvePrimaryRoute("#/knowledge"),
          CapabilityUiLogic.resolvePrimaryRoute("#/capabilities/mcp-servers"),
          CapabilityUiLogic.resolvePrimaryRoute("#/capabilities/skills"),
          CapabilityUiLogic.resolvePrimaryRoute("#/unknown")
        ]"""
    )
    assert resolved == [
        "chat",
        "chat",
        "knowledge",
        "mcp-servers",
        "skills",
        "chat",
    ]


def test_confirmation_target_lock_and_safe_detail_model_are_behavioral() -> None:
    result = run_capability_logic(
        """(() => {
          const confirmation = {
            id: "confirmation-1",
            destination: "alpha",
            risk: "dangerous",
            arguments_summary: {
              operation: "server.refresh",
              target: "alpha",
              diff: {connection_state: ["ready", "ready"]},
              risk: "external_process_lifecycle",
              impact: ["server:alpha", "tool_availability"],
              payload: {candidate_hash: "<unsafe>&"}
            }
          };
          const key = CapabilityUiLogic.confirmationTargetKey(confirmation);
          return {
            key,
            locked: CapabilityUiLogic.isTargetLocked(key, [key]),
            details: CapabilityUiLogic.confirmationDetails(confirmation)
          };
        })()"""
    )
    assert result["key"] == "server:alpha"
    assert result["locked"] is True
    assert result["details"]["diff"] == [
        {"field": "connection_state", "before": "ready", "after": "ready"}
    ]
    assert result["details"]["risk"] == "external_process_lifecycle"
    assert result["details"]["impact"] == ["server:alpha", "tool_availability"]
    assert result["details"]["data"]["candidate_hash"] == "<unsafe>&"


def test_raw_definition_access_requires_admin_and_explicit_expansion() -> None:
    result = run_capability_logic(
        """[
          CapabilityUiLogic.canLoadRawDefinition("admin", true),
          CapabilityUiLogic.canLoadRawDefinition("admin", false),
          CapabilityUiLogic.canLoadRawDefinition("viewer", true),
          CapabilityUiLogic.canLoadRawDefinition("operator", true)
        ]"""
    )
    assert result == [True, False, False, False]


def test_request_epoch_rejects_cross_route_selection_and_api_commits() -> None:
    result = run_capability_logic(
        """(() => {
          const token = {
            epoch: 7,
            route: "skills",
            selection: "research",
            apiBase: "http://127.0.0.1:8000"
          };
          return [
            CapabilityUiLogic.isRequestCurrent(token, {...token}),
            CapabilityUiLogic.isRequestCurrent(token, {...token, epoch: 8}),
            CapabilityUiLogic.isRequestCurrent(token, {...token, route: "mcp-servers"}),
            CapabilityUiLogic.isRequestCurrent(token, {...token, selection: "other"}),
            CapabilityUiLogic.isRequestCurrent(
              token,
              {...token, apiBase: "http://127.0.0.1:9000"}
            )
          ];
        })()"""
    )
    assert result == [True, False, False, False, False]


def test_hashchange_drives_every_primary_view_and_navigation_preserves_history() -> None:
    source = function_source("applyPrimaryHashRoute", "setKnowledgeStatus")
    compact = " ".join(source.split())
    assert 'CapabilityUiLogic.resolvePrimaryRoute( window.location.hash )' in compact
    assert 'showPrimaryView("chat")' in source
    assert 'showPrimaryView("knowledge")' in source
    assert 'showPrimaryView("capabilities")' in source
    assert "history.replaceState" not in HTML
    for route_hash in (
        "#/chat",
        "#/knowledge",
        "#/capabilities/mcp-servers",
        "#/capabilities/skills",
    ):
        assert route_hash in HTML


def test_confirmation_queue_is_recovered_and_proposal_target_remains_locked() -> None:
    for contract in (
        "/v1/capabilities/confirmations/pending",
        "loadCapabilityConfirmations",
        "capabilityState.confirmations",
        "confirmationTargetKey",
        "isCapabilityTargetLocked",
        "renderCapabilityConfirmations",
    ):
        assert contract in HTML


def test_raw_definition_is_not_part_of_ordinary_skill_detail_lifecycle() -> None:
    load_skill = function_source(
        "loadCapabilitySkill", "mutateCapabilityServer"
    )
    assert "/raw" not in load_skill
    assert "loadCapabilityRawDefinition" in HTML
    assert "clearCapabilityRawState" in HTML
    for nonexistent in (
        "skill.trigger_examples",
        "skill.negative_examples",
        "skill.validation",
        "skill.failure_policy",
    ):
        assert nonexistent not in HTML


def test_async_capability_loads_guard_commits_with_request_context() -> None:
    for function_name, next_name in (
        ("loadCapabilityServers", "loadCapabilityServer"),
        ("loadCapabilityServer", "loadCapabilityTool"),
        ("loadCapabilityTool", "loadCapabilitySkills"),
        ("loadCapabilitySkills", "loadCapabilitySkill"),
        ("loadCapabilitySkill", "mutateCapabilityServer"),
    ):
        source = function_source(function_name, next_name)
        assert "captureCapabilityRequest" in source
        assert "isCapabilityRequestCurrent" in source


def test_management_pending_snapshot_replaces_terminal_records_and_filters_identity() -> None:
    result = run_capability_logic(
        """CapabilityUiLogic.reconcileManagementConfirmations(
          [
            {id: "kept", server_id: "management", session_id: "management"},
            {id: "foreign-server", server_id: "playwright", session_id: "management"},
            {id: "foreign-session", server_id: "management", session_id: "chat-1"}
          ],
          [
            {id: "terminal", server_id: "management", session_id: "management"},
            {id: "proposal", server_id: "management", session_id: "management"}
          ],
          ["proposal"]
        ).map(item => item.id)"""
    )
    assert result == ["kept", "proposal"]


def test_management_pending_request_is_scoped_and_decisions_reconcile_authority() -> None:
    load_pending = function_source(
        "loadCapabilityConfirmations", "refreshCapabilityAuthorityForConfirmation"
    )
    decide = function_source(
        "decideCapabilityConfirmation", "refreshCapabilityRoute"
    )
    assert (
        '"/v1/capabilities/confirmations/pending?session_id=management"'
        in load_pending
    )
    assert "reconcileManagementConfirmations" in load_pending
    assert "await loadCapabilityConfirmations()" in decide
    assert "await refreshCapabilityAuthorityForConfirmation(confirmation)" in decide


def test_raw_definition_clear_is_synchronously_reflected_before_new_requests() -> None:
    clear_raw = function_source(
        "clearCapabilityRawState", "renderCapabilityRawDefinition"
    )
    load_raw = function_source(
        "loadCapabilityRawDefinition", "renderCapabilitySkillDetail"
    )
    assert "renderCurrentCapabilityState()" in clear_raw
    assert load_raw.index("clearCapabilityRawState()") < load_raw.index(
        "capabilityRequest("
    )


def test_background_authority_refresh_ignores_route_but_rejects_identity_or_api_changes() -> None:
    result = run_capability_logic(
        """(() => {
          const token = {apiBase: "http://a", identityRevision: 4};
          return [
            CapabilityUiLogic.isAuthorityRequestCurrent(
              token,
              {apiBase: "http://a", identityRevision: 4, route: "skills"}
            ),
            CapabilityUiLogic.isAuthorityRequestCurrent(
              token,
              {apiBase: "http://b", identityRevision: 4}
            ),
            CapabilityUiLogic.isAuthorityRequestCurrent(
              token,
              {apiBase: "http://a", identityRevision: 5}
            )
          ];
        })()"""
    )
    assert result == [True, False, False]


def test_confirmation_authority_refresh_updates_cache_without_rendering_or_loaders() -> None:
    source = function_source(
        "refreshCapabilityAuthorityForConfirmation",
        "decideCapabilityConfirmation",
    )
    for contract in (
        "captureCapabilityAuthorityRequest",
        "isCapabilityAuthorityRequestCurrent",
        "capabilityState.serverDetails.set",
        "capabilityState.toolDetails.set",
        "capabilityState.skillDetails.set",
        "/v1/capabilities/servers/",
        "/v1/capabilities/tools/",
        "/v1/capabilities/skills/",
    ):
        assert contract in source
    for forbidden in (
        "loadCapabilityServer(",
        "loadCapabilityTool(",
        "loadCapabilitySkill(",
        "renderCapability",
        "refreshCapabilityRoute(",
    ):
        assert forbidden not in source
