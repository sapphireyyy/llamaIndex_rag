## Purpose

Defines the platform control plane for provisioning and operating independent tenants while keeping platform administration separate from access to tenant-owned content.

## ADDED Requirements

### Requirement: Platform-scoped tenant administration
The system SHALL expose tenant creation, discovery, and lifecycle operations only to identities with the trusted platform-administrator role. Platform administration SHALL use an explicit platform scope rather than a caller-selected tenant role.

#### Scenario: Platform administrator creates a tenant
- **WHEN** a platform administrator submits a unique slug, display name, and initial configuration
- **THEN** the system creates the tenant in `provisioning` state, records the creator and correlation ID, initializes configuration and quota state, and returns an opaque tenant identifier

#### Scenario: Tenant administrator cannot create another tenant
- **WHEN** a tenant administrator without the platform-administrator role calls a platform tenant mutation endpoint
- **THEN** the system denies the operation without revealing any tenant outside the caller's memberships

### Requirement: Validated tenant lifecycle state machine
The system SHALL support the ordered states `provisioning`, `active`, `suspended`, and `archived`, SHALL validate every transition, and SHALL require optimistic-concurrency metadata for lifecycle mutations.

#### Scenario: Provisioning completes
- **WHEN** all required tenant defaults and control-plane records have been created successfully
- **THEN** the system atomically changes the tenant from `provisioning` to `active` and emits an auditable tenant-activated event

#### Scenario: Stale lifecycle update
- **WHEN** an administrator submits a lifecycle transition using an outdated tenant revision
- **THEN** the system returns a conflict response and preserves the current tenant state

#### Scenario: Unsupported lifecycle transition
- **WHEN** an administrator requests a transition that is not allowed by the state machine
- **THEN** the system rejects the request with a stable validation error and records no partial state change

### Requirement: Suspended and archived tenants fail closed
The system MUST deny tenant data-plane operations whenever the selected tenant is not active. Suspension and archival SHALL invalidate authorization caches and SHALL prevent queued or retried work from publishing new tenant data.

#### Scenario: Tenant is suspended during an active session
- **WHEN** a tenant is suspended after a user has authenticated
- **THEN** the user's next API, query, ingestion, preview, download, evaluation, or export operation is denied before tenant content is read or written

#### Scenario: Worker receives work for a suspended tenant
- **WHEN** a worker consumes a queued job whose tenant is suspended or archived
- **THEN** the worker records a non-retryable tenant-state outcome and does not publish artifacts, indexes, or outbound events

#### Scenario: Tenant is reactivated
- **WHEN** a platform administrator reactivates a suspended tenant with the current revision
- **THEN** authorized active members can resume new operations while previously cancelled or rejected work remains terminal unless explicitly resubmitted

### Requirement: Tenant discovery and current-tenant selection
The system SHALL list only tenants the authenticated subject is allowed to administer or access, SHALL accept an explicit tenant selection only after membership validation, and SHALL retain the OIDC `tenant_id` claim as the backward-compatible default selection hint.

#### Scenario: User belongs to multiple tenants
- **WHEN** an authenticated subject lists available tenants
- **THEN** the system returns only active or administratively visible tenant memberships for that subject together with role and lifecycle metadata needed by the tenant switcher

#### Scenario: Caller selects an unauthorized tenant
- **WHEN** a caller supplies a tenant identifier for which the subject has no active membership
- **THEN** the system returns an indistinguishable not-found or forbidden response and creates no tenant-scoped request identity

#### Scenario: Existing single-tenant token is used
- **WHEN** an authenticated subject omits explicit tenant selection and the token contains a `tenant_id` claim
- **THEN** the system treats the claim as a tenant hint and still validates active tenant membership before authorizing the request

### Requirement: Platform operators have no implicit content access
The platform-administrator role SHALL permit tenant control-plane operations but MUST NOT by itself grant access to a tenant's documents, assistants, conversations, queries, evaluations, telemetry content, or exports.

#### Scenario: Platform administrator opens tenant content
- **WHEN** a platform administrator without an active tenant membership attempts a tenant data-plane operation
- **THEN** the system denies the request even though the identity can administer tenant lifecycle metadata

#### Scenario: Platform administrator is separately assigned
- **WHEN** a platform administrator also has an active role assignment in the selected tenant
- **THEN** tenant content access is evaluated only from that tenant assignment and the ordinary tenant permission matrix

### Requirement: Tenant lifecycle changes are auditable and idempotent
Every tenant creation and lifecycle mutation SHALL produce an append-only audit event and SHALL support idempotency so retries cannot create duplicate tenants or repeat a transition.

#### Scenario: Tenant creation request is retried
- **WHEN** the same platform administrator repeats a tenant creation request with the same idempotency key and payload
- **THEN** the system returns the original tenant result without creating another tenant or duplicate activation events

#### Scenario: Lifecycle mutation is audited
- **WHEN** a tenant is suspended, reactivated, archived, or restored
- **THEN** the audit record contains actor, target tenant, previous state, new state, reason, correlation ID, and timestamp without containing secrets

