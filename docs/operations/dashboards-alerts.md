# Dashboards and alerts

Initial dashboards use Prometheus metrics plus structural telemetry grouped by tenant-safe
dimensions. Panels cover API/worker readiness, request and terminal outcomes, refusal and
degradation rate, p50/p95/p99 stage latency, retrieval counts, queue backlog, ingestion failure
and retry, provider circuit health, tokens and attributable cost, revocation backlog, ACK/NACK
settlement, lease recovery, OCR outcomes, index generation publication, and reconciliation by
index and status.

The primary metric names are `enterprise_rag_outbox_events_total`,
`enterprise_rag_queue_deliveries_total`, `enterprise_rag_worker_lease_events_total`,
`enterprise_rag_stream_first_delta_seconds`, `enterprise_rag_stream_duration_seconds`,
`enterprise_rag_ocr_pages_total`, `enterprise_rag_index_generations_total`, and
`enterprise_rag_reconciliation_results_total`. Keep tenant identifiers out of metric labels;
tenant-safe details remain in sampled, redacted telemetry keyed by correlation and job IDs.

Page on readiness below 99.9% for five minutes, queue age above 10 minutes, deletion/revocation
backlog above five minutes, cross-tenant/access-control evaluation below 1.0, query failure
above 5%, ingestion failure above 10%, or p95 latency above the documented SLO for 15 minutes.
Warn on cost deviation above 30%, unavailable provider cost, telemetry export failure, increasing
NACK/poison rates, lease expiry without recovery, OCR required-mode failures, and a sustained
reconciliation stale result. A failed RLS preflight is a release-blocking alert, not a degraded
traffic condition.
