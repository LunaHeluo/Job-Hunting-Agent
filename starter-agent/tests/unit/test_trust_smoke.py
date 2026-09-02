from pathlib import Path
from uuid import uuid4

from starter_agent.trust.smoke import (
    SMOKE_MODEL_INSTRUCTION,
    _trace,
    _run_or_await_delegated_child,
    create_smoke_parent_run,
    expected_smoke_report_fields,
    select_smoke_candidates,
    validate_jd_snapshot,
)
from starter_agent.job_research.candidates import JobCandidate
from starter_agent.trust.store import TrustStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_ONLY_ROOT = PROJECT_ROOT / ".session-only-trust-smoke-tests"


def test_real_smoke_report_requires_search_and_candidate_trace() -> None:
    assert {
        "route_decision",
        "resume_evidence",
        "serpapi_search",
        "candidate_attempts",
        "verified_source_url",
        "separate_from_fixture_baseline",
    }.issubset(expected_smoke_report_fields())


def test_smoke_parent_run_is_created_before_smoke_record() -> None:
    db_path = SESSION_ONLY_ROOT / uuid4().hex / "agent.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = TrustStore(f"sqlite:///{db_path}", PROJECT_ROOT)

    run = create_smoke_parent_run(
        store,
        run_id="real-smoke-parent",
        provider="zhipu",
        model="glm-4.7",
        policy_version="live-smoke",
    )

    assert run.run_type == "smoke"
    assert store.get_run("real-smoke-parent") == run


def test_real_smoke_uses_readable_instruction_and_linked_trace() -> None:
    assert "真实 Smoke" in SMOKE_MODEL_INSTRUCTION
    assert "公开 JD" in SMOKE_MODEL_INSTRUCTION
    assert "完整正文" in SMOKE_MODEL_INSTRUCTION

    trace = _trace("linked-smoke", "model", "passed", {"model": "fixture"})
    assert trace.eval_run_id == "linked-smoke"


def test_smoke_rejects_empty_or_error_page_snapshot() -> None:
    for snapshot in (
        "",
        "Error: net::ERR_NAME_NOT_RESOLVED",
        "404 Not Found - page does not exist",
        "Access denied",
    ):
        valid, detail = validate_jd_snapshot(snapshot)
        assert valid is False
        assert detail["reason_code"]


def test_smoke_accepts_structured_public_jd_snapshot() -> None:
    valid, detail = validate_jd_snapshot(
        "Agent Engineer\nResponsibilities\nBuild agent workflows and evaluations.\n"
        "Requirements\nPython, RAG, Playwright and three years experience.\nApply now"
    )
    assert valid is True
    assert detail["jd_signal_count"] >= 2


def test_smoke_attempts_all_bounded_https_candidates() -> None:
    candidates = tuple(
        JobCandidate(
            url=f"https://jobs.example.test/openings/{index}",
            title=f"AI Engineer {index}",
            url_kind="organic",
            confidence=0.4,
            provider_position=index,
        )
        for index in range(5)
    )

    selected = select_smoke_candidates(candidates, limit=5)

    assert [item.provider_position for item in selected] == [0, 1, 2, 3, 4]


def test_smoke_candidate_selection_rejects_non_https_and_honors_limit() -> None:
    candidates = (
        JobCandidate(
            url="http://jobs.example.test/openings/unsafe",
            title="HTTP role",
            url_kind="organic",
            confidence=0.4,
            provider_position=0,
        ),
        JobCandidate(
            url="https://jobs.example.test/openings/1",
            title="HTTPS role 1",
            url_kind="organic",
            confidence=0.4,
            provider_position=1,
        ),
        JobCandidate(
            url="https://jobs.example.test/openings/2",
            title="HTTPS role 2",
            url_kind="organic",
            confidence=0.4,
            provider_position=2,
        ),
    )

    selected = select_smoke_candidates(candidates, limit=1)

    assert [item.url for item in selected] == [
        "https://jobs.example.test/openings/1"
    ]


def test_explicit_public_smoke_url_is_prioritized_and_labeled() -> None:
    candidates = (
        JobCandidate(
            url="https://search.example.test/jobs/1",
            title="Search result",
            source="serpapi_google",
            url_kind="organic",
            confidence=0.4,
            provider_position=0,
        ),
    )

    selected = select_smoke_candidates(
        candidates,
        limit=2,
        source_url="https://jobs.example.test/openings/probe",
    )

    assert [item.url for item in selected] == [
        "https://jobs.example.test/openings/probe",
        "https://search.example.test/jobs/1",
    ]
    assert selected[0].source == "explicit_smoke_url"


def test_explicit_smoke_url_must_be_https() -> None:
    assert select_smoke_candidates(
        (),
        limit=1,
        source_url="http://jobs.example.test/openings/probe",
    ) == ()


async def test_smoke_observes_child_claimed_by_background_worker() -> None:
    class Pool:
        async def run_once(self, _worker_id):
            return False

    class Worker:
        pool = Pool()

    class Store:
        def get_run_tree(self, _parent_run_id):
            child = type("Child", (), {"id": "child:1", "status": "succeeded"})()
            task = type(
                "Task",
                (),
                {"id": "task:1", "accepted_result_envelope_ref": "artifact:result"},
            )()
            return type("Tree", (), {"child_runs": [child], "child_tasks": [task]})()

    application = type(
        "Application",
        (),
        {"delegation_worker": Worker(), "delegation_store": Store()},
    )()
    receipt = type(
        "Receipt",
        (),
        {
            "parent_run_id": "parent:1",
            "child_run_id": "child:1",
            "child_task_id": "task:1",
        },
    )()

    tree = await _run_or_await_delegated_child(
        application, receipt, worker_id="smoke", timeout_seconds=1
    )

    assert tree.child_runs[0].status == "succeeded"


async def test_smoke_keeps_temporary_runtime_alive_for_bounded_child_retry() -> None:
    state = {"status": "queued", "accepted": None, "attempts": 0}

    class Pool:
        async def run_once(self, _worker_id):
            state["attempts"] += 1
            if state["attempts"] == 1:
                state["status"] = "queued"
            else:
                state["status"] = "partial"
                state["accepted"] = "artifact:result"
            return True

    class Worker:
        pool = Pool()

    class Store:
        def get_run_tree(self, _parent_run_id):
            child = type("Child", (), {"id": "child:1", "status": state["status"]})()
            task = type(
                "Task",
                (),
                {"id": "task:1", "accepted_result_envelope_ref": state["accepted"]},
            )()
            return type("Tree", (), {"child_runs": [child], "child_tasks": [task]})()

    application = type(
        "Application",
        (),
        {"delegation_worker": Worker(), "delegation_store": Store()},
    )()
    receipt = type(
        "Receipt",
        (),
        {
            "parent_run_id": "parent:1",
            "child_run_id": "child:1",
            "child_task_id": "task:1",
        },
    )()

    tree = await _run_or_await_delegated_child(
        application, receipt, worker_id="smoke", timeout_seconds=1
    )

    assert state["attempts"] == 2
    assert tree.child_runs[0].status == "partial"
