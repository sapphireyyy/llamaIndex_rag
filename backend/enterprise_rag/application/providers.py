"""适配器实现：注册模型、解析器、连接器、搜索和追踪等运行时能力。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, cast

import httpx
from opentelemetry import trace

from enterprise_rag.application.ports import ModelAdapter, ModelDelta, ModelResponse, ModelUsage


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    """适配器的静态描述，包含类别、能力集合和实例工厂。"""

    name: str
    kind: str
    capabilities: frozenset[str]
    factory: Callable[[dict[str, Any]], object]


class AdapterRegistry:
    """保存可插拔适配器描述，并按类别和名称创建实现。"""

    def __init__(self) -> None:
        """初始化按类别和名称索引的适配器注册表。"""
        self._descriptors: dict[tuple[str, str], AdapterDescriptor] = {}

    def register(self, descriptor: AdapterDescriptor) -> None:
        """注册一个可供配置层选择的适配器，拒绝重复键。"""
        key = (descriptor.kind, descriptor.name)
        if key in self._descriptors:
            raise ValueError(f"adapter {descriptor.kind}/{descriptor.name} is already registered")
        self._descriptors[key] = descriptor

    def describe(self, kind: str, name: str) -> AdapterDescriptor | None:
        """按类别和名称查询适配器能力描述。"""
        return self._descriptors.get((kind, name))

    def create(self, kind: str, name: str, config: dict[str, Any]) -> object:
        """调用已注册工厂，根据配置创建适配器实例。"""
        descriptor = self.describe(kind, name)
        if descriptor is None:
            raise ValueError(f"adapter {kind}/{name} is not registered")
        return descriptor.factory(config)

    def descriptors(self) -> tuple[AdapterDescriptor, ...]:
        """返回全部已注册描述，供配置校验和管理界面展示。"""
        return tuple(self._descriptors.values())


class ExtractiveModelAdapter:
    """无凭证本地回答器：直接抽取已授权上下文，仅用于开发与测试。"""

    capabilities = frozenset({"completion", "grounded-context", "local"})

    async def complete(self, system: str, user: str, context: str) -> ModelResponse:
        """忽略远端模型调用，从上下文生成可重复的确定性回答。"""
        del system, user
        answer = context.strip() or "当前授权知识不足，无法回答该问题"
        return ModelResponse(
            text=answer,
            usage=ModelUsage(len(context.split()), len(answer.split()), 0.0),
            provider="extractive",
        )

    def stream(self, system: str, user: str, context: str) -> AsyncIterator[str]:
        """抽取式 Provider 不伪报 streaming 能力，调用流式接口时明确失败。"""
        del system, user, context

        async def iterator() -> AsyncIterator[str]:
            """保留旧端口但不把完整回答伪装成增量流。"""
            raise RuntimeError("extractive provider does not support true streaming")
            yield ""

        return iterator()


class OpenAICompatibleModelAdapter:
    """调用 OpenAI Chat Completions 兼容接口的远程模型适配器。"""

    capabilities = frozenset({"completion", "streaming", "grounded-context", "remote"})

    def __init__(self, api_base: str, api_key: str, model: str, timeout_seconds: float) -> None:
        """保存远端端点、凭据引用解析后的密钥、模型名和超时设置。"""
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def complete(self, system: str, user: str, context: str) -> ModelResponse:
        """发送带授权证据的请求，并统一解析回答文本与 token 用量。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"问题: {user}\n\n已授权证据:\n{context}",
                },
            ],
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.api_base}/chat/completions", json=payload, headers=headers
            )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("model response did not contain choices")
        message = choices[0].get("message", {})
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("model response did not contain text")
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
        completion_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
        return ModelResponse(
            text=content.strip(),
            usage=ModelUsage(
                int(prompt_tokens) if isinstance(prompt_tokens, int) else 0,
                int(completion_tokens) if isinstance(completion_tokens, int) else 0,
                None,
            ),
            provider=f"openai-compatible:{self.model}",
        )

    def stream_events(
        self, system: str, user: str, context: str
    ) -> AsyncIterator[ModelDelta]:
        """请求 OpenAI-compatible SSE，并逐条返回真实 delta 与最终 usage。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"问题: {user}\n\n已授权证据:\n{context}",
                },
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async def iterator() -> AsyncIterator[ModelDelta]:
            """保持 HTTP 流连接直到终端事件或异常。"""
            finished = False
            async with (
                httpx.AsyncClient(timeout=self.timeout_seconds) as client,
                client.stream(
                    "POST",
                    f"{self.api_base}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response,
            ):
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            if not finished:
                                finished = True
                                yield ModelDelta(
                                    finish_reason="stop",
                                    provider=f"openai-compatible:{self.model}",
                                )
                            break
                        try:
                            value = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            raise RuntimeError("invalid model SSE payload") from exc
                        choices = value.get("choices", [])
                        choice = choices[0] if isinstance(choices, list) and choices else {}
                        delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                        content = delta.get("content", "") if isinstance(delta, dict) else ""
                        if isinstance(content, list):
                            content = "".join(
                                str(item.get("text", ""))
                                for item in content
                                if isinstance(item, dict)
                            )
                        if not isinstance(content, str):
                            content = ""
                        finish_reason = (
                            choice.get("finish_reason") if isinstance(choice, dict) else None
                        )
                        usage_value = value.get("usage")
                        usage = _usage_from_payload(usage_value)
                        if finish_reason is not None:
                            finished = True
                        if content or finish_reason is not None or usage is not None:
                            yield ModelDelta(
                                text=content,
                                finish_reason=str(finish_reason) if finish_reason else None,
                                usage=usage,
                                provider=f"openai-compatible:{self.model}",
                            )

        return iterator()

    def stream(self, system: str, user: str, context: str) -> AsyncIterator[str]:
        """将真实 SSE 的文本 delta 暴露给旧的字符串流端口。"""

        async def iterator() -> AsyncIterator[str]:
            """逐个转发远端 delta，不重组完整回答。"""
            async for event in self.stream_events(system, user, context):
                if event.text:
                    yield event.text

        return iterator()


class UploadConnector:
    """上传数据源连接器：登记本地上传标识，不主动拉取远程内容。"""

    capabilities = frozenset({"upload", "content-fingerprint", "acl-metadata"})

    async def discover(
        self, cursor: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """上传连接器没有外部游标，因此返回空发现结果。"""
        del cursor
        return [], None

    async def fetch(self, source_identity: str) -> tuple[bytes, dict[str, Any]]:
        """明确拒绝远端拉取，上传内容由 API 直接交给摄取服务。"""
        raise FileNotFoundError(source_identity)


class StructuralTraceSink:
    """结构化追踪接收器：本地默认实现只接收事件、不落盘。"""

    capabilities = frozenset({"structural-events", "redaction", "tenant-scope"})

    async def emit(self, event: dict[str, Any]) -> None:
        """接收结构化事件，为生产实现保留可替换端口。"""
        del event


@dataclass(slots=True)
class CircuitState:
    """模型熔断器的失败次数和开启时间。"""

    failures: int = 0
    opened_at: float | None = None


class ModelGateway:
    """为模型调用提供限流、并发控制、重试、熔断和可选回退。"""

    def __init__(
        self,
        primary: ModelAdapter,
        fallback: ModelAdapter | None = None,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        failure_threshold: int = 5,
        recovery_seconds: float = 30.0,
        requests_per_minute: int = 60,
        concurrency_limit: int = 8,
    ) -> None:
        """初始化主备模型通道及其可靠性控制参数。"""
        self.primary = primary
        self.fallback = fallback
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.circuit = CircuitState()
        self.total_usage = ModelUsage(0, 0, 0.0)
        self.requests_per_minute = requests_per_minute
        self._request_times: deque[float] = deque()
        self._semaphore = asyncio.Semaphore(concurrency_limit)

    def _check_rate_limit(self) -> None:
        """清理过期时间戳并检查当前分钟是否超过调用额度。"""
        now = time.monotonic()
        while self._request_times and now - self._request_times[0] >= 60.0:
            self._request_times.popleft()
        if len(self._request_times) >= self.requests_per_minute:
            raise RuntimeError("model rate limit exceeded")
        self._request_times.append(now)

    def _circuit_open(self) -> bool:
        """判断熔断是否仍在恢复窗口内，窗口结束后自动复位。"""
        if self.circuit.opened_at is None:
            return False
        if time.monotonic() - self.circuit.opened_at >= self.recovery_seconds:
            self.circuit = CircuitState()
            return False
        return True

    def _record_usage(self, usage: ModelUsage) -> None:
        """把本次模型调用的 token 和成本累加到网关统计。"""
        cost = None
        if self.total_usage.cost is not None and usage.cost is not None:
            cost = self.total_usage.cost + usage.cost
        self.total_usage = ModelUsage(
            self.total_usage.input_tokens + usage.input_tokens,
            self.total_usage.output_tokens + usage.output_tokens,
            cost,
        )

    async def complete(
        self, system: str, user: str, context: str, allow_fallback: bool = False
    ) -> ModelResponse:
        """在并发信号量保护下执行一次完整模型生成。"""
        self._check_rate_limit()
        async with self._semaphore:
            return await self._complete(system, user, context, allow_fallback)

    async def _complete(
        self, system: str, user: str, context: str, allow_fallback: bool
    ) -> ModelResponse:
        """建立 tracing span，并进入带重试与回退的调用流程。"""
        tracer = trace.get_tracer("enterprise_rag.model")
        with tracer.start_as_current_span("model.complete") as span:
            span.set_attribute("model.fallback_allowed", allow_fallback)
            return await self._complete_traced(system, user, context, allow_fallback)

    async def _complete_traced(
        self, system: str, user: str, context: str, allow_fallback: bool
    ) -> ModelResponse:
        """执行主模型调用；网络失败时重试，持续失败后熔断并按需回退。"""
        if self._circuit_open():
            if allow_fallback and self.fallback is not None:
                response = await self.fallback.complete(system, user, context)
                self._record_usage(response.usage)
                return response
            raise RuntimeError("model circuit is open")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await asyncio.wait_for(
                    self.primary.complete(system, user, context), self.timeout_seconds
                )
                self.circuit = CircuitState()
                self._record_usage(response.usage)
                return response
            except (TimeoutError, ConnectionError, OSError, httpx.HTTPError) as exc:
                last_error = exc
                self.circuit.failures += 1
                if self.circuit.failures >= self.failure_threshold:
                    self.circuit.opened_at = time.monotonic()
                if attempt < self.retries:
                    await asyncio.sleep(min(2**attempt * 0.1, 1.0))
        if allow_fallback and self.fallback is not None:
            response = await self.fallback.complete(system, user, context)
            self._record_usage(response.usage)
            return response
        raise RuntimeError("model request failed") from last_error

    def stream(
        self, system: str, user: str, context: str, allow_fallback: bool = False
    ) -> AsyncIterator[str]:
        """通过真实 delta 流提供兼容的字符串迭代器。"""

        async def iterator() -> AsyncIterator[str]:
            """只转发模型增量，不从完整回答切词。"""
            async for event in self.stream_events(system, user, context, allow_fallback):
                if event.text:
                    yield event.text

        return iterator()

    def stream_events(
        self,
        system: str,
        user: str,
        context: str,
        allow_fallback: bool = False,
    ) -> AsyncIterator[ModelDelta]:
        """首个 delta 前允许重试/回退，首个 delta 后任何失败都原样终止。"""

        async def iterator() -> AsyncIterator[ModelDelta]:
            self._check_rate_limit()
            async with self._semaphore:
                emitted = False
                last_error: Exception | None = None
                for attempt in range(self.retries + 1):
                    try:
                        async for event in self._adapter_stream(
                            self.primary, system, user, context
                        ):
                            if event.text:
                                emitted = True
                            if event.usage is not None:
                                self._record_usage(event.usage)
                            yield event
                        self.circuit = CircuitState()
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if emitted:
                            raise
                        last_error = exc
                        self.circuit.failures += 1
                        if self.circuit.failures >= self.failure_threshold:
                            self.circuit.opened_at = time.monotonic()
                        if attempt < self.retries:
                            await asyncio.sleep(min(2**attempt * 0.1, 1.0))
                if allow_fallback and self.fallback is not None:
                    async for event in self._adapter_stream(
                        self.fallback, system, user, context
                    ):
                        if event.text:
                            emitted = True
                        if event.usage is not None:
                            self._record_usage(event.usage)
                        yield event
                    return
                raise RuntimeError("model streaming request failed") from last_error

        return iterator()

    @staticmethod
    def _adapter_stream(
        adapter: ModelAdapter,
        system: str,
        user: str,
        context: str,
    ) -> AsyncIterator[ModelDelta]:
        """把真实 Provider 流或兼容字符串流统一成 ModelDelta。"""
        stream_events = getattr(adapter, "stream_events", None)
        if callable(stream_events):
            stream_factory = cast(
                Callable[[str, str, str], AsyncIterator[ModelDelta]], stream_events
            )
            return stream_factory(system, user, context)
        if "streaming" not in getattr(adapter, "capabilities", frozenset()):
            raise RuntimeError("model provider does not support true streaming")

        async def iterator() -> AsyncIterator[ModelDelta]:
            async for chunk in adapter.stream(system, user, context):
                yield ModelDelta(text=chunk, provider=type(adapter).__name__)

        return iterator()


def _usage_from_payload(value: object) -> ModelUsage | None:
    """把兼容服务的 usage 对象转换为领域用量。"""
    if not isinstance(value, dict):
        return None
    prompt = value.get("prompt_tokens", 0)
    completion = value.get("completion_tokens", 0)
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return ModelUsage(prompt, completion, None)


def default_registry() -> AdapterRegistry:
    """构造开发和生产共用的默认适配器注册表。"""
    from enterprise_rag.application.guardrails import BuiltinGuardrails
    from enterprise_rag.infrastructure.ocr import (
        DeterministicOCRAdapter,
        NoopOCRAdapter,
        TesseractOCRAdapter,
    )
    from enterprise_rag.infrastructure.parsers import AllowListParser
    from enterprise_rag.infrastructure.search import (
        DeterministicEmbedding,
        MemoryLexicalSearch,
        MemoryVectorSearch,
        TokenOverlapReranker,
    )

    registry = AdapterRegistry()
    descriptors = [
        AdapterDescriptor(
            "extractive", "model", ExtractiveModelAdapter.capabilities,
            lambda config: ExtractiveModelAdapter(),
        ),
        AdapterDescriptor(
            "openai-compatible", "model", OpenAICompatibleModelAdapter.capabilities,
            lambda config: OpenAICompatibleModelAdapter(
                str(config["api_base"]),
                str(config["api_key"]),
                str(config["model"]),
                float(config.get("timeout_seconds", 45.0)),
            ),
        ),
        AdapterDescriptor(
            "upload", "connector", UploadConnector.capabilities,
            lambda config: UploadConnector(),
        ),
        AdapterDescriptor(
            "allowlist", "parser",
            frozenset({"structure", "pages", "sections", "tables"}),
            lambda config: AllowListParser(),
        ),
        AdapterDescriptor(
            "none", "ocr", NoopOCRAdapter.capabilities,
            lambda config: NoopOCRAdapter(),
        ),
        AdapterDescriptor(
            "deterministic", "ocr", DeterministicOCRAdapter.capabilities,
            lambda config: DeterministicOCRAdapter(),
        ),
        AdapterDescriptor(
            "tesseract", "ocr", TesseractOCRAdapter.capabilities,
            lambda config: TesseractOCRAdapter(str(config.get("language", "chi_sim+eng"))),
        ),
        AdapterDescriptor(
            "deterministic", "embedding", frozenset({"batch", "local"}),
            lambda config: DeterministicEmbedding(int(config.get("dimensions", 64))),
        ),
        AdapterDescriptor(
            "memory-vector", "dense", frozenset({"acl-filter", "staging", "publish"}),
            lambda config: MemoryVectorSearch(DeterministicEmbedding()),
        ),
        AdapterDescriptor(
            "memory-lexical", "lexical", frozenset({"acl-filter", "staging", "publish"}),
            lambda config: MemoryLexicalSearch(),
        ),
        AdapterDescriptor(
            "token-overlap", "reranker", TokenOverlapReranker.capabilities,
            lambda config: TokenOverlapReranker(),
        ),
        AdapterDescriptor(
            "builtin", "guardrail", BuiltinGuardrails.capabilities,
            lambda config: BuiltinGuardrails(),
        ),
        AdapterDescriptor(
            "structural", "trace", StructuralTraceSink.capabilities,
            lambda config: StructuralTraceSink(),
        ),
    ]
    for descriptor in descriptors:
        registry.register(descriptor)
    return registry
