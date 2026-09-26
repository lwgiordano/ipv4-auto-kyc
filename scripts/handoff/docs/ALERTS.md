# Alerts

Scrape `GET /v1/metrics.prom` (signed read access — same auth as `/v1/metrics`; see the scrape-identity
note below). Latency series aggregate the declared 24h window.

| Alert | Expression | For | Why |
|---|---|---|---|
| KycDeadJobs | `kyc_jobs{status="dead"} > 0` | 5m | A dead-lettered job means a failed run that a human must requeue. See RUNBOOK and `/v1/ops/requeue/job/{id}`. |
| KycFailedRuns | `kyc_runs{state="FAILED"} > 0` | 5m | This zero-safe series identifies failed runs that need the same requeue attention. |
| KycDeadOutbox | `kyc_outbox{status="dead"} > 0` | 5m | An undeliverable callback or email usually indicates a platform endpoint or HMAC secret problem. Use `/v1/ops/requeue/outbox/{id}` after fixing the cause. |
| KycOutboxBacklogLevel | `kyc_outbox{status="pending"} > 100` | 15m | Level threshold, not a growth rule. The endpoint exposes gauges, not counters; no counter series is available for a `rate()` rule. |
| KycDecisionLatency | `kyc_event_to_decision_seconds_p95 > 120` | 15m | Enforces the DEPLOYMENT §5 budget for full runs. The exporter provides one latency aggregate. Separate alerting for the light-run budget below 10 seconds requires persisted run classes and separate exported series. |
| KycAdapterErrors | `kyc_adapter_error_rate > 0.2` | 15m | Sustained upstream error rate after in-adapter transient retry |
| KycReadyzDown | `probe_success{job="kyc-readyz"} == 0` | 5m | `/readyz` is the DB/migration/storage gate (blackbox-probe it) |

## Scrape identity (production)

Production requires a current path-bound signed request. A stock Prometheus cannot produce one, and
scraping with the legacy v1 scheme is prohibited because every accepted v1 request records v1 traffic in
the durable witness and would stall the v1 sunset forever. Until the dedicated least-privilege scrape
identity is available, scoped as a read-only bearer for `/v1/metrics.prom` only, run the scrape
through a sidecar that v2-signs each request. Staging hosts set up per `docs/DEPLOYMENT.md`
§2 require signed reads too, so scrape them the same way. Do NOT wire a v1 signer into a scraper.
