from starter_agent.cv_workbench.jd_analysis import extract_job_analysis


def test_extract_job_analysis_keeps_explicit_sections_separate() -> None:
    analysis = extract_job_analysis(
        """# AI 产品经理
## 岗位职责
- 负责 AI 产品需求分析与迭代
## 任职要求
- 熟悉 Python 和数据分析
- 3 年产品经验
## 加分项
- 有 LLM Agent 项目经验
"""
    )

    assert analysis == {
        "responsibilities": ["负责 AI 产品需求分析与迭代"],
        "required_skills": ["熟悉 Python 和数据分析", "3 年产品经验"],
        "preferred_skills": ["有 LLM Agent 项目经验"],
    }


def test_extract_job_analysis_does_not_keep_following_uppercase_section_in_requirements() -> None:
    analysis = extract_job_analysis(
        """REQUIREMENTS
Python and FastAPI
BENEFITS
Remote work and annual leave
"""
    )

    assert analysis["required_skills"] == ["Python and FastAPI"]
    assert analysis["responsibilities"] == []
