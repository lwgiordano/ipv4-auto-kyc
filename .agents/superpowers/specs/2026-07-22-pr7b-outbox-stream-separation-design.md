# PR 7b — Outbox stream separation + platform-authoritative decision ordering (item 8) — design (rev 4)

## Context

PR 7b is reordered **before PR 6b** because it supplies the callback-ordering guarantee 6b's
coordinator callbacks must rest on (Codex PR-6b rev-5 F3). The transactional outbox
(`src/kyc_tool/outbox/publisher.py`) has two present defects:

1. **One mixed per-case FIFO.** `_CLAIM_SQL` (`publisher.py:29-49`) claims the min-id pending row
   per `case_id`, mixing `decision_callback` + `poc_email`; a stuck POC email blocks the case's
   decision callbacks.
2. **No decision ordering / revert guard.** Callbacks are ordered only by outbox `id`; a requeued
   older decision can be delivered after a newer one, reverting the platform. Delivery stamps
   `decisions.published_at` + run `COMPLETE` (`_record_delivered`, `publisher.py:160-191`) — 6b's
   gate reads this — but nothing prevents the revert.

### Revision history

- **Rev 1** (DB-only guard, rolling migration) — wrong: the publisher commits its claim
  (`publisher.py:146-148`), HTTPs outside any txn (`151-152`), stamps `published_at` in a second txn
  (`160-191`), so no local guard closes the cross-replica race; NULL `ordering_stream` from an old
  replica made rows unclaimable. Under-delivered vs ROADMAP **M3**(`:96`)/**D1**(`:32`).
- **Rev 2** — platform high-water authoritative, `decision_sequence` on the wire, drained
  transactional cutover, legacy sequencing, forward-only downgrade, `superseded` lifecycle,
  relational integrity.
- **Rev 3** — emission decided at the publisher (not frozen at decide time), production high-water
  bootstrap, two-phase rollback, cutover reusing the shipped recovery one-shot, exact 6b
  convergence, ROADMAP detailed sections renumbered.
- **Rev 4 (this doc)** — makes the **activation authority executable, authenticated, and
  crash-recoverable**, modeled on the shipped epoch machinery (`activate_bundle_pinning_epoch.py`,
  `policy_store/repo.activate_epoch:73-85`, `activate_hmac_v1_observation.py`): (F1) activation is a
  **second drained window** with a shared **runtime phase reader**, closing the silent-loss window
  where a stripped callback gets a sticky-receiver no-op and is locally marked delivered; (F2) the
  bootstrap manifest is a **tool-derivable candidate set** the platform reconciles against its
  **accepted-run ledger + separate effective source** (handling manual and reverted state); (F3) the
  activation record becomes a **phase state machine + immutable HMAC-authenticated artifacts + exact
  CLIs**; (F4) migration 013 **separates the claim lease from the retry schedule**; (F5) a **triple
  identity** binds each callback to its exact decision; (F6) an **`integrity_mismatch`** terminal is
  distinct from `superseded`, and the backlog proof is split (dead rows require a real requeue).

This is `.agents/ROADMAP.md` item 8.

## Decisions (rev 4)

1. **Stream separation** — `outbox.ordering_stream` (`decision`|`email`), NOT NULL; claim FIFO per
   `(case_id, ordering_stream)`.
2. **Per-case decision sequence** — `decisions.decision_sequence` from the locked counter
   (`cases.last_decision_sequence`, never `max()+1`), callback-emitting decisions only; persisted in
   the internal outbox payload at enqueue.
3. **The platform high-water is the ordering authority**, seeded by an authenticated **bootstrap**
   from the platform's *accepted-run ledger* before emission, and **sticky** thereafter.
4. **Emission is decided at the publisher** via a **shared runtime phase reader**, checked before
   claim and again before HTTP; fail-closed on any unexpected phase/flag tuple.
5. **The activation authority is an executable phase state machine** (`legacy →
   bootstrap_in_progress → bootstrapped → active`) with immutable authenticated artifacts and exact
   CAS CLIs — the single source of truth for `/readyz`, the publisher, `dev_worker`, metrics, and 6b.
6. **The local `superseded` guard is an optimization**; a tuple/integrity mismatch is a **distinct
   non-retryable terminal**, never `superseded`.
7. **Two drained windows** — the migration cutover (§Rollout A) and the activation cutover
   (§Rollout B); **forward-only-after-use** downgrade; **two-phase irreversible** rollback.
8. **Triple-identity integrity** — each decision callback is bound to `(run_id, case_id,
   decision_sequence)` of exactly one automatic decision; the pre-HTTP reader compares all three.

## Architecture

### 1. Migration 013 — drained cutover (`down_revision='012'`)

**7b owns `013`** (ROADMAP table + detailed sections renumbered this commit: 7b `013` / 6b `014` /
7a `015` / 8 `016` / 10 `017`). Precondition (§Rollout A): platform paused, **zero** app writers
attested at the orchestrator level (not `pg_stat_activity` — engines set no `application_name`,
`session.py:16-17`), the shipped job + new outbox recovery one-shots already run.

Schema (one ordinary transaction — writers stopped, no `CONCURRENTLY`):

**Stream + sequence + terminal-state columns**
- `outbox.ordering_stream TEXT` — add nullable → backfill by kind → assert zero NULL/unknown →
  add+VALIDATE `CHECK (ordering_stream IS NOT NULL AND ordering_stream IN ('decision','email'))`.
- `outbox.decision_sequence BIGINT NULL`, `outbox.resolved_at TIMESTAMPTZ NULL`,
  `decisions.decision_sequence BIGINT NULL`, `cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`.
- **`outbox.failure_class TEXT NULL`** (F6) — set only on a terminal `dead` from an integrity
  mismatch (`'integrity_mismatch'`); NULL for ordinary retryable/dead-lettered rows.

**Claim-lease/retry separation (F4)**
- `outbox.claim_lease_expires_at TIMESTAMPTZ NULL` (+ `claimed_by TEXT NULL` for diagnostics). The
  claim sets **only** this lease; `next_attempt_at` stays the *retry due time*. A failed delivery
  clears the lease and advances `next_attempt_at`; a successful delivery clears the lease. So a
  claimed-but-unstamped row is identifiable by `claim_lease_expires_at IS NOT NULL`, distinct from a
  backing-off retry.

**Triple-identity integrity (F5)**
- `UNIQUE decisions(run_id)` (Postgres permits multiple NULL manual rows) and a unique referenced
  identity `UNIQUE decisions(run_id, case_id, decision_sequence)`.
- CHECK on `decisions`: `(manual=false → run_id IS NOT NULL AND decision_sequence > 0) AND
  (manual=true → run_id IS NULL AND decision_sequence IS NULL)`.
- CHECK on `outbox`: `(kind='decision_callback' → ordering_stream='decision' AND case_id IS NOT NULL
  AND run_id IS NOT NULL AND decision_sequence > 0) AND (kind='poc_email' → ordering_stream='email'
  AND run_id IS NULL AND decision_sequence IS NULL)`.
- **Triple FK** `outbox(run_id, case_id, decision_sequence) → decisions(run_id, case_id,
  decision_sequence)` — no NULL member on a decision-callback row, so it is always enforced (closes
  the NULL-case_id FK bypass). One callback per automatic run follows from `UNIQUE decisions(run_id)`
  + the FK; preflight also asserts exactly-one existing callback per automatic run.

**Activation state machine + immutable artifacts (F3)**
- `outbox_ordering_activation` singleton (`id smallint PK DEFAULT 1 CHECK (id=1)`): `phase TEXT NOT
  NULL DEFAULT 'legacy' CHECK (phase IN ('legacy','bootstrap_in_progress','bootstrapped','active'))`;
  `bootstrap_started_at`, `bootstrapped_at`, `activated_at` (ordered); `request_digest`,
  `response_digest` (`CHECK (~ '^[0-9a-f]{64}$')` when present); FKs to the artifact table. Phase
  CHECKs: `bootstrapped`/`active` require both digests + ordered timestamps; **no reverse
  transition** (enforced by the CAS CLIs + a trigger/CHECK that the new phase only advances).
- `outbox_ordering_bootstrap_artifacts(digest TEXT PK CHECK (~ '^[0-9a-f]{64}$'), kind TEXT CHECK
  (kind IN ('request','response')), body_json JSONB NOT NULL, created_at)` — immutable
  (insert-only), the singleton FK-bound to its request+response rows. Seeded empty; the singleton
  seeded `phase='legacy'`.

**Historical backfill (rev-2/3, kept)** — per case, `decision_sequence = row_number() OVER
(PARTITION BY case_id ORDER BY decided_at, id)` for callback decisions (`manual=false AND run_id IS
NOT NULL`); manual NULL; bind every `decision_callback` outbox row (incl. **dead**) via `run_id`;
seed `last_decision_sequence = max`; backfill the sequence into each existing
`decision_callback.payload_json` (F1, rev 3). Preflight refusals (raise, roll back): orphan/dup-run
callback, sequence mismatch, remaining NULL decision-stream sequence, sequenced email row, **more
than one callback per automatic run, any automatic decision with NULL run or non-positive sequence**.

**Downgrade — forward-only-after-use** (F3/rev-2): raises if any `decision_sequence`,
`last_decision_sequence > 0`, or `phase != 'legacy'` exists; else drops cleanly (`up→down→up` green
on a fresh DB). Ordinary transactional index/DDL; add `ix_outbox_stream_claim (case_id,
ordering_stream, status, next_attempt_at)`.

### 2. Stream-scoped claim + lease (publisher) (F4)

`_CLAIM_SQL` gains `AND o2.ordering_stream = o.ordering_stream` (never UNKNOWN — NOT NULL), requires
`(claim_lease_expires_at IS NULL OR claim_lease_expires_at <= now())`, and on claim sets **only**
`claim_lease_expires_at = now()+lease` (leaving `next_attempt_at` as the retry due time).
`_record_delivered`/`_record_failure`/`_record_superseded` clear the lease; failure alone advances
`next_attempt_at`. `enqueue_decision_callback` sets `ordering_stream='decision'` +
`decision_sequence`; `enqueue_poc_email` sets `ordering_stream='email'`.

### 3. Decision-sequence allocation (decide transaction)

Seam `pipeline._decide_txn` (`pipeline.py:351-518`) holds the Case `FOR UPDATE` from `_load`
(`:201`) across the `DecisionRow` build (`:485-495`) and enqueue (`:506`). A callback-emitting
decision increments `cases.last_decision_sequence` under the lock, stamps
`DecisionRow.decision_sequence`, and passes it to `enqueue_decision_callback(..., decision_sequence=seq)`
which writes it on the outbox row **and inside `payload_json`**. Manual approve
(`ingest._handle_manual_approve`, `ingest.py:242-267`, `run_id=None`, no callback) allocates no
sequence. Single writer of the counter, always under the case lock.

### 4. Emission at the publisher + the shared runtime phase reader (F1)

The pipeline never flag-gates `decision_sequence` (it always writes it to the internal payload).
All emission logic lives at `_deliver_decision_callback` (`publisher.py:82-122`), governed by one
shared reader `read_ordering_phase(session) → (phase, flag)`:

| phase / flag | claim + send behavior |
|---|---|
| `legacy` (no bootstrap) | strip the field; deliver byte-identical to today |
| `bootstrap_in_progress` or `bootstrapped` | **refuse to claim or send** (publishers are stopped in §Rollout B; this is the fail-closed backstop) |
| `active` AND `callback_include_decision_sequence=true` | require a non-NULL sequence; emit it |
| any other tuple (e.g. `active` + flag false) | **fail closed** — halt the process on an active row; never reinterpret `active` as emission-off |

The reader is checked **before claim** and **again immediately before HTTP** (phase can advance
between). This closes the silent-loss window: an already-running `legacy`/false-flag publisher can
never strip-and-send into an initialized (sticky) receiver and then locally stamp delivered —
because activation (§Rollout B) drains publishers to zero and pre-arms them at `flag=true` before
the phase becomes `active`. `_record_delivered` (`publisher.py:160-191`) still stamps delivered +
run `COMPLETE` + `published_at` — but only for a genuinely applied send.

Config (`config.py:77-79`): `callback_include_decision_sequence: bool = False`. Schema
(`api/schemas.py:144-164`): `decision_sequence: int | None = None` (preserved on validate). `/readyz`,
`outbox_worker.build_publisher`, `dev_worker.main`, and metrics all consume `read_ordering_phase`;
**startup fails if `phase='active'` but the flag is false** (F3/rev-3 rollback invariant).

### 5. Platform authority — candidate manifest, reconciliation, and bootstrap protocol (F2/F3)

Documented in `PLATFORM_INTEGRATION.md` + **ADR-008** (ADR-006 reserved for 6b, ADR-007 for PR 10).

**Receiver contract:** on `POST /kyc/decision` for `case_id=c`, `decision_sequence=s`: one txn — if
`s > h(c)` apply + set `h(c)=s`; if `s ≤ h(c)` no-op + success; once initialized, a body **without**
a sequence is a no-op (sticky), never a mutation. Emails carry no sequence.

**Candidate manifest (tool-derivable, F2):** the tool exports, over an agreed case universe, **every
immutable callback decision** as `(case_id, run_id, decision_sequence, callback_body_sha256,
local_outbox_status)`. It **never invents `platform_current_run_id`** (it cannot know the platform's
effective state; and a manual-current case, `run_id=NULL`, has no callback run at all). New/
manual-only cases carry an explicit `no_prior_callback` marker.

**Platform reconciliation (F2):** the platform returns, for the exact universe: (a) its **accepted
callback-run ledger** (or at least the greatest accepted callback run) and (b) its **current
effective source** separately — `callback:<run_id>` or `manual:<manual-event-id>`. The tool maps
each returned callback run uniquely to a candidate and computes `h(c)` = the greatest sequence the
platform **proves it accepted**. Rules:
- Current source is a callback **below** the greatest accepted sequence (a legacy revert, e.g. seq 2
  applied then legacy seq 1) → **refuse activation until content is reconciled and re-attested** — do
  not merely advance the integer.
- Current source is **manual** → it may remain effective while `h(c)` is seeded from the callback
  ledger, so no older callback can overwrite the manual action.
- **Exact two-sided coverage** — unknown, duplicate, extra, or omitted cases **fail closed**.
This attested mapping (not the tool's local `delivered`/`published_at`) is what §7's convergence
predicate consumes.

**Bootstrap protocol (authenticated, idempotent, crash-recoverable, F3):**
- **Canonical codec** — a versioned encoding (compact UTF-8 JSON, sorted object keys and rows,
  integer sequences, no floats) + lowercase SHA-256, mirroring the policy-bundle
  reconstruction-by-hash pattern (`policy_store/repo.py`). Request and response bytes are persisted
  verbatim in `outbox_ordering_bootstrap_artifacts` keyed by digest.
- **Endpoint** — the platform bootstrap endpoint is authenticated with the existing path-bound
  **HMAC-v2** contract, idempotent on `request_digest`, and **queryable by that digest after a
  timeout** (so a lost response is recoverable, not a guess).
- **Three CLIs** (modeled on `activate_bundle_pinning_epoch.py` + `activate_epoch` CAS):
  - `export_outbox_ordering_manifest` — writes the candidate manifest + its `request_digest`.
  - `record_platform_ordering_bootstrap --expect-request-sha --response-file` — validates the
    returned signature + coverage against the local candidate set, locks row 1, persists both
    artifacts, CAS `phase: legacy → bootstrap_in_progress → bootstrapped` with `request_digest`/
    `response_digest`, read-back-asserts; an exact rerun is idempotent, a **different** rerun refuses.
  - `activate_outbox_ordering --expect-request-sha --expect-response-sha` — CAS `phase:
    bootstrapped → active` only when the digests match the stored artifacts, using DB time,
    write-once + read-back (exactly `activate_epoch`'s shape).
- **On timeout / unknown external commit** — remain `bootstrap_in_progress` with publishers stopped;
  query/reconcile by digest; **never resume legacy delivery** (a lost response may mean the platform
  already committed sticky high-waters).

### 6. `superseded` vs `integrity_mismatch`, and the local guard (F6)

**Superseded (optimization, rev-2/3):** in `process_once`, after claiming a `decision`-stream row,
before `_deliver`: if a higher-sequence decision for the case is already delivered
(`EXISTS(... published_at IS NOT NULL)`), `_record_superseded(row)` (one txn, `WHERE status='pending'`):
outbox `superseded` + `resolved_at`; `DecisionRow.published_at` NULL; run through the existing legal
`PUBLISH_DECISION→COMPLETE` edge (no new RunState, `models.py:35-46`); audit `outbox.superseded`.
Retention prunes it with `delivered` (`retention.py:29-35`); metrics separate + excluded from
pending/dead alerts (`routes_metrics.py:60`).

**Integrity mismatch (F6):** the pre-HTTP reader compares **`case_id`, `run_id`, and
`decision_sequence`** across the decision row, the outbox columns, and the internal JSON (not
sequence alone). A mismatch → immediate **non-retryable** terminal: `status='dead'`,
`failure_class='integrity_mismatch'`, `resolved_at`, **zero HTTP**, `published_at` NULL, an audit
entry with only identifiers + expected-vs-observed hashes, and a metric/alert. It is **never
`superseded`** (that is alert-excluded and would hide corruption). The generic UI requeue
(`ui/routes.py:451-476`) returns **409** for `failure_class='integrity_mismatch'` until a dedicated
repair procedure reconstructs and verifies the canonical tuple.

### 7. PR 6b convergence contract (F2/F5) — the exact shared predicate

6b consumes a precise predicate over each case's allocated `decision_sequence`, keyed to the
**attested platform mapping** from §5 (never the tool's local `published_at` alone):
- Let `g` = the greatest allocated sequence. Convergence for `g` holds **iff** the platform's
  attested `h(case) ≥ g` **and** `g`'s outbox row is terminal-clean (`delivered`, or `superseded`
  only when a strictly higher **delivered** sequence exists). If `g`'s row is `pending` or **`dead`**
  (including a `dead` higher callback the UI can requeue), activation **blocks**.
- A **missing or duplicate** callback→decision mapping is a **blocker**, never "converged."
- Lower `dead` rows are safe only after `phase='active'` (sticky high-water live).
- A **superseded 6b coordinator** needs 6b **rev 6** to separately prove the higher delivered
  decision was produced under the **target validator pair** — 7b proves order, not freshness.
This predicate ships as the shared query 7b owns and 6b imports.

## Rollout — two drained windows + two-phase rollback

**A. Migration cutover (013)** — digest-pinned images; every one-shot/service on the reviewed digest.
1. Pause platform submission; edge-block the composer; disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue **and** outbox, `dev_worker.py:128-139`),
   retention, every DB writer; attest zero at the orchestrator (no heartbeat exists; not
   `pg_stat_activity`).
3. Run the shipped `python -m kyc_tool.ops.requeue_interrupted_jobs` (requeues `running`, decrements
   attempts so a killed final attempt retries; refuses if a worker is live) + the **new
   outbox-lease-reset one-shot** (clears **only non-NULL `claim_lease_expires_at`**, read-back-asserts
   zero, leaves retry schedules untouched — F4).
4. `alembic upgrade head` (013) — backfill + preflights + constraints; a preflight raise rolls back.
5. Start API only; probe `/readyz` (dynamic head + phase). Then start + attest pipeline/outbox.
   **No mutating production smoke** — behavior proven against the exact digest in staging / an
   isolated DB.
6. Resume submission (phase is `legacy`; delivery byte-identical to today). No non-digest writer after.

**B. Activation cutover (later, F1)** — a second drained window, **not** a per-producer flag flip.
1. Pause platform KYC state changes **including manual approvals**; stop + orchestrator-attest zero
   outbox publishers **and `dev_worker`**; `record_platform_ordering_bootstrap` CAS →
   `bootstrap_in_progress`.
2. Run the external bootstrap (§5) + local verification; CAS → `bootstrapped`. Publishers stay at
   **zero** throughout.
3. Pre-arm every exact-digest publisher with `callback_include_decision_sequence=true` **while
   stopped**; `activate_outbox_ordering` CAS → `active`; then start + attest publishers; resume.
4. On any timeout/anomaly, stay `bootstrap_in_progress` with publishers stopped; reconcile by digest.
Only after `phase='active'` may PR 6b activate.

**Rollback — two irreversible phases (rev-3, tightened):**
- **Before `phase` leaves `legacy`:** rollback only on the **schema-compatible PR-7b image**,
  publisher unstarted / `legacy` — never the pre-7b binary (its enqueue writes no `ordering_stream`,
  `publisher.py:52-63`, and fails 013's NOT-NULL/CHECK/FK). 
- **After `phase='active'`:** emission-off is **prohibited** (sticky receiver rejects a missing
  sequence). Rollback stays on a sequence-capable image with the flag on; a defect is **forward-fix**
  or a **separately approved** reconciliation. **Startup/readiness FAILS if `phase='active'` and the
  flag is false.** Document exact permitted image digests per phase + the preflight queries.

## Invariants

- Claim FIFO per `(case_id, ordering_stream)`; `ordering_stream` NOT NULL everywhere. The **claim
  lease** (`claim_lease_expires_at`) is separate from the **retry schedule** (`next_attempt_at`); the
  recovery CLI resets only claimed rows.
- `decision_sequence` allocated only under `Case FOR UPDATE`, unique per case, monotonic, and carried
  in the internal payload. Each decision callback is bound by the **triple identity** `(run_id,
  case_id, decision_sequence)` to exactly one automatic decision; the pre-HTTP reader compares all
  three across decision/outbox/JSON.
- Ordering authority is the **platform high-water**, seeded by the **authenticated bootstrap** from
  the platform's *accepted-run ledger* (reconciling manual/reverted state) and **sticky** thereafter.
  The activation authority is an **executable phase state machine** with immutable authenticated
  artifacts and CAS CLIs; the same **runtime phase reader** governs `/readyz`, the publisher,
  `dev_worker`, metrics, and 6b, and is checked before claim and before HTTP, fail-closed.
- Emission is a **drained window**, never a live per-producer flag flip; no stripped callback is ever
  sent into an initialized receiver and locally stamped delivered.
- `superseded` (a genuine higher-delivered obsolescence) is distinct from `integrity_mismatch` (a
  non-retryable, alert-raising, 409-on-requeue corruption terminal). At-least-once preserved.
- Delivery still stamps `published_at` + run `COMPLETE`. **No `ENGINE_BUILD_ID`/scoring change** —
  delivery-layer only; the whole-tree drift guard re-pins on the src touch **with no bump**. Golden
  decision *content* byte-identical.
- `KYC_Tool_Build_Package/` + **M2** untouched — strict hardening.

## Testing strategy (each finding carries a mutation witness)

- **Migration 013 (rev-2/3 + F4/F5):** seed both kinds + delivered/pending/**dead** callbacks +
  manual decisions; upgrade; assert mapped/claimable rows, contiguous unique per-case sequences,
  manual NULL, outbox↔decision sequence match, counter = max, backfilled `payload_json`. Direct
  post-migration negatives **fail at commit**: NULL/bad stream; NULL-case or NULL-run decision
  callback; zero/negative sequence; wrong-case or wrong-run same sequence; duplicate decision per run
  (`UNIQUE run_id`); duplicate/missing callback mapping; sequenced email row. Mutation-removing any
  CHECK/FK/UNIQUE/backfill fails. `up→down→up` clean on empty schema.
- **Claim-lease separation (F4):** seed one due+claimed row (`claim_lease_expires_at` set) and one
  unclaimed future-backoff decision **and** email row; run the real recovery CLI; prove **only** the
  claimed row is immediately claimable and the scheduled retries are **unchanged**. Kill after
  HTTP-before-stamp → duplicate-send behavior preserved. Mutation resetting all pending rows fails.
- **Phase reader / silent-loss window (F1):** barrier an old `legacy`/false-flag publisher
  immediately before HTTP; enter `bootstrap_in_progress`; prove **zero HTTP + no delivered/published
  stamps**; hold the external response after the platform commits and prove the system stays
  quiesced; CAS `active` + start only true-flag publishers → the queued callback applies **once**.
  Mutations omitting `dev_worker`, permitting a legacy send in an intermediate phase, or stamping
  local delivery on a receiver no-op must fail.
- **Bootstrap reconciliation (F2):** cover (i) seq2-then-seq1 legacy revert, (ii) manual-current with
  an older accepted callback, (iii) manual-only/no-callback, (iv) send-before-local-stamp — each
  yields one deterministic `h` **or** blocks with a named reconciliation reason. Mutation using the
  tool's latest/local-delivered row as platform truth must fail.
- **Activation state machine + CLIs (F3):** direct SQL for every illegal state tuple
  (`active`/`bootstrapped` without digests/artifacts, reverse transition, out-of-order timestamps)
  fails; the real CLIs with a wrong digest/signature/partial coverage leave row 1 byte-stable;
  commit-platform / lose-response / retry-same-digest converges; retry-different-digest refuses.
  Mutations that only compare digests in a test helper, overwrite an existing epoch, or treat a
  missing row / query error as `legacy` must fail.
- **Integrity terminal + backlog split (F6):** a tuple tamper (case/run/sequence across
  decision/outbox/JSON) → the exact `integrity_mismatch` terminal (zero HTTP, `dead`, `resolved_at`,
  no `published_at`, metric/alert) and UI requeue **409**; a genuine lower row with a higher delivered
  decision → `superseded` only. Backlog proof split: **pending-off → activate → emits**; **dead-off →
  authenticated real UI requeue → pending → emits**. Mutations letting corruption enter ordinary
  retry/requeue or excluding it from alerts must fail.
- **6b convergence (F2/F5):** greatest row `dead` ⇒ false; lower superseded by a delivered higher ⇒
  true for ordering; missing/duplicate mapping ⇒ blocked; coordinator superseded by a higher
  **pre-target** decision ⇒ 6b activation still false.
- **Kept (rev-2/3):** stream separation (stuck email never blocks the case's decision); concurrent
  decides → unique increasing sequences; `superseded` atomic tuple + retention + metrics + requeue-409;
  legacy dead callback requeued through the real UI is superseded and cannot change the fake receiver.
- **Lineage (F6/rev-3):** `tests/unit/test_migration_lineage.py` green after the §C **and** detailed
  renumber; `rg` shows no contradictory live allocation.
- **Gate:** `./manage.sh lint`; the real lineage test; targeted real-Postgres
  migration/outbox/bootstrap/ops/UI tests; then `./manage.sh test`.

## Docs + governance (in the build)

`PLATFORM_INTEGRATION.md` (receiver contract + candidate manifest + reconciliation + HMAC-v2 bootstrap
endpoint), `DEPLOYMENT.md` + `RUNBOOK.md` (two drained windows, orchestrator attestation, the three
CLIs, phase state machine, two-phase rollback + startup-refusal + preflight queries),
`OVERVIEW.md`, **ADR-008**, `AUDIT_FINDINGS.md` (backfill = deterministic reconstruction; the
activation authority + reconciliation semantics), `.agents/ROADMAP.md` (table + detailed sections +
ADR-008 + the added 013 columns, this commit).

## ROADMAP renumber (F6/rev-3, kept; detail updated for rev-4 columns)

§C table, status prose, and detailed sections: PR 7b `013`/down `012` (item 8, **pending** until
authored; 013 now also carries `outbox.{claim_lease_expires_at,failure_class}`, the triple-identity
constraints, and the activation state machine + artifact table), PR 6b `014`, PR 7a `015`; 8=`016`,
10=`017`. Lineage test green; the build flips only PR 7b's State to `shipped`.

## Out of scope (YAGNI)

- No email-semantics change beyond stream separation; no per-decision content change; no cross-case
  ordering. No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched.
- `event_sequence` (D1, shipped PR 2) keeps its `_callback_body` gate (an ordering hint, not the
  authority); only `decision_sequence` moves to publisher-side emission. A future unit may align it.
- PR 7a and PR 6b remain separate units; 7b supplies the ordering primitive, platform contract, and
  activation authority 6b consumes.
