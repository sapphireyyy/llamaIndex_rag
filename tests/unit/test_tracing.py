from enterprise_rag.application.providers import ExtractiveModelAdapter, ModelGateway
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


async def test_model_gateway_emits_opentelemetry_span_with_structural_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    await ModelGateway(ExtractiveModelAdapter()).complete("policy", "question", "evidence")
    spans = exporter.get_finished_spans()
    model_span = next(span for span in spans if span.name == "model.complete")
    assert model_span.attributes["model.fallback_allowed"] is False
