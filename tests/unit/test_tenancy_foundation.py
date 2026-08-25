from __future__ import annotations

import pytest
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_bootstrap import backfill_all_tenants
from enterprise_rag.application.tenant_configuration import TenantQuotaService
from enterprise_rag.application.worker_context import (
    WorkerContextRejected,
    validate_worker_context,
)
from enterprise_rag.domain.tenant_config import TenantConfiguration
from enterprise_rag.domain.types import JobMessage, QuotaResource, RequestIdentity, Role
from enterprise_rag.infrastructure.cache import MemoryCoordinationStore, tenant_coordination_key
from enterprise_rag.infrastructure.database import tenant_session_scope
from enterprise_rag.infrastructure.orm import (
    TenantConfigurationVersionRecord,
    TenantMembershipRecord,
    TenantRecord,
    TenantUsageRecord,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_backfill_creates_configuration_usage_and_administrator(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-a", name="Tenant A"))
        await session.commit()

    async with sessions() as session:
        report = await backfill_all_tenants(
            session, administrators={"tenant-a": "oidc|alice"}
        )
        await session.commit()
        assert report.configurations_created == 1
        assert report.usage_rows_created == 6
        assert report.administrators_created == 1
        assert report.missing_administrator_tenant_ids == []

    async with sessions() as session:
        tenant = await session.get(TenantRecord, "tenant-a")
        assert tenant is not None
        assert tenant.state == "active"
        assert tenant.active_config_version_id
        assert len((await session.scalars(select(TenantUsageRecord))).all()) == 6
        configuration = await session.get(
            TenantConfigurationVersionRecord, tenant.active_config_version_id
        )
        membership = await session.scalar(select(TenantMembershipRecord))
        assert configuration is not None and configuration.version == 1
        assert membership is not None and membership.direct_roles == ["administrator"]


@pytest.mark.asyncio
async def test_sqlite_tenant_session_scope_preserves_context_without_rls(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with tenant_session_scope(sessions, "tenant-a") as session:
        assert session.info["tenant_id"] == "tenant-a"


def test_tenant_configuration_rejects_unknown_fields_and_raw_secrets() -> None:
    with pytest.raises(ValueError, match="raw secret field"):
        TenantConfiguration.model_validate({"provider_policy": {"api_key": "raw"}})
    with pytest.raises(ValueError, match="Extra inputs"):
        TenantConfiguration.model_validate({"unsupported": True})


@pytest.mark.asyncio
async def test_quota_reservations_are_locked_and_tenant_coordination_is_prefixed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-q", slug="tenant-q", name="Tenant Q"))
        await session.flush()
        await backfill_all_tenants(session)
        tenant = await session.get(TenantRecord, "tenant-q")
        assert tenant is not None
        identity = RequestIdentity(
            "tenant-q",
            "admin",
            roles=frozenset({Role.ADMINISTRATOR}),
            configuration_version_id=tenant.active_config_version_id,
        )
        reservation = await TenantQuotaService(session).reserve(
            identity, QuotaResource.MEMBERS, 1000
        )
        with pytest.raises(AppError, match="quota exceeded"):
            await TenantQuotaService(session).reserve(identity, QuotaResource.MEMBERS, 1)
        usage = await TenantQuotaService(session).release(reservation, consume=False)
        assert usage.reserved == 0

    coordination = MemoryCoordinationStore()
    await coordination.start()
    assert tenant_coordination_key("tenant-q", "rate", "query").startswith(
        "rag:tenant:tenant-q:"
    )
    assert await coordination.check_rate("tenant-q", "query", 1)
    assert not await coordination.check_rate("tenant-q", "query", 1)
    assert await coordination.check_rate("tenant-other", "query", 1)


@pytest.mark.asyncio
async def test_worker_revalidates_snapshot_and_rejects_suspended_tenant(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        session.add(TenantRecord(id="tenant-worker", slug="tenant-worker", name="Worker"))
        await session.flush()
        await backfill_all_tenants(session)
        tenant = await session.get(TenantRecord, "tenant-worker")
        assert tenant is not None and tenant.active_config_version_id
        message = JobMessage(
            "job",
            "ingest",
            tenant.id,
            "correlation",
            {
                "authorization_epoch": tenant.acl_epoch,
                "configuration_version_id": tenant.active_config_version_id,
                "resource_version_id": "document-version-1",
                "policy_version_ids": ["policy-1"],
            },
        )
        snapshot = await validate_worker_context(
            session, message, require_resource_version=True
        )
        assert snapshot.tenant_id == tenant.id
        tenant.state = "suspended"
        await session.flush()
        with pytest.raises(WorkerContextRejected, match="tenant_suspended_rejected"):
            await validate_worker_context(session, message)

    with pytest.raises(ValueError, match="does not match"):
        JobMessage("bad", "ingest", "tenant-a", "c", {"tenant_id": "tenant-b"})
