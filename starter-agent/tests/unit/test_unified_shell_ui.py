from html.parser import HTMLParser
from pathlib import Path


WEB = Path("frontend/web")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
STATE = (WEB / "app/shell-state.js").read_text(encoding="utf-8")
MODALS = (WEB / "app/modal-manager.js").read_text(encoding="utf-8")
CSS = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "styles").glob("*.css"))


class IdTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ancestors_by_id: dict[str, tuple[str, ...]] = {}
        self.ancestor_ids_by_id: dict[str, tuple[str, ...]] = {}
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}
        self.stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ancestors_by_id[element_id] = tuple(tag_name for tag_name, _ in self.stack)
            self.ancestor_ids_by_id[element_id] = tuple(parent_id for _, parent_id in self.stack if parent_id)
            self.attributes_by_id[element_id] = attributes
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}:
            self.stack.append((tag, attributes.get("id")))

    def handle_endtag(self, tag: str) -> None:
        tags = [tag_name for tag_name, _ in self.stack]
        if tag in tags:
            del self.stack[tags[::-1].index(tag) * -1 - 1 :]


def parse_html_tree() -> IdTreeParser:
    parser = IdTreeParser()
    parser.feed(HTML)
    return parser


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


def test_app_uses_primary_only_routing_and_never_moves_chat_dom() -> None:
    for contract in (
        'from "./app/shell-state.js"',
        'from "./app/modal-manager.js"',
        'openAdvancedWindow("knowledge"',
        'openAdvancedWindow("capabilities"',
        'openAdvancedWindow("trust"',
    ):
        assert contract in APP
    assert ".append(chatDock)" not in APP
    assert 'navigatePrimaryHash("#/chat")' not in APP


def test_modal_manager_owns_focus_close_and_replacement() -> None:
    for contract in (
        "createModalManager", "returnFocus", 'event.key === "Escape"',
        'event.key !== "Tab"', "overlay.hidden = false", "overlay.hidden = true",
        "returnFocus?.focus()", "activeType",
    ):
        assert contract in MODALS


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
    tree = parse_html_tree()
    for view_id in ("knowledgeView", "capabilitiesView", "trustView"):
        assert "advancedDialog" in tree.ancestor_ids_by_id[view_id]


def test_advanced_overlay_is_a_dialog_sibling_after_the_persistent_shell() -> None:
    tree = parse_html_tree()
    assert tree.ancestors_by_id["advancedOverlay"] == ("html", "body")
    assert tree.attributes_by_id["advancedDialog"] == {
        "id": "advancedDialog",
        "class": "advanced-dialog",
        "role": "dialog",
        "aria-modal": "true",
        "aria-labelledby": "advancedTitle",
    }
    for context_id in ("advancedTitle", "advancedCloseButton"):
        assert "advancedDialog" in tree.ancestor_ids_by_id[context_id]
