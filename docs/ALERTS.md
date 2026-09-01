# Alerts (PR 10a, honesty pass in the f2929f8..6a4cd87 fold)

Scrape `GET /v1/metrics.prom` (signed read access — same auth as `/v1/metrics`; see the scrape-identity
note below). Latency series aggregate the declared 24h window.

| Alert | Expression | For | Why |
|---|---|---|---|
| KycDeadJobs | `kyc_jobs{status="dead"} > 0` | 5m | A dead-lettered job means a FAILED run a human must requeue (RUNBOOK; `/v1/ops/requeue/job/{id}`) |
| KycFailedRuns | `kyc_runs{state="FAILED"} > 0` | 5m | Zero-safe series: a FAILED run needs the same requeue attention |
| KycDeadOutbox | `kyc_outbox{status="dead"} > 0` | 5m | An undeliverable callback/email — platform endpoint or HMAC secret problem (`/v1/ops/requeue/outbox/{id}`) |
| KycOutboxBacklogLevel | `kyc_outbox{status="pending"} > 100` | 15m | A LEVEL threshold (not a growth/derivative rule — this endpoint exposes gauges, not counters; a rate() rule needs a counter series, which is future work) |
| KycDecisionLatency | `kyc_event_to_decision_seconds_p95 > 120` | 15m | DEPLOYMENT §5 budget for FULL runs. HONEST LIMIT: the exporter ships ONE latency aggregate; the separate <10s light-run budget cannot be alerted on until run classes are persisted and exported as separate series (scheduled with the PR 10b observation unit) |
| KycAdapterErrors | `kyc_adapter_error_rate > 0.2` | 15m | Sustained upstream error rate after in-adapter transient retry |
| KycReadyzDown | `probe_success{job="kyc-readyz"} == 0` | 5m | `/readyz` is the DB/migration/storage gate (blackbox-probe it) |

## Scrape identity (production)

Production requires a CURRENT path-bound signed request; a stock Prometheus cannot produce one, and
scraping with the LEGACY v1 scheme is PROHIBITED — every accepted v1 request records v1 traffic in
the durable witness and would stall the v1 sunset forever. Until the dedicated least-privilege scrape
identity ships (scheduled: a scoped read-only bearer for `/v1/metrics.prom` only), run the scrape
through a sidecar that v2-signs each request, or scrape from a network position where
`KYC_READ_AUTH_REQUIRED` staging rules apply. Do NOT wire a v1 signer into a scraper.
