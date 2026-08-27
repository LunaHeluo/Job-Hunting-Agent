from pathlib import Path


WEB = Path("frontend/web")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
STATE = (WEB / "app/shell-state.js").read_text(encoding="utf-8")
CSS = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "styles").glob("*.css"))


def test_shell_state_separates_primary_routes_from_advanced_windows() -> None:
    for name in ("PRIMARY_ROUTES", "ADVANCED_VIEWS", "resolveShellRoute", "createShellState"):
        assert f"{name}" in STATE
    for route in ('"workbench"', '"version-map"', '"applications"'):
        assert route in STATE
    for advanced in ('"knowledge"', '"capabilities"', '"trust"'):
        assert advanced in STATE
    assert 'return "workbench"' in STATE
    assert 'return "chat"' not in STATE


def test_shell_state_guards_late_overlay_requests_and_scroll_positions() -> None:
    for contract in (
        "captureOverlayRequest",
        "isOverlayRequestCurrent",
        "overlayEpoch",
        "rememberScroll",
        "scrollFor",
    ):
        assert contract in STATE


def test_markup_has_one_persistent_shell_and_one_agent_mount() -> None:
    for element_id in (
        "appShell", "primaryNavigation", "workbenchChatDock", "workspaceCanvas",
        "contextRail", "advancedOverlay", "advancedDialog", "advancedTitle",
        "advancedCloseButton",
    ):
        assert HTML.count(f'id="{element_id}"') == 1
    assert HTML.count('id="chatDock"') == 1
    assert 'id="chatView"' not in HTML
    assert 'id="chatNavButton"' not in HTML
    assert 'class="sidebar"' not in HTML
    assert HTML.index('id="chatDock"') > HTML.index('id="workbenchChatDock"')


def test_advanced_modules_live_inside_k1_dialog() -> None:
    window = HTML.split('id="advancedDialog"', 1)[1]
    for view_id in ("knowledgeView", "capabilitiesView", "trustView"):
        assert f'id="{view_id}"' in window
