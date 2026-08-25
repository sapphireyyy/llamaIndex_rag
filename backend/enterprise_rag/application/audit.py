"""审计应用服务：记录带哈希链的租户级操作事件。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.domain.types import RequestIdentity, new_id, stable_json_hash, utc_now
from enterprise_rag.infrastructure.orm import AuditEventRecord
from enterprise_rag.infrastructure.redaction import redact


@dataclass(frozen=True, slots=True)
class AuditEventInput:
    """待记录的审计事件字段，details 会在落库前脱敏。"""

    action: str
    resource_type: str
    resource_id: str
    authorization_result: str
    outcome: str
    correlation_id: str
    details: dict[str, Any]


class AuditService:
    """负责审计事件的哈希链写入、查询和完整性验证。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存当前事务会话，保证审计记录与业务变更同事务提交。"""
        self.session = session

    @staticmethod
    def _timestamp(value: datetime) -> str:
        """把可能无时区的时间统一转换为 UTC ISO 字符串。"""
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat()

    async def record(self, identity: RequestIdentity, event: AuditEventInput) -> AuditEventRecord:
        """读取上一条哈希，脱敏当前事件并追加到租户哈希链。"""
        previous = await self.session.scalar(
            select(AuditEventRecord)
            .where(AuditEventRecord.tenant_id == identity.tenant_id)
            .order_by(AuditEventRecord.created_at.desc())
            .limit(1)
        )
        previous_hash = previous.event_hash if previous else ""
        created_at = utc_now()
        safe_details = redact(event.details)
        event_hash = stable_json_hash(
            {
                "tenant_id": identity.tenant_id,
                "principal_id": identity.user_id,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "authorization_result": event.authorization_result,
                "outcome": event.outcome,
                "correlation_id": event.correlation_id,
                "details": safe_details,
                "previous_hash": previous_hash,
                "created_at": self._timestamp(created_at),
            }
        )
        record = AuditEventRecord(
            id=new_id("audit"),
            tenant_id=identity.tenant_id,
            principal_id=identity.user_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            authorization_result=event.authorization_result,
            outcome=event.outcome,
            correlation_id=event.correlation_id,
            details=safe_details,
            previous_hash=previous_hash,
            event_hash=event_hash,
            created_at=created_at,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def list_for_tenant(self, tenant_id: str, limit: int = 200) -> list[AuditEventRecord]:
        """按时间倒序查询租户审计事件，并限制最大返回数量。"""
        statement = (
            select(AuditEventRecord)
            .where(AuditEventRecord.tenant_id == tenant_id)
            .order_by(AuditEventRecord.created_at.desc())
            .limit(min(limit, 1_000))
        )
        return list((await self.session.scalars(statement)).all())

    async def verify_chain(self, tenant_id: str) -> bool:
        """逐条重算事件哈希，确认审计链没有断裂或被篡改。"""
        records = list(
            (
                await self.session.scalars(
                    select(AuditEventRecord)
                    .where(AuditEventRecord.tenant_id == tenant_id)
                    .order_by(AuditEventRecord.created_at)
                )
            ).all()
        )
        previous_hash = ""
        for record in records:
            if record.previous_hash != previous_hash:
                return False
            expected = stable_json_hash(
                {
                    "tenant_id": record.tenant_id,
                    "principal_id": record.principal_id,
                    "action": record.action,
                    "resource_type": record.resource_type,
                    "resource_id": record.resource_id,
                    "authorization_result": record.authorization_result,
                    "outcome": record.outcome,
                    "correlation_id": record.correlation_id,
                    "details": record.details,
                    "previous_hash": record.previous_hash,
                    "created_at": self._timestamp(record.created_at),
                }
            )
            if expected != record.event_hash:
                return False
            previous_hash = record.event_hash
        return True
