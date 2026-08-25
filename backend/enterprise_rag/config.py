"""进程配置：读取环境变量并校验开发、生产运行所需的配置组合。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程配置模型：保存服务参数，密钥字段只保存引用而不直接暴露秘密。"""

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    database_url: str = "sqlite+aiosqlite:///./enterprise_rag.db"
    object_store_backend: Literal["filesystem", "s3"] = "filesystem"
    object_store_root: Path = Path("./var/objects")
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_bucket: str = "enterprise-rag"
    s3_access_key_reference: str = "env://MINIO_ROOT_USER"
    s3_secret_key_reference: str = "env://MINIO_ROOT_PASSWORD"
    queue_backend: Literal["inprocess", "rabbitmq"] = "inprocess"
    rabbitmq_url_reference: str = "env://RABBITMQ_URL"
    rabbitmq_queue_name: str = "enterprise-rag-jobs"
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    vector_backend: Literal["memory", "qdrant"] = "qdrant"
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "enterprise-rag-chunks"
    lexical_backend: Literal["memory", "opensearch"] = "memory"
    opensearch_url: str = "http://localhost:9200"
    opensearch_index: str = "enterprise-rag-chunks"
    model_backend: Literal["extractive", "openai_compatible"] = "extractive"
    model_api_base: str = ""
    model_name: str = ""
    model_api_key_reference: str = "env://OPENAI_API_KEY"
    oidc_enabled: bool = False
    oidc_issuer: str = ""
    oidc_audience: str = "enterprise-rag"
    oidc_jwks_url: str = ""
    dev_auth_enabled: bool = True
    dev_tenant_id: str = "tenant-demo"
    dev_user_id: str = "admin-demo"
    dev_groups: list[str] = Field(default_factory=lambda: ["administrators"])
    dev_platform_admin: bool = True
    claim_only_tenant_authorization: bool = False
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    allowed_extensions: set[str] = Field(
        default_factory=lambda: {".pdf", ".docx", ".xlsx", ".pptx", ".md", ".txt", ".html"}
    )
    max_upload_bytes: int = 25 * 1024 * 1024
    content_capture_enabled: bool = False
    telemetry_sample_rate: float = 1.0
    telemetry_retention_days: int = 30
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    request_timeout_seconds: float = 45.0
    provider_retry_attempts: int = 2
    query_concurrency_limit: int = 32
    ingestion_concurrency_limit: int = 4
    context_token_budget: int = 8_000
    upload_rate_limit_per_minute: int = 30
    query_rate_limit_per_minute: int = 60

    @field_validator("dev_groups", "allowed_origins", mode="before")
    @classmethod
    def split_csv(cls, value: object) -> object:
        """把环境变量中的逗号分隔值转换成去空白后的列表。"""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("allowed_extensions", mode="before")
    @classmethod
    def normalize_extensions(cls, value: object) -> object:
        """统一扩展名大小写，并确保每项以点号开头。"""
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, set, tuple)):
            return {
                str(item).lower() if str(item).startswith(".") else f".{str(item).lower()}"
                for item in value
            }
        return value

    @field_validator("oidc_jwks_url")
    @classmethod
    def oidc_requires_jwks(cls, value: str, info: object) -> str:
        """保留 OIDC JWKS 校验钩子，实际组合校验在运行时配置检查中完成。"""
        del info
        return value

    def validate_runtime(self) -> list[str]:
        """检查环境、认证、依赖后端和模型配置之间的组合约束。"""
        problems: list[str] = []
        if self.environment not in {"development", "test"} and self.dev_auth_enabled:
            problems.append("development authentication must be disabled outside development")
        if (
            self.environment not in {"development", "test"}
            and self.claim_only_tenant_authorization
        ):
            problems.append("claim-only tenant authorization is forbidden outside development")
        if self.oidc_enabled and not (self.oidc_issuer and self.oidc_jwks_url):
            problems.append("OIDC requires issuer and JWKS URL")
        if not self.oidc_enabled and not self.dev_auth_enabled:
            problems.append("either OIDC or development authentication must be enabled")
        if self.max_upload_bytes <= 0:
            problems.append("maximum upload bytes must be positive")
        if not 0 <= self.telemetry_sample_rate <= 1:
            problems.append("telemetry sample rate must be between zero and one")
        if self.object_store_backend == "s3" and not (
            self.s3_endpoint_url
            and self.s3_bucket
            and self.s3_access_key_reference
            and self.s3_secret_key_reference
        ):
            problems.append("S3 object storage requires endpoint, bucket, and secret references")
        if self.queue_backend == "rabbitmq" and not self.rabbitmq_url_reference:
            problems.append("RabbitMQ requires a secret URL reference")
        if self.vector_backend == "qdrant" and not self.qdrant_url:
            problems.append("Qdrant requires a service URL")
        if self.lexical_backend == "opensearch" and not self.opensearch_url:
            problems.append("OpenSearch requires a service URL")
        if self.cache_backend == "redis" and not self.redis_url:
            problems.append("Redis requires a service URL")
        if self.model_backend == "openai_compatible":
            if not self.model_api_base:
                problems.append("OpenAI-compatible model requires an API base URL")
            if not self.model_name:
                problems.append("OpenAI-compatible model requires a model name")
            if not self.model_api_key_reference.startswith("env://"):
                problems.append("model API key must use an environment secret reference")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """缓存并返回当前进程使用的唯一配置实例。"""
    return Settings()
