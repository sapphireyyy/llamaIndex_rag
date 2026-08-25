## Context

See `proposal.md` for motivation and the three delta specifications for required behavior. The current system already stores `tenant_id` on most domain rows and provider metadata, derives a single-tenant `RequestIdentity` from OIDC claims, applies a centralized operation-to-role matrix, increments a tenant ACL epoch, and scopes object/search data by tenant. Its gaps are control-plane APIs, persisted tenant memberships and roles, tenant lifecycle state, typed configuration versions, quota accounting, and a database-level tenant barrier.

The implementation must preserve the existing FastAPI/application/infrastructure boundaries, provider-neutral adapters, PostgreSQL production path, SQLite unit-test path, Keycloak acceptance environment, and default-deny retrieval behavior. The change is security sensitive and crosses API, workers, persistence, search, cache, telemetry, and the web client, so a design artifact is required.

## Goals / Non-Goals

**Goals:**

- Introduce separate platform-control-plane and tenant-data-plane identities and authorization paths.
- Make active tenant membership and current persisted roles authoritative for tenant operations.
- Provide reversible tenant lifecycle, membership/group administration, invitations, typed immutable settings, quotas, and usage views.
- Add defense-in-depth tenant isolation at application, relational database, object, queue, cache, search, telemetry, and worker boundaries.
- Deploy through an expand/backfill/enforce sequence without silently retaining claim-only authorization in staging or production.

**Non-Goals:**

- Hard tenant purge, customer billing, payment plans, identity-provider provisioning, SCIM synchronization, or invitation-email delivery.
- Redesigning knowledge-space ACL semantics or granting platform operators tenant content access.
- Per-tenant physical databases or search clusters; the first implementation uses secure logical isolation on the existing shared providers.
- Dynamic custom role definitions; the fixed role matrix remains versioned in code and specs.

## Decisions

### 1. Split authentication from tenant-context resolution

OIDC verification will first create an immutable `AuthenticatedSubject` containing issuer, subject, trusted platform roles, groups, token ID, and the optional `tenant_id` hint. A second resolver will create `RequestIdentity` only after selecting a tenant from `X-Tenant-ID` or the token hint and loading the active tenant, active principal, direct/group assignments, and authorization epoch from PostgreSQL.

Platform endpoints will depend on the subject plus `platform_administrator`; tenant endpoints will depend on the resolved tenant identity. Platform role claims come only from an explicit configured claim/realm role, never from a tenant group name. Token tenant roles are migration hints, not grants. `X-Tenant-ID` is a selector and is ignored as authority until the database membership check succeeds.

This separation keeps OIDC responsible for authentication and the application database responsible for current authorization and immediate revocation. Continuing to trust token roles was rejected because role removal would remain ineffective until token expiry. Encoding all tenant memberships into the token was rejected because it makes tenant switching, revocation, and large memberships hard to operate.

### 2. Use explicit platform and tenant API surfaces

The platform control plane will use `/api/v1/platform/tenants` and explicit tenant IDs for lifecycle and fleet summaries. Authenticated subjects will use `/api/v1/tenants` to discover their memberships. Tenant administration will use `/api/v1/tenant/*` under the already validated current tenant context for members, groups, invitations, settings, versions, usage, and audit links. Existing data-plane endpoints retain their URLs and receive the resolved tenant dependency centrally.

Responses will echo the resolved tenant ID and revision in non-secret metadata. Cross-tenant identifiers will retain the existing non-enumerating error policy. A tenant selector in arbitrary resource payloads was rejected because it creates inconsistent authorization seams.

### 3. Extend the relational control-plane model with immutable versions

`TenantRecord` will gain unique `slug`, lifecycle `state`, optimistic `revision`, suspension/archive metadata, active configuration reference, and timestamps. New records will include:

- `TenantMembershipRecord`: tenant, principal, state, direct role, revision, provenance.
- `TenantGroupMembershipRecord`: tenant, principal, group, revision, with composite tenant ownership constraints.
- `TenantGroupRoleRecord`: tenant, group, role, revision.
- `TenantInvitationRecord`: tenant, normalized destination, token hash, requested role, state, expiry, issuer, consumption metadata.
- `TenantConfigurationVersionRecord`: tenant, sequential version, schema version, fully resolved typed payload, stable hash, creator, activation metadata.
- `TenantUsageRecord`: tenant, durable quota counters, reconciliation timestamp, and revision.

Existing principal and group records remain tenant scoped. Composite uniqueness and foreign-key constraints will prevent linking principals, groups, memberships, or configuration records across tenants. Configuration versions are append-only; rollback creates a newly activated version referencing the prior source version.

A mutable JSON blob on `TenantRecord` was rejected because it provides no safe concurrency, provenance, or rollback. A fully generic role/permission schema was rejected because it would multiply authorization and migration risk beyond the requested fixed roles.

### 4. Add database row isolation for tenant-owned data

Application services will continue to require tenant predicates and centralized authorization. PostgreSQL migrations will additionally enable and force row-level-security policies on tenant-owned data-plane tables, using a transaction-local `app.tenant_id` set from a validated execution context. API and worker session helpers will set the context with `SET LOCAL`, ensuring pooled connections cannot retain it after a transaction.

Control-plane tenant, membership, invitation, and configuration tables must support pre-selection discovery and therefore remain accessible only through narrowly scoped platform/identity repositories that always apply explicit subject or tenant predicates. The application database role will have no general tenant-data RLS bypass. Migration ownership remains operationally separate from request-time access.

SQLite cannot enforce PostgreSQL RLS, so unit tests will retain repository guards while integration/security suites run the RLS contract against PostgreSQL. Application-only filtering was rejected as the sole boundary because a missed predicate could expose data. Per-tenant databases were rejected for the initial change because provisioning, migrations, fleet operations, and cost would become disproportionate.

### 5. Represent tenant configuration as a resolved typed snapshot

A project-owned typed schema will define locale/time zone, retention, upload policy, durable quotas, rate/concurrency limits, feature flags, allowed provider-profile references, secret-binding references, and default RAG policy IDs. Unknown fields and raw secret-bearing values will be rejected. Platform defaults will be merged and validated when a tenant configuration version is created; the stored active payload is fully resolved so runtime behavior cannot drift when platform defaults later change.

Updates require `If-Match` or a revision field and an idempotency key. Activation occurs in the same transaction as the audit/outbox event and cache/configuration epoch update. Services snapshot the active configuration version for queries and jobs where reproducibility matters; lifecycle and authorization policy are always revalidated live.

Runtime merging on every request was rejected because it makes historical execution irreproducible. In-place edits were rejected because they prevent audit and safe rollback.

### 6. Enforce durable quotas transactionally and rate limits through coordination storage

Durable cardinality and byte quotas will use a tenant usage row locked in the same transaction as the accepted mutation. Expensive work is rejected before object writes, queue publication, embedding, indexing, or model calls. Deletion decrements or marks counters for reconciliation according to retention state. A periodic reconciliation operation recomputes counters from authoritative data and emits drift metrics.

High-frequency query/upload rates and concurrency will use tenant-prefixed Redis keys through the coordination-store abstraction, with fail-closed or documented degraded behavior per limit type. Durable quota truth remains PostgreSQL; Redis is not the source of record. Counting rows on every request was rejected for latency and race reasons, while Redis-only quotas were rejected because eviction or outage could lose durable limits.

### 7. Use lifecycle and authorization epochs for immediate invalidation

Tenant lifecycle, membership, group, role, and relevant configuration changes will increment the existing tenant ACL epoch or a unified authorization epoch. All authorization, retrieval, configuration, and rate/cache keys will include tenant ID plus the applicable epoch and configuration version. Lifecycle mutations publish transactional outbox events consumed by workers and cache/search adapters.

Workers must validate required message metadata, reload the tenant state, and apply an explicit configuration snapshot policy before processing. Suspension creates high-priority cancellation/invalidation work; already terminal jobs are not silently replayed on reactivation.

Time-based cache expiry alone was rejected because it cannot provide immediate revocation. Trusting the enqueue-time tenant state was rejected because queued work may run after suspension.

### 8. Preserve provider isolation and add post-retrieval validation

Object paths, queue/outbox messages, Redis keys, Qdrant/OpenSearch metadata and filters, telemetry attributes, evaluation records, and exports will continue or be updated to require tenant ID. Search adapters will validate tenant metadata again after provider retrieval and before fusion/model context. A foreign candidate is discarded and emits a security signal.

Shared collections/indexes remain acceptable because every indexed document carries tenant, space, version, ACL, and epoch metadata and both provider-side and application-side filters are mandatory. Dedicated per-tenant provider resources remain a future deployment option behind the same adapters.

### 9. Build role-aware administration views without duplicating policy

The web client will add a tenant switcher, platform tenant console, and a tenant administration area for overview, lifecycle state, members, groups, invitations, settings versions, quota usage, and audit navigation. The API remains the authority; the UI hides unavailable actions for usability but never relies on client-side checks for security. Suspended tenants show an explicit blocked state and allow only role-appropriate control-plane recovery views.

The API client will attach the selected tenant header from one state boundary and clear tenant-scoped cached UI data on switch. Storing a tenant ID independently in every screen was rejected because stale selections could mix UI state.

### 10. Bootstrap and compatibility are explicit deployment stages

Development/test startup will seed the demo tenant, principal, active administrator membership, and configuration version. Production will require an operator bootstrap command to confirm at least one trusted platform administrator and assign the first administrator for each existing tenant before enforcement is enabled. A claim-compatibility mode may exist only in development/test and must be rejected by staging/production startup validation.

This makes the authorization behavior intentionally breaking but prevents a silent production fallback. Keeping a permanent legacy fallback was rejected because it would undermine persisted revocation.

## Risks / Trade-offs

- [Migration can lock out every administrator] → Use expand/backfill verification, a one-purpose bootstrap command, readiness checks, and an audited platform recovery path before enabling enforcement.
- [RLS context could leak through connection pooling] → Use transaction-local settings only, reset/pool tests, forced RLS, and production PostgreSQL isolation tests for API and worker sessions.
- [Platform control-plane repositories can see membership metadata across tenants] → Keep them separate from tenant-content repositories, return minimum fields, require trusted platform or same-subject filters, and audit every platform query/mutation.
- [Usage counters can drift after failures or retention cleanup] → Update them transactionally where possible, make mutations idempotent, reconcile periodically, and alert on drift.
- [Immediate invalidation increases cache misses] → Use monotonically increasing epochs and targeted prefixes; correctness takes priority over hit rate.
- [Logical provider isolation shares blast radius] → Require tenant metadata twice, maintain cross-tenant failure tests, and preserve adapter support for later per-tenant infrastructure.
- [Fixed roles may not fit every future organization] → Keep the permission matrix centralized and versioned; introduce custom roles only through a separate spec with migration semantics.
- [Invitation delivery is incomplete without email/SCIM] → Implement secure invitation creation and acceptance plus a delivery adapter boundary; production delivery integration remains separately configurable.

## Migration Plan

1. Add nullable/new-default tenant lifecycle fields, configuration, membership, invitation, usage, and composite-ownership schema without changing current authorization behavior.
2. Backfill every existing tenant to `active`, create configuration version 1 from current defaults, calculate usage, repair tenant ownership constraints, and seed development fixtures.
3. Deploy the bootstrap command and verify trusted platform administrators plus at least one active administrator membership per tenant; fail readiness when production prerequisites are missing.
4. Deploy subject resolution, tenant discovery/switching, administration APIs, audit/outbox events, quota checks, and the web views behind membership enforcement in development/test.
5. Add PostgreSQL RLS policies and tenant-aware session context, run cross-tenant API/worker/provider acceptance tests, and enable persisted membership enforcement in staging.
6. Enable production enforcement only after backfill and access reports pass; monitor denial, RLS, quota-drift, stale-job, and foreign-search-candidate signals.
7. Roll back the application by disabling new routes and restoring the prior binary only before RLS enforcement. After RLS is enabled, rollback uses the prepared compatibility release that still sets tenant context; schema/data remain additive. Destructive migration rollback requires a verified backup and is not part of ordinary application rollback.
