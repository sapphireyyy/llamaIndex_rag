from __future__ import annotations

import asyncio
import json
import statistics
import time

from enterprise_rag.application.ports import IndexDocument
from enterprise_rag.domain.types import AuthorizationScope
from enterprise_rag.infrastructure.search import (
    DeterministicEmbedding,
    MemoryLexicalSearch,
    MemoryVectorSearch,
)


async def main() -> None:
    embedding = DeterministicEmbedding()
    dense = MemoryVectorSearch(embedding)
    lexical = MemoryLexicalSearch()
    texts = [f"Policy item {index} requires approval code TERM-{index}." for index in range(300)]
    started = time.perf_counter()
    vectors = await embedding.embed(texts)
    for index, (text, vector) in enumerate(zip(texts, vectors, strict=True)):
        version = f"version-{index}"
        item = IndexDocument(
            f"chunk-{index}", text, vector,
            {
                "tenant_id": "benchmark",
                "knowledge_space_id": "general",
                "document_id": f"document-{index}",
                "document_version_id": version,
                "acl_tokens": ["group:employees"],
            },
        )
        for search in (dense, lexical):
            await search.stage([item])
            await search.publish(version)
            search.set_acl_epoch("benchmark", 1)
    ingest_seconds = time.perf_counter() - started
    scope = AuthorizationScope(
        "benchmark", frozenset({"group:employees"}), frozenset({"general"}), 1
    )
    latencies: list[float] = []
    correct = 0
    for index in range(100):
        query = f"TERM-{index}"
        started = time.perf_counter()
        dense_results, lexical_results = await asyncio.gather(
            dense.retrieve(query, scope, 10), lexical.retrieve(query, scope, 10)
        )
        latencies.append((time.perf_counter() - started) * 1000)
        result_ids = {item.chunk_id for item in dense_results + lexical_results}
        correct += int(f"chunk-{index}" in result_ids)
    ordered = sorted(latencies)
    report = {
        "documents": len(texts),
        "ingestion_documents_per_second": len(texts) / ingest_seconds,
        "hybrid_query_p50_ms": statistics.median(ordered),
        "hybrid_query_p95_ms": ordered[94],
        "exact_term_recall_at_10": correct / 100,
        "concurrency_model": "bounded API semaphores; benchmark loop is serial",
        "availability": "covered by readiness and failure-injection suite",
        "model_cost": 0.0,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
