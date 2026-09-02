# Unified Desktop Workbench QA — 2026-08-26

## Scope and environment

Task 7 fix round 1 was accepted against commit `8681c05` plus the changes in this record. The application ran with `uv run agent serve` at `http://127.0.0.1:8000`; the frontend ran with `python -m http.server 8001 --directory frontend/web` at `http://127.0.0.1:8001`. Desktop checks used Microsoft Edge 151.0.4129.107 in headless mode through the Chrome DevTools Protocol at device scale factor 1. The reproducible measurement output is `artifacts/unified-shell-qa/browser-acceptance.json`.

## Automated regression results

| Suite | Exact command | Exit code | Result |
| --- | --- | ---: | --- |
| Focused frontend contracts | `uv run pytest tests/unit/test_unified_shell_ui.py tests/unit/test_workbench_shell_ui.py tests/unit/test_workbench_agent_ui.py tests/unit/test_workbench_quality.py tests/unit/test_knowledge_ui_contract.py tests/unit/test_capability_ui_contract.py tests/unit/test_trust_ui_contract.py tests/unit/test_token_ui_contract.py tests/unit/test_tool_confirmation_ui_contract.py -q` | 0 | 65 passed, 0 failed |
| Complete repository suite (run once) | `uv run pytest -q` | 1 | 0 tests executed; collection interrupted by 9 errors |
| Final unified-shell contract | `uv run pytest tests/unit/test_unified_shell_ui.py -q` | 0 | 24 passed, 0 failed |
| Browser acceptance harness | `node artifacts/unified-shell-qa/run-qa.mjs` | 0 | 3 desktop viewports, 1 safe-flow viewport, real Agent input, and the interaction sequence completed |

The one complete-suite run stopped during collection because nine modules import helpers through `tests.*`, but this environment did not resolve `tests` as an importable package (`ModuleNotFoundError: No module named 'tests'`). The affected files were five integration modules, `tests/unit/orchestration/test_pending_action.py`, and three unit modules. No test body ran, so this record does not misreport the complete suite as green.

The existing SDD baseline ruling also records an unrelated Playwright MCP configuration assertion mismatch and an intentional crash-recovery `KeyboardInterrupt`. This run did not reach those cases because collection stopped earlier. None of the three conditions was changed or masked in Task 7.

## Desktop geometry and screenshots

All values are integer CSS pixels from `getBoundingClientRect()`. `.workbench-left`, `.workbench-canvas`, and `.workbench-context-card` must differ by no more than 1px at their top and bottom edges.

| Viewport | Left top/bottom/height | Canvas top/bottom/height | Context top/bottom/height | Max top delta | Max bottom delta | Horizontal overflow | Screenshot |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| 1280x800 | 64 / 758 / 694 | 64 / 758 / 694 | 64 / 758 / 694 | 0 | 0 | PASS — 1280 scrollWidth / 1280 clientWidth | `artifacts/unified-shell-qa/unified-shell-1280x800.png` |
| 1440x900 | 64 / 858 / 794 | 64 / 858 / 794 | 64 / 858 / 794 | 0 | 0 | PASS — 1440 / 1440 | `artifacts/unified-shell-qa/unified-shell-1440x900.png` |
| 1920x1080 | 64 / 1038 / 974 | 64 / 1038 / 974 | 64 / 1038 / 974 | 0 | 0 | PASS — 1920 / 1920 | `artifacts/unified-shell-qa/unified-shell-1920x1080.png` |

Visual inspection of all three screenshots found the persistent Agent rail, center canvas, right context rail, action bar, and top navigation visible without clipping or unintended page-width expansion. At 1280x800 the composer/input/send bounds were `580–652 / 580–652 / 616–652`; at 1440x900 they were `644–716 / 644–716 / 680–716`. Each control was visible, inside the viewport, and the hit-test target. CDP mouse events focused the textarea and `Input.insertText` entered `Edge 1280 composer input` and `Edge 1440 composer input`; the harness did not assign the hidden input value directly.

## State, route, modal, and keyboard checks

| # | Check | Status | Observed evidence |
| ---: | --- | --- | --- |
| 1 | Agent composer input survives primary navigation | PASS | The exact draft `Task 7 draft stays with the Agent` remained after Workbench → Version Map → Applications Board → Workbench. |
| 2 | Agent message scroll survives primary navigation | PASS | The real `.messages` scroll position was 86px before and 86px after the route sequence. |
| 3 | All three primary routes activate without replacing the shell | PASS | Hashes and `aria-current` progressed through `#/version-map`, `#/applications`, and `#/workbench`; the persistent Agent DOM remained mounted. Version Map and Applications showed their explicit `版本详情` and `投递详情` empty contexts instead of retaining Workbench candidate context. |
| 4 | Center/right route scroll and selection context restore | PASS | Browser QA restored Workbench center to 88px and right context to 64px. Its clean backend had no selected resume/job, so browser QA does not claim a non-empty selection. The focused behavior contract separately rendered non-empty Version Map and Applications fixtures, re-rendered each route, and verified selected id, active state, inspector/context callback, and removal recovery. |
| 5 | Settings opens Knowledge, Models/Tools/MCP, and Trust in K1 | PASS | The single Agent-header Settings button opened the overlay; its Knowledge, Capability, and Trust triggers opened the expected panel and title inside `#advancedDialog`, while the primary hash did not change. Connection/model/knowledge configuration lives in this overlay and the legacy profile Settings trigger is absent. |
| 6 | Escape closes K1 and returns focus | PASS | Knowledge, Capability, and Trust each closed on a real Escape key event and returned focus to `#workbenchSettingsButton`. |
| 7 | Failed advanced request remains local to K1 | PASS | With the API temporarily set to `127.0.0.1:9`, Knowledge rendered `Failed to fetch` inside `#advancedDialog`; K1 and the persistent shell remained present, the legacy shell remained absent, and no horizontal overflow appeared. |
| 8 | Keyboard focus visibility and trap | PASS | A real Tab event recovered focus from outside K1 to `#advancedCloseButton`; `:focus-visible` was true. Shift+Tab from the first control wrapped to a visible control inside the dialog, with no keyboard escape from the trap. |

The focused Trust ownership regression contract also verified that a pending eval run disables the button, a stale close/tab transition does not mutate a detached window, and settlement restores the button state when the current Trust Evals view is activated again.

## Safe-flow below 1280px

Widths below 1280px received safe-flow rather than pixel-perfect acceptance. At 1024x768, the layout used one 969px grid track with body scrolling enabled. The left, canvas, and context regions flowed vertically at tops 64, 769, and 1291px, and document scroll width equaled client width (1009px), so there was no horizontal overflow. No full responsive redesign is asserted.

## Task 7 regression found and resolved

The first Task 7 browser measurement exposed `.workbench-context-card` as `0×0` in all three desktop viewports; the stage renderer was corrected to keep the shell-owned context rail visible. Final review then found the Agent composer below the viewport at 1280x800 and 1440x900, route selection/context loss after Version Map and Applications re-render, and a Trust eval run button that could remain disabled after stale close/tab ownership changes. Fix round 1 moved connection controls into the single Agent Settings overlay, rebuilt route selection/context from stored ids, added explicit route empty contexts, and made the Trust run disabled state session-owned. The final measurements above come from a fresh, cache-disabled Edge navigation after those fixes.
