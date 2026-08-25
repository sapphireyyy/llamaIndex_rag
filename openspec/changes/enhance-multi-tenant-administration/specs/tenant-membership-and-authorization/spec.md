## Purpose

Defines tenant-level identity, membership, group, invitation, and role behavior so every tenant operation is authorized from current persisted state and revoked access stops immediately.

## ADDED Requirements

### Requirement: Persisted tenant principals and memberships
The system SHALL maintain tenant-scoped principals, groups, user-to-group links, and role assignments. Each membership and assignment SHALL have an explicit lifecycle state and SHALL be unique within its tenant boundary.

#### Scenario: OIDC subject first joins a tenant
- **WHEN** an accepted invitation or authorized administrator assignment references an authenticated OIDC subject
- **THEN** the system creates or links a tenant-scoped principal and an active membership without changing records in any other tenant

#### Scenario: Same external subject belongs to multiple tenants
- **WHEN** one OIDC subject has active memberships in two tenants
- **THEN** each membership, group link, effective role, and audit history remains independently manageable in its tenant

### Requirement: Tenant and platform roles remain separate
The system SHALL support tenant roles `administrator`, `editor`, `reader`, `quality`, and `operator`, and a separate trusted `platform_administrator` role. Platform roles SHALL never be inferred from tenant group names or tenant role assignments.

#### Scenario: Tenant administrator manages tenant members
- **WHEN** an active tenant administrator creates, updates, suspends, or removes a member, group, invitation, or tenant role assignment in the selected tenant
- **THEN** the system permits the operation according to the tenant permission matrix and records an audit event

#### Scenario: Tenant role attempts platform operation
- **WHEN** any tenant role calls a platform-scoped tenant lifecycle endpoint
- **THEN** the system denies the operation regardless of the tenant's name, groups, or local settings

### Requirement: Request identity is resolved from trusted authentication and current state
The system MUST verify the OIDC token before resolving a tenant, MUST validate the selected tenant and active membership from persisted state, and MUST calculate effective tenant roles from current direct and group assignments. Client-supplied tenant identifiers and token role claims SHALL NOT independently authorize tenant data access.

#### Scenario: Token role exceeds persisted assignment
- **WHEN** a valid token claims `administrator` but the selected tenant stores only an active `reader` assignment
- **THEN** the request identity receives only the persisted effective tenant roles and administrator operations are denied

#### Scenario: Membership is suspended after token issuance
- **WHEN** a membership becomes suspended while its OIDC token remains valid
- **THEN** the next tenant-scoped request is denied and no cached role or retrieval scope is reused

#### Scenario: Identity dependencies are unavailable
- **WHEN** tenant, membership, or group authorization state cannot be loaded consistently
- **THEN** the system fails closed and returns a dependency or authorization error without falling back to token claims

### Requirement: Tenant permission matrix is centralized
The system SHALL evaluate platform operations, tenant administration, knowledge administration, ingestion, query, configuration, quality, operations, telemetry, and audit actions through one default-deny permission matrix.

#### Scenario: Unknown operation is requested
- **WHEN** an authorization check uses an operation not defined in the permission matrix
- **THEN** the system denies the operation and records a sanitized authorization diagnostic

#### Scenario: Reader attempts a write
- **WHEN** a reader attempts to change tenant settings, upload content, modify an assistant, or manage membership
- **THEN** the system denies the operation before any mutation or external provider call

#### Scenario: Quality role evaluates content
- **WHEN** an active quality member operates on an authorized evaluation resource
- **THEN** the system permits quality actions but does not grant tenant administration or knowledge mutation rights

### Requirement: Invitations are scoped, expiring, and single use
The system SHALL allow authorized tenant administrators to issue, resend, revoke, and accept tenant invitations with a role, expiry, and normalized destination. Invitation secrets SHALL be stored only as hashes and SHALL be single use.

#### Scenario: Valid invitation is accepted
- **WHEN** the intended authenticated subject accepts a valid unexpired invitation
- **THEN** the system atomically activates the tenant membership, consumes the invitation, increments the authorization epoch, and audits the acceptance

#### Scenario: Invitation is invalid or expired
- **WHEN** a subject presents a revoked, expired, already-used, mismatched, or cross-tenant invitation
- **THEN** the system returns a stable non-enumerating error and changes no membership state

### Requirement: Membership and group changes revoke access immediately
Every change affecting effective tenant or knowledge-space access SHALL increment a tenant authorization epoch, invalidate tenant authorization and retrieval caches, and apply to new requests and background work immediately.

#### Scenario: Last administrator removal is attempted
- **WHEN** an administrator tries to remove, suspend, or demote the tenant's final active administrator
- **THEN** the system rejects the change unless a platform administrator performs an audited recovery operation that leaves another active administrator

#### Scenario: Group access is revoked
- **WHEN** a user is removed from a group that grants tenant or knowledge-space access
- **THEN** the next request and retrieval use the new authorization epoch and cannot return documents previously visible only through that group

### Requirement: Membership administration is tenant isolated and concurrency safe
Membership, group, invitation, and role APIs SHALL require tenant scope, optimistic concurrency where records can be edited, idempotency for retryable mutations, and non-enumerating cross-tenant errors.

#### Scenario: Cross-tenant member identifier is submitted
- **WHEN** a tenant administrator uses a principal, group, membership, or invitation identifier owned by another tenant
- **THEN** the system returns a non-enumerating error and does not reveal or mutate the foreign record

#### Scenario: Duplicate membership request is retried
- **WHEN** an identical member assignment is repeated with the same idempotency key
- **THEN** the system returns the original assignment and emits no duplicate membership or audit event

