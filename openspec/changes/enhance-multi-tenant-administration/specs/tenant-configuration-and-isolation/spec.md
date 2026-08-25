## Purpose

Defines versioned tenant policy, quota enforcement, usage visibility, and defense-in-depth isolation across all storage, retrieval, execution, telemetry, and export boundaries.

## ADDED Requirements

### Requirement: Tenant configuration is typed and versioned
The system SHALL store tenant configuration as immutable versions validated against a documented schema. Configuration SHALL cover locale and time zone, retention, upload limits, quotas, feature flags, allowed provider policy, and default RAG policy references without embedding secret values.

#### Scenario: Valid configuration is activated
- **WHEN** a tenant administrator submits a schema-valid update referencing permitted tenant-owned policies and providers
- **THEN** the system creates an immutable configuration version, atomically activates it, and returns its version, revision, and stable hash

#### Scenario: Invalid or unknown setting is submitted
- **WHEN** a configuration update violates a type, range, enum, ownership, compatibility, or allow-list constraint
- **THEN** the system rejects the entire update with field-level errors and preserves the active version

#### Scenario: Configuration contains a secret value
- **WHEN** a caller submits a raw provider credential or other prohibited secret-bearing field
- **THEN** the system rejects the value and requires an authorized secret-binding reference instead

### Requirement: Configuration updates support concurrency and rollback
Configuration mutations SHALL require the current revision or equivalent conditional-update token, SHALL support idempotent retries, and SHALL permit rollback only by revalidating and activating a prior immutable version as a new current version.

#### Scenario: Two administrators edit concurrently
- **WHEN** a configuration update is based on a stale revision
- **THEN** the system returns a conflict containing the current non-secret revision metadata and does not overwrite the newer configuration

#### Scenario: Administrator rolls back configuration
- **WHEN** an authorized administrator selects a compatible prior version
- **THEN** the system revalidates it against current tenant resources, creates a new activation record, invalidates affected caches, and audits the rollback

### Requirement: Quotas are enforced before expensive or durable work
The system SHALL enforce configured limits for members, groups, knowledge spaces, documents, stored bytes, upload size, ingestion concurrency, query rate, and configured provider usage. Quota checks SHALL be tenant scoped and race safe.

#### Scenario: Tenant reaches a hard quota
- **WHEN** an operation would exceed a configured hard limit
- **THEN** the system rejects it before object upload, queue publication, embedding, indexing, or model invocation and returns the limit name plus non-sensitive current usage

#### Scenario: Tenant approaches a warning threshold
- **WHEN** current usage crosses a configured warning threshold without exceeding the hard limit
- **THEN** the system permits the operation, exposes the warning in authorized usage views, and emits an operational metric without exposing another tenant's usage

### Requirement: Authorized users can inspect tenant usage and effective settings
The system SHALL provide tenant-scoped views of effective non-secret configuration, active version history, quota limits, current usage, warning states, and relevant configuration provenance according to role.

#### Scenario: Tenant administrator opens settings
- **WHEN** an active tenant administrator requests configuration and usage
- **THEN** the system returns effective settings, version metadata, quota consumption, and validation status without secret values

#### Scenario: Platform administrator views fleet usage
- **WHEN** a platform administrator requests fleet-level operational summaries
- **THEN** the system returns tenant metadata and aggregate usage permitted by platform policy but not document, prompt, answer, or secret content

### Requirement: Tenant isolation is enforced across every data boundary
Every tenant-owned relational row, object key, queue or outbox message, cache key, search document, trace, metric attribution, evaluation item, export, and background task SHALL carry an immutable tenant identifier and SHALL be read or mutated only under a matching validated tenant context.

#### Scenario: Relational identifier belongs to another tenant
- **WHEN** a request supplies an otherwise valid resource identifier owned by a different tenant
- **THEN** application authorization and database isolation both prevent access and return a non-enumerating response

#### Scenario: Search provider returns a foreign candidate
- **WHEN** a vector or lexical provider returns a candidate whose tenant metadata does not match the compiled authorization scope
- **THEN** post-retrieval validation removes the candidate, records a security signal, and prevents it from reaching model context or citations

#### Scenario: Cache key lacks complete tenant context
- **WHEN** a cache operation cannot construct a key containing tenant, authorization epoch, configuration version, and relevant resource version
- **THEN** the system bypasses or rejects the cache operation instead of using a shared or partial key

### Requirement: Background work revalidates tenant context
Workers SHALL treat tenant ID, authorization epoch, configuration version, and correlation ID as required message metadata and SHALL revalidate current tenant lifecycle and applicable policy before processing or publishing results.

#### Scenario: Queued work has stale configuration
- **WHEN** a worker receives a job created under an older tenant configuration version
- **THEN** the worker applies the operation's declared snapshot policy or terminates it as stale; it never silently mixes old and current configuration

#### Scenario: Queue message lacks tenant metadata
- **WHEN** a worker receives a message without a valid tenant identifier or correlation metadata
- **THEN** the worker quarantines the message, emits a sanitized operational event, and performs no tenant data access

### Requirement: Tenant telemetry, audit, and exports remain isolated
Telemetry, audit search, evaluation results, feedback, and exports SHALL be filtered by validated tenant context, SHALL apply content-capture and redaction policy from the active tenant configuration, and SHALL never aggregate tenant-identifying content into another tenant's view.

#### Scenario: Tenant operator exports audit data
- **WHEN** an authorized tenant operator exports an allowed time range
- **THEN** the export contains only that tenant's authorized events, active redaction policy, configuration provenance, and an integrity reference

#### Scenario: Tenant disables content capture
- **WHEN** the active tenant configuration disables prompt and response capture
- **THEN** structural telemetry and required security audit events remain available while prompt, answer, and retrieved content are omitted or irreversibly redacted

