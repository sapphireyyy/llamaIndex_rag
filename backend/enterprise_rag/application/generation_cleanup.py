"""处理代次保留期清理：先保护活动指针和审计引用，再删除外部投影。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.application.ports import SearchAdapter
from enterprise_rag.infrastructure.orm import (
    AuditEventRecord,
    ChunkRecord,
    DocumentRecord,
    IndexGenerationRecord,
)


@dataclass(frozen=True, slots=True)
class GenerationCleanupResult:
    """一次旧代次清理的非敏感统计。"""

    scanned: int
    retained: int
    deleted: int
    unsupported: int = 0


class IndexGenerationCleanupService:
    """清理超过保留期、非活动且没有审计引用的处理代次。"""

    def __init__(
        self,
        session: AsyncSession,
        dense_index: SearchAdapter,
        lexical_index: SearchAdapter,
        retention_seconds: int = 7 * 86_400,
    ) -> None:
        """绑定权威数据库、双索引和不可逆删除的安全窗口。"""
        self.session = session
        self.indexes = (dense_index, lexical_index)
        self.retention_seconds = retention_seconds

    async def sweep(
        self,
        tenant_id: str,
        *,
        now: datetime | None = None,
        limit: int = 100,
        generation_id: str | None = None,
    ) -> GenerationCleanupResult:
        """清理满足保留期和引用条件的代次，外部删除失败时保留数据库事实。"""
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=self.retention_seconds)
        statement = (
            select(IndexGenerationRecord)
            .where(
                IndexGenerationRecord.tenant_id == tenant_id,
                IndexGenerationRecord.state == "superseded",
                IndexGenerationRecord.superseded_at.is_not(None),
                IndexGenerationRecord.superseded_at <= cutoff,
            )
            .order_by(IndexGenerationRecord.superseded_at)
            .limit(limit)
        )
        if generation_id is not None:
            statement = statement.where(IndexGenerationRecord.id == generation_id)
        candidates = list((await self.session.scalars(statement)).all())
        retained = 0
        deleted = 0
        unsupported = 0
        for generation in candidates:
            document = await self.session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == generation.document_id,
                    DocumentRecord.tenant_id == tenant_id,
                )
            )
            audit_count = int(
                await self.session.scalar(
                    select(func.count(AuditEventRecord.id)).where(
                        AuditEventRecord.tenant_id == tenant_id,
                        AuditEventRecord.resource_type == "index_generation",
                        AuditEventRecord.resource_id == generation.id,
                    )
                )
                or 0
            )
            if document is None or document.active_generation_id == generation.id or audit_count:
                retained += 1
                continue
            deleters: list[Callable[[str, str], Awaitable[None]]] = []
            for index in self.indexes:
                deleter = getattr(index, "delete_generation", None)
                if not callable(deleter):
                    deleters = []
                    break
                deleters.append(cast(Callable[[str, str], Awaitable[None]], deleter))
            if len(deleters) != len(self.indexes):
                retained += 1
                unsupported += 1
                continue
            for deleter in deleters:
                await deleter(generation.document_id, generation.id)
            await self.session.execute(
                delete(ChunkRecord).where(
                    ChunkRecord.tenant_id == tenant_id,
                    ChunkRecord.index_generation_id == generation.id,
                )
            )
            await self.session.delete(generation)
            deleted += 1
        await self.session.flush()
        return GenerationCleanupResult(len(candidates), retained, deleted, unsupported)
