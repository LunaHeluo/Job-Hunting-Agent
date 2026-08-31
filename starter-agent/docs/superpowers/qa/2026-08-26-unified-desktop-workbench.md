# Unified Desktop Workbench QA — 2026-08-26

## Scope and environment

Task 7 was accepted against commit `1ca9316` plus the Task 7 changes in this record. The application ran with `uv run agent serve` at `http://127.0.0.1:8000`; the frontend ran with `python -m http.server 8001 --directory frontend/web` at `http://127.0.0.1:8001`. Desktop checks used Microsoft Edge 151.0.4129.107 in headless mode through the Chrome DevTools Protocol at device scale factor 1. The reproducible measurement output is `artifacts/unified-shell-qa/browser-acceptance.json`.

## Automated regression results

| Suite | Exact command | Exit code | Result |
| --- | --- | ---: | --- |
| Focused frontend contracts | `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py tests/unit/test_workbench_agent_ui.py tests/unit/test_workbench_quality.py tests/unit/test_knowledge_ui_contract.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py tests/unit/test_token_ui_contract.py tests/unit/test_tool_confirmation_ui_contract.py -q` | 0 | 65 passed, 0 failed |
| Complete repository suite (run once) | `uv run pytest -q` | 1 | 0 tests executed; collection interrupted by 9 errors |
| Final unified-shell contract | `uv run pytest tests/unit/test_unified_shell_ui.py -q` | 0 | 24 passed, 0 failed |
| Browser acceptance harness | `node artifacts/unified-shell-qa/run-qa.mjs` | 0 | 3 desktop viewports, 1 safe-flow viewport, and the interaction sequence completed |

The one complete-suite run stopped during collection because nine modules import helpers through `tests.*`, but this environment did not resolve `tests` as an importable package (`ModuleNotFoundError: No module named 'tests'`). The affected files were five integration modules, `tests/unit/orchestration/test_pending_action.py`, and three unit modules. No test body ran, so this record does not misreport the complete suite as green.

The existing SDD baseline ruling also records an unrelated Playwright MCP configuration assertion mismatch and an intentional crash-recovery `KeyboardInterrupt`. This run did not reach those cases because collection stopped earlier. None of the three conditions was changed or masked in Task 7.

## Desktop geometry and screenshots

All values are integer CSS pixels from `getBoundingClientRect()`. `.workbench-left`, `.workbench-canvas`, and `.workbench-context-card` must differ by no more than 1px at their top and bottom edges.

| Viewport | Left top/bottom/height | Canvas top/bottom/height | Context top/bottom/height | Max top delta | Max bottom delta | Horizontal overflow | Screenshot |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| 1280x800 | 64 / 758 / 694 | 64 / 758 / 694 | 64 / 758 / 694 | 0 | 0 | PASS — 1280 scrollWidth / 1280 clientWidth | `artifacts/unified-shell-qa/unified-shell-1280x800.png` |
| 1440x900 | 64 / 858 / 794 | 64 / 858 / 794 | 64 / 858 / 794 | 0 | 0 | PASS — 1440 / 1440 | `artifacts/unified-shell-qa/unified-shell-1440x900.png` |
| 1920x1080 | 64 / 1038 / 974 | 64 / 1038 / 974 | 64 / 1038 / 974 | 0 | 0 | PASS — 1920 / 1920 | `artifacts/unified-shell-qa/unified-shell-1920x1080.png` |

Visual inspection of all three screenshots found the persistent Agent rail, center canvas, right context rail, action bar, and top navigation visible without clipping or unintended page-width expansion.

## State, route, modal, and keyboard checks

| # | Check | Status | Observed evidence |
| ---: | --- | --- | --- |
| 1 | Agent composer input survives primary navigation | PASS | The exact draft `Task 7 draft stays with the Agent` remained after Workbench → Version Map → Applications Board → Workbench. |
| 2 | Agent message scroll survives primary navigation | PASS | The real `.messages` scroll position was 86px before and 86px after the route sequence. |
| 3 | All three primary routes activate without replacing the shell | PASS | Hashes and `aria-current` progressed through `#/version-map`, `#/applications`, and `#/workbench`; the persistent Agent DOM remained mounted. |
| 4 | Center/right route scroll and selection context restore | PASS | Workbench center restored 88px and right context restored 64px. The clean backend had no selected resume/job; its explicit empty selection text stayed identical before and after. A non-empty resume/job selection was therefore not claimed as exercised. |
| 5 | Settings opens Knowledge, Models/Tools/MCP, and Trust in K1 | PASS | Each Settings trigger opened the expected panel and title inside `#advancedDialog`; the primary hash did not change. |
| 6 | Escape closes K1 and returns focus | PASS | Knowledge, Capability, and Trust each closed on a real Escape key event and returned focus to `#workbenchSettingsButton`. |
| 7 | Failed advanced request remains local to K1 | PASS | With the API temporarily set to `127.0.0.1:9`, Knowledge rendered `Failed to fetch` inside `#advancedDialog`; K1 and the persistent shell remained present, the legacy shell remained absent, and no horizontal overflow appeared. |
| 8 | Keyboard focus visibility and trap | PASS | A real Tab event recovered focus from outside K1 to `#advancedCloseButton`; `:focus-visible` was true. Shift+Tab from the first control wrapped to a visible control inside the dialog, with no keyboard escape from the trap. |

## Safe-flow below 1280px

Widths below 1280px received safe-flow rather than pixel-perfect acceptance. At 1024x768, the layout used one 969px grid track with body scrolling enabled. The left, canvas, and context regions flowed vertically at tops 64, 818, and 1340px, and document scroll width equaled client width (1009px), so there was no horizontal overflow. No full responsive redesign is asserted.

## Task 7 regression found and resolved

The first browser measurement exposed `.workbench-context-card` as `0×0` in all three desktop viewports. Root-cause tracing found that stage A still assigned `hidden` to the entire right rail. Task 7 changed the stage renderer to keep the shell-owned context rail visible; the existing empty-state content remains authoritative. The final measurements above are from a fresh, cache-disabled navigation after that fix.
