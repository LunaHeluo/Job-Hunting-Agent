from uuid import uuid4

import httpx
import pytest

from starter_agent.tools.base import ToolContext
from starter_agent.tools.builtin.job_search import (
    SearchJobsSerpApiTool,
    sanitize_url,
)
from starter_agent.tools.adapters.serpapi_location import LocationResolution


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        request = httpx.Request("GET", "https://serpapi.com/search.json")
        self.response = httpx.Response(status_code, request=request)

    def raise_for_status(self) -> None:
        if self.response.status_code >= 400:
            response = httpx.Response(
                self.response.status_code,
                request=self.response.request,
                json=self.payload,
            )
            response.raise_for_status()

    def json(self) -> object:
        return self.payload


class FakeClient:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url: str, *, params: dict, timeout: float):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeLocationResolver:
    def __init__(self, result: LocationResolution):
        self.result = result
        self.calls = []

    async def resolve(self, location: str) -> LocationResolution:
        self.calls.append(location)
        return self.result


class SequentialLocationResolver:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def resolve(self, location: str) -> LocationResolution:
        self.calls.append(location)
        return self.results.pop(0)


def resolved_location(value: str) -> FakeLocationResolver:
    return FakeLocationResolver(
        LocationResolution(
            requested=value,
            canonical_name=value,
            status="resolved",
        )
    )


def context() -> ToolContext:
    return ToolContext(session_id=uuid4(), turn_id=uuid4())


async def test_non_latin_location_uses_only_provider_validated_alias():
    resolver = SequentialLocationResolver(
        [
            LocationResolution("上海", None, "not_found"),
            LocationResolution(
                "Shanghai",
                "Shanghai,Shanghai,China",
                "resolved",
                city_alias="Shanghai",
                country_code="cn",
            ),
        ]
    )
    client = FakeClient(
        [
            FakeResponse({"jobs_results": []}),
            FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "上海 AI Agent 工程师",
                            "link": "https://employer.example/jobs/42",
                            "snippet": "岗位职责：构建智能体。任职要求：Python。",
                        }
                    ]
                }
            ),
        ]
        + [
            FakeResponse({"jobs_results": []}),
            FakeResponse({"organic_results": []}),
        ]
        * 11
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolver,
    )

    result = await tool.execute(
        {
            "query": "AI Agent engineer",
            "location": "上海",
            "location_alias": "Shanghai",
            "expand_location_aliases": True,
        },
        context(),
    )

    assert result.ok
    assert resolver.calls == ["上海", "Shanghai"]
    assert result.data["resolved_location"] == "Shanghai,Shanghai,China"
    assert result.data["location_aliases"] == ["上海", "Shanghai"]
    assert all(
        call["params"]["hl"]
        == ("zh" if call["params"]["engine"] == "google_jobs" else "zh-cn")
        for call in client.calls
    )
    assert all(call["params"]["gl"] == "cn" for call in client.calls)
    assert all(
        call["params"]["google_domain"] == "google.com"
        for call in client.calls
    )

async def test_missing_key_is_safe_and_does_not_call_provider() -> None:
    client = FakeClient([])
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("backup", None, "SERPAPI_API_KEY_BACKUP"),
        client=client,
    )

    result = await tool.execute({"query": "AI Agent jobs"}, context())

    assert not result.ok
    assert result.error_code == "missing_api_key"
    assert result.metadata == {
        "api_key_profile": "backup",
        "api_key_env": "SERPAPI_API_KEY_BACKUP",
    }
    assert client.calls == []


async def test_google_jobs_result_has_source_time_and_no_secret() -> None:
    secret = "unit-test-secret"
    client = FakeClient(
        [
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "AI Agent Engineer",
                            "company_name": "Example",
                            "location": "Sydney NSW",
                            "share_link": (
                                "https://jobs.example/1?token=leak&ref=public#fragment"
                            ),
                            "description": "Build agent systems",
                        }
                    ]
                }
            )
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", secret, "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Sydney"),
    )

    result = await tool.execute(
        {"query": "AI Agent engineer jobs", "location": "Sydney", "limit": 1},
        context(),
    )

    assert result.ok
    assert result.data["search_engine"] == "google_jobs"
    assert result.data["results"][0]["source"] == "serpapi_google_jobs"
    assert result.data["results"][0]["company"] == "Example"
    assert result.data["results"][0]["company_source"] == "google_jobs"
    assert result.data["results"][0]["company_confidence"] == "high"
    assert result.data["results"][0]["retrieved_at"]
    assert result.data["results"][0]["url"] == "https://jobs.example/1?ref=public"
    assert secret not in result.model_dump_json()


def test_organic_result_recovers_only_explicit_company_attribution() -> None:
    rows = SearchJobsSerpApiTool._parse_google(
        {
            "organic_results": [
                {
                    "title": "AI Agent开发工程师招聘_成都恒合实业有限责任公司招聘",
                    "link": "https://m.zhaopin.com/jobs/example.htm",
                    "snippet": "成都岗位，负责智能体研发。",
                },
                {
                    "title": "AI工程师招聘 - 猎聘",
                    "link": "https://www.liepin.com/job/example.shtml",
                    "snippet": "热门职位。",
                    "source": "猎聘",
                },
            ]
        },
        "2026-08-02T00:00:00Z",
    )

    assert rows[0]["company"] == "成都恒合实业有限责任公司"
    assert rows[0]["company_source"] == "organic_explicit"
    assert rows[0]["company_confidence"] == "medium"
    assert rows[1]["company"] == ""
    assert rows[1]["company_source"] == ""
    assert rows[1]["company_confidence"] == ""


async def test_google_jobs_prefers_all_direct_apply_links_before_share_link() -> None:
    client = FakeClient(
        [
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "Agent Engineer",
                            "company_name": "Example",
                            "location": "Berlin",
                            "share_link": "https://search.example.test/share/42",
                            "apply_options": [
                                {
                                    "title": "Employer",
                                    "link": "https://employer.example.test/jobs/42",
                                },
                                {
                                    "title": "Board",
                                    "link": "https://board.example-new.test/jobs/42",
                                },
                            ],
                        }
                    ]
                }
            )
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Berlin"),
    )

    result = await tool.execute(
        {"query": "Agent engineer", "location": "Berlin", "limit": 3},
        context(),
    )

    assert [item["url_kind"] for item in result.data["results"]] == [
        "structured_apply",
        "structured_apply",
        "structured_share",
    ]
    assert result.data["results"][0]["url"] == (
        "https://employer.example.test/jobs/42"
    )


async def test_sparse_google_jobs_results_are_augmented_with_organic_details() -> None:
    client = FakeClient(
        [
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "Agent Engineer",
                            "company_name": "Example",
                            "location": "Shanghai",
                            "apply_options": [
                                {
                                    "title": "Employer",
                                    "link": "https://employer.example.test/jobs/42",
                                }
                            ],
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "Agent Platform Engineer",
                            "link": "https://careers.example.test/jobs/84",
                            "snippet": "Build agent platforms",
                        }
                    ]
                }
            ),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Shanghai"),
    )

    result = await tool.execute(
        {"query": "AI Agent", "location": "Shanghai", "limit": 3},
        context(),
    )

    assert result.ok
    assert result.data["search_engine"] == "google_jobs+google"
    assert {item["url"] for item in result.data["results"]} == {
        "https://employer.example.test/jobs/42",
        "https://careers.example.test/jobs/84",
    }


async def test_sparse_share_links_are_augmented_with_organic_detail_pages() -> None:
    client = FakeClient(
        [
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "Agent Engineer",
                            "company_name": "Example",
                            "location": "Shanghai",
                            "share_link": "https://jobs.google.com/share/42",
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "Agent Engineer | Example Careers",
                            "link": "https://careers.example.test/jobs/42",
                            "snippet": "Responsibilities and requirements",
                        }
                    ]
                }
            ),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Shanghai"),
    )

    result = await tool.execute(
        {"query": "AI Agent", "location": "Shanghai", "limit": 3},
        context(),
    )

    assert result.ok
    assert result.data["search_engine"] == "google_jobs+google"
    assert {item["url"] for item in result.data["results"]} == {
        "https://jobs.google.com/share/42",
        "https://careers.example.test/jobs/42",
    }


async def test_search_uses_dynamically_resolved_canonical_location() -> None:
    client = FakeClient([FakeResponse({"jobs_results": []}), FakeResponse({"organic_results": []})])
    resolver = FakeLocationResolver(
        LocationResolution(
            requested="深圳",
            canonical_name="Shenzhen,Guangdong Province,China",
            status="resolved",
        )
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolver,
    )

    result = await tool.execute(
        {"query": "Python backend engineer", "location": "深圳"}, context()
    )

    assert result.error_code == "no_results"
    assert resolver.calls == ["深圳"]
    assert client.calls[0]["params"]["location"] == (
        "Shenzhen,Guangdong Province,China"
    )
    assert client.calls[0]["params"]["q"] == "Python backend engineer"


async def test_unsupported_location_degrades_to_query_without_location_parameter() -> None:
    client = FakeClient([FakeResponse({"jobs_results": []}), FakeResponse({"organic_results": []})])
    resolver = FakeLocationResolver(
        LocationResolution(
            requested="深圳",
            canonical_name=None,
            status="not_found",
        )
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolver,
    )

    result = await tool.execute(
        {"query": "Python backend engineer", "location": "深圳"}, context()
    )

    assert result.error_code == "no_results"
    assert client.calls[0]["params"]["q"] == "Python backend engineer 深圳"
    assert "location" not in client.calls[0]["params"]
    assert client.calls[1]["params"]["q"] == (
        'Python backend engineer 深圳 "responsibilities" "requirements" '
        "current opening apply"
    )
    assert result.metadata["location_resolution_status"] == "not_found"


async def test_provider_rejected_location_retries_once_without_location_parameter():
    client = FakeClient(
        [
            FakeResponse(
                {"error": "Unsupported location - location parameter."},
                status_code=400,
            ),
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "Python Backend Engineer",
                            "company_name": "Example",
                            "location": "Shenzhen",
                            "share_link": "https://jobs.example.com/backend",
                        }
                    ]
                }
            ),
            FakeResponse({"organic_results": []}),
        ]
    )
    resolver = FakeLocationResolver(
        LocationResolution(
            requested="深圳",
            canonical_name="Shenzhen,Guangdong Province,China",
            status="resolved",
        )
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolver,
    )

    result = await tool.execute(
        {"query": "Python backend engineer", "location": "深圳"}, context()
    )

    assert result.ok
    assert len(client.calls) == 3
    assert client.calls[0]["params"]["location"] == (
        "Shenzhen,Guangdong Province,China"
    )
    assert client.calls[1]["params"]["q"] == "Python backend engineer 深圳"
    assert "location" not in client.calls[1]["params"]
    assert result.metadata["location_fallback_used"] is True


async def test_http_400_keeps_bounded_safe_provider_error_classification():
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient(
            [FakeResponse({"error": "Malformed search request"}, status_code=400)]
        ),
    )

    result = await tool.execute({"query": "Python backend engineer"}, context())

    assert result.error_code == "invalid_search_request"
    assert result.metadata["provider_error_code"] == "invalid_request"
    assert result.metadata["provider_error_summary"] == "Malformed search request"


async def test_empty_google_jobs_falls_back_to_google() -> None:
    client = FakeClient(
        [
            FakeResponse({"jobs_results": []}),
            FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "AI Engineer - Sydney",
                            "link": "https://example.com/careers/1",
                            "snippet": "Agent engineering role",
                        }
                    ]
                }
            ),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Sydney"),
    )

    result = await tool.execute(
        {"query": "AI Agent engineer jobs", "location": "Sydney", "limit": 3},
        context(),
    )

    assert result.ok
    assert len(client.calls) == 2
    assert client.calls[0]["params"]["engine"] == "google_jobs"
    assert client.calls[1]["params"]["engine"] == "google"
    assert client.calls[1]["params"]["q"] == (
        'AI Agent engineer jobs Sydney "responsibilities" "requirements" '
        "current opening apply"
    )
    assert result.data["search_engine"] == "google"
    assert result.data["results"][0]["source"] == "serpapi_google"


async def test_unusable_google_jobs_candidates_fall_back_to_google() -> None:
    client = FakeClient(
        [
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "成都ai工程师招聘信息",
                            "company_name": "",
                            "location": "成都",
                            "apply_options": [
                                {
                                    "title": "Search page",
                                    "link": "https://board.example/zhaopin/ai-engineer/",
                                }
                            ],
                            "share_link": (
                                "https://social.example/jobs/ai-chengdu-jobs"
                                "?position=1&pageNum=0"
                            ),
                        }
                    ]
                }
            ),
            FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "AI Agent Platform Engineer",
                            "link": "https://employer.example/careers/openings/agent-42",
                            "snippet": "Build and operate production agent systems.",
                        }
                    ]
                }
            ),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Chengdu"),
    )

    result = await tool.execute(
        {"query": "AI Agent engineer", "location": "Chengdu", "limit": 5},
        context(),
    )

    assert result.ok
    assert len(client.calls) == 2
    assert result.data["search_engine"] == "google"
    assert [item["url"] for item in result.data["results"]] == [
        "https://employer.example/careers/openings/agent-42"
    ]


async def test_google_jobs_no_results_error_falls_back_to_google() -> None:
    client = FakeClient(
        [
            FakeResponse(
                {"error": "Google hasn't returned any results for this query."}
            ),
            FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "AI Agent Engineer Sydney",
                            "link": "https://example.com/jobs/agent",
                            "snippet": "Sydney role",
                        }
                    ]
                }
            ),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Sydney"),
    )

    result = await tool.execute(
        {"query": "AI Agent engineer jobs", "location": "Sydney"}, context()
    )

    assert result.ok
    assert len(client.calls) == 2
    assert result.data["search_engine"] == "google"


async def test_both_searches_empty_returns_no_results() -> None:
    client = FakeClient(
        [FakeResponse({"jobs_results": []}), FakeResponse({"organic_results": []})]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
    )

    result = await tool.execute({"query": "AI Agent jobs"}, context())

    assert not result.ok
    assert result.error_code == "no_results"
    assert len(client.calls) == 2


@pytest.mark.parametrize("limit", [0, 11, -1, True, "5"])
async def test_invalid_limit_is_rejected(limit) -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient([]),
    )
    result = await tool.execute(
        {"query": "AI Agent jobs", "limit": limit}, context()
    )
    assert result.error_code == "invalid_arguments"


async def test_multi_query_search_uses_engine_compatible_localization() -> None:
    client = FakeClient(
        [
            FakeResponse({"jobs_results": []}),
            FakeResponse({"organic_results": [{
                "title": "智能体研发工程师",
                "link": "https://employer.example/jobs/agent-1",
                "snippet": "岗位职责：研发智能体。任职要求：熟悉 Python。",
            }]}),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        location_resolver=resolved_location("Shanghai, China"),
    )

    result = await tool.execute(
        {
            "query": "AI Agent",
            "query_variants": ["上海 AI Agent 工程师 招聘"],
            "location": "Shanghai, China",
            "hl": "zh-cn",
            "gl": "cn",
            "google_domain": "google.com",
            "limit": 5,
        },
        context(),
    )

    assert result.ok
    assert len(client.calls) == 2
    assert {item["params"]["engine"] for item in client.calls} == {
        "google_jobs", "google"
    }
    assert all(item["params"]["location"] == "Shanghai, China" for item in client.calls)
    by_engine = {item["params"]["engine"]: item["params"] for item in client.calls}
    assert by_engine["google_jobs"]["hl"] == "zh"
    assert by_engine["google"]["hl"] == "zh-cn"
    assert all(item["params"]["gl"] == "cn" for item in client.calls)
    assert all(item["params"]["google_domain"] == "google.com" for item in client.calls)
    assert result.data["planned_queries"] == ["上海 AI Agent 工程师 招聘"]
    assert result.data["request_count"] == 2
    assert result.data["raw_result_count"] == 1
    assert result.data["results"][0]["matched_queries"] == [
        "上海 AI Agent 工程师 招聘"
    ]
    assert result.data["results"][0]["search_engines"] == ["google"]


async def test_ranking_diagnostics_keep_top_ten_independent_of_result_limit() -> None:
    organic = [
        {
            "title": f"AI Agent Engineer {index}",
            "link": f"https://employer-{index}.example/jobs/{index}",
            "snippet": "Responsibilities: build agents. Requirements: Python.",
        }
        for index in range(6)
    ]
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient([
            FakeResponse({"jobs_results": []}),
            FakeResponse({"organic_results": organic}),
        ]),
    )

    result = await tool.execute(
        {
            "query": "AI Agent",
            "query_variants": ["Munich AI Agent Engineer jobs"],
            "limit": 1,
        },
        context(),
    )

    assert len(result.data["results"]) == 1
    assert len(result.data["ranking_diagnostics"]) == 6


async def test_multi_query_diagnostics_report_general_query_families() -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient(
            [
                FakeResponse({"jobs_results": []}),
                FakeResponse({
                    "organic_results": [{
                        "title": "AI Agent Engineer",
                        "link": "https://careers.example.test/job/41",
                        "snippet": "AI Agent engineering role in Hangzhou.",
                    }]
                }),
                FakeResponse({"jobs_results": []}),
                FakeResponse({
                    "organic_results": [{
                        "title": "AI智能体工程师",
                        "link": "https://talent.example.test/job/42",
                        "snippet": "岗位职责：开发智能体。任职要求：熟悉 Python。",
                    }]
                }),
            ]
        ),
    )

    result = await tool.execute(
        {
            "query": "AI Agent",
            "query_variants": [
                "Hangzhou AI Agent Engineer jobs",
                "杭州 AI Agent 岗位职责 任职要求",
            ],
            "limit": 5,
        },
        context(),
    )

    assert result.ok
    assert result.data["query_family_counts"] == {
        "detail_evidence": 1,
        "role_recall": 1,
    }
    families = {
        family
        for item in result.data["ranking_diagnostics"]
        for family in item["matched_query_families"]
    }
    assert families == {"detail_evidence", "role_recall"}


async def test_search_reports_collection_rows_filtered_before_ranking() -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient(
            [
                FakeResponse(
                    {
                        "jobs_results": [
                            {
                                "title": "1000+ 北京示例科技有限公司jobs in Worldwide",
                                "share_link": (
                                    "https://www.linkedin.com/jobs/"
                                    "example-jobs-worldwide"
                                ),
                            }
                        ]
                    }
                ),
                FakeResponse(
                    {
                        "organic_results": [
                            {
                                "title": "AI jobs in Beijing",
                                "link": "https://board.example.test/jobs/search",
                            },
                            {
                                "title": "AI Agent Engineer",
                                "link": (
                                    "https://employer.example.test/jobs/agent-1"
                                ),
                                "snippet": (
                                    "Responsibilities: build agents. "
                                    "Requirements: Python."
                                ),
                            },
                        ]
                    }
                ),
            ]
        ),
    )

    result = await tool.execute(
        {
            "query": "AI Agent",
            "query_variants": ["Beijing AI Agent Engineer jobs"],
            "limit": 10,
        },
        context(),
    )

    assert result.ok
    assert result.data["filtered_collection_count"] == 2
    assert [item["url"] for item in result.data["results"]] == [
        "https://employer.example.test/jobs/agent-1"
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "AI Agent", "query_variants": ["q"] * 13},
        {"query": "AI Agent", "query_variants": []},
        {"query": "AI Agent", "hl": "bad locale"},
        {"query": "AI Agent", "gl": "china"},
        {"query": "AI Agent", "google_domain": "evil.example"},
    ],
)
async def test_multi_query_localization_arguments_are_bounded(arguments) -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient([]),
    )

    result = await tool.execute(arguments, context())

    assert result.error_code == "invalid_arguments"


@pytest.mark.parametrize(
    ("status", "error_code"),
    [(401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited")],
)
async def test_http_errors_are_classified(status, error_code) -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient([FakeResponse({}, status_code=status)]),
    )
    result = await tool.execute({"query": "AI Agent jobs"}, context())
    assert result.error_code == error_code


async def test_provider_quota_error_is_classified() -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient([FakeResponse({"error": "Your plan has no credits"})]),
    )
    result = await tool.execute({"query": "AI Agent jobs"}, context())
    assert result.error_code == "quota_exceeded"


async def test_transient_timeout_is_retried_then_succeeds() -> None:
    request = httpx.Request("GET", "https://serpapi.com/search.json")
    client = FakeClient(
        [
            httpx.ReadTimeout("temporary timeout", request=request),
            FakeResponse(
                {
                    "jobs_results": [
                        {
                            "title": "AI Engineer",
                            "company_name": "Example",
                            "location": "Shanghai",
                            "share_link": "https://example.com/job/1",
                        }
                    ]
                }
            ),
            FakeResponse({"organic_results": []}),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        max_retries=1,
        retry_backoff_seconds=0,
        location_resolver=resolved_location("Shanghai"),
    )

    result = await tool.execute(
        {"query": "AI jobs", "location": "Shanghai"}, context()
    )

    assert result.ok
    assert len(client.calls) == 3


async def test_connection_failure_has_actionable_error_after_retry() -> None:
    request = httpx.Request("GET", "https://serpapi.com/search.json")
    client = FakeClient(
        [
            httpx.ConnectError("connection failed", request=request),
            httpx.ConnectError("connection failed", request=request),
        ]
    )
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=client,
        max_retries=1,
        retry_backoff_seconds=0,
    )

    result = await tool.execute({"query": "AI jobs"}, context())

    assert result.error_code == "search_connection_failed"
    assert result.retryable is True
    assert result.metadata["failure_type"] == "connection_failed"
    assert result.metadata["attempts"] == 2
    assert "网络" in result.display


async def test_invalid_provider_response_is_classified() -> None:
    tool = SearchJobsSerpApiTool(
        key_resolver=lambda: ("primary", "secret", "SERPAPI_API_KEY"),
        client=FakeClient([FakeResponse(["not", "an", "object"])]),
        retry_backoff_seconds=0,
    )

    result = await tool.execute({"query": "AI jobs"}, context())

    assert result.error_code == "invalid_response"
    assert result.metadata["failure_type"] == "invalid_response"


def test_sanitize_url_rejects_non_http_and_removes_sensitive_values() -> None:
    assert sanitize_url("javascript:alert(1)") == ""
    assert (
        sanitize_url("https://example.test/job?api_key=x&ref=y#secret")
        == "https://example.test/job?ref=y"
    )
