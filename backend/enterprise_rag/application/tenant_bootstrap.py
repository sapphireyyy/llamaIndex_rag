"""Idempotent tenant backfill and bootstrap helpers for operators and startup fixtures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.application.retrieval import (
    DEFAULT_RETRIEVAL_POLICY_CONFIG,
    DEFAULT_RETRIEVAL_POLICY_ID,
    DEFAULT_RETRIEVAL_POLICY_VERSION,
)
from enterprise_rag.domain.tenant_config import default_tenant_configuration
from enterprise_rag.domain.types import QuotaResource, Role, new_id, stable_json_hash, utc_now
from enterprise_rag.infrastructure.orm import (
    DocumentRecord,
    DocumentVersionRecord,
    GroupRecord,
    KnowledgeSpaceRecord,
    PolicyVersionRecord,
    PrincipalRecord,
    TenantConfigurationVersionRecord,
    TenantMembershipRecord,
    TenantRecord,
    TenantUsageRecord,
)


@dataclass(slots=True)
class TenantBackfillReport:
    """Machine-readable evidence produced by a tenant backfill pass."""

    tenants_seen: int = 0
    configurations_created: int = 0
    usage_rows_created: int = 0
    administrators_created: int = 0
    missing_administrator_tenant_ids: list[str] = field(default_factory=list)


async def _unique_slug(session: AsyncSession, tenant: TenantRecord) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", tenant.name.strip().lower()).strip("-")
    base = (base or tenant.id.lower().replace("_", "-"))[:100]
    candidate = base
    suffix = 1
    while await session.scalar(select(TenantRecord.id).where(TenantRecord.slug == candidate)):
        suffix += 1
        candidate = f"{base[:110 - len(str(suffix))]}-{suffix}"
    return candidate


async def calculate_tenant_usage(
    session: AsyncSession, tenant_id: str
) -> dict[QuotaResource, int]:
    """Calculate durable quota baselines from tenant-owned source-of-truth rows."""
    active_members = list(
        (
            await session.scalars(
                select(TenantMembershipRecord).where(
                    TenantMembershipRecord.tenant_id == tenant_id,
                    TenantMembershipRecord.state == "active",
                )
            )
        ).all()
    )
    return {
        QuotaResource.MEMBERS: len(active_members),
        QuotaResource.GROUPS: int(
            await session.scalar(
                select(func.count(GroupRecord.id)).where(
                    GroupRecord.tenant_id == tenant_id,
                    GroupRecord.state == "active",
                )
            )
            or 0
        ),
        QuotaResource.KNOWLEDGE_SPACES: int(
            await session.scalar(
                select(func.count(KnowledgeSpaceRecord.id)).where(
                    KnowledgeSpaceRecord.tenant_id == tenant_id,
                    KnowledgeSpaceRecord.state == "active",
                )
            )
            or 0
        ),
        QuotaResource.DOCUMENTS: int(
            await session.scalar(
                select(func.count(DocumentRecord.id)).where(
                    DocumentRecord.tenant_id == tenant_id,
                    DocumentRecord.state == "active",
                )
            )
            or 0
        ),
        QuotaResource.STORED_BYTES: int(
            await session.scalar(
                select(func.coalesce(func.sum(DocumentVersionRecord.size_bytes), 0)).where(
                    DocumentVersionRecord.tenant_id == tenant_id
                )
            )
            or 0
        ),
        QuotaResource.PROVIDER_UNITS: 0,
    }


async def ensure_tenant_control_plane(
    session: AsyncSession,
    tenant: TenantRecord,
    *,
    created_by_subject: str,
    administrator_external_id: str | None = None,
    administrator_display_name: str = "Tenant administrator",
) -> TenantBackfillReport:
    """Idempotently establish lifecycle, configuration, usage and optional administrator."""
    report = TenantBackfillReport(tenants_seen=1)
    tenant.state = tenant.state or "active"
    tenant.revision = max(tenant.revision or 0, 1)
    tenant.acl_epoch = max(tenant.acl_epoch or 0, 1)
    if not tenant.slug:
        tenant.slug = await _unique_slug(session, tenant)

    active_config = await session.scalar(
        select(TenantConfigurationVersionRecord)
        .where(
            TenantConfigurationVersionRecord.tenant_id == tenant.id,
            TenantConfigurationVersionRecord.state == "active",
        )
        .order_by(TenantConfigurationVersionRecord.version.desc())
    )
    if active_config is None:
        config = default_tenant_configuration().model_dump(mode="json")
        active_config = TenantConfigurationVersionRecord(
            id=new_id("tenant_config"),
            tenant_id=tenant.id,
            version=1,
            schema_version=1,
            state="active",
            config=config,
            config_hash=stable_json_hash(config),
            validation_errors=[],
            created_by_subject=created_by_subject,
            activated_at=utc_now(),
        )
        session.add(active_config)
        report.configurations_created += 1
    tenant.active_config_version_id = active_config.id

    default_policy = await session.scalar(
        select(PolicyVersionRecord).where(
            PolicyVersionRecord.tenant_id == tenant.id,
            PolicyVersionRecord.policy_id == DEFAULT_RETRIEVAL_POLICY_ID,
            PolicyVersionRecord.version == DEFAULT_RETRIEVAL_POLICY_VERSION,
        )
    )
    if default_policy is None:
        session.add(
            PolicyVersionRecord(
                id=new_id("policy_version"),
                tenant_id=tenant.id,
                policy_id=DEFAULT_RETRIEVAL_POLICY_ID,
                kind="retrieval",
                version=DEFAULT_RETRIEVAL_POLICY_VERSION,
                state="active",
                config=dict(DEFAULT_RETRIEVAL_POLICY_CONFIG),
                content_hash=stable_json_hash(DEFAULT_RETRIEVAL_POLICY_CONFIG),
            )
        )

    calculated = await calculate_tenant_usage(session, tenant.id)
    now = utc_now()
    for resource, used in calculated.items():
        usage = await session.get(TenantUsageRecord, (tenant.id, resource.value))
        if usage is None:
            usage = TenantUsageRecord(
                tenant_id=tenant.id,
                resource=resource.value,
                used=used,
                reserved=0,
                revision=1,
                reconciled_at=now,
            )
            session.add(usage)
            report.usage_rows_created += 1
        else:
            usage.used = used
            usage.revision += 1
            usage.reconciled_at = now

    if administrator_external_id:
        principal = await session.scalar(
            select(PrincipalRecord).where(
                PrincipalRecord.tenant_id == tenant.id,
                PrincipalRecord.external_id == administrator_external_id,
            )
        )
        if principal is None:
            principal = PrincipalRecord(
                id=new_id("principal"),
                tenant_id=tenant.id,
                external_id=administrator_external_id,
                display_name=administrator_display_name,
                active=True,
                state="active",
                source="bootstrap",
                provenance={"subject": created_by_subject},
            )
            session.add(principal)
            await session.flush()
        membership = await session.scalar(
            select(TenantMembershipRecord).where(
                TenantMembershipRecord.tenant_id == tenant.id,
                TenantMembershipRecord.principal_id == principal.id,
            )
        )
        if membership is None:
            session.add(
                TenantMembershipRecord(
                    id=new_id("tenant_membership"),
                    tenant_id=tenant.id,
                    principal_id=principal.id,
                    state="active",
                    direct_roles=[Role.ADMINISTRATOR.value],
                    source="bootstrap",
                    provenance={"subject": created_by_subject},
                    state_reason="initial tenant administrator",
                )
            )
            tenant.acl_epoch += 1
            report.administrators_created += 1
            members_usage = await session.get(
                TenantUsageRecord, (tenant.id, QuotaResource.MEMBERS.value)
            )
            if members_usage is not None:
                members_usage.used += 1
                members_usage.revision += 1
    await session.flush()
    return report


async def tenants_without_administrator(session: AsyncSession) -> list[str]:
    """Return active tenant ids lacking an active direct tenant administrator."""
    tenant_ids = list(
        (
            await session.scalars(
                select(TenantRecord.id).where(TenantRecord.state != "archived")
            )
        ).all()
    )
    memberships = list(
        (
            await session.scalars(
                select(TenantMembershipRecord).where(
                    TenantMembershipRecord.state == "active"
                )
            )
        ).all()
    )
    covered = {
        item.tenant_id
        for item in memberships
        if Role.ADMINISTRATOR.value in set(item.direct_roles)
    }
    return sorted(set(tenant_ids) - covered)


async def backfill_all_tenants(
    session: AsyncSession,
    *,
    created_by_subject: str = "operator:tenant-backfill",
    administrators: dict[str, str] | None = None,
) -> TenantBackfillReport:
    """Backfill every tenant and report any tenant still lacking an administrator."""
    aggregate = TenantBackfillReport()
    tenants = list((await session.scalars(select(TenantRecord))).all())
    for tenant in tenants:
        item = await ensure_tenant_control_plane(
            session,
            tenant,
            created_by_subject=created_by_subject,
            administrator_external_id=(administrators or {}).get(tenant.id),
        )
        aggregate.tenants_seen += item.tenants_seen
        aggregate.configurations_created += item.configurations_created
        aggregate.usage_rows_created += item.usage_rows_created
        aggregate.administrators_created += item.administrators_created
    aggregate.missing_administrator_tenant_ids = await tenants_without_administrator(session)
    return aggregate
