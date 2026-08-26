# ADR 0001: Initial provider baseline

Status: accepted for the first implementation; every choice remains behind a project-owned port.

## Choices

- Identity: generic OIDC contract, exercised locally with Keycloak.
- Secrets: secret references with environment/file development binding and an external secret-manager port for production.
- Relational system of record: PostgreSQL; SQLite is the isolated test and zero-infrastructure development fallback.
- Object storage: S3-compatible contract, exercised with MinIO; filesystem fallback for tests and local development.
- Durable queue: RabbitMQ contract; deterministic in-process queue for tests.
- Cache and coordination: Redis contract; in-process fallback for tests.
- Vector retrieval: Milvus contract; in-memory cosine-like test adapter. The Qdrant adapter
  remains available as a compatibility rollback path.
- Lexical retrieval: OpenSearch contract; in-memory BM25-like test adapter.
- Model access: OpenAI-compatible model gateway with an extractive deterministic fallback for tests and credential-free development.
- Embedding and reranking: configurable provider profiles; deterministic local adapters for tests.
- Parsing: PyMuPDF, python-docx, openpyxl, python-pptx, BeautifulSoup, Markdown/plain-text adapters.
- Telemetry: OpenTelemetry-compatible spans and Prometheus metrics.

## Rationale

The baseline exercises distinct enterprise responsibilities and failure modes without exposing those
vendors in domain models or APIs. Lightweight deterministic fallbacks keep unit and acceptance tests
reproducible and prevent credentials or Docker availability from becoming a correctness prerequisite.
