from pathlib import Path

from starter_agent.settings import load_settings
from starter_agent.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[2]
SYSTEM_PROMPT = ROOT / "config" / "prompts" / "system.md"
JOB_RESEARCH_SKILL = ROOT  / "backend" / "src" / "starter_agent" / "skills" / "job-research" / "SKILL.md"


def test_system_prompt_allows_public_job_search_to_fetch_jd_without_selection() -> None:
    prompt = SYSTEM_PROMPT.read_text(encoding="utf-8")

    removed_name = "search_job" + "_description"
    assert f"Call `{removed_name}`" not in prompt
    assert "Playwright MCP" in prompt
    assert "read separately" in prompt
    assert "snippets are not complete JDs" in prompt


def test_job_research_skill_allows_auto_jd_enrichment_for_public_job_queries() -> None:
    skill = JOB_RESEARCH_SKILL.read_text(encoding="utf-8")

    assert "不等待用户先提供或选择 URL" in skill
    assert "不要自动入库" in skill
    assert "单个失败时保留错误并继续下一个候选" in skill


def test_legacy_job_description_tool_is_absent_from_runtime_and_prompt() -> None:
    removed_name = "search_job" + "_description"
    settings = load_settings(ROOT / "config" / "config.yaml")
    prompt = SYSTEM_PROMPT.read_text(encoding="utf-8")

    assert removed_name not in settings.tools.enabled
    assert removed_name not in prompt
    assert removed_name not in {
        tool.name for tool in ToolRegistry(settings.tools.enabled, settings).list()
    }
    assert not (
        ROOT
         / "backend" / "src" / "starter_agent"
        / "tools"
        / "builtin"
        / ("job_description" + "_search.py")
    ).exists()
