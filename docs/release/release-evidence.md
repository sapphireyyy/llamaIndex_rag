# Release evidence package

## Inventory and versions

Python and JavaScript dependency inventories are locked by `uv.lock` and
`web/package-lock.json`. Assistant, policy, provider, document, dataset, evaluator, and run
versions are stored as immutable identifiers and hashes. The provider baseline decision is in
architecture record 0001.

## Required evidence commands

```text
uv sync --frozen --all-groups --all-extras
uv run ruff check backend tests alembic scripts
uv run mypy backend
uv run pytest --cov=enterprise_rag --cov-report=term-missing
uv run alembic upgrade head
uv run alembic check
npm.cmd --prefix web ci
npm.cmd --prefix web run check
npm.cmd --prefix web run build
python scripts/production_smoke.py
openspec validate build-general-enterprise-rag-assistant --type change --strict
```

Migration rollback evidence is a downgrade/upgrade cycle for the latest revision. Benchmark
evidence is recorded in `docs/quality/benchmark-baseline.md`. Security evidence consists of
the threat model, automated authorization, malicious content, guardrail, telemetry redaction,
audit integrity, and failure-injection tests. Operational evidence consists of readiness,
dashboard/alert definitions, backup verification, incident playbooks, and recovery objectives.

## Known limitations before production promotion

The default development profile intentionally uses in-memory search, an in-process queue,
local object storage, deterministic embeddings, and an extractive model so it runs without
secrets. The production adapters are executable and have been locally verified, but the
acceptance stack uses single-node services, development credentials, deterministic embeddings,
and the extractive model. Target-managed secrets, the selected production model and embedding
providers, load, multi-zone failover, backup restore, malware engine, and provider cost must be
revalidated before production promotion. No release gate override changes this requirement.

## Local acceptance result (2026-08-13)

- Backend: 44 tests passed; measured statement coverage was 78% before the final
  provider-backed corpus retrieval gate was added.
- Ruff and strict mypy: passed across backend, tests, migrations, and scripts.
- Frontend TypeScript/ESLint and Vite production build: passed; main JavaScript bundle is
  213.12 kB before gzip and 66.71 kB after gzip.
- Alembic: latest migration downgrade/upgrade cycle passed and autogeneration drift is empty.
- Docker Compose configuration and OpenSpec strict validation: passed.
- General three-domain deterministic security gate: access control 1.0 and refusal
  correctness 1.0, with no cited source outside expected authorized scope.

## Production-like local acceptance result (2026-08-14)

- Infrastructure: PostgreSQL 17, MinIO, RabbitMQ 4, Redis 8, Qdrant 1.15,
  OpenSearch 3, and Keycloak 26.3 were all live through Docker Compose. Readiness reported
  configuration, database, object storage, queue, cache, vector search, and lexical search as
  healthy.
- Database: a real PostgreSQL upgrade, drift check, downgrade-to-base, and re-upgrade cycle
  passed. The final main-database drift check reported no new upgrade operations.
- Backend: the complete production-like suite passed with 49 tests and one non-blocking
  Starlette/httpx deprecation warning. It covered API contracts, provider adapters, OIDC and
  authorization, ingestion atomicity, query and citation behavior, evaluation, telemetry,
  failure injection, and the three-domain security gate.
- Provider contracts: Keycloak-issued tenant and role claims, a tenant-scoped MinIO object
  round trip, RabbitMQ durable priority delivery, equivalent Qdrant/OpenSearch ACL and version
  behavior, and full production-container readiness all passed.
- Live HTTP path: the repeatable production smoke flow obtained a Keycloak token, proved
  anonymous access was denied, created a knowledge space and assistant, uploaded and indexed a
  document, activated the assistant, received a successful SSE answer, and verified its source
  citation against the ingested document.
- Frontend and static checks: TypeScript/ESLint checks, the Vite production build, Ruff, and
  strict mypy passed. The production JavaScript bundle is 213.12 kB before gzip and 66.70 kB
  after gzip.
- Deployment and specification: Docker Compose configuration and OpenSpec strict validation
  passed. The local acceptance services remain running for continued development.
