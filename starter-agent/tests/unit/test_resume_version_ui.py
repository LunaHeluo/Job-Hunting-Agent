from pathlib import Path


WEB = Path("frontend/web")
SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.js")))
CSS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.css")))


def test_resume_import_preserves_input_and_uses_operation_ids() -> None:
    for contract in (
        "/v1/workbench/resumes/imports",
        "confirmed_authorized: true",
        "operation_id:",
        "idempotency_key:",
        "导入失败",
        "submit.disabled = false",
    ):
        assert contract in SOURCE


def test_imported_resume_is_rendered_in_the_workbench_preview() -> None:
    for contract in (
        "renderResumePreview",
        "/resume-versions/${encodeURIComponent(versionId)}/content",
        "resume-document-preview",
        "当前简历档案",
    ):
        assert contract in SOURCE


def test_version_map_uses_backend_lineage_and_graph_adapter() -> None:
    assert "export class GraphRenderer" in SOURCE
    assert "/version-map`" in SOURCE
    assert "map.nodes" in SOURCE
    assert "map.edges" in SOURCE
    assert "parent_version_id" in SOURCE
    assert "child_version_id" in SOURCE
    assert "/view-preference" in SOURCE
    assert "node_positions" in SOURCE
    assert "onPreferenceChange" in SOURCE


def test_draft_and_merge_actions_keep_confirmation_boundaries() -> None:
    for contract in (
        "有尚未保存的修改",
        "pending.disabled = true",
        "/merge-proposals",
        "expected_revision: proposal.revision",
        "operation.status !== \"committed\"",
        "只在目标分支新增一个已确认版本",
    ):
        assert contract in SOURCE


def test_version_map_has_accessible_small_screen_fallback() -> None:
    for contract in (
        'role", "tree"',
        'role", "treeitem"',
        "version-map-canvas",
        "display: flex",
        "flex-direction: column",
        "有上游变化",
        "在工作台打开",
    ):
        assert contract in SOURCE or contract in CSS
