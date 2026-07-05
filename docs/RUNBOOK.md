# KYC Tool Runbook

## Processes

| Process | Command | Notes |
|---|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` | stateless; scale horizontally |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | N processes; per-case FIFO is queue-enforced |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` | delivers decision callbacks + POC emails |
| Retention | `python -m kyc_tool.workers.retention` | cron (daily); prunes per KYC_RETENTION_DAYS |
| Migrations | `alembic upgrade head` | before rollout; all revisions downgrade cleanly |

## Ops console (`/ui`)

The built-in console covers most of this runbook visually: Overview (health
tiles, dead-letter tables with one-click requeue), Cases (score meter, gates,
supersession chains, per-run state machine, audit trail), Integrations
(stub/live/needs-config per adapter, env presence, reachability probes),
Field Map (live Salesforce projection per case), Policy, and a Composer that
sends signed events server-side. **Security**: same trust domain as the read
API, and the composer/requeue endpoints mutate — set `KYC_UI_ENABLED=false`
in production, or restrict the port to the ops network. Local dev:
`bash scripts/dev.sh` boots the whole stack and prints the console URL.

## Health & dashboards

- `GET /healthz` — liveness + the policy bundle hash. **A hash change without a
  deploy is an incident** (policy files are immutable per release).
- `GET /v1/metrics` — watch: `jobs_by_status.dead` (alert > 0),
  `outbox_by_status.dead` (alert > 0), `runs_by_state.FAILED`,
  `review_tasks_open_by_type` growth, `event_to_decision_seconds.p95`
  (budget: < 10s adapter-light, < 120s full runs, human waits excluded),
  `adapter_latency[].error_rate` per upstream.

## Failure playbooks

### Dead-lettered job (`jobs.status = 'dead'`)
The run is FAILED with the error recorded; the case is untouched (no partial
writes — transitions are transactional). After fixing the cause:
```sql
UPDATE jobs SET status='queued', attempts=0, run_after=now(), last_error=NULL WHERE id = :id;
UPDATE runs SET state=(SELECT state FROM runs WHERE id=:run_id), error=NULL WHERE id = :run_id;
```
or simply have the platform POST `recalculate.requested` — a fresh run from
current live checks is always safe.

### Dead outbox row (callback undeliverable)
The run sits in PUBLISH_DECISION (visible, correct). Confirm the platform
endpoint + HMAC secret, then:
```sql
UPDATE outbox SET status='pending', attempts=0, next_attempt_at=now() WHERE id = :id;
```
Redelivery is safe — the platform dedupes on (case_id, run_id).

### RIR / registry outage
Runs complete as `partial` (upstream_error recorded, prior checks stay live,
no failing check is invented — G12 semantics). No action needed; when the
upstream recovers, re-drive affected cases with `recalculate.requested` or
the original evidence event (idempotency keys must be fresh).

### Review queue growing
`GET /v1/review-tasks?status=open`. Website tasks award +10 on pass;
`poc_email_unavailable` needs an alternate-proof decision by compliance
(v1: resolve on the platform; the tool records outcomes via events).

### Latency
Adapter p95 in `/v1/metrics`; per-upstream rate caps via
`KYC_ADAPTER_RATE_LIMITS` (requests/sec, process-local — divide by worker
count). Queue depth is `jobs_by_status.queued`; scale pipeline workers
horizontally (SKIP LOCKED makes them safe; per-case ordering is preserved).

## Retention & compliance

`workers.retention` prunes audit rows, delivered outbox rows, and expired
unverified POC tokens past `KYC_RETENTION_DAYS` (default 7y). Checks,
decisions, and raw evidence are NOT auto-pruned — deleting the decision
record requires compliance sign-off; do it as a supervised one-off.

## Policy changes

Rubric/decision/broker JSON changes ship as a deploy: bump the file's
`version`, update `tests/policy_driven/policy_baseline.json` in the same
commit (the drift-guard test enforces this), redeploy. Every run/decision
records the sha of the policy that produced it.

## Secrets

All via environment (see `.env.example`): platform HMAC, CH/ARIN keys,
email provider, S3. Nothing is ever logged; POC token emails log only a
recipient hash.
