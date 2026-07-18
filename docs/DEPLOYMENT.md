# Deployment & Releases

For the platform team operating the KYC tool in IPv4.Global's AWS account.
Ownership: IPv4.Global maintains the code and cuts releases; you pull a
release and redeploy. No code is edited on the server — if something needs
changing, it changes in the repo and ships as the next release.

## 1. One image, four processes

The repo `Dockerfile` builds a single image. Each process is the same image
with a different command:

| Process | Command | HTTP |
|---|---|---|
| API | (image default) `uvicorn kyc_tool.api.app:create_app --factory --host 0.0.0.0 --port 8000` | 8000 |
| Migrations (one-shot) | `alembic upgrade head` | — |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | — |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` | — |
| Retention (daily cron) | `python -m kyc_tool.workers.retention` | — |

All are stateless; scale the API and pipeline workers horizontally as needed.
Per-case ordering is enforced by the database, so extra workers are safe.
Disable the image's HTTP healthcheck on worker containers (they serve no HTTP).

## 2. Environments

| | Staging | Production |
|---|---|---|
| `KYC_ENVIRONMENT` | `development` (until real providers land) | `production` |
| `KYC_ENFORCE_POSITIVE_DECISIONS` | `true` — rehearse full automation | `false` at launch; flipped after staging proves out |
| Providers | built-in stand-ins (fixture registries, email file sink) | real OCR/email/registry providers, required |
| Secret | staging secret | separate production secret |

Production mode validates config at boot and refuses to start on anything
unsafe (missing secret, stub providers, non-HTTPS callback URL), listing every
violation at once. A bad deploy fails loudly instead of running quietly broken.

Staging's automation-on is safe **only** while staging is closed to untrusted
callers. PR 5a adds path-bound HMAC v2, but during the dual-accept window a
**v1-only** request is still path-unbound — a captured signed event could be
replayed to another case within the skew window. The redirect closes for v2
traffic at deploy, but for everyone only once **inbound v1 is actually disabled**
(the §6 zero-witness satisfied AND `hmac_v1_inbound_sunset_at` in effect). Keep
staging's perimeter closed until then — not merely until PR 5a is deployed.
Production automation stays off regardless until the M2 gate is met.

**PR 5a is a non-hot cutover.** Migration 010 drops the global unique that the
old image's ingest still uses, so an old replica serving after the migration
would fail event inserts. Deploy **stop → migrate → start** (not a rolling
upgrade): drain all old API replicas, run `alembic upgrade head`, start the new
replicas, readiness-verify them, then run the one-shot
`python -m kyc_tool.ops.activate_hmac_v1_observation` to start the v1
observation clock. Do NOT skip the activation step — until it runs, the sunset
zero-witness never turns green (by design), so v1 can never be sunset.

## 3. First-time setup (per environment)

1. Provision: RDS PostgreSQL 14+, an S3 bucket, an ECS/Fargate service (or
   EC2) for the processes above.
2. Generate the shared HMAC secret into AWS Secrets Manager; set the same
   value in the platform's config for that environment.
3. Set env vars (`KYC_` prefix; full table in `docs/RUNBOOK.md`; sample in
   `.env.example`). Minimum: `KYC_DATABASE_URL`, `KYC_PLATFORM_HMAC_SECRET`,
   `KYC_PLATFORM_CALLBACK_URL`, `KYC_OBJECT_STORE=s3`, `KYC_S3_BUCKET`, and
   the two per-environment values from §2.
4. Run the migration task: `alembic upgrade head`.
5. Start the processes. Wire `GET /readyz` to the load balancer — it checks
   DB connectivity, migration version, and storage access, and returns 503
   until all pass. Config safety is validated only in production mode; in
   staging's development mode `/readyz` does NOT vet the env vars, so verify
   the §3 values by hand.
6. Smoke test: send one signed `kyb.run_requested` (script in
   `docs/PLATFORM_BRIEFING.md` §7) and confirm the decision arrives at the
   callback URL.

## 4. Deploying an update

Each release from IPv4.Global is a tagged version with release notes stating
three things: does it include a **migration**, any **new env vars**, and any
**contract change** (almost always: none — the API contract is stable).

1. Pull the release tag; build the image.
2. If the notes list new env vars, set them first.
3. Run the migration task (`alembic upgrade head`). Safe to run when there is
   no migration — it does nothing. **Do not assume a migration is compatible
   with the still-running previous image**: the release notes state whether it
   is. When they don't say so (or say it isn't), use a brief cutover — stop
   the processes, migrate, start the new image. Not every migration is
   hot-compatible; 008 was not.
4. Rolling restart: API, then workers.
5. Verify (§5).

## 5. Post-deploy verification

- `GET /readyz` → 200 on every instance.
- `GET /healthz` → returns the policy bundle hash; it must match the release
  notes. A hash change **without** a deploy is an incident (policy files are
  immutable per release).
- Staging: run the smoke event end to end.
- Watch `GET /v1/metrics` for 15 minutes: `jobs_by_status.dead` and
  `outbox_by_status.dead` must stay 0; `event_to_decision_seconds.p95` budget
  is < 10 s for light runs, < 120 s for full runs.

## 6. Rollback

- Code: redeploy the previous image tag. That is the whole rollback when the
  release had no migration (most releases).
- With a migration: every revision downgrades cleanly
  (`alembic downgrade <previous revision>` — the release notes name it), but
  once real traffic has written data under the new schema, prefer rolling
  forward with a fix. Downgrade without hesitation in staging; in production,
  check with IPv4.Global first.

## 7. Monitoring and incidents

Alert on, from `GET /v1/metrics`:

- `jobs_by_status.dead` > 0 — a run gave up after retries
- `outbox_by_status.dead` > 0 — a callback or email became undeliverable
- `runs_by_state.FAILED` growth
- `adapter_latency[].error_rate` per upstream registry
- `event_to_decision_seconds.p95` over budget

`docs/RUNBOOK.md` has the failure playbooks; the ops console's requeue
buttons (`/ui/api/requeue/...`) are the preferred recovery path — they reset
both the job and its failed run. Three limits to know:

- A dead `poc_email` row cannot be requeued: its token was scrubbed when it
  died (the endpoint refuses it). Recovery is a fresh `poc.submitted`.
- `recalculate.requested` re-decides from existing evidence but does **not**
  re-run the broker screen — after a blocklist update, re-send the original
  evidence event (or `kyb.run_requested`) instead.
- Registry-outage behavior needs no action: runs complete as partial and
  nothing wrong is ever emitted.

## 8. Rules

- No code edits on the server; no schema or data edits outside the runbook
  playbooks. The audit trail assumes the repo is the truth.
- Policy files (scoring, decisions, broker list) change only via release —
  a guard test forces a version bump, and every decision records the policy
  hash that produced it.
- Secrets only via environment / Secrets Manager; nothing secret is logged.
- Never set `KYC_AUTH_DISABLED` outside local dev. Production boot refuses it.
