"""遥测基础设施：采样记录查询指标，并写入持久化事件与 Prometheus 指标。"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.domain.types import new_id
from enterprise_rag.infrastructure.orm import (
    TelemetryEventRecord,
    TenantConfigurationVersionRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.redaction import redact

REQUESTS = Counter(
    "enterprise_rag_requests_total",
    "API requests by route and outcome.",
    ("method", "route", "status"),
)
REQUEST_LATENCY = Histogram(
    "enterprise_rag_request_duration_seconds",
    "API request duration.",
    ("method", "route"),
)
QUERY_OUTCOMES = Counter(
    "enterprise_rag_query_outcomes_total",
    "Query terminal outcomes.",
    ("status",),
)
INGESTION_OUTCOMES = Counter(
    "enterprise_rag_ingestion_outcomes_total",
    "Ingestion outcomes.",
    ("status",),
)
PROVIDER_TOKENS = Counter(
    "enterprise_rag_provider_tokens_total",
    "Attributed model tokens.",
    ("provider", "direction"),
)
PROVIDER_COST = Counter(
    "enterprise_rag_provider_cost_total",
    "Attributed provider cost when available.",
    ("provider",),
)
QUEUE_BACKLOG = Gauge("enterprise_rag_queue_backlog", "In-process queue backlog.")
QUEUE_DELIVERIES = Counter(
    "enterprise_rag_queue_deliveries_total",
    "Queue deliveries settled by ACK or NACK.",
    ("kind", "outcome"),
)
QUEUE_DELIVERY_LATENCY = Histogram(
    "enterprise_rag_queue_delivery_duration_seconds",
    "Time between delivery receipt and settlement.",
    ("kind", "outcome"),
)
OUTBOX_EVENTS = Counter(
    "enterprise_rag_outbox_events_total",
    "Outbox publish outcomes.",
    ("outcome",),
)
LEASE_EVENTS = Counter(
    "enterprise_rag_worker_lease_events_total",
    "Worker lease recovery outcomes.",
    ("outcome",),
)
INGESTION_STAGES = Counter(
    "enterprise_rag_ingestion_stages_total",
    "Ingestion terminal and stage observations.",
    ("stage", "status"),
)
OCR_PAGES = Counter(
    "enterprise_rag_ocr_pages_total",
    "OCR page outcomes.",
    ("mode", "provider", "outcome"),
)
OCR_LATENCY = Histogram(
    "enterprise_rag_ocr_page_duration_seconds",
    "OCR page processing duration.",
    ("provider",),
)
INDEX_GENERATIONS = Counter(
    "enterprise_rag_index_generations_total",
    "Index generation outcomes.",
    ("outcome",),
)
RECONCILIATION_RESULTS = Counter(
    "enterprise_rag_reconciliation_results_total",
    "Index reconciliation outcomes.",
    ("index", "status"),
)
STREAM_EVENTS = Counter(
    "enterprise_rag_stream_events_total",
    "Query stream terminal outcomes.",
    ("delivery_mode", "outcome", "provider"),
)
STREAM_FIRST_DELTA = Histogram(
    "enterprise_rag_stream_first_delta_seconds",
    "Time from query start to first model delta.",
    ("provider",),
)
STREAM_LATENCY = Histogram(
    "enterprise_rag_stream_duration_seconds",
    "End-to-end query stream duration.",
    ("delivery_mode", "outcome"),
)


@dataclass(frozen=True, slots=True)
class TelemetryInput:
    """待采样遥测事件的租户、阶段、状态、属性和可选内容。"""

    tenant_id: str
    correlation_id: str
    kind: str
    stage: str
    status: str
    duration_ms: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    content: dict[str, Any] = field(default_factory=dict)


class TelemetryService:
    """按采样率记录脱敏后的遥测事件，并遵守内容采集开关。"""

    def __init__(
        self,
        session: AsyncSession,
        content_capture_enabled: bool,
        sample_rate: float = 1.0,
    ) -> None:
        """保存会话和隐私配置。"""
        self.session = session
        self.content_capture_enabled = content_capture_enabled
        self.sample_rate = sample_rate

    async def record(self, item: TelemetryInput) -> TelemetryEventRecord | None:
        """按采样率生成 trace 关联的遥测记录并写入当前事务。"""
        if random.random() > self.sample_rate:
            return None
        context = trace.get_current_span().get_span_context()
        capture_enabled, patterns = await self._capture_policy(item.tenant_id)

        def apply_patterns(value: Any) -> Any:
            safe = redact(value)
            if isinstance(safe, str):
                for pattern in patterns:
                    try:
                        safe = re.sub(pattern, "[REDACTED]", safe)
                    except re.error:
                        continue
                return safe
            if isinstance(safe, dict):
                return {key: apply_patterns(child) for key, child in safe.items()}
            if isinstance(safe, list):
                return [apply_patterns(child) for child in safe]
            return safe

        record = TelemetryEventRecord(
            id=new_id("telemetry"),
            tenant_id=item.tenant_id,
            correlation_id=item.correlation_id,
            trace_id=f"{context.trace_id:032x}" if context.is_valid else item.correlation_id,
            span_id=f"{context.span_id:016x}" if context.is_valid else "",
            kind=item.kind,
            stage=item.stage,
            status=item.status,
            duration_ms=item.duration_ms,
            attributes=apply_patterns(item.attributes),
            content=apply_patterns(item.content) if capture_enabled else {},
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def _capture_policy(self, tenant_id: str) -> tuple[bool, tuple[str, ...]]:
        """Resolve capture and custom redaction from the active tenant snapshot."""
        version = await self.session.scalar(
            select(TenantConfigurationVersionRecord)
            .join(
                TenantRecord,
                TenantRecord.active_config_version_id
                == TenantConfigurationVersionRecord.id,
            )
            .where(
                TenantRecord.id == tenant_id,
                TenantConfigurationVersionRecord.tenant_id == tenant_id,
                TenantConfigurationVersionRecord.state == "active",
            )
        )
        if version is None:
            return self.content_capture_enabled, ()
        policy = version.config.get("content_capture", {})
        if not isinstance(policy, dict):
            return False, ()
        patterns = policy.get("redact_patterns", [])
        return bool(policy.get("enabled", False)), tuple(
            str(pattern) for pattern in patterns if str(pattern)
        )


def record_query_metrics(status: str, usage: dict[str, object]) -> None:
    """更新问答终态、token 数和可用模型成本的 Prometheus 指标。"""
    QUERY_OUTCOMES.labels(status=status).inc()
    provider = str(usage.get("provider", "unavailable"))
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cost = usage.get("cost")
    if isinstance(input_tokens, int):
        PROVIDER_TOKENS.labels(provider=provider, direction="input").inc(input_tokens)
    if isinstance(output_tokens, int):
        PROVIDER_TOKENS.labels(provider=provider, direction="output").inc(output_tokens)
    if isinstance(cost, (int, float)):
        PROVIDER_COST.labels(provider=provider).inc(cost)


def record_queue_delivery(kind: str, outcome: str, duration_seconds: float) -> None:
    """Record ACK/NACK settlement without retaining message content."""
    QUEUE_DELIVERIES.labels(kind=kind, outcome=outcome).inc()
    QUEUE_DELIVERY_LATENCY.labels(kind=kind, outcome=outcome).observe(duration_seconds)


def record_outbox_event(outcome: str) -> None:
    """Record an outbox publish, retry, or poison outcome."""
    OUTBOX_EVENTS.labels(outcome=outcome).inc()


def record_lease_event(outcome: str, count: int = 1) -> None:
    """Record lease recovery or lease-loss observations."""
    if count > 0:
        LEASE_EVENTS.labels(outcome=outcome).inc(count)


def record_ingestion_stage(stage: str, status: str) -> None:
    """Record a non-sensitive ingestion stage observation."""
    INGESTION_STAGES.labels(stage=stage, status=status).inc()


def record_ocr_page(
    mode: str, provider: str, outcome: str, elapsed_ms: float | None = None
) -> None:
    """Record OCR outcome and optional provider duration."""
    OCR_PAGES.labels(mode=mode, provider=provider, outcome=outcome).inc()
    if elapsed_ms is not None:
        OCR_LATENCY.labels(provider=provider).observe(max(elapsed_ms, 0.0) / 1000)


def record_index_generation(outcome: str) -> None:
    """Record index generation publication or rollback outcome."""
    INDEX_GENERATIONS.labels(outcome=outcome).inc()


def record_reconciliation(index: str, status: str) -> None:
    """Record per-index reconciliation status without indexing document content."""
    RECONCILIATION_RESULTS.labels(index=index, status=status).inc()


def record_stream_metrics(
    delivery_mode: str,
    outcome: str,
    provider: str,
    duration_seconds: float,
    first_delta_seconds: float | None = None,
) -> None:
    """Record streaming/buffered timing and terminal outcome."""
    STREAM_EVENTS.labels(
        delivery_mode=delivery_mode, outcome=outcome, provider=provider
    ).inc()
    STREAM_LATENCY.labels(
        delivery_mode=delivery_mode, outcome=outcome
    ).observe(max(duration_seconds, 0.0))
    if first_delta_seconds is not None:
        STREAM_FIRST_DELTA.labels(provider=provider).observe(
            max(first_delta_seconds, 0.0)
        )
