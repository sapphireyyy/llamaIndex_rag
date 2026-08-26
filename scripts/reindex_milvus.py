"""把数据库中的活动分块重新写入 Milvus。"""

from __future__ import annotations

import asyncio
from collections import defaultdict

from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.config import get_settings
from enterprise_rag.infrastructure.database import create_engine, create_session_factory
from enterprise_rag.infrastructure.orm import ChunkRecord, DocumentRecord
from enterprise_rag.infrastructure.search import DeterministicEmbedding, MilvusVectorSearch
from enterprise_rag.infrastructure.secrets import EnvironmentSecretStore
from sqlalchemy import select


async def reindex() -> None:
    """按活动文档版本重建 Milvus 向量集合。"""
    settings = get_settings()
    if settings.vector_backend != "milvus":
        raise RuntimeError("RAG_VECTOR_BACKEND must be milvus before reindexing")

    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    token = None
    if settings.milvus_token_reference:
        token = await EnvironmentSecretStore().resolve(settings.milvus_token_reference)
    index = MilvusVectorSearch(
        settings.milvus_url,
        settings.milvus_collection,
        DeterministicEmbedding(),
        token=token,
    )

    try:
        await index.start()
        async with sessions() as session:
            result = await session.execute(
                select(ChunkRecord, DocumentRecord)
                .join(DocumentRecord, DocumentRecord.id == ChunkRecord.document_id)
                .where(
                    ChunkRecord.active.is_(True),
                    DocumentRecord.state == "active",
                    DocumentRecord.active_version_id == ChunkRecord.document_version_id,
                )
                .order_by(ChunkRecord.document_version_id, ChunkRecord.ordinal)
            )
            rows = result.all()

        by_version: defaultdict[str, list[tuple[ChunkRecord, DocumentRecord]]] = defaultdict(
            list
        )
        for chunk, document in rows:
            by_version[chunk.document_version_id].append((chunk, document))

        embedding = DeterministicEmbedding()
        indexed_chunks = 0
        for version_id, version_rows in by_version.items():
            vectors = await embedding.embed([chunk.text for chunk, _ in version_rows])
            documents = [
                IndexDocument(
                    chunk.id,
                    chunk.text,
                    vector,
                    {
                        **dict(chunk.metadata_json),
                        "tenant_id": chunk.tenant_id,
                        "knowledge_space_id": chunk.knowledge_space_id,
                        "document_id": chunk.document_id,
                        "document_version_id": chunk.document_version_id,
                        "title": document.title,
                        "page": chunk.page,
                        "section": chunk.section,
                        "acl_tokens": chunk.acl_tokens,
                        "acl_epoch": chunk.acl_epoch,
                    },
                )
                for (chunk, document), vector in zip(version_rows, vectors, strict=True)
            ]
            await index.stage(documents)
            await index.publish(version_id)
            indexed_chunks += len(documents)

        print(
            f"Reindexed {indexed_chunks} chunks from {len(by_version)} active document versions "
            f"into {settings.milvus_url}/{settings.milvus_collection}"
        )
    finally:
        await index.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(reindex())
