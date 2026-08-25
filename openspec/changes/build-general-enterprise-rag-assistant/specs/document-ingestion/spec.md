## Purpose

Defines a reliable and auditable lifecycle for turning supported enterprise files and connected-source content into versioned, permission-aware, retrievable knowledge.

## ADDED Requirements

### Requirement: The system accepts supported content through a consistent ingestion contract
The system SHALL accept configured supported file types and connector records with a stable source identifier, content, source metadata, and access-control metadata. It SHALL reject unsupported, oversized, malformed, or unsafe inputs with a user-visible reason and without indexing partial content.

#### Scenario: Submit a supported file
- **WHEN** an authorized editor uploads a supported file to an active knowledge space
- **THEN** the system creates an ingestion job and returns the job identifier and initial status

#### Scenario: Reject unsupported input
- **WHEN** an input violates a configured format, size, integrity, or safety constraint
- **THEN** the system rejects the input, records the reason, and creates no active index entries for it

### Requirement: Ingestion jobs expose durable lifecycle state
The system SHALL expose queued, running, succeeded, failed, cancelled, and superseded states for ingestion jobs. A job SHALL report its document, current processing stage, timestamps, attempt count, and a sanitized failure reason when applicable.

#### Scenario: Observe a successful job
- **WHEN** an ingestion job completes all required processing and publication stages
- **THEN** the system marks it succeeded and exposes the published document version and completion time

#### Scenario: Observe a failed job
- **WHEN** a processing stage exhausts its retry policy
- **THEN** the system marks the job failed, preserves the last valid published version, and exposes a sanitized actionable error

### Requirement: Parsed content retains provenance and access metadata
The system SHALL preserve document identity, source location, version, title hierarchy, page or section location when available, and applicable access-control metadata on every retrievable unit. Structured elements such as tables SHALL retain enough provenance to navigate back to their source.

#### Scenario: Parse a paginated document
- **WHEN** a supported paginated document is successfully ingested
- **THEN** every retrievable unit records its document version and page or equivalent source location

#### Scenario: Preserve source permissions
- **WHEN** a source document includes access-control metadata
- **THEN** all retrievable units derived from that version carry an equivalent enforceable access boundary

### Requirement: Reprocessing is idempotent and version-aware
The system SHALL detect unchanged source content using a stable source identity and content fingerprint. Unchanged content SHALL NOT create a new active version, while changed content SHALL produce a new version and atomically replace the prior active version only after successful publication.

#### Scenario: Synchronize unchanged content
- **WHEN** the system receives a source record whose identity and content fingerprint match the active version
- **THEN** it records the synchronization outcome without creating duplicate retrievable units

#### Scenario: Publish an updated version
- **WHEN** changed content completes parsing, validation, and indexing successfully
- **THEN** the new version becomes active atomically and new queries no longer retrieve units from the superseded version

### Requirement: Deletion and revocation propagate to retrieval
The system MUST remove deleted documents, removed data sources, and revoked access from new retrieval results. It SHALL retain only the minimum tombstone and audit metadata required to prove the action while applying configured retention rules to source and derived artifacts.

#### Scenario: Delete a document
- **WHEN** an authorized editor deletes an active document
- **THEN** the system initiates deletion propagation and the document becomes unavailable to new queries, previews, and downloads after completion

#### Scenario: Revoke source access
- **WHEN** a synchronized source reports a permission removal for a user or group
- **THEN** new authorization decisions exclude the affected content for that principal without requiring content regeneration

### Requirement: Failed ingestion can be safely retried
The system SHALL support policy-driven automatic retries for transient failures and authorized manual retries for failed jobs. Retrying MUST be idempotent and MUST NOT replace a valid active version unless the retry completes successfully.

#### Scenario: Retry a transient failure
- **WHEN** a retryable external dependency failure occurs and retry budget remains
- **THEN** the system schedules another attempt and increments the visible attempt count

#### Scenario: Manually retry a failed job
- **WHEN** an authorized editor requests a retry after correcting the failure condition
- **THEN** the system creates or resumes a traceable attempt linked to the original job without duplicating an active version

