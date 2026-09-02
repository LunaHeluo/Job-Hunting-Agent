from pathlib import Path


SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted(Path("frontend/web").rglob("*.js"))
)
HTML = Path("frontend/web/index.html").read_text(encoding="utf-8")


def test_operation_card_distinguishes_execution_and_business_commit() -> None:
    for contract in (
        "Run 已完成，正在验证业务结果",
        "验证通过，正在提交业务对象",
        "Run 成功不等于业务成功",
        "commit_failed",
        "waiting_for_user",
    ):
        assert contract in SOURCE
    assert "workbenchOperationCards" in HTML


def test_monitor_recovers_with_rest_sse_and_event_seq_deduplication() -> None:
    for contract in (
        "/events?after_seq=",
        "/events/stream?after_seq=",
        "state.seen.has",
        "state.lastSeq",
        "2 ** state.attempts",
        "AbortController",
        "TERMINAL.has",
    ):
        assert contract in SOURCE


def test_cancel_uses_authoritative_run_version_and_idempotency() -> None:
    for contract in (
        "/cancel`",
        "expected_version: detail.parent.version",
        "idempotency_key:",
        "不会再启动新的模型或 Tool 调用",
    ):
        assert contract in SOURCE
