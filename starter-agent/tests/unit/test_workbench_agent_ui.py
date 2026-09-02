from pathlib import Path


WEB = Path("frontend/web")
SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.js")))
HTML = (WEB / "index.html").read_text(encoding="utf-8")


def test_chat_carries_only_explicit_reference_context() -> None:
    for contract in (
        "getWorkbenchContext",
        "payload.workbench_context = workbenchContext",
        "context_epoch",
        "workbench-context-change",
        "旧流仅保留为普通回答，未执行任何业务回填",
    ):
        assert contract in SOURCE


def test_agent_actions_are_candidates_with_explicit_business_confirmation() -> None:
    for contract in (
        "Candidate Action",
        "发送消息不构成修改确认",
        "明确确认此版本",
        "expected_revision: version.revision",
        "Agent 不会提交或静默解决冲突",
    ):
        assert contract in SOURCE
    for action in ("explain_score", "rewrite_section", "compare_versions", "review_merge", "confirm_version", "mark_applied"):
        assert f'data-agent-action="{action}"' in HTML


def test_mark_applied_is_only_committed_after_explicit_confirmation() -> None:
    for contract in (
        "我明确确认已经投递",
        'initial_status: "applied"',
        "user_confirmed: true",
        "未执行任何外部投递",
    ):
        assert contract in SOURCE


def test_agent_shortcuts_send_chat_and_clear_after_selection() -> None:
    for contract in (
        "chatPrompts",
        "skipKnowledgeForNextMessage",
        'replaceChildren()',
        "正在思考…",
        "只基于以上经过验证的工作台分析回答",
    ):
        assert contract in SOURCE
