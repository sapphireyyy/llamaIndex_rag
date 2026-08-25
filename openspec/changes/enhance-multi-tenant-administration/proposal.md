## Why

The platform already carries a tenant identifier through persistence, authorization, retrieval, and audit paths, but it has no supported way to create, suspend, configure, or administer tenants and their tenant-level memberships. A complete multi-tenant control plane is required before the assistant can be operated safely for multiple independent organizations.

## What Changes

- Add a platform administration boundary for creating, listing, updating, suspending, reactivating, archiving, and restoring tenants without granting platform operators implicit access to tenant content.
- Add tenant-level principals, groups, memberships, role assignments, invitations, and current-tenant selection, with persisted authorization taking precedence over untrusted request input.
- Add typed, versioned tenant configuration for quotas, feature flags, locale, retention, upload, model/provider policy, and default RAG policies, including optimistic concurrency and validation.
- Enforce active-tenant and active-membership checks before every tenant data-plane operation, and propagate tenant isolation through database queries, object keys, queues, caches, search indexes, telemetry, exports, and background jobs.
- Add audited platform and tenant administration APIs plus web administration flows for tenant lifecycle, tenant switching, members, groups, roles, settings, quota usage, and access-denied states.
- Add migrations, backfill/bootstrap behavior, authorization tests, cross-tenant isolation tests, configuration tests, UI tests, and production-like acceptance coverage.
- **BREAKING**: Existing OIDC `tenant_id` identities remain supported as a default tenant hint, but authorization roles for tenant operations become validated against persisted active tenant membership rather than accepted solely from token roles or client-selected context.
- Hard tenant purge and billing integration are intentionally excluded; archival remains reversible and any later purge requires a separate retention-governed change.

## Capabilities

### New Capabilities

- `tenant-lifecycle-management`: Platform-scoped tenant provisioning, lifecycle transitions, discovery, tenant selection, and operational status behavior.
- `tenant-membership-and-authorization`: Tenant principals, groups, memberships, invitations, role assignments, platform-role separation, and fail-closed tenant authorization.
- `tenant-configuration-and-isolation`: Typed tenant settings, quotas, feature policy, configuration versioning, usage reporting, and isolation across every persistence and provider boundary.

### Modified Capabilities

None. The repository has no synchronized main specifications yet; this change introduces three additive capability contracts and builds on the existing tenant-aware implementation seams.

## Impact

- Backend domain identity and role types, authentication resolution, centralized authorization, tenant and membership application services, API routers, audit events, and dependency container startup.
- SQLAlchemy models and Alembic migrations for tenant lifecycle metadata, memberships, invitations, groups, configuration revisions, and quota/usage state.
- Tenant scoping in repositories, object storage, RabbitMQ messages, Redis keys, Qdrant/OpenSearch metadata and filters, telemetry, evaluation exports, and worker execution.
- Web navigation, API client, platform tenant console, tenant switcher, tenant member/group/role administration, settings editor, and suspended/unauthorized states.
- Existing development bootstrap, Keycloak acceptance realm, fixtures, security suites, migration checks, and production smoke workflow.
