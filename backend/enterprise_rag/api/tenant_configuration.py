"""Tenant configuration versions, effective settings, usage and fleet APIs."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity, PlatformSubject
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.tenant_configuration import (
    TenantConfigurationService,
    TenantQuotaService,
    fleet_summary,
)
from enterprise_rag.domain.types import ErrorCategory
from enterprise_rag.infrastructure.orm import TenantConfigurationVersionRecord

router = APIRouter(prefix="/api/v1", tags=["tenant-configuration"])


class ConfigurationVersionInput(BaseModel):
    config: dict[str, Any]
    activate: bool = False


class RollbackInput(BaseModel):
    source_version_id: str = Field(min_length=1, max_length=80)


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(
            ErrorCategory.DEPENDENCY_UNAVAILABLE,
            "Tenant configuration service unavailable.",
            503,
        )
    return cast(async_sessionmaker[AsyncSession], sessions)


def _revision(value: str | None) -> int:
    if value is None:
        raise AppError("precondition_required", "If-Match revision is required.", 428)
    try:
        revision = int(value.strip().strip('"'))
    except ValueError as exc:
        raise AppError(ErrorCategory.INVALID_REQUEST, "If-Match revision is invalid.", 422) from exc
    if revision < 1:
        raise AppError(ErrorCategory.INVALID_REQUEST, "If-Match revision is invalid.", 422)
    return revision


def _version_view(record: TenantConfigurationVersionRecord) -> dict[str, object]:
    return {
        "id": record.id,
        "version": record.version,
        "schema_version": record.schema_version,
        "state": record.state,
        "config": record.config,
        "config_hash": record.config_hash,
        "validation_errors": record.validation_errors,
        "previous_version_id": record.previous_version_id,
        "created_by_subject": record.created_by_subject,
        "activated_at": record.activated_at,
        "created_at": record.created_at,
    }


@router.get("/tenant/settings/effective")
async def effective_settings(request: Request, identity: Identity) -> dict[str, object]:
    async with _sessions(request)() as session:
        tenant, version, config = await TenantConfigurationService(session).active(identity)
        return {
            "tenant_id": tenant.id,
            "tenant_revision": tenant.revision,
            "configuration_version_id": version.id,
            "version": version.version,
            "config_hash": version.config_hash,
            "config": config.model_dump(mode="json"),
        }


@router.get("/tenant/settings/versions")
async def configuration_history(request: Request, identity: Identity) -> dict[str, object]:
    async with _sessions(request)() as session:
        records = await TenantConfigurationService(session).history(identity)
        return {"items": [_version_view(record) for record in records]}


@router.post("/tenant/settings/versions", status_code=201)
async def create_configuration_version(
    request: Request,
    payload: ConfigurationVersionInput,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    container = request.app.state.container
    async with _sessions(request)() as session:
        record = await TenantConfigurationService(
            session, container.coordination
        ).create_version(
            identity,
            payload.config,
            expected_tenant_revision=_revision(if_match),
            activate=payload.activate,
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _version_view(record)


@router.post("/tenant/settings/versions/{version_id}/activate")
async def activate_configuration_version(
    request: Request,
    version_id: str,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    container = request.app.state.container
    async with _sessions(request)() as session:
        record = await TenantConfigurationService(
            session, container.coordination
        ).activate_version(
            identity,
            version_id,
            expected_tenant_revision=_revision(if_match),
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _version_view(record)


@router.post("/tenant/settings/rollback", status_code=201)
async def rollback_configuration(
    request: Request,
    payload: RollbackInput,
    identity: Identity,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> dict[str, object]:
    container = request.app.state.container
    async with _sessions(request)() as session:
        record = await TenantConfigurationService(session, container.coordination).rollback(
            identity,
            payload.source_version_id,
            expected_tenant_revision=_revision(if_match),
            correlation_id=request.state.correlation_id,
        )
        await session.commit()
        return _version_view(record)


@router.get("/tenant/settings/validation-status")
async def configuration_validation_status(
    request: Request, identity: Identity
) -> dict[str, object]:
    async with _sessions(request)() as session:
        tenant, version, _config = await TenantConfigurationService(session).active(identity)
        return {
            "valid": not version.validation_errors,
            "errors": version.validation_errors,
            "tenant_revision": tenant.revision,
            "configuration_version_id": version.id,
            "config_hash": version.config_hash,
        }


@router.get("/tenant/usage")
async def tenant_usage(request: Request, identity: Identity) -> dict[str, object]:
    async with _sessions(request)() as session:
        records = await TenantQuotaService(session).usage(identity)
        _tenant, _version, config = await TenantConfigurationService(session).active(identity)
        return {
            "items": [
                {
                    "resource": record.resource,
                    "used": record.used,
                    "reserved": record.reserved,
                    "limit": int(getattr(config.quotas, record.resource)),
                    "revision": record.revision,
                    "reconciled_at": record.reconciled_at,
                    "warning": (
                        (record.used + record.reserved) * 100
                        / int(getattr(config.quotas, record.resource))
                    )
                    >= config.quotas.warning_percent
                    if int(getattr(config.quotas, record.resource)) > 0
                    else False,
                }
                for record in records
            ]
        }


@router.post("/tenant/usage/reconcile")
async def reconcile_tenant_usage(
    request: Request, identity: Identity
) -> dict[str, object]:
    async with _sessions(request)() as session:
        results = await TenantQuotaService(session).reconcile(
            identity, correlation_id=request.state.correlation_id
        )
        await session.commit()
        return {"items": results}


@router.get("/platform/fleet-summary")
async def platform_fleet_summary(
    request: Request, subject: PlatformSubject
) -> dict[str, object]:
    async with _sessions(request)() as session:
        return {"items": await fleet_summary(session, subject)}
