from pathlib import Path


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))


def test_capability_navigation_is_modal_local_and_has_shared_layout() -> None:
    for contract in (
        'id="capabilitiesNavButton"',
        'id="capabilitiesView"',
        'openAdvancedWindow("capabilities", settingsReturnFocus)',
        "function setCapabilityRoute(route)",
        'advancedWindow.activeType() === "capabilities"',
        'id="capabilityServersTab"',
        'id="capabilitySkillsTab"',
        'id="capabilityRefreshTime"',
        'id="capabilityGlobalError"',
        'aria-live="polite"',
        'id="capabilityList"',
        'id="capabilityDetail"',
        "capability-skeleton",
        "capability-empty",
        "capability-stale",
    ):
        assert contract in HTML


def test_capability_details_cover_mcp_and_skill_governance() -> None:
    for contract in (
        "Tools",
        "Resources",
        "Prompts",
        "Tool Schema",
        "Policy scope",
        "Context exposure",
        "connect",
        "disconnect",
        "health-check",
        "refresh",
        "管理员 Raw definition",
        "完整定义，包含示例、验证规则与失败策略",
        "/raw",
        "/health",
        "reload",
    ):
        assert contract in HTML


def test_external_capability_content_is_rendered_with_dom_apis() -> None:
    for function_name in (
        "renderCapabilityServerDetail",
        "renderCapabilityToolDetail",
        "renderCapabilitySkillDetail",
        "renderCapabilityRawDefinition",
        "renderCapabilityError",
    ):
        assert f"function {function_name}" in HTML

    for target in (
        "capabilityList",
        "capabilityDetail",
        "capabilityGlobalError",
        "capabilityConfirmation",
    ):
        assert f"{target}.innerHTML" not in HTML
    assert "textContent" in HTML


def test_capability_layout_is_keyboard_and_narrow_screen_accessible() -> None:
    for contract in (
        'role="tablist"',
        'role="tab"',
        'aria-controls="capabilityList"',
        'aria-controls="capabilityDetail"',
        "@media (max-width: 800px)",
        "grid-template-columns: 1fr;",
        "focus()",
        'event.key === "Escape"',
    ):
        assert contract in HTML


def test_server_tools_use_a_bounded_responsive_scroll_region() -> None:
    for contract in (
        ".capability-tools-list {",
        "max-height: 560px;",
        "overflow-y: auto;",
        "overscroll-behavior: contain;",
        "scrollbar-gutter: stable;",
        "max-height: 420px;",
        '"capability-tools-list"',
        'toolList.setAttribute("aria-label", "Server tools")',
        "toolList.appendChild(toolButton)",
    ):
        assert contract in HTML


def test_management_confirmation_and_raw_definition_state_are_isolated() -> None:
    for contract in (
        "reconcileManagementConfirmations",
        "isManagementConfirmation",
        "pending?session_id=management",
        "confirmationProposalIds",
        "clearCapabilityRawState",
        "renderCurrentCapabilityState()",
    ):
        assert contract in HTML
