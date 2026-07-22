# PR 7b — Outbox stream separation + platform-authoritative decision ordering (item 8) — design (rev 3)

## Context

PR 7b is reordered **before PR 6b** (revalidation) because it supplies the callback-ordering
guarantee 6b's coordinator callbacks must rest on — without it, 6b needs a throwaway maintenance
fence (Codex PR-6b rev-5 F3). The tool's transactional outbox
(`src/kyc_tool/outbox/publisher.py`) has two present defects:

1. **One mixed per-case FIFO.** `_CLAIM_SQL` (`publisher.py:29-49`) claims the **min-id pending**
   row **per `case_id`**, mixing `decision_callback` and `poc_email` rows. A stuck POC email
   **blocks that case's decision callbacks** behind it.
2. **No decision ordering / revert guard.** Decision callbacks are ordered only by outbox `id`. A
   **requeued older** decision (retried or 6b-obsoleted, lower `id`) can be delivered **after** a
   newer decision, reverting the platform. Delivery stamps `decisions.published_at` and moves the
   run to `COMPLETE` (`_record_delivered`, `publisher.py:160-191`) — 6b's activation gate reads
   this — but nothing prevents the revert.

### Revision history (what each round corrected)

- **Rev 1** proposed a DB-only guard and a rolling migration. Both wrong: the publisher commits its
  claim (`publisher.py:146-148`), does HTTP **outside** any transaction (`151-152`), then stamps
  `published_at` in a **second** transaction (`160-191`), so two replicas race and no local guard
  closes it; and an old replica writes NULL `ordering_stream` (`52-63` sets neither column) →
  unclaimable NULL/NULL rows + unguarded NULL-sequence reverts. ROADMAP **M3**(`:96`)/**D1**(`:32`)
  already reserved `decision_sequence` for the wire with platform high-water dedupe — rev 1
  under-delivered.
- **Rev 2** made the platform high-water the authority, put `decision_sequence` on the wire, and
  replaced the rolling migration with a drained transactional cutover, legacy sequencing,
  forward-only downgrade, a `superseded` lifecycle, and relational integrity.
- **Rev 3 (this doc)** closes six defects where that design meets **frozen outbox payloads, the
  platform's pre-existing effective state, real rollback binaries, and the shipped worker-recovery
  machinery**: (F1) emission is decided at the **publisher**, not frozen at decide time, and the
  sequence is persisted in the internal payload + backfilled into existing rows; (F2) a **platform
  high-water bootstrap** seeds the receiver from its *actually-effective* state before flag-on;
  (F3) **two-phase irreversible rollback** on schema-compatible images only; (F4) the cutover
  reuses the **shipped** recovery one-shot + a symmetric outbox-lease reset, digest-pinned, with no
  mutating production smoke; (F5) an **exact** 6b convergence contract; (F6) the ROADMAP
  **detailed sections** are renumbered too. The keystone is a durable **singleton activation
  record** (modeled on PR 5a's `hmac_v1_observation`) that every consumer — `/readyz`, the
  publisher, PR 6b — reads to tell "flag configured" from "platform authority bootstrapped."

This is `.agents/ROADMAP.md` item 8.

## Decisions (rev 3)

1. **Stream separation** — `outbox.ordering_stream` (`decision`|`email`), **NOT NULL**; claim FIFO
   scoped per **`(case_id, ordering_stream)`**.
2. **Per-case decision sequence** — `decisions.decision_sequence`, allocated from the **locked case
   counter** (`cases.last_decision_sequence`, never `max()+1`), callback-emitting decisions only;
   unique per case; **persisted in the internal outbox payload** at enqueue (not flag-gated there).
3. **The platform high-water is the ordering authority** (F1/F2). Its receiver compares
   `(case_id, decision_sequence)` against a durable per-case high-water, applies/advances **only**
   higher, no-ops lower-or-equal, and — once a case is initialized (or after the global cutover) —
   treats an **absent** sequence as a no-op (**sticky**), never a mutation.
4. **Emission is decided at the publisher** (F1), the single network emitter. The internal payload
   always carries the sequence; `_deliver_decision_callback` builds an **outbound copy** that
   includes the field iff emission is active. Pipeline code never freezes the flag decision into a
   long-lived row.
5. **A durable singleton activation record** (F2/F3/F5) tracks the lifecycle: `schema_migrated` →
   `platform_bootstrapped` (manifest verified) → `emission_active`. It is the single source of
   truth for `/readyz`, the publisher, and 6b.
6. **The local `superseded` guard is an optimization** (never the authority), with a full honest
   lifecycle (§6).
7. **Drained cutover migration** (F4) using the **shipped** recovery machinery; **forward-only-
   after-use** and **two-phase irreversible** rollback (F3); ordinary transactional DDL.
8. **Integrity binding** (rev-2, kept) — `UNIQUE(case_id, decision_sequence)`, composite FK, and a
   kind↔stream↔sequence CHECK; the outbound body's sequence is asserted against the row before HTTP.

## Architecture

### 1. Migration 013 — drained cutover (`down_revision='012'`)

**7b owns `013`.** The ROADMAP renumber (table **and** detailed sections, F6) is done in this
spec's commit: PR 7b `013` / PR 6b `014` / PR 7a `015` / PR 8 `016` / PR 10 `017`; the build flips
only PR 7b's row to `shipped`.

**Precondition (operator, §Rollout):** platform submission paused; **zero** app writers running,
attested at the **orchestrator/process level** (not `pg_stat_activity` — engines set no
`application_name`, `session.py:16-17`); the shipped job + outbox recovery one-shots already run.

Schema (one ordinary transaction — writers stopped, so no `CONCURRENTLY`):

- **`outbox.ordering_stream TEXT`** — add nullable → backfill by kind → **assert zero NULL/unknown**
  → add+VALIDATE `CHECK (ordering_stream IS NOT NULL AND ordering_stream IN ('decision','email'))`.
- **`outbox.decision_sequence BIGINT NULL`**, **`outbox.resolved_at TIMESTAMPTZ NULL`** (F5
  terminal timestamp for `superseded`), **`decisions.decision_sequence BIGINT NULL`**,
  **`cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`**.
- **Singleton activation record** — `outbox_ordering_activation(id smallint PRIMARY KEY DEFAULT 1
  CHECK (id=1), schema_migrated_at, platform_bootstrapped_at, emission_active_at,
  manifest_expected_digest, manifest_returned_digest)`, seeded one row with only
  `schema_migrated_at=now()` set (bootstrap/activation NULL). Modeled on `hmac_v1_observation`
  (`tables.py:282-289`).

**Historical backfill (rev-2, kept):** per case, `decision_sequence = row_number() OVER (PARTITION
BY case_id ORDER BY decided_at, id)` for callback-producing decisions (`manual=false AND run_id IS
NOT NULL`); manual stays NULL. Bind every existing `decision_callback` outbox row (delivered/
pending/**dead**) to its decision's sequence via `run_id`; seed `cases.last_decision_sequence =
max` per case. **Also backfill the sequence into the frozen payload (F1):**
`UPDATE outbox SET payload_json = jsonb_set(payload_json, '{decision_sequence}',
to_jsonb(decision_sequence)) WHERE kind='decision_callback' AND decision_sequence IS NOT NULL` — so
a pre-flag pending/dead callback can emit its sequence when the publisher's emission turns on,
never stranded. **Preflight refusals** (raise, rolling the cutover back): orphan callback,
duplicate-run decision, sequence mismatch, remaining NULL `decision`-stream sequence, `email` row
with a sequence. Record in `AUDIT_FINDINGS.md`: reconstructed order is **deterministic backfill
(`decided_at,id`), not proof of original publication order**.

**Integrity (rev-2, kept):** `UNIQUE(case_id, decision_sequence)` on `decisions` (Postgres allows
multiple NULLs); composite FK `outbox(case_id, decision_sequence) → decisions(case_id,
decision_sequence)` (NULL member ⇒ not enforced for email rows, correct); CHECK
`(kind='decision_callback' AND ordering_stream='decision' AND decision_sequence IS NOT NULL) OR
(kind='poc_email' AND ordering_stream='email' AND decision_sequence IS NULL)`. Add
`ix_outbox_stream_claim (case_id, ordering_stream, status, next_attempt_at)`.

**Downgrade — forward-only-after-use (F3/rev-2):** the down migration raises if any
`decisions.decision_sequence`, `outbox.decision_sequence`, `cases.last_decision_sequence > 0`, **or
a non-NULL `platform_bootstrapped_at`/`emission_active_at`** exists — the sequence namespace and the
activation epoch are externally observed. On a never-used schema it drops cleanly (`up→down→up`
green on a fresh DB).

### 2. Stream-scoped claim (publisher)

`_CLAIM_SQL`'s inner subquery (`publisher.py:39-42`) gains `AND o2.ordering_stream =
o.ordering_stream`. Because 013 makes `ordering_stream` NOT NULL, the equality is never UNKNOWN.
`enqueue_decision_callback` sets `ordering_stream='decision'` + `decision_sequence`;
`enqueue_poc_email` sets `ordering_stream='email'`. (`case_id IS NULL` rows keep the global path.)

### 3. Decision-sequence allocation (decide transaction)

Seam: `pipeline._decide_txn` (`pipeline.py:351-518`) holds the Case `FOR UPDATE` from `_load`
(`:201`) across the `DecisionRow` build (`:485-495`) and enqueue (`:506`). For a callback-emitting
decision (manual approve takes a different path, no callback → no sequence): increment
`cases.last_decision_sequence` under the lock (never `max()+1`); stamp `DecisionRow.decision_sequence`;
pass it to `enqueue_decision_callback(..., decision_sequence=seq)`, which records it on the outbox
row **and inside `payload_json` (the internal canonical body always carries it)**. Single writer of
the counter, always under the case lock; the UNIQUE constraint backstops any bug.

### 4. Emission at the publisher (F1) — not frozen at decide time

The pipeline no longer flag-gates `decision_sequence` in `_callback_body` (`pipeline.py:571-598`);
it always writes the sequence into the internal payload (§3). All M3 emission logic moves to the
one real network emitter, `_deliver_decision_callback` (`publisher.py:82-122`):

- **Config** (`config.py`, beside `callback_include_event_sequence:77-79`):
  `callback_include_decision_sequence: bool = False` — default off, prod-configurable. **Effective
  emission** = this flag AND the activation record's `emission_active_at IS NOT NULL` (both, so a
  stray flag can't emit before bootstrap; §5).
- **Schema** (`api/schemas.py:144-164`): `decision_sequence: int | None = None` on `DecisionCallback`
  (preserved on validate, like `event_sequence`).
- **Outbound copy:** `_deliver_decision_callback(row)` reads the internal `payload_json` and builds
  the wire body: emission active ⇒ **require** a non-NULL `decision_sequence` (integrity-fail if
  missing on a `decision`-stream row) and include it; emission off ⇒ **strip** the field so the
  body is byte-identical to today's. A pre-flag frozen row therefore emits correctly after
  activation (it was backfilled, §1) and never strands.
- **Pre-HTTP integrity seam:** before `self.http.send`, load the decision + outbox column + internal
  JSON value and compare; on mismatch take an **explicit immediate integrity-failure path** (zero
  HTTP, durable audit + terminal `dead`/superseded per case), **not** the ordinary retry loop.

### 5. Platform authority — contract, bootstrap, and sticky semantics (F1/F2)

Documented in `PLATFORM_INTEGRATION.md` + **ADR-008** (ADR-006 reserved for 6b, ADR-007 for PR 10 —
left intact). Receiver contract:

> On `POST /kyc/decision` for `case_id=c` with `decision_sequence=s`: in one transaction, read the
> durable per-case high-water `h(c)`; if `s > h(c)`, apply and set `h(c)=s`; if `s ≤ h(c)`, no-op +
> success. Once `h(c)` is initialized (or after the global cutover), a body **without**
> `decision_sequence` is a no-op (**sticky**), never a mutation. Emails carry no sequence.

**Production high-water bootstrap (F2) — before any emission:** a staging `seq 2 → seq 1` test
proves the *algorithm*; it does **not** initialize production, where the platform already has
effective state keyed on legacy `(case_id, run_id)` (`test_phase4_platform.py:88`) and its
high-water would default to 0. Without bootstrap: legacy seq 2 already applied, tool crashed before
`published_at`, 013 leaves platform h=0, requeued old seq 1 is admitted (`1>0`) → **revert**.
Bootstrap procedure:

1. The tool emits a canonical, **hash-stamped manifest** of `{case_id, platform_current_run_id,
   mapped decision_sequence}` (platform_current_run_id from the tool's immutable decisions).
2. The **platform** derives each case's current run id from its **actually-effective** record,
   resolves it uniquely against that manifest, seeds `h(case)` transactionally **without changing
   decision content**, and returns a coverage/result manifest with a digest.
3. The tool **refuses activation** on any unknown/duplicate run id, missing case, sequence mismatch,
   or partial application; on full coverage it stamps `platform_bootstrapped_at` +
   `manifest_expected_digest`/`manifest_returned_digest` on the singleton record.
4. Only after bootstrap **and** the adversarial staging test may `emission_active_at` be stamped and
   the flag turned on, per producer.

`/readyz`, the publisher's effective-emission check, and 6b all read the singleton record to
distinguish **flag configured** from **platform authority bootstrapped**. Residual risk before
bootstrap (documented, mirrors PR 5a's pre-sunset v1 residual): only the local best-effort guard
covers ordering; full cross-replica safety begins at `emission_active_at`.

### 6. Local `superseded` guard as optimization — full lifecycle (F5/rev-2)

In `process_once` (`publisher.py:143-158`), after claiming a `decision`-stream row with a non-NULL
sequence, before `_deliver`: if a higher-sequence decision for the case is already delivered
(`EXISTS(SELECT 1 FROM decisions WHERE case_id=:c AND decision_sequence > :seq AND published_at IS
NOT NULL)`), call `_record_superseded(row)` and return without sending. `_record_superseded` is one
all-or-nothing txn (guarded `WHERE status='pending'`): sets `outbox.status='superseded'` +
`resolved_at=now()`; **leaves `DecisionRow.published_at` NULL** (never sent); transitions the run
through the **existing legal** `PUBLISH_DECISION→COMPLETE` edge with `finished_at` (**no** new
`SUPERSEDED` RunState — that would contradict `domain/models.py:35-46`); appends audit
`outbox.superseded` with old/superseding sequence + reason `higher_decision_already_published`.
Retention prunes `superseded` alongside `delivered` (`retention.py:29-35`); metrics report it
separately and **exclude** it from pending/dead alerts (`routes_metrics.py:60`); requeue already
409s non-dead rows (`ui/routes.py:459`).

### 7. PR 6b convergence contract (F5) — the exact shared predicate

6b's activation must consume a **precise** predicate, not "latest delivered or superseded." For
each case, over its allocated `decision_sequence` values:

- Let `g` = the greatest allocated sequence. **Convergence for `g` holds iff** `g` is durably
  **platform-acknowledged** — its decision has `published_at` set **and** its outbox row is
  `delivered`. If `g`'s row is `pending` **or `dead`**, activation **blocks** (a later manual
  requeue of a dead higher row legitimately changes platform state — higher wins — so it is a hard
  blocker, matching 6b rev-5).
- A `superseded` row counts resolved **only** when a strictly higher **delivered** sequence exists
  (a truly-latest sequence can never be `superseded`, since superseding requires a strictly higher
  one that would then be latest — so the rev-2 phrasing "delivered or superseded" was ill-formed).
- **Lower** `dead` rows may be classified safe **only after** `emission_active_at` is stamped
  (sticky platform high-water active, F1/F2) — before that, the local guard cannot prove safety.
- For a **superseded 6b coordinator** callback, 6b **rev 6** must *separately* prove the higher
  delivered decision was produced under the **target validator pair** — 7b ordering proves *order*,
  not *freshness*.

This predicate is written once as the shared query 6b imports; 7b ships it and its tests, 6b
consumes it.

## Rollout / cutover (operator order — RUNBOOK + DEPLOYMENT) (F4)

Every step names what the **tool** proves vs what the **platform/orchestrator** attests. Images are
**digest-pinned**; every one-shot and service runs the reviewed digest.

0. Build/publish the reviewed image; pin **every** one-shot/service to its digest.
1. **Pause** platform submission and **edge-block** the composer; **disable autoscaling/restarts**
   (so nothing respawns a writer).
2. **Hard-stop** API, pipeline, outbox, `dev_worker` (owns queue **and** outbox,
   `dev_worker.py:128-139`), retention, and every other DB writer. Prove zero by
   **orchestrator replica/process state** — not a heartbeat (none exists) and not `pg_stat_activity`.
3. From the pinned image: run **`python -m kyc_tool.ops.requeue_interrupted_jobs`** (the shipped
   one-shot — requeues `running` jobs and **decrements attempts** so a killed final attempt is
   retried, not passively dead-lettered; it refuses if any worker is live) and assert zero
   `running`; then run a **dedicated outbox-lease-reset one-shot** (new, symmetric) that resets
   every claimed-but-pending outbox lease (`next_attempt_at=now()` on `status='pending'`) **only
   after** zero publishers, so a killed publisher's in-flight claim is immediately re-claimable.
4. Run `alembic upgrade head` (013) — backfill + preflights + constraints, transactional; a
   preflight `raise` rolls the whole cutover back untouched.
5. Start **API only**; direct-probe `/readyz` (dynamic head + schema/activation state). Then start
   and attest pipeline/outbox workers. **No mutating production smoke decision** — behavior is
   proven against the exact digest in **staging / an isolated DB**, never by emitting a real
   callback here.
6. **Resume** submission. Start **no** old/non-digest writer after this point.
7. Later, on the platform's schedule: run the **bootstrap** (§5) → stamp `platform_bootstrapped_at`;
   run the adversarial staging `seq2→seq1` test; then stamp `emission_active_at` and set
   `callback_include_decision_sequence=true` per producer. Only then may PR 6b activate.

## Rollback — two irreversible phases (F3)

- **Before `platform_bootstrapped_at`/`emission_active_at` exist:** operational rollback is allowed
  **only on the schema-compatible PR-7b image** with publisher emission off — **never** the pre-7b
  binary (its enqueue writes no `ordering_stream`/`decision_sequence`, `publisher.py:52-63`, and
  fails 013's NOT-NULL/CHECK/FK on every insert). Recovery reuses the same stop→recover sequence.
- **After activation:** emission-off is **prohibited** (a sticky-sequenced case would reject every
  new callback, or — if missing values were still accepted — reopen the revert). Rollback stays on a
  sequence-capable image with **emission on**; a code defect is handled by **forward-fix** or a
  **separately approved** full maintenance/reconciliation procedure. **Production
  startup/readiness FAILS if the activation record exists but `callback_include_decision_sequence`
  is false.** Document the exact permitted image digests per phase and the preflight query.

## Invariants

- Claim FIFO per `(case_id, ordering_stream)`; `ordering_stream` NOT NULL everywhere (no
  NULL/NULL). `decision_sequence` allocated only under `Case FOR UPDATE`, unique per case, monotonic.
- Every `decision`-stream row is sequenced, FK-bound, and carries the sequence in its internal
  payload; every `email` row has NULL sequence; the CHECK enforces the triple. **Emission is decided
  at the publisher**, gated on the flag **and** `emission_active_at`; the outbound body's sequence is
  asserted against the row before HTTP.
- Ordering authority is the **platform high-water**, bootstrapped from its effective state before
  flag-on and **sticky** thereafter. The local `superseded` guard is a best-effort optimization only.
- `superseded` is a real terminal: run `COMPLETE` via the legal edge, `published_at` NULL,
  `resolved_at` set, same retention as `delivered`, metrics separate, requeue refuses.
- The migration is a **drained cutover** using the shipped recovery one-shot + a symmetric
  outbox-lease reset; the downgrade is **forward-only-after-use**; rollback is **two-phase
  irreversible**; startup refuses flag-off after activation.
- The **6b convergence contract** (§7) is exact: greatest sequence platform-acknowledged or
  activation blocks (incl. `dead`); superseded resolved only under a strictly higher **delivered**.
- Delivery still stamps `published_at` + run `COMPLETE`. At-least-once preserved (`superseded` is a
  deliberate terminal for a *superseded* decision, never a drop of a *current* one).
- **No `ENGINE_BUILD_ID`/scoring change** — delivery-layer only; the whole-tree drift guard
  (`EXPECTED_ENGINE_SOURCE_HASH` over `src/kyc_tool/**/*.py`) re-pins on the src touch **with no
  bump** (same handling as `callback_include_event_sequence`). Golden decision *content*
  byte-identical; only ordering, the optional wire field, and the terminal-state set change.
- `KYC_Tool_Build_Package/` + **M2** untouched. This lifts nothing about enforcement/scoring; it
  makes an obsolete callback unable to revert platform state — strict hardening.

## Testing strategy (each finding carries a mutation witness)

- **Migration 013 (rev-2 + F1 payload backfill):** seed both kinds + delivered/pending/**dead**
  pre-013 callbacks + manual decisions; upgrade; assert mapped/claimable rows, contiguous unique
  per-case sequences (`decided_at,id`), manual NULL, outbox sequences matching decisions, counter =
  per-case max, **and each pre-013 `decision_callback.payload_json` now contains its sequence**.
  Direct post-migration inserts of NULL/bad stream, NULL-sequence decision row, sequenced email row,
  outbox≠decision sequence, and duplicate `(case_id, decision_sequence)` all **fail at commit**.
  Preflight refusals (orphan/dup-run/remaining-NULL) roll back. Mutation: removing the CHECK, FK,
  assert-zero-NULL, or the payload backfill each fails a test. `up→down→up` clean on empty schema.
- **Frozen-payload emission (F1):** create pending **and** dead callbacks while emission off; flip
  only the publisher's effective emission on; prove **both emit their backfilled sequence**. Then
  send seq 2, then a body with **no** sequence, and prove the latter **cannot** change receiver
  state (sticky). Mutations that gate in `_callback_body`, omit the JSON backfill, or accept
  missing-after-sequenced must fail. Tamper the outbound body's sequence ≠ the row → **zero** HTTP +
  terminal integrity error.
- **Production bootstrap (F2):** platform effectively at legacy seq 2, high-water unset; prove
  **activation refuses** and seq 1 cannot be admitted; seed the verified mapping to `h=2`; prove
  seq 1 is a no-op and seq 3 applies. Mutation-skipping coverage, digest comparison, or
  platform-current-run binding must fail.
- **Two-phase rollback (F3):** execute the **old** enqueue SQL against post-013 constraints and
  prove it **fails** (pre-7b image barred). Stamp `emission_active_at`, boot with the flag false,
  and prove **startup/readiness fails** (no publisher constructed, no callback claimed). Mutation
  permitting post-activation flag-off must fail.
- **Hard-stop recovery (F4):** kill a **final-attempt** pipeline job and an outbox publisher
  **after claim**; run the real `requeue_interrupted_jobs` + outbox-lease-reset one-shots; prove the
  job is requeued **without consuming the forced-stop attempt** and the callback is **immediately
  claimable**. Duplicate HTTP after a send-before-crash is allowed and handled by receiver
  dedupe/high-water. Mutation omitting either recovery command must fail. Assert the runbook order
  bars any post-cutover non-digest writer and forbids a mutating production smoke.
- **Downgrade refusal (F3/rev-2):** empty-schema `up→down→up` works; populate/publish one sequence
  **or** stamp bootstrap/activation and prove downgrade **refuses without changing** any row.
- **Stream separation / allocation / superseded lifecycle / legacy-dead-via-UI** (rev-2, kept):
  stuck POC email never blocks the case's decision; two concurrent decides → strictly increasing
  unique sequences; superseded atomic tuple (outbox+run+decision+audit) + retention + metrics-
  separate + requeue-409; a backfilled old **dead** callback requeued through the real
  `POST /ui/api/requeue/outbox/{id}` is superseded and cannot change the fake receiver.
- **6b convergence contract (F5):** highest row **dead** ⇒ convergence **false**; lower row
  superseded by a delivered higher ⇒ **true for ordering**; coordinator superseded by a higher
  **pre-target** decision ⇒ 6b activation still **false**.
- **Lineage (F6):** `tests/unit/test_migration_lineage.py` green after the §C **and detailed-section**
  renumber; `rg` shows no contradictory live ROADMAP allocation. No helper-only duplicate test.
- **Gate:** `./manage.sh lint`; the real lineage test; targeted real-Postgres migration/outbox/UI/
  **ops** tests above; then `./manage.sh test`.

## Docs + governance (updated in the build)

`PLATFORM_INTEGRATION.md` (receiver contract + bootstrap manifest + sticky semantics),
`DEPLOYMENT.md` + `RUNBOOK.md` (digest-pinned stop→recover→migrate→start order, orchestrator
attestation, bootstrap, activation, **two-phase** rollback + startup-refusal + preflight queries),
`OVERVIEW.md` (stream separation + platform-authoritative ordering), **ADR-008**, `AUDIT_FINDINGS.md`
(backfill = deterministic reconstruction, not proof of publication order; activation record),
`.agents/ROADMAP.md` (table **and** detailed sections + ADR-008, this commit).

## ROADMAP renumber (F6 — table **and** detailed sections, this commit)

§C table, status prose, **and the per-unit detailed sections** updated now: PR 7b `013`/down `012`
(item 8, **pending** until authored, rev-3 schema/cutover), PR 6b `014`/down `013`, PR 7a `015`/down
`014`; 8=`016`, 10=`017`. `rg` the repo for stale reservation prose (the paused PR-6b rev-5 spec may
stay historically versioned, but its future rev 6 uses `014`). Keep the table-based lineage test; the
build flips only PR 7b's State to `shipped`.

## Out of scope (YAGNI)

- No email-semantics change beyond stream separation. No per-decision *content* change; no
  cross-case ordering.
- No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched.
- `event_sequence` (D1, shipped in PR 2) keeps its existing `_callback_body` gate — it is an
  ordering *hint*, not the authority, and is out of scope here; only `decision_sequence` moves to
  publisher-side emission. (A future unit may align `event_sequence` to the same pattern.)
- PR 7a (queue lease fencing) and PR 6b (revalidation) remain separate units; 7b supplies the
  ordering primitive, platform contract, and activation record 6b consumes.
