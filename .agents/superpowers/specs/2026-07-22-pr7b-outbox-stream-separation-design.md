# PR 7b — Outbox stream separation + decision ordering (item 8) — design

## Context

PR 7b is reordered **before PR 6b** (revalidation) because it supplies the callback-ordering
guarantee 6b's coordinator callbacks must rest on — without it, 6b needs a throwaway
maintenance fence (Codex PR-6b rev-5 F3). The tool's transactional outbox
(`src/kyc_tool/outbox/publisher.py`) has two holes:

1. **One mixed per-case FIFO.** `_CLAIM_SQL` claims the **min-id pending** row **per
   `case_id`**, mixing `decision_callback` and `poc_email` rows. A POC email that keeps
   failing stays `pending` on backoff as the case's min-id row, so it **blocks that case's
   decision callbacks** behind it (a decision cannot be delivered while an older email of the
   same case is stuck).
2. **No decision ordering / revert guard.** Decision callbacks are ordered only by outbox
   `id`. A **requeued older** decision (e.g. a retried or 6b-revalidation-obsoleted row, lower
   `id`) can be re-claimed and delivered **after** a newer decision already delivered,
   reverting the platform to the stale decision. Delivery already stamps
   `decisions.published_at` and moves the run to `COMPLETE` (`_record_delivered`,
   publisher.py:160-190) — 6b's activation gate reads this — but nothing prevents the revert.

PR 7b separates the two streams and adds a monotonic per-case decision sequence with a
delivery-time high-water mark. This is `AGENTS.md`/`.agents/ROADMAP.md` item 8 (PR 7b).

## Decisions

1. **Stream separation** — `outbox.ordering_stream` (`decision`|`email`); the claim FIFO is
   scoped per **`(case_id, ordering_stream)`** so the two streams drain independently.
2. **Per-case decision sequence** — `decisions.decision_sequence`, allocated from the **locked
   case counter** (`cases.last_decision_sequence`, never `max()+1`), only for
   callback-emitting decisions; `UNIQUE(case_id, decision_sequence)`.
3. **Revert guard** — a decision callback is delivered only if no **higher-sequence** decision
   for the case has already been delivered; otherwise the outbox row becomes terminal
   `superseded` and is never sent. Normal progression still delivers in order; the guard fires
   only on an out-of-order requeue.
4. **Derived high-water** (not a second column); **emails** get only stream separation.

## Architecture

### 1. Migration 013 (`down_revision='012'`)

(7b now owns `013` — reordered ahead of 6b. The build's first task renumbers `.agents/ROADMAP.md`
§C — PR 7b→`013`, PR 6b→`014`, PR 7a→`015` — and updates the lineage-guard reservations in the
same commit.)

- **`outbox.ordering_stream TEXT`** — added nullable, **backfilled** in-migration
  (`UPDATE outbox SET ordering_stream = CASE kind WHEN 'poc_email' THEN 'email' ELSE
  'decision' END`), then a `CHECK (ordering_stream IN ('decision','email'))` added `NOT VALID`
  (hot-compat on the append-only `outbox`; enqueue paths always set it) and a follow-on
  `VALIDATE` — **but** 7b owns only `013`; since the backfill makes every existing row valid
  and the enqueue paths set the column, the CHECK is added `NOT VALID` and left so (validating
  all-populated rows is a no-op we do not spend a revision on), mirroring PR 6b's precedent.
- **`decisions.decision_sequence BIGINT NULL`** (NULL = pre-7b / non-callback decision).
- **`cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`** — the locked allocation counter.
- **Partial unique index** `uq_decisions_case_sequence ON decisions(case_id, decision_sequence)
  WHERE decision_sequence IS NOT NULL` — pre-7b NULL rows don't collide. Built
  `CREATE UNIQUE INDEX CONCURRENTLY` via an autocommit `op.execute` block (outside the
  migration's transaction) so it does not lock writes on a large `decisions` table; the drained
  cutover (below) is the fallback if concurrent build is unavailable.
- **`outbox` decision rows carry the sequence**: `outbox.decision_sequence BIGINT NULL` (set on
  enqueue for `decision` stream; NULL for emails/pre-7b) so the publisher can guard without a
  join.
- ORM (`db/tables.py`): the four columns. Forward-only-after-use downgrade is unnecessary here
  (no immutable-provenance rows are destroyed) — a plain reversible downgrade dropping the
  columns/index is fine, matching the outbox's non-audit nature; the migration test proves
  up/down/up clean.

### 2. Stream-scoped claim (publisher)

`_CLAIM_SQL`'s inner correlated subquery gains `AND o2.ordering_stream = o.ordering_stream`, so
the eligible row is the **min-id pending row of its own `(case_id, ordering_stream)`**. Emails
and decisions of the same case become independent FIFOs — a stuck email no longer heads the
decision stream. `enqueue_decision_callback` sets `ordering_stream='decision'` +
`decision_sequence`; `enqueue_poc_email` sets `ordering_stream='email'`. (`case_id IS NULL`
rows, if any, keep the global path.)

### 3. Decision-sequence allocation (decide transaction)

In the fused decide path (`pipeline._decide_txn`, already holding `Case FOR UPDATE`), a
**callback-emitting** decision:
- increments `cases.last_decision_sequence` (the locked counter — race-free under the case
  lock; never `max()+1`), and
- stamps that value on `DecisionRow.decision_sequence` and passes it to
  `enqueue_decision_callback(..., decision_sequence=seq)` which records it on the outbox row.

Manual approve writes no decision callback (AUDIT C3) → **no** sequence (stays NULL). The
allocation is the single writer of `last_decision_sequence`, always under the case lock.

### 4. Revert guard (delivery)

In `process_once`, after claiming a `decision`-stream row that has a non-NULL
`decision_sequence`, **before `_deliver`**: if a higher-sequence decision for the case has
already been delivered —
`EXISTS(SELECT 1 FROM decisions WHERE case_id=:c AND decision_sequence > :seq AND published_at
IS NOT NULL)` — mark the outbox row **`status='superseded'`** (a new terminal status, alongside
`delivered`/`dead`) and return without sending. Otherwise deliver as today (which stamps
`published_at` + run `COMPLETE`). A NULL-sequence row (pre-7b / email) is delivered with no
guard. So a requeued older decision after a newer delivery is **never** sent → the platform
never reverts. `superseded` rows count as resolved for health/metrics (neither pending nor
dead).

### 5. Interaction with PR 6b (why this unblocks it)

6b's revalidation coordinator emits a **new** (higher-sequence) decision callback. If an older
callback for the case is later requeued, this guard supersedes it. 6b's activation "delivery
convergence" then reduces to: the latest per-case decision is delivered (`published_at`) and no
`pending` decision row outranks it — no maintenance fence, no public-write pause.

## Backward compatibility / rollout

- Pre-7b decisions have NULL `decision_sequence`; the guard doesn't apply to them (they are
  historical, already delivered). New decisions are sequenced from `last_decision_sequence`
  (starts 0 → first is 1).
- **Rolling-safe:** an old replica during a rolling deploy writes NULL sequence + defaults
  `ordering_stream` (backfilled/`decision`); the guard simply doesn't fire for its rows. Full
  ordering applies once all replicas are the new image. (A brief drained cutover is available if
  the concurrent index build is not; 013 is otherwise hot-compatible.)
- `KYC_Tool_Build_Package/` + M2 untouched; no flag needed (this is a strict hardening — the
  new ordering is always on, and it only *adds* a terminal `superseded` outcome).

## Invariants (Global Constraints + review rubric)

- The claim FIFO is per `(case_id, ordering_stream)`; a stuck row in one stream never blocks the
  other.
- `decision_sequence` is allocated **only** under `Case FOR UPDATE` from the locked counter,
  is unique per case (partial unique index), and is monotonic.
- A decision callback is delivered **iff** no higher-sequence decision for the case is already
  delivered; otherwise `superseded`, never sent — no revert, ever.
- Delivery still stamps `decisions.published_at` + run `COMPLETE` (unchanged; 6b depends on it).
- At-least-once is preserved (a non-superseded row still retries with backoff; `superseded` is a
  deliberate terminal, not a drop of a *current* decision).
- No `ENGINE_BUILD_ID`/scoring change (this is delivery-layer only; the whole-tree drift guard
  re-pins on the src touch with **no bump**). Golden decision *content* is byte-identical; only
  the delivery ordering/terminal-state set changes.

## Testing strategy

- **Migration 013:** up/down/up clean; backfill maps kinds → streams; partial unique index
  rejects a duplicate `(case, sequence)` and allows many NULLs; `NOT VALID` CHECK rejects a bad
  stream value on a new row. Lineage guard green after the §C renumber (7b=`013`, `shipped`).
- **Stream separation:** a perpetually-failing POC email (stuck `pending` on backoff) never
  blocks the same case's decision callback — the decision delivers while the email retries.
  Real `OutboxPublisher` + real Postgres.
- **Sequence allocation:** two concurrent decide transactions on one case (a real barrier)
  produce strictly increasing, unique sequences; the partial unique index backstops a duplicate.
- **Revert guard (the 6b-critical case):** decision seq 2 delivered; requeue seq 1 (older) →
  claimed → `superseded`, **never sent**; assert the platform receiver saw only seq 2. Also:
  normal progression seq 1→2→3 all delivered in order (guard never fires); a NULL-sequence
  pre-7b row still delivers.
- **Lifecycle:** `superseded` counts as resolved (health/metrics show neither pending nor dead);
  `published_at`/run `COMPLETE` still set on real delivery.
- Full suite green on real Postgres; ruff clean; import-linter 2/0.

## Out of scope (YAGNI)

- No change to email semantics beyond stream separation (POC emails are independent; already
  redacted on delivery/dead). No per-decision *content* change. No cross-case ordering (each
  case is independent). No new config/flag. No UI. PR 7a (queue lease fencing) and PR 6b
  (revalidation) are separate units; 7b only supplies the ordering primitive 6b will consume.
