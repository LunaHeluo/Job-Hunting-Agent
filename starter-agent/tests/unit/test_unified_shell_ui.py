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


def test_conversation_management_actions_belong_to_starter_agent() -> None:
    tree = parse_html_tree()
    for button_id in ("newSessionButton", "clearAllSessionsButton"):
        assert "workbenchAgentCard" in tree.ancestor_ids_by_id[button_id]
        assert "workbenchAgentConversationActions" in tree.ancestor_ids_by_id[button_id]


def test_legacy_shell_and_conflicting_height_rules_are_absent() -> None:
    assert 'id="chatView"' not in HTML
    assert 'class="sidebar"' not in HTML
    assert ".append(chatDock)" not in APP
    assert 'showPrimaryView("chat")' not in APP
    assert "body.workbench-active { overflow-y: auto; }" not in CSS
    assert ".workbench-agent-card { height: min(760px" not in CSS


def test_desktop_acceptance_sizes_are_documented() -> None:
    qa = Path("docs/superpowers/qa/2026-08-26-unified-desktop-workbench.md").read_text(encoding="utf-8")
    for viewport in ("1280x800", "1440x900", "1920x1080"):
        assert viewport in qa


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
        "--app-bg: #e4ebf0", "--app-surface: #fffdf8", "--app-line: #cbd5d9",
        "--app-ink: #263c31",
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
    assert _effective_declaration(".workbench-left", "grid-template-rows", 1023) == "auto minmax(470px,1fr)"
    assert _effective_declaration(".workbench-chat-dock", "flex-direction", 1280) == "column"


def test_desktop_left_rail_keeps_reference_ratio_and_visible_surface_layers() -> None:
    assert _effective_declaration(".workbench-left", "grid-template-rows", 1280) == (
        "minmax(0, 1fr) minmax(0, 4fr)"
    )
    assert _effective_declaration(".workbench-profile-card", "min-height", 1280) == "0"

    def rgb(css_color: str) -> tuple[int, int, int]:
        value = css_color.removeprefix("#")
        return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))

    background = rgb(_effective_declaration(":root", "--app-bg", 1280) or "")
    surface = rgb(_effective_declaration(":root", "--app-surface", 1280) or "")
    contrast_distance = sum((left - right) ** 2 for left, right in zip(background, surface)) ** 0.5
    assert contrast_distance >= 30
    assert _effective_declaration(".workbench-card", "box-shadow", 1280) not in (None, "none")


def test_css_cascade_allows_match_scroll_item_to_shrink_on_desktop() -> None:
    assert _effective_declaration("#workbenchMatchContent", "min-height", 1280) == "0"


def test_desktop_profile_summary_uses_compact_vertical_spacing() -> None:
    assert _effective_declaration(
        "body.workbench-active .workbench-profile-identity", "padding", 1280
    ) == "10px 14px 7px"
    assert _effective_declaration(
        "body.workbench-active .workbench-profile-metrics", "padding", 1280
    ) == "8px 14px"
    assert (
        "body.workbench-active .workbench-profile-footer { padding: 8px 14px;"
        in CSS
    )
    assert _effective_declaration(
        "body.workbench-active .workbench-version-picker summary", "padding", 1280
    ) == "7px 14px"


def test_left_rail_uses_shared_profile_colors_and_smaller_auxiliary_type() -> None:
    for contract in (
        ".workbench-profile-card { padding: 0; overflow: hidden; border-color: var(--wb-line);",
        "background: var(--wb-paper);",
        "border-bottom: 1px solid var(--wb-line);",
        "--wb-aux-font-size: 11px;",
        "--wb-micro-font-size: 10px;",
        ".workbench-agent-conversation-actions",
    ):
        assert contract in CSS
    assert "background: #faf4e8" not in CSS


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
  emit(type, event = {{}}) {{ this.listeners.get(type)?.({{ currentTarget: this, target: this, ...event }}); }}
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
selected = null;
let cardPrevented = false;
card.emit("keydown", {{ key: "Enter", preventDefault: () => {{ cardPrevented = true; }} }});
assert.equal(cardPrevented, true);
assert.equal(selected, application);
selected = null;
let descendantPrevented = false;
card.emit("keydown", {{
  key: " ", target: card.children[0],
  preventDefault: () => {{ descendantPrevented = true; }},
}});
assert.equal(descendantPrevented, false, "card selection does not cancel a descendant control");
assert.equal(selected, null, "a descendant key event does not select the card");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_version_and_application_selection_round_trip_with_their_context() -> None:
    module_url = (WEB / "app/features/resume-workspace.js").resolve().as_uri()
    application_module_url = (WEB / "app/features/applications-board.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createResumeWorkspace }} from {json.dumps(module_url)};
import {{ createApplicationsBoard }} from {json.dumps(application_module_url)};

class Element {{
  constructor(tagName = "div") {{
    this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = new Map();
    this.dataset = {{}}; this.className = ""; this.textContent = ""; this.value = "";
    this.style = {{ setProperty() {{}} }}; this.parentElement = null;
    this.classList = {{
      add: token => {{ this.className = `${{this.className}} ${{token}}`.trim(); }},
      remove: token => {{ this.className = this.className.split(/\\s+/).filter(item => item && item !== token).join(" "); }},
      toggle: (token, active) => active ? this.classList.add(token) : this.classList.remove(token),
    }};
  }}
  append(...items) {{ for (const item of items) {{
    if (item.parentElement) item.parentElement.children = item.parentElement.children.filter(child => child !== item);
    item.parentElement = this; this.children.push(item);
  }} }}
  add(item) {{ this.append(item); }}
  replaceChildren(...items) {{ for (const child of this.children) child.parentElement = null; this.children = []; this.append(...items); }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }}
  emit(type, event = {{}}) {{ this.listeners.get(type)?.({{ currentTarget: this, target: this, ...event }}); }}
  setAttribute(name, value) {{ this[name] = value; }}
  querySelectorAll(selector) {{
    const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    if (selector.startsWith(".")) return all.filter(item => item.className.split(/\\s+/).includes(selector.slice(1)));
    if (selector === '[role="treeitem"]') return all.filter(item => item.role === "treeitem");
    return [];
  }}
  querySelector(selector) {{ return this.querySelectorAll(selector)[0] || null; }}
  get lastElementChild() {{ return this.children.at(-1) || null; }}
  contains(target) {{ for (let node = target; node; node = node.parentElement) if (node === this) return true; return false; }}
  focus() {{}}
}}
globalThis.document = {{ createElement: tagName => new Element(tagName), querySelector: () => null }};
globalThis.Option = class Option extends Element {{
  constructor(text, value) {{ super("option"); this.textContent = text; this.value = value; }}
}};
globalThis.window = {{ dispatchEvent() {{}}, clearTimeout() {{}}, setTimeout: callback => callback() }};
globalThis.CustomEvent = class CustomEvent {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
const contextContent = new Element(); const inspectorMount = new Element(); contextContent.append(inspectorMount);
const main = new Element();
let versionRouteCurrent = true;
const workspace = createResumeWorkspace({{
  apiBase: () => "", elements: {{ main, resumes: new Element(), jobs: inspectorMount, status: new Element() }},
  reloadHome: () => {{}},
  onVersionSelect: (_node, selection) => contextContent.replaceChildren(selection?.inspectorMount || new Element()),
  request: async path => path.includes("version-map")
    ? {{ nodes: [{{ version_id: "version_1", branch_id: "branch_1", label: "基础版本", status: "confirmed", node_type: "base", revision: 7 }}], edges: [] }}
    : path.includes("view-preference") ? {{ node_positions: {{}}, collapsed_branch_ids: [], viewport_zoom: 1 }}
    : {{ items: [] }},
}});
await workspace.renderVersionMap("workspace_1", "resume_1", {{ isCurrent: () => versionRouteCurrent }});
const button = main.querySelectorAll(".version-node")[0];
assert.ok(button, "the real graph rendered a version node");
button.emit("click", {{ shiftKey: false }});
const findControl = node => node.tagName === "BUTTON"
  ? node : node.children.map(findControl).find(Boolean);
const inspectorControl = findControl(contextContent);
assert.ok(inspectorControl, "the version inspector remains visible in the context rail");
assert.equal(contextContent.contains(inspectorControl), true);
const newerRoute = new Element(); newerRoute.className = "newer-route";
versionRouteCurrent = false;
main.replaceChildren(newerRoute);
await workspace.renderVersionMap("workspace_1", "resume_1");
assert.equal(main.children[0], newerRoute, "a Version Map user reload retains its original route guard");
versionRouteCurrent = true;
contextContent.replaceChildren(new Element());
await workspace.renderVersionMap("workspace_1", "resume_1", {{ isCurrent: () => versionRouteCurrent }});
const restoredVersion = main.querySelectorAll(".version-node")[0];
assert.equal(restoredVersion["aria-current"], "true", "the selected version is active after returning to Version Map");
assert.ok(findControl(contextContent), "the selected version inspector is rebuilt after returning");

const application = {{
  application_id: "app_1", current_status: "applied", priority: 50,
  resume_version_id: "version_1", next_action: "等待通知", remind_at: null,
  events: [], revision: 3,
}};
const applicationMain = new Element();
const selectedApplications = [];
const board = createApplicationsBoard({{
  elements: {{ main: applicationMain }},
  onApplicationSelect: item => selectedApplications.push(item?.application_id || null),
  request: async path => path.includes("applications?")
    ? {{ items: [{{ application, job_snapshot: {{ company: "OpenAI", title: "Engineer" }} }}] }}
    : path.includes("funnel") ? {{ definition_version: "v1", stages: [] }} : {{ items: [] }},
}});
await board.render("workspace_1");
applicationMain.querySelectorAll(".application-card")[0].emit("click");
applicationMain.replaceChildren(new Element());
await board.render("workspace_1");
const restoredApplication = applicationMain.querySelectorAll(".application-card")[0];
assert.ok(restoredApplication.className.split(/\\s+/).includes("is-active"), "the selected application is active after returning");
assert.deepEqual(selectedApplications, ["app_1", "app_1"], "the application context is rebuilt after returning");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_route_activation_coordinator_restores_only_the_latest_route() -> None:
    module_url = (WEB / "app/features/workbench-shell.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createRouteActivationCoordinator }} from {json.dumps(module_url)};

const deferred = new Map();
const restored = [];
const scheduled = [];
let renderedRoute = "workbench";
const captured = [];
const scrollTop = {{ workbench: 11, "version-map": 29, applications: 47 }};
const coordinator = createRouteActivationCoordinator({{
  activate: route => new Promise(resolve => deferred.set(route, resolve)),
  onStart() {{ captured.push([renderedRoute, scrollTop[renderedRoute]]); }},
  onActivated: route => {{ renderedRoute = route; }},
  onCurrent: route => restored.push(route),
  schedule: callback => scheduled.push(callback),
}});
const first = coordinator.apply("version-map");
deferred.get("version-map")();
await first;
assert.deepEqual(captured, [["workbench", 11]]);
assert.equal(renderedRoute, "version-map", "ownership advances before its restore frame");
const second = coordinator.apply("applications");
assert.deepEqual(captured, [["workbench", 11], ["version-map", 29]]);
deferred.get("applications")();
await second;
scheduled.splice(0).forEach(callback => callback());
assert.deepEqual(restored, ["applications"]);
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "createRouteActivationCoordinator" in APP


def test_real_shell_discards_late_version_map_render_after_applications_renders() -> None:
    module_url = (WEB / "app/features/workbench-shell.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createWorkbenchShell }} from {json.dumps(module_url)};

class Element {{
  constructor(tagName = "div") {{
    this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = new Map();
    this.dataset = {{}}; this.className = ""; this.textContent = ""; this.value = "";
    this.style = {{ setProperty() {{}} }}; this.classList = {{ add() {{}}, remove() {{}} }};
    this.hidden = false; this.disabled = false;
  }}
  append(...items) {{ this.children.push(...items); }}
  replaceChildren(...items) {{ this.children = [...items]; }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }}
  setAttribute() {{}}
  querySelector() {{ return null; }}
  closest() {{ return new Element(); }}
  add(item) {{ this.children.push(item); }}
}}
globalThis.document = {{ createElement: tagName => new Element(tagName), querySelector: () => null }};
globalThis.Option = class Option extends Element {{ constructor(text, value) {{ super("option"); this.textContent = text; this.value = value; }} }};
globalThis.localStorage = {{ getItem: () => "workspace_1", setItem() {{}} }};
globalThis.window = {{ dispatchEvent() {{}}, clearTimeout() {{}}, setTimeout() {{}} }};
globalThis.CustomEvent = class CustomEvent {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
let resolveMap;
const response = payload => ({{ ok: true, json: async () => payload }});
globalThis.fetch = async url => {{
  const path = String(url);
  if (path.includes("/version-map")) return new Promise(resolve => {{ resolveMap = () => resolve(response({{ nodes: [], edges: [] }})); }});
  if (path.includes("/view-preference")) return response({{ node_positions: {{}}, collapsed_branch_ids: [], viewport_zoom: 1 }});
  if (path.includes("workspaces?")) return response({{ items: [{{ workspace_id: "workspace_1", name: "Target" }}] }});
  if (path.includes("/home")) return response({{
    stats: {{ resume_count: 1, job_count: 1, active_operation_count: 0 }},
    recent_versions: [{{ resume_id: "resume_1", version_id: "version_1", label: "Base" }}],
    workspace: {{ workspace_id: "workspace_1", name: "Target", revision: 1 }},
  }});
  if (path.includes("/applications?")) return response({{ items: [] }});
  if (path.includes("/analytics/funnel")) return response({{ definition_version: "v1", stages: [] }});
  if (path.includes("/reminders?")) return response({{ items: [] }});
  if (path.includes("/content?")) return response({{ markdown: "", profile: null }});
  return response({{ items: [] }});
}};
const element = () => new Element();
const main = element();
const shell = createWorkbenchShell({{
  getApiBase: () => "",
  elements: {{
    status: element(), workspace: element(), jobCount: element(), jobList: element(), agentContext: element(), operationCards: element(),
    mode: element(), title: element(), main, match: element(), archiveTab: element(), matchTab: element(), view: element(), candidateRail: element(),
    stageResume: element(), stageJob: element(), stageAnalysis: element(), stageEyebrow: element(), stageTitle: element(), stageDescription: element(),
    stagePrimary: element(), stageSecondary: element(), agentActions: element(), taskCenter: element(), tailorResumeButton: element(),
    contextTitle: element(), contextMeta: element(), contextDescription: element(), contextContent: element(), actionBar: element(), actionStatus: element(),
    resumeList: element(),
  }},
}});
const versionMap = shell.activate("version-map");
for (let turn = 0; turn < 8 && !resolveMap; turn += 1) await Promise.resolve();
assert.equal(typeof resolveMap, "function", "Version Map child loader is in flight");
const applications = shell.activate("applications");
await applications;
assert.equal(main.children[0]?.className, "applications-board", "Applications rendered before the late map response");
resolveMap();
await versionMap;
assert.equal(main.children[0]?.className, "applications-board", "late Version Map cannot replace Applications");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def _real_shell_merge_fixture(module_url: str) -> str:
    return r'''
import assert from "node:assert/strict";
import { createWorkbenchShell } from __MODULE_URL__;

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = new Map();
    this.dataset = {}; this.className = ""; this._textContent = ""; this.value = "";
    this.style = { setProperty() {} }; this.parentElement = null; this.hidden = false; this.disabled = false;
    this.classList = {
      add: token => { this.className = `${this.className} ${token}`.trim(); },
      remove: token => { this.className = this.className.split(/\s+/).filter(item => item && item !== token).join(" "); },
      toggle: (token, active) => active ? this.classList.add(token) : this.classList.remove(token),
    };
  }
  get textContent() { return this._textContent; }
  set textContent(value) { this._textContent = String(value); for (const child of this.children) child.parentElement = null; this.children = []; }
  append(...items) { for (const item of items) {
    if (item.parentElement) item.parentElement.children = item.parentElement.children.filter(child => child !== item);
    item.parentElement = this; this.children.push(item);
  } }
  prepend(...items) { this.replaceChildren(...items, ...this.children); }
  replaceChildren(...items) { for (const child of this.children) child.parentElement = null; this.children = []; this._textContent = ""; this.append(...items); }
  add(item) { this.append(item); }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  emit(type, event = {}) { return this.listeners.get(type)?.({ currentTarget: this, target: this, ...event }); }
  setAttribute(name, value) { this[name] = value; }
  querySelectorAll(selector) {
    const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    if (selector.startsWith(".")) {
      const token = selector.slice(1);
      return all.filter(item => item.className.split(/\s+/).includes(token));
    }
    if (selector === '[role="treeitem"]') return all.filter(item => item.role === "treeitem");
    return [];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get lastElementChild() { return this.children.at(-1) || null; }
  contains(target) { for (let node = target; node; node = node.parentElement) if (node === this) return true; return false; }
  closest() { return new Element(); }
  focus() {}
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this); this.parentElement = null; }
}

globalThis.document = { createElement: tagName => new Element(tagName), querySelector: () => null };
globalThis.Option = class Option extends Element { constructor(text, value) { super("option"); this.textContent = text; this.value = value; } };
globalThis.localStorage = { getItem: () => "workspace_1", setItem() {} };
globalThis.window = { dispatchEvent() {}, clearTimeout() {}, setTimeout() { return 1; }, prompt: (_label, fallback) => fallback };
globalThis.CustomEvent = class CustomEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } };

const response = (payload, ok = true, status = 200) => ({ ok, status, json: async () => payload });
const versionMap = {
  nodes: [
    { version_id: "base_1", branch_id: "branch_base", label: "共同祖先", status: "confirmed", node_type: "base", revision: 1 },
    { version_id: "upstream_1", branch_id: "branch_upstream", parent_version_id: "base_1", label: "上游版本", status: "confirmed", node_type: "direction", revision: 2 },
    { version_id: "target_1", branch_id: "branch_target", parent_version_id: "base_1", label: "目标版本", status: "confirmed", node_type: "company", revision: 3 },
  ],
  edges: [],
};

function createShellFixture(createMergeRequest) {
  globalThis.fetch = async (url, options = {}) => {
    const path = String(url);
    if (path.endsWith("/v1/workbench/merge-proposals") && options.method === "POST") return createMergeRequest();
    if (path.includes("/version-map")) return response(versionMap);
    if (path.includes("/view-preference")) return response({ node_positions: {}, collapsed_branch_ids: [], viewport_zoom: 1 });
    if (path.includes("workspaces?")) return response({ items: [{ workspace_id: "workspace_1", name: "Target" }] });
    if (path.includes("/home")) return response({
      stats: { resume_count: 1, job_count: 1, active_operation_count: 0 },
      recent_versions: [{ resume_id: "resume_1", version_id: "target_1", label: "目标版本" }],
      workspace: { workspace_id: "workspace_1", name: "Target", revision: 1 },
    });
    if (path.includes("/applications?")) return response({ items: [] });
    if (path.includes("/analytics/funnel")) return response({ definition_version: "v1", stages: [] });
    if (path.includes("/reminders?")) return response({ items: [] });
    if (path.includes("/content?")) return response({ markdown: "", profile: null });
    return response({ items: [] });
  };
  const element = () => new Element(); const main = element(); const status = element(); const jobList = element(); const contextContent = element();
  const shell = createWorkbenchShell({ getApiBase: () => "", elements: {
    status, workspace: element(), jobCount: element(), jobList, agentContext: element(), operationCards: element(),
    mode: element(), title: element(), main, match: element(), archiveTab: element(), matchTab: element(), view: element(), candidateRail: element(),
    stageResume: element(), stageJob: element(), stageAnalysis: element(), stageEyebrow: element(), stageTitle: element(), stageDescription: element(),
    stagePrimary: element(), stageSecondary: element(), agentActions: element(), taskCenter: element(), tailorResumeButton: element(),
    contextTitle: element(), contextMeta: element(), contextDescription: element(), contextContent, actionBar: element(), actionStatus: element(),
    resumeList: element(),
  } });
  return { shell, main, status, jobList, contextContent };
}

const findByText = (node, text) => node.tagName === "BUTTON" && node.textContent === text
  ? node : node.children.map(child => findByText(child, text)).find(Boolean);
const flush = async () => { for (let turn = 0; turn < 12; turn += 1) await Promise.resolve(); };
async function selectTargetVersion(fixture) {
  await fixture.shell.activate("version-map");
  const target = fixture.main.querySelectorAll(".version-node").find(item => item.dataset.versionId === "target_1");
  assert.ok(target, "the real Version Map rendered the target graph node");
  target.emit("click", { shiftKey: false });
  const merge = findByText(fixture.contextContent, "创建三方合并方案");
  assert.ok(merge, "the real inspector rendered the merge proposal control");
  return merge;
}
'''.replace("__MODULE_URL__", json.dumps(module_url))


def test_real_shell_late_merge_proposal_cannot_overwrite_applications() -> None:
    module_url = (WEB / "app/features/workbench-shell.js").resolve().as_uri()
    harness = _real_shell_merge_fixture(module_url) + r'''
let resolveMerge;
const fixture = createShellFixture(() => new Promise(resolve => { resolveMerge = payload => resolve(response(payload)); }));
const merge = await selectTargetVersion(fixture);
merge.emit("click");
await flush();
assert.equal(typeof resolveMerge, "function", "the real merge proposal POST is in flight");
await fixture.shell.activate("applications");
const applications = fixture.main.children[0];
assert.equal(applications?.className, "applications-board", "Applications rendered while the old merge POST was pending");
resolveMerge({ proposal_id: "proposal_1", status: "ready", revision: 1, decisions: [] });
await flush();
assert.equal(fixture.main.children[0], applications, "late merge proposal cannot replace Applications");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_application_filter_refresh_keeps_its_route_guard_and_refreshes_while_current() -> None:
    module_url = (WEB / "app/features/applications-board.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createApplicationsBoard }} from {json.dumps(module_url)};

class Element {{
  constructor(tagName = "div") {{
    this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = new Map();
    this.dataset = {{}}; this.className = ""; this.textContent = ""; this.value = "";
    this.classList = {{ add() {{}}, remove() {{}} }};
  }}
  append(...items) {{ this.children.push(...items); }}
  replaceChildren(...items) {{ this.children = [...items]; }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }}
  emit(type, event = {{}}) {{ this.listeners.get(type)?.({{ currentTarget: this, target: this, ...event }}); }}
  setAttribute() {{}}
  add(item) {{ this.children.push(item); }}
  querySelector() {{ return null; }}
}}
globalThis.document = {{ createElement: tagName => new Element(tagName) }};
globalThis.Option = class Option extends Element {{ constructor(text, value) {{ super("option"); this.textContent = text; this.value = value; }} }};
const application = {{ application_id: "app_1", current_status: "applied", priority: 1, resume_version_id: "r1", next_action: null, remind_at: null, events: [], revision: 1 }};
let current = true;
let applicationCalls = 0;
const deferred = [];
const request = path => {{
  if (path.includes("applications?")) {{
    applicationCalls += 1;
    if (applicationCalls > 1) return new Promise(resolve => deferred.push(() => resolve({{ items: [{{ application, job_snapshot: {{ company: "Acme", title: "Engineer" }} }}] }})));
    return Promise.resolve({{ items: [{{ application, job_snapshot: {{ company: "Acme", title: "Engineer" }} }}] }});
  }}
  if (path.includes("funnel")) return Promise.resolve({{ definition_version: "v1", stages: [] }});
  return Promise.resolve({{ items: [] }});
}};
const main = new Element();
const board = createApplicationsBoard({{ request, elements: {{ main }} }});
await board.render("workspace_1", "", "", {{ isCurrent: () => current }});
const filterButton = () => main.children[0].children[0].children[2];
filterButton().emit("click");
deferred.shift()();
for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
assert.equal(main.children[0]?.className, "applications-board", "filter refresh renders while Applications remains current");
filterButton().emit("click");
const newerRoute = new Element(); newerRoute.className = "version-map-toolbar";
current = false;
main.replaceChildren(newerRoute);
deferred.shift()();
for (let turn = 0; turn < 4; turn += 1) await Promise.resolve();
assert.equal(main.children[0], newerRoute, "late filter refresh cannot replace a newer route");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_real_shell_filter_refresh_cannot_overwrite_a_newer_route() -> None:
    module_url = (WEB / "app/features/workbench-shell.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createWorkbenchShell }} from {json.dumps(module_url)};

class Element {{
  constructor(tagName = "div") {{ this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = new Map(); this.dataset = {{}}; this.className = ""; this.textContent = ""; this.value = ""; this.hidden = false; this.disabled = false; this.style = {{ setProperty() {{}} }}; this.classList = {{ add() {{}}, remove() {{}}, toggle() {{}} }}; }}
  append(...items) {{ this.children.push(...items); }} replaceChildren(...items) {{ this.children = [...items]; }} add(item) {{ this.children.push(item); }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }} emit(type, event = {{}}) {{ this.listeners.get(type)?.({{ currentTarget: this, target: this, ...event }}); }}
  setAttribute() {{}} querySelector() {{ return null; }} querySelectorAll() {{ return []; }} closest() {{ return new Element(); }}
}}
globalThis.document = {{ createElement: tagName => new Element(tagName), querySelector: () => null }};
globalThis.Option = class Option extends Element {{ constructor(text, value) {{ super("option"); this.textContent = text; this.value = value; }} }};
globalThis.localStorage = {{ getItem: () => "workspace_1", setItem() {{}} }};
globalThis.window = {{ dispatchEvent() {{}}, clearTimeout() {{}}, setTimeout() {{}} }};
globalThis.CustomEvent = class CustomEvent {{ constructor(type, init) {{ this.type = type; this.detail = init.detail; }} }};
let applicationCalls = 0; let resolveFilter;
const response = payload => ({{ ok: true, json: async () => payload }});
globalThis.fetch = async url => {{
  const path = String(url);
  if (path.includes("/applications?")) {{ applicationCalls += 1; return applicationCalls === 1 ? response({{ items: [] }}) : new Promise(resolve => {{ resolveFilter = () => resolve(response({{ items: [] }})); }}); }}
  if (path.includes("/version-map")) return response({{ nodes: [], edges: [] }});
  if (path.includes("/view-preference")) return response({{ node_positions: {{}}, collapsed_branch_ids: [], viewport_zoom: 1 }});
  if (path.includes("workspaces?")) return response({{ items: [{{ workspace_id: "workspace_1", name: "Target" }}] }});
  if (path.includes("/home")) return response({{ stats: {{ resume_count: 1, job_count: 1, active_operation_count: 0 }}, recent_versions: [{{ resume_id: "resume_1", version_id: "version_1", label: "Base" }}], workspace: {{ workspace_id: "workspace_1", name: "Target", revision: 1 }} }});
  if (path.includes("/analytics/funnel")) return response({{ definition_version: "v1", stages: [] }});
  if (path.includes("/reminders?")) return response({{ items: [] }});
  if (path.includes("/content?")) return response({{ markdown: "", profile: null }});
  return response({{ items: [] }});
}};
const element = () => new Element(); const main = element();
const shell = createWorkbenchShell({{ getApiBase: () => "", elements: {{
  status: element(), workspace: element(), jobCount: element(), jobList: element(), agentContext: element(), operationCards: element(), mode: element(), title: element(), main, match: element(), archiveTab: element(), matchTab: element(), view: element(), candidateRail: element(), stageResume: element(), stageJob: element(), stageAnalysis: element(), stageEyebrow: element(), stageTitle: element(), stageDescription: element(), stagePrimary: element(), stageSecondary: element(), agentActions: element(), taskCenter: element(), tailorResumeButton: element(), contextTitle: element(), contextMeta: element(), contextDescription: element(), contextContent: element(), actionBar: element(), actionStatus: element(), resumeList: element(),
}} }});
await shell.activate("applications");
main.children[0].children[0].children[2].emit("click");
for (let turn = 0; turn < 8 && !resolveFilter; turn += 1) await Promise.resolve();
assert.equal(typeof resolveFilter, "function", "real filter refresh is in flight");
await shell.activate("version-map");
const newerRoute = main.children[0];
resolveFilter();
for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
assert.equal(main.children[0], newerRoute, "late real-shell filter refresh cannot replace Version Map");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_application_mutation_refresh_retains_the_route_guard() -> None:
    module_url = (WEB / "app/features/applications-board.js").resolve().as_uri()
    harness = f'''
import assert from "node:assert/strict";
import {{ createApplicationsBoard }} from {json.dumps(module_url)};
class Element {{
  constructor(tagName = "div") {{ this.tagName = tagName.toUpperCase(); this.children = []; this.listeners = new Map(); this.dataset = {{}}; this.className = ""; this.textContent = ""; this.value = ""; this.classList = {{ add() {{}}, remove() {{}} }}; }}
  append(...items) {{ this.children.push(...items); }} replaceChildren(...items) {{ this.children = [...items]; }} add(item) {{ this.children.push(item); }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }} emit(type, event = {{}}) {{ this.listeners.get(type)?.({{ currentTarget: this, target: this, ...event }}); }}
  setAttribute() {{}} querySelector() {{ return null; }}
}}
globalThis.document = {{ createElement: tagName => new Element(tagName) }};
globalThis.Option = class Option extends Element {{ constructor(text, value) {{ super("option"); this.textContent = text; this.value = value; }} }};
const application = {{ application_id: "app_1", current_status: "applied", priority: 1, resume_version_id: "r1", next_action: null, remind_at: null, events: [], revision: 1 }};
let current = true; let applicationCalls = 0; let resolveRefresh;
const request = path => {{
  if (path.includes("/events")) return Promise.resolve({{}});
  if (path.includes("applications?")) {{ applicationCalls += 1; return applicationCalls === 1 ? Promise.resolve({{ items: [{{ application, job_snapshot: {{ company: "Acme", title: "Engineer" }} }}] }}) : new Promise(resolve => {{ resolveRefresh = () => resolve({{ items: [] }}); }}); }}
  if (path.includes("funnel")) return Promise.resolve({{ definition_version: "v1", stages: [] }});
  return Promise.resolve({{ items: [] }});
}};
const main = new Element(); const board = createApplicationsBoard({{ request, elements: {{ main }} }});
await board.render("workspace_1", "", "", {{ isCurrent: () => current }});
const card = main.children[0].children[2].children.find(column => column.children.some(child => child.className === "application-card")).children.find(child => child.className === "application-card");
card.children[6].emit("click");
card.children.at(-1).children[3].emit("click");
for (let turn = 0; turn < 8 && !resolveRefresh; turn += 1) await Promise.resolve();
assert.equal(typeof resolveRefresh, "function", "post-mutation refresh is in flight");
const newerRoute = new Element(); newerRoute.className = "newer-route"; current = false; main.replaceChildren(newerRoute);
resolveRefresh();
for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
assert.equal(main.children[0], newerRoute, "late post-mutation refresh cannot replace a newer route");
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
