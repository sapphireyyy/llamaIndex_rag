"""Strict, immutable tenant-configuration schema and safe defaults."""

from __future__ import annotations

from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enterprise_rag.domain.types import stable_json_hash


class StrictConfigModel(BaseModel):
    """Base model that rejects unknown fields and mutation after validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RetentionPolicy(StrictConfigModel):
    audit_days: int = Field(default=365, ge=1, le=3650)
    content_days: int = Field(default=365, ge=1, le=3650)
    telemetry_days: int = Field(default=30, ge=1, le=3650)


class UploadPolicy(StrictConfigModel):
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    allowed_extensions: tuple[str, ...] = (
        ".pdf",
        ".docx",
        ".xlsx",
        ".pptx",
        ".md",
        ".txt",
        ".html",
    )

    @field_validator("allowed_extensions")
    @classmethod
    def normalize_extensions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            sorted(
                {
                    value.lower() if value.startswith(".") else f".{value.lower()}"
                    for value in values
                    if value.strip()
                }
            )
        )
        if not normalized:
            raise ValueError("at least one upload extension is required")
        return normalized


class DurableQuotas(StrictConfigModel):
    members: int = Field(default=1000, ge=1)
    groups: int = Field(default=250, ge=0)
    knowledge_spaces: int = Field(default=100, ge=1)
    documents: int = Field(default=100_000, ge=1)
    stored_bytes: int = Field(default=100 * 1024 * 1024 * 1024, ge=1)
    provider_units: int = Field(default=10_000_000, ge=0)
    warning_percent: int = Field(default=80, ge=1, le=100)


class RatePolicy(StrictConfigModel):
    query_per_minute: int = Field(default=60, ge=1)
    upload_per_minute: int = Field(default=30, ge=1)
    administration_per_minute: int = Field(default=120, ge=1)


class ConcurrencyPolicy(StrictConfigModel):
    query: int = Field(default=32, ge=1)
    ingestion: int = Field(default=4, ge=1)
    evaluation: int = Field(default=2, ge=1)


class ProviderPolicy(StrictConfigModel):
    allowed_kinds: tuple[str, ...] = ("model", "embedding", "reranker", "connector")
    allowed_profile_ids: tuple[str, ...] = ()
    allow_external_network: bool = False


class ContentCapturePolicy(StrictConfigModel):
    enabled: bool = False
    capture_feedback_comments: bool = False
    redact_patterns: tuple[str, ...] = ()


class IngestionPolicy(StrictConfigModel):
    """版本化的文档处理策略，任务提交后会完整复制该快照。"""

    strategy_version: str = Field(default="ingestion-v1", min_length=1, max_length=80)
    chunk_size: int = Field(default=512, ge=32, le=8192)
    chunk_overlap: int = Field(default=64, ge=0, le=2048)
    ocr_mode: Literal["disabled", "best_effort", "required"] = "disabled"
    ocr_min_text_chars: int = Field(default=32, ge=0, le=100_000)
    ocr_provider_id: str | None = Field(default=None, min_length=1, max_length=120)
    ocr_max_pages: int = Field(default=100, ge=1, le=10_000)
    ocr_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    ocr_concurrency: int = Field(default=2, ge=1, le=32)
    max_chunks: int = Field(default=100_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def overlap_must_fit_chunk(self) -> IngestionPolicy:
        """防止重叠大于切分窗口导致无限循环或空分块。"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.ocr_mode == "required" and not self.ocr_provider_id:
            raise ValueError("required OCR mode needs an OCR provider")
        return self


class TenantConfiguration(StrictConfigModel):
    """Fully resolved tenant policy snapshot persisted as an immutable version."""

    schema_version: Literal[1] = 1
    locale: str = Field(default="zh-CN", pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
    time_zone: str = "Asia/Shanghai"
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    uploads: UploadPolicy = Field(default_factory=UploadPolicy)
    quotas: DurableQuotas = Field(default_factory=DurableQuotas)
    rates: RatePolicy = Field(default_factory=RatePolicy)
    concurrency: ConcurrencyPolicy = Field(default_factory=ConcurrencyPolicy)
    ingestion: IngestionPolicy = Field(default_factory=IngestionPolicy)
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    provider_policy: ProviderPolicy = Field(default_factory=ProviderPolicy)
    content_capture: ContentCapturePolicy = Field(default_factory=ContentCapturePolicy)
    secret_binding_ids: tuple[str, ...] = ()
    default_rag_policy_ids: dict[str, str] = Field(default_factory=dict)

    @field_validator("time_zone")
    @classmethod
    def valid_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown IANA time zone") from exc
        return value

    @field_validator("secret_binding_ids")
    @classmethod
    def unique_secret_bindings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("secret binding identifiers must not be empty")
        return tuple(dict.fromkeys(values))

    @model_validator(mode="before")
    @classmethod
    def reject_raw_secrets(cls, value: Any) -> Any:
        sensitive = {
            "password",
            "secret",
            "client_secret",
            "api_key",
            "access_token",
            "refresh_token",
            "private_key",
        }

        def walk(node: Any, path: str = "configuration") -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    normalized = str(key).strip().lower()
                    if normalized in sensitive:
                        raise ValueError(f"raw secret field is forbidden: {path}.{key}")
                    walk(child, f"{path}.{key}")
            elif isinstance(node, (list, tuple)):
                for index, child in enumerate(node):
                    walk(child, f"{path}[{index}]")

        walk(value)
        return value


def default_tenant_configuration() -> TenantConfiguration:
    """Return the complete version-one configuration used for new tenants."""
    return TenantConfiguration()


def ingestion_processing_snapshot(
    configuration: TenantConfiguration,
    configuration_version_id: str | None,
) -> dict[str, Any]:
    """返回可持久化、稳定哈希且不含秘密的入库处理快照。"""
    policy = configuration.ingestion.model_dump(mode="json")
    return {
        "schema_version": configuration.schema_version,
        "configuration_version_id": configuration_version_id,
        "strategy_version": configuration.ingestion.strategy_version,
        "policy": policy,
        "policy_hash": stable_json_hash(policy),
    }
