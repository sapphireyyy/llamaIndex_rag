import pytest
from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.domain.types import AuthorizationScope
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
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
