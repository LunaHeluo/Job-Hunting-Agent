import json

from starter_agent.domain.models import Message, ModelResponse, ToolCall
from starter_agent.providers.openai_compatible import OpenAICompatibleProvider
from starter_agent.settings import ModelPricingConfig, ModelPricingTier


async def test_tool_call_is_returned_to_provider(monkeypatch) -> None:
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)

            class MessageResult:
                content = "done"
                tool_calls = []

            class Choice:
                message = MessageResult()

            class Response:
                choices = [Choice()]
                usage = None

            return Response()

    provider = OpenAICompatibleProvider(
        name="test",
        base_url="https://example.test/v1",
        api_key="not-a-real-key",
        timeout=1,
        max_retries=0,
        temperature=0,
    )
    monkeypatch.setattr(provider.client.chat, "completions", FakeCompletions())
    await provider.complete(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="get_current_time",
                        arguments={"timezone": "UTC"},
                    )
                ],
            ),
            Message(
                role="tool",
                content='{"ok": true}',
                name="get_current_time",
                tool_call_id="call-1",
            ),
        ],
        model="test-model",
        tools=[],
        tool_choice="get_current_time",
    )

    tool_call = captured["messages"][0]["tool_calls"][0]
    assert tool_call["id"] == "call-1"
    assert json.loads(tool_call["function"]["arguments"]) == {"timezone": "UTC"}
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_current_time"},
    }


def test_model_response_accepts_nested_provider_usage() -> None:
    response = ModelResponse(
        content="OK",
        provider="zhipu",
        model="glm-5.2",
        usage={
            "prompt_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    )

    assert response.usage["prompt_tokens_details"] == {"cached_tokens": 0}


async def test_configured_model_price_estimates_provider_cost(monkeypatch) -> None:
    class Usage:
        def model_dump(self):
            return {
                "prompt_tokens": 1_000,
                "completion_tokens": 100,
                "total_tokens": 1_100,
            }

    class FakeCompletions:
        async def create(self, **_kwargs):
            class MessageResult:
                content = "done"
                tool_calls = []

            class Choice:
                message = MessageResult()

            class Response:
                choices = [Choice()]
                usage = Usage()

            return Response()

    pricing = ModelPricingConfig(
        currency="CNY",
        price_version="zhipu-glm-4.7-cny-2026-08-14",
        source_url="https://bigmodel.cn/pricing",
        tiers=[
            ModelPricingTier(
                max_input_tokens=31_999,
                max_output_tokens=199,
                input_microunits_per_token=2,
                output_microunits_per_token=8,
            ),
            ModelPricingTier(
                max_input_tokens=31_999,
                input_microunits_per_token=3,
                output_microunits_per_token=14,
            ),
            ModelPricingTier(
                input_microunits_per_token=4,
                output_microunits_per_token=16,
            ),
        ],
    )
    provider = OpenAICompatibleProvider(
        name="zhipu",
        base_url="https://example.test/v1",
        api_key="not-a-real-key",
        timeout=1,
        max_retries=0,
        temperature=0,
        pricing={"glm-4.7": pricing},
    )
    monkeypatch.setattr(provider.client.chat, "completions", FakeCompletions())

    response = await provider.complete(
        [Message(role="user", content="x")], model="glm-4.7", tools=[]
    )

    assert response.usage["cost_microunits"] == 2_800
    assert response.usage["cost_status"] == "estimated"
    assert response.usage["cost_estimated"] is True
    assert response.usage["price_version"] == pricing.price_version
    assert response.usage["currency"] == "CNY"


async def test_unpriced_model_keeps_cost_unknown(monkeypatch) -> None:
    class Usage:
        def model_dump(self):
            return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    class FakeCompletions:
        async def create(self, **_kwargs):
            class MessageResult:
                content = "done"
                tool_calls = []

            class Choice:
                message = MessageResult()

            class Response:
                choices = [Choice()]
                usage = Usage()

            return Response()

    provider = OpenAICompatibleProvider(
        name="test",
        base_url="https://example.test/v1",
        api_key="not-a-real-key",
        timeout=1,
        max_retries=0,
        temperature=0,
    )
    monkeypatch.setattr(provider.client.chat, "completions", FakeCompletions())

    response = await provider.complete(
        [Message(role="user", content="x")], model="unknown", tools=[]
    )

    assert "cost_microunits" not in response.usage
