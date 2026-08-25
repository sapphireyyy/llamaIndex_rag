"""可观测性接口：按租户查询受控的遥测事件。"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from enterprise_rag.api.auth import Identity
from enterprise_rag.api.errors import AppError
from enterprise_rag.domain.types import ErrorCategory, Role, utc_now
from enterprise_rag.infrastructure.orm import TelemetryEventRecord

router = APIRouter(prefix="/api/v1", tags=["telemetry"])


def _sessions(request: Request) -> async_sessionmaker[AsyncSession]:
    """从容器取得遥测查询所需的异步会话工厂。"""
    sessions = request.app.state.container.sessions
    if sessions is None:
        raise AppError(ErrorCategory.DEPENDENCY_UNAVAILABLE, "Telemetry unavailable.", 503)
    return cast(async_sessionmaker[AsyncSession], sessions)


@router.get("/telemetry/events")
async def list_telemetry(
    request: Request, identity: Identity, correlation_id: str | None = None
) -> dict[str, object]:
    """限制为运维或管理员角色，查询保留期内的遥测事件。"""
    if not identity.roles.intersection({Role.OPERATOR, Role.ADMINISTRATOR}):
        raise AppError(ErrorCategory.FORBIDDEN, "Operation is not permitted.", 403)
    cutoff = utc_now() - timedelta(days=request.app.state.settings.telemetry_retention_days)
    statement = (
        select(TelemetryEventRecord)
        .where(
            TelemetryEventRecord.tenant_id == identity.tenant_id,
            TelemetryEventRecord.created_at >= cutoff,
        )
        .order_by(TelemetryEventRecord.created_at.desc())
        .limit(500)
    )
    if correlation_id:
        statement = statement.where(TelemetryEventRecord.correlation_id == correlation_id)
    async with _sessions(request)() as session:
        records = list((await session.scalars(statement)).all())
        return {
            "content_capture_enabled": request.app.state.settings.content_capture_enabled,
            "items": [
                {
                    "id": item.id,
                    "correlation_id": item.correlation_id,
                    "trace_id": item.trace_id,
                    "span_id": item.span_id,
                    "kind": item.kind,
                    "stage": item.stage,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "attributes": item.attributes,
                    "content": item.content,
                    "created_at": item.created_at,
                }
                for item in records
            ],
        }
