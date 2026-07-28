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
   `.env.example`). Minimum: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL`,
   `KYC_OBJECT_STORE=s3`, `KYC_S3_BUCKET`, the two per-environment values from
   §2, and the full **HMAC credential set** — production boot refuses without
   all of it (PR 5a):
   - v1 legacy secret: `KYC_PLATFORM_HMAC_SECRET`
   - v2 **inbound** (platform→tool): `KYC_HMAC_INBOUND_KEY_ID` +
     `KYC_HMAC_INBOUND_SECRET`
   - v2 **outbound** (tool→platform callbacks): `KYC_HMAC_OUTBOUND_KEY_ID` +
     `KYC_HMAC_OUTBOUND_SECRET`
   - both sunsets `KYC_HMAC_V1_INBOUND_SUNSET_AT` /
     `KYC_HMAC_V1_OUTBOUND_SUNSET_AT` (tz-aware ISO-8601 — a naive or malformed
     value is refused at boot, not at request time)
   - `KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS` (≥ 1)

   Set both sunset dates in the future at launch (dual-accept). The inbound one
   only *takes effect* once the zero-witness is green (activate per §2), so a
   date alone never cuts off live v1.
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
- With a migration: revisions downgrade cleanly
  (`alembic downgrade <previous revision>` — the release notes name it), but
  once real traffic has written data under the new schema, prefer rolling
  forward with a fix. Downgrade without hesitation in staging; in production,
  check with IPv4.Global first. **Exception — migration 010 (PR 5a) is
  forward-only after cross-case idempotency-key reuse:** its downgrade
  deliberately refuses (it will not delete immutable audit events to recreate
  the old global unique — see `docs/RUNBOOK.md` and ADR-003). If two cases have
  shared an idempotency key, roll forward with a fix; do not downgrade 010.
  **Exception — migrations 013-016 (PR 7b-core) are forward-only after any wire
  witness exists, positive OR negative** (an `attempt_v1` decision callback with no attempt
  is durable proof nothing was staged, and counts). Their downgrades refuse — with stable
  sentinels, in execution order
  (`MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`) — when an
  `outbox_delivery_attempts` row, a terminal `callback_wire_sha256`, or a
  `superseded` outbox row exists: those are immutable delivery evidence (for a
  pending/dead callback, the attempt row is the ONLY record that bytes were
  staged), and a local terminal status is never a reason to destroy the record
  of what the platform accepted. On refusal, KEEP or redeploy the reviewed
  **016-compatible** image — an older publisher lacks the receipt/terminal
  contract and must not run against preserved evidence. Rollback after first
  witness use is a flag/image rollback on that compatible schema, never a
  schema downgrade; a pre-7b image is permitted only after the entire walk
  reaches 012.

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

## 9. PR 5b cutover — brief full maintenance window

PR 5b adds the reviewer-actor trust floor that closes the "review completed /
approved by anyone holding the shared secret" forgery: it requires the signed
envelope's `actor` to identify the reviewer (`docs/PLATFORM_INTEGRATION.md`
§3), not just the payload. **This is not a rolling deploy.** During any
old/new overlap, an old replica still honors the exact forgery this release
closes — an old API applies `reviewer.manual_approve` inline with no actor
floor, and an old pipeline worker (which claims a job purely by kind, with no
event-type filter) can still close a queued `system`-actor website completion
under the old actorless semantics. There is no way to keep an old replica
serving *any* traffic while guaranteeing it never touches a sensitive event,
so this release ships as a **brief full maintenance window** — the same
non-hot **stop → deploy → start** pattern PR 5a used (§2), extended to workers
as well as the API, which removes old/new overlap entirely. No migration
ships with this change.

The window is a real interruption, not a seamless roll: `POST
/v1/cases/{case_id}/events` is unavailable for its duration, for every event
type, and the pipeline is stopped. The "no loss" guarantee for that
interruption is a **platform prerequisite**, not something the current API
contract provides on its own — today the contract only directs retry on a
*network failure* (`docs/PLATFORM_INTEGRATION.md` §3), but a load balancer
with every API target down instead returns 502/503/504, and a delayed retry
that reuses the original signature can blow the 300-second HMAC skew. Before
scheduling the window, confirm with the platform team that they will
pause/buffer **all** event submission for its duration and drain it
afterward — re-signing each retried body with a **fresh timestamp/signature**
against the **same idempotency key** — or, if they prefer to keep sending,
that they treat 502/503/504 the same as a transport failure: retryable with
the same body/key and a fresh signature.

Steps:

0. **Before the window:** build and publish the reviewed image; record its
   **digest**. Every step below that runs code — the recovery one-shot, the
   new API, the new workers — is pinned to that one digest. The recovery
   module (`kyc_tool.ops.requeue_interrupted_jobs`) exists only in the new
   image, so running it on the still-current old task definition fails with
   `No module named …`.
1. **Pause all platform event submission** (every event type, not only the
   two sensitive ones) and block the ops composer: the platform buffers
   outbound events, and the composer route (`POST /ui/api/send-event`) is
   edge-blocked — or old replicas are flipped to `KYC_UI_ENABLED=false` — so
   an operator on a still-live old replica can't post an inline forged
   approval. The whole window is a maintenance pause; a partial pause cannot
   guarantee no-loss.
2. **Stop all old processes together** — the API pool and the pipeline-worker
   pool, as one coordinated action, **no graceful drain**, without awaiting
   either pool before signaling the other. Confirm both pools are at **zero**
   before continuing. A sequenced stop leaves the not-yet-stopped pool live
   and able to commit a forgery in the gap; the edge block cannot revoke a
   request already inside an old API's threadpool, so old APIs must be
   *stopped*, not drained. Hard termination is safe here: every transition
   (and every ingest) commits in one transaction, so interrupted work simply
   rolls back.
3. **Recover interrupted jobs.** With both pools confirmed at zero, run
   `python -m kyc_tool.ops.requeue_interrupted_jobs` as a one-shot task
   **pinned to the §0 digest**, before any new worker starts. With every
   worker stopped and none yet restarted, every `status='running'` job row is
   by definition interrupted — regardless of lease expiry, and with no need
   for a worker-liveness registry. The command requeues that whole set
   transactionally **without** consuming the forced-stop attempt (this is an
   operator-initiated interruption, not a handler failure — consuming the
   attempt could let the passive reaper dead-letter an expired final-attempt
   job before it ever reaches the new actor guard), and it asserts zero
   `running` rows remain on completion. It must not run while any worker is
   live — confirming both pools at zero (step 2) is its precondition.
4. **Deploy the new API and worker services, both pinned to the §0 digest**
   (attest it). Bring the **API** up first and hold **workers at zero** until
   step 5 passes — workers serve no HTTP, so the API probes below cannot
   vouch for a stale or mismatched worker image, and starting workers early
   would let one honor a recovered pre-upgrade completion.
5. **Direct-probe each new API replica** on a trusted path that bypasses the
   edge rule (internal target-group address or port-forward) — probing
   through the edge would let the load balancer's own 403 falsely certify a
   broken app. Use only side-effect-free probes (each is rejected at the
   ingest floor and rolls back, writing no rows) and assert the response
   **body**, not just the status code:
   - a signed `system`-actor `website.review_completed` against a valid open
     task → the app's **422** (a 404/409 would mask a broken actor floor);
   - a mismatched-actor `reviewer.manual_approve` → the app's **422**;
   - the composer → the app's **403** for both sensitive event types.

   Do not probe the decide-txn guard's live behavior in production this way —
   a real pipeline run there writes a decision and enqueues a callback
   unconditionally, and the outbox publisher (a separate process, not stopped
   in step 2) would deliver it to the platform. That behavior is proven
   **before the window, in staging**, against the exact §0 digest; attest the
   same digest here.
6. **Start the new workers** (they now claim the recovered queue under the
   new decide-txn actor guard), then **resume**: unpause platform event
   submission and unblock the composer route.

### Rollback

Rollback mirrors the same window and pins the recovery one-shot to the **last
image that still contains it**: pause all submission, stop all new processes
together (same coordinated hard stop; same recovery command, run against a
digest that has the module), redeploy the prior image for API **and**
workers, verify, start workers, resume — accepting that the prior image
restores pre-PR-5b behavior.

**Rollback verification is non-mutating only:** `GET /readyz`, `GET
/healthz`, and prior-image digest attestation. Do **not** run the step-5
sensitive-mutation probes against the prior image — that image is the current
vulnerable code with no actor floor, so a mismatched-actor `manual_approve`
probe would actually `approve` the case inline, and a `system`-actor
completion probe would queue a run the restored old worker can honor: the
probe would *perform* the forgery it is meant to detect, not find it.
Exercise that behavior only in staging or an isolated DB. Keep all submission
and the composer blocked until the safe, non-mutating checks pass.

## 10. PR 6 cutover — bundle-pinning activation

PR 6 pins the policy bundle (and records the engine build) a run is actually
scored and decided under, instead of trusting whatever the worker process
happened to have loaded. It ships in two parts: a **rolling** part (safe to
deploy like any other release) and a **drained** part (the flag flip, not
safe to roll).

**Rolling — migration + provenance, flag stays off.** Migration 011
(`policy_bundles`, `bundle_pinning_epoch`, and the new nullable provenance
columns) is additive and hot-compatible — deploy it through the normal §4
flow. It is hot-compatible precisely because its three provenance-column
CHECKs land `NOT VALID` (a brief, metadata-only lock, no table scan) and are
validated by the follow-on migration 012 via `VALIDATE CONSTRAINT` under a
non-blocking lock, so the rolling `alembic upgrade head` does not stall
ingest/decide writers on large `checks`/`decisions` audit tables. On the PR6
image, the API and pipeline worker seed and read back the
on-disk policy bundle at startup (failing closed on a corrupt persisted row)
and every automatic/manual decision starts recording bundle **and** engine
provenance immediately — `Settings.enforce_bundle_pinning`
(`KYC_ENFORCE_BUNDLE_PINNING`) stays `false`, so scoring itself is
byte-identical to pre-PR6 (a strict no-op; see ADR-005). `GET /readyz`
unconditionally confirms the process's loaded bundle is durably resolvable
from the store — watch it like any other readiness check during this
rollout.

**Seed the bundle.** Before relying on the store for anything beyond a
process's own startup seeding (in particular, before the preflight below),
confirm the release's bundle is in `policy_bundles`:

```
python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>
```

`--expect-hash` is the hash an operator names from a reviewed source (the
release notes / deploy manifest) — the command computes the hash of the
bundle at `KYC_POLICY_DIR`, compares it to `--expect-hash`, and **only on a
match** stores it; a mismatch raises and writes nothing, so a wrong policy
directory can never land a row silently.

**Drained cutover — flip the flag.** This is not a rolling deploy: every
worker that claims a `run_transition` job while `enforce_bundle_pinning` is
inconsistent across the pool risks resolving a bundle differently from its
peers. Flip it with the pool fully drained, the same shape PR 5a/5b used:

1. **Preflight.** `python -m kyc_tool.ops.verify_pinnable_backlog` — checks
   that every queued/running/dead `run_transition` job's run has a
   creation-pin bundle that actually loads from the store. **A nonzero exit
   blocks the cutover** — seed the missing bundle(s) (§ above, or historical
   recovery via the same command with the older policy directory) and re-run
   until it exits 0.
2. **Disable autoscaling/restarts; confirm zero old workers.** At the
   orchestrator (not by row count — a job-table count doesn't prove process
   quiescence), confirm every pipeline-worker replica currently running
   predates this cutover is gone.
3. **Recover interrupted jobs.** With workers confirmed at zero, run
   `python -m kyc_tool.ops.requeue_interrupted_jobs` once — every
   `status='running'` job at this point is by definition interrupted; it
   requeues the whole set without consuming the forced-stop attempt and
   asserts zero `running` rows remain. Its precondition is "all workers
   confirmed stopped" (step 2) — do not run it while any worker is live.
4. **Start flag-on workers.** Set `KYC_ENFORCE_BUNDLE_PINNING=true` and start
   the pipeline-worker pool. Confirm the startup **attestation** log line on
   every replica — a structured `bundle_pinning_ready` event carrying
   `flag=true`, `bundle_hash`, and `engine_build_id` — emitted only after the
   worker's own seed-and-verify passes, so a replica that never logs it never
   started claiming jobs.
5. **Resume.** Unpause whatever was paused for the drain (the API itself
   never stopped — only the worker pool is drained here).

**Activate the epoch.** Once the cutover is verified stable, write the
durable activation record:

```
python -m kyc_tool.ops.activate_bundle_pinning_epoch \
    --expect-bundle-hash <sha256> --expect-engine eng-1
```

This compares the **locally loaded** policy bundle and this process's
`ENGINE_BUILD_ID` against the `--expect-*` arguments before touching the
database at all (a valid-but-wrong bundle is refused here, not merely by
store-absence later), writes the singleton `bundle_pinning_epoch` row with
database time, and read-back-fails if a concurrent activation already wrote
different values — so a skewed operator clock or a mismatched second
activation can never silently move the boundary. From `activated_at`
onward, `docs/RUNBOOK.md`'s post-epoch alert treats any check/decision
missing its provenance stamp as an anomaly, not an expected state.

**Rollback — flag-only, no data migration.** Normal rollback for PR 6 never
means resuming on a pre-PR6 image (that would silently stop writing
provenance and, after the epoch, mint permanent NULLs — a defect, not a
safe fallback). It means disabling the flag **on the PR6 image**, via the
same drained shape: stop the worker pool → confirm zero running →
`ops.requeue_interrupted_jobs` → start workers with
`KYC_ENFORCE_BUNDLE_PINNING=false` → resume. The creation pin and
provenance writing are preserved throughout; scoring simply returns to the
process-loaded bundle. Migration 011 is **retained** — never downgrade it
once any bundle row, provenance column, or the epoch row is populated (its
downgrade deliberately refuses, the same forward-only-after-use contract
migration 010 established in ADR-003).
