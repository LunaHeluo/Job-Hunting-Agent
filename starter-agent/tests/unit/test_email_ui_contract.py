from pathlib import Path


def test_frontend_supports_manual_email_preview_approval_and_send() -> None:
    web = Path(__file__).resolve().parents[2]  / "frontend" / "web"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(web.rglob("*.html")) + sorted(web.rglob("*.js"))
    )

    assert "queueEmailApproval" in source
    assert "renderEmailApprovalCard" in source
    assert "confirmAndSendEmail" in source
    assert "cancelEmailApproval" in source
    assert "/v1/email/drafts/${item.draftId}/approval-challenges" in source
    assert (
        "/v1/email/approval-challenges/"
        "${approval.approval_id}/confirm"
    ) in source
    assert "/v1/email/approvals/${approval.approval_id}/send" in source
    assert "邮件已成功发送" in source
    assert "发送结果待核验，请勿重复发送" in source
