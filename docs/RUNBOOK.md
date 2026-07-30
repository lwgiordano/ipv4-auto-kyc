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

### Migration refusal sentinels

Every deliberate migration refusal raises a **stable sentinel string**, so a refused
`alembic upgrade`/`downgrade` reads as a designed stop rather than a broken migration.
Grep the sentinel out of the command's output and find it here. The exception message
itself always names the offending rows or objects and the remediation — this index tells
you what class of stop you are looking at; the message tells you what to do about it.

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
> processes at the orchestrator, then let the leases expire and retry. (The migrations' own refusal text
> names a `reset_interrupted_outbox_claims` CLI: that command is PLANNED and NOT YET
> BUILT — it ships with the checkpointed ops tasks. Until it does, waiting out the lease
> is the remediation; `KYC_OUTBOX_LEASE_SECONDS` bounds how long that takes.) `018` and `019` additionally refuse with
> `MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH` / `MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH`
> when the
> observable authority surface (columns, constraints, indexes, trigger definitions, authority
> function bodies, pinned `search_path`) is not the one `017` installed — reconcile the
> environment, or restore from the reviewed image, before retrying. Every refusal happens BEFORE
> any DDL: the schema and the alembic head are left exactly as they were.
