import ast
import json
import os
import re
from pathlib import Path

import pytest
import yaml

from starter_agent.settings import SerpApiToolConfig, load_settings
from starter_agent.tools.builtin.job_search import SearchJobsSerpApiTool
from starter_agent.tools.registry import ToolRegistry


PROJECT_ROOT = Path(__file__).parents[2]
AUDIT_PATH = PROJECT_ROOT / "docs" / "job-research-implementation-audit.md"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def _audit_text() -> str:
    return AUDIT_PATH.read_text(encoding="utf-8")


def _documented_serpapi_schema(audit: str) -> dict[str, object]:
    match = re.search(
        r"完整 schema：\s*```json\s*(\{.*?\})\s*```",
        audit,
        flags=re.DOTALL,
    )
    assert match is not None, "audit must contain the structured SerpAPI schema"
    return json.loads(match.group(1))


def _assert_serpapi_contract_matches_source(audit: str) -> None:
    tool = SearchJobsSerpApiTool

    assert f"**Name**：`{tool.name}`" in audit
    assert f"**Description**：`{tool.description}`" in audit
    assert f"**Risk**：`{tool.risk_level}`" in audit
    assert _documented_serpapi_schema(audit) == tool.input_schema


def _declared_tool_names() -> set[str]:
    names: set[str] = set()
    tools_root = PROJECT_ROOT  / "backend" / "src" / "starter_agent" / "tools"
    for path in tools_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for statement in node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == "name"
                    for target in statement.targets
                ):
                    continue
                if isinstance(statement.value, ast.Constant) and isinstance(
                    statement.value.value, str
                ):
                    names.add(statement.value.value)
    return names


def _project_skill_definitions() -> list[Path]:
    definitions: list[Path] = []
    ignored_directories = {".git", ".venv", "__pycache__"}
    for current, directories, filenames in os.walk(PROJECT_ROOT):
        directories[:] = [
            name
            for name in directories
            if name not in ignored_directories
            and not name.startswith(".")
            and not name.startswith(".session-only-")
        ]
        if "SKILL.md" in filenames:
            definitions.append(Path(current) / "SKILL.md")
    return definitions


def _source_class_names() -> set[str]:
    names: set[str] = set()
    source_root = PROJECT_ROOT  / "backend" / "src" / "starter_agent"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names.update(
            node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
        )
    return names


def test_job_research_audit_links_to_real_repository_files() -> None:
    audit = _audit_text()
    required_source_paths = (
        "backend/src/starter_agent/settings.py",
        "backend/src/starter_agent/bootstrap.py",
        "backend/src/starter_agent/tools/registry.py",
        "backend/src/starter_agent/tools/base.py",
        "backend/src/starter_agent/tools/policy.py",
        "backend/src/starter_agent/agent/runtime.py",
        "backend/src/starter_agent/interfaces/api.py",
        "backend/src/starter_agent/observability/logging.py",
        "frontend/web/index.html",
    )

    for source_path in required_source_paths:
        assert (PROJECT_ROOT / source_path).is_file()
        assert source_path in audit


def test_serpapi_audit_contract_matches_tool_source() -> None:
    _assert_serpapi_contract_matches_source(_audit_text())


def test_serpapi_audit_config_and_registration_match_repository() -> None:
    audit = _audit_text()
    raw_config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    settings = load_settings(CONFIG_PATH)
    raw_serpapi = raw_config["tools"]["serpapi"]
    actual_config = SerpApiToolConfig.model_validate(raw_serpapi)
    config_match = re.search(
        r"当前值为 profile `(?P<active_key>[^`]+)`、"
        r"切换变量 `(?P<active_key_env>[^`]+)`、"
        r"(?P<timeout>[\d.]+) 秒 timeout、"
        r"(?P<retries>\d+) 次 retry、"
        r"(?P<backoff>[\d.]+) 秒 backoff，"
        r"(?P<profiles>[^ ]+) 分别引用 `(?P<first_env>[^`]+)` 与 `(?P<second_env>[^`]+)`",
        audit,
    )

    assert actual_config == settings.tools.serpapi
    assert SearchJobsSerpApiTool.name in raw_config["tools"]["enabled"]
    assert config_match is not None, "audit must expose the real SerpAPI config values"
    documented_config = config_match.groupdict()
    profile_names = list(actual_config.keys)
    assert documented_config == {
        "active_key": actual_config.active_key,
        "active_key_env": actual_config.active_key_env,
        "timeout": f"{actual_config.timeout_seconds:g}",
        "retries": str(actual_config.max_retries),
        "backoff": f"{actual_config.retry_backoff_seconds:g}",
        "profiles": "/".join(profile_names),
        "first_env": actual_config.keys[profile_names[0]].api_key_env,
        "second_env": actual_config.keys[profile_names[1]].api_key_env,
    }

    registry = ToolRegistry([SearchJobsSerpApiTool.name], settings=settings)
    registered = registry.get(SearchJobsSerpApiTool.name)
    assert isinstance(registered, SearchJobsSerpApiTool)
    assert registry.schemas() == [registered.schema()]
    assert registered.input_schema == _documented_serpapi_schema(audit)


def test_implemented_dependencies_match_project_scope() -> None:
    evidence_tool = "retrieve_resume_evidence"
    registry = ToolRegistry(
        [SearchJobsSerpApiTool.name],
        settings=load_settings(CONFIG_PATH),
    )

    assert evidence_tool in _declared_tool_names()
    assert registry.get(evidence_tool) is None
    assert _project_skill_definitions() == [
        PROJECT_ROOT
         / "backend" / "src" / "starter_agent"
        / "skills"
        / "job-research"
        / "SKILL.md"
    ]
    assert {
        "SkillRegistry",
        "SkillParser",
        "SkillSelector",
    }.issubset(_source_class_names())


def test_serpapi_source_comparison_detects_documented_schema_drift() -> None:
    audit = _audit_text()
    drifted_audit = audit.replace('"maximum": 10', '"maximum": 11', 1)

    assert drifted_audit != audit
    with pytest.raises(AssertionError):
        _assert_serpapi_contract_matches_source(drifted_audit)


def test_trust_layer_task1_audit_captures_current_runtime_and_gaps() -> None:
    audit = _audit_text()
    required_current_entries = (
        "backend/src/starter_agent/agent/runtime.py",
        "backend/src/starter_agent/application.py",
        "backend/src/starter_agent/agent/context.py",
        "backend/src/starter_agent/capabilities/registry.py",
        "backend/src/starter_agent/mcp/manager.py",
        "backend/src/starter_agent/skills/registry.py",
        "backend/src/starter_agent/capabilities/gate.py",
        "backend/src/starter_agent/capabilities/store.py",
        "backend/src/starter_agent/infrastructure/session_store.py",
        "backend/src/starter_agent/observability/logging.py",
        "frontend/web/index.html",
        "model.context.snapshot",
        "gate.evaluated",
        "tool.started",
        "tool.completed",
        "/v1/capabilities/traces",
        "/v1/capabilities/context-snapshots/{session_id}",
        "turn_usage",
        "tool_artifacts",
        "job-research",
        "JobResearchOrchestrator",
        "search_jobs_serpapi",
        "mcp__playwright__browser_navigate",
        "mcp__playwright__browser_snapshot",
        "retrieve_resume_evidence",
        "#/chat",
        "#/knowledge",
        "#/capabilities/mcp-servers",
        "#/capabilities/skills",
    )
    required_gaps = (
        "没有专用 Eval Runner",
        "没有固定求职调研 Fixture 目录",
        "没有 Eval Run/Case 存储",
        "没有完整 Trust Center",
        "没有真实模型 Smoke",
        "缺少独立 model_request_id",
        "缺少独立 policy_decision_id",
    )

    for entry in required_current_entries:
        assert entry in audit
    for gap in required_gaps:
        assert gap in audit
