from __future__ import annotations

import importlib

import pytest

from starter_agent.domain.models import ModelResponse


class SequencedProvider:
    name = "fake"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[object]] = []

    async def complete(self, messages, model, tools, **_kwargs):
        self.calls.append(messages)
        return ModelResponse(
            content=self.responses.pop(0),
            provider=self.name,
            model=model,
        )


def tailoring_module():
    return importlib.import_module("starter_agent.cv_workbench.tailoring")


def generation_request(module):
    return module.TailoringGenerationRequest(
        analysis_id="ma_1",
        draft_id="rd_1",
        draft_revision=1,
        requirements=(
            {
                "requirement_id": "req_1",
                "original_text": "需要 Python API 经验",
            },
        ),
        blocks=(
            module.TailoringBlock(
                block_id="b1",
                original_text="负责 Python API",
            ),
        ),
        evidence=(
            module.TailoringEvidence(
                evidence_id="ev_1",
                requirement_id="req_1",
                quote="负责 Python API",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_provider_generator_runs_reflection_before_writer() -> None:
    module = tailoring_module()
    provider = SequencedProvider(
        [
            '{"issues_found":0,"issues":[],"notes":"证据可用"}',
            (
                '{"candidates":[{"block_id":"b1",'
                '"proposed_text":"交付 Python API","reason":"贴合岗位",'
                '"requirement_ids":["req_1"],"evidence_ids":["ev_1"],'
                '"risk":"请确认职责边界"}]}'
            ),
        ]
    )
    generator = module.ProviderTailoredResumeGenerator(
        provider_resolver=lambda _name: provider,
        provider_name="fake",
        model="fake-model",
    )

    result = await generator.generate(generation_request(module))

    assert len(provider.calls) == 2
    assert "反思" in provider.calls[0][0].content
    assert "撰写" in provider.calls[1][0].content
    assert result.reflection.notes == "证据可用"
    assert result.candidates[0].block_id == "b1"


@pytest.mark.asyncio
async def test_provider_generator_rejects_non_json_writer_output() -> None:
    module = tailoring_module()
    provider = SequencedProvider(
        [
            '{"issues_found":0,"issues":[],"notes":"证据可用"}',
            "not-json",
        ]
    )
    generator = module.ProviderTailoredResumeGenerator(
        provider_resolver=lambda _name: provider,
        provider_name="fake",
        model="fake-model",
    )

    with pytest.raises(
        module.TailoringServiceError,
        match="tailoring_output_invalid",
    ):
        await generator.generate(generation_request(module))
