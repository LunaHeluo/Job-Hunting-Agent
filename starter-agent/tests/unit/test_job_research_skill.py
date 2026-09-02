from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from starter_agent.agent.context import ContextBuilder
from starter_agent.capabilities.gate import PreToolCallGate, UnifiedToolExecutor
from starter_agent.capabilities.registry import UnifiedToolRegistry
from starter_agent.capabilities.store import CapabilityStore
from starter_agent.domain.models import Message, ToolResult
from starter_agent.job_research.candidates import JobCandidate
from starter_agent.job_research.search_profile import PublicJobSearchProfile
from starter_agent.skills.job_research import JobResearchOrchestrator
from starter_agent.skills.models import SkillRunResult, SkillToolTrace
from starter_agent.skills.registry import SkillRegistry
from starter_agent.skills.selector import SkillSelector
from starter_agent.tools.base import ToolContext
from starter_agent.tools.registry import ToolRegistry


SKILLS_ROOT = (
    Path(__file__).parents[2]  / "backend" / "src" / "starter_agent" / "skills"
)


def test_job_research_definition_contains_fixed_governed_workflow():
    registry = SkillRegistry(SKILLS_ROOT)
    snapshot = registry.reload()
    skill = registry.get("job-research")

    assert snapshot.stale is False
    assert skill is not None
    assert skill.source_path.endswith(
        "backend/src/starter_agent/skills/job-research/SKILL.md"
    )
    assert skill.enabled is True
    assert skill.version == "1.3.0"
    assert {item.key for item in skill.dependencies} == {
        "tool:search_jobs_serpapi",
        "tool:retrieve_resume_evidence",
        "mcp:mcp__playwright__browser_navigate",
        "mcp:mcp__playwright__browser_snapshot",
        "service:job_description_ingestion",
    }
    steps = [
        "retrieve_resume_evidence",
        "search_jobs_serpapi",
        "规范化并排序",
        "browser_navigate",
        "browser_snapshot",
        "提取职责",
        "生成带引用",
    ]
    fixed_steps = skill.definition.split("## Workflow", 1)[1].split(
        "## Validation", 1
    )[0]
    positions = [fixed_steps.index(token) for token in steps]
    assert positions == sorted(positions)
    for heading in (
        "## Preconditions",
        "## Workflow",
        "## Validation",
        "## Failure Handling",
        "## Output Format",
        "## Safety Boundaries",
        "## Trigger Examples",
        "## Non-trigger Examples",
    ):
        assert heading in skill.definition
    for phrase in (
        "不使用默认岗位或默认城市",
        "Locations API",
        "检索简历证据",
        "最小公开搜索画像",
        "不向搜索 Tool 发送简历正文",
        "多个候选",
        "职责",
        "必备要求",
        "加分项",
        "关键限制",
        "内容被裁剪",
        "未验证信息",
        "Tool Trace",
        "能力管理",
        "Pre-Tool-Call Gate",
    ):
        assert phrase in skill.definition


def test_job_validation_accepts_source_backed_jd_when_employer_is_undisclosed() -> None:
    validation = JobResearchOrchestrator._validate_job(
        {
            "title": "Agent Engineer",
            "company": "",
            "location": "Shanghai",
            "responsibilities": ["Build agent workflows"],
            "requirements": ["Production Python experience"],
            "source_url": "https://example.test/jobs/42",
            "page_type": "job_detail",
            "validation_state": "verified",
            "source_spans": [
                {"text": "Build agent workflows"},
                {"text": "Production Python experience"},
            ],
        },
        "https://example.test/jobs/42",
    )

    assert validation.state == "verified"
    assert validation.reason_codes == ("company_not_disclosed",)


def test_job_validation_rejects_browser_error_page_with_complete_looking_fields() -> None:
    validation = JobResearchOrchestrator._validate_job(
        {
            "title": "Agent Engineer",
            "company": "Example",
            "location": "Shanghai",
            "responsibilities": ["Build agent workflows"],
            "requirements": ["Production Python experience"],
            "source_url": "https://example.test/jobs/42",
            "page_type": "error",
        },
        "https://example.test/jobs/42",
    )

    assert validation.state == "rejected"
    assert "not_job_detail_page" in validation.reason_codes


def test_candidate_attempt_audit_persists_only_safe_bounded_summary() -> None:
    registry = UnifiedToolRegistry(ToolRegistry([]))
    store = CapabilityStore("sqlite:///:memory:", Path("."))
    gate = PreToolCallGate(store, registry=registry)
    orchestrator = JobResearchOrchestrator(
        registry,
        UnifiedToolExecutor(store, gate=gate),
    )
    context = ToolContext(session_id=uuid4(), turn_id=uuid4())
    fake_secret = "TEST_ONLY_TOKEN_DO_NOT_USE_123"

    orchestrator._audit_candidate_attempt(
        {
            "candidate_index": 0,
            "source_url": f"https://example.test/jobs/42?token={fake_secret}",
            "status": "invalid_jd",
            "error_code": "incomplete_job_description",
            "page_type": "unknown",
            "truncated": False,
            "duration_ms": 12,
            "validation_state": "rejected",
            "reason_codes": ["missing_requirements"],
            "browser_error_code": "playwright_timeout",
            "fallback_method": "search_snippet",
            "fallback_failures": [
                {
                    "error_code": "access_blocked_challenge",
                    "safe_reason": f"must not persist {fake_secret}",
                }
            ],
            "final_error_code": None,
            "candidate_score": 0.93,
            "candidate_page_kind": "job_detail_candidate",
            "candidate_reason_codes": ["target_location_match"],
            "matched_queries": ["北京 AI Agent 工程师 招聘"],
            "search_engines": ["google"],
            "started_before_deadline": True,
        },
        context=context,
        call_id="snapshot-call-1",
    )

    event = store.list_audit_events()[-1]
    persisted = event.model_dump_json()
    assert event.action == "job_research.candidate.completed"
    assert event.call_id == "snapshot-call-1"
    assert len(event.payload["source_url_hash"]) == 64
    assert event.payload["reason_codes"] == ("missing_requirements",)
    assert event.payload["browser_error_code"] == "playwright_timeout"
    assert event.payload["fallback_method"] == "search_snippet"
    assert event.payload["fallback_failure_codes"] == (
        "access_blocked_challenge",
    )
    assert event.payload["candidate_score"] == 0.93
    assert event.payload["candidate_page_kind"] == "job_detail_candidate"
    assert event.payload["candidate_reason_codes"] == (
        "target_location_match",
    )
    assert event.payload["matched_queries"] == (
        "北京 AI Agent 工程师 招聘",
    )
    assert event.payload["search_engines"] == ("google",)
    assert event.payload["started_before_deadline"] is True
    assert fake_secret not in persisted
    assert "https://example.test" not in persisted


def test_selector_triggers_research_but_not_general_advice_or_rewrite():
    registry = SkillRegistry(SKILLS_ROOT)
    registry.reload()
    selector = SkillSelector(registry)

    assert selector.select("请帮我搜索上海的 AI Agent 岗位").name == "job-research"
    assert selector.select("读取这个公开 JD 并和我的简历比较").name == "job-research"
    assert selector.select("给我一些通用求职建议") is None
    assert selector.select("只润色这段已经提供的文字") is None
    assert selector.select("Compare this JD with my resume").name == "job-research"
    assert selector.select("Analyze the JD for this role").name == "job-research"
    assert selector.select("Give me general career advice") is None
    assert selector.select("Just rewrite this supplied JD paragraph") is None


def test_context_has_light_catalog_until_a_skill_is_triggered(tmp_path: Path):
    identity = tmp_path / "identity.md"
    prompt = tmp_path / "system.md"
    identity.write_text("Agent", encoding="utf-8")
    prompt.write_text("{identity}", encoding="utf-8")
    registry = SkillRegistry(SKILLS_ROOT)
    registry.reload()
    builder = ContextBuilder(
        identity,
        prompt,
        skill_registry=registry,
        skill_selector=SkillSelector(registry),
    )

    idle = builder.build([Message(role="user", content="你好")])
    triggered = builder.build(
        [Message(role="user", content="请搜索 AI Agent 岗位")]
    )

    assert "Enabled Skills" in idle[1].content
    assert "job-research" in idle[1].content
    assert "SerpAPI" not in idle[1].content
    assert "Full Skill Definition: job-research" in triggered[2].content
    assert "SerpAPI" in triggered[2].content


class _ScriptedCandidateOrchestrator(JobResearchOrchestrator):
    def __init__(self, *, snapshot_company: str = "Example") -> None:
        async def no_sleep(_seconds: float) -> None:
            return None

        super().__init__(  # type: ignore[arg-type]
            None,
            None,
            ingestion_available=True,
            browser_sleeper=no_sleep,
        )
        self.calls: list[tuple[str, dict]] = []
        self.audited_attempts: list[tuple[dict, str]] = []
        self.snapshot_count = 0
        self.snapshot_company = snapshot_company

    def _audit_candidate_attempt(self, attempt, *, context, call_id):
        del context
        self.audited_attempts.append((dict(attempt), call_id))

    def _missing(self, dependencies):
        return ()

    async def _call(self, tool_name, arguments, context):
        del context
        self.calls.append((tool_name, dict(arguments)))
        if tool_name == self.browser_tool_name:
            result = ToolResult(
                ok=True,
                data={"source_url": arguments["url"]},
            )
        elif tool_name == self.browser_snapshot_tool_name:
            self.snapshot_count += 1
            if self.snapshot_count <= 2:
                result = ToolResult(
                    ok=True,
                    data={
                        "title": "Job Search Results",
                        "company": "",
                        "location": "",
                        "responsibilities": [],
                        "requirements": [],
                        "source_url": "https://jobs.example.test/listing",
                    },
                )
            else:
                result = ToolResult(
                    ok=True,
                    data={
                        "title": "Agent Engineer",
                        "company": self.snapshot_company,
                        "location": "Berlin",
                        "responsibilities": ["Build agents"],
                        "requirements": ["Python"],
                        "source_url": "https://employer.example.test/jobs/42",
                        "retrieved_at": "2026-07-27T00:00:00Z",
                    },
                )
        else:
            result = ToolResult(
                ok=True,
                data={
                    "evidence": [
                        {
                            "chunk_id": str(uuid4()),
                            "document_id": str(uuid4()),
                            "version": 1,
                            "section": "Skills",
                            "start_line": 1,
                            "end_line": 1,
                            "quote": "Python agent systems",
                            "source_ref": "resume.md@v1#L1-L1",
                        }
                    ]
                },
            )
        trace = SkillToolTrace(
            tool_name=tool_name,
            call_id=f"call-{len(self.calls)}",
            arguments=dict(arguments),
            result=result.model_dump(mode="json"),
            gate_outcome="allow",
            error_code=result.error_code,
        )
        return result, trace


@pytest.mark.asyncio
async def test_candidate_failure_continues_and_resume_evidence_is_read_once():
    orchestrator = _ScriptedCandidateOrchestrator()
    candidates = (
        JobCandidate(
            url="https://jobs.example.test/listing",
            title="Search results",
            url_kind="organic",
            confidence=0.4,
            provider_position=0,
        ),
        JobCandidate(
            url="https://employer.example.test/jobs/42",
            title="Agent Engineer",
            url_kind="structured_apply",
            confidence=1.0,
            provider_position=1,
        ),
    )

    result = await orchestrator.analyze_candidates(
        query="Berlin Agent Engineer",
        candidates=candidates,
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        target_count=1,
    )

    assert [item["status"] for item in result.data["candidate_attempts"]] == [
        "browser_failed",
        "succeeded",
    ]
    assert [item["candidate_index"] for item in result.data["candidate_attempts"]] == [
        0,
        1,
    ]
    assert all(
        item["page_type"] in {"unknown", "job_description"}
        for item in result.data["candidate_attempts"]
    )
    assert all(item["truncated"] is False for item in result.data["candidate_attempts"])
    assert all(item["duration_ms"] >= 0 for item in result.data["candidate_attempts"])
    assert result.data["job"]["source_url"] == candidates[1].url
    assert result.data["job"]["company_source"] == "page_html"
    assert result.data["job"]["company_confidence"] == "high"
    assert len(result.data["jobs"]) == 1
    assert [name for name, _ in orchestrator.calls].count(
        orchestrator.evidence_tool_name
    ) == 1
    evidence_arguments = next(
        arguments
        for name, arguments in orchestrator.calls
        if name == orchestrator.evidence_tool_name
    )
    assert "Build agents" in evidence_arguments["query"]
    assert "Python" in evidence_arguments["query"]
    assert "Berlin Agent Engineer" in evidence_arguments["query"]
    assert [item[0]["status"] for item in orchestrator.audited_attempts] == [
        "browser_failed",
        "succeeded",
    ]


@pytest.mark.asyncio
async def test_browser_success_preserves_trusted_candidate_company_when_page_omits_it():
    orchestrator = _ScriptedCandidateOrchestrator(snapshot_company="")
    candidates = (
        JobCandidate(
            url="https://jobs.example.test/listing",
            title="Search results",
            url_kind="organic",
            confidence=0.4,
            provider_position=0,
        ),
        JobCandidate(
            url="https://employer.example.test/jobs/42",
            title="Agent Engineer",
            company="Official Employer",
            company_source="google_jobs",
            company_confidence="high",
            url_kind="structured_apply",
            confidence=1.0,
            provider_position=1,
        ),
    )

    result = await orchestrator.analyze_candidates(
        query="Berlin Agent Engineer",
        candidates=candidates,
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        target_count=1,
        resume_evidence=[],
    )

    assert result.data["jobs"][0]["company"] == "Official Employer"
    assert result.data["jobs"][0]["company_source"] == "google_jobs"
    assert result.data["jobs"][0]["company_confidence"] == "high"
    assert [item[1] for item in orchestrator.audited_attempts] == [
        "call-8",
        "call-12",
    ]


class _FallbackGate:
    def request_for_tool(self, *, tool_name, arguments, **_kwargs):
        return SimpleNamespace(tool_name=tool_name, arguments=arguments)

    async def evaluate(self, _request):
        return SimpleNamespace(
            outcome="allow",
            permit=SimpleNamespace(id="permit-1"),
        )


class _FallbackExecutor:
    gate = _FallbackGate()

    async def execute(self, request, **_kwargs):
        if request.tool_name.endswith("browser_snapshot"):
            raise RuntimeError("browser_network_target_required")
        return ToolResult(
            ok=True,
            data={"source_url": request.arguments.get("url", "")},
        )


class _FallbackOrchestrator(JobResearchOrchestrator):
    def _missing(self, _dependencies):
        return ()

    def _audit_candidate_attempt(self, *_args, **_kwargs):
        return None


class _ScriptedClock:
    def __init__(self, values: list[float]) -> None:
        self.values = iter(values)

    def __call__(self) -> float:
        return next(self.values)


class _ScriptedFallback:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = iter(outcomes)

    async def retrieve(self, candidate):
        outcome = next(self.outcomes)
        payload = {
            "title": candidate.title,
            "company": "Example",
            "location": "Beijing",
            "responsibilities": ["Build agent workflows"],
            "requirements": ["Python"],
            "source_url": candidate.url,
            "retrieval_method": (
                "http_json_ld" if outcome == "verified" else "search_snippet"
            ),
            "validation_state": (
                "verified" if outcome == "verified" else "partial_verified"
            ),
        }
        return SimpleNamespace(
            jobs=(payload,) if outcome == "verified" else (),
            partial_jobs=(payload,) if outcome == "partial" else (),
            method=payload["retrieval_method"],
            failures=(),
        )


def _candidate_batch(count: int) -> tuple[JobCandidate, ...]:
    return tuple(
        JobCandidate(
            url=f"https://jobs.example.test/{index}",
            title=f"Agent Engineer {index}",
            url_kind="structured_apply",
            confidence=1.0,
            provider_position=index,
            score=1.0,
            reason_codes=("employer_detail_signal",),
            matched_queries=("Beijing AI Agent Engineer jobs",),
            search_engines=("google",),
        )
        for index in range(count)
    )


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_partial_results_do_not_consume_complete_jd_target() -> None:
    orchestrator = _FallbackOrchestrator(
        None,  # type: ignore[arg-type]
        _FallbackExecutor(),  # type: ignore[arg-type]
        page_fallback=_ScriptedFallback(
            ["partial", "verified", "partial", "verified", "verified"]
        ),
        browser_sleeper=_no_sleep,
        clock=_ScriptedClock([0, 0, 1, 2, 3, 4]),
    )

    result = await orchestrator.analyze_candidates(
        query="Beijing AI Agent",
        candidates=_candidate_batch(5),
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        target_count=3,
        max_candidates=10,
        retrieval_budget_seconds=180,
        resume_evidence=[],
    )

    assert len(result.data["jobs"]) == 3
    assert len(result.data["partial_jobs"]) == 2
    assert len(result.data["candidate_attempts"]) == 5


@pytest.mark.asyncio
async def test_soft_deadline_prevents_starting_the_next_candidate() -> None:
    orchestrator = _FallbackOrchestrator(
        None,  # type: ignore[arg-type]
        _FallbackExecutor(),  # type: ignore[arg-type]
        page_fallback=_ScriptedFallback(["partial"]),
        browser_sleeper=_no_sleep,
        clock=_ScriptedClock([0, 0, 181]),
    )

    result = await orchestrator.analyze_candidates(
        query="Beijing AI Agent",
        candidates=_candidate_batch(3),
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        max_candidates=10,
        retrieval_budget_seconds=180,
        resume_evidence=[],
    )

    assert len(result.data["candidate_attempts"]) == 1
    assert result.data["budget_exhausted"] is True


@pytest.mark.asyncio
async def test_candidate_limit_prevents_an_eleventh_start() -> None:
    orchestrator = _FallbackOrchestrator(
        None,  # type: ignore[arg-type]
        _FallbackExecutor(),  # type: ignore[arg-type]
        page_fallback=_ScriptedFallback(["partial"] * 10),
        browser_sleeper=_no_sleep,
        clock=_ScriptedClock(list(range(11))),
    )

    result = await orchestrator.analyze_candidates(
        query="Beijing AI Agent",
        candidates=_candidate_batch(11),
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        max_candidates=10,
        retrieval_budget_seconds=180,
        resume_evidence=[],
    )

    assert len(result.data["candidate_attempts"]) == 10
    assert result.data["candidate_limit"] == 10


@pytest.mark.asyncio
async def test_network_guard_snapshot_rejection_uses_fallback_instead_of_crashing():
    class Gate:
        def request_for_tool(self, *, tool_name, arguments, **_kwargs):
            return SimpleNamespace(tool_name=tool_name, arguments=arguments)

        async def evaluate(self, _request):
            return SimpleNamespace(
                outcome="allow",
                permit=SimpleNamespace(id="permit-1"),
            )

    class Executor:
        gate = Gate()

        async def execute(self, request, **_kwargs):
            if request.tool_name.endswith("browser_snapshot"):
                raise RuntimeError("browser_network_target_required")
            return ToolResult(
                ok=True,
                data={"source_url": request.arguments.get("url", "")},
            )

    class Fallback:
        async def retrieve(self, candidate):
            return SimpleNamespace(
                jobs=(
                    {
                        "title": "Agent Engineer",
                        "company": "Example",
                        "location": "Shanghai",
                        "responsibilities": ["Build agent workflows"],
                        "requirements": ["Python"],
                        "source_url": candidate.url,
                        "retrieval_method": "http_json_ld",
                        "validation_state": "verified",
                    },
                ),
                partial_jobs=(),
                method="http_json_ld",
                failures=(),
            )

    class Orchestrator(JobResearchOrchestrator):
        def _missing(self, _dependencies):
            return ()

        def _audit_candidate_attempt(self, *_args, **_kwargs):
            return None

    async def no_sleep(_seconds: float) -> None:
        return None

    orchestrator = Orchestrator(
        None,  # type: ignore[arg-type]
        Executor(),  # type: ignore[arg-type]
        page_fallback=Fallback(),  # type: ignore[arg-type]
        browser_sleeper=no_sleep,
    )
    candidate = JobCandidate(
        url="https://jobs.example.test/agent",
        title="Agent Engineer",
        url_kind="structured_apply",
        confidence=1.0,
        provider_position=0,
    )

    result = await orchestrator.analyze_candidates(
        query="Shanghai Agent Engineer",
        candidates=(candidate,),
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        target_count=1,
        resume_evidence=[],
    )

    assert result.data["jobs"][0]["source_url"] == candidate.url
    assert result.data["candidate_attempts"][0]["status"] == "fallback_succeeded"
    assert result.data["candidate_attempts"][0]["browser_error_code"] == (
        "browser_network_target_required"
    )
    assert result.data["candidate_attempts"][0]["fallback_method"] == "http_json_ld"


@pytest.mark.asyncio
async def test_search_prepared_requests_generic_location_alias_expansion() -> None:
    class CapturingOrchestrator(JobResearchOrchestrator):
        def __init__(self) -> None:
            super().__init__(None, None)  # type: ignore[arg-type]
            self.arguments = None

        def _missing(self, dependencies):
            return ()

        async def _call(self, tool_name, arguments, context):
            self.arguments = dict(arguments)
            result = ToolResult(
                ok=True,
                data={
                    "results": [{
                        "title": "智能体研发工程师",
                        "url": "https://example.test/jobs/42",
                    }],
                    "filtered_collection_count": 2,
                },
            )
            return result, SkillToolTrace(
                tool_name=tool_name,
                call_id="search-call",
                arguments=dict(arguments),
                result=result.model_dump(mode="json"),
                gate_outcome="allow",
            )

    orchestrator = CapturingOrchestrator()
    prepared = SkillRunResult(
        status="search_profile_ready",
        data={"search_profile": {
            "query": "AI Agent engineer",
            "location": "深圳",
            "location_alias": "Shenzhen",
            "evidence_refs": ["E1"],
        }},
    )

    result = await orchestrator.search_prepared(
        prepared=prepared,
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        limit=5,
    )

    assert orchestrator.arguments == {
        "query": "AI Agent engineer",
        "location": "深圳",
        "location_alias": "Shenzhen",
        "limit": 5,
        "expand_location_aliases": True,
    }
    assert result.data["search_statistics"]["filtered_collection_count"] == 2


@pytest.mark.asyncio
async def test_prepare_request_adds_alias_only_for_non_latin_location() -> None:
    class ProfileBuilder:
        async def build(self, **kwargs):
            return PublicJobSearchProfile(
                query="AI Agent engineer",
                location="深圳",
                evidence_refs=("E1",),
            )

    class AliasBuilder:
        def __init__(self):
            self.locations = []

        async def build(self, *, location, provider, model):
            self.locations.append(location)
            return "Shenzhen"

    class PreparingOrchestrator(JobResearchOrchestrator):
        def _missing(self, dependencies):
            return ()

        def _audit_profile_attempts(self, *args, **kwargs):
            return None

        async def _call(self, tool_name, arguments, context):
            result = ToolResult(
                ok=True,
                data={
                    "evidence": [
                        {
                            "quote": "Python and AI systems",
                            "filename": "resume.md",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ]
                },
            )
            return result, SkillToolTrace(
                tool_name=tool_name,
                call_id="evidence-call",
                arguments=dict(arguments),
                result=result.model_dump(mode="json"),
                gate_outcome="allow",
            )

    alias_builder = AliasBuilder()
    orchestrator = PreparingOrchestrator(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        profile_builder=ProfileBuilder(),  # type: ignore[arg-type]
        location_alias_builder=alias_builder,  # type: ignore[arg-type]
    )

    result = await orchestrator.prepare_request(
        user_request="根据我的简历查询深圳的岗位",
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
        provider=SimpleNamespace(name="test-provider"),
        model="test-model",
    )

    assert result.data["search_profile"]["location_alias"] == "Shenzhen"
    assert alias_builder.locations == ["深圳"]


@pytest.mark.asyncio
async def test_resume_evidence_falls_back_to_local_comparison_retrieval() -> None:
    class FallbackOrchestrator(_ScriptedCandidateOrchestrator):
        def __init__(self) -> None:
            super().__init__()
            self.evidence_calls = 0

        async def _call(self, tool_name, arguments, context):
            if tool_name == self.evidence_tool_name:
                self.evidence_calls += 1
                if self.evidence_calls == 1:
                    self.calls.append((tool_name, dict(arguments)))
                    result = ToolResult(
                        ok=False,
                        data={"evidence": []},
                        error_code="no_evidence",
                    )
                    return result, SkillToolTrace(
                        tool_name=tool_name,
                        call_id="call-evidence-empty",
                        arguments=dict(arguments),
                        result=result.model_dump(mode="json"),
                        gate_outcome="allow",
                        error_code=result.error_code,
                    )
            return await super()._call(tool_name, arguments, context)

    orchestrator = FallbackOrchestrator()
    result, traces = await orchestrator._retrieve_resume_evidence(
        query="AI Engineer",
        jobs=(
            {
                "title": "AI Engineer",
                "responsibilities": ["Build agents"],
                "requirements": ["Python"],
            },
        ),
        top_k=6,
        context=ToolContext(session_id=uuid4(), turn_id=uuid4()),
    )

    assert result.ok is True
    assert len(traces) == 2
    evidence_calls = [
        arguments["query"]
        for name, arguments in orchestrator.calls
        if name == orchestrator.evidence_tool_name
    ]
    assert evidence_calls[0] != evidence_calls[1]
    assert evidence_calls[1] == "我的简历匹配这个岗位"
