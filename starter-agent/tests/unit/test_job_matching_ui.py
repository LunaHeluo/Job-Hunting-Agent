from pathlib import Path


WEB = Path("frontend/web")
SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.js")))
CSS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.css")))


def test_job_candidate_requires_explicit_retention() -> None:
    create_at = SOURCE.index('/v1/workbench/job-candidates"')
    retain_at = SOURCE.index('/v1/workbench/job-candidates/retain"')
    assert create_at < retain_at
    for contract in ("评估并留存", "confirmed_authorized: true", "正在创建候选；尚未写入正式岗位", "不可变 JD 快照"):
        assert contract in SOURCE


def test_match_analysis_is_explainable_and_gaps_are_not_written() -> None:
    for contract in (
        "/v1/workbench/match-analyses/evaluate",
        "analysis.dimensions",
        "analysis.requirements",
        "ref.quote",
        "能力缺口：不会自动写入简历",
        "analysis.total_score",
    ):
        assert contract in SOURCE
    assert "requirement-missing" in CSS


def test_suggestion_decision_only_targets_draft() -> None:
    for contract in (
        "/suggestions/${encodeURIComponent(suggestion.suggestion_id)}/decisions",
        "/suggestion-candidates",
        "正在创建可恢复 Draft",
        'decision === "accept"',
        "建议已应用到 Draft；正式版本未改变",
        "尚无经证据验证的修改建议",
    ):
        assert contract in SOURCE


def test_ai_tailored_resume_is_traceable_reusable_and_batch_applied() -> None:
    for contract in (
        "/tailored-resume-candidates",
        "/v1/workbench/suggestions/batch-decisions",
        "AI 定制简历建议",
        "关联岗位要求",
        "证据摘录",
        "批量接受到 Draft",
        "正式版本未改变",
        'suggestion.change_type === "ai_tailor_v1"',
        'suggestion.status === "pending"',
    ):
        assert contract in SOURCE
    for contract in (
        ".tailored-suggestion-card",
        ".tailored-evidence",
        ".tailored-batch-bar",
    ):
        assert contract in CSS


def test_empty_ai_tailoring_result_explains_rejection_and_can_retry() -> None:
    for contract in (
        "generated.rejected_reasons",
        "自动修复生成 2 次",
        "模型没有返回候选改写",
        "新增了原文证据中没有的数字",
        "证据与简历区块不对应",
        "重新生成 AI 建议",
    ):
        assert contract in SOURCE
