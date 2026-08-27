import json

import pytest
from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.domain.types import AuthorizationScope
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
    MilvusVectorSearch,
    OpenSearchLexicalSearch,
    QdrantVectorSearch,
)


async def test_dense_and_lexical_enforce_identical_authorization_filters() -> None:
    embedding = DeterministicEmbedding()
    vector = MemoryVectorSearch(embedding)
    lexical = MemoryLexicalSearch()
    text = "Internal VPN configuration"
    metadata = {
        "tenant_id": "tenant-a",
        "knowledge_space_id": "space-a",
        "document_id": "document-a",
        "document_version_id": "version-a",
        "acl_tokens": ["group:employees"],
    }
    item = IndexDocument("chunk-a", text, (await embedding.embed([text]))[0], metadata)
    for index in (vector, lexical):
        await index.stage([item])
        await index.publish("version-a")
        index.set_acl_epoch("tenant-a", 4)
    allowed = AuthorizationScope(
        "tenant-a", frozenset({"group:employees"}), frozenset({"space-a"}), 4
    )
    denied = AuthorizationScope(
        "tenant-a", frozenset({"group:contractors"}), frozenset({"space-a"}), 4
    )
    stale = AuthorizationScope(
        "tenant-a", frozenset({"group:employees"}), frozenset({"space-a"}), 3
    )
    assert [item.chunk_id for item in await vector.retrieve("VPN", allowed, 5)] == ["chunk-a"]
    assert [item.chunk_id for item in await lexical.retrieve("VPN", allowed, 5)] == ["chunk-a"]
    assert await vector.retrieve("VPN", denied, 5) == []
    assert await lexical.retrieve("VPN", denied, 5) == []
    with pytest.raises(PermissionError, match="stale"):
        await vector.retrieve("VPN", stale, 5)
    with pytest.raises(PermissionError, match="stale"):
        await lexical.retrieve("VPN", stale, 5)


class _FakeMilvusClient:
    """为 Milvus 适配器单元测试提供最小同步 SDK 替身。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.search_filter = ""

    def upsert(self, *, collection_name: str, data: list[dict[str, object]]) -> None:
        del collection_name
        by_id = {str(row["id"]): row for row in self.rows}
        by_id.update({str(row["id"]): row for row in data})
        self.rows = list(by_id.values())

    def query(
        self,
        *,
        collection_name: str,
        filter: str,
        output_fields: list[str],
    ) -> list[dict[str, object]]:
        del collection_name, output_fields
        if "document_version_id ==" in filter:
            return [
                dict(row)
                for row in self.rows
                if row["document_version_id"] == "version-a"
            ]
        if "document_id ==" in filter:
            return [
                dict(row)
                for row in self.rows
                if row["document_id"] == "document-a"
            ]
        return [dict(row) for row in self.rows]

    def search(self, *, filter: str, **kwargs: object) -> list[list[dict[str, object]]]:
        del kwargs
        self.search_filter = filter
        row = self.rows[0]
        return [
            [
                {
                    "distance": 0.91,
                    "entity": {
                        "chunk_id": row["chunk_id"],
                        "text": row["text"],
                        "metadata": row["metadata"],
                    },
                }
            ]
        ]

    def delete(self, *, collection_name: str, filter: str) -> None:
        del collection_name
        self.rows = [row for row in self.rows if "document-a" not in filter]

    def flush(self, *, collection_name: str) -> None:
        del collection_name

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_milvus_adapter_preserves_publish_acl_and_retrieve_contract() -> None:
    embedding = DeterministicEmbedding()
    adapter = MilvusVectorSearch(
        "http://localhost:19530",
        "enterprise_rag_chunks",
        embedding,
    )
    fake = _FakeMilvusClient()
    adapter._client = fake
    text = "Internal VPN configuration requires manager approval."
    metadata = {
        "tenant_id": "tenant-a",
        "knowledge_space_id": "space-a",
        "document_id": "document-a",
        "document_version_id": "version-a",
        "acl_tokens": ["group:employees"],
        "title": "VPN policy",
    }
    item = IndexDocument("chunk-a", text, (await embedding.embed([text]))[0], metadata)

    await adapter.stage([item])
    await adapter.publish("version-a")
    adapter.set_acl_epoch("tenant-a", 4)

    scope = AuthorizationScope(
        "tenant-a", frozenset({"group:employees"}), frozenset({"space-a"}), 4
    )
    results = await adapter.retrieve("VPN approval", scope, 5)

    assert [candidate.chunk_id for candidate in results] == ["chunk-a"]
    assert results[0].metadata["title"] == "VPN policy"
    assert 'tenant_id == "tenant-a"' in fake.search_filter
    assert "ARRAY_CONTAINS_ANY" in fake.search_filter
    assert "published == true" in fake.search_filter

    await adapter.delete_document("document-a")
    assert fake.rows == []
    await adapter.close()


@pytest.mark.asyncio
async def test_remote_reconciliation_inspection_omits_indexed_text(respx_mock) -> None:
    metadata = {
        "tenant_id": "tenant-a",
        "knowledge_space_id": "space-a",
        "document_id": "document-a",
        "document_version_id": "version-a",
        "index_generation_id": "generation-a",
        "acl_tokens": ["group:employees"],
        "acl_epoch": 4,
    }
    qdrant_route = respx_mock.post(
        "http://qdrant.example/collections/test/points/scroll"
    ).respond(
        200,
        json={"result": {"points": [{"payload": {**metadata, "published": True}}]}},
    )
    qdrant = QdrantVectorSearch(
        "http://qdrant.example", "test", DeterministicEmbedding()
    )
    qdrant_report = await qdrant.inspect_generation("document-a", "generation-a")
    qdrant_payload = json.loads(qdrant_route.calls[0].request.content)
    assert qdrant_report["count"] == 1
    assert qdrant_report["published_count"] == 1
    assert "text" not in qdrant_payload["with_payload"]
    await qdrant.close()

    opensearch_route = respx_mock.post(
        "http://opensearch.example/test/_search"
    ).respond(
        200,
        json={
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [{"_source": {**metadata, "published": True}}],
            }
        },
    )
    opensearch = OpenSearchLexicalSearch("http://opensearch.example", "test")
    opensearch_report = await opensearch.inspect_generation(
        "document-a", "generation-a"
    )
    opensearch_payload = json.loads(opensearch_route.calls[0].request.content)
    assert opensearch_report["count"] == 1
    assert opensearch_report["published_count"] == 1
    assert "text" not in opensearch_payload["_source"]
    await opensearch.close()
