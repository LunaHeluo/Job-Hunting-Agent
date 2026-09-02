from __future__ import annotations

import json
from pathlib import Path
import subprocess


MODULE_URI = (Path.cwd() / "frontend/web/app/features/job-matching.js").as_uri()


def run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_v2_ui_helpers_prefer_upgraded_analysis_and_count_evidence() -> None:
    run_node(
        f"""
        import assert from "node:assert/strict";
        import {{ analysisEvidenceCount, requiresEvidenceUpgrade, selectRestorableAnalysis }} from {json.dumps(MODULE_URI)};

        const v2 = {{
          analysis_id: "ma_v2",
          status: "validated",
          rule_version: "match-rule.v2",
          resume_content_sha256: "resume-hash",
          job_content_sha256: "job-hash",
          created_at: "2026-08-26T09:00:00Z",
          requirements: [
            {{ evidence: [{{ chunk_id: "one" }}, {{ chunk_id: "two" }}] }},
            {{ evidence: [{{ chunk_id: "three" }}] }},
          ],
        }};
        const newerLegacy = {{
          ...v2,
          analysis_id: "ma_v1",
          rule_version: "match-rule.v1",
          created_at: "2026-08-26T10:00:00Z",
        }};

        assert.equal(selectRestorableAnalysis([newerLegacy, v2]).analysis_id, "ma_v2");
        assert.equal(analysisEvidenceCount(v2), 3);
        assert.equal(requiresEvidenceUpgrade(newerLegacy), true);
        assert.equal(requiresEvidenceUpgrade(v2), false);
        """
    )


def test_v1_upgrade_reuses_the_existing_evaluate_payload_contract() -> None:
    run_node(
        f"""
        import assert from "node:assert/strict";
        import {{ buildMatchEvaluationPayload }} from {json.dumps(MODULE_URI)};

        assert.deepEqual(
          buildMatchEvaluationPayload(
            "ws_demo",
            "rv_current",
            "js_current",
            {{ analysisId: "ma_new", operationId: "op_new" }},
          ),
          {{
            analysis_id: "ma_new",
            operation_id: "op_new",
            idempotency_key: "op_new",
            workspace_id: "ws_demo",
            resume_version_id: "rv_current",
            job_snapshot_id: "js_current",
          }},
        );
        """
    )
