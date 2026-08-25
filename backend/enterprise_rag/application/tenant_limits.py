"""Tenant-configured distributed rate and concurrency leases."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_configuration import TenantConfigurationService
from enterprise_rag.domain.tenant_config import TenantConfiguration
from enterprise_rag.domain.types import ErrorCategory, RequestIdentity


@asynccontextmanager
async def tenant_operation_lease(
    session_factory: async_sessionmaker[AsyncSession],
    coordination: Any,
    identity: RequestIdentity,
    operation: str,
) -> AsyncIterator[TenantConfiguration]:
    """Acquire tenant-prefixed rate and concurrency capacity from active settings."""
    async with session_factory() as session:
        _tenant, _version, config = await TenantConfigurationService(
            session
        )._load_active(identity.tenant_id)
    if operation == "query":
        rate_limit = config.rates.query_per_minute
        concurrency_limit = config.concurrency.query
    elif operation == "upload":
        rate_limit = config.rates.upload_per_minute
        concurrency_limit = config.concurrency.ingestion
    else:
        rate_limit = config.rates.administration_per_minute
        concurrency_limit = 1
    if not await coordination.check_rate(identity.tenant_id, operation, rate_limit):
        raise AppError(ErrorCategory.RATE_LIMITED, "Tenant rate limit exceeded.", 429)
    acquired = await coordination.acquire_concurrency(
        identity.tenant_id, operation, concurrency_limit
    )
    if not acquired:
        raise AppError(ErrorCategory.RATE_LIMITED, "Tenant concurrency limit exceeded.", 429)
    try:
        yield config
    finally:
        await coordination.release_concurrency(identity.tenant_id, operation)
