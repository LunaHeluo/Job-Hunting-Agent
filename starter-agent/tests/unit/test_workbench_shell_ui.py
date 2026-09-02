from pathlib import Path


WEB = Path("frontend/web")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.js")))
CSS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.css")))


def test_workbench_shell_has_required_regions_and_agent_location() -> None:
    for contract in (
        'id="workbenchView"',
        'id="workbenchWorkspaceSelect"',
        'id="workbenchProgressSteps"',
        'class="workbench-left"',
        'class="workbench-main"',
        'class="workbench-right"',
        'class="workbench-card workbench-agent-card"',
        'id="workbenchCandidateRail"',
        'aria-label="简历定制进度"',
    ):
        assert contract in HTML


def test_workbench_modes_use_backend_home_without_fake_success_data() -> None:
    assert "/v1/workbench/workspaces?limit=50" in JS
    assert "/home`" in JS
    assert "不会显示虚假统计" in JS
    assert "stats.resume_count" in JS
    assert "stats.job_count" in JS
    assert "86 / 100" not in HTML
    assert "92" not in HTML


def test_workbench_visual_tokens_focus_and_breakpoints_are_explicit() -> None:
    for contract in (
        "--wb-bg: #f7f6f2",
        "--wb-accent: #176b4d",
        ":focus-visible",
        "grid-template-columns: 360px minmax(640px,1fr) 300px",
        "@media (max-width: 1439px)",
        "@media (max-width: 1279px)",
        "@media (max-width: 1023px)",
        "@media (max-width: 767px)",
        "@media (max-width: 374px)",
    ):
        assert contract in CSS


def test_workbench_routes_are_first_class_and_existing_routes_remain() -> None:
    for route in (
        '#/workbench',
        '#/version-map',
        '#/applications',
        '#/chat',
        '#/knowledge',
        '#/capabilities/mcp-servers',
        '#/trust/evals',
    ):
        assert route in JS or route in HTML


def test_application_board_uses_backend_timeline_and_confirmation() -> None:
    for contract in (
        'id="applicationsPageTab"',
        "/v1/workbench/applications?",
        "时间线 ${application.events.length} 条",
        "明确确认并记录",
        "不会访问招聘网站",
    ):
        assert contract in JS or contract in HTML
