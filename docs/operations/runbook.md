# Operator runbook

## Startup and readiness

Run migrations, verify secret bindings, start workers, then API and web. `/health/live` proves
the process is alive; `/health/ready` must show configuration, database, object store, and
queue ready before traffic. `/metrics` exposes service counters and latency histograms.

## Local persistent vector index

Development uses Milvus for the dense index so API and worker processes share the same
vectors. Start Milvus before the API:

```powershell
docker compose up -d milvus-etcd milvus-minio milvus
uv sync --extra providers
uv run --extra providers python scripts/reindex_milvus.py
```

Milvus data is persisted in the `milvus-data`, `milvus-etcd-data`, and `milvus-minio-data`
Compose volumes. Re-run the reindex
command after migrating an existing database from the in-memory backend.

## Web Keycloak rollout

The browser uses a public Keycloak client with Authorization Code Flow and PKCE S256. Deploy in
this order so the first browser callback is already valid:

1. Update the Keycloak client first: keep Standard Flow enabled, disable Direct Access Grants,
   require PKCE S256, retain the `enterprise-rag` audience mapper, and register exact redirect,
   silent-check, post-logout, and Web Origin values.
2. Configure only public `WEB_*` values on the Web container. Never inject a client secret,
   password, access token, or refresh token.
3. Rebuild and start API, worker, and Web, then wait for delayed health checks:

   ```powershell
   docker compose up -d --force-recreate keycloak
   docker compose up -d --build api worker web
   docker compose ps
   curl.exe -f http://localhost:8101/health/ready
   curl.exe -f http://localhost:3000/health/ready
   ```

4. Open <http://localhost:3000>, complete an interactive Keycloak login, and verify tenant
   discovery, one ordinary API request, streaming chat, protected preview/download, refresh,
   logout, and a second subject. Do not collect browser traces, screenshots, or request headers
   containing credentials during acceptance.

`/runtime-config.js` must return `Cache-Control: no-store`. The file is rendered into `/tmp` by
the non-root Web container, so the same image can switch public OIDC addresses without a rebuild.
Vite development uses port 5173 and must explicitly select development auth or provide its own
complete `VITE_OIDC_*` configuration. Staging/production fail closed when OIDC configuration is
missing or invalid.

API issuer must match the browser-visible Keycloak issuer. API JWKS may use the internal container
URL. A mismatch typically produces a valid browser login followed by API 401 responses.

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
- **Repeated 401:** verify the browser completed login, runtime config matches Keycloak, and API
  issuer/audience/JWKS are coherent. The Web retries one forced refresh only; never enable
  development auth in staging to suppress the error.
- **403:** keep the login session and inspect persisted tenant state, membership, role, and ACL
  epoch. Do not treat authorization failure as token expiry.
- **Callback error:** compare the exact current Origin, root redirect, silent-check redirect, and
  post-logout URI with the Keycloak client. Avoid broad wildcards.
- **Keycloak outage or third-party Cookie restriction:** the page must remain outside protected
  content and offer retry/login. Silent SSO may fall back to a top-level check; tokens must not be
  persisted as a workaround.

## Rollback and recovery

Assistant rollback revalidates the historical version. Application rollback is allowed only
while its database compatibility range includes the current migration. Database rollback is
tested one revision at a time. Index rollback changes the document active-version pointer;
destructive cleanup remains delayed until the rollback window expires.

For a Web authentication rollback, restore the previous Web image and its public runtime config.
The additional exact Keycloak callbacks may remain temporarily; remove them only after confirming
no deployed client uses them. No database rollback is required for this frontend change. Verify
that old containers are gone and that no logs or acceptance artifacts contain credentials.

Restore database and object storage to the same recovery point, rebuild indexes from active
versions, replay durable events, validate tenant counts and audit hashes, then reopen traffic.
