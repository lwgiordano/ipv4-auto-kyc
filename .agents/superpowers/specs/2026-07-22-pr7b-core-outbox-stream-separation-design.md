# PR 7b-core — Outbox stream separation + local decision ordering (item 8, part 1) — design (rev 1)

## Context

PR 7b was split (user decision, 2026-07-22) into **7b-core** (this doc, migration `013`, shippable
hardening) and **7b-activation** (migration `014`, the platform-authoritative cutover — bootstrap,
wire emission, phase state machine). The split isolates the intricate platform-coordination into its
own unit and lets this self-contained hardening land and reach REVIEW-CLEAN on its own. Both remain
ahead of PR 6b; 6b's *activation* still waits on 7b-activation.

The outbox (`src/kyc_tool/outbox/publisher.py`) has two present defects and one latent bug:

1. **Mixed per-case FIFO.** `_CLAIM_SQL` (`publisher.py:29-49`) claims the min-id pending row **per
   `case_id`**, mixing `decision_callback` + `poc_email`, so a stuck POC email (backoff `pending`)
   **blocks that case's decision callbacks**.
2. **Single-replica requeue revert.** A requeued **older** decision callback (lower `id`) can be
   delivered **after** a newer one, reverting the case. Delivery stamps `decisions.published_at` +
   run `COMPLETE` (`_record_delivered`, `publisher.py:160-191`).
3. **Latent: unfenced claim.** The claim only pushes `next_attempt_at` forward (`publisher.py:34`);
   terminal writes are `WHERE id=:id` only (`publisher.py:164,200,215-219`). A publisher whose lease
   expired mid-HTTP can resume and **overwrite** the terminal a reclaiming publisher already wrote
   (e.g. stamp `dead` over a `delivered` row) — falsifying downstream convergence.

7b-core fixes all three with **local** mechanisms (no platform contract): stream separation, a
per-case decision sequence with a **local** `superseded` guard, and a **fenced** claim. It does **not**
make the platform authoritative — the cross-replica race (publisher A sends seq 2, B later sends a
requeued seq 1 in the claim/HTTP/stamp gap) is closed only by the platform high-water in
**7b-activation**. 7b-core is honest about that boundary. This is ROADMAP item 8 (part 1).

## Decisions

1. **Stream separation** — `outbox.ordering_stream` (`decision`|`email`), **NOT NULL**; claim FIFO
   scoped per `(case_id, ordering_stream)` so the two streams drain independently.
2. **Per-case decision sequence** — `decisions.decision_sequence`, allocated from the **locked
   counter** `cases.last_decision_sequence` (never `max()+1`), callback-emitting decisions only. It
   is **internal** in 7b-core (not on the wire — the callback body stays byte-identical); it drives
   the local guard and is the primitive 7b-activation later emits.
3. **Local `superseded` guard** — a decision callback whose case already has a **higher-sequence
   delivered** decision becomes terminal `superseded`, never sent. Prevents the single-replica revert
   (defect 2). Not the cross-replica authority (that is 7b-activation).
4. **Fenced claim** — a per-claim `claim_token` so a stale claimant cannot overwrite the reclaimer's
   terminal (defect 3), with the claim lease separated from the retry schedule.
5. **Relational integrity** — the callback↔decision binding is a real triple FK; outbox status and
   lifecycle tuples are DB CHECKs.
6. **Drained migration** with a **plain reversible** downgrade (the sequence namespace is internal
   until 7b-activation emits it, so nothing external is destroyed by a rollback here).

## Architecture

### 1. Migration 013 (`down_revision='012'`) — drained cutover

**7b-core owns `013`.** Precondition (§Rollout): platform paused, **zero** app writers attested at the
orchestrator level (engines set no `application_name`, `session.py:16-17`); the shipped
`requeue_interrupted_jobs` + a new outbox-lease-reset one-shot already run. One ordinary transaction
(writers stopped, no `CONCURRENTLY`).

- **`outbox.ordering_stream TEXT`** — add nullable → backfill `CASE kind WHEN 'poc_email' THEN
  'email' ELSE 'decision' END` → assert zero NULL/unknown → add+VALIDATE `CHECK (ordering_stream IS
  NOT NULL AND ordering_stream IN ('decision','email'))`. (A DB default can't work — it would
  mislabel emails — hence the drain.)
- **`outbox.{decision_sequence BIGINT NULL, resolved_at TIMESTAMPTZ NULL}`**,
  **`decisions.decision_sequence BIGINT NULL`**,
  **`cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`**.
- **Fenced claim:** `outbox.{claim_lease_expires_at TIMESTAMPTZ NULL, claim_token UUID NULL,
  claimed_by TEXT NULL}`, `CHECK ((claim_token IS NULL) = (claim_lease_expires_at IS NULL))`.
- **Triple identity:** `UNIQUE decisions(run_id)` (multiple NULL manual rows legal),
  `UNIQUE decisions(run_id, case_id, decision_sequence)`, triple FK `outbox(run_id, case_id,
  decision_sequence) → decisions(...)` (no NULL member on a callback row → always enforced; closes a
  NULL-`case_id` bypass), and a partial `UNIQUE outbox(run_id) WHERE kind='decision_callback'`
  (one callback per automatic run — the FK alone cannot enforce this).
- **CHECKs:** `decisions` — `(manual=false → run_id NOT NULL AND decision_sequence > 0) AND
  (manual=true → run_id NULL AND decision_sequence NULL)`. `outbox` — `status IN
  ('pending','delivered','dead','superseded')`; `(kind='decision_callback' → ordering_stream='decision'
  AND case_id NOT NULL AND run_id NOT NULL AND decision_sequence > 0) AND (kind='poc_email' →
  ordering_stream='email' AND run_id NULL AND decision_sequence NULL)`; lifecycle tuples —
  `superseded ⇒ resolved_at NOT NULL AND delivered_at NULL AND claim_token NULL`; `delivered ⇒
  delivered_at NOT NULL AND claim_token NULL`; `pending` may carry a paired claim token/lease.
  (`decisions.published_at` is cross-table → not a plain CHECK; guarded by the fenced transition, §4.)
- **Legacy backfill:** per case `decision_sequence = row_number() OVER (PARTITION BY case_id ORDER BY
  decided_at, id)` for callback decisions (`manual=false AND run_id NOT NULL`); manual NULL; bind
  every existing `decision_callback` outbox row (incl. **dead**) via `run_id`; seed
  `last_decision_sequence = per-case max`. Preflight refusals (raise, roll back): orphan/dup-run
  callback, `>1` callback per run, remaining NULL decision-stream sequence, sequenced email row,
  automatic decision with NULL run / non-positive sequence. `AUDIT_FINDINGS.md`: backfill is a
  deterministic reconstruction (`decided_at,id`), not proof of publication order.
- **Downgrade — plain reversible:** drop the columns/constraints/index (nothing external observes the
  sequence yet — 7b-activation, not core, makes it authoritative). Migration test proves `up→down→up`
  clean. Add `ix_outbox_stream_claim (case_id, ordering_stream, status, next_attempt_at)`.
- ORM in `db/tables.py`.

### 2. Stream-scoped, fenced claim (publisher)

`_CLAIM_SQL` gains `AND o2.ordering_stream = o.ordering_stream` (never UNKNOWN — NOT NULL) so the
eligible row is the min-id pending row of its own `(case_id, ordering_stream)`; requires
`(claim_lease_expires_at IS NULL OR claim_lease_expires_at <= now())`; on claim sets **only** a fresh
`claim_token = gen_random_uuid()` + `claim_lease_expires_at = now()+lease` + `claimed_by`, and
**RETURNs the token** (leaving `next_attempt_at` as the retry due time — so the lease-reset one-shot
can target claimed rows without disturbing backoff schedules). `enqueue_decision_callback` sets
`ordering_stream='decision'` + `decision_sequence`; `enqueue_poc_email` sets `ordering_stream='email'`.

### 3. Decision-sequence allocation (decide transaction)

Seam `pipeline._decide_txn` (`pipeline.py:351-518`) holds the Case `FOR UPDATE` from `_load` (`:201`)
across the `DecisionRow` build (`:485-495`) and enqueue (`:506`). A callback-emitting decision
increments `cases.last_decision_sequence` under the lock (never `max()+1`), stamps
`DecisionRow.decision_sequence`, and passes it to `enqueue_decision_callback(..., decision_sequence=seq)`.
Decision + enqueue stay in the one existing transaction (so `UNIQUE outbox(run_id) WHERE
kind='decision_callback'` enforces at-most-one atomically). Manual approve
(`ingest._handle_manual_approve`, `ingest.py:242-267`, `run_id=None`, no callback) allocates no
sequence. Single writer of the counter, always under the case lock.

### 4. Local `superseded` guard + fenced terminals (delivery)

In `process_once` (`publisher.py:143-158`), after claiming a `decision`-stream row with a non-NULL
`decision_sequence`, **before `_deliver`**: if a higher-sequence decision for the case is already
delivered — `EXISTS(SELECT 1 FROM decisions WHERE case_id=:c AND decision_sequence > :seq AND
published_at IS NOT NULL)` — call `_record_superseded(row, token)` and return without sending.
Otherwise deliver as today (unchanged: stamps `published_at` + run `COMPLETE`; the callback body is
byte-identical to pre-7b).

**All terminal transitions are fenced** (`_record_delivered`/`_record_failure`/`_record_superseded`):
`WHERE id=:id AND status='pending' AND claim_token=:token`, **assert exactly one row**, and clear
`claim_token`/`claim_lease_expires_at`/`claimed_by`. A zero-row stale completion (the claimant's lease
expired, another publisher reclaimed + finished) is an **audited/metric no-op** that never stamps the
run/decision — closing defect 3. `_record_superseded` sets outbox `superseded` + `resolved_at`, leaves
`published_at` NULL (never sent), moves the run through the existing legal `PUBLISH_DECISION→COMPLETE`
edge with `finished_at` (**no** new `RunState`, `models.py:35-46`), and audits with old/superseding
sequence. Retention prunes `superseded` with `delivered` (`retention.py:29-35`); metrics report it
separately and exclude it from pending/dead alerts (`routes_metrics.py:60`); the UI requeue already
409s non-`dead` rows (`ui/routes.py:459`).

### 5. Scope boundary (what 7b-activation adds)

7b-core deliberately stops at **local** ordering. It does **not**: put `decision_sequence` on the
wire; add the platform high-water bootstrap / signed envelope; add the `outbox_ordering_activation`
phase machine or the `integrity_mismatch` wire-tuple terminal. Those live in **7b-activation** (014),
which makes the ordering cross-replica-authoritative. PR 6b's *activation* consumes 7b-activation;
6b may **build** on 7b-core's sequence primitive but not activate until 7b-activation is `active`.

## Rollout / rollback

Digest-pinned drained cutover: (1) pause submission, edge-block composer, disable autoscaling/
restarts; (2) hard-stop API/pipeline/outbox/`dev_worker` (queue **and** outbox, `dev_worker.py:128-139`)
/retention/every writer, attest zero at the orchestrator (no heartbeat; not `pg_stat_activity`);
(3) shipped `python -m kyc_tool.ops.requeue_interrupted_jobs` (decrements attempts so a killed final
attempt retries) + the new **outbox-lease-reset** one-shot (clears only non-NULL `claim_token`/
`claim_lease_expires_at`, read-back-asserts zero, leaves retry schedules); (4) `alembic upgrade head`
(013); (5) start API only, probe `/readyz`, then start+attest workers — **no mutating prod smoke**
(prove on the exact digest in staging/isolated DB); (6) resume. **Rollback** is the same
stop→recover sequence on the schema-compatible image, then `alembic downgrade` (reversible — §1)
— or, simplest, redeploy the pre-7b image only **before** 013 is applied. Because the callback body
is byte-identical and nothing external observes the sequence, a post-013 rollback is safe (unlike
7b-activation, which is forward-only-after-use).

## Invariants

- Claim FIFO per `(case_id, ordering_stream)`; `ordering_stream` NOT NULL everywhere; a stuck stream
  never blocks the other.
- `decision_sequence` allocated only under `Case FOR UPDATE`, unique per case, monotonic; **internal**
  (not emitted). Each callback bound by the triple identity to exactly one automatic decision
  (partial unique index = at-most-one).
- Every terminal transition is **fenced** on `claim_token`; a stale write is a no-op that never stamps
  the run/decision. The claim lease is separate from the retry schedule.
- The local `superseded` guard prevents the **single-replica** revert; the **cross-replica** authority
  is 7b-activation (documented boundary, not silently implied).
- Every outbox status/lifecycle tuple is a DB CHECK. Delivery still stamps `published_at` + run
  `COMPLETE`; the callback body is byte-identical to pre-7b. At-least-once preserved.
- **No `ENGINE_BUILD_ID`/scoring change** — delivery-layer only; the whole-tree drift guard re-pins on
  the src touch **with no bump**. `KYC_Tool_Build_Package/` + **M2** untouched.

## Testing strategy (real Postgres, each with a mutation witness)

- **Migration 013:** seed both kinds + delivered/pending/**dead** callbacks + manual decisions;
  upgrade; assert mapped/claimable rows, contiguous unique per-case sequences, manual NULL,
  outbox↔decision match, counter = max. Direct post-migration negatives **fail at commit**: bad/NULL
  stream; NULL-case/NULL-run callback; zero/negative sequence; wrong-case/wrong-run same sequence;
  **duplicate callback per run**; sequenced email; unpaired claim token/lease; illegal status tuple
  (`superseded`+NULL `resolved_at`, `delivered`+claim token). `up→down→up` clean. Mutation-removing any
  CHECK/index/FK fails.
- **Stream separation:** a perpetually-failing POC email (stuck `pending`) never blocks the same
  case's decision callback — the decision delivers while the email retries.
- **Sequence allocation:** two concurrent decides on one case (real barrier) → strictly increasing,
  unique sequences; the partial unique index backstops a forced duplicate.
- **Local superseded guard:** seq 2 delivered; requeue seq 1 → claimed → `superseded`, never sent;
  atomic tuple (outbox `superseded`+`resolved_at`, run `COMPLETE`, decision `published_at` NULL,
  audit); normal seq 1→2→3 all delivered in order; retention + metrics + UI-409.
- **Fenced claim (defect 3):** barrier publisher A after claim; expire its lease; B reclaims +
  delivers; release A into **both** failure and success paths → B's terminal tuple, run state, and
  `published_at` are **unchanged**; A affects zero rows (audited no-op). Mutation removing the
  `claim_token` predicate must fail.
- **Lineage:** `tests/unit/test_migration_lineage.py` green after the §C table + detail split; `rg`
  shows no contradictory live allocation.
- **Gate:** `./manage.sh lint` (ruff + import-linter 2/0); the real lineage test; the targeted
  real-Postgres tests above; then `./manage.sh test`.

## Docs + governance (in the build)

`OVERVIEW.md` (stream separation + local ordering + the 7b-core/activation boundary),
`RUNBOOK.md`/`DEPLOYMENT.md` (drained cutover + lease-reset one-shot), `AUDIT_FINDINGS.md` (backfill =
deterministic reconstruction; the local-guard/cross-replica boundary), `.agents/ROADMAP.md` (the
7b-core/activation split + renumber, this commit). No ADR needed for core (no cross-service contract);
ADR-008 covers 7b-activation.

## Out of scope (YAGNI)

- No wire `decision_sequence`, no platform contract, no bootstrap/phase machine, no
  `integrity_mismatch` terminal (all 7b-activation). No email-semantics change beyond stream
  separation. No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched. PR 7a fences the **jobs**
  queue separately; 7b-core fences its own **outbox** claim.
