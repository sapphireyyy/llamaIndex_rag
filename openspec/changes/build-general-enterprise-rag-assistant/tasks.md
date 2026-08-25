## 1. Project Foundation and Delivery Baseline

- [x] 1.1 Scaffold the Python backend with separate FastAPI API and worker entrypoints plus domain, application, adapter, and infrastructure package boundaries.
- [x] 1.2 Scaffold the TypeScript web client with chat and administration application shells and a typed API-client boundary.
- [x] 1.3 Declare and pin backend, frontend, test, lint, type-check, migration, telemetry, and LlamaIndex dependencies in reproducible lock files.
- [x] 1.4 Record the first-deployment choices for OIDC, secret storage, object storage, durable queue, vector search, lexical search, model, embedding, reranking, and parsing adapters without weakening the provider-neutral contracts.
- [x] 1.5 Implement validated environment configuration, secret references, startup checks, and health/readiness endpoints for API and workers.
- [x] 1.6 Add local development infrastructure for the selected relational, object, queue, cache, and search dependencies with isolated test configuration.
- [x] 1.7 Configure formatting, linting, static type checking, unit tests, migration checks, frontend checks, and build verification in CI.

## 2. Domain Model, Persistence, and Asynchronous Foundations

- [x] 2.1 Define project-owned identifiers, tenant-aware domain primitives, timestamps, lifecycle states, error categories, and idempotency semantics.
- [x] 2.2 Implement relational models and initial migrations for tenants, principals, groups, knowledge spaces, memberships, data sources, documents, versions, chunks, jobs, and attempts.
- [x] 2.3 Add relational models and migrations for assistant versions, policy versions, conversations, queries, answers, citations, feedback, evaluation records, and audit-event indexes.
- [x] 2.4 Implement repository and transaction interfaces that prevent domain modules from importing each other's persistence models.
- [x] 2.5 Implement tenant-scoped object-storage paths and an object-store adapter for originals, parsed artifacts, previews, and exports.
- [x] 2.6 Implement transactional outbox persistence, dispatch, deduplication, retry, and poison-message handling.
- [x] 2.7 Implement durable job submission and worker-consumption contracts with correlation IDs, priorities, cancellation, retry metadata, and terminal states.
- [x] 2.8 Add database, object-store, queue, and outbox integration-test fixtures with tenant-isolated test data.

## 3. Identity, Authorization, Secrets, and Audit

- [x] 3.1 Implement OIDC-compatible token verification and an immutable request identity derived only from trusted tenant, user, and group claims.
- [x] 3.2 Implement centralized default-deny authorization for administration, ingestion, query, configuration, evaluation, preview, download, and audit operations.
- [x] 3.3 Implement normalized principal/group ACLs, compiled retrieval scopes, ACL epochs, and authorization-sensitive cache-key construction.
- [x] 3.4 Implement a secret-binding abstraction that stores provider credentials outside ordinary configuration and never returns secret values.
- [x] 3.5 Implement structured redaction for logs, traces, provider failures, API errors, and evaluation exports.
- [x] 3.6 Implement append-only audit-event recording and authorized tenant-scoped search/export APIs.
- [x] 3.7 Add stable sanitized API error responses with correlation IDs and indistinguishable restricted-resource errors.
- [x] 3.8 Add security tests for invalid identities, role boundaries, cross-tenant access, stale or missing policy data, secret disclosure, and audit immutability.

## 4. Knowledge-Space Management

- [x] 4.1 Implement knowledge-space creation, rename, archive, restore, and tenant ownership invariants.
- [x] 4.2 Implement administrator, editor, and reader membership management for users and groups with immediate authorization updates.
- [x] 4.3 Implement data-source association, enable/disable, removal, and deletion-propagation event creation.
- [x] 4.4 Implement knowledge-space default retrieval and content-policy references with validation of active tenant-owned policies.
- [x] 4.5 Expose versioned knowledge-space, membership, and data-source administration APIs with idempotent mutation support.
- [x] 4.6 Build administration UI flows for listing, creating, editing, archiving, restoring, and assigning members to knowledge spaces.
- [x] 4.7 Add unit and API tests for lifecycle, membership changes, archived-space exclusion, source removal, and cross-tenant denial.

## 5. Assistant and Provider Configuration

- [x] 5.1 Implement draft and immutable activated assistant versions plus versioned prompt, model, retrieval, citation, refusal, and guardrail policies.
- [x] 5.2 Define capability-declaring interfaces for connectors, parsers, embeddings, dense retrieval, lexical retrieval, reranking, model completion/streaming, guardrails, and trace sinks.
- [x] 5.3 Implement adapter registration and provider-profile loading without global mutable LlamaIndex settings.
- [x] 5.4 Implement model-gateway timeouts, bounded retries, rate limits, circuit breaking, usage accounting, cost attribution, and policy-controlled fallback.
- [x] 5.5 Implement assistant-version validation for tenant ownership, referenced resources, provider capabilities, secret bindings, and policy compatibility.
- [x] 5.6 Implement activation, immutable query snapshots, historical inspection, rollback revalidation, and configuration audit events.
- [x] 5.7 Expose configuration APIs that return secret-binding status but never secret values.
- [x] 5.8 Build administration UI flows for assistant drafts, component selection, validation findings, activation history, and rollback.
- [x] 5.9 Add contract tests for compatible and incompatible providers, cross-tenant references, failed activation, immutable versions, and rollback.

## 6. Document Ingestion and Index Publication

- [x] 6.1 Implement document, immutable version, source identity, content fingerprint, chunk provenance, ingestion job, and attempt state machines.
- [x] 6.2 Implement authorized multipart upload with configured format, size, integrity, malware, and idempotency validation before source publication.
- [x] 6.3 Implement the first parser adapters for the selected allow-list and preserve title hierarchy, page or section location, tables, source metadata, and sanitized parse diagnostics.
- [x] 6.4 Implement LlamaIndex-backed normalization, deterministic chunking, metadata enrichment, ACL propagation, and embedding transformations behind project-owned interfaces.
- [x] 6.5 Implement vector-index and lexical-index adapters with equivalent tenant, knowledge-space, document-version, provenance, ACL-token, and ACL-epoch metadata.
- [x] 6.6 Implement staging-index writes, verification, atomic active-version publication, and asynchronous superseded-version cleanup.
- [x] 6.7 Implement unchanged-content detection and ensure repeated upload or synchronization creates no duplicate active version or chunks.
- [x] 6.8 Implement automatic transient retries, authorized manual retry, attempt accounting, cancellation, and preservation of the last valid active version.
- [x] 6.9 Implement high-priority document deletion, source removal, ACL revocation, tombstoning, cache invalidation, and retention-aware artifact cleanup.
- [x] 6.10 Expose ingestion submission, job status, failure detail, retry, cancellation, document listing, deletion, preview, and download APIs.
- [x] 6.11 Build administration UI flows for upload, document/version listing, job progress, failure diagnosis, retry, cancellation, deletion, and preview.
- [x] 6.12 Add ingestion tests for unsupported content, provenance, ACL preservation, unchanged sync, changed publication, partial failure, retry, deletion, revocation, and cleanup.

## 7. Permission-Aware Retrieval and Reranking

- [x] 7.1 Implement query normalization and safe follow-up rewriting while keeping prior conversation text separate from retrieved evidence.
- [x] 7.2 Implement dense and lexical retrieval requests that require a compiled authorization scope and reject execution when it is missing or stale.
- [x] 7.3 Implement deterministic candidate deduplication and rank fusion with versioned policy parameters.
- [x] 7.4 Implement reranking with provider capability validation, bounded candidate sets, traceable scores, and policy-controlled degradation.
- [x] 7.5 Implement evidence selection with active-version enforcement, relevance thresholds, context budgets, source authority metadata, and conflict detection.
- [x] 7.6 Add adapter contract tests proving tenant, knowledge-space, document, user/group ACL, active-version, and revocation filters are enforced identically for dense and lexical search.
- [x] 7.7 Add retrieval-quality tests for exact terms, semantic paraphrases, mixed-language questions, duplicate candidates, conflicting sources, and degraded dependencies.

## 8. Grounded Conversational Answering

- [x] 8.1 Implement conversation, message, query execution, answer, citation, and immutable assistant-version snapshot persistence.
- [x] 8.2 Implement the deterministic LlamaIndex workflow for authentication context, profile resolution, authorized retrieval, fusion, reranking, context assembly, synthesis, validation, and terminal outcome.
- [x] 8.3 Implement grounded answer prompting that separates trusted policies, user input, retrieved content, and external outputs.
- [x] 8.4 Implement paragraph-level citation construction and validation against document version and page, section, or equivalent source locations.
- [x] 8.5 Implement explicit insufficient-evidence refusal and cited conflicting-evidence responses without unsupported model completion.
- [x] 8.6 Implement input, retrieved-content, and output guardrails for prompt injection, restricted disclosure, and configured sensitive data.
- [x] 8.7 Implement the streaming protocol with query ID, answer events, citation events, and explicit success, refusal, degraded, cancelled, or failed terminal status.
- [x] 8.8 Implement follow-up resolution that rechecks current assistant version, authorization, active document versions, and retrieval on each fact-seeking turn.
- [x] 8.9 Implement citation source navigation that reauthorizes preview and download at access time.
- [x] 8.10 Build the web chat experience for assistant selection, streaming answers, citations, source preview, follow-up, refusal, degradation, and recoverable errors.
- [x] 8.11 Add end-to-end query tests for grounded answers, citation accuracy, no-evidence refusal, conflicts, multi-turn follow-up, profile switching, mid-conversation revocation, and post-generation validation failure.

## 9. Telemetry and Operational Controls

- [x] 9.1 Implement OpenTelemetry-compatible correlation and spans across API requests, query stages, ingestion attempts, queue messages, retrieval, model calls, and evaluation items.
- [x] 9.2 Emit request volume, terminal outcome, refusal, degradation, stage latency, retrieval counts, model tokens, and attributable provider-cost metrics.
- [x] 9.3 Implement tenant-scoped telemetry access plus configurable content capture, redaction, sampling, retention, and export policy enforcement.
- [x] 9.4 Ensure disabling prompt and response capture retains required structural telemetry, aggregate metrics, and security audit events.
- [x] 9.5 Add operational dashboards and alerts for API/worker readiness, queue backlog, ingestion failure, query failure, latency, provider health, cost, and revocation backlog.
- [x] 9.6 Add telemetry tests for correlation completeness, version provenance, retry linkage, redaction, content-capture disablement, unavailable cost data, and tenant isolation.

## 10. Evaluation, Release Gates, and Feedback

- [x] 10.1 Implement immutable evaluation dataset versions with question, expected source scope, optional reference answer, expected answerability, and access context.
- [x] 10.2 Implement evaluation-run orchestration that snapshots dataset, assistant, component, evaluator, and parameter versions.
- [x] 10.3 Implement separate retrieval relevance, retrieval coverage, groundedness, citation correctness, answer relevance, refusal correctness, latency, and access-control evaluators.
- [x] 10.4 Create a versioned general evaluation corpus spanning policy, IT-help, and product documentation with answerable, unanswerable, conflicting, permission-sensitive, multi-turn, update, and deletion cases.
- [x] 10.5 Implement aggregate and per-item evaluation result APIs plus authorized failure-analysis views.
- [x] 10.6 Implement configurable release gates, candidate/baseline comparison, promotion eligibility, and authorized reasoned overrides with audit evidence.
- [x] 10.7 Implement answer and citation feedback capture linked to immutable query, answer, citation, and configuration versions.
- [x] 10.8 Build administration UI flows for datasets, evaluation runs, metric comparison, failed-item inspection, gates, overrides, and user feedback review.
- [x] 10.9 Add evaluation tests for dataset immutability, reproducible runs, unanswerable scoring, failed gates, audited overrides, redacted exports, and missing provider cost.

## 11. Reliability, Performance, and Security Hardening

- [x] 11.1 Run failure-injection tests for database, object store, queue, vector search, lexical search, parser, embedding, reranker, model, and telemetry outages.
- [x] 11.2 Verify fail-closed authorization and last-valid-version behavior under timeout, retry, duplicate delivery, partial indexing, stale cache, and worker restart conditions.
- [x] 11.3 Add rate limits, concurrency limits, request cancellation, context and token budgets, upload limits, queue backpressure, and bounded retry defaults.
- [x] 11.4 Benchmark the general corpus to establish documented initial baselines for ingestion throughput, freshness, query latency, concurrency, availability, quality, and cost.
- [x] 11.5 Tune default chunking, fusion, reranking, context, citation, refusal, timeout, retry, and fallback policies against the versioned evaluation corpus.
- [x] 11.6 Complete threat modeling and security tests for prompt injection, malicious files, cross-tenant access, source-token exposure, telemetry leakage, restricted-resource enumeration, and export controls.
- [x] 11.7 Verify forward and rollback database migrations, application rollback, assistant-version rollback, index-version rollback, and delayed destructive cleanup.

## 12. Production Readiness and Acceptance

- [x] 12.1 Add production deployment manifests, secret bindings, network and storage policy, scaling settings, backup/restore, and disaster-recovery configuration for selected providers.
- [x] 12.2 Document operator procedures for startup, readiness, provider outage, queue recovery, ingestion retry, deletion/revocation incidents, audit export, rollback, and data retention.
- [x] 12.3 Document administrator and end-user workflows for knowledge spaces, assistants, uploads, permissions, chat, citations, refusals, feedback, evaluation, and rollback.
- [x] 12.4 Run the complete backend, frontend, migration, adapter-contract, security, ingestion, query, evaluation, failure-injection, and end-to-end suites in a production-like environment.
- [x] 12.5 Confirm mandatory evaluation and security gates pass for the general three-domain corpus with no cross-tenant or unauthorized-document retrieval.
- [x] 12.6 Verify that every capability requirement and scenario is covered by an automated test or an explicitly documented operational acceptance check.
- [x] 12.7 Produce the release evidence package containing dependency inventory, configuration versions, migration status, test and evaluation results, security findings, SLO baselines, rollback evidence, and known limitations.
