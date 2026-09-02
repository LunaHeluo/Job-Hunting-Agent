from starter_agent.job_research.company_attribution import infer_organic_company


def test_explicit_chinese_recruiting_title_attributes_company() -> None:
    result = infer_organic_company(
        "AI Agent开发工程师招聘_成都恒合实业有限责任公司招聘",
        "成都岗位，负责智能体研发。",
    )

    assert result.company == "成都恒合实业有限责任公司"
    assert result.source == "organic_explicit"
    assert result.confidence == "medium"


def test_platform_and_ambiguous_titles_do_not_invent_company() -> None:
    assert infer_organic_company("AI工程师招聘 - 猎聘", "热门职位").company == ""
    assert infer_organic_company("AI Engineer - Jobs", "Apply today").company == ""
    assert infer_organic_company("AI Agent开发工程师招聘", "成都岗位").company == ""
    assert infer_organic_company("AI工程师招聘_智联招聘", "热门职位").company == ""
    assert infer_organic_company("AI工程师招聘_猎聘网招聘", "热门职位").company == ""
