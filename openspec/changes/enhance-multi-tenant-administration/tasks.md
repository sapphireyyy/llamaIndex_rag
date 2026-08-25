## 1. Domain Model and Persistence Foundation

- [x] 1.1 Add tenant lifecycle, membership lifecycle, platform-role, tenant-role, invitation-state, quota, and configuration domain primitives with validated transitions.
- [x] 1.2 Extend tenant, principal, group, and user-group persistence models with lifecycle, revision, provenance, and composite tenant-ownership constraints.
- [x] 1.3 Add tenant membership, group role, invitation, immutable configuration version, and durable usage persistence models and indexes.
- [x] 1.4 Create the additive Alembic migration for tenant control-plane fields and tables, including unique slugs, foreign keys, check constraints, and concurrency revisions.
- [x] 1.5 Add PostgreSQL row-level-security policies for tenant-owned data-plane tables and a safe transaction-local tenant context, while preserving the SQLite test path.
- [x] 1.6 Add backfill helpers that activate existing tenants, create configuration version 1, calculate usage, and verify that every production tenant can receive an administrator.

## 2. Authentication and Tenant Context

- [x] 2.1 Introduce an immutable authenticated-subject type that separates verified OIDC claims and trusted platform roles from tenant authorization.
- [x] 2.2 Split authentication dependencies into subject verification, platform authorization, and persisted tenant-context resolution.
- [x] 2.3 Resolve `X-Tenant-ID` or the OIDC tenant hint only after validating tenant state, principal state, membership, direct roles, group roles, and authorization epoch.
- [x] 2.4 Extend the centralized default-deny permission matrix with platform tenant, tenant administration, membership, configuration, usage, and recovery operations.
- [x] 2.5 Add tenant-aware API and worker session scopes that set and clear PostgreSQL transaction-local tenant context without leaking it through connection pools.
- [x] 2.6 Add runtime validation that forbids claim-only tenant authorization outside development/test and reports missing production bootstrap prerequisites in readiness.

## 3. Tenant Lifecycle Control Plane

- [x] 3.1 Implement platform tenant creation, list, search, detail, and metadata update services with idempotency and optimistic concurrency.
- [x] 3.2 Implement validated provisioning, activation, suspension, reactivation, archival, and restoration transitions with reasons and revisions.
- [x] 3.3 Emit append-only audit and transactional outbox events for tenant creation and every lifecycle transition.
- [x] 3.4 Implement subject-scoped tenant discovery and current-tenant selection that never enumerates unauthorized tenants.
- [x] 3.5 Enforce active tenant and membership checks before all existing administration, ingestion, query, preview, download, evaluation, telemetry, audit, and export operations.
- [x] 3.6 Revalidate tenant lifecycle in workers and terminate or quarantine jobs that are suspended, archived, stale, or missing required tenant metadata.
- [x] 3.7 Expose platform lifecycle and authenticated tenant-discovery APIs with stable errors, revisions, idempotency keys, and resolved-tenant response metadata.

## 4. Tenant Membership, Groups, Roles, and Invitations

- [x] 4.1 Implement tenant-scoped principal and membership listing, assignment, role update, suspension, reactivation, and removal services.
- [x] 4.2 Implement tenant group creation, update, deletion, user-to-group membership, and group-role assignment with composite ownership validation.
- [x] 4.3 Calculate effective roles from current direct and group assignments and increment the authorization epoch on every access-affecting change.
- [x] 4.4 Prevent removal, suspension, or demotion of the last active tenant administrator and add the audited platform recovery operation.
- [x] 4.5 Implement hashed, expiring, revocable, single-use invitations with normalized destinations and atomic acceptance.
- [x] 4.6 Add idempotency, optimistic concurrency, non-enumerating cross-tenant errors, and secret-safe audit records to membership mutations.
- [x] 4.7 Expose tenant member, group, group-membership, group-role, invitation, and recovery APIs through the centralized permission matrix.

## 5. Tenant Configuration, Quotas, and Usage

- [x] 5.1 Define the strict tenant configuration schema for locale, time zone, retention, uploads, durable quotas, rates, concurrency, feature flags, provider policy, secret bindings, and default RAG policies.
- [x] 5.2 Implement fully resolved immutable configuration version creation, validation, activation, history, optimistic concurrency, and rollback-as-new-version.
- [x] 5.3 Reject raw secret values and validate that referenced providers, secret bindings, policies, and resources are allowed, active, and tenant owned.
- [x] 5.4 Implement transactionally locked durable quota reservations and releases for members, groups, spaces, documents, stored bytes, and provider usage before expensive work.
- [x] 5.5 Extend Redis/in-memory coordination adapters with tenant-prefixed rate and concurrency limits derived from the active configuration.
- [x] 5.6 Implement authorized effective-settings, configuration-history, fleet-summary, tenant-usage, quota-warning, and validation-status APIs.
- [x] 5.7 Implement usage reconciliation, drift metrics, warning events, configuration cache invalidation, and secret-safe configuration audit events.

## 6. Defense-in-Depth Isolation

- [x] 6.1 Refactor tenant-owned repository and application queries to require validated tenant context and add guards against unscoped identifier lookup.
- [x] 6.2 Verify and complete tenant prefixes and metadata for object keys, queue/outbox messages, Redis keys, rate keys, and all background-task payloads.
- [x] 6.3 Require tenant, authorization epoch, configuration version, resource version, and policy version in authorization, retrieval, and configuration cache keys.
- [x] 6.4 Add post-retrieval tenant/version/ACL validation for dense and lexical candidates before fusion, model context, and citations, with security telemetry for rejected foreign candidates.
- [x] 6.5 Apply tenant configuration capture/redaction policy to telemetry, audit, evaluation, feedback, fleet summaries, and exports without exposing tenant content to platform roles.
- [x] 6.6 Add worker configuration-snapshot policy, current-lifecycle validation, stale-job outcomes, missing-context quarantine, and reactivation semantics.

## 7. Web Administration Experience

- [x] 7.1 Extend the typed web API client and central application state with tenant discovery, one selected-tenant header boundary, role metadata, and tenant-scoped cache clearing.
- [x] 7.2 Add a tenant switcher and clear active, provisioning, suspended, archived, unauthorized, and no-membership states without mixing data between selections.
- [x] 7.3 Add the platform tenant console for tenant search, creation, metadata, lifecycle transitions, reasons, revisions, and fleet-safe usage summaries.
- [x] 7.4 Add tenant administration views for members, roles, groups, group assignments, invitations, last-administrator protection, and recovery feedback.
- [x] 7.5 Add tenant settings views for typed editing, validation errors, version history, rollback, effective policy, quotas, usage, and warnings without rendering secrets.
- [x] 7.6 Add role-aware navigation, disabled/hidden action states, accessible forms, confirmation flows, responsive layouts, and frontend interaction tests.

## 8. Bootstrap, Validation, and Release Evidence

- [x] 8.1 Update development fixtures and the Keycloak acceptance realm to seed a platform administrator, multiple isolated tenants, users, groups, roles, and active configuration versions.
- [x] 8.2 Add an operator bootstrap command and runbook for first platform administrator, first tenant administrator, backfill verification, readiness, recovery, and staged enforcement.
- [x] 8.3 Add unit tests for lifecycle transitions, configuration validation, effective roles, last-administrator protection, invitation expiry, quota reservations, and cache-key isolation.
- [x] 8.4 Add API tests for platform/tenant role separation, tenant switching, lifecycle failures, membership administration, configuration concurrency, quota responses, and secret redaction.
- [x] 8.5 Add PostgreSQL integration tests proving RLS, transaction-local context reset, non-enumerating identifier access, and cross-tenant denial for API and worker sessions.
- [x] 8.6 Add provider-contract and end-to-end tests proving object, queue, cache, Qdrant, OpenSearch, telemetry, evaluation, export, suspension, and revocation isolation across at least two tenants.
- [x] 8.7 Run migration upgrade, backfill, rollback-compatible upgrade, schema-drift, Ruff, mypy, complete backend, frontend check/build, OpenSpec strict validation, and production-like Docker acceptance suites.
- [x] 8.8 Extend the production smoke flow and release evidence with tenant creation, membership assignment, tenant switching, cross-tenant denial, configuration activation, quota rejection, suspension, worker rejection, reactivation, and citation isolation.
