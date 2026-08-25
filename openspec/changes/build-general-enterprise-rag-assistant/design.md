## Context

The repository currently contains OpenSpec configuration but no application implementation or main capability specs. The product has no selected vertical business domain, so the architecture must provide stable general-purpose knowledge and RAG abstractions while preserving a narrow initial delivery boundary. See `proposal.md` for motivation and the six capability specs for observable behavior.

The design must support heterogeneous enterprise documents, tenant and document permissions, asynchronous ingestion, provider substitution, reproducible configuration, and quality evidence. LlamaIndex is the RAG framework, but externally visible product behavior and core domain data must not depend directly on framework-specific objects.

## Goals / Non-Goals

**Goals:**

- Establish a modular architecture that can start as one API deployment plus independent workers and can split at stable boundaries later.
- Keep tenant, knowledge, authorization, configuration, audit, and quality state in project-owned domain models.
- Use LlamaIndex behind explicit ingestion, retrieval, workflow, model, and telemetry adapters.
- Make document publication, replacement, deletion, and permission revocation safe under retries and partial dependency failures.
- Enforce authorization before both dense and lexical retrieval results can enter model context.
- Make every answer, citation, refusal, configuration, evaluation, and operational decision traceable to immutable versions.
- Permit provider and deployment choices to be changed through configuration and adapters rather than application rewrites.

**Non-Goals:**

- Designing autonomous action-taking agents or department-specific business workflows.
- Starting with independently deployed microservices for every domain module.
- Supporting every enterprise connector, document format, modality, or model provider in the first release.
- Using a vector database as the system of record for documents, permissions, configuration, or audit history.
- Training or fine-tuning foundation models as part of the initial platform.

## Decisions

### Technology baseline: Python services and a TypeScript web client

The initial backend uses Python with FastAPI for versioned HTTP and streaming endpoints, Pydantic-compatible boundary schemas, and LlamaIndex's Python framework behind project-owned ports. API and worker deployments share the same domain and application packages so ingestion and query behavior do not diverge. Relational persistence uses explicit migrations and repository interfaces, and automated backend tests use the Python test ecosystem.

The user-facing chat and administration client uses TypeScript with a component-based web framework and a generated or contract-tested API client. The UI does not contain authoritative authorization or retrieval logic. Exact package versions are pinned during implementation after compatibility checks.

An end-to-end TypeScript stack was considered, but the Python baseline was selected for the initial release because the planned document-processing, evaluation, and LlamaIndex workflow surface is centered on the Python ecosystem. Provider-specific infrastructure remains behind adapters, so this choice does not fix the model, search, object-storage, queue, or telemetry vendors.

### 1. Start with a modular API application and separately scalable workers

The initial deployment consists of:

- a stateless API application serving chat, administration, configuration, source access, and evaluation APIs;
- asynchronous ingestion workers consuming durable jobs;
- optional scheduled connector workers using the same ingestion contract;
- external persistent stores and model/search providers.

Inside the API codebase, modules own their domain contracts and persistence boundaries: identity/access, knowledge catalog, ingestion control, assistant configuration, query orchestration, quality/evaluation, audit, and provider adapters. Modules communicate through typed application commands, queries, and domain events rather than importing each other's persistence models.

This shape keeps early deployment and transactions manageable while allowing ingestion, evaluation, or query workloads to become separate services later. Starting with microservices was rejected because there is no demonstrated load or team boundary to justify distributed consistency and operational overhead. A single synchronous process was rejected because parsing, embedding, connector synchronization, and evaluation are long-running and retryable workloads.

### 2. Keep LlamaIndex behind project-owned ports

LlamaIndex provides ingestion transformations, document/node processing, retrievers, rerankers, answer synthesis, workflows, and instrumentation integrations. Project-owned interfaces define the product contracts for:

- `ConnectorAdapter`
- `ParserAdapter`
- `EmbeddingAdapter`
- `LexicalRetriever`
- `VectorRetriever`
- `RerankerAdapter`
- `ModelGateway`
- `GuardrailAdapter`
- `TraceSink`

Framework documents and nodes are converted at the boundary to domain documents, versions, chunks, citations, and retrieval candidates. API schemas and database records never expose serialized LlamaIndex internals.

Directly exposing LlamaIndex query engines or storage objects was rejected because it would make authorization, migrations, testing, and provider replacement dependent on framework behavior. Building the complete RAG stack without LlamaIndex was rejected because it would duplicate mature orchestration and integration capabilities.

### 3. Use explicit product aggregates and immutable versions

The core aggregates are:

- `Tenant`, `Principal`, `Group`, and role or policy assignments;
- `KnowledgeSpace` and `DataSource`;
- `Document`, immutable `DocumentVersion`, and derived `Chunk` provenance;
- `AssistantProfile` and immutable activated `AssistantVersion`;
- versioned prompt, model, retrieval, citation, refusal, and guardrail policies;
- `Conversation`, `QueryExecution`, `Answer`, and `Citation`;
- `IngestionJob` and individual attempts;
- `EvaluationDatasetVersion`, `EvaluationRun`, metric results, and `Feedback`;
- append-only `AuditEvent`.

PostgreSQL is the system of record for these aggregates, state machines, relationships, outbox events, and audit indexes. Immutable versions make historical answers and evaluation results reproducible. Mutable in-place assistant or dataset configuration was rejected because it destroys traceability and makes rollback ambiguous.

### 4. Assign each storage technology one authoritative responsibility

- PostgreSQL stores tenant, identity references, knowledge catalog, jobs, configuration, conversations, feedback, evaluation metadata, active-version pointers, audit indexes, and transactional outbox records.
- Object storage stores original content, normalized parsing artifacts, previews, and export artifacts under tenant-scoped keys and retention policies.
- A vector store stores embeddings plus mandatory filterable tenant, knowledge-space, document, version, and ACL metadata.
- A lexical search implementation stores equivalent identifiers and security metadata for keyword retrieval. It may be implemented by a dedicated engine or a selected search backend's hybrid capability.
- Redis or an equivalent ephemeral store supports rate limits, short-lived caches, distributed coordination, and queue mechanics but is not authoritative for business or audit state.
- A durable queue transports ingestion, connector, deletion, and evaluation work.

A transactional outbox bridges PostgreSQL state changes to asynchronous work so successful domain transactions cannot silently lose required indexing or deletion events. Using only a vector store was rejected because it does not provide the required relational lifecycle, audit, configuration, or transaction semantics.

### 5. Model knowledge spaces separately from assistant profiles

A knowledge space owns content scope, membership, data-source associations, lifecycle state, and default policies. An assistant profile is a versioned presentation and behavior configuration that references one or more knowledge spaces plus model, retrieval, prompting, citation, refusal, and guardrail policies.

This many-to-many separation allows the same governed knowledge to power multiple assistants and allows one assistant to combine multiple authorized spaces. Equating one assistant with one index was rejected because it duplicates content, complicates permissions, and prevents independent configuration changes.

### 6. Implement ingestion as a durable staged state machine

The ingestion flow is:

```text
discover/upload -> validate -> store source -> parse -> normalize metadata/ACL
-> chunk -> embed/index to staging -> verify -> publish -> clean superseded data
```

Each job and attempt is durable and idempotent. Stable source identity plus content fingerprint prevents duplicate versions. Chunk identifiers are deterministic within a document version. The new document version is written to staging indexes and verified before a PostgreSQL transaction changes the active-version pointer and emits cleanup events. Queries filter on the active version, so partial indexing never replaces a valid published version.

Deletion and permission-revocation events use higher-priority work lanes. Access revocation updates the authoritative policy and retrieval filters before optional physical cleanup; content deletion additionally tombstones the document, removes it from active retrieval, and schedules derived and source artifact cleanup according to retention policy.

In-place index mutation was rejected because a failed update could mix old and new chunks. Fully rebuilding a knowledge-space index for each change was rejected because it scales poorly and delays urgent revocations.

### 7. Use a deterministic permission-aware online query workflow

The query workflow is:

```text
authenticate -> resolve assistant version -> compute authorized knowledge scope
-> normalize/rewrite query -> dense + lexical retrieval with mandatory filters
-> fuse -> rerank -> evidence threshold -> context assembly
-> answer synthesis -> citation/grounding validation -> output guardrails -> stream terminal result
```

Authorization produces a compiled retrieval scope containing tenant, allowed knowledge spaces, principal and group identifiers, policy or ACL epoch, and any document constraints. Both dense and lexical retrieval receive this scope as mandatory filters. Unauthorized candidates are never reranked, sent to a model, cached as reusable evidence, cited, or returned.

Candidate fusion uses a deterministic rank-fusion policy before reranking. Policy versions define top-k values, thresholds, degradation behavior, context limits, and refusal rules. If evidence is absent or below threshold, the workflow produces an explicit insufficient-evidence result. Conflicting authoritative evidence produces a cited conflict response rather than silently choosing a claim.

Free-form agent planning was rejected for the default query path because authorization, latency, cost, evaluation, and refusal need deterministic control. Specialized agentic or tool workflows can be added later behind a separate capability and policy.

### 8. Treat conversation context as intent memory, not durable evidence

Conversation history can help resolve pronouns, entities, and follow-up intent, but prior retrieved text is not automatically trusted as current evidence. Each fact-seeking turn re-resolves the assistant version, current authorization, active document versions, and retrieval results. Switching assistant profiles starts a new evidence scope.

Short-term conversation state is stored with tenant and user ownership. Long conversations may use a versioned summary, but the original message history remains the auditable source while retained. User conversation text is not added to enterprise knowledge indexes by default.

Reusing old retrieved chunks across turns was rejected because permissions, document versions, and active configuration can change between turns.

### 9. Centralize identity, authorization, and untrusted-content handling

The API gateway verifies OIDC-compatible identity tokens and constructs an immutable request identity. An authorization module evaluates tenant roles, knowledge-space membership, document ACLs, operation type, and policy state with default-deny semantics. Client-supplied tenant and group values are ignored as authority.

Every index record carries filterable `tenant_id`, `knowledge_space_id`, `document_id`, `document_version_id`, and normalized ACL principal or group tokens plus an ACL epoch. Authorization-sensitive caches include tenant, principal or policy scope, assistant version, and ACL epoch in their keys. Revocation increments the relevant epoch and prevents stale cache reuse.

Trusted system policies, untrusted user input, retrieved content, and external outputs remain distinct in prompt construction. Guardrails inspect inputs, retrieved content, generated output, and source access. Secrets are referenced through a secret manager and redacted from API responses, logs, traces, and evaluation exports.

Application-only filtering after broad retrieval was rejected because unauthorized content could still reach rerankers, models, caches, and telemetry.

### 10. Version configuration and route all providers through a model gateway

Assistant changes begin as drafts, validate against tenant ownership, policy compatibility, provider capabilities, secret bindings, and availability, then activate as immutable versions. New queries snapshot the active version at start; in-flight queries do not change configuration mid-execution. Rollback reactivates a historical version after revalidation.

The model gateway supplies common completion, streaming, embedding, and reranking contracts plus timeouts, bounded retries, circuit breaking, rate limits, token accounting, cost attribution, and policy-controlled fallback. Provider profiles declare capabilities so incompatible combinations fail during activation rather than user requests.

Global mutable framework settings were rejected because they prevent tenant-specific configuration and make concurrent versioned execution unsafe.

### 11. Build observability and evaluation into the execution contracts

All API requests, query workflows, ingestion attempts, model calls, retrieval stages, and evaluation items share correlation identifiers and emit OpenTelemetry-compatible spans and metrics through a telemetry boundary. Content capture is separately policy-controlled from structural telemetry.

General evaluation datasets cover at least policy documents, IT help content, and product documentation, including answerable, unanswerable, conflicting, permission-sensitive, multi-turn, update, and deletion cases. Evaluation records retrieval relevance and coverage, groundedness, citation correctness, answer relevance, refusal correctness, latency, and access-control results separately. Release gates compare immutable candidate and baseline versions; overrides require explicit authorization and audit.

Only end-to-end user ratings were rejected because they do not identify whether a regression came from parsing, retrieval, reranking, generation, citation, or refusal.

### 12. Expose stable versioned APIs and idempotent mutation semantics

External APIs are versioned and use stable domain schemas rather than framework types. Long-running mutations return job identifiers. Upload, synchronization, deletion, retry, configuration activation, and evaluation-start operations accept idempotency keys where duplicate client submission is plausible. Streaming query responses include a query identifier and an explicit terminal status.

Errors use stable categories with correlation identifiers; restricted resource existence, provider secrets, internal prompts, stack traces, and storage paths are never exposed. Source preview and download re-check authorization at access time rather than trusting a citation created earlier.

## Risks / Trade-offs

- **[Risk] General-purpose abstractions grow into an over-configurable platform before a useful assistant exists** → Ship file upload, a small format set, one default hybrid policy, one model path, citations, refusal, and three-domain evaluation first; add adapters only at demonstrated seams.
- **[Risk] ACL filters differ across vector and lexical stores** → Define one compiled authorization scope, require contract tests for every search adapter, and include cross-tenant and revocation tests in mandatory release gates.
- **[Risk] Permission revocation races with caches or asynchronous index cleanup** → Make authorization authoritative outside the index, include ACL epochs in filters and cache keys, fail closed on stale policy state, and prioritize revocation events.
- **[Risk] Dual retrieval systems increase operational complexity** → Permit a backend with native hybrid support, but preserve separate lexical and vector ports and identical security metadata contracts.
- **[Risk] Parsing quality varies widely across PDFs and Office files** → Preserve originals and parsed artifacts, expose per-stage failures, evaluate representative layout and table samples, and keep parser replacement behind an adapter.
- **[Risk] Provider fallback changes answer quality or data residency** → Make fallback opt-in per policy, validate provider capabilities and compliance metadata, and record the actual provider in traces and evaluation results.
- **[Risk] Versioned indexes temporarily increase storage use** → Clean superseded data asynchronously after safe publication and enforce configurable retention while preserving audit metadata.
- **[Risk] Traces and evaluation artifacts capture sensitive content** → Separate structural telemetry from content capture, apply tenant-specific redaction and retention before persistence, and restrict exports.
- **[Trade-off] Modular monolith limits independent API module scaling initially** → Keep worker workloads separate now and preserve module/domain boundaries that permit later extraction using the same contracts.

## Migration Plan

This is a greenfield introduction rather than a migration of existing application behavior.

1. Establish the application and worker deployment skeleton, configuration loading, health/readiness behavior, telemetry, and local development dependencies.
2. Create PostgreSQL schemas and migrations, object-storage conventions, durable queue/outbox processing, tenant identity integration, and authorization foundations.
3. Implement knowledge-space and assistant-configuration administration with immutable versions, validation, activation, rollback, and audit.
4. Implement file ingestion through staged publication using one parser path, one embedding path, vector indexing, lexical indexing, retries, deletion, and revocation.
5. Implement the permission-aware query workflow, streaming protocol, citations, refusal, conversation handling, and source access.
6. Add feedback, evaluation datasets and runs, release gates, dashboards, alerts, retention, and redaction controls.
7. Validate with a general corpus spanning policy, IT help, and product documentation, including security and failure-injection suites, before enabling production tenants.

Rollback uses immutable application releases, database migration compatibility, assistant-version rollback, and preservation of the last valid active document versions. Destructive schema cleanup and superseded index deletion occur only after the rollback window. If a new query release fails, route traffic to the previous release and reactivate the prior validated assistant versions. If ingestion publication fails, retain the last valid active version.

## Open Questions

- Which OIDC identity provider, secret manager, object store, queue, vector store, and lexical search implementation will be selected for the first deployment?
- Which hosted or self-managed LLM, embedding, reranking, and parsing providers satisfy the deployment's privacy and residency requirements?
- What initial document-size, corpus-size, concurrency, freshness, latency, availability, and cost targets should be used as benchmark baselines?
- Which exact file formats form the first supported allow-list beyond the three-domain evaluation corpus?

These choices remain behind the defined adapters or configuration and can be finalized without changing the capability contracts or task structure.
