from pathlib import Path


WEB = Path("frontend/web")
SOURCE = "\n".join(path.read_text(encoding="utf-8") for path in sorted(WEB.rglob("*.js")))


def test_delegated_research_is_controlled_by_authoritative_feature() -> None:
    for contract in (
        "home.features?.delegated_research !== true",
        "Release Gate 未通过",
        "自动调研关闭",
        "手工 JD 与单 URL 仍可使用",
        "/v1/workbench/research-runs",
    ):
        assert contract in SOURCE


def test_research_results_remain_candidates_until_explicit_retention() -> None:
    for contract in (
        "结果只进入候选栏",
        "candidate.evidence_level !== \"complete\"",
        "证据不完整，不能留存",
        "/retain`",
        "候选本身未被当作投递记录",
        "查看来源",
        "查看 JD",
    ):
        assert contract in SOURCE
