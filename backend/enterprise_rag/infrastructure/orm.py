"""SQLAlchemy 持久化模型：描述租户、知识库、问答、评测和审计数据。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from enterprise_rag.domain.types import new_id, utc_now


class Base(DeclarativeBase):
    """所有 SQLAlchemy 模型的声明式基类。"""

    pass


class TimestampMixin:
    """为业务记录提供创建时间和更新时间字段。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class TenantRecord(TimestampMixin, Base):
    """租户及其 ACL 版本、显示名称。"""

    __tablename__ = "tenants"
    __table_args__ = (
        Index("ux_tenants_slug", "slug", unique=True),
        CheckConstraint(
            "state IN ('provisioning', 'active', 'suspended', 'archived')",
            name="ck_tenants_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    slug: Mapped[str] = mapped_column(
        String(120),
        default=lambda: new_id("tenant").replace("_", "-"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False, index=True
    )
    state_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    acl_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active_config_version_id: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PrincipalRecord(TimestampMixin, Base):
    """租户内用户主体记录。"""

    __tablename__ = "principals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id"),
        UniqueConstraint("tenant_id", "id", name="uq_principals_tenant_id_id"),
        CheckConstraint(
            "state IN ('invited', 'active', 'suspended', 'removed')",
            name="ck_principals_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class GroupRecord(TimestampMixin, Base):
    """租户内群组主体记录。"""

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id"),
        UniqueConstraint("tenant_id", "id", name="uq_groups_tenant_id_id"),
        CheckConstraint(
            "state IN ('active', 'suspended', 'removed')", name="ck_groups_state"
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class PrincipalGroupRecord(TimestampMixin, Base):
    """用户与群组的关联记录。"""

    __tablename__ = "principal_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_principal_groups_tenant_principal",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            name="fk_principal_groups_tenant_group",
        ),
        CheckConstraint(
            "state IN ('active', 'suspended', 'removed')",
            name="ck_principal_groups_state",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, index=True
    )
    principal_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TenantMembershipRecord(TimestampMixin, Base):
    """Persisted tenant membership and direct tenant-role assignments."""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "principal_id"),
        ForeignKeyConstraint(
            ["tenant_id", "principal_id"],
            ["principals.tenant_id", "principals.id"],
            name="fk_tenant_memberships_tenant_principal",
        ),
        CheckConstraint(
            "state IN ('invited', 'active', 'suspended', 'removed')",
            name="ck_tenant_memberships_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    principal_id: Mapped[str] = mapped_column(String(80), index=True)
    state: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False, index=True
    )
    direct_roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)


class TenantGroupRoleRecord(TimestampMixin, Base):
    """Tenant roles inherited by every active member of a tenant group."""

    __tablename__ = "tenant_group_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "group_id", "role"),
        ForeignKeyConstraint(
            ["tenant_id", "group_id"],
            ["groups.tenant_id", "groups.id"],
            name="fk_tenant_group_roles_tenant_group",
        ),
        CheckConstraint(
            "state IN ('active', 'suspended', 'removed')",
            name="ck_tenant_group_roles_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    group_id: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TenantInvitationRecord(TimestampMixin, Base):
    """Hashed, expiring and single-use invitation for a tenant membership."""

    __tablename__ = "tenant_invitations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "token_hash"),
        CheckConstraint(
            "state IN ('pending', 'accepted', 'revoked', 'expired')",
            name="ck_tenant_invitations_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    destination: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by_principal_id: Mapped[str | None] = mapped_column(String(80))
    created_by_subject: Mapped[str] = mapped_column(String(255), nullable=False)


class TenantConfigurationVersionRecord(TimestampMixin, Base):
    """Immutable, fully resolved and activatable tenant configuration snapshot."""

    __tablename__ = "tenant_configuration_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("version > 0", name="ck_tenant_configuration_version_positive"),
        CheckConstraint(
            "state IN ('draft', 'active', 'superseded', 'invalid')",
            name="ck_tenant_configuration_versions_state",
        ),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_errors: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    previous_version_id: Mapped[str | None] = mapped_column(String(80), index=True)
    created_by_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantUsageRecord(TimestampMixin, Base):
    """Durable, revisioned usage and reservation counter per quota resource."""

    __tablename__ = "tenant_usage"
    __table_args__ = (
        CheckConstraint("used >= 0", name="ck_tenant_usage_used_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_tenant_usage_reserved_nonnegative"),
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), primary_key=True, index=True
    )
    resource: Mapped[str] = mapped_column(String(80), primary_key=True)
    used: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeSpaceRecord(TimestampMixin, Base):
    """可授权检索的知识空间及默认策略绑定。"""

    __tablename__ = "knowledge_spaces"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="active", nullable=False, index=True)
    default_policy_ids: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)


class SpaceMembershipRecord(TimestampMixin, Base):
    """用户或群组在知识空间中的角色关联。"""

    __tablename__ = "space_memberships"
    __table_args__ = (UniqueConstraint("knowledge_space_id", "principal_token"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_space_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_spaces.id"), index=True
    )
    principal_token: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False)


class DataSourceRecord(TimestampMixin, Base):
    """知识空间绑定的数据源及其外部配置。"""

    __tablename__ = "data_sources"
    __table_args__ = (UniqueConstraint("knowledge_space_id", "external_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_space_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_spaces.id"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    secret_binding_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class DocumentRecord(TimestampMixin, Base):
    """逻辑文档记录，维护活动版本和生命周期状态。"""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("data_source_id", "source_identity"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_space_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_spaces.id"), index=True
    )
    data_source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    source_identity: Mapped[str] = mapped_column(String(768), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="active", nullable=False, index=True)
    active_version_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    active_generation_id: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True
    )
    acl_tokens: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    acl_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class DocumentVersionRecord(TimestampMixin, Base):
    """文档原文件的不可变版本及其来源元数据。"""

    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    parsed_key: Mapped[str | None] = mapped_column(String(1024))
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="staging", nullable=False, index=True)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class IndexGenerationRecord(TimestampMixin, Base):
    """文档内容的可回滚索引处理代次及其外部投影状态。"""

    __tablename__ = "index_generations"
    __table_args__ = (
        UniqueConstraint("document_id", "generation_number"),
        Index("ix_index_generations_document_state", "document_id", "state"),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), index=True
    )
    generation_number: Mapped[int] = mapped_column(Integer, nullable=False)
    processing_config_version_id: Mapped[str | None] = mapped_column(String(80), index=True)
    processing_strategy_version: Mapped[str] = mapped_column(
        String(80), default="ingestion-v1", nullable=False
    )
    processing_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    component_versions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    reason: Mapped[str] = mapped_column(String(80), default="upload", nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="staging", nullable=False, index=True)
    dense_state: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    lexical_state: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(512))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChunkRecord(TimestampMixin, Base):
    """文档版本切分出的检索分块及 ACL 快照。"""

    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("index_generation_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    knowledge_space_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_spaces.id"), index=True
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(
        ForeignKey("document_versions.id"), index=True
    )
    index_generation_id: Mapped[str | None] = mapped_column(
        ForeignKey("index_generations.id"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(512))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    acl_tokens: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    acl_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class IngestionJobRecord(TimestampMixin, Base):
    """文档摄取任务的阶段、重试和错误信息。"""

    __tablename__ = "ingestion_jobs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), index=True)
    document_version_id: Mapped[str | None] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(80), default="queued", nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(120), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    processing_config_version_id: Mapped[str | None] = mapped_column(String(80), index=True)
    processing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class JobAttemptRecord(Base):
    """一次摄取任务尝试的开始、结束和失败信息。"""

    __tablename__ = "job_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(512))


class PolicyVersionRecord(TimestampMixin, Base):
    """策略配置的不可变版本记录。"""

    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "policy_id", "version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    policy_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class AssistantRecord(TimestampMixin, Base):
    """助手逻辑实体及当前活动版本指针。"""

    __tablename__ = "assistants"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(String(80), index=True)


class AssistantVersionRecord(TimestampMixin, Base):
    """助手配置草稿或活动版本的冻结快照。"""

    __tablename__ = "assistant_versions"
    __table_args__ = (UniqueConstraint("assistant_id", "version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    knowledge_space_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    policy_version_ids: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    provider_profile_ids: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_errors: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecretBindingRecord(TimestampMixin, Base):
    """密钥引用及其有效性状态，不保存实际密钥值。"""

    __tablename__ = "secret_bindings"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ProviderProfileRecord(TimestampMixin, Base):
    """模型、检索或连接器供应商的能力配置。"""

    __tablename__ = "provider_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    adapter: Mapped[str] = mapped_column(String(120), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    secret_binding_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    profile_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)


class ConversationRecord(TimestampMixin, Base):
    """用户与助手之间的会话容器。"""

    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    assistant_id: Mapped[str] = mapped_column(ForeignKey("assistants.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)


class MessageRecord(TimestampMixin, Base):
    """会话中的用户或助手消息及内容哈希。"""

    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class QueryExecutionRecord(TimestampMixin, Base):
    """一次问答执行的状态、配置快照、追踪和用量。"""

    __tablename__ = "query_executions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    assistant_version_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_versions.id"), index=True
    )
    correlation_id: Mapped[str] = mapped_column(String(80), index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    component_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(30), default="buffered", nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provider_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    trace: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AnswerRecord(TimestampMixin, Base):
    """问答执行产生的回答文本和终态。"""

    __tablename__ = "answers"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    query_id: Mapped[str] = mapped_column(ForeignKey("query_executions.id"), unique=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    grounded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class CitationRecord(TimestampMixin, Base):
    """回答引用的文档分块及页码章节信息。"""

    __tablename__ = "citations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id"), index=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(String(80), index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(512))


class FeedbackRecord(TimestampMixin, Base):
    """用户对回答或引用提交的反馈。"""

    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    answer_id: Mapped[str] = mapped_column(ForeignKey("answers.id"), index=True)
    citation_id: Mapped[str | None] = mapped_column(ForeignKey("citations.id"), index=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)


class EvaluationDatasetRecord(TimestampMixin, Base):
    """评测数据集逻辑实体及活动版本指针。"""

    __tablename__ = "evaluation_datasets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(String(80), index=True)


class EvaluationDatasetVersionRecord(TimestampMixin, Base):
    """评测样本集合的不可变版本。"""

    __tablename__ = "evaluation_dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("evaluation_datasets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class EvaluationRunRecord(TimestampMixin, Base):
    """一次固定数据集和助手版本的评测运行。"""

    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    dataset_version_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_dataset_versions.id"), index=True
    )
    assistant_version_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_versions.id"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    evaluator_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    aggregates: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)


class EvaluationResultRecord(TimestampMixin, Base):
    """评测运行中的单个样本指标和结果详情。"""

    __tablename__ = "evaluation_results"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), index=True)
    item_id: Mapped[str] = mapped_column(String(120), index=True)
    metrics: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ReleaseGateRecord(TimestampMixin, Base):
    """按聚合指标判断是否允许发布的质量门禁。"""

    __tablename__ = "release_gates"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    operator: Mapped[str] = mapped_column(String(10), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    mandatory: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ReleaseGateOverrideRecord(TimestampMixin, Base):
    """管理员对质量门禁的带理由覆盖记录。"""

    __tablename__ = "release_gate_overrides"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    gate_id: Mapped[str] = mapped_column(ForeignKey("release_gates.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), index=True)
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class TelemetryEventRecord(Base):
    """脱敏后的请求、查询或摄取遥测事件。"""

    __tablename__ = "telemetry_events"
    __table_args__ = (
        Index("ix_telemetry_tenant_created", "tenant_id", "created_at"),
        Index("ix_telemetry_correlation", "tenant_id", "correlation_id"),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    correlation_id: Mapped[str] = mapped_column(String(80), index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    stage: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEventRecord(Base):
    """带前序哈希和当前哈希的不可抵赖审计事件。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_resource", "tenant_id", "resource_type", "resource_id"),
    )
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    principal_id: Mapped[str] = mapped_column(String(255), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authorization_result: Mapped[str] = mapped_column(String(30), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(80), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OutboxRecord(Base):
    """事务 outbox 待投递事件及退避、毒消息状态。"""

    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(120), index=True)
    correlation_id: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    poisoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(120), index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IdempotencyRecord(Base):
    """幂等请求键、请求哈希和响应引用。"""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "operation", "key"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
