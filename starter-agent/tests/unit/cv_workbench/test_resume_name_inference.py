from starter_agent.cv_workbench.jd_ingestion import infer_resume_name


def test_infer_resume_name_prefers_explicit_name_field() -> None:
    assert infer_resume_name("# 个人简历\n姓名：张伟\n\n教育经历", "candidate.pdf") == "张伟"


def test_infer_resume_name_uses_filename_when_document_has_no_safe_name() -> None:
    assert infer_resume_name("# 个人简历\n联系方式：someone@example.com\n\n教育经历", "张伟-前端.docx") == "张伟-前端"
