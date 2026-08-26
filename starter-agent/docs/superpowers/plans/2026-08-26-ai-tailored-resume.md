# AI Tailored Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the workbench's fixed weak-verb “AI 定制简历” behavior with provider-generated, evidence-bound resume suggestions that are applied only to a recoverable Draft.

**Architecture:** Add an async TailoredResumeService between MatchAnalysis/SuggestionService and the configured Starter Agent Provider. The service converts only positive match evidence into a constrained generation request, validates the provider's JSON deterministically, and persists valid output as existing Suggestion objects; the HTTP and browser layers expose generation and batch approval without changing immutable versions.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy/SQLite stores, Starter Agent Provider abstraction, vanilla browser JavaScript, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-ai-tailored-resume-design.md`

## Global Constraints

- Do not copy `resume_agent` SQLite tables, Chroma collections, `content_json`, or its standalone LLM client.
- Never use `missing` or `conflict` requirements to produce resume claims.
- Generation creates Suggestion records against a ResumeDraft; it never mutates or confirms a ResumeVersion.
- Every accepted generated claim must retain MatchAnalysis requirement IDs and EvidenceReference values.
- All reads and writes remain principal- and workspace-scoped.
- No model or parsing failure may change Draft content.
- Preserve the user's existing uncommitted work. Before staging any already-modified file, inspect its pre-task diff; do not commit unrelated hunks.
- Use `D:\code\C\Personal-Agent\starter-agent\.venv\Scripts\python.exe -m pytest` for local tests.

---

### Task 1: Provider-backed tailoring contracts and JSON generation

**Files:**
- Create: `backend/src/starter_agent/cv_workbench/tailoring.py`
- Test: `tests/unit/cv_workbench/test_tailoring.py`

**Interfaces:**
- Consumes: `starter_agent.providers.base.Provider.complete(messages, model, tools=[])` and `starter_agent.domain.models.Message`.
- Produces: `TailoringGenerationRequest`, `ReflectionResult`, `GeneratedTailoringCandidate`, `TailoringGenerationResult`, `TailoredResumeGenerator`, and `ProviderTailoredResumeGenerator.generate()`.

- [ ] **Step 1: Write failing provider adapter tests**

```python
@pytest.mark.asyncio
async def test_provider_generator_runs_reflection_then_writer() -> None:
    provider = SequencedProvider([
        '{"issues_found":0,"issues":[],"notes":"证据可用"}',
        '{"candidates":[{"block_id":"b1","proposed_text":"交付 Python API",'
        '"reason":"贴合岗位","requirement_ids":["req_1"],'
        '"evidence_ids":["ev_1"],"risk":"请确认职责边界"}]}',
    ])
    generator = ProviderTailoredResumeGenerator(
        provider_resolver=lambda _name: provider,
        provider_name="fake",
        model="fake-model",
    )

    result = await generator.generate(generation_request())

    assert len(provider.calls) == 2
    assert result.reflection.notes == "证据可用"
    assert result.candidates[0].block_id == "b1"


@pytest.mark.asyncio
async def test_provider_generator_rejects_non_json_writer_output() -> None:
    generator = generator_with_responses([
        '{"issues_found":0,"issues":[],"notes":"ok"}',
        "not-json",
    ])
    with pytest.raises(TailoringServiceError, match="tailoring_output_invalid"):
        await generator.generate(generation_request())
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench/test_tailoring.py -q
```

Expected: collection fails because `starter_agent.cv_workbench.tailoring` does not exist.

- [ ] **Step 3: Implement the contracts, parser, prompts, and provider adapter**

Create frozen Pydantic request/result models with these stable fields:

```python
PROMPT_VERSION = "tailored-resume-v1"

class TailoringBlock(BaseModel):
    model_config = ConfigDict(frozen=True)
    block_id: str
    original_text: str

class TailoringEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)
    evidence_id: str
    requirement_id: str
    quote: str

class TailoringGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    analysis_id: str
    draft_id: str
    draft_revision: int
    requirements: tuple[dict[str, str], ...]
    blocks: tuple[TailoringBlock, ...]
    evidence: tuple[TailoringEvidence, ...]

class GeneratedTailoringCandidate(BaseModel):
    block_id: str
    proposed_text: str = Field(min_length=1, max_length=10_000)
    reason: str = Field(min_length=1, max_length=2_000)
    requirement_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    risk: str | None = Field(default=None, max_length=2_000)

class ReflectionResult(BaseModel):
    issues_found: int = Field(default=0, ge=0)
    issues: tuple[dict[str, str], ...] = ()
    notes: str = ""

class TailoringGenerationResult(BaseModel):
    reflection: ReflectionResult
    candidates: tuple[GeneratedTailoringCandidate, ...] = ()
```

Implement `_parse_json_object()` with fenced-JSON support, two provider calls (reflection then writer), `tools=[]`, and `max_output_tokens=4_000`. The writer system prompt must state that only supplied evidence is allowed, new metrics/skills/roles are forbidden, and output is JSON.

- [ ] **Step 4: Run focused tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench/test_tailoring.py -q
```

Expected: provider adapter tests pass with no network access.

- [ ] **Step 5: Record a safe checkpoint**

```powershell
git diff --check -- backend/src/starter_agent/cv_workbench/tailoring.py tests/unit/cv_workbench/test_tailoring.py
git add -- backend/src/starter_agent/cv_workbench/tailoring.py tests/unit/cv_workbench/test_tailoring.py
git commit -m "feat: add tailored resume generation adapter"
```

Commit only these new files.

---

### Task 2: Evidence-bound TailoredResumeService

**Files:**
- Modify: `backend/src/starter_agent/cv_workbench/tailoring.py`
- Modify: `backend/src/starter_agent/cv_workbench/suggestions.py:62-353`
- Test: `tests/unit/cv_workbench/test_tailoring.py`

**Interfaces:**
- Consumes: Task 1 generation contracts, `ResumeVersionService.content`, `ResumeMarkdownNormalizer`, `MatchAnalysis`, `ResumeDraft`, and `SuggestionService.create()`.
- Produces: `TailoringCommand`, `TailoringResult`, and `TailoredResumeService.generate_candidates(command, principal)`.

- [ ] **Step 1: Write failing domain tests for evidence filtering and persistence**

```python
@pytest.mark.asyncio
async def test_tailoring_creates_only_positive_evidence_bound_suggestions(tmp_path) -> None:
    service, fake, analysis, draft, store = setup_tailoring_service(tmp_path)
    fake.result = TailoringGenerationResult(
        reflection=ReflectionResult(notes="ok"),
        candidates=(GeneratedTailoringCandidate(
            block_id=aligned_block_id(service, draft),
            proposed_text="交付 Python API",
            reason="贴合岗位要求",
            requirement_ids=(analysis.requirements[0].requirement_id,),
            evidence_ids=("ev_1",),
            risk="请确认职责边界",
        ),),
    )

    result = await service.generate_candidates(
        TailoringCommand("ws_demo", analysis.analysis_id, draft.draft_id),
        principal=PRINCIPAL,
    )

    assert len(result.items) == 1
    assert result.items[0].change_type == "ai_tailor_v1"
    assert result.items[0].resume_evidence == analysis.requirements[0].evidence
    assert store.get(ResumeDraft, draft.draft_id, principal=PRINCIPAL).revision == 1


@pytest.mark.asyncio
async def test_tailoring_rejects_new_numbers_and_unknown_evidence(tmp_path) -> None:
    service, fake, analysis, draft, _store = setup_tailoring_service(tmp_path)
    fake.result = result_for(draft, proposed_text="性能提升 99%", evidence_ids=("unknown",))

    result = await service.generate_candidates(
        TailoringCommand("ws_demo", analysis.analysis_id, draft.draft_id),
        principal=PRINCIPAL,
    )

    assert result.items == ()
    assert set(result.rejected_reasons) == {"unknown_evidence", "new_numeric_claim"}
```

Add separate tests for base-version mismatch, no positive evidence, duplicate Block output, and idempotent retry (`fake.call_count == 1`, `result.reused is True`).

- [ ] **Step 2: Run the new service tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench/test_tailoring.py -q
```

Expected: tests fail because `TailoredResumeService` and `TailoringCommand` are missing.

- [ ] **Step 3: Implement deterministic preparation and validation**

Implement `TailoredResumeService` with this constructor and method:

```python
class TailoredResumeService:
    def __init__(self, *, store, versions, suggestions, generator): ...

    async def generate_candidates(
        self,
        command: TailoringCommand,
        *,
        principal: str,
    ) -> TailoringResult: ...
```

`TailoringResult` is a frozen Pydantic model with `draft_id: str`,
`draft_revision: int`, `items: tuple[Suggestion, ...]`,
`reflection: ReflectionResult`, `reused: bool`, and
`rejected_reasons: tuple[str, ...]`.

Required behavior:

1. Load analysis and draft through the scoped store and assert workspace membership.
2. Require analysis status `validated` or `partial` and matching `draft.base_version_id`.
3. Normalize Draft Markdown and align each positive EvidenceReference quote to one Block.
4. Assign stable input IDs `ev_1`, `ev_2`, ...; never expose evidence supplied by the model.
5. Return existing `change_type == "ai_tailor_v1"` suggestions for the same analysis/draft/revision before calling the generator.
6. Reject unknown requirement/evidence/Block IDs, duplicate Blocks, unchanged text, and numeric tokens absent from the original Block plus cited quotes.
7. Create stable Suggestion IDs from `PROMPT_VERSION`, analysis, draft revision, Block and requirement IDs.
8. Call `SuggestionService.create()` so its existing positive-requirement and evidence guards remain authoritative.

Delete the second duplicate definitions of `generate_safe_candidates()` and `_all_suggestions()` in `suggestions.py`; retain the first implementations unchanged.

- [ ] **Step 4: Run tailoring and existing suggestion tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench/test_tailoring.py tests/unit/cv_workbench/test_versioning.py -q
```

Expected: all tests pass; the duplicate cleanup does not change existing candidate behavior.

- [ ] **Step 5: Record a safe checkpoint**

```powershell
git diff --check -- backend/src/starter_agent/cv_workbench/tailoring.py backend/src/starter_agent/cv_workbench/suggestions.py tests/unit/cv_workbench/test_tailoring.py
```

If `suggestions.py` had pre-task user changes, do not commit it separately; retain the verified working-tree checkpoint for the final handoff.

---

### Task 3: Runtime wiring, generation API, and atomic batch decisions

**Files:**
- Modify: `backend/src/starter_agent/cv_workbench/runtime.py:38-118`
- Modify: `backend/src/starter_agent/cv_workbench/suggestions.py:396-461`
- Modify: `backend/src/starter_agent/interfaces/workbench_api.py:200-232,443-446,851-883`
- Modify: `backend/src/starter_agent/bootstrap.py:112-120`
- Modify: `tests/integration/test_workbench_api.py`
- Test: `tests/unit/cv_workbench/test_versioning.py`

**Interfaces:**
- Consumes: `TailoredResumeService`, `ProviderTailoredResumeGenerator`, `SuggestionService.apply_batch()` and existing API error translation.
- Produces: `POST .../tailored-resume-candidates`, `POST /suggestions/batch-decisions`, and `WorkbenchRuntime.tailoring`.

- [ ] **Step 1: Write failing batch-edit and API tests**

Extend `test_batch_apply_is_explicit_and_updates_multiple_blocks_once`:

```python
result = service.apply_batch(
    tuple(item.suggestion_id for item in suggestions),
    workspace_id="ws_demo",
    principal=PRINCIPAL,
    edited_text_by_id={"sg_batch_p": "Edited evidence-backed paragraph."},
)
assert "Edited evidence-backed paragraph." in repository.read(
    result.draft.content, principal=PRINCIPAL, workspace_id="ws_demo"
)
```

Add an integration client with an injected Fake Generator, then assert:

```python
generated = client.post(
    "/v1/workbench/match-analyses/ma_api_evaluate/tailored-resume-candidates",
    json={"workspace_id": "ws_api", "draft_id": "rd_api_suggestion"},
)
assert generated.status_code == 201
assert generated.json()["items"][0]["change_type"] == "ai_tailor_v1"

batch = client.post("/v1/workbench/suggestions/batch-decisions", json={
    "workspace_id": "ws_api",
    "accept_ids": [generated.json()["items"][0]["suggestion_id"]],
    "reject_ids": [],
    "edited_text_by_id": {},
})
assert batch.status_code == 200
assert batch.json()["draft"]["revision"] == 2
```

- [ ] **Step 2: Run the tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench/test_versioning.py tests/integration/test_workbench_api.py -q
```

Expected: failures for the missing argument and missing HTTP routes.

- [ ] **Step 3: Implement runtime and HTTP boundaries**

Make `create_workbench_runtime(..., tailoring_generator=None)` construct one shared SuggestionService and an optional TailoredResumeService. Add `tailoring: TailoredResumeService | None` to WorkbenchRuntime.

In `bootstrap.create_cv_workbench_runtime()` pass:

```python
ProviderTailoredResumeGenerator(
    provider_resolver=ProviderRegistry(settings).get,
    provider_name=settings.model.default_provider,
    model=settings.model.default_model,
)
```

Add strict API bodies:

```python
class TailoredSuggestionGenerateBody(ApiModel):
    workspace_id: str
    draft_id: str

class SuggestionBatchDecisionBody(ApiModel):
    workspace_id: str
    accept_ids: tuple[str, ...] = ()
    reject_ids: tuple[str, ...] = ()
    edited_text_by_id: dict[str, str] = Field(default_factory=dict)
```

The async generation endpoint must translate TailoringServiceError through `_translate()`. The batch endpoint validates disjoint IDs and edited-text keys, preloads every pending suggestion before Draft autosave, passes edits to `apply_batch()`, then rejects validated reject IDs. Return `{"draft": ..., "accepted_ids": ..., "rejected_ids": ..., "invalidated_ids": ...}`.

- [ ] **Step 4: Run unit and integration tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench/test_tailoring.py tests/unit/cv_workbench/test_versioning.py tests/integration/test_workbench_api.py -q
```

Expected: generation, batch editing, and existing Workbench APIs pass.

- [ ] **Step 5: Record a safe checkpoint**

```powershell
git diff --check -- backend/src/starter_agent/cv_workbench/runtime.py backend/src/starter_agent/cv_workbench/suggestions.py backend/src/starter_agent/interfaces/workbench_api.py backend/src/starter_agent/bootstrap.py tests/integration/test_workbench_api.py tests/unit/cv_workbench/test_versioning.py
```

Do not commit overlapping pre-existing user changes; preserve the verified diff for final review.

---

### Task 4: Workbench AI tailoring approval UI

**Files:**
- Modify: `frontend/web/app/features/job-matching.js:336-391`
- Modify: `frontend/web/styles/workbench.css`
- Modify: `tests/unit/test_job_matching_ui.py`

**Interfaces:**
- Consumes: Task 3 generation and batch-decision endpoints plus existing Suggestion JSON.
- Produces: recoverable “AI 定制简历” generation/approval flow with editable candidates and batch acceptance.

- [ ] **Step 1: Write failing frontend contract tests**

```python
def test_ai_tailoring_uses_provider_candidates_and_batch_draft_approval() -> None:
    for contract in (
        "/tailored-resume-candidates",
        "/v1/workbench/suggestions/batch-decisions",
        "AI 定制简历建议",
        "关联岗位要求",
        "证据摘录",
        "批量接受到 Draft",
        "正式版本未改变",
    ):
        assert contract in SOURCE
    tailored = SOURCE[SOURCE.index("async function prepareTailoredResume"):]
    assert "/suggestion-candidates" not in tailored.split("async function renderSuggestions", 1)[0]
```

- [ ] **Step 2: Run the frontend contract test and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_matching_ui.py -q
```

Expected: missing new endpoint and approval copy.

- [ ] **Step 3: Implement the browser flow with DOM APIs**

Change `prepareTailoredResume()` to:

1. Fetch existing analysis suggestions and reuse an active `ai_tailor_v1` draft when present.
2. Otherwise create a Draft from the analysis ResumeVersion and POST to `/tailored-resume-candidates`.
3. Render AI candidates with editable textarea, evidence quotes, requirement IDs, risk, and selection checkboxes.
4. POST selected IDs and edited text to `/suggestions/batch-decisions`.
5. Disable generation/apply buttons while requests run and display backend recovery messages without clearing the current analysis.

Keep `textContent`, `createElement`, and `setAttribute`; do not inject model output through `innerHTML`. Add only scoped `.tailored-*` styles consistent with existing Workbench tokens.

- [ ] **Step 4: Run UI contracts and Workbench shell tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_matching_ui.py tests/unit/test_workbench_agent_ui.py tests/unit/test_workbench_shell_ui.py -q
```

Expected: all UI contracts pass.

- [ ] **Step 5: Record a safe checkpoint**

```powershell
git diff --check -- frontend/web/app/features/job-matching.js frontend/web/styles/workbench.css tests/unit/test_job_matching_ui.py
```

These frontend files already contain user work; do not commit or overwrite unrelated hunks.

---

### Task 5: End-to-end version boundary and regression verification

**Files:**
- Modify: `tests/integration/test_cv_workbench_mvp_e2e.py`
- Modify only if verification exposes a defect: files changed in Tasks 1-4

**Interfaces:**
- Consumes: all Task 1-4 endpoints and services.
- Produces: an executable proof that AI output reaches a Draft and a pending version without changing the source version.

- [ ] **Step 1: Write the failing E2E scenario before any final fixes**

Add a Fake Generator to the runtime and replace the deterministic-candidate portion with:

```python
source = client.get("/v1/workbench/resume-versions/rv_mvp_company_v1").json()
generated = client.post(
    "/v1/workbench/match-analyses/ma_mvp/tailored-resume-candidates",
    json={"workspace_id": "ws_mvp", "draft_id": suggestion_draft["draft_id"]},
).json()
applied = client.post("/v1/workbench/suggestions/batch-decisions", json={
    "workspace_id": "ws_mvp",
    "accept_ids": [item["suggestion_id"] for item in generated["items"]],
    "reject_ids": [],
    "edited_text_by_id": {},
}).json()
pending = client.post(
    f"/v1/workbench/drafts/{suggestion_draft['draft_id']}/versions",
    json={
        "workspace_id": "ws_mvp",
        "version_id": "rv_mvp_company_v2",
        "label": "Example Co tailored v2",
        "expected_draft_revision": applied["draft"]["revision"],
    },
)
assert pending.status_code == 201
assert client.get("/v1/workbench/resume-versions/rv_mvp_company_v1").json()["content"] == source["content"]
assert pending.json()["status"] == "pending_confirmation"
```

- [ ] **Step 2: Run the E2E scenario and verify RED or existing GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_cv_workbench_mvp_e2e.py -q
```

Expected before final integration fixes: fail at the first incomplete boundary. If it is already green from Tasks 1-4, temporarily change the expected `change_type` to prove the assertion fails, restore it, and rerun green.

- [ ] **Step 3: Apply only the minimal integration fix required by the failing assertion**

Do not broaden the feature. Preserve Draft-only writes, positive evidence guards, and pending confirmation.

- [ ] **Step 4: Run fresh full verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/cv_workbench tests/unit/test_job_matching_ui.py tests/unit/test_workbench_agent_ui.py tests/unit/test_workbench_shell_ui.py tests/integration/test_workbench_api.py tests/integration/test_cv_workbench_mvp_e2e.py -q
.\.venv\Scripts\python.exe -m pytest -q -m "not external"
git diff --check
```

Expected: zero test failures and no whitespace errors. If the repository-wide suite has failures unrelated to this feature, record exact failing tests and rerun the complete feature-focused command to distinguish them.

- [ ] **Step 5: Review the requirement checklist and working-tree scope**

Verify explicitly:

- AI generation used the configured Provider through a fake in automated tests.
- `missing` and `conflict` requirements never entered the generation request.
- every created Suggestion carries existing EvidenceReference values.
- batch acceptance changed one Draft revision and no source version.
- the saved result is `pending_confirmation`.
- refresh/idempotent retry did not call the generator twice.
- no Chroma dependency, direct `content_json` update, auto-confirmation, or auto-export was added.

Inspect:

```powershell
git status --short
git diff --stat
git diff -- backend/src/starter_agent/cv_workbench/tailoring.py backend/src/starter_agent/cv_workbench/runtime.py backend/src/starter_agent/cv_workbench/suggestions.py backend/src/starter_agent/interfaces/workbench_api.py backend/src/starter_agent/bootstrap.py frontend/web/app/features/job-matching.js frontend/web/styles/workbench.css tests/unit/cv_workbench/test_tailoring.py tests/unit/cv_workbench/test_versioning.py tests/unit/test_job_matching_ui.py tests/integration/test_workbench_api.py tests/integration/test_cv_workbench_mvp_e2e.py
```

Do not commit pre-existing unrelated changes. Report the feature diff and verification evidence to the user.
