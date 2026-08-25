"""Tenant discovery and platform lifecycle HTTP APIs."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity, PlatformSubject, Subject
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_admin import (
    PlatformTenantService,
    TenantCreateCommand,
)
from enterprise_rag.application.tenant_context import discover_tenants
from enterprise_rag.domain.types import ErrorCategory, TenantState
from enterprise_rag.infrastructure.orm import TenantRecord

router = APIRouter(prefix="/api/v1", tags=["tenants"])


class TenantCreateInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=120)
    metadata: dict[str, object] = Field(default_factory=dict)
    administrator_external_id: str | None = Field(default=None, max_length=255)


class TenantUpdateInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    metadata: dict[str, object] | None = None


class TenantTransitionInput(BaseModel):
    reason: str = Field(default="", max_length=2_000)


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Tenant service unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


def _tenant_view(tenant: TenantRecord) -> dict[str, object]:
    return {
        "id": tenant.id,
        "slug": tenant.slug,
        "name": tenant.name,
        "state": tenant.state,
        "state_reason": tenant.state_reason,
        "revision": tenant.revision,
        "authorization_epoch": tenant.acl_epoch,
        "active_configuration_version_id": tenant.active_config_version_id,
        "metadata": tenant.settings,
        "suspended_at": tenant.suspended_at,
        "archived_at": tenant.archived_at,
        "created_at": tenant.created_at,
        "updated_at": tenant.updated_at,
    }


def _revision(if_match: str | None) -> int:
    if if_match is None:
        raise AppError("precondition_required", "If-Match revision is required.", 428)
    value = if_match.strip().strip('"')
    try:
        revision = int(value)
    except ValueError as exc:
        raise AppError(ErrorCategory.INVALID_REQUEST, "If-Match revision is invalid.", 422) from exc
    if revision < 1:
        raise AppError(ErrorCategory.INVALID_REQUEST, "If-Match revision is invalid.", 422)
    return revision


@router.get("/tenants")
async def tenant_discovery(request: Request, subject: Subject) -> dict[str, object]:
    """List only the tenants connected to the authenticated subject."""
    async with _sessions(request)() as session:
        items = await discover_tenants(session, subject)
    return {
        "platform_roles": sorted(role.value for role in subject.platform_roles),
        "items": [
            {
                "id": item.id,
                "slug": item.slug,
                "name": item.name,
                "state": item.state,
                "revision": item.revision,
                "membership_state": item.membership_state,
                "roles": item.roles,
            }
            for item in items
        ]
    }


@router.get("/tenant/context")
async def current_tenant_context(identity: Identity) -> dict[str, object]:
    """Return the persisted authorization metadata resolved for this request."""
    return {
        "tenant_id": identity.tenant_id,
        "tenant_state": identity.tenant_state,
        "principal_id": identity.principal_id,
        "membership_id": identity.membership_id,
        "roles": sorted(role.value for role in identity.roles),
        "groups": identity.groups,
        "authorization_epoch": identity.authorization_epoch,
        "configuration_version_id": identity.configuration_version_id,
    }


@router.get("/platform/tenants")
async def list_platform_tenants(
    request: Request,
    subject: PlatformSubject,
    search: Annotated[str, Query(max_length=255)] = "",
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        items = await PlatformTenantService(session).list(
            subject, search=search, state=state, limit=limit, offset=offset
        )
    return {"items": [_tenant_view(item) for item in items]}


@router.post("/platform/tenants", status_code=201)
async def create_platform_tenant(
    request: Request,
    payload: TenantCreateInput,
    subject: PlatformSubject,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        tenant = await PlatformTenantService(session).create(
            subject,
            TenantCreateCommand(
                name=payload.name,
                slug=payload.slug,
                metadata=payload.metadata,
                administrator_external_id=payload.administrator_external_id,
            ),
            correlation_id=request.state.correlation_id,
            idempotency_key=idempotency_key,
        )
        await session.commit()
        return _tenant_view(tenant)


@router.get("/platform/tenants/{tenant_id}")
async def get_platform_tenant(
    request: Request, tenant_id: str, subject: PlatformSubject
) -> dict[str, object]:
    async with _sessions(request)() as session:
        tenant = await PlatformTenantService(session).get(subject, tenant_id)
        return _tenant_view(tenant)


@router.patch("/platform/tenants/{tenant_id}")
async def update_platform_tenant(
    request: Request,
    tenant_id: str,
    payload: TenantUpdateInput,
    subject: PlatformSubject,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    async with _sessions(request)() as session:
        tenant = await PlatformTenantService(session).update_metadata(
            subject,
            tenant_id,
            expected_revision=_revision(if_match),
            name=payload.name,
            metadata=payload.metadata,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _tenant_view(tenant)


@router.post("/platform/tenants/{tenant_id}/actions/{action}")
async def transition_platform_tenant(
    request: Request,
    tenant_id: str,
    action: str,
    payload: TenantTransitionInput,
    subject: PlatformSubject,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    targets = {
        "activate": TenantState.ACTIVE,
        "suspend": TenantState.SUSPENDED,
        "reactivate": TenantState.ACTIVE,
        "archive": TenantState.ARCHIVED,
        "restore": TenantState.ACTIVE,
    }
    target = targets.get(action)
    if target is None:
        raise AppError(ErrorCategory.INVALID_REQUEST, "Tenant action is invalid.", 422)
    async with _sessions(request)() as session:
        tenant = await PlatformTenantService(session).transition(
            subject,
            tenant_id,
            target,
            expected_revision=_revision(if_match),
            reason=payload.reason,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _tenant_view(tenant)
