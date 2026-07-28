# PR 6b — Revalidation under the pinned engine (item 7B) — design (rev 5)

> **PAUSED / SUPERSEDED ORDERING — re-audit `4dfdf8a` F6 (2026-07-27).** This spec predates the
> PR-7b split and reordering. The user reordered PR 7b BEFORE PR 6b (7b-core supplies the
> callback-ordering primitive 6b's revalidation coordinator rests on), and the migration chain has
> since become: `013` 7b-core, `014` witness repair, `015` witness-authority hardening,
> `016` witness admission, `017` authority boundary (all shipped), `018` 7b-activation (pending).
> **PR 6b is now migration `019` (`down_revision='018'`)
> and builds AFTER activation** — its coordinator consumes activation's convergence contract
> (greatest per-case platform-acknowledged sequence), and activation itself carries three OPEN
> blockers recorded as O1/O2/O3 in the activation spec (rev 9) that must be resolved before this
> unit is re-planned. Every `migration 013` / `down_revision='012'` / "6b lands before 7b" claim
> below is historical text from the pre-split ordering — superseded by this banner and by
> `.agents/ROADMAP.md §C`, which is canonical. The validation-boundary DESIGN content (validator
> build id, snapshot codec, replay binding, closure ledger) remains the accepted basis for the
> future re-plan.

Codex rev-4: *"Rev 4 materially closes all five rev-3 findings … the validation/replay core
is coherent."* Rev 5 folds the five remaining **concurrency/ordering** findings: the
coordinator scorer is now **unconditionally fail-closed** (F1); per-case callback recovery is
**serialized until PR 7b** exists (F2); the **batch row is the verified idempotency authority**
(F3); the closure successor carries **full PR-6 provenance + a governed reason** (F4); and the
freshness readiness contract is **split per surface** (F5, workers have no `/readyz`).

## Context

PR 6 re-prices every live check under a run's pinned bundle but leaves PASS/FAIL as judged. A
check's PASS/FAIL is a **validator-code** property spanning `validators/*`, the dispatch, the
DB→`extras` normalization, the review-guard eligibility, and the new snapshot codec + replay
binding — one pure **validation boundary** (`VALIDATOR_BUILD_ID`). Two axes: **bundle →
pricing** (PR 6, live) and **validator → PASS/FAIL trust** (PR 6b: fail-closed on
validator-stale PASSes + deterministic replay + a one-time closure of the legacy backlog).

**Cross-PR note (F2):** PR 6b lands before **PR 7b** (`decision_sequence` high-water mark +
outbox stream separation). Until 7b, 6b must not assume callback ordering; it enforces its own
**at-most-one unresolved revalidation coordinator per case** and per-case delivery witnesses.
When 7b ships, its high-water contract subsumes this interim serialization.

## Decisions

1. **Fail-closed** on validator-stale PASSes (ephemeral projection, no row mutation).
2. **Two identifiers**: `ENGINE_BUILD_ID` `eng-1`→**`eng-2`** (runs/decisions); new
   `VALIDATOR_BUILD_ID = "val-1"` on **checks**; staleness keys on it.
3. **NULL provenance is unknown ⇒ stale**, never relabeled/backfilled; the legacy PASS
   backlog is **closed**.
4. Deterministic replay from a versioned, **canonical-digest-verified** snapshot.
5. Revalidation is a per-case **coordinator run**, dispatched + idempotency-keyed off a
   **durable batch row**, scored under an **unconditional** validator-freshness projection.
6. **One hashed validation boundary** guards `VALIDATOR_BUILD_ID`.
7. A **dedicated `enforce_validator_freshness` flag**, independent of `enforce_bundle_pinning`.
8. Build the full writer now (replay lane + closure lane).

## Architecture

### 1. Validation boundary + guard (unchanged)

A cohesive **pure** `validation_engine/` surface (validators, dispatch, pure `extras`
normalization, pure review-guard eligibility, snapshot codec, replay-binding), import-linter
pure. `VALIDATOR_BUILD_ID` pinned by a **framed transitive-closure hash** (fallback: an
explicit test-proven manifest). Mutation-tested against validator / review-guard / extras
decoder / replay-binding edits.

### 2. Data model — migration 013 (`down_revision='012'`)

- **`checks.validator_build_id TEXT NULL`** + nonblank `CHECK … NOT VALID` (existing = NULL =
  unknown). `CheckView` gains `validator_build_id`.
- **`run_validation_snapshots`** (append-only): `run_id PK`, `snapshot_json JSONB`,
  `snapshot_digest TEXT` (`CHECK ~ '^[0-9a-f]{64}$'`), `captured_at`.
- **`revalidation_batches`** — the coordinator **idempotency + delivery authority** (F3/F2):
  `coordinator_run_id TEXT PK → runs.id`, `case_id TEXT NOT NULL → cases.id`,
  `target_validator_id TEXT NOT NULL`, `expected_checks JSONB NOT NULL` (typed codec: sorted,
  unique, nonempty `[(check_id, created_by_run_id)]`, explicit NULL encoding),
  `batch_digest TEXT NOT NULL CHECK (~ '^[0-9a-f]{64}$')`, and lifecycle
  `state TEXT` (`open|scored|delivered|superseded`), `delivered_at timestamptz`, counters
  (`replayed`/`closed`/`skipped`), `created_at`/`resolved_at`.
  **`UNIQUE(case_id, target_validator_id, batch_digest)`.** Request columns
  (case/target/expected/digest) are **immutable**; only lifecycle state/counters transition
  (so *not* literally append-only). Dispatch + idempotency key off **this row under the Case
  lock**, never a caller-supplied event payload.
- **`engine_activation_epoch`** (append-only *pair* history, distinct from PR 6's immutable
  `bundle_pinning_epoch`): `id` PK, `engine_build_id`, `validator_build_id`, `activated_at`.
- Forward-only-after-use downgrade. Lineage guard: PR 6b = `013`, `shipped`.

### 3. Identifiers + flags + readiness (F5)

`ENGINE_BUILD_ID = "eng-2"` (whole-tree guard re-pinned with the bump);
`VALIDATOR_BUILD_ID = "val-1"` (validation-closure guard, §1).

**`Settings.enforce_validator_freshness: bool = False`** — **independent** of
`enforce_bundle_pinning` (unchanged from PR 6). When **on**, the DB active `(engine,validator)`
pair must equal the local `(ENGINE_BUILD_ID, VALIDATOR_BUILD_ID)` before a process serves or
claims — enforced **per surface**: each **API** replica via `/readyz` (pair echoed in body);
each **pipeline/dev worker** via a structured `validator_freshness_ready` attestation emitted
**after** the DB pair comparison and **before** constructing/starting the `Worker` (and, for
dev, the publisher) — on mismatch the worker constructs/starts **nothing** and claims no job.
(`outbox`/`retention` stay freshness-independent.)

### 4. Snapshot codec + canonical digest (unchanged)

`ValidationSnapshotV1` (Pydantic JSON primitives). One shared codec: validate → UTC-normalize
timestamps (one textual form) → dump `sort_keys=True, separators=(",",":"),
ensure_ascii=False, allow_nan=False` → UTF-8 → **SHA-256 lowercase hex** = `snapshot_digest`.
Store parsed object + digest; on load validate + re-encode + **compare before replay** (fail
closed on mismatch/unknown version/type). Fixed **test vector** (key-order, JSONB round-trip,
`+00:00` vs `Z`, non-ASCII). `ValidationContext` gains `evaluated_at`; validators read it.

### 5. One snapshot-capture seam (unchanged)

A single builder+persister for **every** validator-produced-check path — VALIDATE **and** the
broker-blocked DECIDE short-circuit; guard/`evaluated_at` computed once and fed to both intent
creation and persistence in the fused transaction. Live path byte-identical.

### 6. Provenance stamping

Thread `validator_build_id` through the checkstore writers; runs/decisions stamp `eng-2`.

### 7. Fail-closed projection + the parameterized fused scorer (F1)

Pure `validator_fresh_views(views, target_validator_id)`: each view whose
`validator_build_id IS DISTINCT FROM target` is **downgraded PASS→NEEDS_REVIEW** (0 points;
reason_codes retained → stale hard-conflict still fires gate 5; non-PASS untouched). The
**extracted fused scorer takes an explicit `freshness_target: str | None`** (F1):
- an **ordinary** run passes the local validator **only when `enforce_validator_freshness` is
  on** (else `None` → no projection → flag-off no-op);
- a **revalidation coordinator ALWAYS passes `batch.target_validator_id`**, **independent of
  the rollout flag**, after requiring it to equal the local `VALIDATOR_BUILD_ID`.

Applied to a **re-read of all live views under the Case lock, after staging/skips and before
score/gates/callback** — so a coordinator never emits a callback that counts a replacement
stale PASS, even during flag-off closure. Also invoked (flag-gated) in `_handle_manual_approve`
(raw-views ORG-ID gap, F5-rev2; Settings passed explicitly into `ingest`); a stale ORG-ID PASS
⇒ `approved_manual` + `buy_locked_org_id_required`. Only stale **PASS** rows change a decision
or block activation.

### 8. Revalidation writer — coordinator run, batch-authoritative (F1–F4)

`ops.revalidate_backlog` scans for cases with a validator-stale live **PASS**. Per case, a
**shared internal-ingest primitive**, under **`Case FOR UPDATE`**:
- computes the **typed canonical expected-set** + `batch_digest`; **creates-or-reads the
  `revalidation_batches` row as the idempotency authority** (F3) — an event-key collision is
  treated as a retry **only** when this `(case_id, target, batch_digest)` row matches; a
  changed set is a **new** coordinator; **at most one unresolved coordinator per case** (F2)
  — refuse to create a new one while a prior batch for the case is not `delivered`;
- allocates the **same gap-free `event_sequence`** and atomically writes the internal
  `recalculate.requested` **Event** (explicit internal actor; the event key
  `revalidate:<target>:<batch_digest>` is an **audit label**, not the authority — F3), the
  coordinator **Run** (own creation-pin), the **batch** row, and the **job**.

The pipeline handles the job under `Case FOR UPDATE`, **requiring `batch.target_validator_id
== VALIDATOR_BUILD_ID` at job entry** (F3), in one fused commit:
1. Reselect each expected stale live PASS by id + `created_by_run_id`; skip if a newer run
   replaced it (surgical).
2. **Replay lane** (source run has a digest-verified snapshot): rebuild the context from
   immutable evidence + snapshot, run the boundary `build_intents` under the **target
   validator**, **re-verify the immutable binding** (POC id/digest/tuple, consumption-as-proof,
   expiry at snapshot time; website terminal task case/type/reviewer, no `open` requirement).
   Successor `created_by` the coordinator, `policy_bundle_hash` = the **source** run's pin,
   `validator_build_id` = target. Tampered ⇒ FAIL/stale.
3. **Closure lane** (no snapshot / context unprovable — F4): supersede the PASS with a **fully
   provenanced** successor — status `NEEDS_REVIEW`, 0 points, **category from the
   coordinator's resolved rubric**, `policy_bundle_hash = coordinator_run.policy_bundle_hash`,
   `validator_build_id = target`, `source = "revalidation_closure"`, `source_detail =
   {batch, coordinator run, source run/check ids, snapshot-failure class}`, reason
   **`ReasonCode.HISTORICAL_VALIDATION_CONTEXT_UNAVAILABLE`** (a new governed enum, in
   ADR-006 + `AUDIT_FINDINGS`). No PR 6 post-epoch NULL-provenance alert fires on it.
4. Apply §7's **unconditional** coordinator projection to a lock-held re-read of live views,
   then **score under the coordinator run's creation pin** → decide → project → **enqueue the
   outbox callback** → set batch `state=scored` → complete the job → revalidation audit record.

Extract a reusable fused **`score→decide→project→outbox` primitive** (do **not** call
`_decide_txn` on a complete source run). The coordinator is a real run ⇒ a **fresh `run_id`**
callback (not deduped). On successful delivery the publisher sets the batch `delivered_at`/
`state=delivered` (the witness §9 checks). Idempotent via the batch authority.

### 9. Activation — pair CAS + delivery convergence + per-case ordering (F8, F3-rev3, F2)

`ops.activate_revalidation_epoch --expect-current-engine <eng|none> --expect-current-validator
<val|none> --expect-engine eng-N --expect-validator val-N`. Under a **transaction-scoped
lock**: (a) current max pair == expected-current; (b) local constants == target; (c) the
**security gate** — zero validator-stale live **PASS** rows **AND** every case's **latest**
revalidation coordinator batch is `state=delivered` with a non-NULL matching
`decisions.published_at` and **no pending/dead** callback (a dead callback is a **hard
blocker** → requeue in order, never a bypass). Then insert the next pair + read back. Same
target ⇒ idempotent; expected-predecessor→new ⇒ allowed (a future `eng-3/val-2` is not blocked
by `eng-2`); other predecessor / race ⇒ refuse without a write. Recovery **refuses to requeue
an older coordinator once a newer decision for the case delivered** (F2); the bundle epoch stays
immutable. Alert: post-epoch live **PASS** checks whose `validator_build_id IS DISTINCT FROM`
the active validator.

### 10. Executable drained cutover (F2/F5)

1. Build + publish + **pin the reviewed image by digest**.
2. **`alembic upgrade head` (013) BEFORE starting the new image.**
3. **Pause all event submission**; disable autoscaling/restarts.
4. **Hard-stop + confirm zero** old API + pipeline + dev-worker processes; `requeue_interrupted_jobs`.
5. Start **only** the pinned image, `enforce_bundle_pinning` **unchanged**, freshness **off**;
   drain ordinary work.
6. `ops.revalidate_backlog` (closure) → **start/drain the outbox publisher** in the window;
   report undelivered case/run ids until every latest per-case batch is `delivered`.
7. `ops.activate_revalidation_epoch` pair-CAS — passes only under §9's gate (checks **and**
   ordered delivery).
8. Roll API+worker processes with freshness **on**; **certify each surface** (F5): API via
   `/readyz` (pair in body), workers via the `validator_freshness_ready` attestation, before
   resuming.
Rollback: freshness-off on the PR6b image; never delete epoch/snapshot/batch history.
(Zero-downtime per-writer fencing is out of scope; the drained window is the contract.)

### 11. Docs / governance

**ADR-006** — revalidation (axes, fail-closed + parameterized scorer, snapshot codec/digest,
batch-authoritative coordinator + internal `recalculate.requested` + interim per-case
serialization pending PR 7b, closure provenance + the new reason code, pair activation +
delivery convergence, drained cutover). **PR 10 → ADR-007** in ROADMAP §I **and** the PR 10
detailed section (done); ROADMAP guard extended to assert both agree. `DEPLOYMENT.md` §10,
`RUNBOOK.md`, ROADMAP PR 6b → `shipped`/`013`, `AUDIT_FINDINGS` (internal event + closure
reason).

## Invariants

- NULL validator provenance is stale/unknown; never relabeled/backfilled.
- `enforce_validator_freshness` is separate; `enforce_bundle_pinning` unchanged. Flag-off
  (both) is a strict scoring no-op; golden cases byte-identical.
- The fused scorer is **unconditionally** fail-closed for a coordinator (its `freshness_target`
  is `batch.target`, flag-independent); ordinary runs project only when the flag is on.
- Batch request columns are immutable and the **verified idempotency authority**; only lifecycle
  state/counters mutate; `target_validator_id == VALIDATOR_BUILD_ID` required at job entry.
- **At most one unresolved revalidation coordinator per case** until PR 7b; recovery never
  delivers an older decision after a newer one.
- Activation requires zero stale live PASSes **and** every latest per-case coordinator batch
  delivered.
- The closure successor carries full bundle+validator+source provenance and a governed reason.
- Revalidation runs under `Case FOR UPDATE`, never clobbers a newer check; replay uses only
  immutable evidence + the digest-verified snapshot; zero adapter/network calls.
- `ENGINE_BUILD_ID=eng-2`, `VALIDATOR_BUILD_ID=val-1`, both guards re-pinned in-commit.
  `KYC_Tool_Build_Package/` + M2 untouched; validation boundary import-pure (2/0).

## Testing strategy (incl. every rev-4 named regression)

- **Coordinator fail-closed (F1):** replace expected A with stale B at the barrier ⇒ the
  coordinator's decision + callback assign B zero points / needs-review even with the flag
  **off**; a changed-set batch then closes B; mutate away the coordinator override ⇒ failure;
  ordinary flag-off golden no-op retained.
- **Per-case ordering (F2):** dead-letter A, attempt changed-set B ⇒ waits/refuses; requeue +
  deliver A, then allow + deliver B; a forced requeue of A after B ⇒ 409 / mark-superseded,
  never sends. Via the real `OutboxPublisher` + admin recovery seam.
- **Batch authority (F3):** a public `recalculate.requested` preempting the deterministic key ⇒
  the internal batch still creates safely or refuses loudly (never reuses the public run);
  tamper target/expected-set/digest/event-key independently ⇒ job fails, zero writes; two real
  creators converge on one batch/run.
- **Closure provenance (F4):** close a NULL-provenance pre-6b PASS ⇒ successor has complete
  bundle+validator+source provenance, 0 points, the stable reason in the callback, and no PR 6
  post-epoch NULL alert.
- **Worker readiness (F5):** call the real worker builders/mains with a mismatched pair ⇒ no
  `Worker`/`OutboxPublisher` constructed, no thread, no job claimed; matching startup emits
  flag/engine/validator/image identity. API `/readyz` echoes the pair.
- **Core (carried):** migration 013 up/down + forward-only + `NOT VALID`; validation-closure
  guard mutation set; canonical-digest vector + tamper-fail-closed; broker-blocked capture
  seam; manual-approve freshness lock; backlog closure of a pre-PR3 permissive NULL PASS (no
  retro-stamp); replay determinism (consumed/`done`/aged POC+website ⇒ identical PASS; tampered
  ⇒ stale); document both run orders; cascade `needs_review` placeholder doesn't block;
  activation CAS matrix; concurrency barrier + fault rollback; flag matrix; ADR docs test.
- Full suite green on real Postgres; ruff clean; import-linter 2/0.

## Out of scope (YAGNI)

- No adapter re-fetch; no retro-stamp/backfill of pre-6b provenance; no zero-downtime
  per-writer epoch fence; no multi-engine runtime beyond the two-identifier + fail-closed hook;
  no re-judging of the manual *decision* itself (only its buy-enablement freshness); no public
  revalidation route/UI. PR 7b's `decision_sequence` high-water mark is **not** built here —
  6b uses interim per-case coordinator serialization until 7b lands.
