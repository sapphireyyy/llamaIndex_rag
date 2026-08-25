"""Platform tenant lifecycle control-plane services."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditEventInput, AuditService
from enterprise_rag.application.tenant_bootstrap import ensure_tenant_control_plane
from enterprise_rag.application.tenant_context import require_platform_permission
from enterprise_rag.domain.types import (
    AuthenticatedSubject,
    ErrorCategory,
    RequestIdentity,
    TenantState,
    new_id,
    require_tenant_transition,
    stable_json_hash,
    utc_now,
)
from enterprise_rag.infrastructure.database import set_transaction_tenant
from enterprise_rag.infrastructure.orm import IdempotencyRecord, TenantRecord
from enterprise_rag.infrastructure.queue import OutboxService
from enterprise_rag.infrastructure.redaction import redact


@dataclass(frozen=True, slots=True)
class TenantCreateCommand:
    name: str
    slug: str | None = None
    metadata: dict[str, object] | None = None
    administrator_external_id: str | None = None


def normalize_tenant_slug(value: str) -> str:
    """Normalize a user-facing slug into a stable URL-safe identifier."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    if not slug or len(slug) > 120:
        raise AppError(ErrorCategory.INVALID_REQUEST, "Tenant slug is invalid.", 422)
    return slug


class PlatformTenantService:
    """Create and transition tenants without granting platform actors data access."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _identity(subject: AuthenticatedSubject, tenant_id: str) -> RequestIdentity:
        return RequestIdentity(tenant_id=tenant_id, user_id=subject.subject_id)

    async def get(self, subject: AuthenticatedSubject, tenant_id: str) -> TenantRecord:
        require_platform_permission(subject, "platform.tenant.read")
        tenant = await self.session.get(TenantRecord, tenant_id)
        if tenant is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Tenant not found.", 404)
        return tenant

    async def list(
        self,
        subject: AuthenticatedSubject,
        *,
        search: str = "",
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TenantRecord]:
        require_platform_permission(subject, "platform.tenant.read")
        statement = select(TenantRecord)
        if search.strip():
            term = f"%{search.strip()}%"
            statement = statement.where(
                or_(TenantRecord.name.ilike(term), TenantRecord.slug.ilike(term))
            )
        if state:
            try:
                TenantState(state)
            except ValueError as exc:
                raise AppError(
                    ErrorCategory.INVALID_REQUEST, "Tenant state is invalid.", 422
                ) from exc
            statement = statement.where(TenantRecord.state == state)
        statement = statement.order_by(TenantRecord.name, TenantRecord.id).offset(offset).limit(
            min(max(limit, 1), 500)
        )
        return list((await self.session.scalars(statement)).all())

    async def create(
        self,
        subject: AuthenticatedSubject,
        command: TenantCreateCommand,
        *,
        correlation_id: str,
        idempotency_key: str | None,
    ) -> TenantRecord:
        require_platform_permission(subject, "platform.tenant.write")
        normalized_name = command.name.strip()
        if not normalized_name:
            raise AppError(ErrorCategory.INVALID_REQUEST, "Tenant name is required.", 422)
        slug = normalize_tenant_slug(command.slug or normalized_name)
        payload_hash = stable_json_hash(
            {
                "name": normalized_name,
                "slug": slug,
                "metadata": command.metadata or {},
                "administrator_external_id": command.administrator_external_id,
            }
        )
        if idempotency_key:
            previous = await self.session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == "__platform__",
                    IdempotencyRecord.operation == "platform.tenant.create",
                    IdempotencyRecord.key == idempotency_key,
                )
            )
            if previous:
                if previous.request_hash != payload_hash:
                    raise AppError(ErrorCategory.CONFLICT, "Idempotency key was reused.", 409)
                existing_id = (
                    previous.response_body.decode("utf-8") if previous.response_body else ""
                )
                existing = await self.session.get(TenantRecord, existing_id)
                if existing is not None:
                    return existing
        if await self.session.scalar(select(TenantRecord.id).where(TenantRecord.slug == slug)):
            raise AppError(ErrorCategory.CONFLICT, "Tenant slug already exists.", 409)
        tenant = TenantRecord(
            id=new_id("tenant"),
            slug=slug,
            name=normalized_name,
            state=TenantState.PROVISIONING.value,
            revision=1,
            settings=redact(command.metadata or {}),
        )
        self.session.add(tenant)
        await self.session.flush()
        await ensure_tenant_control_plane(
            self.session,
            tenant,
            created_by_subject=subject.subject_id,
            administrator_external_id=command.administrator_external_id,
        )
        await self._record_event(
            subject,
            tenant,
            action="tenant.create",
            correlation_id=correlation_id,
            details={"slug": slug, "state": tenant.state},
        )
        if idempotency_key:
            self.session.add(
                IdempotencyRecord(
                    id=new_id("idempotency"),
                    tenant_id="__platform__",
                    operation="platform.tenant.create",
                    key=idempotency_key,
                    request_hash=payload_hash,
                    response_status=201,
                    response_body=tenant.id.encode("utf-8"),
                )
            )
        await self.session.flush()
        return tenant

    async def update_metadata(
        self,
        subject: AuthenticatedSubject,
        tenant_id: str,
        *,
        expected_revision: int,
        name: str | None,
        metadata: dict[str, object] | None,
        correlation_id: str,
    ) -> TenantRecord:
        require_platform_permission(subject, "platform.tenant.write")
        tenant = await self._locked(tenant_id, expected_revision)
        if name is not None:
            normalized = name.strip()
            if not normalized:
                raise AppError(ErrorCategory.INVALID_REQUEST, "Tenant name is required.", 422)
            tenant.name = normalized
        if metadata is not None:
            tenant.settings = redact(metadata)
        tenant.revision += 1
        await self._record_event(
            subject,
            tenant,
            action="tenant.metadata.update",
            correlation_id=correlation_id,
            details={"revision": tenant.revision},
        )
        return tenant

    async def transition(
        self,
        subject: AuthenticatedSubject,
        tenant_id: str,
        target: TenantState,
        *,
        expected_revision: int,
        reason: str,
        correlation_id: str,
    ) -> TenantRecord:
        require_platform_permission(subject, "platform.tenant.lifecycle")
        tenant = await self._locked(tenant_id, expected_revision)
        try:
            require_tenant_transition(TenantState(tenant.state), target)
        except ValueError as exc:
            raise AppError(ErrorCategory.CONFLICT, "Tenant transition is invalid.", 409) from exc
        if target in {TenantState.SUSPENDED, TenantState.ARCHIVED} and not reason.strip():
            raise AppError(ErrorCategory.INVALID_REQUEST, "Transition reason is required.", 422)
        tenant.state = target.value
        tenant.state_reason = reason.strip()
        tenant.suspended_at = utc_now() if target == TenantState.SUSPENDED else None
        tenant.archived_at = utc_now() if target == TenantState.ARCHIVED else None
        tenant.revision += 1
        tenant.acl_epoch += 1
        await self._record_event(
            subject,
            tenant,
            action=f"tenant.{target.value}",
            correlation_id=correlation_id,
            details={"state": target.value, "reason": reason, "revision": tenant.revision},
        )
        return tenant

    async def _locked(self, tenant_id: str, expected_revision: int) -> TenantRecord:
        tenant = await self.session.scalar(
            select(TenantRecord).where(TenantRecord.id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Tenant not found.", 404)
        if tenant.revision != expected_revision:
            raise AppError(ErrorCategory.CONFLICT, "Tenant revision is stale.", 409)
        return tenant

    async def _record_event(
        self,
        subject: AuthenticatedSubject,
        tenant: TenantRecord,
        *,
        action: str,
        correlation_id: str,
        details: dict[str, object],
    ) -> None:
        await set_transaction_tenant(self.session, tenant.id)
        await AuditService(self.session).record(
            self._identity(subject, tenant.id),
            AuditEventInput(
                action,
                "tenant",
                tenant.id,
                "allowed",
                "success",
                correlation_id,
                details,
            ),
        )
        await OutboxService(self.session).add(
            tenant.id,
            action,
            correlation_id,
            {
                "tenant_id": tenant.id,
                "state": tenant.state,
                "revision": tenant.revision,
            },
            priority=100,
        )
