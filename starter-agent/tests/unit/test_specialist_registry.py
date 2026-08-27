from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from starter_agent.delegation.models import BudgetLimits
from starter_agent.delegation.registry import (
    SQLiteSpecialistRegistryStore,
    SpecialistRegistry,
    SpecialistRegistryError,
)


PROJECT_ROOT = Path(__file__).parents[2]
SPECIALISTS_ROOT = PROJECT_ROOT / "config" / "specialists"
SESSION_ONLY_ROOT = PROJECT_ROOT / ".session-only-specialist-registry-tests"


def _candidate_root(name: str) -> Path:
    root = SESSION_ONLY_ROOT / f"{name}-{uuid4().hex}" / "specialists"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _registry(
    root: Path = SPECIALISTS_ROOT,
    *,
    dependencies: dict[str, bool] | None = None,
    store: SQLiteSpecialistRegistryStore | None = None,
    project_root: Path = PROJECT_ROOT,
) -> SpecialistRegistry:
    states = dependencies or {}
    return SpecialistRegistry(
        root,
        project_root=project_root,
        dependency_resolver=lambda dependency: states.get(dependency, True),
        store=store,
    )


def test_repository_definitions_load_with_stable_hashes_and_minimum_permissions() -> None:
    registry = _registry()
    snapshot = registry.reload()

    assert snapshot.revision == 1
    assert len(snapshot.snapshot_hash) == 64
    web = registry.resolve("job_web_researcher")
    profile = registry.resolve("profile_evidence_analyst")
    assert web.version == "1.0.0"
    assert profile.version == "1.0.0"
    assert len(web.prompt_hash) == 64
    assert len(web.input_schema_hash) == 64
    assert len(web.output_schema_hash) == 64
    assert "retrieve_resume_evidence" not in web.allowed_tools
    assert "delegate_task" not in web.allowed_tools
    assert profile.allowed_tools == ("retrieve_resume_evidence",)
    assert not any("playwright" in name or name == "search_jobs_serpapi" for name in profile.allowed_tools)
    assert "delegate_task" not in profile.allowed_tools


def test_resolve_rejects_missing_disabled_dependency_and_capability_mismatch() -> None:
    registry = _registry(dependencies={"service:serpapi": False})
    registry.reload()

    with pytest.raises(SpecialistRegistryError) as unavailable:
        registry.resolve("job_web_researcher")
    assert unavailable.value.code == "specialist_dependency_unavailable"
    with pytest.raises(SpecialistRegistryError) as missing:
        registry.resolve("does_not_exist")
    assert missing.value.code == "specialist_not_found"

    healthy = _registry()
    healthy.reload()
    healthy.set_disabled("profile_evidence_analyst", reason="maintenance")
    with pytest.raises(SpecialistRegistryError) as disabled:
        healthy.resolve("profile_evidence_analyst")
    assert disabled.value.code == "specialist_disabled"
    with pytest.raises(SpecialistRegistryError) as mismatch:
        healthy.resolve(
            "job_web_researcher", required_capabilities=("resume_evidence",)
        )
    assert mismatch.value.code == "specialist_capability_mismatch"


def test_resolve_validates_input_schema_and_budget_maximum() -> None:
    registry = _registry()
    registry.reload()

    resolved = registry.resolve(
        "job_web_researcher",
        inputs={
            "query": "Agent engineer Sydney",
            "target_fields": ["title", "company", "requirements"],
            "max_pages": 3,
            "stop_conditions": {"target_jobs": 5},
            "output_schema_version": "job-web-output-v1",
        },
        requested_budget=BudgetLimits(
            tokens=10_000,
            cost_microunits=500_000,
            wall_clock_ms=120_000,
            model_calls=10,
            tool_calls=20,
        ),
    )
    assert resolved.specialist_id == "job_web_researcher"

    with pytest.raises(SpecialistRegistryError) as invalid:
        registry.resolve("job_web_researcher", inputs={"query": "Sydney"})
    assert invalid.value.code == "specialist_schema_invalid"
    with pytest.raises(SpecialistRegistryError) as over_budget:
        registry.resolve(
            "job_web_researcher",
            requested_budget=BudgetLimits(
                tokens=200_001,
                cost_microunits=1,
                wall_clock_ms=1,
                model_calls=1,
                tool_calls=1,
            ),
        )
    assert over_budget.value.code == "specialist_budget_exceeded"


def test_failed_reload_is_atomic_and_old_runtime_snapshot_remains_available(
) -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    candidate_root = _candidate_root("atomic")
    candidate = candidate_root / source.name
    candidate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    registry = _registry(candidate_root)
    initial = registry.reload()
    pinned = registry.resolve("profile_evidence_analyst")

    candidate.write_text("specialist_id: [invalid", encoding="utf-8")
    with pytest.raises(SpecialistRegistryError) as captured:
        registry.reload()
    assert captured.value.code == "specialist_registry_invalid"
    assert registry.snapshot() is initial
    assert registry.resolve("profile_evidence_analyst") is pinned
    assert registry.snapshot(initial.snapshot_hash) is initial


def test_reload_publishes_new_snapshot_without_mutating_pinned_definition(
) -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    candidate_root = _candidate_root("snapshot")
    candidate = candidate_root / source.name
    original = source.read_text(encoding="utf-8")
    candidate.write_text(original, encoding="utf-8")
    registry = _registry(candidate_root)
    first_snapshot = registry.reload()
    pinned = registry.resolve("profile_evidence_analyst")

    candidate.write_text(original.replace('version: "1.0.0"', 'version: "1.0.1"'), encoding="utf-8")
    second_snapshot = registry.reload()

    assert second_snapshot.revision == 2
    assert second_snapshot.snapshot_hash != first_snapshot.snapshot_hash
    assert registry.resolve("profile_evidence_analyst").version == "1.0.1"
    assert pinned.version == "1.0.0"
    assert registry.snapshot(first_snapshot.snapshot_hash) is first_snapshot


def test_prompt_boundary_text_is_part_of_the_versioned_snapshot() -> None:
    registry = _registry()
    registry.reload()
    web = registry.resolve("job_web_researcher")
    profile = registry.resolve("profile_evidence_analyst")

    assert "不可信" in web.system_prompt
    assert "验证码" in web.system_prompt
    assert "不得递归委派" in web.system_prompt
    assert "不得补写" in profile.system_prompt
    assert "授权" in profile.system_prompt
    assert "不得递归委派" in profile.system_prompt


def test_disabled_override_cannot_enable_a_file_disabled_definition() -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    candidate_root = _candidate_root("file-disabled")
    candidate = candidate_root / source.name
    candidate.write_text(
        source.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    registry = _registry(candidate_root)
    registry.reload()
    registry.clear_disabled("profile_evidence_analyst")

    with pytest.raises(SpecialistRegistryError) as captured:
        registry.resolve("profile_evidence_analyst")
    assert captured.value.code == "specialist_disabled"


def test_unknown_disable_override_does_not_create_a_specialist() -> None:
    registry = _registry()
    registry.reload()
    registry.set_disabled("does_not_exist", reason="maintenance")

    with pytest.raises(SpecialistRegistryError) as captured:
        registry.resolve("does_not_exist")
    assert captured.value.code == "specialist_not_found"


def test_reload_rejects_prompt_path_escape_and_symlink_escape() -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    candidate_root = _candidate_root("path-escape")
    candidate = candidate_root / source.name
    candidate.write_text(
        source.read_text(encoding="utf-8").replace(
            "config/specialists/prompts/profile_evidence_analyst-v1.md",
            "../outside.md",
        ),
        encoding="utf-8",
    )
    registry = _registry(candidate_root)

    with pytest.raises(SpecialistRegistryError) as captured:
        registry.reload()
    assert captured.value.code == "specialist_registry_invalid"


def test_reload_rejects_wrong_file_name_and_wrong_specialist_tool_boundary() -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    wrong_name_root = _candidate_root("wrong-name")
    (wrong_name_root / "unexpected.yaml").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(SpecialistRegistryError):
        _registry(wrong_name_root).reload()

    wrong_tool_root = _candidate_root("wrong-tool")
    (wrong_tool_root / source.name).write_text(
        source.read_text(encoding="utf-8").replace(
            "allowed_tools: [retrieve_resume_evidence]",
            "allowed_tools: [retrieve_resume_evidence, search_jobs_serpapi]",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecialistRegistryError):
        _registry(wrong_tool_root).reload()


def test_current_disable_is_persisted_but_pinned_runtime_snapshot_is_immutable() -> None:
    database_root = _candidate_root("override-db").parent
    url = f"sqlite:///{database_root / 'registry.db'}"
    store = SQLiteSpecialistRegistryStore(url, PROJECT_ROOT)
    registry = _registry(store=store)
    snapshot = registry.reload()
    registry.set_disabled("profile_evidence_analyst", reason="maintenance")

    with pytest.raises(SpecialistRegistryError) as disabled:
        registry.resolve("profile_evidence_analyst")
    assert disabled.value.code == "specialist_disabled"
    pinned = registry.resolve_pinned(
        "profile_evidence_analyst", snapshot_hash=snapshot.snapshot_hash
    )
    assert pinned.version == "1.0.0"
    store.close()

    reopened_store = SQLiteSpecialistRegistryStore(url, PROJECT_ROOT)
    reopened = _registry(store=reopened_store)
    assert reopened.resolve_pinned(
        "profile_evidence_analyst", snapshot_hash=snapshot.snapshot_hash
    ).definition_hash == pinned.definition_hash
    reopened.reload()
    with pytest.raises(SpecialistRegistryError) as persisted:
        reopened.resolve("profile_evidence_analyst")
    assert persisted.value.code == "specialist_disabled"
    reopened_store.close()


def test_dependency_health_is_checked_at_resolve_time() -> None:
    states = {"service:serpapi": True, "mcp:playwright": True}
    registry = SpecialistRegistry(
        SPECIALISTS_ROOT,
        project_root=PROJECT_ROOT,
        dependency_resolver=lambda dependency: states.get(dependency, True),
    )
    registry.reload()
    assert registry.resolve("job_web_researcher").specialist_id == "job_web_researcher"

    states["service:serpapi"] = False
    with pytest.raises(SpecialistRegistryError) as unavailable:
        registry.resolve("job_web_researcher")
    assert unavailable.value.code == "specialist_dependency_unavailable"


def test_prompt_must_be_in_prompt_root_and_must_not_contain_secrets() -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    isolated_project = _candidate_root("prompt-project").parent
    definitions = isolated_project / "config" / "specialists"
    prompts = definitions / "prompts"
    definitions.mkdir(parents=True)
    prompts.mkdir()
    outside_root = _candidate_root("prompt-outside")
    outside_prompt = definitions / "not-a-prompt.md"
    outside_prompt.write_text("safe but outside prompt directory", encoding="utf-8")
    (outside_root / source.name).write_text(
        source.read_text(encoding="utf-8").replace(
            "config/specialists/prompts/profile_evidence_analyst-v1.md",
            "config/specialists/not-a-prompt.md",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecialistRegistryError):
        _registry(outside_root, project_root=isolated_project).reload()

    secret_root = _candidate_root("prompt-secret")
    prompt = prompts / "session-secret.md"
    prompt.write_text("authorization: Bearer sk-test-secret-value", encoding="utf-8")
    (secret_root / source.name).write_text(
        source.read_text(encoding="utf-8").replace(
            "config/specialists/prompts/profile_evidence_analyst-v1.md",
            "config/specialists/prompts/session-secret.md",
        ),
        encoding="utf-8",
    )
    with pytest.raises(SpecialistRegistryError):
        _registry(secret_root, project_root=isolated_project).reload()


def test_json_definition_loads_and_uri_format_is_enforced() -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    candidate_root = _candidate_root("json")
    import json
    import yaml

    material = yaml.safe_load(source.read_text(encoding="utf-8"))
    (candidate_root / "profile_evidence_analyst.json").write_text(
        json.dumps(material, ensure_ascii=False), encoding="utf-8"
    )
    registry = _registry(candidate_root)
    assert registry.reload().definitions[0].specialist_id == "profile_evidence_analyst"

    web = _registry()
    web.reload()
    with pytest.raises(SpecialistRegistryError) as invalid_uri:
        web.resolve(
            "job_web_researcher",
            inputs={
                "query": "Sydney",
                "urls": ["not a uri"],
                "target_fields": ["title"],
                "max_pages": 1,
                "stop_conditions": {},
                "output_schema_version": "job-web-output-v1",
            },
        )
    assert invalid_uri.value.code == "specialist_schema_invalid"


def test_output_schemas_reject_uncontracted_fields_and_hashes_are_stable() -> None:
    from jsonschema import Draft202012Validator

    registry = _registry()
    first = registry.reload()
    second = registry.reload()
    assert first.snapshot_hash == second.snapshot_hash
    web = registry.resolve("job_web_researcher")
    profile = registry.resolve("profile_evidence_analyst")
    with pytest.raises(Exception):
        Draft202012Validator(web.output_schema).validate(
            {
                "jobs": [{
                    "title": "Agent Engineer",
                    "company": "Example",
                    "location": "Sydney",
                    "responsibilities": [],
                    "requirements": [],
                    "source_url": "https://example.test/job",
                    "final_url": "https://example.test/job",
                    "retrieved_at": "2026-08-12T00:00:00Z",
                    "validation_state": "verified",
                    "content_hash": "a" * 64,
                    "artifact_refs": [],
                    "secret": "leak",
                }],
                "missing": [],
                "errors": [],
                "visited": {"page_count": 1, "step_count": 1},
            }
        )


def test_registry_without_dependency_resolver_fails_closed() -> None:
    registry = SpecialistRegistry(SPECIALISTS_ROOT, project_root=PROJECT_ROOT)
    registry.reload()
    with pytest.raises(SpecialistRegistryError) as captured:
        registry.resolve("job_web_researcher")
    assert captured.value.code == "specialist_dependency_unavailable"


def test_definition_hash_changes_when_prompt_or_schema_changes() -> None:
    source = SPECIALISTS_ROOT / "profile_evidence_analyst.yaml"
    candidate_root = _candidate_root("hash-content")
    candidate = candidate_root / source.name
    original = source.read_text(encoding="utf-8")
    candidate.write_text(original, encoding="utf-8")
    registry = _registry(candidate_root)
    first = registry.reload().definitions[0]

    candidate.write_text(
        original.replace("maxItems: 500", "maxItems: 499"), encoding="utf-8"
    )
    second = registry.reload().definitions[0]
    assert second.input_schema_hash != first.input_schema_hash
    assert second.definition_hash != first.definition_hash


def test_tampered_persisted_snapshot_is_rejected_and_cannot_become_current() -> None:
    import json
    import sqlite3

    database_root = _candidate_root("tamper-db").parent
    database_path = database_root / "registry.db"
    url = f"sqlite:///{database_path}"
    store = SQLiteSpecialistRegistryStore(url, PROJECT_ROOT)
    registry = _registry(store=store)
    snapshot = registry.reload()
    store.close()

    connection = sqlite3.connect(database_path)
    payload = json.loads(
        connection.execute(
            "SELECT payload_json FROM delegation_specialist_snapshots WHERE snapshot_hash = ?",
            (snapshot.snapshot_hash,),
        ).fetchone()[0]
    )
    profile = next(
        item
        for item in payload["definitions"]
        if item["specialist_id"] == "profile_evidence_analyst"
    )
    profile["allowed_tools"].append("search_jobs_serpapi")
    connection.execute(
        "UPDATE delegation_specialist_snapshots SET payload_json = ? WHERE snapshot_hash = ?",
        (json.dumps(payload), snapshot.snapshot_hash),
    )
    connection.commit()
    connection.close()

    reopened_store = SQLiteSpecialistRegistryStore(url, PROJECT_ROOT)
    reopened = _registry(store=reopened_store)
    with pytest.raises(SpecialistRegistryError) as captured:
        reopened.resolve_pinned(
            "profile_evidence_analyst", snapshot_hash=snapshot.snapshot_hash
        )
    assert captured.value.code == "specialist_registry_invalid"
    assert reopened.reload().definitions[1].allowed_tools == (
        "retrieve_resume_evidence",
    )
    reopened_store.close()


def test_persisted_snapshot_payload_hash_must_match_database_lookup_key() -> None:
    import json
    import sqlite3

    database_root = _candidate_root("rehash-db").parent
    database_path = database_root / "registry.db"
    url = f"sqlite:///{database_path}"
    store = SQLiteSpecialistRegistryStore(url, PROJECT_ROOT)
    registry = _registry(store=store)
    snapshot = registry.reload()
    store.close()

    connection = sqlite3.connect(database_path)
    payload = json.loads(connection.execute(
        "SELECT payload_json FROM delegation_specialist_snapshots WHERE snapshot_hash = ?",
        (snapshot.snapshot_hash,),
    ).fetchone()[0])
    payload["snapshot_hash"] = "f" * 64
    connection.execute(
        "UPDATE delegation_specialist_snapshots SET payload_json = ? WHERE snapshot_hash = ?",
        (json.dumps(payload), snapshot.snapshot_hash),
    )
    connection.commit()
    connection.close()

    reopened_store = SQLiteSpecialistRegistryStore(url, PROJECT_ROOT)
    reopened = _registry(store=reopened_store)
    with pytest.raises(SpecialistRegistryError) as captured:
        reopened.resolve_pinned(
            "profile_evidence_analyst", snapshot_hash=snapshot.snapshot_hash
        )
    assert captured.value.code == "specialist_registry_invalid"
    reopened_store.close()


def test_dependency_resolver_exception_fails_closed_with_stable_error() -> None:
    def unavailable(_dependency: str) -> bool:
        raise RuntimeError("health backend down")

    registry = SpecialistRegistry(
        SPECIALISTS_ROOT,
        project_root=PROJECT_ROOT,
        dependency_resolver=unavailable,
    )
    registry.reload()
    with pytest.raises(SpecialistRegistryError) as captured:
        registry.resolve("job_web_researcher")
    assert captured.value.code == "specialist_dependency_unavailable"


def test_sqlite_registry_store_serializes_concurrent_override_writes() -> None:
    from concurrent.futures import ThreadPoolExecutor

    database_root = _candidate_root("concurrent-db").parent
    store = SQLiteSpecialistRegistryStore(
        f"sqlite:///{database_root / 'registry.db'}", PROJECT_ROOT
    )

    def update(index: int) -> None:
        specialist_id = f"specialist:{index % 4}"
        for attempt in range(20):
            store.set_disabled(specialist_id, f"maintenance:{index}:{attempt}")
            store.disabled_reason(specialist_id)
            store.clear_disabled(specialist_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(update, range(16)))
    store.close()
    with pytest.raises(Exception):
        Draft202012Validator(profile.output_schema).validate(
            {
                "matches": [{
                    "requirement_ref": "r1",
                    "match_status": "matched",
                    "evidence": [{"chunk_id": "c1", "secret": "leak"}],
                    "evidence_strength": "strong",
                }],
                "missing": [],
                "conflicts": [],
            }
        )
