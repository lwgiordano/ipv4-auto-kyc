# KYC Tool Runbook

## Processes

| Process | Command | Notes |
|---|---|---|
| API | `uvicorn kyc_tool.api.app:create_app --factory` | stateless; scale horizontally |
| Pipeline worker | `python -m kyc_tool.workers.pipeline_worker` | N processes; per-case FIFO is queue-enforced |
| Outbox publisher | `python -m kyc_tool.workers.outbox_worker` | delivers decision callbacks + POC emails |
| Retention | `python -m kyc_tool.workers.retention` | cron (daily); prunes per KYC_RETENTION_DAYS |
| Migrations | `alembic upgrade head` | before rollout; downgrade clean EXCEPT migration 010 and the 013-023 witness chain (see below). **018 through 022 are forward-only: once installed there is NO supported schema downgrade** — rollback is image-only. 017 and 018 both refuse to UPGRADE while any live outbox claim exists (`MIGRATION_017_PREFLIGHT_LIVE_CLAIMS`, `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS` — publishers AND retention must be drained) |
| v1 witness activation | `python -m kyc_tool.ops.activate_hmac_v1_observation` | one-shot, POST-cutover (PR 5a §6a); idempotent |
| Bundle preflight | `python -m kyc_tool.ops.verify_pinnable_backlog` | one-shot; PRE-cutover for `enforce_bundle_pinning` (PR 6, `docs/DEPLOYMENT.md` §10) — nonzero exit + the un-pinnable run ids blocks the cutover |
| Bundle seed | `python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>` | one-shot; stores a policy bundle only if it hashes to `--expect-hash` (no write on mismatch) — also the historical-recovery path when reprocessing a run under an older bundle |
| Bundle epoch activation | `python -m kyc_tool.ops.activate_bundle_pinning_epoch --expect-bundle-hash <sha256> --expect-engine <id>` | one-shot, POST-cutover (PR 6, `docs/DEPLOYMENT.md` §10); idempotent on a matching re-run, fails on a mismatched one |
| 7b-core pre-window diagnostic | `python -m kyc_tool.ops.verify_pr7b_core_backfill` | one-shot, schema-012-compatible, SHARE-locked, read-only; PRE-window (retention suspended + attested zero) — nonzero exit + `BLOCKED_NO_AUTHORITATIVE_MAPPING` blocks the cutover (see the cutover section) |
| 7b-core ops prerequisites | `python -m kyc_tool.ops.verify_pr7b_ops_prerequisites --expect-revision 012` | one-shot, READ-ONLY, takes NO lock (no writer stop needed) — run BEFORE pausing service to confirm the maintenance credential: refuses unless the schema is exactly `--expect-revision` (the restore path is `012`), then reports current role, `outbox_id_seq` owner, whether they match, and the lock/statement budgets; nonzero unless the current role OWNS the sequence, so a wrong credential OR wrong phase is caught before the outage, not inside it |
| Outbox claim reset | `python -m kyc_tool.ops.reset_interrupted_outbox_claims` | one-shot, post-013-only; ONLY with every publisher stopped + attested — clears complete claim tuples, preserves `next_attempt_at`, atomic (refuses on any surviving tuple) |
| Outbox sequence repair | `python -m kyc_tool.ops.repair_outbox_sequence [--floor N]` | one-shot, DRAINED maintenance stop only (takes `ACCESS EXCLUSIVE` on outbox); restarts `outbox_id_seq` at `GREATEST(max(id), floor)+1` with a fail-closed read-back — exit status IS the result |
| 7b-core callback restore | `python -m kyc_tool.ops.restore_pr7b_core_callback --evidence <file> --expect-original-id <id> --expect-manifest-digest <sha256> [--apply]` | one-shot, schema-012 ONLY, pre-window maintenance stop; dry-run by default; `--expect-manifest-digest` (sha256 of the file, from the signed backup manifest) is MANDATORY — the file cannot self-certify; inserts the exact backed-up row AND floors the sequence past it in one transaction, double fail-closed read-back (see cutover step 0.5/0.6) |

> **Migration 010 (PR 5a) is a non-hot, forward-only-after-reuse cutover.** It
> drops the global unique on `events.idempotency_key`, which the *old* image's
> ingest still references — deploy **stop/migrate/start**, never rolling. Once
> the tool has admitted the same idempotency key in two different cases, 010's
> downgrade **refuses** (it will not delete immutable audit events to recreate
> the old constraint); roll forward instead. After the new replicas are up and
> readiness-verified, run the activation command above once to start the v1
> observation clock.

> **Migrations 013-023 (PR 7b-core) are forward-only after any wire witness — positive OR
> negative** (an `attempt_v1` decision callback with no attempt is durable proof nothing was
> staged, and counts). **`018` through `022` go further: they refuse downgrade
> unconditionally** (`MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY`,
> `MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY`) — walking below them would restore
> search-path-vulnerable or under-validated authority functions, so once `018` is on the schema
> the ONLY rollback is redeploying the prior reviewed **023-compatible** image against it.
> `023` itself is validation-only and its downgrade is a no-op, so a walk started from the head
> does not stop there — it reaches `022` and refuses with that sentinel, one revision lower than
> the command names. Do not apply `018` or anything above it in production until that bridge
> image has been reviewed and staged; this preproduction branch otherwise rolls forward. Below
> `018` the walk still preflights with stable sentinels, in execution order
> (`MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`,
> `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`) once an attempt row, a
> terminal `callback_wire_sha256`, or a `superseded` row exists — immutable
> delivery evidence is never destroyed because local status looks terminal;
> for a pending/dead callback the attempt row is the only proof bytes were
> staged. On refusal, KEEP or redeploy the reviewed **023-compatible** image — an older
> publisher lacks the receipt/terminal contract and must not run against preserved evidence;
> a pre-7b image is permitted only after the entire walk reaches 012.

> **`022` and `023` need EVERY decision writer drained — the pipeline AND the API — not just
> the publishers.** They are the first revisions in the chain to take `ACCESS EXCLUSIVE` on
> `decisions` and `cases`. Two writers take those locks in the opposite order: the pipeline's
> decide transaction (case `FOR UPDATE`, then the `decisions` insert), and the **API process
> itself** — `reviewer.manual_approve` is handled inline in the ingest transaction with the
> same case-lock-then-decision-insert shape, with no job and no run, so a job/run drain check
> cannot see it. Running either migration against either writer **deadlocks** (Postgres
> reports `40P01` and kills one side; reproduced against both a live decide and a live inline
> manual approval; `021` does not do it). This is not silent corruption: DDL is transactional,
> so a killed migration rolls back whole and the schema stays where it was. But it costs the
> window and it can kill the approval instead of the migration, so before applying `022`/`023`
> pause event submission, stop and attest the API writers AND the pipeline workers (as well as
> publishers/retention), then re-run. Unlike the live-claim preflight, this one is **not
> machine-checked** — `022` and `023` are published and cannot be amended to add one; the
> machine-checked fence ships with `024` (activation blocker O4).

### Migration refusal sentinels

Every deliberate migration refusal raises a **stable sentinel string**, so a refused
`alembic upgrade`/`downgrade` reads as a designed stop rather than a broken migration.
Grep the sentinel out of the command's output and find it here. The exception message
names the offending rows or objects and a remediation — but published migrations are
frozen, so a frozen message can lag this document: **where the message and this runbook
disagree, the runbook wins.** Concretely, `022`'s forward-only refusal still names the
compatible image for the revision it froze at (`022`); the image to keep is always the one
compatible with the **live head** — `023`-compatible today, kept current in this document
by a head-derived test that a frozen migration message cannot satisfy.

`tests/unit/test_plan_artifact_static.py` fails if a migration raises a sentinel this
table omits, so a new refusal cannot ship undocumented.

| Sentinel | Fires when |
|---|---|
| `MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH` | upgrade: the pre-existing `outbox_delivery_attempts` table being adopted is not the exact expected shape (the amended-013 history means 014 adopts-or-creates) |
| `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS` | upgrade: an unexpired outbox claim exists — stop publishers **and** retention, attest zero old processes, let leases expire or run `reset_interrupted_outbox_claims`, retry |
| `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS` | upgrade: same live-claim preflight as 017 |
| `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` | upgrade: the observable authority surface (columns, named constraints, indexes, trigger set, function-body digests) is not the one the prior revision installed — refuses **before** any DDL |
| `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH` | upgrade: same manifest check, pinned to 018's surface |
| `MIGRATION_020_AUTHORITY_CODE_MISMATCH` | upgrade: an owned function body or `search_path` pin, or an enabled trigger, differs from the canonical code surface |
| `MIGRATION_021_AUTHORITY_CODE_MISMATCH` | upgrade: same code-surface check, pinned to 020's |
| `MIGRATION_021_PREFLIGHT_UNSENDABLE_PENDING_ROWS` | upgrade: `pending` rows carry a redacted body and can never be delivered — the message lists their ids and the `UPDATE … SET status='dead'` that retires them. Arming the guard over them would head-of-line block their streams |
| `MIGRATION_022_AUTHORITY_SURFACE_MISMATCH` | upgrade: 021's exact trigger definitions or origin-enable modes do not validate |
| `MIGRATION_022_MANUAL_POINTER_MISMATCH` | upgrade: a `cases.latest_manual_decision_row_id` does not reference a same-case **manual** decision, so the guard cannot be installed over the data |
| `MIGRATION_023_CROSS_TABLE_AUTHORITY_MISMATCH` | upgrade: a cross-table authority constraint (`fk_outbox_decision_triple`, `fk_cases_latest_decision`, `fk_cases_latest_manual_decision`, their unique targets) or a same-case pointer/outbox row does not validate |
| `MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY` | downgrade: 013's recorded history was amended, so its own downgrade cannot be trusted to be the inverse of what ran |
| `MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: wire witness exists (attempt row, terminal `callback_wire_sha256`, or a `superseded` row) |
| `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 013 |
| `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 013 |
| `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 013, and additionally on NEGATIVE evidence (an `attempt_v1` row with no attempt is durable proof nothing was staged) |
| `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` | downgrade: as 016; this is the outermost witness authority, so a walk from above stops here first |
| `MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: **unconditional** — walking below 018 restores search-path-vulnerable or under-validated authority functions |
| `MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional |
| `MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional |
| `MIGRATION_021_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional |
| `MIGRATION_022_DOWNGRADE_REFUSED_FORWARD_ONLY` | downgrade: unconditional — the highest refusal on the chain, so this is the sentinel a walk from the head actually hits (`023`'s downgrade is a validation-only no-op) |

> **`enforce_bundle_pinning` (PR 6) is a drained, not rolling, flag flip.**
> Off (default), every worker scores under its own process-loaded policy
> bundle — today's behavior, unchanged. On, a worker resolves and scores each
> run under **that run's creation-pin bundle** (`runs.policy_bundle_hash`)
> loaded from the durable `policy_bundles` store, and refuses the job
> (dead-letter, zero side effects — no adapter call, check, decision, token,
> or outbox row) rather than silently falling back if that bundle can't be
> loaded. An inconsistent flag value across the worker pool risks different
> workers resolving different rubrics for the same run — flip it only via
> the drained cutover in `docs/DEPLOYMENT.md` §10, preflighted by the bundle
> preflight command above.

## Production configuration (startup kill switches)

Every process (`api`, `pipeline_worker`, `outbox_worker`) refuses to boot when
`KYC_ENVIRONMENT=production` and any of these is unsafe — `ProductionConfigError`
lists **all** violations at once:

| Variable | Production requirement |
|---|---|
| `KYC_AUTH_DISABLED` | `false` |
| `KYC_PLATFORM_HMAC_SECRET` | ≥ 32 chars (v1 legacy secret) |
| `KYC_HMAC_INBOUND_KEY_ID` / `KYC_HMAC_INBOUND_SECRET` | non-empty / ≥ 32 chars (v2 inbound) |
| `KYC_HMAC_OUTBOUND_KEY_ID` / `KYC_HMAC_OUTBOUND_SECRET` | non-empty / ≥ 32 chars (v2 callbacks) |
| `KYC_HMAC_V1_INBOUND_SUNSET_AT` / `KYC_HMAC_V1_OUTBOUND_SUNSET_AT` | tz-aware ISO-8601, both required (naive/malformed refused at boot) |
| `KYC_HMAC_V1_OBSERVATION_WINDOW_DAYS` | ≥ 1 |
| `KYC_PLATFORM_CALLBACK_URL` | HTTPS, not localhost |
| `KYC_OBJECT_STORE` / `KYC_S3_BUCKET` | `s3` / non-empty |
| `KYC_OCR_ENGINE` | not the `json_scan` dev stub |
| `KYC_EMAIL_PROVIDER` | not the `logging` dev stub |
| `KYC_ADAPTERS_PROFILE` | not the `fixture` stub |
| `KYC_READ_AUTH_REQUIRED` | `true` (read API requires a signed request) |
| `KYC_UI_ADMIN_TOKEN` | required when `KYC_UI_ENABLED=true` |
| `KYC_OUTBOX_LEASE_SECONDS` | must EXCEED `4 × KYC_OUTBOX_HTTP_TIMEOUT_SECONDS + KYC_OUTBOX_LEASE_MARGIN_SECONDS` — the publisher enforces 4 × timeout as a hard per-attempt deadline, and a lease that expires mid-attempt makes every delivery unwitnessable |
| `KYC_OUTBOX_HTTP_TIMEOUT_SECONDS` | per HTTPX **inactivity** phase (not a total clock); 4 × this is the enforced whole-attempt deadline. Raising it raises the required lease FOUR-fold — move the two together or production refuses to boot |
| `KYC_OUTBOX_LEASE_MARGIN_SECONDS` | DB commit/processing room added to the deadline in the lease rule above |
| `KYC_OUTBOX_MAX_ATTEMPTS` | delivery-attempt ceiling (1 ≤ n ≤ int4 max). **NOT hot-swappable — ANY change, raise OR lower, is a DRAINED publisher cutover, never a rolling restart** (each publisher enforces the ceiling it started with; overlapping old/new publishers either send once past a lowered value or dead-letter — and irreversibly redact a POC token — before a raised value takes effect). Cutover, in order: disable autoscaling/rolling restart → stop ALL outbox publishers of every role (`outbox_worker` AND `dev_worker`) → attest zero running → attest every new task definition carries the exact new value → start. Canonical record: `kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER` (DEPLOYMENT §8) |

`KYC_OUTBOX_MAX_ATTEMPTS` is a both-direction drained publisher cutover — the
canonical record (rendered from `kyc_tool.ops.cutover.OUTBOX_MAX_ATTEMPTS_CUTOVER`,
identical to DEPLOYMENT §8 and `.env.example`):

<!-- cutover:KYC_OUTBOX_MAX_ATTEMPTS:start -->
KYC_OUTBOX_MAX_ATTEMPTS: both-direction DRAINED publisher cutover (NOT a rolling restart)
1. disable autoscaling and rolling restart
2. stop ALL publishers of roles: outbox_worker, dev_worker
3. attest zero publishers running of roles: outbox_worker, dev_worker
4. attest every new task definition carries KYC_OUTBOX_MAX_ATTEMPTS
5. start publishers of roles: outbox_worker, dev_worker
<!-- cutover:KYC_OUTBOX_MAX_ATTEMPTS:end -->

The real OCR/email/adapter providers are not implemented yet (they land with the
executable-contract work), so a production worker cannot start until they exist —
that is intentional fail-closed behaviour, not a bug.

> **Temporary safety hold.** Until the approval-grade validators are hardened,
> `KYC_ENFORCE_POSITIVE_DECISIONS` defaults to `false`: a computed `approve` /
> `approve_buy_locked` is emitted as `manual_review_insufficient` (the callback
> carries an `enforcement_held` object with the computed decision; the audit
> trail records both). Flip to `true` only once the validators fail closed.

## Ops console (`/ui`)

The built-in console covers most of this runbook visually: Overview (health
tiles, dead-letter tables with one-click requeue), Cases (score meter, gates,
supersession chains, per-run state machine, audit trail), Integrations
(stub/live/needs-config per adapter, env presence, reachability probes),
Field Map (live Salesforce projection per case), Policy, and a Composer that
sends signed events server-side. **Security**: **off by default**
(`KYC_UI_ENABLED=false`); when enabled in production it requires
`KYC_UI_ADMIN_TOKEN`, and every mutating endpoint (composer, requeue, probe)
demands `Authorization: Bearer <token>`. Local dev: `bash scripts/dev.sh`
boots the whole stack and prints the console URL.

**Composer production prohibition (PR 5b).** In production
(`KYC_ENVIRONMENT=production`) the composer endpoint (`POST
/ui/api/send-event`) refuses `website.review_completed` and
`reviewer.manual_approve` with **403** — real reviewer actions must arrive as
signed platform events carrying a genuine reviewer actor (see
`docs/PLATFORM_INTEGRATION.md` §3, "Reviewer actor requirement"), not be
typed into the console by an operator. This server-side 403 is the actual
security boundary; hiding the composer's controls for these two event types
in the console UI is optional polish on top of it, not a substitute for it. In
dev/staging the composer still sends both event types, but with a real
`{"type": "reviewer", "id": <reviewer_id>}` actor instead of the generic
`system`/`ops-console` actor it uses for everything else, so console testing
exercises the same binding production enforces rather than bypassing it.

## Health & dashboards

- `GET /healthz` — liveness + the policy bundle hash. **A hash change without a
  deploy is an incident** (policy files are immutable per release).
- `GET /readyz` — readiness: config validity (production), DB connectivity,
  migration head match, S3 access, and — **unconditionally, regardless of
  `enforce_bundle_pinning`** — that this process's own loaded policy bundle
  is durably resolvable from the `policy_bundles` store (PR 6). `503` on any
  failing check, including a missing/corrupt bundle row; wire it to the load
  balancer so a mis-migrated, misconfigured, or un-seeded instance drains.
- `GET /v1/metrics` — watch: `jobs_by_status.dead` (alert > 0),
  `outbox_by_status.dead` (alert > 0), `runs_by_state.FAILED`,
  `review_tasks_open_by_type` growth, `event_to_decision_seconds.p95`
  (budget: < 10s adapter-light, < 120s full runs, human waits excluded),
  `adapter_latency[].error_rate` per upstream.

## Failure playbooks

### Dead-lettered job (`jobs.status = 'dead'`)
The run is FAILED with the error recorded; the case is untouched (no partial
writes — transitions are transactional). After fixing the cause, **prefer the
console requeue** (`POST /ui/api/requeue/job/{id}`, admin-authenticated) — it
resets both the job and its FAILED run. The equivalent by hand:
```sql
UPDATE jobs SET status='queued', attempts=0, run_after=now(), locked_by=NULL,
       lease_expires_at=NULL, last_error=NULL, updated_at=now() WHERE id = :id;
UPDATE runs SET state='QUEUED', error=NULL, finished_at=NULL
 WHERE id = :run_id AND state='FAILED';
```
(The run reset matters: a requeued job whose run is still FAILED completes
immediately without doing anything.) `recalculate.requested` also produces a
fresh decision from current live checks, but it does **not** re-run the broker
screen — after a blocklist update, re-send the original evidence event instead.

**Died with `BundleUnavailable` (PR 6, `enforce_bundle_pinning=true`)?** The
run's creation-pin bundle (`runs.policy_bundle_hash`) isn't in the
`policy_bundles` store. **Requeuing without seeding it first just fails
again the same way.** To reprocess the run, you must first seed that exact
historical bundle: run `ops.seed_policy_bundle --expect-hash
<runs.policy_bundle_hash>` pointed at a `KYC_POLICY_DIR` containing those
exact files (the command computes-then-compares-then-stores, so a
mismatched directory writes nothing) — only then does the requeue above
succeed.

### Dead outbox row (callback undeliverable)
The run sits in PUBLISH_DECISION (visible, correct). Confirm the platform
endpoint + HMAC secret, then requeue via the console
(`POST /ui/api/requeue/outbox/{id}`) or:
```sql
UPDATE outbox SET status='pending', attempts=0, next_attempt_at=now() WHERE id = :id;
```
Redelivery of a decision callback is safe — the platform dedupes on
(case_id, run_id). **A row whose body has been REDACTED is the exception, for
either kind** (migration 020+): a POC email's payload is scrubbed the moment it
dies, because the raw token is never retained, and a decision callback's is
scrubbed by retention once past `KYC_RETENTION_DAYS`. Either way there is
nothing deliverable left, so the console endpoint returns 409 — and the raw SQL
above is refused by the database with *"a redacted outbox row can never be MADE
sendable again"*. Recovery is a fresh `poc.submitted` (which cancels old tokens
and sends a new email) or, for a callback, `recalculate.requested`, which
produces a NEW decision under a new `run_id` — it does not restore the old
body. A pending row that somehow already carries a redacted body is unsendable
and should be retired, not requeued:
```sql
UPDATE outbox SET status='dead', last_error='body redacted; unsendable'
WHERE status='pending' AND payload_json = '{"redacted": true}'::jsonb;
```

### RIR / registry outage
Runs complete as `partial` (upstream_error recorded, prior checks stay live,
no failing check is invented — G12 semantics). No action needed; when the
upstream recovers, re-drive affected cases by re-sending the original
evidence event (fresh idempotency keys). `recalculate.requested` re-decides
without re-fetching and without the broker screen — use it only when no new
evidence or blocklist change is in play.

### Review queue growing
`GET /v1/review-tasks?status=open`. Website tasks award +10 on pass;
`poc_email_unavailable` needs an alternate-proof decision by compliance
(v1: resolve on the platform; the tool records outcomes via events).

### Latency
Adapter p95 in `/v1/metrics`; per-upstream rate caps via
`KYC_ADAPTER_RATE_LIMITS` (requests/sec, process-local — divide by worker
count). Queue depth is `jobs_by_status.queued`; scale pipeline workers
horizontally (SKIP LOCKED makes them safe; per-case ordering is preserved).

## PR 7b-core cutover — drained maintenance window (migration 013)

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

## Retention & compliance

`workers.retention` prunes audit rows, delivered **`poc_email`** outbox rows,
and expired unverified POC tokens past `KYC_RETENTION_DAYS` (default 7y).
Checks, decisions, and raw evidence are NOT auto-pruned — deleting the
decision record requires compliance sign-off; do it as a supervised one-off.

**`decision_callback` outbox ROWS are NOT pruned either (PR 7b-core), but their
BODIES are destroyed past the window.** The row is the durable ordering
authority the platform reconciliation is built from — `id` the order, `status`
the local delivery outcome — so it is kept indefinitely. `payload_json`, which
carries `checks[].source` (can be reviewer-derived), is a different matter:
`workers.retention` redacts it to `{"redacted": true}` once
`COALESCE(delivered_at, resolved_at, created_at)` is past
`KYC_RETENTION_DAYS`, for any row whose `status` is `delivered`, `superseded`
**or `dead`** (migration 020 — a callback that exhausted its attempts during a
platform outage carries the same reviewer-derived body as any other, and
keeping it forever inverted this policy; a dead row has neither `delivered_at`
nor `resolved_at`, so it ages on `created_at`). Redaction **never** touches a
`pending` row: that body is still sendable, and the database refuses the write.
A redacted row can no longer be requeued — the console returns 409 and names
the remedy per kind — which is the intended trade: the body is destroyed on
schedule, and nothing is left able to transmit a `{"redacted": true}` payload
to the platform.

What survives redaction — `case_id`, `run_id`, `decision_sequence`, `status`,
`delivered_at`/`resolved_at`, and the recorded wire digest
(`callback_wire_sha256`, `wire_version`) — is pseudonymous, not out of scope: a
hash plus internal ordinals, still joinable back to a case. Retaining that
remainder past the window is a governed decision recorded in
`AUDIT_FINDINGS.md` (D9), not a claim that it falls outside backup, erasure,
privacy, or compliance review; accountability for it is the **deployer's data
controller's** — this repo cannot name that owner and does not discharge the
controller's obligations, it only bounds what it keeps and documents the
bound. Two things this repo does NOT reach: (1) an erasure request landing
**inside** the window must still reach the live callback snapshot (its body is
not yet redacted) as well as the decision record; (2) **backups taken before
redaction** are a separate durable copy and still contain the pre-redaction
body until they age out on the backup retention schedule, independent of
`KYC_RETENTION_DAYS`.

## Policy changes

Rubric/decision/broker JSON changes ship as a deploy: bump the file's
`version`, update `tests/policy_driven/policy_baseline.json` in the same
commit (the drift-guard test enforces this), redeploy. Every run/decision
records the sha of the policy that produced it.

## Policy bundle pinning & provenance (PR 6)

Full cutover procedure: `docs/DEPLOYMENT.md` §10. Summary of the ongoing
operator surface once it's live:

- **The flag.** `enforce_bundle_pinning` (default off) — see the callout
  under Processes above. Flip it only via a drained cutover, never a rolling
  toggle.
- **`ops.verify_pinnable_backlog`** — preflight before enabling the flag;
  prints the run ids whose creation-pin bundle wouldn't resolve and exits
  nonzero if any exist.
- **`ops.seed_policy_bundle --expect-hash <sha256>`** — the only way to add
  a bundle to the durable store by hand (compute → compare to
  `--expect-hash` → store; a mismatch writes nothing). Every process also
  self-seeds its own on-disk bundle at startup, so day-to-day this command
  is for the preflight gap case above and for **historical recovery** — see
  the reprocess note below.
- **`GET /readyz`** — 503 when the process's own policy bundle isn't in the
  store (unconditional, both flag states).
- **Post-epoch NULL-provenance check.** Once
  `ops.activate_bundle_pinning_epoch` has run, any `checks` row with `NULL
  policy_bundle_hash`, or any `decisions`/`runs` row with `NULL
  engine_build_id`, created **after** `bundle_pinning_epoch.activated_at` is
  an anomaly — a rollover gap or a bypassed write path, never an expected
  state (a legitimately still-queued run is not flagged). The check is
  `kyc_tool.ops.activate_bundle_pinning_epoch.post_epoch_null_provenance(session)`
  — it returns `{"decisions": [...], "checks": [...], "runs": [...]}` of the
  offending ids; it is not yet wired to `/v1/metrics` or a CLI, so run it
  ad hoc (a Python shell against the production DB) or wire it into your own
  alerting.
- **To reprocess a run, you must first seed its bundle.** Whether via
  `enforce_bundle_pinning` at the time (a live `BundleUnavailable`
  dead-letter — see the failure playbook above) or later, historical
  reprocessing under a run's original rubric requires that exact bundle to
  be in `policy_bundles` first; `ops.seed_policy_bundle --expect-hash
  <hash>` against the matching historical policy directory is the only
  supported path to add it back.

## Secrets

All via environment (see `.env.example`): platform HMAC, CH/ARIN keys,
email provider, S3. Nothing is ever logged; POC token emails log only a
recipient hash.

> **Upgrade preflight (`017`, `018`).** Both refuse with
> `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS` / `MIGRATION_018_PREFLIGHT_LIVE_CLAIMS`
> while any unexpired outbox claim exists — the
> database checks live outbox claims, but it cannot prove that no API, publisher, pipeline,
> dev worker, or retention process is still running. Stop those processes, attest zero old
> processes at the orchestrator, then either let the leases expire
> (`KYC_OUTBOX_LEASE_SECONDS` bounds how long that takes) or run the shipped
> `python -m kyc_tool.ops.reset_interrupted_outbox_claims` (post-013-only; clears complete
> claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero — it refuses
> rather than partially resetting if any tuple survives), then retry. It MUST NOT run while
> any publisher is live. `018` and `019` additionally refuse with
> `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` / `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH`
> when the
> observable authority surface (columns, constraints, indexes, trigger definitions, authority
> function bodies, pinned `search_path`) is not the one `017` installed — reconcile the
> environment, or restore from the reviewed image, before retrying. Every refusal happens BEFORE
> any DDL: the schema and the alembic head are left exactly as they were.
