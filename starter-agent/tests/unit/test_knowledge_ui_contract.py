from html.parser import HTMLParser
from pathlib import Path


WEB = Path("frontend/web")
HTML = "\n".join(path.read_text(encoding="utf-8") for path in (WEB / "index.html", *sorted(WEB.rglob("*.css")), *sorted(WEB.rglob("*.js"))))
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


class IdTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ancestors_by_id: dict[str, tuple[str, ...]] = {}
        self.ancestor_ids_by_id: dict[str, tuple[str, ...]] = {}
        self.stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ancestors_by_id[element_id] = tuple(tag_name for tag_name, _ in self.stack)
            self.ancestor_ids_by_id[element_id] = tuple(parent_id for _, parent_id in self.stack if parent_id)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}:
            self.stack.append((tag, attributes.get("id")))

    def handle_endtag(self, tag: str) -> None:
        tags = [tag_name for tag_name, _ in self.stack]
        if tag in tags:
            del self.stack[tags[::-1].index(tag) * -1 - 1 :]


def parse_index_tree() -> IdTreeParser:
    parser = IdTreeParser()
    parser.feed(INDEX_HTML)
    return parser


def test_primary_navigation_and_knowledge_controls_exist() -> None:
    for contract in (
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


def test_knowledge_navigation_stays_in_settings_and_knowledge_view_in_advanced_dialog() -> None:
    tree = parse_index_tree()
    for button_id in ("knowledgeNavButton", "capabilitiesNavButton", "trustNavButton"):
        assert "settingsOverlay" in tree.ancestor_ids_by_id[button_id]
    assert "advancedDialog" in tree.ancestor_ids_by_id["knowledgeView"]


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
        ".advanced-dialog-body .knowledge-layout",
        "height: 100%;",
        "grid-template-rows: auto minmax(0, 1fr)",
        "grid-template-columns: minmax(300px, 0.82fr) minmax(0, 1.18fr)",
        "@media (max-width: 1100px)",
        "@media (max-width: 700px)",
        "height: 440px",
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
