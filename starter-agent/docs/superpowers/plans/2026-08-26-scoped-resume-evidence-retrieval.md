# Scoped Resume Evidence Retrieval Implementation Plan

> **For Codex:** Execute this plan in the current branch with the `superpowers:executing-plans` skill. Follow TDD for every behavior change, preserve unrelated dirty-worktree edits, and stage only the exact files changed by each task.

**Goal:** Replace the first-keyword-line match heuristic with deterministic, current-resume-scoped Top-K evidence retrieval so AI resume tailoring can safely use the actually relevant project blocks.

**Architecture:** A new `ResumeEvidenceSelector` wraps the existing `KnowledgeRetriever`, always scopes retrieval to the active `ResumeVersion.content.document_id`, aligns returned chunks to normalized Markdown blocks, and emits trusted chunk evidence. Matching becomes `match-rule.v2`; the API reuses a v2 analysis for unchanged resume/JD hashes, while historical v1 analyses remain viewable and receive a one-time upgrade path before tailoring.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite/FTS knowledge retrieval, pytest, browser-native JavaScript, source-contract UI tests.

---

## Working-tree constraints

- The repository already contains unrelated modified and untracked files. Never use `git add -A`, `git add .`, reset, checkout, clean, or broad formatters.
- Before every commit, run `git diff -- <exact paths>` and stage only the exact task files.
- `frontend/web/app/features/job-matching.js` and UI test files overlap with user work. Patch only the smallest functions and preserve all unrelated hunks.
- Use `apply_patch` for hand-written edits.
- Run pytest through `.venv\Scripts\python.exe -m pytest -p no:cacheprovider ...`. On Windows, request escalation when `tmp_path` cannot write under the sandbox.

## Task 1: Add deterministic, current-document evidence selection

**Files:**

- Create: `backend/src/starter_agent/cv_workbench/tailoring_evidence.py`
- Create: `tests/unit/cv_workbench/test_tailoring_evidence.py`

Task checklist:

- [ ] Add failing selector contract tests.
- [ ] Confirm the selector tests fail for the expected missing module.
- [ ] Implement scoped retrieval, persisted hash lookup, block alignment, and reranking.
- [ ] Confirm the selector tests pass.
- [ ] Review and commit only Task 1 files.

### Step 1: Write the failing selector contract tests

Create a recording retriever fixture whose `retrieve` method accepts the same keyword arguments as `KnowledgeRetriever.retrieve`. Build `RetrievalMatch` fixtures for:

- a React recommendation-system project from the active resume document;
- an AI research/course/finance-paper block from the same document;
- a higher-ranked React block from another resume document;
- a chunk whose preview does not map to any block in the current Markdown.

Add tests asserting:

```python
selection = selector.select(
    requirement_text="负责前端开发、系统联调和前端 AI 应用，熟悉 React",
    scope=KnowledgeScope(user_id="local-user", project_id="ws_demo"),
    knowledge_base_id=UUID(KB_ID),
    document_id=UUID(ACTIVE_DOCUMENT_ID),
    markdown=RESUME_MARKDOWN,
)

assert retriever.calls[0]["document_ids"] == [UUID(ACTIVE_DOCUMENT_ID)]
assert retriever.calls[0]["top_k"] == 5
assert selection.evidence[0].quote == REACT_PROJECT_BLOCK
assert all(ref.source_ref.startswith(f"knowledge-chunk://{KB_ID}/") for ref in selection.evidence)
assert selection.covered_terms >= {"react", "前端"}
```

Also assert that the other-document match and unmappable preview are excluded, evidence contains no duplicate Markdown block, and no more than three references are returned.

### Step 2: Run the selector tests and confirm RED

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/cv_workbench/test_tailoring_evidence.py -q
```

Expected: collection fails because `starter_agent.cv_workbench.tailoring_evidence` does not exist.

### Step 3: Implement the selector and its explicit retriever protocol

In `tailoring_evidence.py`, add immutable value types and a protocol:

```python
class ScopedResumeRetriever(Protocol):
    def retrieve(
        self,
        scope: KnowledgeScope,
        knowledge_base_id: UUID,
        question: str,
        *,
        top_k: int,
        document_ids: list[UUID] | None = None,
        document_types: list[str] | None = None,
        filenames: list[str] | None = None,
        versions: list[int] | None = None,
    ) -> list[RetrievalMatch]: ...

@dataclass(frozen=True)
class ResumeEvidenceSelection:
    query_terms: tuple[str, ...]
    covered_terms: frozenset[str]
    evidence: tuple[EvidenceReference, ...]
```

Implement:

```python
class ResumeEvidenceSelector:
    def __init__(self, *, retriever: ScopedResumeRetriever, chunk_reader: SQLiteKnowledgeStore): ...

    def select(
        self,
        requirement_text: str,
        *,
        scope: KnowledgeScope,
        knowledge_base_id: UUID,
        document_id: UUID,
        markdown: str,
    ) -> ResumeEvidenceSelection: ...
```

The implementation must:

1. normalize Markdown with the same `ResumeVersionService` block semantics or a small shared pure block-normalization helper;
2. extract stable English technology tokens and meaningful Chinese phrases while dropping requirement boilerplate;
3. call `retrieve(..., top_k=5, document_ids=[document_id], document_types=["resume"])` exactly once;
4. reject every returned match whose `document_id` differs from the requested document, even if a faulty retriever returns it;
5. read the matching `KnowledgeChunk` from `SQLiteKnowledgeStore` under the same scope and knowledge base so the evidence hash comes from persisted content rather than the preview;
6. align normalized chunk text to exactly one current Markdown block and reject ambiguous or missing alignments;
7. rerank by descending core-term coverage, then retrieval rank, then Markdown block order;
8. keep at most three distinct blocks;
9. emit `EvidenceReference(chunk_id=str(chunk.id), source_ref=f"knowledge-chunk://{knowledge_base_id}/{chunk.id}", content_sha256=chunk.content_sha256, quote=block_text[:1000])`.

No LLM calls, storage writes, global-workspace fallback, or fabricated hashes are allowed.

### Step 4: Run selector tests and confirm GREEN

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/cv_workbench/test_tailoring_evidence.py -q
```

Expected: all selector tests pass.

### Step 5: Commit Task 1

```powershell
git diff -- backend/src/starter_agent/cv_workbench/tailoring_evidence.py tests/unit/cv_workbench/test_tailoring_evidence.py
git add -- backend/src/starter_agent/cv_workbench/tailoring_evidence.py tests/unit/cv_workbench/test_tailoring_evidence.py
git commit -m "feat: select scoped resume evidence"
```

## Task 2: Replace first-line matching with `match-rule.v2`

**Files:**

- Modify: `backend/src/starter_agent/cv_workbench/matching.py`
- Modify: `tests/unit/cv_workbench/test_matching.py`
- Create: `tests/unit/cv_workbench/test_matching_requirements_v2.py`

Task checklist:

- [ ] Add failing v2 requirement tests.
- [ ] Confirm the v2 tests fail for the expected old behavior.
- [ ] Remove the duplicate heuristic and implement selector-backed verdicts.
- [ ] Upgrade and verify rule/validator versions.
- [ ] Confirm matching tests pass.
- [ ] Review and commit only Task 2 files.

### Step 1: Write failing v2 requirement tests

Add pure tests around a single exported requirement-builder function. Inject a fake selector returning controlled `ResumeEvidenceSelection` values and assert:

- core-term coverage `3 / 4` plus evidence produces `matched`;
- coverage `1 / 4` plus evidence produces `partial`;
- no aligned evidence produces `missing` even when the resume contains a raw keyword;
- missing/conflict requirements carry no evidence;
- the React block is used rather than the earlier AI-paper line;
- `matching.py` contains exactly one `def deterministic_requirements` or, if renamed, no obsolete first-line implementation and only one public builder definition.

The source-contract assertion should count definitions via `ast`, not fragile string matching.

### Step 2: Run the v2 tests and confirm RED

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/cv_workbench/test_matching_requirements_v2.py tests/unit/cv_workbench/test_matching.py -q
```

Expected: the v2 rule/version and injected selector behavior are absent.

### Step 3: Refactor the requirement builder

In `matching.py`:

- remove the duplicate `import re` and duplicate `deterministic_requirements()` implementation;
- set `RULE_VERSION = "match-rule.v2"` and `VALIDATOR_VERSION = "match-result-validator.v2"`;
- retain deterministic JD line parsing/category/importance behavior in one function;
- replace whole-resume keyword lookup with an explicit selector callback/dependency receiving each normalized requirement;
- compute core-term coverage from `selection.query_terms` and `selection.covered_terms`;
- use the confirmed verdict thresholds: evidence plus coverage `>= 0.60` is `matched`, evidence below `0.60` is `partial`, no trusted evidence is `missing`;
- attach the selector's one-to-three trusted evidence references without cloning a document-version reference;
- mention the selected terms and evidence count in the explanation, but do not expose unrelated resume content.

Preserve `CandidateRequirement`, scoring weights, immutable `MatchAnalysis`, evidence validation, binding, and stale-state behavior.

### Step 4: Make rule versions instance-explicit inside the operation pipeline

Ensure `_input_hash`, `_MatchValidator`, and `_MatchCommitter` all use v2 constants consistently. Add assertions to existing matching tests:

```python
assert analysis.rule_version == "match-rule.v2"
assert analysis.validator_version == "match-result-validator.v2"
```

Historical v1 rows remain untouched because no migration or update is run.

### Step 5: Run matching tests and confirm GREEN

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/cv_workbench/test_matching_requirements_v2.py tests/unit/cv_workbench/test_matching.py -q
```

Expected: all tests pass, including score, evidence downgrade, replay, stale history, and v2 rules.

### Step 6: Commit Task 2

```powershell
git diff -- backend/src/starter_agent/cv_workbench/matching.py tests/unit/cv_workbench/test_matching.py tests/unit/cv_workbench/test_matching_requirements_v2.py
git add -- backend/src/starter_agent/cv_workbench/matching.py tests/unit/cv_workbench/test_matching.py tests/unit/cv_workbench/test_matching_requirements_v2.py
git commit -m "feat: validate resume matches with v2 evidence"
```

## Task 3: Compose the selector and reuse unchanged v2 analyses

**Files:**

- Modify: `backend/src/starter_agent/cv_workbench/runtime.py`
- Modify: `backend/src/starter_agent/interfaces/workbench_api.py`
- Modify: `backend/src/starter_agent/cv_workbench/tailoring.py`
- Modify: `tests/integration/test_workbench_api.py`
- Modify: `tests/unit/cv_workbench/test_tailoring.py`

Task checklist:

- [ ] Add failing API reuse/scope/upgrade tests.
- [ ] Confirm integration tests fail for the expected missing behavior.
- [ ] Compose the selector in `WorkbenchRuntime`.
- [ ] Implement scoped evaluation and v2 hash reuse.
- [ ] Add the v1 tailoring safety guard.
- [ ] Confirm API and tailoring tests pass.
- [ ] Review and commit only Task 3 files.

### Step 1: Write failing API integration tests

Extend the workbench API fixture to ingest a resume whose Markdown contains, in order, an unrelated AI-paper block and a later React recommendation-system project. Ingest a frontend JD containing React, frontend development, integration, and frontend AI application requirements.

Add tests asserting:

1. `POST /v1/workbench/match-analyses/evaluate` creates `match-rule.v2` and the React project is the first positive evidence quote.
2. Every positive evidence source uses `knowledge-chunk://` and resolves to the active resume document.
3. Repeating evaluate with different proposed analysis/operation IDs but unchanged resume and JD hashes returns the original v2 `analysis_id` and marks the response `reused: true` without adding a second analysis.
4. A v1 fixture row remains byte-for-byte unchanged after v2 evaluation.
5. Changing the active resume version or job snapshot marks the old result stale and prevents hash-based reuse.
6. Calling the tailoring endpoint with a v1 analysis returns HTTP 409, code `tailoring_analysis_upgrade_required`, and recovery action `reanalyze_with_current_rule` before the provider is called.

### Step 2: Run the integration and tailoring tests and confirm RED

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/integration/test_workbench_api.py tests/unit/cv_workbench/test_tailoring.py -q
```

Expected: selector composition, v2 cache reuse, and the legacy-upgrade error do not yet exist.

### Step 3: Add the selector to `WorkbenchRuntime`

In `runtime.py`:

- add `evidence_selector: ResumeEvidenceSelector` to `WorkbenchRuntime`;
- construct `KnowledgeRetriever(knowledge, build_query_mapping_catalog(QueryMappingConfig()))` using the built-in deterministic mapping catalog;
- construct `ResumeEvidenceSelector(retriever=retriever, chunk_reader=knowledge)`;
- pass the selector into the runtime dataclass without changing existing public URLs or database schema.

If application settings are already available at the composition root, pass the configured `QueryMappingConfig` into `create_workbench_runtime`; otherwise retain the built-in default and cover that choice with a runtime test.

### Step 4: Scope evaluation and implement hash/rule reuse

In `evaluate_match`:

1. load the resume and job snapshot under the actor principal;
2. assert both entities belong to `body.workspace_id` before reading content or searching the cache;
3. require complete knowledge references on the current resume version;
4. search existing `MatchAnalysis` values for the same workspace, `resume_content_sha256`, `job_content_sha256`, `rule_version == "match-rule.v2"`, and status in `validated`/`partial`;
5. return the newest matching v2 result before retrieval, with JSON-only extra fields `reused: true` and `rule_upgrade_required: false`;
6. otherwise build `KnowledgeScope(user_id=subject, project_id=body.workspace_id)`, call the single v2 requirement builder with `runtime.evidence_selector`, then analyze;
7. return the created analysis plus `reused: false` and `rule_upgrade_required: false`.

Do not reuse by IDs alone, do not search other workspaces, and do not fall back to all knowledge documents.

### Step 5: Enforce v2 at the tailoring safety boundary

In `TailoredResumeService.generate_candidates`, immediately after workspace/status validation, reject any analysis whose `rule_version != "match-rule.v2"` with `TailoringServiceError("tailoring_analysis_upgrade_required")`.

In `workbench_api.py`, translate that specific code to HTTP 409 with recovery action `reanalyze_with_current_rule`; preserve the existing 503 provider-unavailable behavior and all existing provider-output guards.

### Step 6: Run API and tailoring tests and confirm GREEN

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/integration/test_workbench_api.py tests/unit/cv_workbench/test_tailoring.py -q
```

Expected: all tests pass, including active-document scope, React evidence selection, reuse, v1 immutability, stale behavior, and legacy tailoring rejection.

### Step 7: Commit Task 3

```powershell
git diff -- backend/src/starter_agent/cv_workbench/runtime.py backend/src/starter_agent/interfaces/workbench_api.py backend/src/starter_agent/cv_workbench/tailoring.py tests/integration/test_workbench_api.py tests/unit/cv_workbench/test_tailoring.py
git add -- backend/src/starter_agent/cv_workbench/runtime.py backend/src/starter_agent/interfaces/workbench_api.py backend/src/starter_agent/cv_workbench/tailoring.py tests/integration/test_workbench_api.py tests/unit/cv_workbench/test_tailoring.py
git commit -m "feat: reuse scoped v2 match analyses"
```

## Task 4: Add the one-time v1 upgrade flow and v2 evidence presentation

**Files:**

- Modify: `frontend/web/app/features/job-matching.js`
- Modify: `tests/unit/test_job_matching_ui.py`

Task checklist:

- [ ] Add failing UI contract tests.
- [ ] Confirm the UI tests fail for the expected missing upgrade flow.
- [ ] Add the shared evaluate helper, v1 upgrade panel, and v2 restoration preference.
- [ ] Confirm UI tests pass.
- [ ] Isolate and commit only intended UI hunks, or leave overlap uncommitted.

### Step 1: Write failing UI contract tests

Extend the source-contract test to require:

- v2 analyses are preferred over v1 analyses for the same `resume_content_sha256` and `job_content_sha256`;
- analysis metadata renders `match-rule.v2` and a total evidence count;
- v1 analysis tailoring renders the exact action label `一次性升级分析`;
- that action posts the existing `/v1/workbench/match-analyses/evaluate` payload using the v1 analysis's workspace, resume version, and job snapshot, then opens tailoring with the returned v2 analysis;
- refresh/main restoration never calls evaluate automatically;
- the normal v2 path still calls `/tailored-resume-candidates`.

### Step 2: Run the UI contract tests and confirm RED

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_job_matching_ui.py -q
```

Expected: v1 upgrade controls and v2-preferred restoration are absent.

### Step 3: Patch only the matching UI functions

In `job-matching.js`:

- add a small `evaluateMatch(workspaceId, resumeVersionId, jobSnapshotId)` helper and reuse it from both the chooser and upgrade button;
- keep generated operation and analysis IDs for cache-miss compatibility; accept a reused analysis ID from the server;
- in `renderAnalysis`, calculate the number of evidence refs across requirements and show `规则：match-rule.v2 · 已验证证据 N 条`;
- in `prepareTailoredResume`, detect v1 before draft creation/provider calls and render a non-destructive upgrade panel with `一次性升级分析` and `返回分析`;
- on upgrade click, call the evaluate helper once, update workbench context, and continue using the returned v2 analysis;
- in `renderMain`, group/compare by resume and job content hashes and prefer a validated/partial v2 analysis over v1, while preserving normal newest-first ordering across different inputs;
- never evaluate during page refresh or `renderMain`.

Preserve the existing Tailored Suggestion approval workflow, Draft-only mutation, and unrelated layout changes already present in the dirty file.

### Step 4: Run UI tests and confirm GREEN

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/test_job_matching_ui.py -q
```

Expected: all UI contracts pass.

### Step 5: Commit only the intended UI hunks

Review the entire diff and use interactive staging if unrelated user changes share the file:

```powershell
git diff -- frontend/web/app/features/job-matching.js tests/unit/test_job_matching_ui.py
git add -p -- frontend/web/app/features/job-matching.js tests/unit/test_job_matching_ui.py
git diff --cached --check
git commit -m "feat: upgrade legacy match evidence once"
```

If interactive staging cannot isolate the intended hunks safely, leave the UI changes uncommitted and report that explicitly rather than staging user-owned changes.

## Task 5: Prove the end-to-end safety and regression boundaries

**Files:**

- Modify: `tests/integration/test_cv_workbench_mvp_e2e.py`
- Modify: `tests/unit/cv_workbench/test_tailoring.py`

Task checklist:

- [ ] Add the React evidence acceptance regression.
- [ ] Run the focused acceptance suite.
- [ ] Run the broader CV Workbench suite.
- [ ] Run static safety and working-tree checks.
- [ ] Commit isolated acceptance-test changes.

### Step 1: Add the acceptance regression

Add one integration scenario using the screenshot's data semantics:

1. import a confirmed resume containing unrelated AI/course/finance content followed by a React recommendation-system project;
2. confirm a frontend JD snapshot;
3. evaluate and assert `match-rule.v2` chooses the React project evidence;
4. use a deterministic fake tailoring generator that cites that requirement/evidence/block;
5. assert at least one `ai_tailor_v1` Suggestion is created;
6. assert generation and suggestion creation leave the confirmed `ResumeVersion` content hash/revision unchanged;
7. accept selected suggestions into the Draft and assert only the Draft revision/content changes;
8. evaluate the unchanged inputs again and assert the original analysis is reused.

### Step 2: Run focused acceptance tests

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/integration/test_cv_workbench_mvp_e2e.py tests/integration/test_workbench_api.py tests/unit/cv_workbench/test_tailoring_evidence.py tests/unit/cv_workbench/test_matching_requirements_v2.py tests/unit/cv_workbench/test_matching.py tests/unit/cv_workbench/test_tailoring.py tests/unit/test_job_matching_ui.py -q
```

Expected: all focused evidence, matching, tailoring, API, UI, and Draft safety tests pass.

### Step 3: Run broader CV Workbench regression tests

Run:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/unit/cv_workbench tests/integration/test_workbench_api.py tests/integration/test_cv_workbench_mvp_e2e.py -q
```

Expected: all CV Workbench tests pass. Record any unrelated pre-existing failures separately with exact test names and evidence.

### Step 4: Run static safety checks

Run:

```powershell
rg -n "^def deterministic_requirements|match-rule\.v[12]|tailoring_analysis_upgrade_required|document_ids" backend/src/starter_agent/cv_workbench backend/src/starter_agent/interfaces/workbench_api.py
git diff --check
git status --short
```

Expected:

- one requirement builder definition;
- all new analyses use v2;
- the legacy tailoring guard exists;
- retrieval is explicitly document-scoped;
- no whitespace errors;
- only known user-owned changes and intentional task changes remain.

### Step 5: Commit the acceptance regression if it has isolated changes

```powershell
git diff -- tests/integration/test_cv_workbench_mvp_e2e.py tests/unit/cv_workbench/test_tailoring.py
git add -- tests/integration/test_cv_workbench_mvp_e2e.py tests/unit/cv_workbench/test_tailoring.py
git commit -m "test: cover evidence-backed resume tailoring"
```

If those test files already contain inseparable user-owned edits, leave them uncommitted and identify the exact files in the handoff.

## Final verification and handoff

Before claiming completion, invoke `superpowers:verification-before-completion` and rerun the focused acceptance command from Task 5 against the final working tree. Report:

- the exact passing test counts;
- any unrelated failures still present;
- whether the frontend overlap was safely committed or intentionally left uncommitted;
- the commits created;
- the manual browser flow: open a v1 analysis, click `AI 定制简历`, click `一次性升级分析`, confirm React evidence appears, generate suggestions, accept to Draft, refresh, and verify the v2 analysis is reused without rerunning matching.
