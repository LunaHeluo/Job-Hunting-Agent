from starter_agent.cv_workbench.resume_profile import extract_resume_profile


def test_extract_resume_profile_extracts_sections_contacts_and_metrics() -> None:
    profile = extract_resume_profile(
        """# 张伟
138-0000-1234 · zhangwei@example.com

## 教育经历
浙江大学｜计算机科学｜2015.09 - 2019.06

## 实习经历
阿里巴巴｜前端工程师｜2020.01 - 至今
- 负责性能优化

## 项目经历
求职助手｜React｜2024.01 - 2024.06
- 实现简历匹配

## 技能
React、TypeScript、Next.js / CSS
""",
        "candidate.pdf",
    )

    assert profile["name"] == "张伟"
    assert profile["contact"] == {"phone": "138-0000-1234", "email": "zhangwei@example.com"}
    assert profile["metrics"] == {"education": 1, "experience": 1, "projects": 1, "skills": 4}
    assert profile["skills"] == ["React", "TypeScript", "Next.js", "CSS"]


def test_extract_resume_profile_uses_safe_fallback_for_sparse_content() -> None:
    profile = extract_resume_profile("# 个人简历\n\n## 技能\nPython", "李雷.docx")

    assert profile["name"] == "李雷"
    assert profile["metrics"] == {"education": 0, "experience": 0, "projects": 0, "skills": 1}


def test_unrelated_uppercase_headings_do_not_extend_education_section() -> None:
    profile = extract_resume_profile(
        """JUNXIAO HE
EDUCATION
The University of New South Wales  Sep 2023 - Oct 2025
Chengdu Jincheng College  Sep 2018 - Jun 2022
RESEARCH INTERESTS
Large language model agents
RESEARCH EXPERIENCE
Shenzhen University  Jun 2026 - Jul 2026
SELECTED PROJECTS
Job-Hunting AI Agent  Jun 2026 - Present
PROFESSIONAL EXPERIENCE
Xiaomi Corporation  Dec 2024 - Feb 2025
SKILLS
Python, FastAPI
""",
        "CV_JunxiaoHe.pdf",
    )

    assert profile["metrics"] == {"education": 2, "experience": 2, "projects": 1, "skills": 2}
