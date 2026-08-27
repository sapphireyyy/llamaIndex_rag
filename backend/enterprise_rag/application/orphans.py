"""原始对象孤儿清理：只删除超过保留期且未被 PostgreSQL 引用的对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.application.ports import ObjectStore, ObjectStoreInventory
from enterprise_rag.infrastructure.orm import DocumentVersionRecord


@dataclass(frozen=True, slots=True)
class OrphanSweepResult:
    """一次清理扫描的非敏感统计结果。"""

    scanned: int
    referenced: int
    retained: int
    deleted: int
    unsupported: bool = False


class OrphanObjectSweeper:
    """按租户清理数据库提交失败后遗留的原始对象。"""

    def __init__(
        self,
        session: AsyncSession,
        object_store: ObjectStore,
        retention_seconds: int = 86_400,
    ) -> None:
        """绑定数据库引用查询、对象存储和保留期限。"""
        self.session = session
        self.object_store = object_store
        self.retention_seconds = retention_seconds

    async def sweep(
        self,
        tenant_id: str,
        *,
        now: datetime | None = None,
        limit: int = 1_000,
    ) -> OrphanSweepResult:
        """扫描租户前缀并删除安全窗口以前未被任何版本引用的对象。"""
        if not hasattr(self.object_store, "list_objects"):
            return OrphanSweepResult(0, 0, 0, 0, unsupported=True)
        referenced = {
            key
            for row in (
                await self.session.execute(
                    select(
                        DocumentVersionRecord.source_key,
                        DocumentVersionRecord.parsed_key,
                    ).where(DocumentVersionRecord.tenant_id == tenant_id)
                )
            ).all()
            for key in row
            if key
        }
        cutoff = (now or datetime.now(UTC)) - timedelta(
            seconds=self.retention_seconds
        )
        entries = await (
            cast(ObjectStoreInventory, self.object_store).list_objects(
                f"tenants/{tenant_id}/"
            )
        )
        deleted = 0
        retained = 0
        for entry in entries[:limit]:
            if entry.key in referenced or entry.last_modified > cutoff:
                retained += 1
                continue
            await self.object_store.delete(entry.key)
            deleted += 1
        return OrphanSweepResult(len(entries[:limit]), len(referenced), retained, deleted)
