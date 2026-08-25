## Purpose

Defines fail-closed tenant isolation, identity and permission enforcement, content and prompt safety, sensitive-data handling, and auditable evidence for enterprise operations.

## ADDED Requirements

### Requirement: Every protected operation uses verified identity and tenant context
The system MUST authenticate protected requests and derive tenant, user, and group claims from a trusted identity context. Client-supplied tenant or group identifiers SHALL NOT override verified claims.

#### Scenario: Accept a verified identity
- **WHEN** a request carries a valid trusted identity with tenant and principal claims
- **THEN** the system evaluates the operation within that verified tenant and principal context

#### Scenario: Reject an invalid identity
- **WHEN** a protected request has missing, invalid, expired, or unverifiable identity credentials
- **THEN** the system denies the request before reading or mutating protected resources

### Requirement: Authorization is centralized and defaults to deny
The system SHALL evaluate protected administration, ingestion, query, preview, download, configuration, and evaluation operations through consistent authorization policies. Missing, stale, or indeterminate authorization data MUST produce denial rather than permissive fallback.

#### Scenario: Authorized administrative operation
- **WHEN** a verified principal has the required tenant and resource role
- **THEN** the system permits the operation and records the authorization outcome

#### Scenario: Indeterminate policy result
- **WHEN** required membership or policy data cannot be loaded or evaluated
- **THEN** the system denies the operation and records a sanitized failure event

### Requirement: Untrusted content cannot redefine system instructions
The system SHALL treat ingested text, retrieved text, user input, and external tool output as untrusted data. It MUST isolate trusted system policy from that data and SHALL detect or neutralize attempts to override instructions, disclose secrets, or cross authorization boundaries.

#### Scenario: Retrieved prompt-injection text
- **WHEN** retrieved document content instructs the model to ignore platform policy or reveal restricted information
- **THEN** the system treats the instruction as document content, does not grant it control, and records the applicable safety signal

#### Scenario: User requests restricted disclosure
- **WHEN** a user asks the assistant to expose hidden prompts, credentials, or unauthorized documents
- **THEN** the system refuses the restricted portion and does not disclose protected values

### Requirement: Sensitive data and secrets are protected
The system MUST protect credentials, provider secrets, source tokens, and configured sensitive fields from user-visible responses, traces, logs, and exported evaluation data. Stored and transmitted protected data SHALL use organization-approved encryption controls.

#### Scenario: Inspect a failed provider call
- **WHEN** an authorized operator views a trace for a failed provider request
- **THEN** the trace exposes diagnostic metadata while redacting credentials and configured sensitive content

#### Scenario: Export evaluation results
- **WHEN** an authorized user exports an evaluation run
- **THEN** the export applies the configured redaction and retention policy before data leaves the system

### Requirement: Security-relevant operations produce tamper-evident audit events
The system SHALL record tenant, principal, action, resource, authorization result, timestamp, request or job correlation identifier, and outcome for security-relevant operations. Authorized users may search and export audit events, but normal product operations MUST NOT modify or delete individual events.

#### Scenario: Audit a configuration activation
- **WHEN** an authorized administrator activates an assistant configuration version
- **THEN** the system records the actor, assistant, previous version, new version, timestamp, and outcome in the audit trail

#### Scenario: Audit a denied cross-tenant request
- **WHEN** a principal attempts a cross-tenant resource operation
- **THEN** the system denies the request and records an access-safe audit event without copying restricted resource content

### Requirement: Error responses do not leak protected internals
The system SHALL return stable, sanitized error categories and correlation identifiers to clients. It MUST NOT expose stack traces, credentials, internal prompts, storage locations, or unauthorized resource existence through error differences.

#### Scenario: Internal dependency failure
- **WHEN** a protected operation fails because of an internal dependency
- **THEN** the client receives a sanitized error category and correlation identifier while detailed diagnostics remain restricted to authorized telemetry

