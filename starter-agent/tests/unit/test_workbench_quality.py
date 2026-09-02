from pathlib import Path


WEB = Path("frontend/web")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
GRAPH = (WEB / "app/features/version-map.js").read_text(encoding="utf-8")
APPLICATIONS = (WEB / "app/features/applications-board.js").read_text(encoding="utf-8")
RESUMES = (WEB / "app/features/resume-workspace.js").read_text(encoding="utf-8")
CSS = "\n".join(path.read_text(encoding="utf-8") for path in WEB.rglob("*.css"))


def test_advanced_platform_features_are_available_from_settings() -> None:
    settings = HTML.split('id="settingsOverlay"', 1)[1]
    for element_id in ("knowledgeNavButton", "capabilitiesNavButton", "trustNavButton"):
        assert f'id="{element_id}"' in settings
        assert HTML.count(f'id="{element_id}"') == 1
    assert '#/knowledge' in APP
    assert '#/capabilities/mcp-servers' in APP
    assert '#/trust/evals' in APP


def test_dialog_keyboard_and_motion_accessibility_contracts() -> None:
    assert 'aria-modal="true"' in HTML
    assert 'event.key !== "Tab"' in APP
    assert "settingsReturnFocus?.focus()" in APP
    assert "@media (prefers-reduced-motion: reduce)" in CSS
    assert "@media (forced-colors: active)" in CSS
    assert "min-height: 44px" in CSS


def test_large_version_map_is_incremental_and_keyboard_navigable() -> None:
    assert "const pageSize" in GRAPH
    assert ".slice(0, renderLimit)" in GRAPH
    assert '"加载更多节点"' in GRAPH
    for key in ("ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"):
        assert key in GRAPH
    assert 'role", "tree"' in GRAPH
    assert 'role", "treeitem"' in GRAPH


def test_responsive_breakpoints_cover_acceptance_widths() -> None:
    for breakpoint in (1439, 1279, 1023, 767, 374):
        assert f"max-width: {breakpoint}px" in CSS


def test_post_v1_enhancements_keep_confirmation_and_fact_boundaries() -> None:
    for contract in (
        "/interview-review",
        "只记录你主动输入的事实",
        "确认保存本轮事实",
        "/analytics/funnel",
        "/reminders",
        "不会发送外部消息",
    ):
        assert contract in APPLICATIONS
    assert "/export-templates" in RESUMES
    assert "ats-compact@1.0.0" in RESUMES
