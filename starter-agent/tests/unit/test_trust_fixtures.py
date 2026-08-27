import pytest
from pathlib import Path

from starter_agent.trust.fixtures import (
    FixtureLoadError,
    JobResearchFixtureLoader,
)


PROJECT_ROOT = Path(__file__).parents[2]
FIXTURE_ROOT = PROJECT_ROOT / "evals" / "job-research" / "fixtures"
BAD_FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "trust"


def test_job_research_fixture_manifest_loads_redacted_dataset() -> None:
    loader = JobResearchFixtureLoader(FIXTURE_ROOT)

    manifest = loader.load_manifest()

    assert manifest.id == "job-research-fixtures-v1"
    assert manifest.manifest_hash
    assert {fixture.fixture_type for fixture in manifest.fixtures} == {
        "serpapi_search",
        "jd_page",
        "resume_chunks",
        "mcp_response",
        "tool_error",
        "policy",
        "injection",
        "knowledge_routing",
        "orchestration_scenarios",
        "delegation_scenarios",
    }
    assert {fixture.id for fixture in manifest.fixtures} == {
        "serpapi-ai-agent-redacted-v1",
        "jd-public-ai-agent-redacted-v1",
        "resume-chunks-redacted-v1",
        "mcp-snapshot-public-jd-redacted-v1",
        "tool-error-mcp-unavailable-v1",
        "policy-non-allowlisted-browser-v1",
        "injection-web-pdf-email-tool-result-v1",
        "knowledge-routing-redacted-v1",
        "mixed-job-results-redacted-v1",
        "single-block-jd-redacted-v1",
        "job-posting-json-ld-redacted-v1",
        "browser-error-page-redacted-v1",
        "orchestration-scenarios-redacted-v1",
        "delegation-scenarios-redacted-v1",
    }
    assert all(fixture.content_hash == fixture.expected_hash for fixture in manifest.fixtures)
    assert all(fixture.record.redaction_summary for fixture in manifest.fixtures)
    assert manifest.by_id("jd-public-ai-agent-redacted-v1").data["source_url"] == (
        "https://jobs.example.org/ai-agent-engineer"
    )
    assert manifest.by_id("resume-chunks-redacted-v1").data["chunks"][0]["chunk_id"]
    assert manifest.by_id("injection-web-pdf-email-tool-result-v1").data["vectors"]


def test_fixture_loader_rejects_manifest_path_escape() -> None:
    with pytest.raises(FixtureLoadError, match="outside fixture root"):
        JobResearchFixtureLoader(BAD_FIXTURE_ROOT / "path_escape").load_manifest()


def test_fixture_loader_rejects_unredacted_secret_text() -> None:
    with pytest.raises(FixtureLoadError, match="secret"):
        JobResearchFixtureLoader(BAD_FIXTURE_ROOT / "secret_text").load_manifest()
