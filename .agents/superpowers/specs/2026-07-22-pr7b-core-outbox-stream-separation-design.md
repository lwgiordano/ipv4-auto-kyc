# PR 7b-core — Outbox stream separation + local decision ordering (item 8, part 1) — design (rev 10)

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
- **No stored body-digest column — deliberately (rev 10, re-audit P1/F4).** The durable-authority
  contract in §Rollout keeps the `decision_callback` row *and its `payload_json`* forever, so the
  body is never lost and a digest is always derivable on demand. A stored `body_sha256` was designed
  and **rejected**: it must be kept in lockstep with `payload_json` (a second representation of the
  same fact — precisely the drift class this PR exists to eliminate), it needs a backfill and an
  enqueue-time write, and it forces one canonical encoder to be reproduced in **both** Python and SQL
  (`json.dumps` and `jsonb::text` do not agree on separators, so the two consumers could disagree
  silently). The digest is instead **one SQL expression, defined once** and used verbatim by the
  §Rollout restore acceptance predicate and by 014's candidate manifest:
  `callback_body_digest(o) = encode(sha256(convert_to(o.payload_json::text, 'UTF8')), 'hex')`
  (lowercase hex SHA-256; `sha256`/`convert_to`/`encode` are Postgres core — no `pgcrypto`).
  `payload_json` is `jsonb`, so `::text` is already key-normalized and whitespace-canonical, making
  the expression deterministic for equal values on both the backup and the restored database.
  `md5(...)` is **prohibited** — a weak digest with no offsetting benefit here.
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
- **Composite decisions→runs case binding (rev 9, re-audit F2):** the shipped single-column FKs bind
  `decisions.case_id` and `decisions.run_id` **independently**, so after 013 a decision could still
  pair run `r1` (case `c1`) with case `c2` — the exact tuple the parity preflight classifies as
  `decision_case_ne_run_case`, silently recreatable post-migration. Therefore 013 also creates a
  **named unique target `uq_runs_id_case_id` on `runs(id, case_id)`** (before the FK that references
  it) and the **composite FK `fk_decisions_run_case`:
  `decisions(run_id, case_id) → runs(id, case_id)`**. `run_id` is nullable → MATCH SIMPLE semantics:
  a manual decision (`run_id NULL`) is exempt **by design**; every automatic decision is fully bound
  to its run's own case. Both are mirrored in `Run`/`DecisionRow` ORM metadata (`Run` gains its first
  `__table_args__`) and dropped in dependency-safe order on downgrade (FK before unique).
- **Exhaustive per-status XOR lifecycle CHECK (F4)** — not one-way fragments. With
  `claim := (claim_token, claim_lease_expires_at, claimed_by)`:
  - `status='pending'` ⇒ `delivered_at NULL AND resolved_at NULL` AND `claim` is **all-NULL or
    all-non-NULL** (a claimed pending row).
  - `status='delivered'` ⇒ `delivered_at NOT NULL AND resolved_at NULL` AND `claim` all-NULL.
  - `status='dead'` ⇒ `delivered_at NULL AND resolved_at NULL` AND `claim` all-NULL.
  - `status='superseded'` ⇒ `delivered_at NULL AND resolved_at NOT NULL` AND `claim` all-NULL
    **AND `kind='decision_callback'` (rev 9, re-audit F8): `superseded` is a DECISION-ONLY
    terminal** — production supersedes only decision callbacks, and A6's zero-send exception and
    the ordering proof are callback-specific, so a `poc_email` must never be able to enter
    `superseded` (not even by operator UPDATE; a superseded email would silently zero-send AND
    make the downgrade refuse). Spelled as the extra conjunct
    `AND (status <> 'superseded' OR kind = 'decision_callback')` in the same
    `ck_outbox_status_lifecycle` constraint. The schema-012 parity projection is unaffected
    (`superseded` already fails its pre-013 status vocabulary).
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
  be guessed** — fail closed; recovery is **restore-from-authoritative-backup or remain on 012 in
  `BLOCKED_NO_AUTHORITATIVE_MAPPING`** (§Rollout step 0; 014 is downstream and cannot repair this),
  never a `decided_at` fallback); (2) `decision_sequence = row_number() OVER (PARTITION BY decision.case_id
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
claim/HTTP/stamp gap across publishers; (c) **manual-current vs late automatic callback (rev 9,
re-audit F4)** — manual approval is platform-enforced inline (`ingest._handle_manual_approve`:
`run_id=NULL`, emits **no** callback, and **intentionally allocates no sequence** — do not distort
013 by sequencing manual rows), so a previously queued automatic callback can be delivered **after**
that manual approval: trigger = automatic decide enqueues its callback → the callback is not yet
processed → `reviewer.manual_approve` becomes the case's current state → the publisher claims the
queued callback and the local guard sees **no higher locally-published automatic sequence**
(manual has none) → the old automatic callback IS sent. All three are documented as expected
pre-activation and are turned into platform-high-water no-ops only in 014 (whose acceptance is
strengthened so an unaccepted pending/dead callback older than a manual-current platform source
cannot replace it). PR 6b may **build** on 7b-core's sequence primitive but
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

Digest-pinned drained cutover. **(0) Pre-window diagnostic, before any outage (rev-5 F2; rev-6 P2):**
the fail-closed backfill (§1) refuses an automatic decision without a surviving callback — a
**reachable** state, not only corruption: **on schema 012** retention *deletes* old **delivered**
outbox rows (`retention.py:29-35`, period at `config.py:138-139`) while their immutable decision rows
live forever. (013 removes `decision_callback` rows from that delete entirely — see the
durable-authority contract below — but the diagnostic runs **before** 013, against databases whose
history was already pruned under the delete semantics, so this recovery path ships and must be
correct regardless.)
Retention is a **one-shot transaction** (`retention.py:46-50`), so *disabling the schedule is not a
quiescence fence* — an invocation already inside `uow()` can delete a callback after a plain
`READ COMMITTED` diagnostic has passed and then reintroduce the mid-outage refusal. Therefore: (a)
**suspend the retention schedule AND terminate/wait for every active retention Job/process, with an
orchestrator-level zero-running attestation** before the diagnostic (disable alone is insufficient),
and keep it suspended through the cutover; (b) run the reviewed-image, digest-pinned **`python -m
kyc_tool.ops.verify_pr7b_core_backfill`** — raw parameterized SQL **compatible with schema 012**
(imports **no** 013-only ORM columns) that begins its transaction with **`LOCK TABLE outbox IN SHARE
MODE` before any `SELECT`** (defense in depth: the SHARE lock waits for any in-flight `DELETE`'s
`ROW EXCLUSIVE` to resolve and blocks a new outbox delete/write from invalidating the snapshot until
the diagnostic commits — bounded, and event/decision writers may briefly wait), then runs the *exact*
read-only 013 preflights at `READ COMMITTED` (every `manual=false` decision maps to exactly one
`decision_callback` by `run_id`; no orphan/duplicate; all case/run/kind identities valid), prints
actionable decision/run/outbox ids, and exits nonzero on any violation. The result is valid **only
while the schedule stays suspended and the zero-running attestation holds**. **On failure, abort here
— before stopping service** (no outage begun). **Recovery — restore-or-block (user-confirmed decision,
2026-07-23):** the only valid success path is to **restore the exact callback row from authoritative
backup and rerun the diagnostic clean**; otherwise **remain on 012 in `BLOCKED_NO_AUTHORITATIVE_MAPPING`**
(the exact sentinel — one token, no whitespace). **014 is downstream (`down_revision='013'`) and cannot
repair this** — no `014` command is a substitute. There is **no** pre-013 reconciliation unit in this
approved core design (the user considered and declined it). Backup availability is an **operator
prerequisite**, not a consequence of the retention setting. Never fabricate a callback, delete an
immutable decision, or fall back to `decided_at`. The CLI contract on this path: **nonzero exit, the
exact stable `BLOCKED_NO_AUTHORITATIVE_MAPPING` sentinel plus actionable decision/run ids, and no
writes**. **Restore acceptance contract (rev 10, re-audit F1 + rev-6 P1/P2)** — "restore the exact
callback" is an **executable identity requirement**, because the backfill's order authority is
`outbox.id` and a default-id INSERT silently re-ranks a pruned older callback as newer (the guard
would then suppress the actually-newer decision):

(a) record from the backup, per missing callback, the authoritative evidence tuple
`(decision_id, run_id, case_id, original_outbox_id, body_digest, original_status,
original_delivered_at)`. `body_digest` is computed **on the backup row** with the single
`callback_body_digest` expression defined in §1 — the same expression the predicate below and 014's
manifest use. `md5(payload_json::text)` is **prohibited**: a weak digest, and not the repo's
convention.

(b) the restore MUST `INSERT … (id, …) VALUES (<original_outbox_id>, …)` — a default-id INSERT is
prohibited; if the original id is unavailable, remain `BLOCKED_NO_AUTHORITATIVE_MAPPING`. The restore
MUST reinstate the **original lifecycle fields** (`status`, `delivered_at`, `resolved_at`, and the
claim tuple as recorded) — substituting `now()` for `delivered_at` is **not** an exact restore and is
prohibited (it both falsifies the audit record and, pre-rev-10, silently deferred re-pruning; see the
durable-authority contract below).

(c) **ACCEPTANCE PREDICATE — positive and fail-closed.** The pre-rev-10 formulation ("a SELECT of
mismatching rows must return zero rows") was **fail-open**: an absent row, or a row restored under
the wrong `run_id`, matched no rows and was therefore indistinguishable from an exact match, and the
recorded `decision_id` was never used at all. The predicate is now a **positive** assertion: exactly
**one** row must join `outbox` → `decisions` on the full recorded identity
(`o.id = original_outbox_id`, `o.kind='decision_callback'`, `o.case_id`, `o.run_id`, `d.id =
decision_id`, `d.case_id = o.case_id`, `d.run_id = o.run_id`) **and** match `body_digest` **and**
match the recorded lifecycle fields (`status`, `delivered_at`).
**Zero rows or more than one row BLOCKS** — there is no "no news is good news" path.
Each identity component is separately load-bearing (dropping any one must fail the acceptance test).

(d) **NO sequence mutation on this path.** The pre-rev-10 step ran
`setval(pg_get_serial_sequence('outbox','id'), GREATEST(max(id), last_value))` during the **pre-window
diagnostic**, i.e. while API/pipeline/outbox writers were still live (only retention is suspended at
§0; the first hard-stop is cutover step 2). That is a read-modify-write on a **non-transactional**
object: `max(id)` is evaluated against the transaction's MVCC snapshot and `last_value` is read at a
different instant from the write, so a concurrent `nextval()` in that window is invisible to both and
`setval` can **rewind the sequence below an already-allocated id**, producing a duplicate primary key
for the next writer. `LOCK TABLE outbox IN SHARE MODE` does **not** fence a sequence — sequence
functions are deliberately exempt from transactional semantics, and it is `ALTER SEQUENCE`, not a
`SELECT`, that excludes concurrent `nextval`/`setval`. **No sequence write ever happens with writers
live.** It is also unnecessary: the restored id is a retention-pruned *historical* id, so it fills a
gap **below** the already-advanced sequence and cannot collide. What replaces it is a **read-only
pre-window precondition**: read `last_value, is_called` from the sequence and assert that the id the
next writer would be handed is already **past** the restored id —
`last_value + (CASE WHEN is_called THEN 1 ELSE 0 END) > original_outbox_id` — and **abort before the
outage** if it does not hold. (`is_called` is load-bearing: on a never-called sequence `last_value`
is the id `nextval` will *return*, not one already consumed.) The check is durable because that
quantity is monotonically non-decreasing under `nextval` — once it passes with writers live it stays
true. If a genuine sequence repair is ever required (the restored id is **not** below the high-water
mark — not a prune, but a restore from a divergent lineage),
it is a **separate drained cutover action**, performed only after every writer is hard-stopped and
attested zero at the orchestrator, using a sequence-serializing operation that excludes concurrent
`nextval`/`setval` (`ALTER SEQUENCE … RESTART WITH …`), with the resulting value **read back** before
any writer restarts. Never claim a table lock fences a sequence.

(e) only then rerun the diagnostic clean.

The exact runbook text + SQL and the schema-012 recovery acceptance test live in the build plan
(Task 9 Step 5 / Task 7 Step 4b); the frozen `v013_backfill.py` contract itself is unchanged. Either
abort branch must **explicitly re-settle retention** (keep it frozen while restoring/rerunning, or
re-enable it if the cutover is deferred) — never leave a compliance process silently disabled. On
success proceed.

**Durable ordering authority — retention no longer prunes decision callbacks (rev 10, re-audit
P1/F4).** The restore path above repairs *history*; this clause prevents *recurrence*, and it is the
storage/ownership decision 014 depends on. Before rev 10 an exact restore was self-defeating: it
reinstates the original 7-year-old `delivered_at`, so `retention.py:29-35`
(`status='delivered' AND delivered_at < now() - interval`) re-deleted the row on its very next run,
and the tool then had no way to derive 014's per-callback `(body_digest, local_status)` from the
immutable `decisions` row. 013 therefore establishes the **`outbox` row itself** as the durable
authority for a decision callback's identity, order, body, and local status:
- **Retention's outbox prune is narrowed to `kind='poc_email'`.** That is the whole change — one
  added predicate, no new column, no backfill, no second store, therefore no drift class to guard.
  A delivered `decision_callback` row survives indefinitely with `id` (the order authority),
  `case_id`, `run_id`, `decision_sequence`, `status` (= `local_status`), `delivered_at`, and
  `payload_json` intact. Every other retention target is unchanged.
- **It retains nothing new.** The callback body is a *projection* of the `decisions` row (decision,
  score, the five gate booleans, `buy_enablement`, checks summary), and `decisions`, `checks`, and
  raw evidence are already deliberately never pruned — they *are* the decision record
  (`retention.py` module docstring). The genuinely sensitive outbox body is the POC email's raw
  token, which is destroyed twice over: redacted at delivery (`publisher.py:176-182`) and still
  pruned on schedule. So no redaction of callbacks is warranted, and none is performed.
- **014's candidate manifest is built from this authority**, not from an undefined "agreed universe"
  and not from a row that may have been pruned. `local_status` is read directly from `outbox.status`
  and the digest from `payload_json` via §1's `callback_body_digest`; there is no second
  representation to drift against and no dual write to keep consistent.
- Rows grow 1:1 with `decisions`, which is already unbounded by design.
The alternatives were considered and rejected: a separate authority table duplicates
identity/status into a second row and introduces exactly the drift class this PR exists to eliminate;
a stored `body_sha256` column does the same for the body (see §1);
"retain everything until 014 bootstraps" is a temporal band-aid that reopens the hole afterwards; a
signed manifest universe that *excludes* history would require an explicit product decision and a
proof that exclusion cannot admit stale platform state, which this unit does not have. (1) pause submission, edge-block
composer, disable autoscaling/restarts; (2) hard-stop API/pipeline/outbox/`dev_worker` (queue **and**
outbox, `dev_worker.py:128-139`)/retention/every writer, attest zero at the orchestrator; (3) shipped
`python -m kyc_tool.ops.requeue_interrupted_jobs` (decrements attempts so a killed final attempt
retries) —
**no outbox reset here** (F1): the pre-013 schema has no claim columns, and an old in-flight outbox
claim is encoded only in `next_attempt_at` and cannot be distinguished from legitimate backoff.
**Preserve every pending row's `next_attempt_at`**; an interrupted old claim simply waits until its
already-recorded due time (bounded by the documented old lease); (4) `alembic upgrade head` (013) —
which **repeats the §0 preflights under the zero-writer boundary** and remains the authoritative
fail-closed check (the pre-window diagnostic is an early detector, not a substitute);
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
  **locally-stamped** delivery; send-before-stamp, cross-replica, and manual-current-then-late-
  automatic-callback reverts (§5) remain until 7b-activation. `superseded` is a decision-callback-
  only terminal (DB-enforced).
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
  seeded `superseded` row** (F3). (There is deliberately **no migration fixture** for the per-case
  UNIQUE — a 012 DB has no `decision_sequence` column and 013's own `row_number()` cannot produce a
  per-case collision; that constraint is proven by the runtime INSERT/UPDATE negatives above, not by a
  migration seed.) Mutation-removing any
  CHECK/index/FK/`SET NOT NULL` fails.
- **Backfill order authority (rev-4 F1) — two connections, real Postgres:** start B's decide
  transaction (fixing `B.decided_at`) and pause it **before** the case lock; start A later, lock the
  case, insert decision + callback, commit; release B to lock, insert, commit. Assert `B.decided_at <
  A.decided_at` **while** `A.outbox_id < B.outbox_id`; upgrade 013; assert **A=seq 1, B=seq 2,
  counter=2**, and delivering both leaves **B** as the platform's last callback with **neither**
  superseded. Mutation: ordering the backfill by `decided_at,id` reproduces B's erroneous suppression
  and **must fail**. Separately, seed a valid `manual=false` decision with **no** surviving callback
  row on 012 and prove the migration **refuses byte-stably** (no `decided_at` fallback).
- **Restore acceptance (rev 9, F1) — schema 012, real CLI + real migration:** seed two callbacks
  (ids captured), record the evidence tuples, retention-prune the OLDER id; prove a **default-id
  INSERT restore is rejected by the acceptance predicate** (id mismatch); restore the **exact
  original id** → predicate passes (zero mismatches), the id sequence is advanced per the runbook,
  the real diagnostic exits 0, the real 013 upgrade maps old/new to sequences **1/2**. Mutation:
  the default-id restore accepted anywhere reverses the mapping to 2/1 and **must fail**.
- **Composite case binding (rev 9, F2):** direct INSERT **and** UPDATE negatives with two real
  cases/runs and otherwise-valid rows (no other constraint can mask the check) must fail at commit
  with the exact name `fk_decisions_run_case`; mutation-removing **only** that FK makes both pass
  (test fails). ORM/live parity: `fk_outbox_case_id`, `fk_outbox_decision_triple`,
  `fk_decisions_run_case`, `uq_runs_id_case_id` asserted in BOTH `__table__` metadata and the live
  inspector.
- **Superseded is decision-only (rev 9, F8):** INSERT and UPDATE negatives prove a `poc_email`
  cannot enter `superseded` even with an otherwise-valid superseded shape (assert
  `ck_outbox_status_lifecycle` by name); every fixture needing a superseded row builds a valid
  automatic decision+callback chain; mutation-removing only the kind conjunct **must fail** both.
- **Manual-current residual (rev 9, F4):** a REAL ingest → worker → manual-approve → publisher test
  (`…_expected_pre_activation` in its name) drives an automatic decision whose callback is enqueued
  but unprocessed, ingests `reviewer.manual_approve`, then runs the publisher and asserts the old
  automatic callback **IS sent** (HTTP happened) — pinning residual (c) of §5 as expected behavior.
- **Pre-window diagnostic (rev-5 F2):** on real Postgres/**schema 012**, seed a decision + delivered
  callback, run the **real retention function** until the callback is pruned (decision survives), and
  assert `verify_pr7b_core_backfill` exits **nonzero with both ids** while the service can stay on 012
  (no outage). Cover healthy / duplicate / orphan / missing mappings. Mutation removing the
  retention-freeze-then-diagnostic step from the rollout contract **must fail**. Keep the 013
  migration-refusal test as defense in depth (the two share the predicate).
- **Retention-race — DB-lock half (rev-6 P1; the *automatable* proof):** two connections, real retention
  seam: pause A **after** its callback `DELETE` but **before commit**; start the real CLI in B; assert B
  **cannot report green** (its `LOCK TABLE outbox IN SHARE MODE` waits on A). After A **commits**, B
  returns **nonzero** with the decision/run ids; repeat with A **rolling back** and require **green**.
  Mutation **removing the table lock must fail**. Exercise the real CLI entry point, not a helper. **The
  orchestrator "zero active retention tasks *before* it starts deleting" half is NOT provable in pytest**
  (retention is a scheduled one-shot with no in-repo liveness registry) — it is a **deployment/runbook
  acceptance** (§Docs): the exact `TODO(integration)` orchestrator command + output proving zero
  running tasks. The two halves are distinct evidence; do not let a helper-only test pretend to prove
  the external attestation.
- **Sequence is never rewound while writers are live (rev-10 P1):** two connections on schema 012.
  B calls `nextval` on `outbox_id_seq` (reserving the next id) and pauses **without committing**; the
  restore path then runs in A. Assert A performs **no** sequence write and that the sequence is not
  rewound; after B commits its insert, the **next** insert must receive a **strictly newer** id than
  B's — no duplicate primary key. Assert the read-only precondition
  `original_outbox_id <= last_value` passes for a pruned historical id and **aborts pre-window** when
  it does not. **Mutation:** restoring the pre-rev-10 live
  `setval(GREATEST(max(id), last_value))` must **fail** this test. Run this test *first*, then the
  migration/CLI matrix, then the full suite.
- **Restore acceptance is fail-closed (rev-10 P2):** the positive single-row join must **block** on
  each of — row absent, wrong `run_id`, wrong `decision_id`, wrong `case_id`, wrong
  `original_outbox_id`, wrong body (digest mismatch), wrong lifecycle field
  (`status`/`delivered_at`), and duplicate evidence rows (>1 match). The one exact row must pass.
  **Mutation:** dropping each joined identity component **separately** must make a wrong-restore case
  pass. Exercised through the **real** diagnostic CLI and the **real** 013 upgrade, not a helper-only
  assertion.
- **Restore durability under retention (rev-10 P1/F4):** restore an **actually retention-old** row
  with its **original** `delivered_at` (not `now()`), then run the **real** retention job again.
  Assert the row **survives** with `id`/`case_id`/`run_id`/`status`/`delivered_at`/`payload_json`
  intact, and that 014's manifest entry
  `(case_id, run_id, decision_sequence, callback_body_digest, local_status)` is still exactly
  derivable from it. Assert in the same run that an equally-old delivered **`poc_email`** row IS
  deleted, so the narrowing is proven to be a narrowing and not a disablement. **Mutation:** drop the
  `kind` predicate from the retention DELETE → the callback assertions must fail.
  `audit_log`/`poc_tokens` pruning is asserted unchanged.
- **No-backup blocked state (rev-6 P2; user-confirmed restore-or-block):** a missing mapping with **no**
  restorable backup makes the real CLI exit **nonzero** with the **exact** `BLOCKED_NO_AUTHORITATIVE_MAPPING`
  sentinel (asserted literally) + actionable decision/run ids, **before** maintenance, leaving
  schema/data **unchanged**; the test proves **no existing `014` CLI/module** is invoked as a substitute
  (7b-activation is downstream of 013).
- **Stream separation:** a perpetually-failing POC email never blocks the case's decision callback.
- **Sequence allocation + per-case uniqueness (F1):** two concurrent decides on one case → strictly
  increasing unique sequences, counter=max. Direct **INSERT and UPDATE** negatives: **two distinct
  valid runs/decisions for the same `case_id` at the same positive `decision_sequence` fail at commit**
  (backstopped by `uq_decisions_case_decision_sequence`, **not** the outbox partial index), while
  multiple manual NULL-sequence decisions stay legal; build matching valid outbox rows so the duplicate
  cannot hide behind another constraint. Mutation removing **only** `uq_decisions_case_decision_sequence`
  must make this fail. (No migration fixture for this — a 012 DB has no `decision_sequence` column and
  013's `row_number()` cannot produce a per-case duplicate; the runtime negatives are the proof.)
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
best-effort), `RUNBOOK.md`/`DEPLOYMENT.md` — the **step-0 pre-window contract spelled out in full, not
abbreviated to "freeze" (rev-7 P3):** (i) **suspend the retention schedule**, (ii) **terminate/wait for
every active retention task**, (iii) **capture target-orchestrator zero-running evidence** — the exact
ECS/Fargate or EC2 command + expected output; if the production substrate is not yet chosen, this is a
blocking **`TODO(integration)`** deployment prerequisite (a pytest does **not** prove it), (iv) run the
digest-pinned `verify_pr7b_core_backfill` diagnostic with the schedule still suspended, (v) on **every**
abort path explicitly re-enable or deliberately keep-frozen retention; plus the **restore-or-block**
recovery (restore from authoritative backup, else `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012 — backup
availability is an **operator prerequisite**), the drained cutover **without** a pre-013 reset, the
post-013 `reset_interrupted_outbox_claims` CLI, and reversible-before-first-supersession rollback +
preflight query. `AUDIT_FINDINGS.md` (the **A6 exception** + backfill = deterministic reconstruction +
the local/cross-replica boundary), `.agents/ROADMAP.md` (the split + renumber, this commit; "reversible"
→ "reversible before first local supersession"; the restore-or-block prerequisite). No ADR needed for
core; ADR-008 covers 7b-activation.

## Out of scope (YAGNI)

- No wire `decision_sequence`, no platform contract, no bootstrap/phase machine, no
  `integrity_mismatch` terminal (all 7b-activation). No email-semantics change beyond stream
  separation. No enforcement/scoring/`ENGINE_BUILD_ID` change; M2 untouched. PR 7a fences the **jobs**
  queue separately; 7b-core fences its own **outbox** claim.

## Revision note — rev 9 (2026-07-24)

Amendments folding the four spec-affecting findings of the Codex complete-unit re-audit @ `eac3035`
(`AGENT_BUS.md` PLAN-REVIEW 2026-07-24); the plan (rev 5) carries the executable blocks:

- **F1 — Restore acceptance contract (§Rollout):** the restore-or-block recovery is now an
  executable identity requirement — original `outbox.id` + recorded evidence tuple + acceptance
  predicate (zero mismatches) + sequence advance; default-id INSERT prohibited; original id
  unavailable ⇒ remain `BLOCKED_NO_AUTHORITATIVE_MAPPING`. The frozen `v013_backfill.py` contract
  is unchanged.
- **F2 — Composite decisions→runs case FK (§1):** `uq_runs_id_case_id` + `fk_decisions_run_case`
  (MATCH SIMPLE — manual rows exempt by design), ORM-mirrored, dependency-safe downgrade order.
- **F4 — Third residual (§5, §Invariants):** manual-current vs late automatic callback documented
  with its trigger sequence + a real end-to-end expected-pre-activation test; 013 still allocates
  no sequence for manual rows; 014's acceptance is strengthened against it (activation spec rev 2).
- **F8 — `superseded` is decision-only (§1):** the lifecycle CHECK's superseded branch additionally
  requires `kind='decision_callback'`; negatives + fixtures updated accordingly.

## Revision note — rev 10 (2026-07-24)

Folds the three core-owned findings from Codex's rev-5 complete-unit re-review (`0ca264b`). Rev 9's
ten accepted controls are **unchanged and not reopened**; these are additive corrections to the
recovery path plus one new durable-storage contract. The fourth finding is activation-owned
(receiver authority) and lands in the activation spec.

- **P1 — Sequence rewind during the pre-window restore (§Rollout (d)).** Step 0 runs *before* the
  outage — only retention is suspended, so API/pipeline/outbox writers are live until cutover step 2.
  `setval(GREATEST((SELECT max(id) FROM outbox), (SELECT last_value FROM outbox_id_seq)))` is a
  read-modify-write on a **non-transactional** object: `max(id)` sees only the txn's MVCC snapshot and
  `last_value` is read at a different instant from the write, so a concurrent `nextval()` is invisible
  to both and the sequence can be rewound below an already-allocated id → duplicate primary key. The
  diagnostic's `LOCK TABLE outbox IN SHARE MODE` does **not** fence a sequence. **Resolution:** the
  live `setval` is **deleted**, not hardened — an exact restore of a retention-pruned historical id
  fills a gap *below* the advanced sequence and never needed a sequence write. Replaced by a
  read-only, monotonic, abort-before-outage precondition (the next id the sequence would hand out,
  `last_value + (is_called ? 1 : 0)`, is already **past** the restored id), with
  genuine sequence repair relegated to a separate **drained** cutover action using a
  sequence-serializing operation with read-back.
- **P2 — The acceptance predicate was fail-open (§Rollout (c)).** "Zero mismatch rows = accepted"
  made an **absent** row and a row restored under the **wrong `run_id`** indistinguishable from an
  exact match, and never used the recorded `decision_id` at all. **Resolution:** one **positive**
  assertion — exactly one row joining `outbox` → `decisions` on the full recorded identity, matching a
  lowercase SHA-256 over `payload_json::text` and the **original** lifecycle fields.
  Zero rows or >1 blocks. `md5(...)` is prohibited (weak, and not the repo convention);
  substituting `now()` for `delivered_at` is prohibited.
- **P1 — The restore had no durable authority for 014 (§1, §Rollout).** An exact restore reinstates
  the original 7-year-old `delivered_at`, so `retention.py:29-35` re-deleted the row on its next run;
  the plan's `delivered_at=now()` hid this. 014's manifest then had no source for per-callback
  `(body_digest, local_status)`. **Resolution — the `outbox` row *is* the durable authority:**
  retention's outbox prune is narrowed to `kind='poc_email'`, so a delivered `decision_callback` row
  is never deleted and keeps id/case/run/sequence/status/body. Chosen over a separate authority table
  and over a stored `body_sha256` column (both duplicate an existing fact into a second
  representation and create the very drift class this PR eliminates) and over "retain everything
  until 014 bootstraps" (a temporal band-aid that reopens the hole afterwards). It retains no new
  data: the callback body is a projection of the never-pruned `decisions`/`checks` record.
  A signed manifest universe that *excludes* history was **not** taken — it would require an explicit
  product decision and a proof that exclusion cannot admit stale platform state.

Scope discipline: core and activation stay separate, scoring is untouched, M2 and
`KYC_Tool_Build_Package/` are untouched.
