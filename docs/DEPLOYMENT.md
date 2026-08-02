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
   `.env.example`). **Not every setting is hot-swappable by a rolling restart:**
   `KYC_OUTBOX_MAX_ATTEMPTS` is a both-direction DRAINED cutover (§8) — for those,
   follow the release/config-specific non-hot procedure, not the default rolling
   deploy. Minimum: `KYC_DATABASE_URL`, `KYC_PLATFORM_CALLBACK_URL`,
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
  **Exception — migrations 013-023 (PR 7b-core) are forward-only after any wire
  witness exists, positive OR negative** (an `attempt_v1` decision callback with no attempt
  is durable proof nothing was staged, and counts). Their downgrades refuse — with stable
  sentinels, in execution order — and `018` through `022` refuse UNCONDITIONALLY
  (`MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY`,
  `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` — spelled out because a refused
  command is grepped, not read), because walking below them
  would restore search-path-vulnerable or under-validated authority functions
  (`MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
  `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`) — when an
  `outbox_delivery_attempts` row, a terminal `callback_wire_sha256`, or a
  `superseded` outbox row exists: those are immutable delivery evidence (for a
  pending/dead callback, the attempt row is the ONLY record that bytes were
  staged), and a local terminal status is never a reason to destroy the record
  of what the platform accepted. On refusal, KEEP or redeploy the reviewed
  **023-compatible** image — an older publisher lacks the receipt/terminal
  contract and must not run against preserved evidence. Rollback after first
  witness use is a flag/image rollback on that compatible schema, never a
  schema downgrade; a pre-7b image is permitted only after the entire walk
  reaches 012 — which is only possible on a schema that never reached `018`. Once
  `018` through `022` ARE installed, the supported rollback is redeploying the prior
  reviewed `023`-compatible image against the schema it is already on; the schema
  does not move. Do not apply `018` or anything above it in production until that
  bridge image has been reviewed and
  staged; on this preproduction branch, the safe recovery path is roll-forward.
  The UPGRADE side is gated too: `017` and `018` both refuse with
  `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS` / `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS`
  while any live (unexpired) outbox claim
  exists — stop the publishers AND retention, attest zero old processes at the
  orchestrator, let leases expire or run `reset_interrupted_outbox_claims`, then retry
  — and `018`/`019` additionally refuse with
  `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` / `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH`
  when the observable authority surface
  is not the one the prior revision installed. Every other deliberate refusal is
  indexed in `docs/RUNBOOK.md` § "Migration refusal sentinels". Witness writers (the publisher and
  retention) share one advisory fence with the migrations — writers shared, maintenance
  exclusive — so maintenance queues behind those writers instead of deadlocking with
  them. **The fence does not cover the pipeline's decide transaction**, which locks the
  case row before inserting the outbox row and takes no fence; a migration run against a
  live pipeline can still deadlock, and the live-claim preflight cannot see a run
  mid-decide because it holds no outbox claim. `022` and `023` sharpen that from "can" to
  "does": they are the first revisions to take `ACCESS EXCLUSIVE` on `decisions` and `cases`,
  in the opposite order from BOTH decision writers — the pipeline's decide transaction and
  the API's inline `reviewer.manual_approve` (jobless: no run, no claim, invisible to any
  job/run drain check) — so a concurrent decide OR inline approval deadlocks them (`40P01`)
  — see `docs/RUNBOOK.md`. That is what the DRAINED cutover is for —
  stop the pipeline workers too, not just the publishers.

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
- **ANY change to `KYC_OUTBOX_MAX_ATTEMPTS` — raising OR lowering — is a DRAINED
  publisher cutover, not a rolling restart.** Each publisher enforces the ceiling
  it was started with, so during a rolling restart an OLD and a NEW publisher run
  different ceilings against the same rows:
  - Lowering: the OLD (higher) publisher can make one more send past the new value.
  - Raising: the OLD (lower) publisher can dead-letter a row at its lower ceiling
    before the NEW (higher) publisher ever supplies the extra attempts — and for a
    POC email the terminal transition redacts the token, so those lost retries are
    irreversible.

  So for EITHER direction, in this order: (1) disable autoscaling and rolling
  restart; (2) stop ALL outbox publishers of EVERY role — both the standalone
  `outbox_worker` and the embedded `dev_worker`; (3) attest zero publishers are
  running (the same attested-stop the reset CLI requires); (4) attest every new
  task definition carries the exact new value; (5) start. This procedure is the
  canonical record `kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER`, rendered
  below and validated by `tests/unit/test_outbox_ceiling_contract.py`; RUNBOOK and
  `.env.example` embed the SAME rendered block. (A fleet-wide DB-persisted ceiling
  epoch enforced before claim is the fail-closed alternative if runtime config
  drift must be impossible — deferred; the drained cutover is the contract today.)

<!-- cutover:KYC_OUTBOX_MAX_ATTEMPTS:start -->
KYC_OUTBOX_MAX_ATTEMPTS: both-direction DRAINED publisher cutover (NOT a rolling restart)
1. disable autoscaling and rolling restart
2. stop ALL publishers of roles: outbox_worker, dev_worker
3. attest zero publishers running of roles: outbox_worker, dev_worker
4. attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS
5. start publishers of roles: outbox_worker, dev_worker
<!-- cutover:KYC_OUTBOX_MAX_ATTEMPTS:end -->

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

## 11. PR 7b-core cutover — drained maintenance window (migration 013)

**Step 0 — pre-window diagnostic (BEFORE any outage):**
0.1 Suspend the retention schedule.
0.2 Terminate and wait for every active retention task.
0.3 Capture target-orchestrator zero-running evidence. `TODO(integration)`: the exact ECS/Fargate
    `aws ecs list-tasks --cluster <c> --family retention` (or EC2 equivalent) command + its expected
    zero-task output MUST be recorded here once the production substrate is chosen. A pytest does NOT
    prove this — it is a deployment acceptance. Do not invent a substrate.
0.4 With the schedule still suspended, run the digest-pinned
    `python -m kyc_tool.ops.verify_pr7b_core_backfill`. The result is valid ONLY while retention stays
    suspended AND the 0.3 attestation holds.
0.5 On failure, ABORT here — before stopping service (no outage begun). Recovery is restore-or-block:
    restore from authoritative backup the EXACT callback row, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. Activation (`024`) is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.
    THE RESTORE PATH IS A SHIPPED CLI, reachable from HERE — a pre-window maintenance stop, not the
    cutover (which 0.4 still gates): FIRST run `python -m kyc_tool.ops.verify_pr7b_ops_prerequisites
    --expect-revision 012` (read-only, takes NO lock) and confirm it is GREEN — exact schema phase,
    correct role, `outbox_id_seq` ownership, and timeout budgets — so a wrong maintenance credential
    OR wrong phase is caught HERE, not at `ALTER SEQUENCE` inside the stop; then pause submissions,
    hard-stop and attest EVERY writer (API,
    pipeline, outbox, `dev_worker`, retention), then run
    `python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file.json>
    --expect-original-id <id> --expect-manifest-digest <sha256>` (dry-run first; add `--apply` to
    perform). `--expect-manifest-digest` is MANDATORY and is an INTEGRITY check: the tool recomputes
    the sha256 of the evidence file (`sha256sum <file.json>`) and refuses unless it matches, so a
    tampered or wrong file is rejected before any DB work — the file cannot self-certify by carrying
    its own digest. The tool does NOT verify a cryptographic signature; the digest's authenticity is
    yours to establish out of band, from a trusted/signed backup manifest (machine-verified signing
    is a future option).
    It validates the whole
    contract below, inserts the exact original row, floors the sequence past the restored id
    (`GREATEST(max(id), original_id) + 1`) in the SAME transaction, and fail-closed read-backs both
    the acceptance predicate and the sequence before committing — any mismatch rolls back row and
    sequence together. Then rerun 0.4 (the gate that reopens cutover) and either RESUME service or
    proceed to the window. Pasting the SQL below by hand is NOT a sanctioned path — the earlier
    revision of this section prescribed exactly that and was circular: the diagnostic stayed red
    until the restore, while the sequence repair was documented as reachable only after cutover
    step 2 and knew nothing of the id being restored (re-audit `f495de8` F1).
0.6 RESTORE ACCEPTANCE CONTRACT (the restore in 0.5 is an executable identity requirement, not
    advice — the backfill ranks by `outbox.id`, so a wrong id silently reverses the legacy order):
    (a) BEFORE restoring, record from the backup the authoritative evidence tuple per missing
        callback: `decision_id` plus **every schema-012 `outbox` column** —
        `(id, kind, case_id, run_id, payload_json, status, attempts, next_attempt_at,
        delivered_at, last_error, created_at)` — with `body_digest` computed ON THE BACKUP ROW as
        `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`. `md5(...)` is prohibited.
        This procedure runs BEFORE 013, so it must name NO 013-only column: `resolved_at`,
        `ordering_stream`, `decision_sequence` and the claim tuple do not exist yet. Omitting the
        retry/audit columns is what makes "exact" false — a previously retried callback restored
        with a reset `attempts`/`next_attempt_at`/`last_error` is NOT the row that was pruned.
    (b) The restore MUST re-insert the ORIGINAL primary key AND every other recorded column:
        `INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, attempts,
         next_attempt_at, delivered_at, last_error, created_at) VALUES (<original_outbox_id>, ...)`
         — every value from the evidence tuple, none defaulted. A
        default-id INSERT is prohibited (it allocates a fresh id and re-ranks the restored older
        callback as newer), and substituting `now()` for `delivered_at` is prohibited (it falsifies
        the audit record). If the original id is unavailable, do NOT restore: remain
        `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012. The evidence tuple is captured into the JSON
        file `restore_pr7b_core_callback --evidence` consumes; `--expect-original-id` must repeat
        the id (double entry). The id, body digest and decision linkage are machine-refused on
        mismatch; the lifecycle fields are ATTESTED inputs from the backup — but the MANDATORY
        `--expect-manifest-digest` (sha256 of the whole evidence file, from the signed manifest)
        binds every one of them, so a falsified backup value cannot pass without also breaking the
        signed digest. The signed manifest and the documented capture query are the sanctioned source.
    (c) ACCEPTANCE PREDICATE — POSITIVE and fail-closed. Run per restored callback; it MUST return
        EXACTLY ONE row before proceeding. ZERO rows = still blocked. Do NOT invert it into a
        "select the mismatches, expect zero rows" form: an absent row (or one restored under the
        wrong `run_id`) matches nothing and would read as accepted.
        `SELECT 1 AS accepted FROM outbox o JOIN decisions d ON d.id = :decision_id
         WHERE o.id = :original_outbox_id AND o.kind = :original_kind
           AND o.case_id = :case_id AND o.run_id = :run_id
           AND d.case_id = o.case_id AND d.run_id = o.run_id
           AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest
           AND o.status = :original_status
           AND o.delivered_at IS NOT DISTINCT FROM :original_delivered_at
           AND o.attempts = :original_attempts
           AND o.next_attempt_at IS NOT DISTINCT FROM :original_next_attempt_at
           AND o.last_error IS NOT DISTINCT FROM :original_last_error
           AND o.created_at IS NOT DISTINCT FROM :original_created_at;`
        Every schema-012 column is compared, so dropping any one of them from the restore fails
        the predicate. `IS NOT DISTINCT FROM` is used for nullables so NULL matches NULL.
    (d) THE SEQUENCE IS THE RESTORE CLI'S JOB — there is NO separate precondition to satisfy
        first (re-audit `8377440` F3: the old text made the restore reachable only after a
        `next_id > original_outbox_id` check that the documented max=5/missing-id=100 case fails,
        which is exactly the case the restore exists for). `restore_pr7b_core_callback` floors the
        sequence to `GREATEST(max(id), original_id) + 1` in the SAME transaction as the row
        insert, under `ACCESS EXCLUSIVE`, with a fail-closed read-back — whether the missing id is
        below OR above the current high-water. It never `setval`s (a read-modify-write on a
        non-transactional object that can rewind under concurrent `nextval`); `ALTER SEQUENCE …
        RESTART WITH` takes a literal and excludes `nextval` for the transaction. Run it (dry-run,
        then `--apply`) as step 0.5 above — the restore and the sequence floor are ONE action, not
        a check-then-repair sequence.
    (e) `python -m kyc_tool.ops.repair_outbox_sequence` is the SEPARATE DRAINED action for the
        ONLY case the restore does not cover: a divergent sequence high-water with NO row to
        restore (nothing missing, the counter itself is wrong). Same maintenance-stop
        preconditions and owner privilege; `--floor <id>` when an id above max must stay cleared.
        It is never a prerequisite the restore waits on.
    (f) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer;
   attest zero at the orchestrator.
3. Run the shipped `python -m kyc_tool.ops.requeue_interrupted_jobs`. NO outbox reset here — the
   pre-013 schema has no claim columns; an interrupted old claim simply waits until its already-
   recorded `next_attempt_at`. Preserve every pending row's `next_attempt_at`.
4. Run `python -m alembic -c alembic.ini upgrade head` (the chain `013`→`014`→…→`022`→`023`) — the deployment image runs its exact
   equivalent. This repeats the §0 parity preflights under the zero-writer boundary and is the
   authoritative fail-closed check (the pre-window diagnostic is an early detector, not a substitute).
   `017` additionally machine-checks the drain: it refuses with `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS`
   while any live (unexpired) outbox claim exists — leases must expire or be reset first.
5. Start API only, probe `/readyz`, then start + attest the fenced workers. No mutating prod smoke.
6. RESUME (forward completion): re-enable retention, autoscaling/restarts, and submissions, and
   remove the composer edge block. The window is NOT closed until all five paused controls
   (retention, autoscaling, restarts, submissions, composer edge block) are restored or removed.

**Rollback — a two-branch maintenance state machine (as drained as the forward cutover). BOTH branches
end in a full resume — never leave the system stopped or retention frozen:**
R1. Pause submissions, edge-block the composer, disable autoscaling/restarts.
R2. Hard-stop and orchestrator-attest zero API, pipeline, outbox, `dev_worker`, retention, every writer.
R3. While 013 still exists, run `python -m kyc_tool.ops.reset_interrupted_outbox_claims` (post-013-only;
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. **With `018` or anything above it installed there is no schema-downgrade path**: `018` through
    `022` refuse unconditionally — a walk from the head prints
    `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` (`023`'s downgrade is a validation-only
    no-op the walk passes through first; the whole command is ONE transaction, so on refusal
    even that step rolls back and the schema does not move) — because walking below them would restore
    search-path-vulnerable authority functions, so rollback goes straight to R5 (image-only on
    the schema already installed). The walk below is the HISTORICAL path, reachable only on a
    schema that never reached `018`: run `python -m alembic -c alembic.ini downgrade 012` (the revision is a
    REQUIRED positional argument — a bare `alembic downgrade` exits with a usage error
    mid-outage). That walk is `017 → 016 → 015 → 014 → 013 → 012`, and EACH revision preflights
    under
    `LOCK TABLE ... ACCESS EXCLUSIVE` (child-first from `015` on; `017` first takes the shared
    maintenance/writer advisory fence EXCLUSIVE, so it queues behind live witness writers instead
    of reasoning about their lock order). Sentinels in execution order:
    - `017` refuses — `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` — when ANY attempt row,
      terminal wire digest, or `attempt_v1` decision callback exists. NEGATIVE evidence counts:
      an attempt-regime row with no attempt is the durable proof nothing was staged.
    - `016` refuses — `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same rule one revision
      down (defense in depth below `017`), including the `attempt_v1` negative-evidence case.
    - `015` refuses — `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` — on any attempt row or
      terminal digest (child-first lock order; cannot deadlock a live writer).
    - `014` refuses — `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same witness rule.
    - `013` refuses on a `superseded` row, a surviving terminal digest
      (`MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`), or the attempt table under a bare `013`
      stamp (`MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`).
R5. ROLLBACK OUTCOME A — downgrade REFUSED (any sentinel above): the DB stays on the
    witness-authority schema, so KEEP or redeploy the reviewed **`023`-COMPATIBLE image** digest —
    an older publisher lacks the receipt/terminal contract and MUST NOT run against preserved
    evidence; PROHIBIT the pre-7b image outright. Rollback after first witness use is a
    FLAG/IMAGE rollback on the compatible schema, never a schema downgrade. A pre-7b image is
    permitted ONLY after the entire walk reaches `012` (outcome B). Verify `/readyz`, start + attest its fenced workers, then
    re-enable retention, autoscaling/restarts, and submissions and remove the composer edge block —
    OR remain in a DELIBERATELY DECLARED maintenance incident while the forward fix is applied. Do
    not end stopped.
R6. ROLLBACK OUTCOME B — downgrade SUCCEEDED: deploy the recorded prior-image digest; start API, probe
    `/readyz`, then start + attest its workers; attest image digest + running processes; then re-enable
    retention, autoscaling/restarts, and submissions and remove the composer edge block. Redeploying
    the pre-7b image BEFORE 013 is applied is also safe.
