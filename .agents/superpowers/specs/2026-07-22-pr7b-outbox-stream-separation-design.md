# PR 7b — Outbox stream separation + platform-authoritative decision ordering (item 8) — design (rev 2)

## Context

PR 7b is reordered **before PR 6b** (revalidation) because it supplies the callback-ordering
guarantee 6b's coordinator callbacks must rest on — without it, 6b needs a throwaway maintenance
fence (Codex PR-6b rev-5 F3). The tool's transactional outbox
(`src/kyc_tool/outbox/publisher.py`) has two present defects:

1. **One mixed per-case FIFO.** `_CLAIM_SQL` (`publisher.py:29-49`) claims the **min-id pending**
   row **per `case_id`**, mixing `decision_callback` and `poc_email` rows. A POC email that keeps
   failing stays `pending` on backoff as the case's min-id row, so it **blocks that case's
   decision callbacks** behind it.
2. **No decision ordering / revert guard.** Decision callbacks are ordered only by outbox `id`. A
   **requeued older** decision (a retried or 6b-revalidation-obsoleted row, lower `id`) can be
   re-claimed and delivered **after** a newer decision already delivered, reverting the platform
   to the stale decision. Delivery stamps `decisions.published_at` and moves the run to `COMPLETE`
   (`_record_delivered`, `publisher.py:160-191`) — 6b's activation gate reads this — but nothing
   prevents the revert.

### What rev 1 got wrong (why this is a full rewrite, not a patch)

Rev 1 proposed a **DB-only** guard (deliver only if no higher-sequence decision has
`published_at`) and claimed the migration was **rolling-safe**. Both are wrong, and the second
compounds into three separate holes:

- **The tool cannot unilaterally prevent the revert.** The publisher commits its claim/lease
  (`publisher.py:146-148`), does the HTTP **outside any transaction** (`151-152`), and only
  *afterward* opens a second transaction to stamp `published_at` (`160-191`). Two publisher
  replicas therefore race: A sends seq 2 and pauses before `_record_delivered`; an operator
  requeues dead seq 1; B claims seq 1, its `published_at` probe still sees no delivered higher
  row, and **B sends seq 1 after seq 2** — the platform reverts. No purely-local guard closes
  this gap.
- **The canonical roadmap already says the fix is on the wire.** ROADMAP **M3**
  (`.agents/ROADMAP.md:94-97`) defines PR 7b as "`decision_sequence` + platform high-water-mark
  dedupe," and **D1** (`:32-33`) reserves `decision_sequence` as an on-the-wire field. Rev 1 put
  nothing on the wire (`api/schemas.py:144-164` carries only the separately-gated `event_sequence`;
  `config.py:77-79` has only `callback_include_event_sequence`). Rev 1 under-delivered against the
  plan both agents follow.
- **"Rolling-safe" manufactures unclaimable rows and unguarded reverts.** An old replica's enqueue
  omits `ordering_stream` (`publisher.py:52-63` set neither column), so its insert is NULL and the
  proposed inner predicate `o2.ordering_stream = o.ordering_stream` is SQL UNKNOWN for NULL/NULL —
  the row is permanently unclaimable. The same old replica writes NULL-sequence callbacks that
  rev 1 explicitly delivers **unguarded**, and the authenticated UI can requeue a dead one
  (`ui/routes.py:451-476`). A plain reversible downgrade then destroys an externally-observed
  monotonic namespace and reuses sequence numbers after re-upgrade.

**Rev 2 corrects the architecture:** the **platform's per-case high-water mark is the ordering
authority** (M3 cutover, flag-gated, exactly the dual-accept pattern PR 5a already established);
the tool's local `superseded` guard is a best-effort **optimization**, never the authority;
migration 013 is a **drained, non-hot cutover** with a **forward-only-after-use** downgrade; and
every denormalized sequence is bound by a real DB constraint. This is `.agents/ROADMAP.md` item 8.

## Decisions (rev 2)

1. **Stream separation** — `outbox.ordering_stream` (`decision`|`email`), **NOT NULL**; the claim
   FIFO is scoped per **`(case_id, ordering_stream)`** so the two streams drain independently.
2. **Per-case decision sequence** — `decisions.decision_sequence`, allocated from the **locked
   case counter** (`cases.last_decision_sequence`, never `max()+1`), only for callback-emitting
   decisions; unique per case; **put on the wire** (M3, flag-gated).
3. **The platform high-water is the ordering authority** (F1). Its receiver transactionally
   compares `(case_id, decision_sequence)` against a durable per-case high-water, applies and
   advances **only** a higher value, and returns success/no-op for lower-or-equal. This is what
   makes an obsolete callback unable to revert state — under every replica race.
4. **The local `superseded` guard is an optimization** — the tool still declines to send a
   callback it can locally prove obsolete (avoids pointless HTTP, keeps the outbox truthful), but
   correctness does **not** depend on it. It has a full, honest lifecycle (§6).
5. **Drained cutover migration** (F2/F3) — 013 is **not** rolling-safe. All old API, pipeline,
   and outbox processes are stopped and attested before it runs; only the reviewed image starts
   after. Ordinary transactional DDL (no `CONCURRENTLY`, no autocommit split).
6. **Forward-only-after-use downgrade** (F4) — 013 down-migrates only when no sequence/binding/
   counter was ever populated; otherwise it refuses. Operational rollback is **new image +
   emission flag off**, never a schema downgrade.
7. **Integrity binding** (F6) — `UNIQUE(case_id, decision_sequence)` on `decisions`, a composite
   FK from decision-stream outbox rows into it, and a CHECK binding kind↔stream↔sequence
   nullability. The wire body is built from the **same allocated scalar** and asserted before HTTP.

## Architecture

### 1. Migration 013 — drained cutover (`down_revision='012'`)

**7b now owns `013`** (reordered ahead of 6b). Per finding 7 the ROADMAP renumber is done **in
this spec's commit** (§ROADMAP renumber below), not deferred to build: PR 7b `013` (pending),
PR 6b `014`, PR 7a `015`, PR 8 `016`, PR 10 `017`. The build flips only PR 7b's row to `shipped`.

**Precondition (operator, enforced by runbook order — §Rollout):** platform submission paused,
and **zero** old API/pipeline/outbox processes running, attested before `alembic upgrade`. The
migration assumes it is the sole writer.

Schema (one ordinary transaction — writers are stopped, so no `CONCURRENTLY`):

- **`outbox.ordering_stream TEXT`** — add nullable → backfill
  `UPDATE outbox SET ordering_stream = CASE kind WHEN 'poc_email' THEN 'email' ELSE 'decision' END`
  → **assert zero NULL/unknown** (`SELECT count(*) … WHERE ordering_stream IS NULL OR ordering_stream
  NOT IN ('decision','email')` must be 0, else `raise`) → add and **VALIDATE**
  `CHECK (ordering_stream IS NOT NULL AND ordering_stream IN ('decision','email'))`. (`NOT VALID`
  does not change NULL semantics — the CHECK names `IS NOT NULL` explicitly.)
- **`outbox.decision_sequence BIGINT NULL`** — set on enqueue for `decision` rows; NULL for emails.
- **`outbox.resolved_at TIMESTAMPTZ NULL`** (F5) — truthful terminal timestamp for `superseded`
  rows (delivered rows keep `delivered_at`; the two are disjoint by construction).
- **`decisions.decision_sequence BIGINT NULL`** (NULL = manual/non-callback decision).
- **`cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`** — the locked allocation counter.

**Historical backfill (F3) — deterministic per-case sequencing of *existing* callbacks:**

- For each case, assign contiguous `decision_sequence = row_number() OVER (PARTITION BY case_id
  ORDER BY decided_at, id)` to every **callback-producing** decision (`manual = false AND run_id
  IS NOT NULL`). Manual decisions (`manual = true`) stay NULL.
- Bind **every** existing `decision_callback` outbox row — `delivered`, `pending`, **and `dead`** —
  to its decision's sequence via `run_id` (one automatic decision per callback run; see preflight).
- Seed `cases.last_decision_sequence = max(decision_sequence)` per case (0 where none).
- **Preflight refusals** (the migration `raise`s, rolling back the whole cutover, before any
  irreversible write) on: an orphan `decision_callback` outbox row (no matching decision/run); a
  callback run with more than one automatic decision; an outbox/decision sequence mismatch after
  binding; any remaining NULL `decision_sequence` on a `decision`-stream outbox row; any
  `email`-stream row with a non-NULL sequence.
- Record in the migration docstring + `AUDIT_FINDINGS.md`: reconstructed order is **deterministic
  backfill (`decided_at,id`), not proof of original publication order** (mirrors D2's
  `event_sequence` framing). `decided_at` ties break by `id`.

**Integrity constraints (F6):**

- `UNIQUE(case_id, decision_sequence)` on `decisions` — an **ordinary** unique constraint (Postgres
  permits multiple NULLs, so manual/pre-callback rows never collide; no partial index needed).
- Composite FK: `outbox(case_id, decision_sequence) REFERENCES decisions(case_id,
  decision_sequence)` — applies only to `decision`-stream rows (email rows have NULL sequence, and
  a NULL member makes the composite FK not enforced for that row, which is correct).
- `CHECK` binding kind↔stream↔sequence:
  `(kind='decision_callback' AND ordering_stream='decision' AND decision_sequence IS NOT NULL)
  OR (kind='poc_email' AND ordering_stream='email' AND decision_sequence IS NULL)`.

**Index:** the `(case_id, ordering_stream)` claim path reuses the existing `ix_outbox_claim`
(`status, next_attempt_at`) plus the `case_id` index; add `ix_outbox_stream_claim` on
`(case_id, ordering_stream, status, next_attempt_at)` to keep the scoped claim sargable. Built
in-transaction (writers stopped).

**Downgrade — forward-only-after-use (F4):** the down migration first runs a preflight —
`EXISTS(SELECT 1 FROM decisions WHERE decision_sequence IS NOT NULL)` OR any
`outbox.decision_sequence IS NOT NULL` OR `EXISTS(SELECT 1 FROM cases WHERE last_decision_sequence
> 0)` — and **raises an actionable error** ("013 down refused: decision-sequence namespace is
externally observed by the platform high-water; roll back with the reviewed prior image and
`callback_include_decision_sequence=false`, do not downgrade schema") if any is populated. On a
never-used schema (empty namespace) it cleanly drops the columns/constraints/counter, so
`up → down → up` on a fresh DB stays green. ORM (`db/tables.py`): the five columns + constraints.

### 2. Stream-scoped claim (publisher)

`_CLAIM_SQL`'s inner correlated subquery (`publisher.py:39-42`) gains
`AND o2.ordering_stream = o.ordering_stream`, so the eligible row is the **min-id pending row of
its own `(case_id, ordering_stream)`**. Because 013 guarantees `ordering_stream` is NOT NULL for
every row, the equality is never UNKNOWN (F2 dissolved). Emails and decisions of the same case
become independent FIFOs. `enqueue_decision_callback` sets `ordering_stream='decision'` +
`decision_sequence`; `enqueue_poc_email` sets `ordering_stream='email'`. (`case_id IS NULL` rows,
if any, keep the global path — they are non-case system mail.)

### 3. Decision-sequence allocation (decide transaction)

Exact seam: `pipeline._decide_txn` (`pipeline.py:351-518`) already holds the **Case `FOR UPDATE`**
from `_load` (`pipeline.py:201`, `session.get(Case, run.case_id, with_for_update=True)`) across the
`DecisionRow` build (`:485-495`) and `enqueue_decision_callback` (`:506`). For a
**callback-emitting** decision (the `_decide_txn` path; **manual approve** goes through a different
path and writes no callback — AUDIT C3 — so it allocates **no** sequence):

- increment `cases.last_decision_sequence` under the held case lock (race-free; never `max()+1`),
- stamp it on `DecisionRow.decision_sequence` (`:485-495`),
- pass it to `enqueue_decision_callback(..., decision_sequence=seq)` (`:506`) which records it on
  the outbox row and its `ordering_stream='decision'`.

The allocation is the **single writer** of `last_decision_sequence`, always under the case lock, so
two concurrent decides on one case produce strictly increasing, unique sequences; the
`UNIQUE(case_id, decision_sequence)` constraint backstops any bug.

### 4. Wire emission (F1) — flag + schema + body-from-allocated-scalar

- **Config** (`config.py`, beside `callback_include_event_sequence:77-79`): add
  `callback_include_decision_sequence: bool = False` — **default off, independently
  production-configurable**, flipped per environment only after the staging activation test (§5).
- **Schema** (`api/schemas.py:144-164`): add `decision_sequence: int | None = None` to
  `DecisionCallback`, documented like `event_sequence` (preserved on validate, not dropped).
- **Body** (`pipeline._callback_body`, `pipeline.py:571-598`): when the flag is on, set
  `body["decision_sequence"] = seq` from the **exact scalar allocated to the persisted
  `DecisionRow`** in §3 — not a re-read, not `event_sequence`. `event_sequence` (D1, the
  triggering event's ordinal) and `decision_sequence` (the decision's ordinal) are distinct and
  both may be present.
- **Pre-HTTP assertion seam** (`publisher._deliver_decision_callback`, `publisher.py:82-122`):
  before `self.http.send`, assert the claimed outbox row's `decision_sequence` equals the JSON
  body's `decision_sequence` (when the flag is on); a mismatch raises a terminal, actionable error
  and makes **zero** HTTP calls (guards against a drifted denormalized row sending the wrong
  ordinal — F6).

### 5. Platform high-water contract (F1) — the authority

Documented in `PLATFORM_INTEGRATION.md` + **ADR-008** (leave PR 6b's reserved ADR-006 and PR 10's
ADR-007 intact; record the ADR-008 reservation in ROADMAP). The receiver contract:

> On `POST /kyc/decision` with `decision_sequence = s` for `case_id = c`: in one transaction, read
> the durable per-case high-water `h(c)` (default 0); if `s > h(c)`, apply the decision and set
> `h(c) = s`; if `s <= h(c)`, **no-op** and return success (idempotent). Never apply a
> lower-or-equal sequence. Emails carry no `decision_sequence` and are unaffected.

**Activation (M3 cutover, gated):** `callback_include_decision_sequence` stays **off** until a v2
**staging receiver test** proves that delivering `seq 2` then `seq 1` leaves **seq 2 effective**;
then emission is enabled on **every** producer. The local `published_at`/`superseded` guard (§6)
is retained only as an optimization and never as the authority. **Residual risk while the flag is
off** (documented, mirrors PR 5a's pre-sunset v1 residual): the cross-replica race of §Context is
covered only by the local best-effort guard; full cross-replica safety begins at flag-on. **PR 6b
may not rely on 7b's ordering until this activation is complete** (§7 interaction).

### 6. Local `superseded` guard as optimization — full lifecycle (F5)

In `process_once` (`publisher.py:143-158`), after claiming a `decision`-stream row with a non-NULL
`decision_sequence`, **before `_deliver`**: if a higher-sequence decision for the case is already
delivered —
`EXISTS(SELECT 1 FROM decisions WHERE case_id=:c AND decision_sequence > :seq AND published_at IS
NOT NULL)` — call a new **`_record_superseded(row)`** and return without sending. Otherwise deliver
as today.

`_record_superseded` is **one transaction** (all-or-nothing) that, for the still-`pending` outbox
row (guarded `WHERE status='pending'` so a concurrent deliver can't double-resolve):

- sets `outbox.status='superseded'`, `outbox.resolved_at=now()` (a new terminal status alongside
  `delivered`/`dead`),
- **leaves `DecisionRow.published_at` NULL** — truthful: this decision was never sent,
- transitions the run through the **existing legal edge** `PUBLISH_DECISION→COMPLETE` with
  `finished_at` (guarded `WHERE state='PUBLISH_DECISION'`; **no** new `SUPERSEDED` RunState —
  inventing one would contradict the normative state machine, `domain/models.py:35-46`),
- appends an audit event `outbox.superseded` with `{case_id, run_id, outbox_id, old_sequence:seq,
  superseding_sequence, reason:'higher_decision_already_published'}`.

Lifecycle wiring:

- **Retention** (`workers/retention.py:29-35`): extend the delivered-prune to also prune
  `superseded` rows past retention (`status IN ('delivered','superseded') AND
  COALESCE(delivered_at, resolved_at) < now() - …`), so a superseded row is not unpruned forever.
- **Metrics/health** (`api/routes_metrics.py:60`, `outbox_by_status` already groups by status, so
  `superseded` surfaces automatically): confirm the dead-letter/pending **alert** logic keys only
  on `dead` (and genuine pending backlog) and **excludes** `superseded` (it is resolved, neither
  pending nor dead). UI dead-letter panels (`ui/routes.py:110-118`) already filter `status='dead'`
  — unaffected.
- **Requeue** (`ui/routes.py:451-476`): `requeue_outbox` already refuses anything not `dead`
  (`:459`), so a `superseded` row returns 409 unchanged — add a test pinning that.

### 7. Interaction with PR 6b (why this unblocks it — honestly scoped)

6b's revalidation coordinator emits a **new** (higher-sequence) decision callback. An older
callback later requeued is (a) declined locally by the `superseded` guard and (b) **authoritatively**
ignored by the platform high-water. 6b's activation "delivery convergence" then reduces to: the
latest per-case decision is delivered (or superseded) and no `pending` decision row outranks it —
no maintenance fence, no public-write pause. **Honest dependency:** because the cross-replica race
is closed only by the platform high-water, **6b may resume building on this primitive immediately,
but 6b's own activation waits on the M3 decision-sequence cutover (§5) being complete** (flag on,
staging-proven, all producers emitting). This is a real scheduling edge, recorded in ROADMAP M3.

## Rollout / cutover (operator order — RUNBOOK + DEPLOYMENT)

1. Announce + **pause/buffer** platform submission.
2. **Stop** all API, pipeline, and outbox processes; **attest zero** old processes (documented
   probe: no listeners on the API port, no worker heartbeats, `pg_stat_activity` shows no app
   sessions).
3. `alembic upgrade head` (013) — backfill + preflights + constraints, transactional. On any
   preflight `raise`, the cutover rolls back untouched; fix the flagged data and retry.
4. Start **only** the reviewed new image (API + workers). Direct-probe `/readyz` (dynamic head)
   and a smoke decision.
5. Resume submission. **Start no old writer after this point** (enforced by deploy tooling +
   documented).
6. Later, on the platform's schedule: run the staging high-water activation test (§5), then flip
   `callback_include_decision_sequence=true` on every producer. Only then may PR 6b activate.

**Operational rollback** (not a schema downgrade — F4): redeploy the reviewed prior image with
`callback_include_decision_sequence=false`. The schema stays; the down migration refuses once the
namespace is used. Document the exact preflight query and recovery in RUNBOOK.

## Invariants (Global Constraints + review rubric)

- The claim FIFO is per `(case_id, ordering_stream)`; a stuck row in one stream never blocks the
  other. `ordering_stream` is NOT NULL for every row (no unclaimable NULL/NULL).
- `decision_sequence` is allocated **only** under `Case FOR UPDATE` from the locked counter, is
  unique per case (`UNIQUE(case_id, decision_sequence)`), and is monotonic.
- Every `decision`-stream outbox row is sequenced and FK-bound to its decision; every `email` row
  has NULL sequence; the CHECK enforces the kind↔stream↔nullability triple. The wire body's
  sequence is asserted equal to the row's before any HTTP.
- **Ordering authority is the platform high-water** (flag-gated M3 cutover). The local `superseded`
  guard is a best-effort optimization; it never sends a locally-provable-obsolete callback but is
  not relied on for cross-replica correctness.
- `superseded` is a real terminal state: run reaches `COMPLETE` via the existing legal edge,
  `published_at` stays NULL (never sent), `resolved_at` set, same retention as `delivered`, metrics
  report it separately and exclude it from pending/dead alerts, requeue refuses it.
- 013 is a **drained cutover** (no `CONCURRENTLY`, no rolling deploy) with a
  **forward-only-after-use** downgrade.
- Delivery still stamps `decisions.published_at` + run `COMPLETE` (unchanged; 6b depends on it).
- At-least-once preserved (a non-superseded row still retries with backoff; `superseded` is a
  deliberate terminal for a *superseded* decision, never a drop of a *current* one).
- **No `ENGINE_BUILD_ID`/scoring change** — delivery-layer only. The whole-tree drift guard
  (`EXPECTED_ENGINE_SOURCE_HASH` over `src/kyc_tool/**/*.py`) re-pins on the src touch **with no
  bump** (adding a delivery field/flag is not a scoring/gate/decision semantic change; same
  handling as `callback_include_event_sequence`). Golden decision *content* is byte-identical; only
  delivery ordering, the new optional wire field, and the terminal-state set change.
- `KYC_Tool_Build_Package/` + **M2** untouched. This lifts nothing about enforcement/scoring scope;
  it makes an obsolete callback unable to revert platform state, which is a strict hardening.

## Testing strategy (each finding carries a mutation witness)

- **Migration 013 (F2/F3/F6):** seed both kinds + delivered/pending/**dead** pre-013 callbacks +
  manual decisions; upgrade; assert every row mapped and claimable, contiguous unique automatic
  sequences per case in `(decided_at,id)` order, manual sequences NULL, outbox sequences matching
  their decisions, `last_decision_sequence = per-case max`. Prove a direct post-migration insert of
  a NULL/bad `ordering_stream`, a `decision`-stream row with NULL sequence, an `email` row with a
  sequence, an outbox sequence ≠ its decision's, and a duplicate `(case_id, decision_sequence)` all
  **fail at commit** (CHECK/FK/UNIQUE). Preflight: seed an orphan callback / duplicate-run decision
  / remaining-NULL and assert the migration **refuses** (rolls back). Mutation: removing the CHECK,
  the FK, the assert-zero-NULL step, or the backfill each makes a test fail. `up→down→up` clean on
  an empty schema.
- **Downgrade refusal (F4):** empty-schema `up→down→up` works; then populate/publish one sequence
  and assert downgrade **refuses without deleting or changing** any decision/outbox/counter row.
  Mutation-removing any one populated surface from the preflight must fail.
- **Stream separation (F-defect-1):** a perpetually-failing POC email (stuck `pending` on backoff)
  never blocks the same case's decision callback — the decision delivers while the email retries.
  Real `OutboxPublisher` + real Postgres.
- **Sequence allocation:** two concurrent decide transactions on one case (a real barrier) produce
  strictly increasing, unique sequences; the UNIQUE constraint backstops a forced duplicate.
- **Cross-replica revert / platform authority (F1 — the load-bearing 6b test):** two real
  `OutboxPublisher` instances with a barrier at HTTP-success/before-DB-stamp; requeue seq 1 in the
  gap; assert the **fake high-water receiver** sees both attempts but keeps **seq 2 effective**.
  Mutation: deleting the `decision_sequence` callback field, disabling the receiver high-water, or
  treating a lower sequence as an ordinary update each makes the test **fail**. Also: the pre-HTTP
  body/row assertion — tamper the JSON body's sequence and prove **zero** HTTP calls + a terminal
  error.
- **Local superseded optimization + lifecycle (F5):** decision seq 2 delivered; requeue seq 1 →
  claimed → `_record_superseded`: assert the atomic tuple (outbox `superseded`+`resolved_at`, run
  `COMPLETE`+`finished_at`, decision `published_at` **still NULL**, audit `outbox.superseded` with
  old/superseding sequence), retention eligibility, metrics report `superseded` separately and it
  does **not** trip pending/dead alerts, and the UI requeue returns **409**. Fault between each
  staged update and commit → all-or-nothing. Normal progression seq 1→2→3 all delivered in order
  (guard never fires). A NULL-sequence (email) row delivers with no guard.
- **Legacy dead callback via real UI (F3):** deliver a new high sequence; requeue a backfilled old
  **dead** decision callback through the real `POST /ui/api/requeue/outbox/{id}` endpoint; assert
  it is superseded locally and cannot change the fake receiver's high-water. Mutation: allowing one
  old (NULL-sequence-writing) writer after cutover must fail a rollout-order assertion.
- **Lineage (F7):** `tests/unit/test_migration_lineage.py` green after the §C renumber (7b=`013`
  `pending` until authored). Do **not** add a helper-only ownership test duplicating the table.
- **Gate:** `./manage.sh lint` (ruff + import-linter 2/0); targeted real-Postgres
  migration/outbox/UI tests above; `tests/unit/test_migration_lineage.py`; then `./manage.sh test`
  full suite green.

## Docs + governance (updated in the build)

`PLATFORM_INTEGRATION.md` (receiver high-water contract), `DEPLOYMENT.md` + `RUNBOOK.md` (drained
cutover order, attest-zero probe, activation test, forward-only-after-use rollback + preflight
query), `OVERVIEW.md` (stream separation + ordering authority), **ADR-008** (platform-authoritative
decision ordering; ADR-006 reserved for 6b, ADR-007 for PR 10 — left intact), `AUDIT_FINDINGS.md`
(backfill = deterministic reconstruction, not proof of publication order), `.agents/ROADMAP.md`
(renumber + ADR-008 reservation, done in this spec commit per F7).

## ROADMAP renumber (F7 — done in this spec's commit, not deferred)

§C reservation table and status prose updated **now**: PR 7b `013` (item 8, **pending** until the
migration is authored), PR 6b `014` (item 7B), PR 7a `015` (item 9), PR 8 `016`, PR 10 `017`; the
"next unit" prose reflects the approved reorder (PR 7b before PR 6b). The build flips only PR 7b's
State to `shipped` when 013 lands. The real lineage parser/validator is run after the edit.

## Out of scope (YAGNI)

- No email-semantics change beyond stream separation (POC emails already redacted on delivery/dead).
- No per-decision *content* change; no cross-case ordering (each case independent).
- No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched.
- PR 7a (queue lease fencing) and PR 6b (revalidation) remain separate units; 7b supplies the
  ordering primitive + platform contract 6b consumes.
