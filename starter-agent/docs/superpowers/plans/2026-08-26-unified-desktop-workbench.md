# Unified Desktop Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one stable desktop application shell with an equal-height three-column workspace, a persistent left-rail Agent conversation, contextual center/right content, and in-app windows for advanced modules.

**Architecture:** Make the workbench shell the only primary page container and give it ownership of navigation, Agent state, and geometry. Keep primary routes separate from overlay state through a small pure state module, then render Settings and advanced modules through a modal manager without changing the primary hash. Existing knowledge, capability, Trust Center, resume, matching, version, and application logic remains intact behind new containers.

**Tech Stack:** HTML5, CSS custom properties and grid, browser-native ES modules, existing vanilla JavaScript frontend, Python 3.11/3.12, pytest contract tests, existing FastAPI static frontend.

**Spec:** `docs/superpowers/specs/2026-08-26-unified-desktop-workbench-design.md`

## Global Constraints

- Full three-column acceptance begins at a viewport width of 1280 pixels.
- Desktop columns share one computed workspace height; left-card rows plus their gap equal the center and right panel height.
- The Agent conversation has one shell-owned DOM mount point and no full-screen route.
- Primary routes are only `workbench`, `version-map`, and `applications`.
- Knowledge Base, Models/Tools/MCP, and Trust Center open in one K1 window occupying approximately 88 percent of the desktop viewport.
- Settings remains a smaller dialog opened from the `Starter Agent` card header.
- Opening or closing overlays must not mutate primary route, primary selection, Agent state, or saved scroll positions.
- Existing backend APIs, confirmation policies, and fact/evidence boundaries remain unchanged.
- Mobile and tablet receive a safe flow fallback only; a full responsive redesign is out of scope.
- Preserve all unrelated user changes in the dirty worktree; stage only files named by the current task.

---

## File Structure

- Create `frontend/web/app/shell-state.js`: pure primary-route, overlay, request-epoch, and scroll-position state.
- Create `frontend/web/app/modal-manager.js`: DOM focus, backdrop, close, replacement, and focus-return behavior for one modal surface.
- Create `frontend/web/styles/shell.css`: shared S1 tokens, desktop shell geometry, persistent rails, and modal geometry.
- Modify `frontend/web/styles/app.css`: import the shared shell layer before feature-specific workbench rules.
- Modify `frontend/web/index.html`: remove the legacy sidebar/full-screen chat page, keep one Agent chat mount, add stable context regions, and wrap advanced modules in K1.
- Modify `frontend/web/app.js`: use shell state and modal manager, stop moving `chatDock`, keep hashes primary-only, and activate advanced modules inside K1.
- Modify `frontend/web/app/features/workbench-shell.js`: render page-specific center and context content without changing the shell.
- Modify `frontend/web/app/features/resume-workspace.js`: report selected version context to the shell.
- Modify `frontend/web/app/features/applications-board.js`: report selected application context to the shell.
- Modify `frontend/web/styles/workbench.css`: remove conflicting historical height rules and implement A1 inner scrolling/action layout.
- Modify `frontend/web/styles/legacy.css`: scope knowledge/capability/Trust styles to K1 and remove obsolete legacy-shell geometry.
- Create `tests/unit/test_unified_shell_ui.py`: shell, state module, overlay, route, geometry, and visual-token contracts.
- Modify `tests/unit/test_workbench_shell_ui.py`: assert the new route and Agent ownership contract.
- Modify `tests/unit/test_workbench_quality.py`: assert advanced-window and accessibility contracts.
- Modify `tests/unit/test_knowledge_ui_contract.py`: assert K1 placement instead of legacy primary navigation.
- Modify `tests/unit/test_capability_ui_contract.py`: assert modal-local capability tabs and stale-request protection.
- Modify `tests/unit/test_trust_ui_contract.py`: assert modal-local Trust tabs and stale-request protection.
- Create `docs/superpowers/qa/2026-08-26-unified-desktop-workbench.md`: record desktop viewport, state-preservation, keyboard, error-state, and screenshot results.

---

### Task 1: Pure Shell State and Updated Route Contract

**Files:**
- Create: `frontend/web/app/shell-state.js`
- Create: `tests/unit/test_unified_shell_ui.py`
- Modify: `tests/unit/test_workbench_shell_ui.py:50`

**Interfaces:**
- Produces: `PRIMARY_ROUTES`, `ADVANCED_VIEWS`, `resolveShellRoute(hash, requested) -> string`, and `createShellState(initialRoute) -> ShellState`.
- `ShellState` produces `snapshot()`, `navigate(route)`, `openOverlay(type)`, `closeOverlay()`, `captureOverlayRequest()`, `isOverlayRequestCurrent(token)`, `rememberScroll(key, top)`, and `scrollFor(key)`.
- Later tasks consume these exact names from `app.js`.

- [ ] **Step 1: Write failing shell-state and route contract tests**

```python
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
```

Update `test_workbench_routes_are_first_class_and_existing_routes_remain` so it requires the three primary routes and explicitly rejects `#/chat` as a navigation target.

- [ ] **Step 2: Run the focused tests and verify the missing module/new contract fails**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py -q`

Expected: FAIL because `shell-state.js` does not exist and the old test still accepts chat/advanced hashes as primary pages.

- [ ] **Step 3: Implement the pure state module**

```javascript
export const PRIMARY_ROUTES = Object.freeze(["workbench", "version-map", "applications"]);
export const ADVANCED_VIEWS = Object.freeze(["knowledge", "capabilities", "trust"]);

export function resolveShellRoute(hash, requested = "") {
  const byHash = {
    "#/workbench": "workbench",
    "#/version-map": "version-map",
    "#/applications": "applications",
  };
  if (byHash[hash]) return byHash[hash];
  if (PRIMARY_ROUTES.includes(requested)) return requested;
  return "workbench";
}

export function createShellState(initialRoute = "workbench") {
  let primaryRoute = PRIMARY_ROUTES.includes(initialRoute) ? initialRoute : "workbench";
  let overlay = null;
  let overlayEpoch = 0;
  const scrollPositions = new Map();

  return Object.freeze({
    snapshot: () => Object.freeze({ primaryRoute, overlay, overlayEpoch }),
    navigate(route) {
      if (!PRIMARY_ROUTES.includes(route)) throw new Error(`Unknown primary route: ${route}`);
      primaryRoute = route;
      return this.snapshot();
    },
    openOverlay(type) {
      if (!ADVANCED_VIEWS.includes(type)) throw new Error(`Unknown overlay: ${type}`);
      overlay = type;
      overlayEpoch += 1;
      return this.snapshot();
    },
    closeOverlay() {
      overlay = null;
      overlayEpoch += 1;
      return this.snapshot();
    },
    captureOverlayRequest: () => Object.freeze({ overlay, overlayEpoch }),
    isOverlayRequestCurrent: token => token.overlay === overlay && token.overlayEpoch === overlayEpoch,
    rememberScroll(key, top) { scrollPositions.set(key, Math.max(0, Number(top) || 0)); },
    scrollFor: key => scrollPositions.get(key) || 0,
  });
}
```

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add frontend/web/app/shell-state.js tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py
git commit -m "refactor: define unified shell state"
```

---

### Task 2: Persistent Application Shell Markup

**Files:**
- Modify: `frontend/web/index.html:12-402`
- Modify: `tests/unit/test_unified_shell_ui.py`
- Modify: `tests/unit/test_knowledge_ui_contract.py:8-27`

**Interfaces:**
- Consumes: primary route and overlay names from Task 1.
- Produces DOM ids `appShell`, `primaryNavigation`, `workbenchChatDock`, `workspaceCanvas`, `contextRail`, `workbenchContextTitle`, `workbenchContextMeta`, `workbenchContextDescription`, `workbenchContextContent`, `advancedOverlay`, `advancedDialog`, `advancedTitle`, and `advancedCloseButton`.
- Preserves existing business-control ids inside knowledge, capability, Trust, resume, matching, and Agent content.

- [ ] **Step 1: Extend the markup contract test**

```python
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
```

Update the knowledge contract to require `knowledgeNavButton` inside Settings and `knowledgeView` inside `advancedDialog`, while removing the `chatNavButton` expectation.

- [ ] **Step 2: Run the markup tests and verify they fail**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_knowledge_ui_contract.py -q`

Expected: FAIL because the legacy sidebar/full-screen chat still exists and advanced views are primary siblings.

- [ ] **Step 3: Restructure `index.html` around the stable shell**

Use this outer structure while retaining all existing business controls inside their new locations:

```html
<main id="appShell" class="app-shell">
  <section id="workbenchView" class="workbench-view" aria-labelledby="workbenchTitle">
    <header class="workbench-topbar">
      <div class="workbench-brand"><strong>Resume Agent</strong></div>
      <nav id="primaryNavigation" class="workbench-primary-tabs" aria-label="主导航">
        <button id="workbenchPageTab" type="button" aria-current="page">工作台</button>
        <button id="versionMapPageTab" type="button">版本地图</button>
        <button id="applicationsPageTab" type="button">投递看板</button>
      </nav>
    </header>
    <div class="workbench-layout">
      <aside class="workbench-left" aria-label="简历档案与 Agent">
        <section class="workbench-card workbench-profile-card" aria-label="简历档案"></section>
        <section class="workbench-card workbench-agent-card">
          <div class="workbench-section-heading workbench-agent-heading"></div>
          <div id="workbenchChatDock" class="workbench-chat-dock">
            <div id="chatDock"></div>
          </div>
          <div id="workbenchAgentSuggestions" class="workbench-agent-suggestions"></div>
          <details class="workbench-agent-context-panel"></details>
          <details class="workbench-task-center" hidden></details>
        </section>
      </aside>
      <main id="workspaceCanvas" class="workbench-main"></main>
      <aside id="contextRail" class="workbench-right">
        <section class="workbench-card workbench-context-card">
          <div class="workbench-section-heading">
            <h2 id="workbenchContextTitle">岗位候选</h2>
            <span id="workbenchContextMeta">—</span>
          </div>
          <p id="workbenchContextDescription" class="workbench-helper"></p>
          <div id="workbenchContextContent"><div id="workbenchJobList"></div></div>
        </section>
      </aside>
    </div>
  </section>
</main>

<div id="advancedOverlay" class="advanced-overlay" hidden>
  <section id="advancedDialog" class="advanced-dialog" role="dialog" aria-modal="true" aria-labelledby="advancedTitle">
    <header class="advanced-dialog-header">
      <h2 id="advancedTitle"></h2>
      <button id="advancedCloseButton" type="button" aria-label="关闭窗口">×</button>
    </header>
    <div class="advanced-dialog-body"></div>
  </section>
</div>
```

Delete the old `sidebar` and `chatView`. Keep the existing Settings overlay after K1.

- [ ] **Step 4: Run the markup contracts**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_knowledge_ui_contract.py tests/unit/test_workbench_shell_ui.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add frontend/web/index.html tests/unit/test_unified_shell_ui.py tests/unit/test_knowledge_ui_contract.py
git commit -m "refactor: make workbench the persistent app shell"
```

---

### Task 3: Primary Navigation and Modal Manager

**Files:**
- Create: `frontend/web/app/modal-manager.js`
- Modify: `frontend/web/app.js:1-30,200-420,4029-4120,4400-4560`
- Modify: `tests/unit/test_unified_shell_ui.py`
- Modify: `tests/unit/test_workbench_quality.py:13-30`

**Interfaces:**
- Consumes: `createShellState` and `resolveShellRoute` from Task 1 and Task 2 DOM ids.
- Produces: `createModalManager({ overlay, dialog, title, closeButton, panels, onBeforeClose })` with `open(type, trigger)`, `close()`, `replace(type)`, and `activeType()`.
- `app.js` produces `openAdvancedWindow(type, trigger)` and keeps `applyPrimaryHashRoute()` primary-only.

- [ ] **Step 1: Add failing navigation/modal behavior contracts**

```python
MODALS = (WEB / "app/modal-manager.js").read_text(encoding="utf-8")


def test_app_uses_primary_only_routing_and_never_moves_chat_dom() -> None:
    assert 'from "./app/shell-state.js"' in APP
    assert 'from "./app/modal-manager.js"' in APP
    assert ".append(chatDock)" not in APP
    assert 'navigatePrimaryHash("#/chat")' not in APP
    assert 'openAdvancedWindow("knowledge"' in APP
    assert 'openAdvancedWindow("capabilities"' in APP
    assert 'openAdvancedWindow("trust"' in APP


def test_modal_manager_owns_focus_close_and_replacement() -> None:
    for contract in (
        "createModalManager", "returnFocus", 'event.key === "Escape"',
        'event.key !== "Tab"', "overlay.hidden = false", "overlay.hidden = true",
        "returnFocus?.focus()", "activeType",
    ):
        assert contract in MODALS
```

Update the quality test so it expects advanced buttons to call `openAdvancedWindow` and no longer requires advanced primary hashes.

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_quality.py -q`

Expected: FAIL because `app.js` still moves the chat dock and advanced modules still change the hash.

- [ ] **Step 3: Implement the modal manager**

```javascript
export function createModalManager({ overlay, dialog, title, closeButton, panels, onBeforeClose = () => {} }) {
  let currentType = null;
  let returnFocus = null;
  const focusable = () => [...dialog.querySelectorAll(
    'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'
  )].filter(node => !node.hidden);

  function show(type, trigger) {
    currentType = type;
    returnFocus = trigger || document.activeElement;
    for (const [name, panel] of Object.entries(panels)) panel.hidden = name !== type;
    title.textContent = { knowledge: "个人知识库", capabilities: "模型、Tool 与 MCP", trust: "信任中心" }[type];
    overlay.hidden = false;
    document.body.classList.add("modal-open");
    closeButton.focus();
  }

  function close() {
    if (!currentType) return;
    onBeforeClose(currentType);
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
    currentType = null;
    returnFocus?.focus();
  }

  function onKeydown(event) {
    if (!currentType) return;
    if (event.key === "Escape") return close();
    if (event.key !== "Tab") return;
    const nodes = focusable();
    if (!nodes.length) return event.preventDefault();
    const first = nodes[0];
    const last = nodes.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  closeButton.addEventListener("click", close);
  overlay.addEventListener("click", event => { if (event.target === overlay) close(); });
  document.addEventListener("keydown", onKeydown);
  return Object.freeze({ open: show, replace: type => show(type, returnFocus), close, activeType: () => currentType });
}
```

- [ ] **Step 4: Rewire `app.js`**

Import the new modules, initialize `shellState` from the current hash/query parameter, and replace advanced navigation with overlay activation:

```javascript
const requestedRoute = new URLSearchParams(window.location.search).get("route") || "";
const shellState = createShellState(resolveShellRoute(window.location.hash, requestedRoute));
const advancedWindow = createModalManager({
  overlay: advancedOverlay,
  dialog: advancedDialog,
  title: advancedTitle,
  closeButton: advancedCloseButton,
  panels: { knowledge: knowledgeView, capabilities: capabilitiesView, trust: trustView },
  onBeforeClose(type) {
    shellState.closeOverlay();
    if (type === "capabilities") advanceCapabilityRequestEpoch();
    if (type === "trust") advanceTrustRequestEpoch();
  },
});

async function openAdvancedWindow(type, trigger) {
  shellState.openOverlay(type);
  advancedWindow.open(type, trigger);
  if (type === "knowledge") await loadKnowledgeBase();
  if (type === "capabilities") await refreshCapabilityRoute();
  if (type === "trust") await refreshTrustRoute();
}
```

Make `applyPrimaryHashRoute()` resolve only the three primary routes, activate the selected workbench route, and leave overlay state untouched. Remove `showPrimaryView()` and all `chatDock` reparenting. Per-route center/right scroll restoration is added with the stable scroll regions in Task 5.

Advanced settings buttons use `closeSettings(); void openAdvancedWindow("knowledge", knowledgeNavButton)` and equivalent calls for capability and Trust. Capability and Trust tabs update local module state and refresh the active panel without changing `window.location.hash`.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_quality.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```powershell
git add frontend/web/app/modal-manager.js frontend/web/app.js tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_quality.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py
git commit -m "feat: keep advanced modules inside app windows"
```

---

### Task 4: Equal-Height Desktop Geometry and S1 Theme

**Files:**
- Create: `frontend/web/styles/shell.css`
- Modify: `frontend/web/styles/app.css:1-4`
- Modify: `frontend/web/styles/tokens.css`
- Modify: `frontend/web/styles/workbench.css:1-33,63-67,71-111,163-193`
- Modify: `frontend/web/styles/legacy.css:1-140,630-700`
- Modify: `tests/unit/test_unified_shell_ui.py`
- Modify: `tests/unit/test_workbench_shell_ui.py:35-48`

**Interfaces:**
- Produces shared tokens `--app-bg`, `--app-surface`, `--app-ink`, `--app-muted`, `--app-line`, `--app-accent`, `--app-accent-soft`, `--app-header-block-size`, `--app-shell-gap`, and `--app-workspace-block-size`.
- Produces scroll regions `.agent-scroll-region`, `.workspace-scroll-region`, and `.context-scroll-region`.
- Existing feature CSS consumes shared tokens through `--wb-*` aliases.

- [ ] **Step 1: Add failing geometry and visual-token contracts**

```python
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
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py -q`

Expected: FAIL because several conflicting `min-height`, sticky, and page-scroll rules still control the columns.

- [ ] **Step 3: Add `shell.css` and centralize tokens**

```css
:root {
  --app-bg: #f4f1e9;
  --app-surface: #fffdf8;
  --app-ink: #263c31;
  --app-muted: #68716b;
  --app-line: #dfd9cc;
  --app-accent: #176b4d;
  --app-accent-soft: #dcefe3;
  --app-header-block-size: 54px;
  --app-shell-gap: 16px;
  --app-workspace-block-size: calc(100dvh - var(--app-header-block-size) - 32px);
}

body { overflow: hidden; background: var(--app-bg); color: var(--app-ink); }
.app-shell, .workbench-view { min-height: 100dvh; }

@media (min-width: 1280px) {
  .workbench-view { display: grid; grid-template-rows: var(--app-header-block-size) minmax(0, 1fr); }
  .workbench-layout {
    height: var(--app-workspace-block-size);
    align-items: stretch;
    grid-template-columns: clamp(270px, 18vw, 330px) minmax(620px, 1fr) clamp(260px, 18vw, 340px);
    gap: var(--app-shell-gap);
  }
  .workbench-left { display: grid; grid-template-rows: auto minmax(0, 1fr); gap: var(--app-shell-gap); height: 100%; min-height: 0; }
  .workbench-main, .workbench-right, .workbench-canvas, .workbench-context-card { height: 100%; min-height: 0; }
  .workbench-agent-card, .workbench-canvas, .workbench-context-card { overflow: hidden; }
  .agent-scroll-region, .workspace-scroll-region, .context-scroll-region { min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
}

@media (max-width: 1279px) {
  body { overflow: auto; }
  .workbench-layout { height: auto; grid-template-columns: 1fr; }
}
```

Import `shell.css` from `app.css` between `base.css` and feature styles. Alias existing `--wb-*` variables to the new tokens. Delete later `workbench.css` declarations that restore page scrolling, independently cap each column, or give the Agent and center different viewport formulas.

- [ ] **Step 4: Run focused CSS contracts**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py tests/unit/test_workbench_quality.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```powershell
git add frontend/web/styles/tokens.css frontend/web/styles/shell.css frontend/web/styles/app.css frontend/web/styles/workbench.css frontend/web/styles/legacy.css tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py tests/unit/test_workbench_quality.py
git commit -m "style: stabilize equal-height desktop workspace"
```

---

### Task 5: A1 Center Canvas and Contextual Right Rail

**Files:**
- Modify: `frontend/web/index.html:303-400`
- Modify: `frontend/web/app.js`
- Modify: `frontend/web/app/features/workbench-shell.js`
- Modify: `frontend/web/app/features/resume-workspace.js:216-472`
- Modify: `frontend/web/app/features/applications-board.js:16-198`
- Modify: `frontend/web/styles/workbench.css`
- Modify: `tests/unit/test_unified_shell_ui.py`
- Modify: `tests/unit/test_workbench_shell_ui.py`

**Interfaces:**
- `createResumeWorkspace` gains `onVersionSelect(node)` and calls it from the existing version-map selection callback.
- `createApplicationsBoard` gains `onApplicationSelect(application)` and calls it whenever a board card becomes active.
- `createWorkbenchShell` consumes `contextTitle`, `contextMeta`, `contextDescription`, `contextContent`, and `actionBar` elements.
- `app.js` produces `rememberRouteScroll(route)` and `restoreRouteScroll(route)` using Task 1's shell-state scroll API.

- [ ] **Step 1: Add failing A1/context contracts**

```python
def test_a1_canvas_and_context_rail_have_stable_regions() -> None:
    for contract in (
        'id="workbenchStageCallout"', 'class="workbench-content-tabs"',
        'id="workbenchActionBar"', 'class="workspace-scroll-region"',
        'id="workbenchContextTitle"', 'id="workbenchContextDescription"',
        'id="workbenchContextContent"', 'class="context-scroll-region"',
    ):
        assert contract in HTML
    for contract in ("onVersionSelect", "onApplicationSelect", "rememberRouteScroll", "restoreRouteScroll"):
        assert contract in JS
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py -q`

Expected: FAIL because the action bar, contextual callbacks, and scroll-state API do not exist.

- [ ] **Step 3: Add the A1 scroll body and action bar markup**

Keep the stage callout and content tabs fixed above this body:

```html
<div class="workspace-scroll-region" data-scroll-key="center">
  <div class="workbench-section-heading"><!-- existing heading --></div>
  <div id="workbenchMainContent"></div>
  <div id="workbenchMatchContent" hidden></div>
</div>
<footer id="workbenchActionBar" class="workbench-action-bar" aria-live="polite">
  <span id="workbenchActionStatus">选择当前任务后显示可用操作</span>
  <div id="workbenchActionControls"></div>
</footer>
```

Give `workbenchContextContent` the `context-scroll-region` class. AI-tailoring continues to use the existing `.tailored-suggestion-panel` split-review rendering inside the center body.

- [ ] **Step 4: Implement page-specific context rendering and scroll capture**

Add helpers in `workbench-shell.js`:

```javascript
function renderContext({ title, meta = "", description = "", content }) {
  elements.contextTitle.textContent = title;
  elements.contextMeta.textContent = meta;
  elements.contextDescription.textContent = description;
  elements.contextContent.replaceChildren();
  if (content) elements.contextContent.append(content);
}

function renderVersionContext(node) {
  const panel = document.createElement("section");
  panel.className = "context-detail-card";
  const heading = document.createElement("strong");
  heading.textContent = node.label;
  const detail = document.createElement("p");
  detail.textContent = `状态：${node.status} · revision ${node.revision}`;
  panel.append(heading, detail);
  renderContext({ title: "版本详情", meta: `r${node.revision}`, description: "当前选中的简历版本", content: panel });
}

function renderApplicationContext(application) {
  const panel = document.createElement("section");
  panel.className = "context-detail-card";
  const heading = document.createElement("strong");
  heading.textContent = application.title || application.application_id;
  const detail = document.createElement("p");
  detail.textContent = `状态：${application.status} · 下一步：${application.next_action || "未设置"}`;
  panel.append(heading, detail);
  renderContext({ title: "投递详情", description: "当前选中的投递记录", content: panel });
}

function rememberRouteScroll(route) {
  shellState.rememberScroll(`${route}:center`, workbenchCenterScroll.scrollTop);
  shellState.rememberScroll(`${route}:context`, workbenchContextContent.scrollTop);
}

function restoreRouteScroll(route) {
  workbenchCenterScroll.scrollTop = shellState.scrollFor(`${route}:center`);
  workbenchContextContent.scrollTop = shellState.scrollFor(`${route}:context`);
}
```

For `workbench`, render the existing job list in the right rail. For `version-map`, pass `onVersionSelect: renderVersionContext`. For `applications`, pass `onApplicationSelect: renderApplicationContext`. In `applyPrimaryHashRoute()`, call `rememberRouteScroll(previousRoute)` before activation and `requestAnimationFrame(() => restoreRouteScroll(nextRoute))` after rendering.

- [ ] **Step 5: Run focused workbench tests**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py tests/unit/test_workbench_agent_ui.py tests/unit/test_workbench_quality.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

```powershell
git add frontend/web/index.html frontend/web/app.js frontend/web/app/features/workbench-shell.js frontend/web/app/features/resume-workspace.js frontend/web/app/features/applications-board.js frontend/web/styles/workbench.css tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py
git commit -m "feat: add stage canvas and contextual rails"
```

---

### Task 6: K1 Advanced Module Styling and Request Isolation

**Files:**
- Modify: `frontend/web/app.js`
- Modify: `frontend/web/styles/shell.css`
- Modify: `frontend/web/styles/legacy.css`
- Modify: `tests/unit/test_knowledge_ui_contract.py`
- Modify: `tests/unit/test_capability_ui_contract.py`
- Modify: `tests/unit/test_trust_ui_contract.py`
- Modify: `tests/unit/test_workbench_quality.py`

**Interfaces:**
- Consumes: `advancedWindow` and `shellState.captureOverlayRequest()` from Task 3.
- Produces `.advanced-overlay`, `.advanced-dialog`, `.advanced-dialog-header`, and `.advanced-dialog-body` K1 layout.
- Capability, Trust, and knowledge loaders reject late results through their existing epoch logic plus the shell overlay token.

- [ ] **Step 1: Add failing K1 and request-isolation contracts**

```python
def test_k1_window_has_desktop_geometry_and_internal_scrolling() -> None:
    for contract in (
        ".advanced-overlay", ".advanced-dialog", "width: min(88vw, 1600px)",
        "height: min(88dvh, 960px)", "grid-template-rows: auto minmax(0, 1fr)",
        ".advanced-dialog-body", "overflow: auto", "body.modal-open",
    ):
        assert contract in CSS


def test_advanced_loaders_check_overlay_ownership() -> None:
    assert "captureOverlayRequest" in APP
    assert "isOverlayRequestCurrent" in APP
    assert "openAdvancedWindow" in APP
    assert 'window.location.hash = "#/knowledge"' not in APP
```

Update module tests to assert their sections occur after `advancedDialog` in HTML and that their tab buttons update local state without advanced hashes.

- [ ] **Step 2: Run advanced-module tests and verify they fail**

Run: `uv run pytest tests/unit/test_knowledge_ui_contract.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py tests/unit/test_workbench_quality.py tests/unit/test_unified_shell_ui.py -q`

Expected: FAIL until K1 geometry and overlay-token checks are present.

- [ ] **Step 3: Implement K1 layout**

```css
.advanced-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgb(27 37 31 / 38%);
  backdrop-filter: blur(2px);
}
.advanced-dialog {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  width: min(88vw, 1600px);
  height: min(88dvh, 960px);
  overflow: hidden;
  border: 1px solid var(--app-line);
  border-radius: 18px;
  background: var(--app-surface);
  box-shadow: 0 28px 80px rgb(38 60 49 / 24%);
}
.advanced-dialog-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--app-line); }
.advanced-dialog-body { min-height: 0; overflow: auto; }
body.modal-open { overflow: hidden; }
```

Scope existing knowledge and capability heights to `.advanced-dialog-body`, replacing viewport formulas that assume a primary page. Preserve their internal master-detail scroll regions.

- [ ] **Step 4: Guard asynchronous module rendering**

At the beginning of each advanced refresh, capture both existing module request state and the overlay token:

```javascript
async function refreshCapabilityRoute() {
  const overlayToken = shellState.captureOverlayRequest();
  capabilityRefreshButton.disabled = true;
  try {
    if (capabilityState.route === "skills") {
      await loadCapabilitySkills(overlayToken);
    } else {
      await loadCapabilityServers(overlayToken);
    }
  } finally {
    if (shellState.isOverlayRequestCurrent(overlayToken)) {
      capabilityRefreshButton.disabled = false;
    }
  }
}
```

Change `loadCapabilitySkills(overlayToken)` and `loadCapabilityServers(overlayToken)` so every DOM write requires both `shellState.isOverlayRequestCurrent(overlayToken)` and the existing capability request-token check. Apply the same signature pattern to `refreshTrustRoute(overlayToken)` and `loadKnowledgeBase(overlayToken)`. Closing K1 increments the overlay epoch through `shellState.closeOverlay()`.

- [ ] **Step 5: Run advanced-module tests**

Run: `uv run pytest tests/unit/test_knowledge_ui_contract.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py tests/unit/test_workbench_quality.py tests/unit/test_unified_shell_ui.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```powershell
git add frontend/web/app.js frontend/web/styles/shell.css frontend/web/styles/legacy.css tests/unit/test_knowledge_ui_contract.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py tests/unit/test_workbench_quality.py tests/unit/test_unified_shell_ui.py
git commit -m "feat: migrate advanced tools into K1 window"
```

---

### Task 7: Regression, Desktop Visual QA, and Acceptance Record

**Files:**
- Modify: `frontend/web/index.html`
- Modify: `frontend/web/app.js`
- Modify: `frontend/web/styles/shell.css`
- Modify: `frontend/web/styles/workbench.css`
- Modify: `frontend/web/styles/legacy.css`
- Modify: `tests/unit/test_unified_shell_ui.py`
- Create: `docs/superpowers/qa/2026-08-26-unified-desktop-workbench.md`

**Interfaces:**
- Consumes all prior tasks.
- Produces an acceptance record with exact commands, viewport sizes, screenshots, observed geometry, keyboard results, and any known safe-flow limitations below 1280 pixels.

- [ ] **Step 1: Add final anti-regression contracts**

```python
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
```

- [ ] **Step 2: Run the focused frontend contract suite and fix only observed regressions**

Run:

```powershell
uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py tests/unit/test_workbench_agent_ui.py tests/unit/test_workbench_quality.py tests/unit/test_knowledge_ui_contract.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py tests/unit/test_token_ui_contract.py tests/unit/test_tool_confirmation_ui_contract.py -q
```

Expected: PASS. If a failure reflects a retained backend/safety contract, change the implementation; change the assertion only when it explicitly encoded the removed legacy shell.

- [ ] **Step 3: Run the complete test suite**

Run: `uv run pytest -q`

Expected: PASS with no new failures. Record pre-existing failures separately without masking them.

- [ ] **Step 4: Start the app and perform desktop browser acceptance**

Run:

```powershell
uv run agent serve
```

At `1280x800`, `1440x900`, and `1920x1080`, measure the bounding rectangles of `.workbench-left`, `.workbench-canvas`, and `.workbench-context-card`. Their top and bottom coordinates must match within one CSS pixel. Capture screenshots for each viewport under `artifacts/unified-shell-qa/`.

At each viewport, verify this sequence:

1. Enter text in the Agent composer and scroll its message area.
2. Switch Workbench → Version Map → Applications Board → Workbench.
3. Confirm the text, Agent scroll, selected resume/job, and center/right scroll restoration remain.
4. Open Settings from the Agent card.
5. Open Knowledge Base, close it with Escape, and confirm focus returns to its Settings trigger.
6. Repeat for Models/Tools/MCP and Trust Center.
7. Simulate a failed advanced request and confirm the error stays inside K1 without exposing the legacy shell.
8. Check keyboard focus visibility and focus trapping.

- [ ] **Step 5: Write the QA record with measured results**

Create the document only after the observations exist. It must include the title `Unified Desktop Workbench QA — 2026-08-26`, the exact focused and full-suite commands with their exit codes and passed-test counts, a geometry table with integer pixel values for all three columns at `1280x800`, `1440x900`, and `1920x1080`, the eight state/keyboard checks from Step 4 with PASS or FAIL plus evidence, the three screenshot paths, and the statement that widths below 1280 pixels received safe-flow rather than pixel-perfect acceptance.

- [ ] **Step 6: Run final verification after the QA document exists**

Run: `uv run pytest tests/unit/test_unified_shell_ui.py -q`

Expected: PASS, including the documented viewport assertion.

- [ ] **Step 7: Commit Task 7**

```powershell
git add frontend/web/index.html frontend/web/app.js frontend/web/styles/shell.css frontend/web/styles/workbench.css frontend/web/styles/legacy.css tests/unit/test_unified_shell_ui.py docs/superpowers/qa/2026-08-26-unified-desktop-workbench.md artifacts/unified-shell-qa
git commit -m "test: verify unified desktop workbench"
```
