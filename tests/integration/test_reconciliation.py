from __future__ import annotations

import pytest
from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.application.reconciliation import IndexReconciliationService
from enterprise_rag.application.security import DatabaseAuthorizationService
from enterprise_rag.domain.types import RequestIdentity, Role
from enterprise_rag.infrastructure.orm import (
    ChunkRecord,
    DataSourceRecord,
    DocumentRecord,
    DocumentVersionRecord,
    IndexGenerationRecord,
    KnowledgeSpaceRecord,
    SpaceMembershipRecord,
    TenantRecord,
)
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_reconciliation_compares_both_projections_without_returning_content(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = "tenant-reconcile"
    space_id = "space-reconcile"
    document_id = "document-reconcile"
    version_id = "version-reconcile"
    generation_id = "generation-reconcile"
    async with sessions() as session:
        source = DataSourceRecord(
            id="source-reconcile",
            tenant_id=tenant_id,
            knowledge_space_id=space_id,
            external_id="upload",
            kind="upload",
        )
        document = DocumentRecord(
            id=document_id,
            tenant_id=tenant_id,
            knowledge_space_id=space_id,
            data_source_id=source.id,
            source_identity="document-reconcile",
            title="Reconciliation fixture",
            active_version_id=version_id,
            active_generation_id=generation_id,
        )
        session.add_all(
            [
                TenantRecord(id=tenant_id, slug=tenant_id, name="Reconciliation tenant"),
                KnowledgeSpaceRecord(id=space_id, tenant_id=tenant_id, name="Space"),
                SpaceMembershipRecord(
                    id="membership-reconcile",
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    principal_token="user:alice",
                    role="reader",
                ),
                source,
                document,
                DocumentVersionRecord(
                    id=version_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    content_hash="b" * 64,
                    source_key="tenants/tenant-reconcile/originals/source.txt",
                    mime_type="text/plain",
                    size_bytes=10,
                    state="active",
                ),
                IndexGenerationRecord(
                    id=generation_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    generation_number=1,
                    processing_strategy_version="ingestion-v1",
                    processing_config_hash="c" * 64,
                    component_versions={"parser": "test"},
                    reason="test",
                    state="active",
                    dense_state="published",
                    lexical_state="published",
                    chunk_count=1,
                ),
                ChunkRecord(
                    id="chunk-reconcile",
                    tenant_id=tenant_id,
                    knowledge_space_id=space_id,
                    document_id=document_id,
                    document_version_id=version_id,
                    index_generation_id=generation_id,
                    ordinal=0,
                    text="A non-sensitive fixture fact.",
                    acl_tokens=["user:alice"],
                    acl_epoch=1,
                    active=True,
                ),
            ]
        )
        await session.commit()

    embedding = DeterministicEmbedding()
    metadata = {
        "tenant_id": tenant_id,
        "knowledge_space_id": space_id,
        "document_id": document_id,
        "document_version_id": version_id,
        "index_generation_id": generation_id,
        "acl_tokens": ["user:alice"],
        "acl_epoch": 1,
    }
    item = IndexDocument(
        "chunk-reconcile",
        "A non-sensitive fixture fact.",
        (await embedding.embed(["A non-sensitive fixture fact."]))[0],
        metadata,
    )
    dense = MemoryVectorSearch(embedding)
    lexical = MemoryLexicalSearch()
    for index in (dense, lexical):
        await index.stage([item])
        await index.publish_generation(version_id, generation_id)

    identity = RequestIdentity(
        tenant_id,
        "alice",
        roles=frozenset({Role.READER}),
        authorization_epoch=1,
    )
    async with sessions() as session:
        service = IndexReconciliationService(
            session,
            DatabaseAuthorizationService(sessions),
            dense,
            lexical,
        )
        report = await service.inspect(identity, document_id)
        assert report.stale is False
        assert report.expected_count == 1
        assert report.indexes["dense"]["status"] == "ok"
        assert report.indexes["lexical"]["status"] == "ok"
        assert "text" not in report.as_dict()

        await lexical.stage(
            [
                IndexDocument(
                    "stale-extra",
                    "extra fixture",
                    (await embedding.embed(["extra fixture"]))[0],
                    {**metadata, "document_id": document_id},
                )
            ]
        )
        stale = await service.inspect(identity, document_id)

    assert stale.stale is True
    assert stale.indexes["dense"]["status"] == "ok"
    assert stale.indexes["lexical"]["status"] == "mismatch"
