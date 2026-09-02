from starter_agent.job_research.candidates import (
    assess_job_candidate,
    rank_job_candidates,
)


def test_direct_apply_link_ranks_before_share_and_organic() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "Engineer",
                "url": "https://search.example.test/result",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "Engineer",
                "url": "https://jobs.example.test/share/42",
                "url_kind": "structured_share",
                "provider_position": 0,
            },
            {
                "title": "Engineer",
                "url": "https://employer.example.test/jobs/42",
                "url_kind": "structured_apply",
                "provider_position": 0,
            },
        ],
        limit=5,
    )

    assert [item.url_kind for item in ranked] == [
        "structured_apply",
        "structured_share",
        "organic",
    ]
    assert [item.confidence for item in ranked] == [1.0, 0.7, 0.4]


def test_candidate_ranking_does_not_require_known_domain() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "New Board Role",
                "url": "https://jobs.example-new.test/roles/42",
                "url_kind": "organic",
                "provider_position": 3,
            }
        ],
        limit=5,
    )

    assert ranked[0].url.endswith("/roles/42")


def test_candidate_ranking_deduplicates_and_rejects_non_http_urls() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "Role",
                "url": "https://jobs.example.test/42#details",
                "url_kind": "organic",
                "provider_position": 2,
            },
            {
                "title": "Role",
                "url": "https://jobs.example.test/42",
                "url_kind": "structured_apply",
                "provider_position": 1,
            },
            {
                "title": "Unsafe",
                "url": "javascript:alert(1)",
                "url_kind": "structured_apply",
                "provider_position": 0,
            },
        ],
        limit=5,
    )

    assert len(ranked) == 1
    assert ranked[0].url_kind == "structured_apply"


def test_candidate_ranking_interleaves_distinct_jobs_before_mirror_links() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer",
                "company": "Example A",
                "location": "Chengdu",
                "url": "https://board-one.example/jobs/a",
                "url_kind": "structured_apply",
                "provider_position": 0,
            },
            {
                "title": "AI Agent Engineer",
                "company": "Example A",
                "location": "Chengdu",
                "url": "https://board-two.example/jobs/a-copy",
                "url_kind": "structured_apply",
                "provider_position": 0,
            },
            {
                "title": "Python Platform Engineer",
                "company": "Example B",
                "location": "Chengdu",
                "url": "https://example-b.test/careers/42",
                "url_kind": "structured_apply",
                "provider_position": 1,
            },
            {
                "title": "LLM Engineer",
                "company": "Example C",
                "location": "Chengdu",
                "url": "https://example-c.test/roles/7",
                "url_kind": "structured_apply",
                "provider_position": 2,
            },
        ],
        limit=3,
    )

    assert [item.company for item in ranked] == [
        "Example A",
        "Example B",
        "Example C",
    ]


def test_probable_job_detail_ranks_before_search_collection_at_same_priority() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "15000+ Langchain jobs in Worldwide",
                "url": "https://example.test/jobs/langchain-jobs-worldwide",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "Senior Python Infrastructure Engineer",
                "url": "https://example.test/jobs/python-infrastructure-engineer-42",
                "url_kind": "organic",
                "provider_position": 1,
            },
        ],
        limit=2,
    )

    assert ranked[0].title == "Senior Python Infrastructure Engineer"


def test_linkedin_collection_without_space_before_jobs_is_rejected() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "1000+ 北京智能果技术有限公司jobs in Worldwide",
                "url": (
                    "https://www.linkedin.com/jobs/"
                    "%E5%8C%97%E4%BA%AC%E6%99%BA%E8%83%BD%E6%9E%9C"
                    "%E6%8A%80%E6%9C%AF%E6%9C%89%E9%99%90%E5%85%AC"
                    "%E5%8F%B8-jobs-worldwide"
                ),
                "url_kind": "organic",
                "provider_position": 0,
            }
        ],
        limit=10,
        location_aliases=("北京", "Beijing"),
    )

    assert ranked == ()


def test_jobs_worldwide_path_is_a_collection_even_with_job_like_title() -> None:
    assessment = assess_job_candidate(
        {
            "url_kind": "organic",
            "snippet": "Beijing AI Agent Engineer",
        },
        url="https://www.linkedin.com/jobs/example-company-jobs-worldwide",
        title="AI Agent Engineer",
        location_aliases=("北京", "Beijing"),
    )

    assert assessment.page_kind == "collection_page"
    assert assessment.reason_codes == ("collection_page_shape",)


def test_candidate_ranking_rejects_social_documents_and_paginated_job_lists() -> None:
    ranked = rank_job_candidates(
        [
            {
                "url": "https://www.instagram.com/p/example/",
                "title": "AI Agent hiring",
                "url_kind": "organic",
                "provider_position": 1,
            },
            {
                "url": "https://example.test/listing.pdf",
                "title": "AI Agent roles",
                "url_kind": "organic",
                "provider_position": 2,
            },
            {
                "url": "https://board.example/jobs/engineering?page=4",
                "title": "Engineering jobs",
                "url_kind": "organic",
                "provider_position": 3,
            },
            {
                "url": "https://board.example/search-asp-jobs-china",
                "title": "Asp jobs",
                "url_kind": "organic",
                "provider_position": 4,
            },
            {
                "url": "https://board.example/job/16873278/sap-architect/",
                "title": "Browse IT Jobs",
                "url_kind": "organic",
                "provider_position": 5,
            },
            {
                "url": "https://employer.example/careers/job/agent-engineer-1234",
                "title": "AI Agent Engineer",
                "url_kind": "organic",
                "provider_position": 6,
            },
        ],
        limit=5,
    )

    assert [item.url for item in ranked] == [
        "https://employer.example/careers/job/agent-engineer-1234"
    ]


def test_collection_pages_do_not_consume_the_candidate_limit() -> None:
    rows = [
        {
            "title": "1000+ 北京示例科技有限公司jobs in Worldwide",
            "url": "https://www.linkedin.com/jobs/example-jobs-worldwide",
            "url_kind": "organic",
            "provider_position": 0,
        },
        {
            "title": "AI jobs in Beijing",
            "url": "https://board.example.test/jobs/search",
            "url_kind": "organic",
            "provider_position": 1,
        },
        *[
            {
                "title": f"AI Agent Engineer {index}",
                "company": f"Employer {index}",
                "location": "Beijing",
                "url": f"https://employer-{index}.example.test/jobs/{index}",
                "url_kind": "organic",
                "provider_position": index + 2,
            }
            for index in range(11)
        ],
    ]

    ranked = rank_job_candidates(
        rows,
        limit=10,
        location_aliases=("北京", "Beijing"),
    )

    assert len(ranked) == 10
    assert all("linkedin.com" not in item.url for item in ranked)
    assert all("/jobs/search" not in item.url for item in ranked)


def test_candidate_ranking_rejects_observed_collection_search_and_spam_urls() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "成都AI岗位招聘",
                "url": "https://m.cd.example/job/aizhaopintopic/",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "成都ai工程师招聘信息",
                "url": "https://jobs.example/zhaopin/9b84146f8feadf8a03xy09m8EA~~/",
                "url_kind": "organic",
                "provider_position": 1,
            },
            {
                "title": "AI成都{外围联系电话}电话=微信13404032909提供工作室",
                "url": "https://social.example/jobs/ai-chengdu-jobs?position=1&pageNum=0",
                "url_kind": "organic",
                "provider_position": 2,
            },
            {
                "title": "AI Agent Platform Engineer",
                "company": "New Employer",
                "location": "Chengdu",
                "url": "https://new-board.example/roles/agent-platform-42",
                "url_kind": "organic",
                "provider_position": 3,
            },
        ],
        limit=5,
    )

    assert [item.url for item in ranked] == [
        "https://new-board.example/roles/agent-platform-42"
    ]


def test_candidate_ranking_rejects_live_city_and_category_collection_pages() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "上海招聘人工智能人才",
                "url": "https://www.liepin.com/city-sh/career/rengongzhineng/",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "人工智能招聘信息 - 上海前程无忧",
                "url": "https://msearch.51job.com/jobs/shanghai/we06/rengongzhineng/",
                "url_kind": "organic",
                "provider_position": 1,
            },
            {
                "title": "上海 AI Agent 工程师",
                "url": "https://m.liepin.com/job/1982974339.shtml",
                "url_kind": "organic",
                "provider_position": 2,
            },
        ],
        limit=5,
        location_aliases=("上海", "Shanghai"),
    )

    assert [item.url for item in ranked] == [
        "https://m.liepin.com/job/1982974339.shtml"
    ]


def test_candidate_ranking_keeps_unknown_direct_detail_without_domain_allowlist() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "机器学习工程师招聘",
                "company": "Example",
                "location": "杭州",
                "url": "https://unknown-employer.example/careers/openings/ml-engineer-7",
                "url_kind": "structured_apply",
                "provider_position": 0,
            }
        ],
        limit=5,
    )

    assert len(ranked) == 1
    assert ranked[0].company == "Example"


def test_candidate_ranking_classifies_detail_and_rejects_observed_collection_shapes() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer",
                "company": "Example",
                "location": "Shanghai",
                "url": "https://employer.test/careers/openings/agent-engineer-42",
                "url_kind": "structured_apply",
                "provider_position": 2,
            },
            {
                "title": "AI agent jobs in Shanghai",
                "url": "https://board.test/q-ai-agent-l-shanghai-jobs.html",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "We are hiring",
                "url": "https://community.test/posts/2264530973952763/",
                "result_type": "social_post",
                "url_kind": "organic",
                "provider_position": 1,
            },
        ],
        limit=5,
    )

    assert [item.title for item in ranked] == ["AI Agent Engineer"]
    assert ranked[0].page_kind == "job_detail_candidate"
    assert ranked[0].score > 0.8
    assert "structured_job_link" in ranked[0].reason_codes


def test_candidate_ranking_rejects_query_driven_mobile_jobs_list() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "Technical opening",
                "url": "https://jobs.example.test/m/jobs?q\\u003dAi+China",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "AI Agent Engineer",
                "url": "https://careers.example.test/job/agent-42",
                "url_kind": "organic",
                "provider_position": 1,
            },
        ],
        limit=5,
    )

    assert [item.url for item in ranked] == [
        "https://careers.example.test/job/agent-42"
    ]


def test_candidate_ranking_keeps_unknown_candidate_after_probable_detail() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "Agent Engineer",
                "company": "Example",
                "url": "https://employer.test/jobs/agent-engineer-42",
                "url_kind": "structured_apply",
                "provider_position": 1,
            },
            {
                "title": "Technical opening",
                "url": "https://unknown.test/openings/42",
                "url_kind": "organic",
                "provider_position": 0,
            },
        ],
        limit=5,
    )

    assert [item.page_kind for item in ranked] == [
        "job_detail_candidate",
        "unknown_candidate",
    ]


def test_candidate_ranking_merges_query_and_engine_provenance_by_url() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "智能体研发工程师",
                "company": "示例科技",
                "location": "深圳",
                "url": "https://careers.example.cn/jobs/42#details",
                "url_kind": "organic",
                "provider_position": 2,
                "matched_queries": ["深圳 AI Agent 工程师 招聘"],
                "search_engines": ["google"],
            },
            {
                "title": "智能体研发工程师",
                "company": "示例科技",
                "location": "深圳",
                "url": "https://careers.example.cn/jobs/42",
                "url_kind": "structured_apply",
                "provider_position": 0,
                "matched_queries": ["Shenzhen AI Agent Engineer jobs"],
                "search_engines": ["google_jobs"],
            },
        ],
        limit=5,
        location_aliases=("深圳", "Shenzhen"),
    )

    assert len(ranked) == 1
    assert ranked[0].url_kind == "structured_apply"
    assert ranked[0].matched_queries == (
        "深圳 AI Agent 工程师 招聘",
        "Shenzhen AI Agent Engineer jobs",
    )
    assert ranked[0].search_engines == ("google", "google_jobs")


def test_candidate_merge_keeps_strongest_company_attribution_for_same_url() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer",
                "company": "Search Guess",
                "company_source": "organic_explicit",
                "company_confidence": "medium",
                "url": "https://careers.example.test/jobs/42?ref=search",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "AI Agent Engineer",
                "company": "Official Employer",
                "company_source": "google_jobs",
                "company_confidence": "high",
                "url": "https://careers.example.test/jobs/42?ref=search",
                "url_kind": "organic",
                "provider_position": 1,
            },
        ],
        limit=5,
    )

    assert len(ranked) == 1
    assert ranked[0].company == "Official Employer"
    assert ranked[0].company_source == "google_jobs"
    assert ranked[0].company_confidence == "high"


def test_weak_organic_attribution_does_not_replace_legacy_structured_company() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer",
                "company": "Official Employer",
                "url": "https://careers.example.test/jobs/43",
                "url_kind": "structured_apply",
                "provider_position": 0,
            },
            {
                "title": "AI Agent Engineer",
                "company": "Search Guess",
                "company_source": "organic_explicit",
                "company_confidence": "medium",
                "url": "https://careers.example.test/jobs/43",
                "url_kind": "organic",
                "provider_position": 1,
            },
        ],
        limit=5,
    )

    assert len(ranked) == 1
    assert ranked[0].company == "Official Employer"
    assert ranked[0].url_kind == "structured_apply"


def test_company_attribution_has_meaningful_but_bounded_ranking_effect() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer Beijing",
                "company": "Official Employer",
                "company_source": "google_jobs",
                "company_confidence": "high",
                "url": "https://one.example.test/jobs/agent-engineer-42",
                "url_kind": "organic",
                "provider_position": 5,
                "snippet": "Responsibilities and requirements for this role.",
            },
            {
                "title": "AI Agent Engineer Beijing",
                "url": "https://two.example.test/jobs/agent-engineer-43",
                "url_kind": "organic",
                "provider_position": 0,
                "snippet": "Responsibilities and requirements for this role.",
            },
        ],
        limit=5,
        location_aliases=("Beijing",),
    )

    assert ranked[0].company == "Official Employer"
    assert "company_attribution_high" in ranked[0].reason_codes
    assert "company_unattributed" in ranked[1].reason_codes
    assert ranked[0].score - ranked[1].score >= 0.15


def test_local_chinese_detail_ranks_before_non_target_english_aggregator() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Engineer - MLabs",
                "location": "United States",
                "url": "https://builtin.com/job/ai-engineer/9866209",
                "url_kind": "structured_apply",
                "provider_position": 0,
                "snippet": "Build machine learning products.",
            },
            {
                "title": "智能体研发工程师（深圳）",
                "company": "示例科技",
                "location": "深圳",
                "url": "https://careers.example.cn/jobs/agent-42",
                "url_kind": "organic",
                "provider_position": 8,
                "snippet": "岗位职责：研发企业智能体。任职要求：熟悉 Python 和大模型。",
            },
        ],
        limit=5,
        location_aliases=("深圳", "Shenzhen"),
    )

    assert ranked[0].title == "智能体研发工程师（深圳）"
    assert "target_location_match" in ranked[0].reason_codes
    assert "chinese_title" in ranked[0].reason_codes
    assert "job_section_signals" in ranked[0].reason_codes
    assert "aggregator_signal" in ranked[1].reason_codes
    assert "non_target_location" in ranked[1].reason_codes


def test_employer_detail_ranks_before_aggregator_mirror() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer",
                "company": "Example",
                "location": "Munich",
                "url": "https://builtin.com/job/agent-engineer/7",
                "url_kind": "organic",
                "provider_position": 0,
            },
            {
                "title": "AI Agent Engineer",
                "company": "Example",
                "location": "Munich",
                "url": "https://jobs.example.com/careers/agent-engineer-7",
                "url_kind": "organic",
                "provider_position": 4,
            },
        ],
        limit=2,
        location_aliases=("München", "Munich"),
    )

    assert ranked[0].url == "https://jobs.example.com/careers/agent-engineer-7"
    assert "employer_detail_signal" in ranked[0].reason_codes


def test_arbitrary_concrete_detail_uses_general_readability_signals() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI智能体开发工程师",
                "company": "任意科技",
                "location": "杭州",
                "url": "https://talent.arbitrary-example.test/job/87231",
                "url_kind": "organic",
                "provider_position": 6,
                "snippet": (
                    "岗位职责：负责智能体应用研发。"
                    "任职要求：熟悉 Python、RAG 和大模型。"
                ),
            },
            {
                "title": "AI智能体开发工程师",
                "company": "任意科技",
                "location": "杭州",
                "url": "https://builtin.com/job/ai-agent-engineer/87231",
                "url_kind": "structured_apply",
                "provider_position": 0,
                "snippet": "AI engineering opportunity.",
            },
        ],
        limit=2,
        location_aliases=("杭州", "Hangzhou"),
    )

    assert ranked[0].url == "https://talent.arbitrary-example.test/job/87231"
    assert "concrete_job_detail" in ranked[0].reason_codes
    assert "section_rich_snippet" in ranked[0].reason_codes
    assert all(
        "tencent" not in code and "bytedance" not in code
        for candidate in ranked
        for code in candidate.reason_codes
    )


def test_wrong_location_detail_does_not_bypass_location_penalty() -> None:
    ranked = rank_job_candidates(
        [
            {
                "title": "AI Agent Engineer",
                "company": "Arbitrary",
                "location": "London",
                "url": "https://careers.arbitrary.test/job/42",
                "url_kind": "structured_apply",
                "provider_position": 0,
                "snippet": "Responsibilities and requirements for this role.",
            },
            {
                "title": "AI Agent Engineer 杭州",
                "company": "Local Example",
                "location": "杭州",
                "url": "https://jobs.local-example.test/job/77",
                "url_kind": "organic",
                "provider_position": 4,
                "snippet": "岗位职责：开发智能体。任职要求：熟悉 Python。",
            },
        ],
        limit=2,
        location_aliases=("杭州", "Hangzhou"),
    )

    assert ranked[0].company == "Local Example"
    assert "non_target_location" in ranked[1].reason_codes
