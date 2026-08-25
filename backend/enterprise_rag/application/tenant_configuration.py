"""Immutable tenant configuration, quota reservation and usage reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.tenant_bootstrap import calculate_tenant_usage
from enterprise_rag.application.tenant_context import (
    require_platform_permission,
    require_tenant_permission,
)
from enterprise_rag.domain.tenant_config import TenantConfiguration
from enterprise_rag.domain.types import (
    AuthenticatedSubject,
    ErrorCategory,
    QuotaResource,
    RequestIdentity,
    new_id,
    stable_json_hash,
    utc_now,
)
from enterprise_rag.infrastructure.orm import (
    PolicyVersionRecord,
    ProviderProfileRecord,
    SecretBindingRecord,
    TenantConfigurationVersionRecord,
    TenantRecord,
    TenantUsageRecord,
)
from enterprise_rag.infrastructure.queue import OutboxService


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    tenant_id: str
    resource: QuotaResource
    amount: int
    usage_revision: int


class TenantConfigurationService:
    """Validate, version, activate and roll back complete tenant settings."""

    def __init__(self, session: AsyncSession, coordination: Any | None = None) -> None:
        self.session = session
        self.coordination = coordination

    async def active(
        self, identity: RequestIdentity
    ) -> tuple[TenantRecord, TenantConfigurationVersionRecord, TenantConfiguration]:
        require_tenant_permission(identity, "configuration.read")
        return await self._load_active(identity.tenant_id)

    async def _load_active(
        self, tenant_id: str
    ) -> tuple[TenantRecord, TenantConfigurationVersionRecord, TenantConfiguration]:
        tenant = await self._tenant(tenant_id)
        if not tenant.active_config_version_id:
            raise AppError(
                ErrorCategory.DEPENDENCY_UNAVAILABLE,
                "Active tenant configuration is unavailable.",
                503,
            )
        version = await self.session.scalar(
            select(TenantConfigurationVersionRecord).where(
                TenantConfigurationVersionRecord.id == tenant.active_config_version_id,
                TenantConfigurationVersionRecord.tenant_id == tenant.id,
                TenantConfigurationVersionRecord.state == "active",
            )
        )
        if version is None:
            raise AppError(
                ErrorCategory.DEPENDENCY_UNAVAILABLE,
                "Active tenant configuration is unavailable.",
                503,
            )
        try:
            config = TenantConfiguration.model_validate(version.config)
        except ValidationError as exc:
            raise AppError(
                ErrorCategory.DEPENDENCY_UNAVAILABLE,
                "Active tenant configuration is invalid.",
                503,
            ) from exc
        return tenant, version, config

    async def history(
        self, identity: RequestIdentity
    ) -> list[TenantConfigurationVersionRecord]:
        require_tenant_permission(identity, "configuration.read")
        return list(
            (
                await self.session.scalars(
                    select(TenantConfigurationVersionRecord)
                    .where(
                        TenantConfigurationVersionRecord.tenant_id == identity.tenant_id
                    )
                    .order_by(TenantConfigurationVersionRecord.version.desc())
                )
            ).all()
        )

    async def create_version(
        self,
        identity: RequestIdentity,
        raw_config: dict[str, Any],
        *,
        expected_tenant_revision: int,
        activate: bool,
        correlation_id: str,
    ) -> TenantConfigurationVersionRecord:
        require_tenant_permission(identity, "configuration.write")
        try:
            config = TenantConfiguration.model_validate(raw_config)
        except ValidationError as exc:
            raise AppError(
                ErrorCategory.INVALID_REQUEST,
                "Tenant configuration validation failed.",
                422,
            ) from exc
        tenant = await self._tenant(
            identity.tenant_id, for_update=True, expected_revision=expected_tenant_revision
        )
        await self._validate_references(identity.tenant_id, config)
        next_version = int(
            await self.session.scalar(
                select(func.max(TenantConfigurationVersionRecord.version)).where(
                    TenantConfigurationVersionRecord.tenant_id == identity.tenant_id
                )
            )
            or 0
        ) + 1
        resolved = config.model_dump(mode="json")
        record = TenantConfigurationVersionRecord(
            id=new_id("tenant_config"),
            tenant_id=identity.tenant_id,
            version=next_version,
            schema_version=config.schema_version,
            state="draft",
            config=resolved,
            config_hash=stable_json_hash(resolved),
            validation_errors=[],
            previous_version_id=tenant.active_config_version_id,
            created_by_subject=identity.user_id,
        )
        self.session.add(record)
        await self.session.flush()
        if activate:
            await self._activate(identity, tenant, record, correlation_id)
        else:
            tenant.revision += 1
            await self._audit(
                identity,
                "tenant.configuration.create",
                record.id,
                correlation_id,
                {"version": record.version, "config_hash": record.config_hash},
            )
        return record

    async def activate_version(
        self,
        identity: RequestIdentity,
        version_id: str,
        *,
        expected_tenant_revision: int,
        correlation_id: str,
    ) -> TenantConfigurationVersionRecord:
        require_tenant_permission(identity, "configuration.write")
        tenant = await self._tenant(
            identity.tenant_id, for_update=True, expected_revision=expected_tenant_revision
        )
        record = await self.session.scalar(
            select(TenantConfigurationVersionRecord).where(
                TenantConfigurationVersionRecord.id == version_id,
                TenantConfigurationVersionRecord.tenant_id == identity.tenant_id,
            )
        )
        if record is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        try:
            config = TenantConfiguration.model_validate(record.config)
        except ValidationError as exc:
            raise AppError(
                ErrorCategory.INVALID_REQUEST,
                "Tenant configuration validation failed.",
                422,
            ) from exc
        await self._validate_references(identity.tenant_id, config)
        await self._activate(identity, tenant, record, correlation_id)
        return record

    async def rollback(
        self,
        identity: RequestIdentity,
        source_version_id: str,
        *,
        expected_tenant_revision: int,
        correlation_id: str,
    ) -> TenantConfigurationVersionRecord:
        require_tenant_permission(identity, "configuration.write")
        source = await self.session.scalar(
            select(TenantConfigurationVersionRecord).where(
                TenantConfigurationVersionRecord.id == source_version_id,
                TenantConfigurationVersionRecord.tenant_id == identity.tenant_id,
            )
        )
        if source is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        return await self.create_version(
            identity,
            dict(source.config),
            expected_tenant_revision=expected_tenant_revision,
            activate=True,
            correlation_id=correlation_id,
        )

    async def _activate(
        self,
        identity: RequestIdentity,
        tenant: TenantRecord,
        record: TenantConfigurationVersionRecord,
        correlation_id: str,
    ) -> None:
        current = None
        if tenant.active_config_version_id:
            current = await self.session.get(
                TenantConfigurationVersionRecord, tenant.active_config_version_id
            )
        if current is not None and current.id != record.id:
            current.state = "superseded"
        record.state = "active"
        record.activated_at = utc_now()
        tenant.active_config_version_id = record.id
        tenant.revision += 1
        await self._audit(
            identity,
            "tenant.configuration.activate",
            record.id,
            correlation_id,
            {"version": record.version, "config_hash": record.config_hash},
        )
        await OutboxService(self.session).add(
            identity.tenant_id,
            "tenant.configuration.changed",
            correlation_id,
            {
                "tenant_id": identity.tenant_id,
                "configuration_version_id": record.id,
                "tenant_revision": tenant.revision,
            },
            priority=100,
        )
        if self.coordination is not None:
            invalidator = getattr(self.coordination, "invalidate_tenant", None)
            if invalidator is not None:
                await invalidator(identity.tenant_id)

    async def _validate_references(
        self, tenant_id: str, config: TenantConfiguration
    ) -> None:
        binding_ids = set(config.secret_binding_ids)
        if binding_ids:
            count = int(
                await self.session.scalar(
                    select(func.count(SecretBindingRecord.id)).where(
                        SecretBindingRecord.tenant_id == tenant_id,
                        SecretBindingRecord.id.in_(binding_ids),
                        SecretBindingRecord.valid.is_(True),
                    )
                )
                or 0
            )
            if count != len(binding_ids):
                raise AppError(
                    ErrorCategory.INVALID_REQUEST,
                    "Secret binding reference is invalid.",
                    422,
                )
        profile_ids = set(config.provider_policy.allowed_profile_ids)
        if profile_ids:
            count = int(
                await self.session.scalar(
                    select(func.count(ProviderProfileRecord.id)).where(
                        ProviderProfileRecord.tenant_id == tenant_id,
                        ProviderProfileRecord.id.in_(profile_ids),
                        ProviderProfileRecord.enabled.is_(True),
                    )
                )
                or 0
            )
            if count != len(profile_ids):
                raise AppError(
                    ErrorCategory.INVALID_REQUEST,
                    "Provider profile reference is invalid.",
                    422,
                )
        policy_ids = set(config.default_rag_policy_ids.values())
        if policy_ids:
            count = int(
                await self.session.scalar(
                    select(func.count(PolicyVersionRecord.id)).where(
                        PolicyVersionRecord.tenant_id == tenant_id,
                        PolicyVersionRecord.id.in_(policy_ids),
                        PolicyVersionRecord.state == "active",
                    )
                )
                or 0
            )
            if count != len(policy_ids):
                raise AppError(
                    ErrorCategory.INVALID_REQUEST,
                    "Default RAG policy reference is invalid.",
                    422,
                )

    async def _tenant(
        self,
        tenant_id: str,
        *,
        for_update: bool = False,
        expected_revision: int | None = None,
    ) -> TenantRecord:
        statement = select(TenantRecord).where(TenantRecord.id == tenant_id)
        if for_update:
            statement = statement.with_for_update()
        tenant = await self.session.scalar(statement)
        if tenant is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        if expected_revision is not None and tenant.revision != expected_revision:
            raise AppError(ErrorCategory.CONFLICT, "Tenant revision is stale.", 409)
        return tenant

    async def _audit(
        self,
        identity: RequestIdentity,
        action: str,
        resource_id: str,
        correlation_id: str,
        details: dict[str, object],
    ) -> None:
        await AuditService(self.session).record(
            identity,
            AuditEventInput(
                action,
                "tenant_configuration",
                resource_id,
                "allowed",
                "success",
                correlation_id,
                details,
            ),
        )


class TenantQuotaService:
    """Reserve and reconcile durable quotas under row locks before expensive work."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def reserve(
        self, identity: RequestIdentity, resource: QuotaResource, amount: int
    ) -> QuotaReservation:
        if amount <= 0:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Quota amount must be positive.", 422)
        _tenant, _version, config = await TenantConfigurationService(
            self.session
        )._load_active(identity.tenant_id)
        usage = await self._locked_usage(identity.tenant_id, resource)
        limit = int(getattr(config.quotas, resource.value))
        if usage.used + usage.reserved + amount > limit:
            raise AppError("quota_exceeded", f"Tenant {resource.value} quota exceeded.", 429)
        usage.reserved += amount
        usage.revision += 1
        await self.session.flush()
        return QuotaReservation(identity.tenant_id, resource, amount, usage.revision)

    async def release(
        self, reservation: QuotaReservation, *, consume: bool = False
    ) -> TenantUsageRecord:
        usage = await self._locked_usage(reservation.tenant_id, reservation.resource)
        if usage.reserved < reservation.amount:
            raise AppError(ErrorCategory.CONFLICT, "Quota reservation is stale.", 409)
        usage.reserved -= reservation.amount
        if consume:
            usage.used += reservation.amount
        usage.revision += 1
        await self.session.flush()
        return usage

    async def reconcile(
        self, identity: RequestIdentity, *, correlation_id: str
    ) -> list[dict[str, object]]:
        require_tenant_permission(identity, "usage.read")
        calculated = await calculate_tenant_usage(self.session, identity.tenant_id)
        _tenant, _version, config = await TenantConfigurationService(
            self.session
        )._load_active(identity.tenant_id)
        results: list[dict[str, object]] = []
        for resource, actual in calculated.items():
            usage = await self._locked_usage(identity.tenant_id, resource)
            previous = usage.used
            usage.used = actual
            usage.revision += 1
            usage.reconciled_at = utc_now()
            limit = int(getattr(config.quotas, resource.value))
            warning = limit > 0 and ((actual + usage.reserved) * 100 / limit) >= (
                config.quotas.warning_percent
            )
            results.append(
                {
                    "resource": resource.value,
                    "used": actual,
                    "reserved": usage.reserved,
                    "limit": limit,
                    "drift": actual - previous,
                    "warning": warning,
                }
            )
            if warning:
                await OutboxService(self.session).add(
                    identity.tenant_id,
                    "tenant.quota.warning",
                    correlation_id,
                    {
                        "tenant_id": identity.tenant_id,
                        "resource": resource.value,
                        "used": actual,
                        "reserved": usage.reserved,
                        "limit": limit,
                    },
                )
        return results

    async def usage(self, identity: RequestIdentity) -> list[TenantUsageRecord]:
        require_tenant_permission(identity, "usage.read")
        return list(
            (
                await self.session.scalars(
                    select(TenantUsageRecord)
                    .where(TenantUsageRecord.tenant_id == identity.tenant_id)
                    .order_by(TenantUsageRecord.resource)
                )
            ).all()
        )

    async def _locked_usage(
        self, tenant_id: str, resource: QuotaResource
    ) -> TenantUsageRecord:
        usage = await self.session.scalar(
            select(TenantUsageRecord)
            .where(
                TenantUsageRecord.tenant_id == tenant_id,
                TenantUsageRecord.resource == resource.value,
            )
            .with_for_update()
        )
        if usage is None:
            usage = TenantUsageRecord(
                tenant_id=tenant_id,
                resource=resource.value,
                used=0,
                reserved=0,
                revision=1,
            )
            self.session.add(usage)
            await self.session.flush()
        return usage


async def fleet_summary(
    session: AsyncSession, subject: AuthenticatedSubject
) -> list[dict[str, object]]:
    """Return content-free fleet lifecycle and aggregate quota counters."""
    require_platform_permission(subject, "platform.fleet.read")
    tenants = list((await session.scalars(select(TenantRecord).order_by(TenantRecord.name))).all())
    output: list[dict[str, object]] = []
    for tenant in tenants:
        usage = list(
            (
                await session.scalars(
                    select(TenantUsageRecord).where(TenantUsageRecord.tenant_id == tenant.id)
                )
            ).all()
        )
        output.append(
            {
                "tenant_id": tenant.id,
                "name": tenant.name,
                "state": tenant.state,
                "revision": tenant.revision,
                "usage": {item.resource: item.used + item.reserved for item in usage},
            }
        )
    return output
