# Unified Desktop Workbench Design

**Date:** 2026-08-26  
**Status:** Approved for implementation planning

## Summary

Unify the current frontend under one desktop application shell. Remove the standalone full-screen chat page, keep the Agent conversation in the left rail, and prevent the layout from changing height when users move among the workbench, version map, and applications board.

Knowledge, capability management, and the Trust Center will no longer navigate into the legacy chat shell. They will open as large in-app windows above the current workspace. Settings remains a smaller dialog launched from the Agent card.

## Goals

- Give all primary job-search views one stable visual and navigation system.
- Keep the left rail, center workspace, and right context rail the same height on desktop.
- Make the combined height of the two left cards equal to the center and right panels.
- Preserve the Agent conversation while switching primary views.
- Open advanced modules without changing the underlying primary route or losing workspace state.
- Apply one warm, professional component language throughout the frontend.
- Preserve existing backend contracts, safety confirmations, and business behavior.

## Non-goals

- A complete mobile or tablet redesign.
- Backend API or persistence changes unrelated to frontend state restoration.
- Changes to capability, Trust Center, knowledge, job-search, or resume business rules.
- Multiple simultaneous modal windows.
- A standalone full-screen Agent conversation route.

## Selected Direction

The approved design combines four selected concepts:

1. **A — Equal-height three-column shell:** retain the current three-column workbench structure and stabilize its dimensions.
2. **A1 — Stage-driven document workspace:** make the current stage, primary action, and working document the center of the interface.
3. **K1 — Large in-app advanced window:** open knowledge, capability management, and the Trust Center in a large modal surface.
4. **S1 — Warm professional visual language:** use warm neutral surfaces, paper-white content panels, dark green accents, restrained borders, and subtle shadows.

Alternatives were rejected as follows:

- Combining the profile summary and Agent into one left card would simplify alignment but erase a useful content hierarchy.
- A permanent global navigation rail would turn the workbench into a four-column layout and reduce document space.
- A dashboard-first center would make status easier to scan but add a step before reading or editing a resume.
- A permanent split-review center would improve intensive editing but make ordinary browsing unnecessarily dense.
- Compact dialogs and right drawers do not provide enough room for advanced module management.

## Application Shell

### Primary navigation

The top navigation contains only:

- Workbench
- Version Map
- Applications Board

There is no standalone full-screen chat entry. The settings trigger remains in the `Starter Agent` card header instead of moving into the top navigation.

Primary navigation replaces content inside the stable shell. It does not reconstruct the shell or move the Agent conversation between containers.

### Desktop layout

At desktop widths of 1280 pixels and above, the main workspace uses three columns:

- **Left rail:** resume profile card followed by the persistent Agent card.
- **Center workspace:** the selected primary page.
- **Right context rail:** contextual information for the selected primary page.

All columns use the same computed workspace height based on the remaining dynamic viewport height below the top navigation. The left rail is a two-row grid: profile card, gap, then Agent card. Those rows and their gap fill exactly the same height as the center and right panels.

Overflow belongs to explicitly designated inner scroll regions. Loading, empty, error, and long-content states must not change the outer panel dimensions. The browser page itself should not become the normal scrolling container at desktop widths.

The columns retain stable widths and positions while primary views change. At widths below the desktop target, the interface may fall back to natural document flow; this release only requires that the content remain usable, not a complete responsive redesign.

## Primary Pages

### Workbench

The center uses the A1 stage-driven structure:

1. A compact stage header states the current stage and exposes one primary next action.
2. Content tabs organize the archive, job match, and modification suggestions.
3. The main body prioritizes the resume document or matching report.
4. A bottom action bar remains visible for the current task's key actions.

When the user starts AI resume editing, only the center body changes to a split review mode. Suggestions and evidence appear on the left; resume content appears on the right. Leaving review mode restores the standard document-first body.

The right rail displays job candidates and the current job summary.

### Version Map

The left rail and Agent conversation remain unchanged. The center displays the version relationship map. The right rail displays details and actions for the selected version. Selecting a version does not change the shell dimensions.

### Applications Board

The left rail and Agent conversation remain unchanged. The center displays the applications board. The right rail displays details for the selected application. Selecting cards or columns does not change the shell dimensions.

## Agent Conversation

The Agent conversation is a single persistent component owned by the application shell. It remains in the left Agent card across all three primary pages. It must not be duplicated or physically moved between page containers.

The component preserves its message history, composer state, active task cards, confirmation cards, and scroll position while the user changes primary pages. Existing conversation and tool-governance behavior remains intact.

## Settings and Advanced Windows

### Settings dialog

The settings button remains in the Agent card header. It opens a small modal dialog containing ordinary settings and entry points for:

- Knowledge Base
- Models, Tools, and MCP
- Trust Center

### K1 advanced window

Each advanced module opens in a large centered in-app window occupying approximately 88 percent of the desktop viewport. The current primary workspace remains visible beneath a modal backdrop.

The window has its own:

- title bar and close button;
- focus boundary;
- loading, content, empty, and error regions;
- internal scrolling area;
- module-specific actions.

Only one modal surface may be active. Opening an advanced module from Settings replaces the Settings dialog rather than stacking another modal. Opening a second advanced module replaces the first. Closing the advanced window returns focus to the original trigger and reveals the unchanged primary workspace.

Advanced windows do not update the primary route. A browser refresh restores the primary page but leaves modal windows closed.

Escape closes the active modal when no nested confirmation requires a decision. Background interaction and background scrolling are disabled while a modal is open.

## State and Data Flow

The frontend distinguishes two state layers:

- **Primary page state:** active primary route, selected resume, selected job, workbench stage, selected version or application, and per-region scroll positions.
- **Overlay state:** active modal type, modal-local tab, focus return target, and modal request status.

Primary page state survives modal open and close. Each primary page remembers its center and right rail scroll positions during in-session navigation. Agent state is shell-owned and therefore independent of the primary page.

Async requests must verify that their target page or modal is still active before applying results. Closing or replacing a modal invalidates its pending render work so a late response cannot redraw another surface.

## Errors, Loading, and Empty States

- Loading placeholders render inside reserved content regions and never resize the three-column shell.
- A request failure preserves previously loaded content when safe and presents a local retry action.
- Advanced-module failures appear inside the active K1 window and never navigate to another page.
- Empty states use the same panel dimensions as populated states.
- Destructive knowledge operations and capability changes retain their existing confirmation requirements.
- No backend error should silently restore the legacy chat page.

## Visual System

The S1 warm professional theme extends the current paper-like workbench aesthetic:

- warm off-white application background;
- paper-white cards and document surfaces;
- dark green primary text and action accents;
- low-saturation borders and status fills;
- restrained radii and subtle shadows;
- consistent spacing, typography, buttons, tabs, pills, empty states, and form controls.

Color and spacing values should be centralized as design tokens. Existing page-specific overrides should be reduced as components move into the shared shell. Resume document typography may retain its own print-oriented rules within the unified system.

## Component Boundaries

- **AppShell:** owns the header, primary navigation, equal-height grid, theme, and primary page outlet.
- **AgentRail:** owns the profile summary, persistent Agent conversation, and settings trigger.
- **WorkspaceCanvas:** hosts workbench, version map, and applications board center content through explicit page interfaces.
- **ContextRail:** hosts page-specific right-rail content through the same stable container.
- **ModalManager:** owns focus, backdrop, scroll locking, modal replacement, close behavior, and focus restoration.
- **Advanced module views:** retain module-specific data loading and actions while rendering inside the K1 container.

Each component should expose its purpose and required state through an explicit interface. Page modules must not directly control global shell dimensions or move shared DOM nodes.

## Accessibility

- Primary navigation exposes the active page with appropriate current-state semantics.
- Dialogs use labelled modal semantics and trap focus while open.
- Closing a dialog restores focus to the control that opened it.
- All actions remain keyboard accessible with visible focus styles.
- Status, loading, and error messages use suitable live-region behavior without causing repeated announcements.
- Color is not the only indication of selection, progress, risk, or error.

## Testing and Acceptance

### Automated contracts

- Unit tests cover primary route resolution, overlay state, modal replacement, focus return, and stale-request protection.
- UI contract tests verify that the standalone chat route and legacy-shell navigation are removed.
- UI contract tests verify that the Agent conversation has one shell-owned mount point.
- Existing knowledge, capability, Trust Center, workbench, version, and application behavior tests remain valid or are updated only for the new container structure.

### Browser acceptance

At 1280px, 1440px, and a wider desktop viewport:

- the top and bottom edges of all three columns align;
- the two left cards plus their gap equal the center and right panel height;
- switching all primary pages repeatedly causes no shell-height or column-width jump;
- Agent messages, composer state, task cards, and scroll position survive page changes;
- Settings opens from the Agent card;
- each advanced module opens in K1 without exposing the legacy chat shell;
- closing a modal restores the prior route, selections, scroll positions, and keyboard focus;
- long content, loading, empty, and failure states remain inside their intended scroll containers;
- visual snapshots reflect the approved warm professional theme.

## Implementation Scope Guard

This design is one frontend-shell project. Implementation should first establish the shared shell and state ownership, then migrate existing primary pages and advanced modules without changing their backend contracts. Unrelated backend refactoring and mobile-specific redesign must not be added to this scope.
