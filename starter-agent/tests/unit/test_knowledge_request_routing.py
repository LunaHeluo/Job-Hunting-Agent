from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from starter_agent.domain.models import Message, ModelResponse
from starter_agent.knowledge.routing import (
    KnowledgeRequestRoute,
    KnowledgeRequestRouter,
)
from starter_agent.providers.base import Provider
from starter_agent.skills.registry import SkillRegistry
from starter_agent.skills.selector import SkillSelector


SKILLS_ROOT = (
    Path(__file__).parents[2]  / "backend" / "src" / "starter_agent" / "skills"
)


class SequenceProvider(Provider):
    name = "route-fixture"

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[Message],
        model: str,
        tools: list[dict[str, Any]],
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        tool_choice: str | None = None,
        context_revision: int | None = None,
    ) -> ModelResponse:
        del on_delta, tool_choice, context_revision
        self.calls.append({"messages": messages, "model": model, "tools": tools})
        return ModelResponse(
            content=self.contents.pop(0),
            provider=self.name,
            model=model,
        )

    async def health(self, model: str) -> tuple[bool, str]:
        return True, model


def router() -> KnowledgeRequestRouter:
    registry = SkillRegistry(SKILLS_ROOT)
    registry.reload()
    return KnowledgeRequestRouter(SkillSelector(registry))


@pytest.mark.asyncio
async def test_job_research_skill_wins_without_classifier_model_call() -> None:
    provider = SequenceProvider([])

    decision = await router().route(
        "根据我的简历搜索深圳的岗位",
        provider=provider,
        model="fixture-model",
    )

    assert decision.route is KnowledgeRequestRoute.JOB_RESEARCH
    assert decision.reason_code == "skill_selected"
    assert decision.skill_name == "job-research"
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"route":"conversation","reason_code":"social_chat"}', "conversation"),
        ('{"route":"knowledge_query","reason_code":"factual_question"}', "knowledge_query"),
    ],
)
async def test_non_job_route_is_schema_validated_and_never_exposes_tools(
    content: str,
    expected: str,
) -> None:
    provider = SequenceProvider([content])

    decision = await router().route(
        "你好" if expected == "conversation" else "合同的生效日期是什么？",
        provider=provider,
        model="fixture-model",
    )

    assert decision.route.value == expected
    assert len(provider.calls) == 1
    assert provider.calls[0]["tools"] == []
    assert provider.calls[0]["model"] == "fixture-model"


@pytest.mark.asyncio
async def test_invalid_classifier_output_retries_once_then_fails_closed() -> None:
    provider = SequenceProvider(["not-json", '{"route":"public_web"}'])

    decision = await router().route(
        "量子计算是什么？",
        provider=provider,
        model="fixture-model",
    )

    assert decision.route is KnowledgeRequestRoute.KNOWLEDGE_QUERY
    assert decision.reason_code == "classifier_invalid_output"
    assert decision.model_attempts == 2
    assert [call["tools"] for call in provider.calls] == [[], []]
    retry_system = provider.calls[1]["messages"][0].content
    assert "conversation" in retry_system
    assert "knowledge_query" in retry_system
    assert "public_web" not in retry_system


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_text",
    [
        "根据我的简历推荐上海的岗位",
        "查询深圳适合我的工作",
        "帮我看看成都现在有哪些 Agent 工程师职位",
    ],
)
async def test_classifier_can_route_flexible_job_research_without_tools(
    user_text: str,
) -> None:
    provider = SequenceProvider(
        ['{"route":"job_research","reason_code":"job_recommendation"}']
    )

    decision = await router().route(
        user_text,
        provider=provider,
        model="fixture-model",
    )

    assert decision.route is KnowledgeRequestRoute.JOB_RESEARCH
    assert decision.reason_code == "classifier_job_research"
    assert decision.model_attempts == 1
    assert provider.calls[0]["tools"] == []
    assert "job_research" in provider.calls[0]["messages"][0].content
