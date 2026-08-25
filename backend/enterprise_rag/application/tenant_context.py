"""Persisted tenant-context resolution and centralized default-deny permissions."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.domain.types import (
    AuthenticatedSubject,
    ErrorCategory,
    PlatformRole,
    RequestIdentity,
    Role,
    TenantState,
)
from enterprise_rag.infrastructure.orm import (
    GroupRecord,
    PrincipalGroupRecord,
    PrincipalRecord,
    TenantGroupRoleRecord,
    TenantMembershipRecord,
    TenantRecord,
)

TENANT_PERMISSION_MATRIX: dict[str, frozenset[Role]] = {
    "tenant.read": frozenset(
        {Role.READER, Role.EDITOR, Role.QUALITY, Role.OPERATOR, Role.ADMINISTRATOR}
    ),
    "tenant.admin": frozenset({Role.ADMINISTRATOR}),
    "membership.read": frozenset({Role.ADMINISTRATOR}),
    "membership.write": frozenset({Role.ADMINISTRATOR}),
    "configuration.read": frozenset({Role.OPERATOR, Role.ADMINISTRATOR}),
    "configuration.write": frozenset({Role.ADMINISTRATOR}),
    "usage.read": frozenset({Role.OPERATOR, Role.ADMINISTRATOR}),
    "knowledge.read": frozenset({Role.READER, Role.EDITOR, Role.ADMINISTRATOR}),
    "knowledge.write": frozenset({Role.EDITOR, Role.ADMINISTRATOR}),
    "knowledge.admin": frozenset({Role.ADMINISTRATOR}),
    "assistant.read": frozenset({Role.READER, Role.EDITOR, Role.ADMINISTRATOR}),
    "assistant.write": frozenset({Role.EDITOR, Role.ADMINISTRATOR}),
    "assistant.admin": frozenset({Role.ADMINISTRATOR}),
    "ingestion.execute": frozenset({Role.EDITOR, Role.ADMINISTRATOR}),
    "query.execute": frozenset({Role.READER, Role.EDITOR, Role.ADMINISTRATOR}),
    "evaluation.read": frozenset({Role.QUALITY, Role.OPERATOR, Role.ADMINISTRATOR}),
    "evaluation.write": frozenset({Role.QUALITY, Role.ADMINISTRATOR}),
    "telemetry.read": frozenset({Role.OPERATOR, Role.ADMINISTRATOR}),
    "audit.read": frozenset({Role.OPERATOR, Role.ADMINISTRATOR}),
    "export.read": frozenset({Role.OPERATOR, Role.ADMINISTRATOR}),
}

PLATFORM_PERMISSION_MATRIX: dict[str, frozenset[PlatformRole]] = {
    "platform.tenant.read": frozenset({PlatformRole.PLATFORM_ADMINISTRATOR}),
    "platform.tenant.write": frozenset({PlatformRole.PLATFORM_ADMINISTRATOR}),
    "platform.tenant.lifecycle": frozenset({PlatformRole.PLATFORM_ADMINISTRATOR}),
    "platform.tenant.recovery": frozenset({PlatformRole.PLATFORM_ADMINISTRATOR}),
    "platform.fleet.read": frozenset({PlatformRole.PLATFORM_ADMINISTRATOR}),
}


@dataclass(frozen=True, slots=True)
class DiscoverableTenant:
    id: str
    slug: str
    name: str
    state: str
    revision: int
    membership_state: str
    roles: tuple[str, ...]


def require_tenant_permission(identity: RequestIdentity, operation: str) -> None:
    """Apply the centralized tenant permission matrix with default denial."""
    allowed = TENANT_PERMISSION_MATRIX.get(operation)
    if allowed is None or not identity.roles.intersection(allowed):
        raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)


def require_platform_permission(subject: AuthenticatedSubject, operation: str) -> None:
    """Apply trusted platform permissions without consulting tenant assignments."""
    allowed = PLATFORM_PERMISSION_MATRIX.get(operation)
    if allowed is None or not subject.platform_roles.intersection(allowed):
        raise AppError(ErrorCategory.FORBIDDEN, "Platform operation is not permitted.", 403)


def _parse_roles(values: list[str]) -> set[Role]:
    roles: set[Role] = set()
    for value in values:
        try:
            roles.add(Role(value))
        except ValueError:
            continue
    return roles


async def effective_roles(
    session: AsyncSession, tenant_id: str, principal_id: str, direct_roles: list[str]
) -> tuple[frozenset[Role], tuple[str, ...]]:
    """Combine current direct and group assignments using tenant-owned joins only."""
    rows = (
        await session.execute(
            select(GroupRecord.external_id, TenantGroupRoleRecord.role)
            .join(
                PrincipalGroupRecord,
                (PrincipalGroupRecord.tenant_id == GroupRecord.tenant_id)
                & (PrincipalGroupRecord.group_id == GroupRecord.id),
            )
            .outerjoin(
                TenantGroupRoleRecord,
                (TenantGroupRoleRecord.tenant_id == GroupRecord.tenant_id)
                & (TenantGroupRoleRecord.group_id == GroupRecord.id)
                & (TenantGroupRoleRecord.state == "active"),
            )
            .where(
                GroupRecord.tenant_id == tenant_id,
                GroupRecord.state == "active",
                PrincipalGroupRecord.principal_id == principal_id,
                PrincipalGroupRecord.state == "active",
            )
        )
    ).all()
    groups = tuple(sorted({str(row.external_id) for row in rows}))
    role_values = list(direct_roles) + [str(row.role) for row in rows if row.role]
    return frozenset(_parse_roles(role_values)), groups


async def resolve_tenant_context(
    session: AsyncSession, subject: AuthenticatedSubject, selected_tenant_id: str | None
) -> RequestIdentity:
    """Resolve one active tenant identity exclusively from persisted authorization state."""
    tenant_id = (selected_tenant_id or subject.tenant_hint or "").strip()
    if not tenant_id:
        raise AppError(ErrorCategory.INVALID_REQUEST, "Tenant selection is required.", 400)
    tenant = await session.get(TenantRecord, tenant_id)
    if tenant is None:
        raise AppError(ErrorCategory.NOT_FOUND, "Tenant context is unavailable.", 404)
    if tenant.state != TenantState.ACTIVE.value:
        status = 409 if tenant.state == TenantState.PROVISIONING.value else 423
        raise AppError("tenant_not_active", "Tenant is not active.", status)
    principal = await session.scalar(
        select(PrincipalRecord).where(
            PrincipalRecord.tenant_id == tenant.id,
            PrincipalRecord.external_id == subject.subject_id,
        )
    )
    if principal is None or not principal.active or principal.state != "active":
        raise AppError(ErrorCategory.FORBIDDEN, "Tenant membership is unavailable.", 403)
    membership = await session.scalar(
        select(TenantMembershipRecord).where(
            TenantMembershipRecord.tenant_id == tenant.id,
            TenantMembershipRecord.principal_id == principal.id,
        )
    )
    if membership is None or membership.state != "active":
        raise AppError(ErrorCategory.FORBIDDEN, "Tenant membership is unavailable.", 403)
    roles, groups = await effective_roles(
        session, tenant.id, principal.id, membership.direct_roles
    )
    if not roles:
        raise AppError(ErrorCategory.FORBIDDEN, "Tenant role is unavailable.", 403)
    return RequestIdentity(
        tenant_id=tenant.id,
        user_id=subject.subject_id,
        groups=groups,
        roles=roles,
        token_id=subject.token_id,
        principal_id=principal.id,
        membership_id=membership.id,
        authorization_epoch=tenant.acl_epoch,
        configuration_version_id=tenant.active_config_version_id,
        tenant_state=TenantState(tenant.state),
    )


async def discover_tenants(
    session: AsyncSession, subject: AuthenticatedSubject
) -> list[DiscoverableTenant]:
    """List only tenants linked to the verified subject through current memberships."""
    records = (
        await session.execute(
            select(TenantRecord, PrincipalRecord, TenantMembershipRecord)
            .join(PrincipalRecord, PrincipalRecord.tenant_id == TenantRecord.id)
            .join(
                TenantMembershipRecord,
                (TenantMembershipRecord.tenant_id == TenantRecord.id)
                & (TenantMembershipRecord.principal_id == PrincipalRecord.id),
            )
            .where(
                PrincipalRecord.external_id == subject.subject_id,
                PrincipalRecord.state == "active",
                PrincipalRecord.active.is_(True),
                TenantMembershipRecord.state == "active",
            )
            .order_by(TenantRecord.name, TenantRecord.id)
        )
    ).all()
    output: list[DiscoverableTenant] = []
    for tenant, principal, membership in records:
        roles, _groups = await effective_roles(
            session, tenant.id, principal.id, membership.direct_roles
        )
        output.append(
            DiscoverableTenant(
                id=tenant.id,
                slug=tenant.slug,
                name=tenant.name,
                state=tenant.state,
                revision=tenant.revision,
                membership_state=membership.state,
                roles=tuple(sorted(role.value for role in roles)),
            )
        )
    return output
