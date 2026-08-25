import json
from collections.abc import AsyncIterator

import pytest
from enterprise_rag.application.ports import ModelResponse
from enterprise_rag.application.providers import (
    ExtractiveModelAdapter,
    ModelGateway,
    OpenAICompatibleModelAdapter,
)


class FailingModel:
    capabilities = frozenset({"completion"})

    async def complete(self, system: str, user: str, context: str) -> ModelResponse:
        del system, user, context
        raise ConnectionError("provider unavailable")

    def stream(self, system: str, user: str, context: str) -> AsyncIterator[str]:
        del system, user, context

        async def iterator() -> AsyncIterator[str]:
            if False:
                yield ""

        return iterator()


async def test_model_gateway_fallback_and_usage_accounting() -> None:
    gateway = ModelGateway(
        FailingModel(),
        fallback=ExtractiveModelAdapter(),
        retries=0,
        failure_threshold=1,
    )
    response = await gateway.complete("policy", "question", "Grounded answer", True)
    assert response.provider == "extractive"
    assert response.text == "Grounded answer"
    assert gateway.total_usage.input_tokens > 0


async def test_model_gateway_rate_limit() -> None:
    gateway = ModelGateway(ExtractiveModelAdapter(), requests_per_minute=1)
    await gateway.complete("", "", "one")
    with pytest.raises(RuntimeError, match="rate limit"):
        await gateway.complete("", "", "two")


async def test_openai_compatible_adapter_maps_completion_request_and_usage(
    respx_mock,
) -> None:
    route = respx_mock.post("https://model.example/v1/chat/completions").respond(
        200,
        json={
            "choices": [{"message": {"content": "已基于证据回答"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )
    adapter = OpenAICompatibleModelAdapter(
        "https://model.example/v1/", "test-key", "example-chat", 5.0
    )

    response = await adapter.complete("system policy", "问题", "证据文本")

    assert route.called
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content)["model"] == "example-chat"
    assert response.text == "已基于证据回答"
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 3
    assert response.provider == "openai-compatible:example-chat"
