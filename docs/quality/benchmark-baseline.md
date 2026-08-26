# Development benchmark baseline

Measured on the local in-memory baseline on 2026-08-13 with 300 synthetic documents and 100
serial hybrid queries:

- Ingestion transformation and indexing: 8,883 documents/second.
- Hybrid retrieval latency: p50 8.76 ms; p95 9.76 ms.
- Exact-term recall@10: 1.00.
- Model cost: 0 because the deterministic extractive provider is local.

These are repeatable engineering baselines, not production SLO evidence. The run excludes
network, durable provider, parser, model generation, and concurrent user latency. Production
acceptance must replace these values after a full three-domain corpus run on the selected
PostgreSQL, object store, queue, Milvus, OpenSearch, OIDC, and model providers.

Initial objectives are: API availability 99.9%, freshness p95 under five minutes, query p95
under five seconds excluding provider-declared long generation, no unauthorized retrieval,
access-control score 1.0, refusal correctness 1.0, and cost within the approved tenant budget.
