## Why

Organizations need a reusable way to turn heterogeneous internal documents into trustworthy answers without coupling the product to one department or one model provider. A general enterprise RAG assistant establishes the shared knowledge, retrieval, security, configuration, and quality foundations that future domain assistants can reuse.

## What Changes

- Introduce tenant-scoped knowledge spaces for organizing documents, members, access rules, data sources, and retrieval policies.
- Add a document ingestion lifecycle covering upload, parsing, metadata normalization, versioning, incremental reprocessing, indexing, deletion propagation, retries, and job status.
- Add permission-aware hybrid retrieval, reranking, streaming answer synthesis, paragraph-level citations, source navigation, multi-turn follow-up, and evidence-based refusal.
- Add tenant and document access enforcement, security checks, and auditable records across ingestion and query execution.
- Add configurable assistant profiles that combine knowledge spaces, prompts, model profiles, retrieval policies, citation behavior, and refusal thresholds with versioning and rollback.
- Add evaluation and observability capabilities for traces, latency, token and cost metrics, golden-set regression, citation quality, retrieval quality, refusal accuracy, and user feedback.
- Define stable adapter boundaries for connectors, parsers, embedding models, rerankers, LLMs, and guardrails so that domain-specific or provider-specific logic does not enter the platform core.
- Exclude autonomous business actions, department-specific workflows, and exhaustive connector or multimodal coverage from the initial scope.

## Capabilities

### New Capabilities

- `knowledge-space-management`: Tenant-scoped knowledge spaces, membership, data-source association, lifecycle, and default policy boundaries.
- `document-ingestion`: File ingestion, parsing, metadata and ACL preservation, versioning, indexing jobs, retries, and update or deletion propagation.
- `permission-aware-rag`: Query processing, access-filtered hybrid retrieval, reranking, streaming grounded answers, citations, conversation follow-up, and refusal.
- `security-and-audit`: Tenant isolation, identity and permission enforcement, content safety checks, sensitive-data handling, and immutable audit events.
- `assistant-configuration`: Versioned assistant, model, prompt, retrieval, citation, and refusal policies with validation, activation, rollback, and adapter selection.
- `quality-observability`: End-to-end tracing, operational metrics, evaluation datasets and runs, regression gates, failure analysis, and user feedback capture.

### Modified Capabilities

None. The project does not yet contain main capability specs.

## Impact

- Establishes the initial application architecture and public behavior for the repository.
- Introduces a user-facing chat/API surface, an administration surface, an asynchronous ingestion worker, and persistent stores for metadata, source artifacts, indexes, cache, and audit records.
- Adds LlamaIndex-based ingestion and RAG orchestration behind project-owned domain interfaces.
- Requires integrations for an identity provider, relational database, object storage, vector search, keyword search, cache or task coordination, model providers, and telemetry.
- Requires security, retrieval-quality, grounded-answer, citation, refusal, update/delete propagation, and failure-recovery test coverage before production use.
