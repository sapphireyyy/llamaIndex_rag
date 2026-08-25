# Dashboards and alerts

Initial dashboards use Prometheus metrics plus structural telemetry grouped by tenant-safe
dimensions. Panels cover API/worker readiness, request and terminal outcomes, refusal and
degradation rate, p50/p95/p99 stage latency, retrieval counts, queue backlog, ingestion failure
and retry, provider circuit health, tokens and attributable cost, and revocation backlog.

Page on readiness below 99.9% for five minutes, queue age above 10 minutes, deletion/revocation
backlog above five minutes, cross-tenant/access-control evaluation below 1.0, query failure
above 5%, ingestion failure above 10%, or p95 latency above the documented SLO for 15 minutes.
Warn on cost deviation above 30%, unavailable provider cost, and telemetry export failure.
