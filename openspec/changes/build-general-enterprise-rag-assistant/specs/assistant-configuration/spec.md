## Purpose

Defines versioned assistant profiles and provider-neutral policies so administrators can safely configure knowledge scope, models, retrieval, citations, refusal, and guardrails without code changes.

## ADDED Requirements

### Requirement: Administrators can compose assistant profiles
The system SHALL allow an authorized tenant administrator to create a draft assistant profile containing a name, one or more knowledge-space references, prompt policy, model profile, retrieval policy, citation policy, refusal policy, and guardrail policy. Profile references MUST remain within the administrator's tenant.

#### Scenario: Create a valid draft profile
- **WHEN** an authorized administrator supplies valid tenant-owned component references
- **THEN** the system creates a draft assistant profile version without making it available for end-user queries

#### Scenario: Reject a cross-tenant reference
- **WHEN** a draft profile references a knowledge space or policy owned by another tenant
- **THEN** the system rejects the reference without revealing restricted metadata

### Requirement: Activation requires complete validation
The system SHALL validate required references, provider availability, supported capabilities, policy compatibility, secret bindings, and administrator authorization before activating a profile version. A failed validation MUST leave the currently active version unchanged.

#### Scenario: Activate a valid version
- **WHEN** a draft profile passes validation and an authorized administrator activates it
- **THEN** the system makes that immutable version active for new queries and records the activation

#### Scenario: Reject an invalid version
- **WHEN** validation finds a missing knowledge space, unavailable required provider, incompatible policy, or unresolved secret binding
- **THEN** the system keeps the draft inactive and returns actionable validation findings

### Requirement: Active configuration versions are immutable and traceable
The system SHALL assign an immutable version identifier to each activated assistant configuration. Every query SHALL record the assistant and component versions it used, and editing an active configuration SHALL create a new draft version.

#### Scenario: Edit an active profile
- **WHEN** an administrator changes any active profile setting
- **THEN** the system creates a new draft version while existing and in-flight queries retain their recorded versions

#### Scenario: Inspect a historical query
- **WHEN** an authorized operator inspects a query trace
- **THEN** the trace identifies the exact assistant, prompt, model, retrieval, citation, refusal, and guardrail versions used

### Requirement: Administrators can roll back safely
The system SHALL allow an authorized administrator to reactivate a previously valid profile version. Rollback SHALL affect new queries, preserve historical traces, and produce an audit event.

#### Scenario: Roll back after quality regression
- **WHEN** an authorized administrator selects a previously valid version for rollback
- **THEN** the system activates that version for new queries and records the version transition

#### Scenario: Reject rollback to an invalid environment
- **WHEN** a historical version depends on a removed secret or unavailable mandatory provider
- **THEN** the system rejects activation and leaves the current version unchanged

### Requirement: Model and processing providers use a common configuration contract
The system SHALL represent language models, embedding models, rerankers, parsers, connectors, and guardrails through provider-neutral profiles that declare capabilities and operational limits. Unsupported capability combinations MUST fail validation rather than failing unpredictably during a user query.

#### Scenario: Substitute a compatible provider
- **WHEN** an administrator replaces a provider profile with another profile declaring all required capabilities
- **THEN** the assistant configuration can be validated without changing its knowledge-space or externally visible query contract

#### Scenario: Select an incompatible provider
- **WHEN** a selected provider lacks streaming, embedding, reranking, context, or other capabilities required by the active policies
- **THEN** profile validation identifies the incompatibility and prevents activation

### Requirement: Secret values are never returned through configuration APIs
The system SHALL store secret bindings separately from ordinary configuration and SHALL expose only secret references and status to authorized administrators. It MUST NOT return secret values after initial submission.

#### Scenario: Read model configuration
- **WHEN** an authorized administrator retrieves a model profile that uses a secret binding
- **THEN** the response shows the binding identifier and validity status but not the secret value

