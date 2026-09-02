from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from starter_agent.application import ApplicationService
from starter_agent.bootstrap import create_application
from starter_agent.domain.models import Message, ModelResponse
from starter_agent.providers.base import Provider
from starter_agent.runtime_revision import RuntimeRevision
from starter_agent.skills.registry import SkillRegistry
from starter_agent.skills.selector import SkillSelector


SKILLS_ROOT = Path(__file__).parents[2]  / "backend" / "src" / "starter_agent" / "skills"


class _UnusedProvider(Provider):
    name = "route-fixture"

    async def complete(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | None = None,
        context_revision: int | None = None,
    ) -> ModelResponse:
        raise AssertionError("Skill selection must not call the classifier provider")

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def test_runtime_revision_is_stable_for_identical_inputs() -> None:
    first = RuntimeRevision.build(
        code_version="abc123",
        skill_revision=1,
        tool_revision="tools-a",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
    )
    second = RuntimeRevision.build(
        code_version="abc123",
        skill_revision=1,
        tool_revision="tools-a",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
    )

    assert first.id == second.id
    assert first.requires_restart(second) is False


def test_runtime_revision_detects_changed_skill_snapshot() -> None:
    running = RuntimeRevision.build(
        code_version="abc123",
        skill_revision=1,
        tool_revision="tools-a",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
    )
    desired = RuntimeRevision.build(
        code_version="abc123",
        skill_revision=2,
        tool_revision="tools-a",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
    )

    assert running.id != desired.id
    assert running.requires_restart(desired) is True


@pytest.mark.asyncio
async def test_application_route_decision_carries_active_runtime_revision() -> None:
    skills = SkillRegistry(SKILLS_ROOT)
    skills.reload()
    revision = RuntimeRevision.build(
        code_version="abc123",
        skill_revision=skills.snapshot().revision,
        tool_revision="tools-a",
        prompt_hash="a" * 64,
        config_hash="b" * 64,
    )
    application = object.__new__(ApplicationService)
    application.settings = SimpleNamespace(
        model=SimpleNamespace(
            default_provider="route-fixture",
            default_model="fixture-model",
        ),
        providers={"route-fixture": SimpleNamespace(models=("fixture-model",))},
    )
    application.providers = SimpleNamespace(get=lambda _name: _UnusedProvider())
    application.context = SimpleNamespace(skill_selector=SkillSelector(skills))
    application.runtime_revision = revision

    decision = await application.route_knowledge_request(
        content="Find jobs in Shanghai based on my resume",
        provider_name="route-fixture",
        model="fixture-model",
    )

    assert decision.route.value == "job_research"
    assert decision.runtime_revision == revision.id


def test_bootstrap_shares_one_runtime_revision_across_application_and_runtime() -> None:
    create_application.cache_clear()
    application = create_application()

    assert application.runtime_revision.id
    assert application.runtime.runtime_revision is application.runtime_revision
