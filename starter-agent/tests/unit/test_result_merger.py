from __future__ import annotations

from dataclasses import replace

from starter_agent.delegation.results import DeterministicResultMerger, ValidatedEnvelope
from test_result_validator import _context, _envelope, _validator


def _validated(ref: str, **changes: object) -> ValidatedEnvelope:
    envelope = _envelope(**changes)
    result = _validator().validate(envelope, _context())
    assert result.accepted
    return replace(result.validated, envelope_ref=ref)


def test_merger_deduplicates_by_normalized_url_content_hash_and_preserves_sources_missing_conflicts() -> None:
    first = _validated("artifact:env:1", missing=("salary",))
    duplicate = _validated("artifact:env:2", output={**_envelope().output, "jobs": [{**_envelope().output["jobs"][0], "source_url": "https://EXAMPLE.test/jobs/1/"}]})
    merged = DeterministicResultMerger().merge(parent_run_id="parent:1", result_version=1, envelopes=(duplicate, first))
    assert len(merged.final_output["jobs"]) == 1
    assert tuple(merged.report.dedup_groups[0]["source_refs"]) == ("artifact:env:1", "artifact:env:2")
    assert merged.report.missing == ("salary",)
    assert merged.report.deterministic_order[0].startswith("candidate:")


def test_merger_is_order_independent_and_never_silently_overwrites_conflicting_facts() -> None:
    first = _validated("artifact:env:1")
    conflicting = _validated("artifact:env:2", output={**_envelope().output, "jobs": [{**_envelope().output["jobs"][0], "company": "Different Co"}]})
    merger = DeterministicResultMerger()
    left = merger.merge(parent_run_id="parent:1", result_version=1, envelopes=(first, conflicting))
    right = merger.merge(parent_run_id="parent:1", result_version=1, envelopes=(conflicting, first))
    assert left.report.final_output_hash == right.report.final_output_hash
    assert left.report.conflicts and left.final_output["conflicts"]


def test_merger_preserves_profile_matches_chunk_evidence_and_validation_counts() -> None:
    from dataclasses import replace
    profile = _validated("artifact:env:profile")
    profile = replace(profile, envelope=profile.envelope.model_copy(update={
        "output": {"matches": [{"requirement_ref": "job:1:req:python", "match_status": "matched", "evidence_strength": "strong", "evidence": [{"chunk_id": "chunk:1", "source_ref": "artifact:resume:1"}]}], "missing": ["job:1:req:kubernetes"], "conflicts": []},
        "evidence": ({"chunk_id": "chunk:1", "source_ref": "artifact:resume:1"},),
        "missing": ("job:1:req:kubernetes",),
    }))
    merged = DeterministicResultMerger().merge(parent_run_id="parent:1", result_version=1, envelopes=(profile,))
    assert merged.final_output["profile_matches"][0]["evidence"][0]["chunk_id"] == "chunk:1"
    assert merged.report.evidence_validation[0]["authorized_count"] == 1
    assert merged.report.source_validation[0]["accepted_count"] == 1
