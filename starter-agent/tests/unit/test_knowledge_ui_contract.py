from pathlib import Path


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))


def test_primary_navigation_and_knowledge_controls_exist() -> None:
    for contract in (
        'id="chatNavButton"',
        'id="knowledgeNavButton"',
        'id="knowledgeView"',
        'id="knowledgeFile"',
        'accept=".md,.markdown"',
        'id="knowledgeAuthorized"',
        'id="knowledgeUploadButton"',
        'id="knowledgeStatus"',
        'aria-live="polite"',
        'id="knowledgeDocumentList"',
        'id="knowledgeChunkPreview"',
        'id="chatKnowledgeMode"',
    ):
        assert contract in HTML
    assert '<select id="chatKnowledgeMode">' in HTML
    assert '<option value="auto" selected>' in HTML
    assert '<option value="off">' in HTML
    assert 'id="chatKnowledgeMode" type="checkbox"' not in HTML


def test_knowledge_ui_calls_lifecycle_apis_and_uses_safe_rendering() -> None:
    for contract in (
        "/v1/knowledge-bases",
        "/documents",
        "/chunks",
        'method: "DELETE"',
        '"If-Match"',
        "window.confirm",
        "textContent",
        'payload.knowledge_mode = "required"',
        'chatKnowledgeMode.value === "auto"',
        'payload.knowledge_mode = "off"',
        'if (chatKnowledgeMode.value === "auto") await loadKnowledgeBase();',
    ):
        assert contract in HTML
    assert "knowledgeDocumentList.innerHTML" not in HTML
    assert "knowledgeChunkPreview.innerHTML" not in HTML


def test_knowledge_documents_and_chunks_use_scrollable_master_detail_layout() -> None:
    for contract in (
        'class="knowledge-browser"',
        'class="knowledge-document-toolbar"',
        'class="knowledge-document-pane"',
        'class="knowledge-chunk-pane"',
        'id="knowledgeChunkTitle"',
        'row.classList.toggle("is-selected"',
        'row.setAttribute("aria-current"',
        "overflow-y: auto",
        "height: clamp(480px, calc(100vh - 170px), 820px)",
        "grid-template-rows: auto minmax(0, 1fr)",
        "grid-template-columns: minmax(300px, 0.82fr) minmax(0, 1.18fr)",
        "@media (max-width: 1100px)",
        "@media (max-width: 700px)",
        "height: min(46vh, 440px)",
    ):
        assert contract in HTML
    assert "grid-template-columns: 1fr" in HTML


def test_knowledge_documents_support_bulk_selection_and_delete() -> None:
    for contract in (
        "selectedKnowledgeDocumentIds = new Set()",
        "knowledgeDeleteSelectedButton",
        "knowledgeSelectAllDocuments",
        "toggleKnowledgeDocumentSelection(item.id, checkbox.checked)",
        "updateKnowledgeBulkActions()",
        "deleteSelectedKnowledgeDocuments",
        "deleteKnowledgeDocumentRequest(item)",
        "selectedKnowledgeDocumentIds.clear()",
        "knowledgeDeleteSelectedButton.disabled = selectedKnowledgeDocumentIds.size === 0",
    ):
        assert contract in HTML
