import asyncio
import json
import threading
from collections.abc import AsyncIterator

import httpx
import pytest
from enterprise_rag.application.ports import ModelDelta, ModelResponse, ModelUsage
from enterprise_rag.application.providers import (
    ExtractiveModelAdapter,
    ModelGateway,
    OpenAICompatibleModelAdapter,
)
from enterprise_rag.application.query import QueryCancelled, QueryService


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


class ScriptedStreamingModel:
    capabilities = frozenset({"completion", "streaming"})

    def __init__(self, *, fail_after_text: bool = False) -> None:
        self.fail_after_text = fail_after_text
        self.calls = 0

    async def complete(self, system: str, user: str, context: str) -> ModelResponse:
        del system, user, context
        return ModelResponse("complete", ModelUsage(1, 1, None), "scripted")

    def stream_events(
        self, system: str, user: str, context: str
    ) -> AsyncIterator[ModelDelta]:
        del system, user, context
        self.calls += 1

        async def iterator() -> AsyncIterator[ModelDelta]:
            if self.fail_after_text:
                yield ModelDelta(text="partial", provider="scripted")
                raise ConnectionError("stream interrupted")
            raise ConnectionError("stream unavailable")
            yield ModelDelta()

        return iterator()


class SuccessfulFallbackModel(ScriptedStreamingModel):
    def stream_events(
        self, system: str, user: str, context: str
    ) -> AsyncIterator[ModelDelta]:
        del system, user, context
        self.calls += 1

        async def iterator() -> AsyncIterator[ModelDelta]:
            yield ModelDelta(text="fallback", provider="fallback")
            yield ModelDelta(
                finish_reason="stop",
                usage=ModelUsage(2, 1, None),
                provider="fallback",
            )

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


async def test_streaming_gateway_falls_back_only_before_first_delta() -> None:
    primary = ScriptedStreamingModel()
    fallback = SuccessfulFallbackModel()
    gateway = ModelGateway(primary, fallback=fallback, retries=0)

    events = [
        event
        async for event in gateway.stream_events("system", "question", "context", True)
    ]

    assert "".join(event.text for event in events) == "fallback"
    assert primary.calls == 1
    assert fallback.calls == 1

    interrupted = ModelGateway(
        ScriptedStreamingModel(fail_after_text=True),
        fallback=SuccessfulFallbackModel(),
        retries=0,
    )
    with pytest.raises(ConnectionError, match="stream interrupted"):
        _ = [
            event
            async for event in interrupted.stream_events(
                "system", "question", "context", True
            )
        ]
    assert interrupted.fallback is not None
    assert interrupted.fallback.calls == 0


async def test_openai_compatible_adapter_parses_real_sse_delta_and_usage(
    respx_mock,
) -> None:
    body = (
        'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":11,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )
    route = respx_mock.post("https://model.example/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )
    )
    adapter = OpenAICompatibleModelAdapter(
        "https://model.example/v1", "test-key", "example-chat", 5.0
    )

    events = [
        event
        async for event in adapter.stream_events("system", "question", "context")
    ]

    assert route.called
    assert "".join(event.text for event in events) == "你好"
    assert events[-1].finish_reason == "stop"
    assert events[-1].usage == ModelUsage(11, 2, None)
    assert events[-1].provider == "openai-compatible:example-chat"


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


async def test_query_cancellation_closes_the_provider_stream() -> None:
    closed = False

    async def source() -> AsyncIterator[ModelDelta]:
        nonlocal closed
        try:
            yield ModelDelta(text="首段", provider="scripted")
            await asyncio.sleep(60)
        finally:
            closed = True

    cancel_event = threading.Event()
    stream = QueryService._stream_with_cancellation(source(), cancel_event)
    assert (await anext(stream)).text == "首段"
    cancel_event.set()

    with pytest.raises(QueryCancelled):
        await anext(stream)
    assert closed
