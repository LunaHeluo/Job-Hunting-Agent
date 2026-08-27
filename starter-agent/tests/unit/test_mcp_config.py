import json
from pathlib import Path

import pytest

from starter_agent.mcp.config import (
    McpConfigError,
    McpConfigLoader,
    McpConfiguration,
    McpServerConfig,
)
from starter_agent.settings import load_settings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_config(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_loads_playwright_config_referenced_by_main_settings() -> None:
    settings = load_settings(PROJECT_ROOT / "config" / "config.yaml")

    config = McpConfigLoader(settings.project_root).load(settings.mcp.config_path)

    playwright = config.servers["playwright"]
    assert playwright.command == "npx"
    # Runtime config is intentionally pinned for reproducible, reviewable browser behavior.
    assert playwright.args == ("--yes", "@playwright/mcp@0.0.79")
    assert all("@latest" not in value for value in playwright.args)
    assert playwright.cwd is None
    assert config.source_path == PROJECT_ROOT / "config" / "mcp.json"


def test_config_hash_is_canonical_and_changes_with_meaningful_content(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    reordered = tmp_path / "reordered.json"
    changed = tmp_path / "changed.json"
    _write_config(
        first,
        {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@latest"],
                }
            }
        },
    )
    reordered.write_text(
        '{\n  "mcpServers": {"playwright": {'
        '"args": ["@playwright/mcp@latest"], "command": "npx"}}\n}',
        encoding="utf-8",
    )
    _write_config(
        changed,
        {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@1.0.0"],
                }
            }
        },
    )
    loader = McpConfigLoader(tmp_path)

    first_config = loader.load(first)
    reordered_config = loader.load(reordered)
    changed_config = loader.load(changed)

    assert first_config.config_hash == reordered_config.config_hash
    assert first_config.config_hash != changed_config.config_hash
    assert len(first_config.config_hash) == 64


def test_rejects_config_path_escape_and_inline_secrets(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.json"
    _write_config(outside, {"mcpServers": {}})
    loader = McpConfigLoader(project_root)

    with pytest.raises(McpConfigError, match="outside project root"):
        loader.load(outside)

    secret_cases = (
        {"env": {"API_TOKEN": "inline-token"}},
        {"command": "npx --token=inline-token"},
        {"args": ["--password=hunter2"]},
        {"args": ["--cookie", "session=inline-cookie"]},
    )
    for index, secret_fields in enumerate(secret_cases):
        path = project_root / f"secret-{index}.json"
        server = {"command": "npx", **secret_fields}
        _write_config(path, {"mcpServers": {"unsafe": server}})
        with pytest.raises(McpConfigError, match="secret|environment names"):
            loader.load(path)


def test_rejects_cwd_escape_unknown_fields_and_control_characters(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    loader = McpConfigLoader(project_root)
    invalid_servers = (
        {"command": "npx", "cwd": "../outside"},
        {"command": "npx", "shell": True},
        {"command": "npx\nwhoami"},
    )

    for index, server in enumerate(invalid_servers):
        path = project_root / f"invalid-{index}.json"
        _write_config(path, {"mcpServers": {"unsafe": server}})
        with pytest.raises(McpConfigError):
            loader.load(path)


@pytest.mark.parametrize(
    "environment_name",
    [
        "NODE_OPTIONS",
        "NPM_CONFIG_USERCONFIG",
        "AWS_SECRET_ACCESS_KEY",
    ],
)
def test_rejects_dangerous_process_environment_names(
    tmp_path: Path,
    environment_name: str,
) -> None:
    path = tmp_path / "mcp.json"
    _write_config(
        path,
        {
            "mcpServers": {
                "unsafe": {
                    "command": "npx",
                    "env": [environment_name],
                }
            }
        },
    )

    with pytest.raises(McpConfigError, match="dangerous|sensitive"):
        McpConfigLoader(tmp_path).load(path)


@pytest.mark.parametrize(
    "secret_value",
    [
        "ghp_" + "A" * 36,
        "AKIA" + "A" * 16,
        "https://user:password@example.test/job",
        "Basic dXNlcjpwYXNzd29yZA==",
    ],
)
def test_rejects_high_confidence_secret_shapes(
    tmp_path: Path,
    secret_value: str,
) -> None:
    path = tmp_path / "mcp.json"
    _write_config(
        path,
        {
            "mcpServers": {
                "unsafe": {
                    "command": "npx",
                    "args": [secret_value],
                }
            }
        },
    )

    with pytest.raises(McpConfigError, match="secret"):
        McpConfigLoader(tmp_path).load(path)


def test_allows_playwright_package_and_non_sensitive_environment_names(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp.json"
    _write_config(
        path,
        {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@latest"],
                    "env": ["PLAYWRIGHT_BROWSERS_PATH", "LANG"],
                }
            }
        },
    )

    config = McpConfigLoader(tmp_path).load(path)

    assert config.servers["playwright"].args == ("@playwright/mcp@latest",)
    assert config.servers["playwright"].env == (
        "LANG",
        "PLAYWRIGHT_BROWSERS_PATH",
    )


def test_loaded_config_is_defensively_copied_immutable_and_round_trips(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp.json"
    _write_config(
        path,
        {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@latest"],
                    "env": ["LANG"],
                }
            }
        },
    )
    config = McpConfigLoader(tmp_path).load(path)
    original_hash = config.config_hash
    args = ["@playwright/mcp@latest"]
    env = ["LANG"]
    server = McpServerConfig(command="npx", args=args, env=env)
    args.append("mutated")
    env.append("MUTATED")

    with pytest.raises(TypeError):
        config.servers["mutated"] = server

    assert "mutated" not in config.servers
    assert config.config_hash == original_hash
    assert server.args == ("@playwright/mcp@latest",)
    assert server.env == ("LANG",)
    assert McpConfiguration.model_validate_json(config.model_dump_json()) == config
