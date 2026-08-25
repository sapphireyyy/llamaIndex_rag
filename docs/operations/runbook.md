# Operator runbook

## Startup and readiness

Run migrations, verify secret bindings, start workers, then API and web. `/health/live` proves
the process is alive; `/health/ready` must show configuration, database, object store, and
queue ready before traffic. `/metrics` exposes service counters and latency histograms.

## Local persistent vector index

Development uses Qdrant for the dense index so API and worker processes share the same
vectors. Start Qdrant before the API:

```powershell
docker compose up -d qdrant
uv run python scripts/reindex_qdrant.py
```

The Qdrant data is persisted in the `qdrant-data` Compose volume. Re-run the reindex
command after migrating an existing database from the in-memory backend.

## Incidents

- **Provider outage:** inspect provider health, circuit state, timeout and cost metrics. Enable
  only policy-approved fallback; grounded refusal remains safer than an uncited answer.
- **Queue backlog:** pause connectors, scale workers, inspect poisoned outbox records, repair
  the dependency, then replay by correlation ID. Do not bypass idempotency.
- **Ingestion failure:** keep the prior active version, inspect sanitized stage/attempt detail,
  and retry only after classifying transient versus content failure.
- **Deletion or revocation:** prioritize the outbox event, confirm ACL epoch increased, verify
  both indexes reject the source, clear derived caches, then complete retention-aware cleanup.
- **Audit export:** use tenant-scoped API access, preserve hash order, encrypt the export, and
  record requester, purpose, and retention deadline.

## Rollback and recovery

Assistant rollback revalidates the historical version. Application rollback is allowed only
while its database compatibility range includes the current migration. Database rollback is
tested one revision at a time. Index rollback changes the document active-version pointer;
destructive cleanup remains delayed until the rollback window expires.

Restore database and object storage to the same recovery point, rebuild indexes from active
versions, replay durable events, validate tenant counts and audit hashes, then reopen traffic.
