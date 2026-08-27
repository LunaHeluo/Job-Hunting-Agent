from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess


WEB = Path("frontend/web")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
STATE = (WEB / "app/shell-state.js").read_text(encoding="utf-8")
MODALS = (WEB / "app/modal-manager.js").read_text(encoding="utf-8")
CSS = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "styles").glob("*.css"))


def _css_rules(css: str, media: str = "") -> list[tuple[str, str, str]]:
    rules: list[tuple[str, str, str]] = []
    cursor = 0
    while cursor < len(css):
        opening = css.find("{", cursor)
        if opening < 0:
            break
        header = css[cursor:opening].strip()
        depth = 1
        closing = opening + 1
        while depth and closing < len(css):
            depth += (css[closing] == "{") - (css[closing] == "}")
            closing += 1
        body = css[opening + 1:closing - 1]
        if header.startswith("@media"):
            rules.extend(_css_rules(body, f"{media} {header}"))
        elif header and not header.startswith("@"):
            rules.append((header, body, media))
        cursor = closing
    return rules


def _media_matches(media: str, width: int) -> bool:
    for kind, value in re.findall(r"\((min|max)-width:\s*(\d+)px\)", media):
        if kind == "min" and width < int(value):
            return False
        if kind == "max" and width > int(value):
            return False
    return True


def _specificity(selector: str) -> tuple[int, int, int]:
    return (selector.count("#"), len(re.findall(r"[.\[:][\w-]+", selector)), 0)


def _effective_declaration(target: str, property_name: str, width: int) -> str | None:
    imports = re.findall(r'@import url\("\./([^"?]+)', (WEB / "styles/app.css").read_text(encoding="utf-8"))
    winner: tuple[tuple[int, int, int], int, str] | None = None
    order = 0
    for imported in imports:
        stylesheet = (WEB / "styles" / imported).read_text(encoding="utf-8")
        for selectors, declarations, media in _css_rules(stylesheet):
            if not _media_matches(media, width):
                continue
            for selector in selectors.split(","):
                if target not in selector:
                    continue
                match = re.search(rf"(?<![-\w]){re.escape(property_name)}\s*:\s*([^;}}]+)", declarations)
                if not match:
                    continue
                candidate = (_specificity(selector), order, match.group(1).strip())
                if winner is None or candidate[:2] >= winner[:2]:
                    winner = candidate
                order += 1
    return winner[2] if winner else None


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


def test_modal_manager_executes_focus_safe_close_and_replacement_behavior() -> None:
    module_url = (WEB / "app/modal-manager.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createModalManager }} from {json.dumps(module_url)};

const documentListeners = new Map();
const focusOrder = [];
globalThis.document = {{
  activeElement: null,
  body: {{ classList: new Set(), }},
  addEventListener(type, handler) {{ documentListeners.set(type, handler); }},
  dispatch(type, event) {{ documentListeners.get(type)?.(event); }},
}};
document.body.classList.add = Set.prototype.add.bind(document.body.classList);
document.body.classList.remove = Set.prototype.delete.bind(document.body.classList);

class Element {{
  constructor(name, parent = null) {{
    this.name = name;
    this.parentElement = parent;
    this.hidden = false;
    this.textContent = "";
    this.listeners = new Map();
    this.focusables = [];
  }}
  addEventListener(type, handler) {{ this.listeners.set(type, handler); }}
  emit(type, event = {{}}) {{ this.listeners.get(type)?.(event); }}
  querySelectorAll() {{ return this.focusables; }}
  closest(selector) {{
    if (selector !== "[hidden]") return null;
    for (let node = this; node; node = node.parentElement) if (node.hidden) return node;
    return null;
  }}
  getClientRects() {{ return this.closest("[hidden]") ? [] : [{{}}]; }}
  contains(target) {{
    for (let node = target; node; node = node.parentElement) if (node === this) return true;
    return false;
  }}
  focus() {{ document.activeElement = this; focusOrder.push(this.name); }}
}}

const overlay = new Element("overlay");
const dialog = new Element("dialog", overlay);
const title = new Element("title", dialog);
const closeButton = new Element("close", dialog);
const knowledgePanel = new Element("knowledge", dialog);
const capabilityPanel = new Element("capabilities", dialog);
const trustPanel = new Element("trust", dialog);
const visibleButton = new Element("visible", knowledgePanel);
const hiddenButton = new Element("hidden", capabilityPanel);
const opener = new Element("settings opener");
const outside = new Element("outside");
dialog.focusables = [closeButton, visibleButton, hiddenButton];
const closed = [];
const manager = createModalManager({{
  overlay, dialog, title, closeButton,
  panels: {{ knowledge: knowledgePanel, capabilities: capabilityPanel, trust: trustPanel }},
  onBeforeClose: type => closed.push(type),
}});
const tab = (shiftKey = false) => {{
  let prevented = false;
  document.dispatch("keydown", {{ key: "Tab", shiftKey, preventDefault: () => {{ prevented = true; }} }});
  assert.equal(prevented, true);
}};

manager.open("knowledge", opener);
assert.equal(document.activeElement, closeButton);
assert.equal(capabilityPanel.hidden, true);
document.activeElement = outside;
tab();
assert.equal(document.activeElement, closeButton);
document.activeElement = outside;
tab(true);
assert.equal(document.activeElement, visibleButton);

const beforeReplace = focusOrder.length;
let replacementType = null;
manager.replace("capabilities", type => {{ replacementType = type; }});
assert.deepEqual(closed, ["knowledge"]);
assert.equal(replacementType, "capabilities");
assert.equal(manager.activeType(), "capabilities");
assert.equal(overlay.hidden, false);
assert.equal(focusOrder.slice(beforeReplace).includes("settings opener"), false);
manager.close();
assert.deepEqual(closed, ["knowledge", "capabilities"]);
assert.equal(document.activeElement, opener);

manager.open("trust", opener);
document.dispatch("keydown", {{ key: "Escape" }});
assert.equal(overlay.hidden, true);
assert.equal(document.activeElement, opener);
manager.open("knowledge", opener);
overlay.emit("click", {{ target: overlay }});
assert.equal(overlay.hidden, true);
manager.open("capabilities", opener);
closeButton.emit("click");
assert.equal(overlay.hidden, true);
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_settings_handoff_uses_the_settings_opener_as_modal_return_target() -> None:
    assert "closeSettings({ restoreFocus: false })" in APP
    assert "advancedWindow.replace(type, nextType => shellState.openOverlay(nextType));" in APP
    for modal_type in ("knowledge", "capabilities", "trust"):
        assert f'openAdvancedWindow("{modal_type}", settingsReturnFocus)' in APP


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


def test_desktop_shell_has_one_equal_height_geometry_contract() -> None:
    for contract in (
        "--app-workspace-block-size: calc(100dvh - var(--app-header-block-size) - 32px)",
        "height: var(--app-workspace-block-size)",
        "grid-template-rows: auto minmax(0, 1fr)",
        "align-items: stretch",
        ".workspace-scroll-region",
        ".context-scroll-region",
        ".agent-scroll-region",
        "overflow: auto",
        "@media (min-width: 1280px)",
    ):
        assert contract in CSS


def test_s1_tokens_are_shared_by_shell_and_workbench() -> None:
    for token in (
        "--app-bg: #f4f1e9", "--app-surface: #fffdf8", "--app-ink: #263c31",
        "--app-accent: #176b4d", "--app-accent-soft: #dcefe3",
    ):
        assert token in CSS
    assert "--wb-bg: var(--app-bg)" in CSS


def test_css_cascade_keeps_desktop_tracks_bounded_and_columns_equal_at_1280() -> None:
    assert _effective_declaration(".workbench-layout", "grid-template-columns", 1280) == (
        "clamp(270px, 18vw, 330px) minmax(620px, 1fr) clamp(260px, 18vw, 340px)"
    )
    assert _effective_declaration(".workbench-layout", "height", 1280) == "var(--app-workspace-block-size)"
    for column in (".workbench-left", ".workbench-main", ".workbench-right"):
        assert _effective_declaration(column, "height", 1280) == "100%"


def test_css_cascade_uses_safe_flow_and_vertical_agent_layout_below_1280() -> None:
    assert _effective_declaration(".workbench-layout", "grid-template-columns", 1024) == "1fr"
    assert _effective_declaration(".workbench-left", "grid-template-rows", 1280) == "auto minmax(0, 1fr)"
    assert _effective_declaration(".workbench-chat-dock", "flex-direction", 1280) == "column"


def test_css_cascade_allows_match_scroll_item_to_shrink_on_desktop() -> None:
    assert _effective_declaration("#workbenchMatchContent", "min-height", 1280) == "0"


def test_application_card_selection_notifies_the_context_rail() -> None:
    module_url = (WEB / "app/features/applications-board.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createApplicationsBoard }} from {json.dumps(module_url)};

class Element {{
  constructor(tagName = "div") {{
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.dataset = {{}};
    this.className = "";
    this.classList = {{
      add: token => {{ this.className = `${{this.className}} ${{token}}`.trim(); }},
      remove: token => {{ this.className = this.className.split(/\\s+/).filter(item => item && item !== token).join(" "); }},
    }};
    this.textContent = "";
    this.value = "";
  }}
  append(...items) {{ this.children.push(...items); }}
  replaceChildren(...items) {{ this.children = [...items]; }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }}
  emit(type) {{ this.listeners.get(type)?.({{ currentTarget: this }}); }}
  setAttribute() {{}}
  add(item) {{ this.children.push(item); }}
  querySelector() {{ return null; }}
}}
globalThis.document = {{ createElement: tagName => new Element(tagName) }};
globalThis.Option = class Option extends Element {{
  constructor(text, value) {{ super("option"); this.textContent = text; this.value = value; }}
}};
const application = {{
  application_id: "app_1", current_status: "applied", priority: 50,
  resume_version_id: "version_1", next_action: "等待通知", remind_at: null,
  events: [], revision: 3,
}};
const main = new Element();
let selected = null;
const board = createApplicationsBoard({{
  elements: {{ main }},
  onApplicationSelect: item => {{ selected = item; }},
  request: async path => path.includes("applications?")
    ? {{ items: [{{ application, job_snapshot: {{ company: "OpenAI", title: "Engineer" }} }}] }}
    : path.includes("funnel") ? {{ definition_version: "v1", stages: [] }} : {{ items: [] }},
}});
await board.render("workspace_1");
const findCard = node => node.className === "application-card"
  ? node : node.children.map(findCard).find(Boolean);
const card = findCard(main);
assert.ok(card, "the rendered board includes an application card");
card.emit("click");
assert.equal(selected, application);
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
