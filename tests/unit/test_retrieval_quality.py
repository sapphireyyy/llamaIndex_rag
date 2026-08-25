from enterprise_rag.application.ports import IndexDocument, RetrievalCandidate
from enterprise_rag.application.retrieval import _conflicting, reciprocal_rank_fusion
from enterprise_rag.domain.types import AuthorizationScope
from enterprise_rag.infrastructure.search import DeterministicEmbedding, MemoryVectorSearch


def candidate(chunk_id: str, score: float, source: str) -> RetrievalCandidate:
    return RetrievalCandidate(chunk_id, "text", score, {"document_id": chunk_id}, source)


def test_rank_fusion_deduplicates_deterministically() -> None:
    fused = reciprocal_rank_fusion(
        [
            [candidate("shared", 1.0, "dense"), candidate("dense", 0.5, "dense")],
            [candidate("shared", 1.0, "lexical"), candidate("lexical", 0.5, "lexical")],
        ],
        60,
    )
    assert [item.chunk_id for item in fused] == ["shared", "dense", "lexical"]
    assert fused[0].metadata["retrieval_sources"] == ["dense", "lexical"]


def test_conflicting_sources_detect_numbers_and_negation() -> None:
    assert _conflicting(
        [
            RetrievalCandidate("a", "Limit is 20 days.", 1, {"document_id": "a"}, "dense"),
            RetrievalCandidate("b", "Limit is 25 days.", 1, {"document_id": "b"}, "dense"),
        ]
    )
    assert _conflicting(
        [
            RetrievalCandidate("a", "Access is allowed.", 1, {"document_id": "a"}, "dense"),
            RetrievalCandidate("b", "Access is not allowed.", 1, {"document_id": "b"}, "dense"),
        ]
    )


async def test_semantic_alias_and_mixed_language_retrieval() -> None:
    embedding = DeterministicEmbedding()
    search = MemoryVectorSearch(embedding)
    text = "Remote access requires manager approval and employees receive leave benefits."
    vector = (await embedding.embed([text]))[0]
    await search.stage(
        [
            IndexDocument(
                "chunk",
                text,
                vector,
                {
                    "tenant_id": "tenant",
                    "knowledge_space_id": "space",
                    "document_id": "document",
                    "document_version_id": "version",
                    "acl_tokens": ["user:reader"],
                },
            )
        ]
    )
    await search.publish("version")
    search.set_acl_epoch("tenant", 1)
    scope = AuthorizationScope(
        "tenant", frozenset({"user:reader"}), frozenset({"space"}), 1
    )
    assert (await search.retrieve("supervisor authorization", scope, 5))[0].chunk_id == "chunk"
    assert (await search.retrieve("经理审批 remote access", scope, 5))[0].chunk_id == "chunk"
