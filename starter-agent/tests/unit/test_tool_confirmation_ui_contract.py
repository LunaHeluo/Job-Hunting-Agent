from pathlib import Path


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))


def test_chat_confirmation_cards_are_real_dom_and_cover_safe_summary() -> None:
    for contract in (
        'id="chatConfirmationCards"',
        "function renderToolConfirmation",
        "function appendToolConfirmationField",
        "Server",
        "Tool",
        "参数安全摘要",
        "风险",
        "目标 / 数据去向",
        "过期时间",
        "Audit",
        "Trace",
    ):
        assert contract in HTML

    assert "chatConfirmationCards.innerHTML" not in HTML
    assert "card.innerHTML" not in HTML
    assert "textContent" in HTML


def test_stream_events_and_decisions_are_wired_to_chat_confirmation_state() -> None:
    for contract in (
        'event.type === "confirmation_required"',
        'event.type === "confirmation_resolved"',
        'event.type === "tool_started"',
        'event.type === "tool_completed"',
        "decideToolConfirmation",
        '"once"',
        '"allowlist"',
        '"cancel"',
        "allowlist_allowed",
        "allowlist_reason",
        "confirmationDecisionLocks",
        "confirmationIdempotencyKeys",
        "crypto.randomUUID()",
    ):
        assert contract in HTML


def test_pending_confirmations_restore_by_current_session_without_freezing_views() -> None:
    for contract in (
        "loadChatConfirmations",
        "/v1/capabilities/confirmations/pending?session_id=",
        "reconcileChatConfirmations",
        "setChatPending",
        "messageInput.disabled",
        "sendButton.disabled",
        "capabilitiesNavButton",
        "switchSession",
    ):
        assert contract in HTML

    assert "capabilitiesNavButton.disabled" not in HTML


def test_chat_confirmation_cards_are_compact_and_scroll_long_details() -> None:
    for contract in (
        "width: min(720px, calc(100% - 40px));",
        "justify-self: start;",
        "max-height: min(34vh, 280px);",
        "overflow: auto;",
        "min-width: 0;",
        "overflow-wrap: anywhere;",
        "chat-confirmation-details",
        "查看技术详情",
    ):
        assert contract in HTML


def test_terminal_chat_confirmations_are_removed_from_all_authoritative_paths() -> None:
    for contract in (
        "isTerminalToolConfirmation",
        "function removeChatConfirmation",
        "function pruneTerminalChatConfirmations",
        "pruneTerminalChatConfirmations();",
        "removeChatConfirmation(decided.id)",
        "removeChatConfirmation(resolved.id)",
        "removeChatConfirmation(normalized.id)",
        "removeChatConfirmation(event.confirmation_id)",
        "!isTerminalToolConfirmation(normalized)",
    ):
        assert contract in HTML
