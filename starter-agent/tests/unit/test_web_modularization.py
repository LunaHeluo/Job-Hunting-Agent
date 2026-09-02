from pathlib import Path


WEB = Path("frontend/web")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")


def test_index_is_document_shell_with_external_assets() -> None:
    assert '<link rel="stylesheet" href="./styles/app.css">' in INDEX
    assert '<script type="module" src="./app.js"></script>' in INDEX
    assert "<style>" not in INDEX
    assert "function boot()" not in INDEX
    assert len(INDEX.splitlines()) < 500


def test_frontend_foundation_modules_are_dependency_light() -> None:
    for path in (
        WEB / "app/api-client.js",
        WEB / "app/router.js",
        WEB / "app/store.js",
        WEB / "styles/tokens.css",
        WEB / "styles/base.css",
        WEB / "styles/legacy.css",
    ):
        assert path.is_file() and path.stat().st_size > 0
    assert 'from "./app/api-client.js"' in APP
    assert 'from "./app/router.js"' in APP
    assert 'from "./app/store.js"' in APP


def test_existing_mount_points_and_accessible_names_remain_in_shell() -> None:
    for contract in (
        'id="chatView"',
        'id="knowledgeView"',
        'id="capabilitiesView"',
        'id="trustView"',
        'id="messages"',
        'id="composer"',
        'aria-label="主导航"',
    ):
        assert contract in INDEX
