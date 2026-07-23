# PR 7b-core — Outbox stream separation + local decision ordering (item 8, part 1) — design (rev 4)

## Context

PR 7b was split (user decision, 2026-07-22) into **7b-core** (this doc, migration `013`, shippable
hardening) and **7b-activation** (migration `014`, the platform-authoritative cutover — bootstrap,
wire emission, phase state machine). The split isolates the intricate platform-coordination into its
own unit and lets this self-contained hardening land and reach REVIEW-CLEAN on its own. Both remain
ahead of PR 6b; 6b's *activation* still waits on 7b-activation.

The outbox (`src/kyc_tool/outbox/publisher.py`) has two present defects and one latent bug:

1. **Mixed per-case FIFO.** `_CLAIM_SQL` (`publisher.py:29-49`) claims the min-id pending row **per
   `case_id`**, mixing `decision_callback` + `poc_email`, so a stuck POC email (backoff `pending`)
   **blocks that case's decision callbacks**. → 7b-core **closes** this.
2. **Requeue revert.** A requeued **older** decision callback (lower `id`) can be delivered **after**
   a newer one, reverting the case. Delivery stamps `decisions.published_at` + run `COMPLETE`
   (`_record_delivered`, `publisher.py:160-191`). → 7b-core **mitigates** this with a **best-effort
   local guard** that fires only when the higher delivery was *successfully stamped locally*; the
   **send-before-stamp** and **cross-replica** cases are **not** closed here (see §4/§5) — they close
   only under 7b-activation's platform authority. 7b-core does not claim to close defect 2 fully.
3. **Latent: unfenced claim.** The claim only pushes `next_attempt_at` forward (`publisher.py:34`);
   terminal writes are `WHERE id=:id` only (`publisher.py:164,200,215-219`). A publisher whose lease
   expired mid-HTTP can resume and **overwrite** the terminal a reclaiming publisher already wrote
   (e.g. stamp `dead` over `delivered`). → 7b-core **closes** this with a per-claim fence.

7b-core fixes these with **local** mechanisms only — no platform contract. It is honest about its
boundary: the guard is a **best-effort local supersession optimization**, not an authority. This is
ROADMAP item 8 (part 1).

## Decisions

1. **Stream separation** — `outbox.ordering_stream` (`decision`|`email`), **NOT NULL**; claim FIFO
   scoped per `(case_id, ordering_stream)`.
2. **Per-case decision sequence** — `decisions.decision_sequence` from the **locked counter**
   `cases.last_decision_sequence` (never `max()+1`), callback-emitting decisions only. **Internal**
   (not on the wire — callback body byte-identical); drives the local guard; the primitive
   7b-activation later emits.
3. **Best-effort local `superseded` guard** — an older requeued callback is suppressed **only when a
   higher-sequence decision has a locally-stamped `published_at`**. Reduces the common single-replica
   revert; does **not** cover send-before-stamp or cross-replica (7b-activation).
4. **Fenced claim** — a per-claim `claim_token` so a stale claimant cannot overwrite the reclaimer's
   terminal, with the claim lease separated from the retry schedule.
5. **Exhaustive relational integrity** — closed `kind` vocabulary, `case_id NOT NULL`, a triple FK for
   the callback↔decision binding, and **per-status XOR** lifecycle CHECKs (not one-way fragments).
6. **Reversible-before-first-supersession** downgrade (a live `superseded` row a pre-7b image cannot
   interpret makes a blind rollback unsafe — the down migration preflight-refuses once any exists).

## Architecture

### 1. Migration 013 (`down_revision='012'`) — drained cutover

**7b-core owns `013`.** Precondition (§Rollout): platform paused, **zero** app writers attested at the
orchestrator level (engines set no `application_name`, `session.py:16-17`); the shipped
`requeue_interrupted_jobs` already run. One ordinary transaction (writers stopped, no `CONCURRENTLY`).

- **Closed kind + NOT NULL case (F5):** `CHECK kind IN ('decision_callback','poc_email')` (a
  `kind='unknown'` row otherwise passes the implication CHECKs and later hits `_deliver`'s
  "unknown outbox kind" raise, `publisher.py:138-139`). Both shipped enqueue APIs require a case
  (`publisher.py:52-63`), so assert/backfill **zero** NULL/orphan `case_id`, then `outbox.case_id NOT
  NULL` + FK `outbox.case_id → cases(id)`. (The callback triple FK below is the stronger callback
  binding; this is the baseline for email rows and removes the `case_id IS NULL` claim bypass.)
- **`outbox.ordering_stream TEXT`** — add nullable → backfill with **two explicit branches**
  `CASE WHEN kind='poc_email' THEN 'email' WHEN kind='decision_callback' THEN 'decision' ELSE NULL
  END` → **preflight-refuse** any NULL/unknown → **`ALTER COLUMN ordering_stream SET NOT NULL`** (the
  real column attribute, so `information_schema.columns.is_nullable='NO'` — a bare
  `CHECK (... IS NOT NULL)` rejects NULL *values* but leaves the metadata `YES`, contradicting the
  ORM/schema contract and tripping schema-diff drift; F4) + a **separate permanent vocabulary CHECK**
  `ordering_stream IN ('decision','email')`. (The drain means the plain `SET NOT NULL` scan is fine; a
  DB default can't work — it would mislabel emails.) The ORM field is **non-optional**.
- **`outbox.{decision_sequence BIGINT NULL, resolved_at TIMESTAMPTZ NULL}`**,
  **`decisions.decision_sequence BIGINT NULL`**,
  **`cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`**.
- **Fenced claim:** `outbox.{claim_lease_expires_at TIMESTAMPTZ NULL, claim_token UUID NULL,
  claimed_by TEXT NULL}`, claim tuple treated **all-three-together** (below).
- **Triple identity + per-case sequence uniqueness (F1):** four constraints, each load-bearing —
  `UNIQUE decisions(run_id)` (one automatic decision per run; multiple NULL manual rows legal);
  **`UNIQUE decisions(case_id, decision_sequence)` named `uq_decisions_case_decision_sequence`** — the
  actual per-case ordering invariant (a plain PG UNIQUE; manual rows keep `decision_sequence=NULL`, so
  multiple NULLs stay legal). *Without this the namespace is not unique:* since `run_id` is already
  unique, `UNIQUE(run_id, case_id, decision_sequence)` adds **no** per-case uniqueness — `(run=A,
  case=C, seq=1)` and `(run=B, case=C, seq=1)` satisfy it, and after 014 the second to reach the
  platform is a high-water no-op that **silently discards a distinct decision**. Keep also
  `UNIQUE decisions(run_id, case_id, decision_sequence)` (the exact **triple-FK target**), the triple
  FK `outbox(run_id, case_id, decision_sequence) → decisions(...)`, and the partial `UNIQUE
  outbox(run_id) WHERE kind='decision_callback'` (one callback per run). In 013, **after the
  deterministic backfill and before creating `uq_decisions_case_decision_sequence`**, preflight
  `SELECT case_id, decision_sequence FROM decisions WHERE decision_sequence IS NOT NULL GROUP BY 1,2
  HAVING count(*) > 1` and **refuse with actionable identifiers** if any survive. CHECK on `decisions`:
  `(manual=false → run_id NOT NULL AND decision_sequence > 0) AND (manual=true → run_id NULL AND
  decision_sequence NULL)`.
- **Exhaustive per-status XOR lifecycle CHECK (F4)** — not one-way fragments. With
  `claim := (claim_token, claim_lease_expires_at, claimed_by)`:
  - `status='pending'` ⇒ `delivered_at NULL AND resolved_at NULL` AND `claim` is **all-NULL or
    all-non-NULL** (a claimed pending row).
  - `status='delivered'` ⇒ `delivered_at NOT NULL AND resolved_at NULL` AND `claim` all-NULL.
  - `status='dead'` ⇒ `delivered_at NULL AND resolved_at NULL` AND `claim` all-NULL.
  - `status='superseded'` ⇒ `delivered_at NULL AND resolved_at NOT NULL` AND `claim` all-NULL.
  - `status IN ('pending','delivered','dead','superseded')`.
  (7b-activation may replace the `dead` branch when it adds `integrity_mismatch`/`failure_class`.)
- **`kind`/`stream`/identity as an exhaustive OR of the two complete row shapes** (not implications):
  `(kind='decision_callback' AND ordering_stream='decision' AND run_id NOT NULL AND decision_sequence
  > 0) OR (kind='poc_email' AND ordering_stream='email' AND run_id NULL AND decision_sequence NULL)`.
- **Legacy backfill — ordered by `outbox.id`, not `decided_at` (rev-4 F1):** `decided_at` is
  `server_default now()`, and Postgres `now()` is the **transaction-start** time; `_decide_txn` opens
  its transaction (`pipeline.py:361`) **before** acquiring the case `FOR UPDATE` (`_load`, `:362`), so
  two same-case decides can carry `decided_at` in the **opposite** order from their lock-serialized
  commit order — ordering the backfill by `decided_at` would then assign the *newer* callback the
  *lower* sequence, and the local guard would suppress the actually-newer callback (the exact loss this
  unit reduces). `outbox.id` is allocated at enqueue (`:506`) **under** that lock, so it preserves the
  true same-case serialization. Recipe: (1) build a **one-to-one** map from every `manual=false`
  decision to exactly one surviving `decision_callback` outbox row by `run_id`, **refusing with the
  decision/run ids** on any missing, orphan, or duplicate mapping (the decide path writes decision +
  callback in one transaction, so a missing row means retention/corruption and its safe order **cannot
  be guessed** — fail closed; platform-authoritative reconstruction is 7b-activation's job, never a
  `decided_at` fallback); (2) `decision_sequence = row_number() OVER (PARTITION BY decision.case_id
  ORDER BY outbox.id)`, copy it to the matching outbox row, seed `last_decision_sequence = per-case
  max`; manual stays NULL. Other preflight refusals (raise, roll back): `>1` callback per run,
  sequenced email row, automatic decision with NULL run / non-positive sequence. A **defensive**
  post-backfill `GROUP BY case_id, decision_sequence HAVING count(*)>1` assertion (`row_number` cannot
  produce a per-case duplicate, so it always holds). `AUDIT_FINDINGS.md`: order authority is
  `outbox.id` (the under-lock serialization for rows the guard can deliver), a deterministic
  reconstruction, **not** proof of original publication order; a legacy automatic decision without a
  surviving callback is a fail-closed migration refusal.
- **Downgrade — reversible before first local supersession, race-safe (F3 + rev-2 F2):** the **first
  statement** in `downgrade()` is `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE` — a bare
  `EXISTS(status='superseded')` preflight is a TOCTOU race (its `ACCESS SHARE` is compatible with a
  publisher's `ROW EXCLUSIVE`, so a still-running/auto-restarted publisher can commit a `superseded`
  row *between* the preflight and the DDL, stranding the exact unknown terminal the refusal prevents).
  **Under that lock**, in one transaction: run the `EXISTS(status='superseded')` preflight and **refuse
  byte-stably** if any exists (dropping 013's columns would leave a pre-7b image an unknown, unprunable
  terminal — old retention deletes only `delivered` `retention.py:29-35`, old claim takes only
  `pending`, old UI requeues only `dead` `ui/routes.py:451-475`); otherwise drop the
  columns/constraints. Tests: `up→down→up` on a no-supersession DB; the seeded-`superseded`
  byte-stable refusal; **and a two-connection race** — hold the downgrade txn after its table lock,
  attempt a concurrent `pending→superseded`, prove it cannot slip between preflight and DDL (mutation:
  removing/moving the `LOCK TABLE` after the preflight reproduces the stranded-status race).
- Add `ix_outbox_stream_claim (case_id, ordering_stream, status, next_attempt_at)`. ORM in `db/tables.py`.

### 2. Stream-scoped, fenced claim (publisher)

`_CLAIM_SQL` gains `AND o2.ordering_stream = o.ordering_stream` (never UNKNOWN — NOT NULL) so the
eligible row is the min-id pending row of its own `(case_id, ordering_stream)`. With `case_id NOT
NULL` (F5) the old `o.case_id IS NULL` bypass branch is removed — every row is scoped per
`(case, stream)`. The claim requires `(claim_token IS NULL OR claim_lease_expires_at <= now())` and
sets **only** a fresh `claim_token = gen_random_uuid()` + `claim_lease_expires_at = now()+lease` +
`claimed_by`, **RETURNing the token** (leaving `next_attempt_at` as the retry due time).
`enqueue_decision_callback` sets `ordering_stream='decision'` + `decision_sequence`;
`enqueue_poc_email` sets `ordering_stream='email'`.

### 3. Decision-sequence allocation (decide transaction)

Seam `pipeline._decide_txn` (`pipeline.py:351-518`) holds the Case `FOR UPDATE` from `_load` (`:201`)
across the `DecisionRow` build (`:485-495`) and enqueue (`:506`). A callback-emitting decision
increments `cases.last_decision_sequence` under the lock (never `max()+1`), stamps
`DecisionRow.decision_sequence`, and passes it to `enqueue_decision_callback(..., decision_sequence=seq)`.
Decision + enqueue stay in the one existing transaction (partial unique index enforces at-most-one
atomically). Manual approve (`ingest._handle_manual_approve`, `ingest.py:242-267`, `run_id=None`, no
callback) allocates no sequence. Single writer of the counter, always under the case lock.

### 4. Best-effort local guard + fenced terminals (delivery)

In `process_once` (`publisher.py:143-158`), after claiming a `decision`-stream row with a non-NULL
`decision_sequence`, **before `_deliver`**: if a higher-sequence decision for the case has a
**locally-stamped** `published_at` — `EXISTS(SELECT 1 FROM decisions WHERE case_id=:c AND
decision_sequence > :seq AND published_at IS NOT NULL)` — call `_record_superseded(row, token)` and
return without sending. **This is a best-effort optimization, not an authority:** if the higher
callback was sent but its `_record_delivered` had not committed (send-before-stamp), or a concurrent
replica sent it, the predicate is false and the older callback **is** sent — a revert that remains
**expected** until 7b-activation's platform high-water (§5). 7b-core does not close that gap and must
not be described as if it does.

**The fenced outbox UPDATE is the single ownership gate** in every terminal *and the retry* path
(`_record_delivered`, `_record_failure`'s **retry and dead** branches, `_record_superseded`). Each
begins with `UPDATE outbox SET <status/claim-clear/...> WHERE id=:id AND status='pending' AND
claim_token=:token RETURNING id` and branches on an explicit result (`applied: bool` from the returned
row / `rowcount`) — **never** an assertion (a raise would escape `process_once`, and `_record_failure`
already runs inside the delivery exception handler, so a normal lease-loss race must not kill the
worker). The dependent writes are gated on `applied`, because the shipped code performs them
**after** the outbox UPDATE, keyed by `id`/`run_id` (`publisher.py:167-190` POC redaction + run +
`decisions.published_at`), so fencing only the first statement does not protect them:
- **Winner (`applied=true`)** — in the *same* transaction: redact a POC payload, update `runs`, stamp
  `decisions.published_at`, write the normal terminal audit; `_record_superseded` additionally set
  `resolved_at`, leave `published_at` NULL (never sent), move the run through the existing legal
  `PUBLISH_DECISION→COMPLETE` edge with `finished_at` (**no** new `RunState`, `models.py:35-46`), and
  audit old/superseding sequence.
- **Stale loser (`applied=false`)** — its lease expired and another publisher (token B) reclaimed +
  finished: perform **none** of those writes (no POC redaction, no run/decision stamp, no retry-clock
  reset), emit only a structured `outbox_stale_claim_completion` metric/audit (outbox id + attempted
  transition; **no** payload or token), and return a non-raising no-op to `process_once`. Closing
  defect 3 — a stale A can neither dead-letter/redact B's POC row nor stamp B's run/decision.

Retention prunes `superseded` with `delivered` (`retention.py:29-35`); metrics report it separately and
exclude it from pending/dead alerts (`routes_metrics.py:60`); the UI requeue already 409s non-`dead`
rows (`ui/routes.py:459`).

### 5. Scope boundary (what 7b-activation adds) — and the honest residual risk

7b-core stops at **local** ordering. It does **not**: put `decision_sequence` on the wire; add the
platform high-water bootstrap / signed envelope; add the phase machine or the `integrity_mismatch`
terminal. **Residual reverts that remain until 7b-activation:** (a) **send-before-stamp** — a higher
callback accepted by the platform but not yet locally `published_at`; (b) **cross-replica** — the
claim/HTTP/stamp gap across publishers. Both are documented as expected pre-activation and are turned
into platform-high-water no-ops only in 014. PR 6b may **build** on 7b-core's sequence primitive but
not activate until 7b-activation is `active`.

## AUDIT:A6 amendment (F6)

`AUDIT:A6` (`AUDIT_FINDINGS.md:61-66`) resolves callback delivery as **at-least-once** with platform
dedupe. `superseded` deliberately sends an enqueued row **zero** times, so A6 gains an explicit
exception (amended in the build): *eligible non-superseded callbacks remain at-least-once and
platform-deduped; a callback proven obsolete by a higher **locally-stamped** delivery is terminally
suppressed, audited, and retained under the governed `superseded` lifecycle.* This is **not**
exactly-once and **not** platform-authoritative — it is a best-effort local suppression. The
publisher/module docstrings state the same.

## Rollout / rollback

Digest-pinned drained cutover: (1) pause submission, edge-block composer, disable autoscaling/
restarts; (2) hard-stop API/pipeline/outbox/`dev_worker` (queue **and** outbox, `dev_worker.py:128-139`)
/retention/every writer, attest zero at the orchestrator; (3) shipped `python -m
kyc_tool.ops.requeue_interrupted_jobs` (decrements attempts so a killed final attempt retries) —
**no outbox reset here** (F1): the pre-013 schema has no claim columns, and an old in-flight outbox
claim is encoded only in `next_attempt_at` and cannot be distinguished from legitimate backoff.
**Preserve every pending row's `next_attempt_at`**; an interrupted old claim simply waits until its
already-recorded due time (bounded by the documented old lease); (4) `alembic upgrade head` (013);
(5) start API only, probe `/readyz`, then start+attest the fenced workers — **no mutating prod smoke**;
(6) resume. A precisely named `python -m kyc_tool.ops.reset_interrupted_outbox_claims` is shipped for
**post-013 future stops and the post-013 rollback path only**: it **refuses pre-013 schema**, updates
only rows with the complete claim tuple, clears the tuple, **preserves `next_attempt_at`**, and
read-back-asserts zero claims.

**Rollback — a full ordered maintenance procedure (rev-2 F2), not a bare downgrade** (rollback is as
drained as the forward cutover; the `LOCK TABLE` in §1 is defense-in-depth, not a substitute): (1)
pause submissions, disable autoscaling/restarts; (2) hard-stop and orchestrator-attest **zero** API,
pipeline, outbox, `dev_worker`, retention, and every writer; (3) **while 013 still exists**, run
`reset_interrupted_outbox_claims` and verify zero claim tuples; (4) run `alembic downgrade` (its
`LOCK TABLE` + preflight refuses byte-stably if any `superseded` row exists — then rollback stays on a
7b-core-compatible image and is a forward fix); (5) deploy the pre-7b image **only after** the
downgrade succeeds. Redeploying the pre-7b image *before* 013 is applied is also safe. (7b-activation
is separately forward-only-after-use.) Mirror this exact order in `DEPLOYMENT.md`/`RUNBOOK.md` — do not
rely on the operator remembering that the forward drain also applies backward.

## Invariants

- Claim FIFO per `(case_id, ordering_stream)`; `ordering_stream` + `case_id` NOT NULL everywhere; a
  stuck stream never blocks the other.
- `decision_sequence` allocated only under `Case FOR UPDATE`, unique per case, monotonic; **internal**
  (not emitted). Each callback bound by the triple identity to exactly one automatic decision.
- Every **retry and terminal** transition uses one fenced `UPDATE ... WHERE id AND status='pending'
  AND claim_token=:token RETURNING id`: **one row** applies all dependent writes in the same
  transaction (retry-clock reset, POC redaction, run completion, `decisions.published_at`, terminal
  audit); **zero rows** returns a non-raising audited no-op (`outbox_stale_claim_completion`) and
  stamps nothing — **never** an assertion (it would kill the worker). The claim lease is separate from
  the retry schedule.
- The local `superseded` guard is a **best-effort optimization** — it fires only on a higher
  **locally-stamped** delivery; send-before-stamp and cross-replica reverts remain until 7b-activation.
- Every outbox status/lifecycle tuple is an **exhaustive per-status** DB CHECK. Delivery still stamps
  `published_at` + run `COMPLETE`; the callback body is byte-identical to pre-7b. At-least-once holds
  for eligible non-superseded rows; `superseded` is a documented A6 exception (best-effort local
  suppression, zero sends).
- **No `ENGINE_BUILD_ID`/scoring change** — delivery-layer only; drift guard re-pins on the src touch
  **with no bump**. `KYC_Tool_Build_Package/` + **M2** untouched.

## Testing strategy (real Postgres, each with a mutation witness)

- **Migration 013:** seed both kinds + delivered/pending/**dead** callbacks + manual decisions;
  upgrade; assert sequences/counter/binding **and `information_schema.columns.is_nullable='NO'` for
  `outbox.ordering_stream`** (F4 — real column attribute, not only value rejection). Direct **INSERT
  and UPDATE** negatives **fail at commit** for every cross-product: bad/NULL/unknown `kind`;
  NULL/orphan `case_id`; NULL `ordering_stream`; NULL-run callback; zero/negative sequence;
  wrong-case/wrong-run same sequence; **two runs same `(case_id, decision_sequence)`**; **duplicate
  callback per run**; sequenced email; and every illegal status tuple (`pending`+`delivered_at`,
  `pending`+`resolved_at`, `dead`+claim tuple, orphan `claimed_by`, `superseded`+NULL `resolved_at`,
  `delivered`+claim). `up→down→up` clean on a no-supersession DB; **downgrade refuses byte-stably with a
  seeded `superseded` row** (F3). (No "seeded pre-existing per-case duplicate" fixture — a 012 DB has
  no `decision_sequence` column and 013's own `row_number()` creates the values; the per-case UNIQUE is
  proven by the runtime INSERT/UPDATE negatives above, not a migration fixture.) Mutation-removing any
  CHECK/index/FK/`SET NOT NULL` fails.
- **Backfill order authority (rev-4 F1) — two connections, real Postgres:** start B's decide
  transaction (fixing `B.decided_at`) and pause it **before** the case lock; start A later, lock the
  case, insert decision + callback, commit; release B to lock, insert, commit. Assert `B.decided_at <
  A.decided_at` **while** `A.outbox_id < B.outbox_id`; upgrade 013; assert **A=seq 1, B=seq 2,
  counter=2**, and delivering both leaves **B** as the platform's last callback with **neither**
  superseded. Mutation: ordering the backfill by `decided_at,id` reproduces B's erroneous suppression
  and **must fail**. Separately, seed a valid `manual=false` decision with **no** surviving callback
  row on 012 and prove the migration **refuses byte-stably** (no `decided_at` fallback).
- **Stream separation:** a perpetually-failing POC email never blocks the case's decision callback.
- **Sequence allocation + per-case uniqueness (F1):** two concurrent decides on one case → strictly
  increasing unique sequences, counter=max. Direct **INSERT and UPDATE** negatives: **two distinct
  valid runs/decisions for the same `case_id` at the same positive `decision_sequence` fail at commit**
  (backstopped by `uq_decisions_case_decision_sequence`, **not** the outbox partial index), while
  multiple manual NULL-sequence decisions stay legal; build matching valid outbox rows so the duplicate
  cannot hide behind another constraint. Mutation removing **only** `uq_decisions_case_decision_sequence`
  must make this fail. Migration 013 preflight refuses a seeded pre-existing duplicate.
- **Local guard (bounded claim):** seq 2 delivered **and stamped**; requeue seq 1 → `superseded`,
  never sent (atomic tuple + retention + metrics + UI-409); normal seq 1→2→3 all delivered in order.
- **Residual-risk (F2 — pins the honest boundary):** fake receiver; single publisher; seq 2 sent,
  fault injected **after HTTP 2xx but before `_record_delivered` commits**; restart, requeue seq 1 →
  the guard's `published_at` predicate is false and seq 1 **is** sent — assert the revert is
  **expected pre-activation** (the future 014 test turns the same replay into a high-water no-op).
- **Fenced claim (defect 3) — both kinds, winner/loser (rev-2 F1):** barrier publisher A after claim;
  expire its lease; B reclaims. **Decision:** B delivers; release A into **both** success and
  stale-final-attempt failure paths → B's outbox tuple, run state, and `published_at` are unchanged and
  `process_once` stays alive (A emits only `outbox_stale_claim_completion`). **POC email:** release A
  **before** B sends → A must **not** redact the payload (byte-for-byte intact) or dead-letter it, and
  B can still send; repeat with A's stale final-attempt failure. Mutation witnesses — (a) removing the
  loser early-return and (b) making zero rows **raise** — must each **fail**.
- **Downgrade race (rev-2 F2):** two connections — hold the `alembic downgrade` transaction after its
  `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE`, attempt a concurrent `pending→superseded` transition,
  and prove it **cannot** slip between the preflight and the DDL; mutation-removing/moving the
  `LOCK TABLE` after the preflight reproduces the stranded-status race.
- **A6 contract (F6):** the higher callback attempted ≥1 HTTP; the superseded lower callback got
  **zero** HTTP + the required audit/terminal evidence.
- **Rollout order (F1):** on 012, seed one old claimed-looking future row + one real backoff row;
  execute the actual cutover order; prove **neither `next_attempt_at` is rewritten**; the reset CLI
  **refuses** on 012; then on 013 claim rows and prove the CLI clears **only** claimed rows.
- **Lineage:** `tests/unit/test_migration_lineage.py` green after the split renumber; `rg` shows no
  contradictory live allocation.
- **Gate:** `./manage.sh lint` (ruff + import-linter 2/0); the real lineage test; the targeted
  real-Postgres tests above; then `./manage.sh test`.

## Docs + governance (in the build)

`OVERVIEW.md` (stream separation + local ordering + the 7b-core/activation boundary; the guard is
best-effort), `RUNBOOK.md`/`DEPLOYMENT.md` (drained cutover **without** a pre-013 reset; the post-013
`reset_interrupted_outbox_claims` CLI; reversible-before-first-supersession rollback + the preflight
query), `AUDIT_FINDINGS.md` (the **A6 exception** + backfill = deterministic reconstruction + the
local/cross-replica boundary), `.agents/ROADMAP.md` (the split + renumber, this commit; "reversible"
→ "reversible before first local supersession"). No ADR needed for core; ADR-008 covers 7b-activation.

## Out of scope (YAGNI)

- No wire `decision_sequence`, no platform contract, no bootstrap/phase machine, no
  `integrity_mismatch` terminal (all 7b-activation). No email-semantics change beyond stream
  separation. No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched. PR 7a fences the **jobs**
  queue separately; 7b-core fences its own **outbox** claim.
