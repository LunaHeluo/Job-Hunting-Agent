from pathlib import Path


def test_frontend_displays_real_mock_and_budget_token_states() -> None:
    web = Path(__file__).resolve().parents[2]  / "frontend" / "web"
    index = (web / "index.html").read_text(encoding="utf-8")
    # The UI was split into HTML, JS and CSS modules; contracts span that bundle.
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(web.rglob("*"))
        if path.suffix in {".html", ".js", ".css"}
    )

    assert "tokens=${prompt}/${completion}/${total}" in html
    assert 'tokenText = "tokens=mock"' in html
    assert "本会话 tokens:" in html
    assert "接近预算" in html
    assert "已超出预算" in html
    assert "上下文摘要正在执行" in html
    assert "summary前 tokens=${trace.before_tokens}" in html
    assert "summary后 tokens=${trace.after_tokens}" in html
    assert "summary_id=${trace.summary_id}" in html
    assert 'id="settingsButton"' not in index
    assert index.count('id="workbenchSettingsButton"') == 1
    for setting_id in ("apiBase", "providerSelect", "modelSelect", "chatKnowledgeMode"):
        assert index.index('id="settingsOverlay"') < index.index(f'id="{setting_id}"')
    assert 'id="toolGovernanceToggle"' in html
    assert "tool_governance_enabled = state.toolGovernanceEnabled" in html
    assert 'localStorage.getItem(TOOL_GOVERNANCE_STORAGE_KEY) !== "false"' in html
    assert "工具治理已关闭 · ${rawText} tokens 原样进入上下文" in html
    assert "未超过阈值，未裁剪" in html
    assert "减少 ${reduction.toFixed(1)}% · 已裁剪" in html
    assert 'metrics.className = "tool-governance-metrics"' in html
    removed_name = "search_job" + "_description"
    assert removed_name not in html
    assert "raw_result_tokens" in html
    assert "context_result_tokens" in html
    assert "event.display ||" in html
    assert 'event.result.finish_reason === "continuation_required"' in html
    assert "继续生成（已完成 ${continuation.model_calls} 次模型调用" in html
    assert "await sendMessage(continuation.next_message)" in html
    assert "appendHistoricalTool(rendered, message)" in html
    assert 'id="clearAllSessionsButton"' in html
    assert html.index('id="clearAllSessionsButton"') < html.index('id="sessionList"')
    assert "clearAllSessionsButton.addEventListener" in html
    assert "loadSessions(false)" in html
    assert "sessionListEl.scrollHeight" in html
    assert "scrollbar-gutter: stable" in html
    assert "overflow-y: scroll" in html
    assert "清除全部" in html
