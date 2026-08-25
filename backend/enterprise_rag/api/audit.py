"""审计日志接口：查询并导出当前租户的审计事件。"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.application.audit import AuditService
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.application.tenant_configuration import TenantConfigurationService
from enterprise_rag.domain.types import ErrorCategory

router = APIRouter(prefix="/api/v1/audit-events", tags=["audit"])


def _services(
    request: Request,
) -> tuple[async_sessionmaker[AsyncSession], DatabaseAuthorizationService]:
    """取得审计查询使用的会话工厂和数据库授权服务。"""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Audit service is unavailable.", 503)
    return sessions, DatabaseAuthorizationService(sessions)


@router.get("")
async def list_audit_events(
    request: Request, identity: Identity, limit: int = 200
) -> dict[str, object]:
    """校验审计读取权限后，返回当前租户最近的审计事件。"""
    sessions, authorization = _services(request)
    await authorization.require(identity, "audit.read", identity.tenant_id, identity.tenant_id)
    async with sessions() as session:
        records = await AuditService(session).list_for_tenant(identity.tenant_id, limit)
        return {
            "items": [
                {
                    "id": record.id,
                    "principal_id": record.principal_id,
                    "action": record.action,
                    "resource_type": record.resource_type,
                    "resource_id": record.resource_id,
                    "authorization_result": record.authorization_result,
                    "outcome": record.outcome,
                    "correlation_id": record.correlation_id,
                    "details": record.details,
                    "created_at": record.created_at,
                }
                for record in records
            ]
        }


@router.get("/export")
async def export_audit_events(request: Request, identity: Identity) -> StreamingResponse:
    """校验审计读取权限，并把最近事件导出为 CSV 下载流。"""
    sessions, authorization = _services(request)
    await authorization.require(identity, "audit.read", identity.tenant_id, identity.tenant_id)
    async with sessions() as session:
        records = await AuditService(session).list_for_tenant(identity.tenant_id, 1_000)
        _tenant, configuration, _effective = await TenantConfigurationService(
            session
        ).active(identity)
        integrity_hash = records[0].event_hash if records else ""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "id",
            "principal_id",
            "action",
            "resource_type",
            "resource_id",
            "outcome",
            "created_at",
            "configuration_version_id",
            "integrity_event_hash",
        ]
    )
    for record in records:
        writer.writerow(
            [
                record.id,
                record.principal_id,
                record.action,
                record.resource_type,
                record.resource_id,
                record.outcome,
                record.created_at.isoformat(),
                configuration.id,
                integrity_hash,
            ]
        )
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-events.csv"},
    )
