from __future__ import annotations

import pytest
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_context import (
    discover_tenants,
    require_platform_permission,
    require_tenant_permission,
    resolve_tenant_context,
)
from enterprise_rag.config import Settings
from enterprise_rag.domain.types import AuthenticatedSubject, PlatformRole, Role
from enterprise_rag.infrastructure.orm import (
    PrincipalRecord,
    TenantMembershipRecord,
    TenantRecord,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _seed_membership(
    session: AsyncSession, tenant_id: str, subject_id: str, role: str
) -> None:
    session.add(TenantRecord(id=tenant_id, slug=tenant_id, name=tenant_id))
    principal = PrincipalRecord(
        id=f"principal-{tenant_id}",
        tenant_id=tenant_id,
        external_id=subject_id,
        display_name=subject_id,
    )
    session.add(principal)
    session.add(
        TenantMembershipRecord(
            id=f"membership-{tenant_id}",
            tenant_id=tenant_id,
            principal_id=principal.id,
            state="active",
            direct_roles=[role],
            source="test",
            provenance={},
            state_reason="",
        )
    )


@pytest.mark.asyncio
async def test_selected_tenant_uses_persisted_roles_not_token_claims(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        await _seed_membership(session, "tenant-a", "oidc|alice", "reader")
        await _seed_membership(session, "tenant-b", "oidc|alice", "administrator")
        await session.commit()

    subject = AuthenticatedSubject(
        "oidc|alice",
        tenant_hint="tenant-b",
        claims={"roles": ["administrator"]},
    )
    async with sessions() as session:
        identity = await resolve_tenant_context(session, subject, "tenant-a")
        discovered = await discover_tenants(session, subject)

    assert identity.tenant_id == "tenant-a"
    assert identity.roles == {Role.READER}
    assert [item.id for item in discovered] == ["tenant-a", "tenant-b"]
    with pytest.raises(AppError):
        require_tenant_permission(identity, "tenant.admin")


def test_platform_permission_is_never_inferred_from_tenant_role() -> None:
    tenant_only = AuthenticatedSubject("alice", claims={"roles": ["administrator"]})
    with pytest.raises(AppError):
        require_platform_permission(tenant_only, "platform.tenant.read")
    trusted = AuthenticatedSubject(
        "operator",
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMINISTRATOR}),
    )
    require_platform_permission(trusted, "platform.tenant.read")


def test_claim_only_tenant_authorization_is_forbidden_in_staging() -> None:
    settings = Settings(
        environment="staging",
        oidc_enabled=True,
        oidc_issuer="https://identity.example",
        oidc_jwks_url="https://identity.example/jwks",
        dev_auth_enabled=False,
        claim_only_tenant_authorization=True,
    )
    assert "claim-only tenant authorization is forbidden outside development" in (
        settings.validate_runtime()
    )
