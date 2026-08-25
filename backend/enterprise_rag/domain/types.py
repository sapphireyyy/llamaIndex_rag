"""领域基础类型：标识、状态、身份、授权范围和任务消息。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


def new_id(prefix: str) -> str:
    """用业务前缀和 UUID 生成跨实体可读且不重复的标识。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def stable_hash(value: bytes | str) -> str:
    """对字节或 UTF-8 文本计算稳定 SHA-256 哈希。"""
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def stable_json_hash(value: dict[str, Any]) -> str:
    """按排序后的 JSON 表示计算稳定哈希，避免键顺序造成差异。"""
    return stable_hash(json.dumps(value, sort_keys=True, separators=(",", ":")))


class LifecycleState(StrEnum):
    """知识空间和文档等资源的生命周期状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class TenantState(StrEnum):
    """Tenant control-plane lifecycle states."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


TENANT_STATE_TRANSITIONS: dict[TenantState, frozenset[TenantState]] = {
    TenantState.PROVISIONING: frozenset({TenantState.ACTIVE, TenantState.ARCHIVED}),
    TenantState.ACTIVE: frozenset({TenantState.SUSPENDED, TenantState.ARCHIVED}),
    TenantState.SUSPENDED: frozenset({TenantState.ACTIVE, TenantState.ARCHIVED}),
    TenantState.ARCHIVED: frozenset({TenantState.ACTIVE}),
}


def require_tenant_transition(current: TenantState, target: TenantState) -> None:
    """Raise when a requested tenant lifecycle transition is not allowed."""
    if target not in TENANT_STATE_TRANSITIONS[current]:
        raise ValueError(f"invalid tenant transition: {current} -> {target}")


class MembershipState(StrEnum):
    """Lifecycle for tenant principals, memberships, groups, and assignments."""

    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


MEMBERSHIP_STATE_TRANSITIONS: dict[MembershipState, frozenset[MembershipState]] = {
    MembershipState.INVITED: frozenset(
        {MembershipState.ACTIVE, MembershipState.SUSPENDED, MembershipState.REMOVED}
    ),
    MembershipState.ACTIVE: frozenset(
        {MembershipState.SUSPENDED, MembershipState.REMOVED}
    ),
    MembershipState.SUSPENDED: frozenset(
        {MembershipState.ACTIVE, MembershipState.REMOVED}
    ),
    MembershipState.REMOVED: frozenset(),
}


def require_membership_transition(
    current: MembershipState, target: MembershipState
) -> None:
    """Raise when a requested membership transition is not allowed."""
    if target not in MEMBERSHIP_STATE_TRANSITIONS[current]:
        raise ValueError(f"invalid membership transition: {current} -> {target}")


class InvitationState(StrEnum):
    """Single-use tenant invitation lifecycle."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


INVITATION_STATE_TRANSITIONS: dict[InvitationState, frozenset[InvitationState]] = {
    InvitationState.PENDING: frozenset(
        {InvitationState.ACCEPTED, InvitationState.REVOKED, InvitationState.EXPIRED}
    ),
    InvitationState.ACCEPTED: frozenset(),
    InvitationState.REVOKED: frozenset(),
    InvitationState.EXPIRED: frozenset(),
}


def require_invitation_transition(
    current: InvitationState, target: InvitationState
) -> None:
    """Raise when a requested invitation transition is not allowed."""
    if target not in INVITATION_STATE_TRANSITIONS[current]:
        raise ValueError(f"invalid invitation transition: {current} -> {target}")


class PlatformRole(StrEnum):
    """Roles trusted only for the platform control plane."""

    PLATFORM_ADMINISTRATOR = "platform_administrator"


class Role(StrEnum):
    """系统支持的租户角色。"""

    ADMINISTRATOR = "administrator"
    EDITOR = "editor"
    READER = "reader"
    QUALITY = "quality"
    OPERATOR = "operator"


TenantRole = Role


class QuotaResource(StrEnum):
    """Durable tenant quota counters."""

    MEMBERS = "members"
    GROUPS = "groups"
    KNOWLEDGE_SPACES = "knowledge_spaces"
    DOCUMENTS = "documents"
    STORED_BYTES = "stored_bytes"
    PROVIDER_UNITS = "provider_units"


TENANT_CONFIGURATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class AuthenticatedSubject:
    """Verified login subject before any tenant authorization is resolved."""

    subject_id: str
    groups: tuple[str, ...] = ()
    platform_roles: frozenset[PlatformRole] = frozenset()
    tenant_hint: str | None = None
    token_id: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "claims", MappingProxyType(dict(self.claims)))


class JobStatus(StrEnum):
    """摄取任务的状态机状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class TerminalStatus(StrEnum):
    """问答流程对外暴露的终态。"""

    SUCCESS = "success"
    REFUSAL = "refusal"
    DEGRADED = "degraded"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ErrorCategory(StrEnum):
    """统一 HTTP/领域错误分类。"""

    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INTERNAL = "internal_error"


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """当前请求的租户、用户、群组、角色和令牌身份。"""

    tenant_id: str
    user_id: str
    groups: tuple[str, ...] = ()
    roles: frozenset[Role] = frozenset()
    token_id: str | None = None
    principal_id: str | None = None
    membership_id: str | None = None
    authorization_epoch: int = 0
    configuration_version_id: str | None = None
    tenant_state: TenantState = TenantState.ACTIVE

    @property
    def principal_tokens(self) -> frozenset[str]:
        """把用户和群组转换为 ACL 检索使用的主体令牌集合。"""
        return frozenset({f"user:{self.user_id}", *(f"group:{group}" for group in self.groups)})


@dataclass(frozen=True, slots=True)
class AuthorizationScope:
    """检索可使用的租户、主体、知识空间和 ACL 版本范围。"""

    tenant_id: str
    principal_tokens: frozenset[str]
    knowledge_space_ids: frozenset[str]
    acl_epoch: int
    document_ids: frozenset[str] = frozenset()
    configuration_version_id: str | None = None
    resource_versions: tuple[str, ...] = ()
    policy_versions: tuple[str, ...] = ()

    def cache_key(self, assistant_version_id: str) -> str:
        """把授权范围和助手版本编码为可用于缓存隔离的键。"""
        payload = {
            "tenant": self.tenant_id,
            "principals": sorted(self.principal_tokens),
            "spaces": sorted(self.knowledge_space_ids),
            "documents": sorted(self.document_ids),
            "acl_epoch": self.acl_epoch,
            "assistant_version": assistant_version_id,
            "configuration_version": self.configuration_version_id,
            "resource_versions": sorted(self.resource_versions),
            "policy_versions": sorted(self.policy_versions),
        }
        return stable_json_hash(payload)


@dataclass(frozen=True, slots=True)
class JobMessage:
    """队列中传递的任务消息及其重试元数据。"""

    id: str
    kind: str
    tenant_id: str
    correlation_id: str
    payload: dict[str, Any]
    priority: int = 0
    attempt: int = 0
    max_attempts: int = 3
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        payload_tenant = self.payload.get("tenant_id")
        if payload_tenant is not None and str(payload_tenant) != self.tenant_id:
            raise ValueError("job payload tenant does not match its envelope")
        object.__setattr__(self, "payload", {"tenant_id": self.tenant_id, **self.payload})
