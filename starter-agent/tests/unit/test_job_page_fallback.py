import pytest

from starter_agent.job_research.candidates import JobCandidate
from starter_agent.job_research.fallback import JobPageFallback
from starter_agent.tools.adapters.safe_web_fetcher import FetchFailure, FetchedPage


URL = "https://jobs.example.test/42"


def candidate(snippet: str = "") -> JobCandidate:
    return JobCandidate(
        url=URL,
        title="Staff Software Engineer",
        company="Example",
        company_source="google_jobs",
        company_confidence="high",
        location="Beijing",
        snippet=snippet,
        url_kind="organic",
        confidence=0.4,
        provider_position=0,
    )


class HtmlFetcher:
    async def fetch(self, _url: str) -> FetchedPage:
        return FetchedPage(
            source_url=URL,
            final_url=URL,
            status_code=200,
            content_type="text/html",
            content_sha256="a" * 64,
            text='''<script type="application/ld+json">{
              "@type":"JobPosting", "title":"Staff Software Engineer",
              "hiringOrganization":{"name":"Example"},
              "jobLocation":{"address":{"addressLocality":"Beijing"}},
              "description":"<h2>Responsibilities</h2><p>Build reliable AI systems.</p><h2>Requirements</h2><p>Expert Python experience.</p>"
            }</script>''',
        )


class FailingFetcher:
    async def fetch(self, _url: str) -> FetchedPage:
        raise FetchFailure("access_blocked_403", "HTTP 403 access denied")


class ChallengePageFetcher:
    async def fetch(self, _url: str) -> FetchedPage:
        return FetchedPage(
            source_url=URL,
            final_url=URL,
            status_code=200,
            content_type="text/html",
            content_sha256="b" * 64,
            text=(
                "<html><head><title>Security Verification</title></head>"
                "<body>请完成验证码后继续访问</body></html>"
            ),
        )


@pytest.mark.asyncio
async def test_http_json_ld_fallback_returns_verified_job() -> None:
    result = await JobPageFallback(HtmlFetcher()).retrieve(candidate())  # type: ignore[arg-type]
    assert result.jobs[0]["title"] == "Staff Software Engineer"
    assert result.jobs[0]["retrieval_method"] == "http_json_ld"
    assert result.jobs[0]["source_url"] == URL
    assert result.jobs[0]["company_source"] == "page_json_ld"
    assert result.jobs[0]["company_confidence"] == "high"


@pytest.mark.asyncio
async def test_failed_http_preserves_search_snippet_as_partial() -> None:
    result = await JobPageFallback(FailingFetcher()).retrieve(  # type: ignore[arg-type]
        candidate("Build AI agents with Python and distributed systems.")
    )
    assert result.jobs == ()
    assert result.partial_jobs[0]["retrieval_method"] == "search_snippet"
    assert result.partial_jobs[0]["validation_state"] == "partial_verified"
    assert result.partial_jobs[0]["company"] == "Example"
    assert result.partial_jobs[0]["company_source"] == "google_jobs"
    assert result.partial_jobs[0]["company_confidence"] == "high"
    assert result.failures[0].error_code == "access_blocked_403"


@pytest.mark.asyncio
async def test_http_200_challenge_preserves_snippet_and_reports_access_block() -> None:
    result = await JobPageFallback(ChallengePageFetcher()).retrieve(  # type: ignore[arg-type]
        candidate("成都招聘 AI Agent 开发工程师，要求熟悉 Python。")
    )

    assert result.partial_jobs[0]["retrieval_method"] == "search_snippet"
    assert result.failures[0].error_code == "access_blocked_challenge"
