"""PostgreSQL 权威记录与外部索引投影的数量、metadata 对账。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from enterprise_rag.api.errors import AppError
from enterprise_rag.application.ports import SearchAdapter
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.domain.types import ErrorCategory, RequestIdentity, stable_json_hash
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DocumentRecord,
    IndexGenerationRecord,
)
from enterprise_rag.infrastructure.telemetry import record_reconciliation


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """一次文档代次对账的非敏感结果。"""

    tenant_id: str
    document_id: str
    generation_id: str | None
    expected_count: int
    expected_metadata_hash: str
    indexes: dict[str, dict[str, object]]

    @property
    def stale(self) -> bool:
        """只要任一投影缺失、数量或关键 metadata 不一致即视为陈旧。"""
        return any(bool(item.get("stale")) for item in self.indexes.values())

    def as_dict(self) -> dict[str, object]:
        """转换为 API 和审计可用的结构化报告。"""
        return {
            "tenant_id": self.tenant_id,
            "document_id": self.document_id,
            "generation_id": self.generation_id,
            "expected_count": self.expected_count,
            "expected_metadata_hash": self.expected_metadata_hash,
            "stale": self.stale,
            "indexes": self.indexes,
        }


class IndexReconciliationService:
    """从 PostgreSQL 分块记录出发检查 dense/lexical 外部投影。"""

    def __init__(
        self,
        session: AsyncSession,
        authorization: DatabaseAuthorizationService,
        dense: SearchAdapter,
        lexical: SearchAdapter,
    ) -> None:
        """绑定数据库权威查询、授权服务和两个外部投影。"""
        self.session = session
        self.authorization = authorization
        self.dense = dense
        self.lexical = lexical

    async def inspect(
        self,
        identity: RequestIdentity,
        document_id: str,
        generation_id: str | None = None,
    ) -> ReconciliationReport:
        """检查当前身份可访问文档的指定或活动处理代次。"""
        document = await self.session.scalar(
            select(DocumentRecord).where(
                DocumentRecord.id == document_id,
                DocumentRecord.tenant_id == identity.tenant_id,
                DocumentRecord.state != "deleted",
            )
        )
        if document is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Resource not found.", 404)
        await self.authorization.retrieval_scope(identity, [document.knowledge_space_id])
        selected_generation_id = generation_id or document.active_generation_id
        if selected_generation_id is None:
            return ReconciliationReport(
                identity.tenant_id,
                document_id,
                None,
                0,
                stable_json_hash({"items": []}),
                {
                    "dense": {"status": "no_active_generation", "stale": False},
                    "lexical": {"status": "no_active_generation", "stale": False},
                },
            )
        generation = await self.session.scalar(
            select(IndexGenerationRecord).where(
                IndexGenerationRecord.id == selected_generation_id,
                IndexGenerationRecord.tenant_id == identity.tenant_id,
                IndexGenerationRecord.document_id == document_id,
            )
        )
        if generation is None:
            raise AppError(ErrorCategory.NOT_FOUND, "Index generation not found.", 404)
        chunks = list(
            (
                await self.session.scalars(
                    select(ChunkRecord).where(
                        ChunkRecord.tenant_id == identity.tenant_id,
                        ChunkRecord.document_id == document_id,
                        ChunkRecord.index_generation_id == selected_generation_id,
                    )
                )
            ).all()
        )
        signatures = [
            {
                key: getattr(chunk, key)
                for key in (
                    "tenant_id",
                    "knowledge_space_id",
                    "document_id",
                    "document_version_id",
                    "index_generation_id",
                    "acl_epoch",
                )
            }
            | {"acl_tokens": sorted(chunk.acl_tokens)}
            for chunk in chunks
        ]
        expected_hash = stable_json_hash(
            {"items": sorted(signatures, key=lambda item: str(item))}
        )
        indexes: dict[str, dict[str, object]] = {}
        for name, index in (("dense", self.dense), ("lexical", self.lexical)):
            inspector = getattr(index, "inspect_generation", None)
            if inspector is None:
                indexes[name] = {"status": "inspection_unsupported", "stale": True}
                record_reconciliation(name, "inspection_unsupported")
                continue
            try:
                actual = dict(await inspector(document_id, selected_generation_id))
            except Exception as exc:
                indexes[name] = {
                    "status": "inspection_failed",
                    "error": str(exc)[:256],
                    "stale": True,
                }
                record_reconciliation(name, "inspection_failed")
                continue
            actual_count = actual.get("count")
            actual_hash = actual.get("metadata_hash")
            count_matches = isinstance(actual_count, int) and actual_count == len(chunks)
            hash_matches = actual_hash is None or actual_hash == expected_hash
            indexes[name] = {
                "status": "ok" if count_matches and hash_matches else "mismatch",
                "actual_count": actual_count,
                "actual_metadata_hash": actual_hash,
                "published_count": actual.get("published_count"),
                "stale": not (count_matches and hash_matches),
            }
            record_reconciliation(
                name, "ok" if count_matches and hash_matches else "mismatch"
            )
        return ReconciliationReport(
            identity.tenant_id,
            document_id,
            generation.id,
            len(chunks),
            expected_hash,
            indexes,
        )
