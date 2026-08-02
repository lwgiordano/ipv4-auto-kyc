# Alerts (PR 10a)

Scrape `GET /v1/metrics.prom` (signed read access — same auth as `/v1/metrics`) and alert on the
DEPLOYMENT §5 budgets. Latency series aggregate the declared 24h window.

| Alert | Expression | For | Why |
|---|---|---|---|
| KycDeadJobs | `kyc_jobs{status="dead"} > 0` | 5m | A dead-lettered job means a FAILED run a human must requeue (RUNBOOK §dead-letter) |
| KycDeadOutbox | `kyc_outbox{status="dead"} > 0` | 5m | An undeliverable callback/email — platform endpoint or HMAC secret problem |
| KycOutboxBacklog | `kyc_outbox{status="pending"} > 100` | 15m | Publisher saturated or platform slow; check `outbox_delivery_saturated` logs |
| KycDecisionLatency | `kyc_event_to_decision_seconds_p95 > 120` | 15m | DEPLOYMENT §5 budget: p95 < 120 s for full runs |
| KycAdapterErrors | `kyc_adapter_error_rate > 0.2` | 15m | A flaky upstream; in-adapter transient retry already applied, so a sustained rate is real |
| KycReadyzDown | `probe_success{job="kyc-readyz"} == 0` | 5m | `/readyz` is the DB/migration/storage gate (blackbox-probe it) |

Runbook links: dead jobs/outbox → RUNBOOK "Dead job" / "Dead outbox row" (console requeue endpoints);
latency → RUNBOOK §capacity; adapter errors → the adapter's upstream status page.
