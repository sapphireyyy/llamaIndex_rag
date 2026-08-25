# Threat model

## Assets and boundaries

Tenant documents, ACLs, prompts, answers, provider credentials, audit events, evaluation
exports, and source URLs are protected assets. Trust boundaries exist at OIDC, upload,
connector, retrieved content, model provider, browser, telemetry export, and operator access.

## Required controls

- Cross-tenant identifiers return not-found and every database query includes tenant scope.
- Retrieval requires a freshly compiled tenant, space, principal-token, document, and ACL
  epoch scope. Missing or stale scope fails closed before either search adapter executes.
- Uploads enforce allow-list, size, integrity, malware screening, safe object keys, and parser
  isolation. Production parsers run with CPU, memory, time, and network limits.
- Retrieved text is untrusted data, never instruction. Input, retrieved-content, and output
  guardrails block instruction override, system-prompt extraction, credentials, private keys,
  and configured restricted identifiers.
- Secret APIs expose binding status only. Logs, telemetry, errors, and exports redact secret
  keys, bearer tokens, private keys, and restricted identifiers; content capture defaults off.
- Citation preview/download reauthorizes and rejects superseded, deleted, or revoked sources.
- Audit hashes make mutation and override evidence tamper-evident.

Residual risk includes model-provider retention, novel parser exploits, semantic retrieval
misses, and malicious content not matched by deterministic guardrails. Use isolated parsing,
provider zero-retention contracts, continuous red-team corpora, and human review for high-risk
workflows.
