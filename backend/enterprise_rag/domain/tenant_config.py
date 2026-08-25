"""Strict, immutable tenant-configuration schema and safe defaults."""

from __future__ import annotations

from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
