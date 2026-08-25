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
