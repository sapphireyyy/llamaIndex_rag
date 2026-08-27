from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from enterprise_rag.application.generation_cleanup import IndexGenerationCleanupService
from enterprise_rag.application.ingestion import IngestionService
from enterprise_rag.application.ports import IndexDocument, RetrievalCandidate
from enterprise_rag.domain.types import AuthorizationScope, RequestIdentity, Role
from enterprise_rag.infrastructure.object_store import FileSystemObjectStore
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DataSourceRecord,
    DocumentRecord,
    IndexGenerationRecord,
    KnowledgeSpaceRecord,
    OutboxRecord,
    SpaceMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.parsers import AllowListParser
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FailNewPublication:
    def __init__(self, inner: MemoryLexicalSearch, failing_version_id: str | None = None) -> None:
        self.inner = inner
        self.failing_version_id = failing_version_id

    async def stage(self, documents: Sequence[IndexDocument]) -> None:
        await self.inner.stage(documents)

    async def publish(self, document_version_id: str) -> None:
        if document_version_id == self.failing_version_id:
            raise ConnectionError("lexical publication failed")
        await self.inner.publish(document_version_id)

    async def delete_document(self, document_id: str) -> None:
        await self.inner.delete_document(document_id)

    async def retrieve(
        self, query: str, scope: AuthorizationScope, top_k: int
    ) -> list[RetrievalCandidate]:
        return await self.inner.retrieve(query, scope, top_k)


async def test_partial_publication_preserves_last_valid_version_provenance_and_acl(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    identity = RequestIdentity(
        "tenant", "admin", groups=("employees",), roles=frozenset({Role.ADMINISTRATOR})
    )
    embedding = DeterministicEmbedding()
    dense = MemoryVectorSearch(embedding)
    lexical = MemoryLexicalSearch()
    store = FileSystemObjectStore(tmp_path / "objects")
    async with sessions() as session:
        session.add(TenantRecord(id="tenant", name="Tenant"))
        await session.flush()
        session.add(KnowledgeSpaceRecord(id="space", tenant_id="tenant", name="Space"))
        await session.flush()
        session.add_all(
            [
                SpaceMembershipRecord(
                    id="member",
                    tenant_id="tenant",
                    knowledge_space_id="space",
                    principal_token="group:employees",
                    role="reader",
                ),
                DataSourceRecord(
                    id="source",
                    tenant_id="tenant",
                    knowledge_space_id="space",
                    external_id="upload",
                    kind="upload",
                ),
            ]
        )
        await session.commit()
        service = IngestionService(
            session,
            store,
            AllowListParser(),
            embedding,
            dense,
            lexical,
            {".txt"},
            1_000_000,
        )
        first = await service.submit_upload(
            identity, "source", "policy.txt", b"Policy value is 20.", "text/plain", "c1"
        )
        first = await service.process(first.id)
        assert first.status == "succeeded"
        await session.commit()
        document = await session.get(DocumentRecord, first.document_id)
        assert document is not None
        first_version = document.active_version_id
        chunks = list(
            (
                await session.scalars(
                    select(ChunkRecord).where(ChunkRecord.document_version_id == first_version)
                )
            ).all()
        )
        assert chunks[0].acl_tokens == ["group:employees"]
        assert chunks[0].metadata_json["document_version_id"] == first_version

        failing = FailNewPublication(lexical)
        failed_service = IngestionService(
            session,
            store,
            AllowListParser(),
            embedding,
            dense,
            failing,
            {".txt"},
            1_000_000,
        )
        second = await failed_service.submit_upload(
            identity, "source", "policy.txt", b"Policy value is 25.", "text/plain", "c2"
        )
        failing.failing_version_id = second.document_version_id
        second = await failed_service.process(second.id)
        assert second.status == "failed"
        await session.refresh(document)
        assert document.active_version_id == first_version
        scope = AuthorizationScope(
            "tenant", identity.principal_tokens, frozenset({"space"}), 1
        )
        assert "20" in (await dense.retrieve("Policy value", scope, 5))[0].text

        await failed_service.delete_document(identity, document.id, "delete-correlation")
        cleanup = await session.scalar(
            select(OutboxRecord).where(OutboxRecord.kind == "document.cleanup")
        )
        assert cleanup is not None and cleanup.priority == 100


async def test_generation_rollback_restores_a_published_previous_projection(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    identity = RequestIdentity(
        "tenant", "admin", groups=("employees",), roles=frozenset({Role.ADMINISTRATOR})
    )
    embedding = DeterministicEmbedding()
    dense = MemoryVectorSearch(embedding)
    lexical = MemoryLexicalSearch()
    store = FileSystemObjectStore(tmp_path / "objects")
    async with sessions() as session:
        session.add(TenantRecord(id="tenant", name="Tenant"))
        session.add(KnowledgeSpaceRecord(id="space", tenant_id="tenant", name="Space"))
        session.add_all(
            [
                SpaceMembershipRecord(
                    id="member",
                    tenant_id="tenant",
                    knowledge_space_id="space",
                    principal_token="group:employees",
                    role="reader",
                ),
                DataSourceRecord(
                    id="source",
                    tenant_id="tenant",
                    knowledge_space_id="space",
                    external_id="upload",
                    kind="upload",
                ),
            ]
        )
        await session.commit()
        service = IngestionService(
            session,
            store,
            AllowListParser(),
            embedding,
            dense,
            lexical,
            {".txt"},
            1_000_000,
        )
        first = await service.submit_upload(
            identity, "source", "policy.txt", b"Policy value is 20.", "text/plain", "c1"
        )
        assert (await service.process(first.id)).status == "succeeded"
        await session.commit()
        second = await service.submit_upload(
            identity, "source", "policy.txt", b"Policy value is 25.", "text/plain", "c2"
        )
        assert (await service.process(second.id)).status == "succeeded"
        await session.commit()

        generations = list(
            (
                await session.scalars(
                    select(IndexGenerationRecord)
                    .where(IndexGenerationRecord.document_id == first.document_id)
                    .order_by(IndexGenerationRecord.generation_number)
                )
            ).all()
        )
        assert len(generations) == 2
        document = await session.get(DocumentRecord, first.document_id)
        assert document is not None and document.active_generation_id == generations[1].id

        restored = await service.rollback_generation(
            identity, first.document_id or "", generations[0].id, "rollback-correlation"
        )
        await session.commit()
        await session.refresh(document)

        assert restored.id == generations[0].id
        assert document.active_generation_id == generations[0].id
        assert document.active_version_id == generations[0].document_version_id
        scope = AuthorizationScope("tenant", identity.principal_tokens, frozenset({"space"}), 1)
        assert "20" in (await dense.retrieve("Policy value", scope, 5))[0].text
        assert "20" in (await lexical.retrieve("Policy value", scope, 5))[0].text

        cleanup = await IndexGenerationCleanupService(
            session, dense, lexical, retention_seconds=60
        ).sweep(
            "tenant",
            now=datetime.now(UTC) + timedelta(seconds=61),
        )
        await session.commit()
        assert cleanup.deleted == 1
        remaining = list(
            (
                await session.scalars(
                    select(IndexGenerationRecord).where(
                        IndexGenerationRecord.document_id == first.document_id
                    )
                )
            ).all()
        )
        assert [generation.id for generation in remaining] == [generations[0].id]
