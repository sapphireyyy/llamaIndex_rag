# Acceptance matrix

All OpenSpec capability groups have a corresponding automated or operational check:

- Assistant configuration: configuration API tests, model-gateway unit tests, activation
  validation, version history, audit records, and UI production build.
- Document ingestion: upload/update/unchanged/delete API test, parser and index contract tests,
  staged publication state machine, migration checks, and operator incident procedure.
- Knowledge spaces: lifecycle API test, membership authorization integration test, archived
  filtering, source removal outbox, tenant scoping, and administrator UI build.
- Permission-aware RAG: identical dense/lexical ACL contract test, stale epoch rejection,
  deterministic fusion/rerank/evidence selection, grounded chat E2E, citation reauthorization,
  refusal, conflict, and guardrail paths.
- Quality and observability: telemetry redaction/content-off integration test, metrics endpoint,
  immutable evaluation tests, eight evaluators, gate failure/override audit, dashboards/alerts,
  and baseline benchmark.
- Security and audit: OIDC/secret tests, cross-tenant authorization, tamper detection, malicious
  upload rejection, prompt/output guardrails, threat model, network policy, and audit runbook.

Provider-backed, multi-zone production SLOs are an operational acceptance check because this
workspace has no production credentials or cluster. They must be attached to the release
evidence before the first production promotion.
