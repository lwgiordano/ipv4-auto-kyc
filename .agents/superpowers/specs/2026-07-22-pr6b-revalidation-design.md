# PR 6b — Revalidation under the pinned engine (item 7B) — design (rev 4)

Codex rev-3 review: *"the validation/replay core is now coherent"*. Rev 4 folds the five
remaining rev-3 findings — all **operational contracts** around a now-settled core: a
**separate freshness flag** (F1), an **executable drained cutover** with a real fence
(F2), **delivery-convergent activation** (F3), a **durable coordinator batch + internal
ingest primitive** (F4), and a **canonical byte digest** for snapshots (F5).

## Context

PR 6 re-prices every live check under a run's pinned bundle but leaves PASS/FAIL as
originally judged. A check's PASS/FAIL is a **validator-code** property; the verdict-shaping
code spans `validators/*`, the dispatch (`build_intents`), the DB→`extras` normalization,
the website review-guard eligibility, and the new snapshot codec + replay binding — rev 4
treats all of it as **one pure validation boundary** identified by `VALIDATOR_BUILD_ID`.
Two axes: **bundle → pricing** (PR 6, already live) and **validator → PASS/FAIL trust**
(PR 6b: fail-closed on validator-stale PASSes + deterministic replay + a one-time closure
of the untrusted legacy backlog).

## Decisions

1. **Fail-closed** — a live PASS whose `validator_build_id` `IS DISTINCT FROM` the target
   validator is excluded from score/gates (ephemeral projection, no row mutation).
2. **Two identifiers.** `ENGINE_BUILD_ID` `eng-1`→**`eng-2`** (scoring changed;
   runs/decisions). New `VALIDATOR_BUILD_ID = "val-1"` on **checks**; staleness keys on it.
3. **NULL provenance is unknown ⇒ stale**, never relabeled/backfilled (some pre-6b PASSes
   were judged by pre-PR3 *permissive* validators). The legacy PASS backlog is **closed**.
4. **Deterministic replay** from a versioned, **canonical-digest-verified** immutable
   snapshot.
5. **Revalidation is a per-case first-class coordinator run** through a fused
   score→decide→project→outbox primitive, dispatched from a durable batch record.
6. **One hashed validation boundary** for the validator drift guard.
7. **A dedicated `enforce_validator_freshness` flag** — independent of PR 6's
   `enforce_bundle_pinning` (F1).
8. **Build the full writer now** (replay lane + closure lane).

## Architecture

### 1. Validation boundary + guard (F6, unchanged from rev 3)

Consolidate every verdict-shaping unit under a cohesive **pure** `validation_engine/`
surface (validators, dispatch, pure `extras` normalization, the pure scalar review-guard
eligibility, the snapshot codec, the replay-binding helpers; import-linter-enforced
purity). `VALIDATOR_BUILD_ID` is pinned by a **framed hash over the transitive closure** of
that package (mechanically computed from the validation entrypoint's `src/kyc_tool` imports,
so a new dependency auto-extends the hash; fallback: an explicit manifest a test proves
complete). Mutation-tested: editing a validator, the review-guard eligibility, the extras
decoder, or the replay binding each changes the digest.

### 2. Data model — migration 013 (`down_revision='012'`)

- **`checks.validator_build_id TEXT NULL`** + nonblank `CHECK … NOT VALID`. Existing rows
  stay NULL = **unknown** (decision 3). `CheckView` gains `validator_build_id`.
- **`run_validation_snapshots`** (append-only): `run_id TEXT PK → runs.id`,
  `snapshot_json JSONB NOT NULL`, `snapshot_digest TEXT NOT NULL`
  (`CHECK (snapshot_digest ~ '^[0-9a-f]{64}$')`, F5),
  `captured_at timestamptz NOT NULL DEFAULT now()`.
- **`revalidation_batches`** (append-only coordinator record, **F4**):
  `coordinator_run_id TEXT PK → runs.id`, `target_validator_id TEXT NOT NULL`,
  `expected_checks JSONB NOT NULL` (sorted `[(check_id, created_by_run_id)]`),
  `batch_digest TEXT NOT NULL` (sha256 of the canonical expected set),
  `state TEXT NOT NULL` (`open|complete`), result counters (`replayed`/`closed`/`skipped`),
  `created_at`/`resolved_at timestamptz`. Revalidation dispatch keys off the **presence of
  this row**, not a caller-supplied payload.
- **`engine_activation_epoch`** (append-only *pair* history, distinct from PR 6's immutable
  singleton `bundle_pinning_epoch`): `id` DB-sequenced PK, `engine_build_id TEXT NOT NULL`,
  `validator_build_id TEXT NOT NULL`, `activated_at timestamptz NOT NULL`. Active pair =
  max-id row.
- Forward-only-after-use downgrade. Lineage guard: PR 6b = `013`, State `shipped`.

### 3. Identifiers + flags

`domain/engine.py`: `ENGINE_BUILD_ID = "eng-2"` (whole-tree guard re-pinned **with** the
bump); `VALIDATOR_BUILD_ID = "val-1"` (validation-closure guard, §1).

**`Settings.enforce_validator_freshness: bool = False`** (F1) — **independent** of
`enforce_bundle_pinning`, which is left exactly as PR 6 shipped (it still governs bundle
resolution). Freshness (§7) reads **only** this flag. When it is **on**, API and
pipeline/dev-worker **startup + `/readyz`** require the DB's active `(engine, validator)`
pair to equal the local `(ENGINE_BUILD_ID, VALIDATOR_BUILD_ID)` before serving or claiming
work, and emit a per-process pair attestation; a missing/mismatched pair is a hard boot/readiness
failure. (`outbox`/`retention` remain freshness-independent.)

### 4. Versioned snapshot codec + canonical digest (F2 rev3, F5 rev4)

`ValidationSnapshotV1` — a Pydantic model of **JSON primitives** (`schema_version`,
`evaluated_at` ISO-8601 **UTC** strict tz-aware, scalar website-guard fields, explicit
`CheckView` list with status strings, POC token fields, the per-validator normalized
inputs). One shared codec function is the **only** serializer/digester:

1. validate `ValidationSnapshotV1`; normalize every timestamp to UTC in **one** textual
   form; 2. dump JSON with `sort_keys=True, separators=(",",":"), ensure_ascii=False,
   allow_nan=False`; 3. UTF-8 encode; 4. **SHA-256 → lowercase hex** = `snapshot_digest`.
Store the parsed object (`snapshot_json`) **and** the digest. On load: validate, re-encode
through the **same** function, and **compare the digest before replay** — mismatch/unknown
`schema_version`/type error ⇒ fail closed (stale, no writes). A fixed **test vector**
(key-order, JSONB round-trip, `+00:00` vs `Z`, non-ASCII) pins byte-stability so API and
worker implementations cannot diverge. `ValidationContext` gains `evaluated_at`; validators
read `ctx.evaluated_at` (live: now; replay: the snapshot's) so POC expiry is as-of.

### 5. One snapshot-capture seam (F3 rev2, unchanged)

A single snapshot builder+persister used by **every** validator-produced-check path — the
VALIDATE branch **and** the broker-blocked DECIDE short-circuit
(`website.review_completed`, `pipeline.py` ~413). Compute the guard/`evaluated_at` once;
feed the same immutable scalar snapshot to both intent creation and persistence in the
fused transaction. Flag-independent; live path byte-identical.

### 6. Provenance stamping

Thread `validator_build_id` through the checkstore writers (as PR 6 threaded
`policy_bundle_hash`); runs/decisions stamp `eng-2`. Every new check is born `val-1`.

### 7. Fail-closed freshness projection — shared, gated on `enforce_validator_freshness`

Pure `validator_fresh_views(views, target_validator_id)`: each view whose
`validator_build_id IS DISTINCT FROM target` is **downgraded PASS→NEEDS_REVIEW** in the
ephemeral view (0 points; reason_codes retained → stale hard-conflict still fires gate 5;
non-PASS untouched). Invoked, **only when `enforce_validator_freshness` is on**, in **both**
`_decide_txn` and `_handle_manual_approve` (which today calls `org_id_check_passed` on raw
views — F5-rev2). Settings is passed **explicitly** into `ingest` (no hidden global). A
manual case may stay `approved_manual`, but its buy state + manual `DecisionRow` are
**locked** (`buy_locked_org_id_required`) until a current-validator ORG-ID PASS exists.
Only stale **PASS** rows change a decision or block activation.

### 8. Revalidation writer — coordinator run via a durable batch (F4, F1)

`ops.revalidate_backlog` scans for cases with a validator-stale live **PASS**
(`validator_build_id IS DISTINCT FROM VALIDATOR_BUILD_ID`, any case status). Per case it
calls a **shared internal-ingest primitive** that, under `Case FOR UPDATE`, allocates the
**same gap-free `event_sequence`** as ordinary ingest and **atomically** writes: an
internal `recalculate.requested` **Event** (explicit internal actor), a coordinator **Run**
(own creation-pin bundle), the **`revalidation_batches`** row (target validator + the sorted
expected `(check_id, created_by_run_id)` set + `batch_digest`), and the **job** — one
transaction. The event **idempotency key** is `revalidate:<target>:<sha256(canonical
expected-set)>`: an exact retry reuses one coordinator run; a **changed** expected set mints
a **new** coordinator (so a later/different stale set is never suppressed by a completed
event). Dispatch is by the **presence of the batch row**, distinguishing revalidation from a
public empty `recalculate.requested`.

The pipeline handles the coordinator job under `Case FOR UPDATE`, in **one fused commit**:
1. For each expected stale live PASS, **reselect by id + `created_by_run_id` under the
   lock**; skip if a newer run replaced it (surgical).
2. **Replay lane** — source run has a valid, **digest-verified** snapshot: rebuild its
   `ValidationContext` from immutable evidence + snapshot, run the validation-boundary
   `build_intents` under the **target validator**, **re-verify the immutable binding** (POC:
   persisted `token_id`/`token_digest` + `(rir,poc,org,resource)`, its own consumption as
   proof, expiry at snapshot `evaluated_at`; website: terminal task `case`/`type`/`reviewer`
   vs persisted event, **without** requiring `open`). Successor `created_by` the
   **coordinator run**, `policy_bundle_hash` = the **source** run's pin,
   `validator_build_id` = target. Tampered binding ⇒ FAIL/stale.
3. **Closure lane (F1)** — no snapshot / context unprovable (every pre-6b check): supersede
   the PASS with `NEEDS_REVIEW`/`historical_validation_context_unavailable`, target id.
4. **Score under the *coordinator* run's creation pin** → decide → project
   (`cases.current_score`/`latest_decision`) → **enqueue the outbox callback** → complete
   the job → update the batch (counts, `state=complete`) → write a **revalidation audit
   record** (coordinator + per source run/check ids, old/new ids, validator/engine).

Extract a reusable fused **`score→decide→project→outbox` primitive**; do **not** call
`_decide_txn` on a complete source run. The coordinator is a real run ⇒ the callback carries
a **fresh `run_id`** (not deduped as an old run). This internal use of the ops-only
`recalculate.requested` event is recorded in **ADR-006** + `AUDIT_FINDINGS.md`.

### 9. Activation — pair CAS + delivery convergence (F8 rev2, F3 rev3)

`ops.activate_revalidation_epoch --expect-current-engine <eng|none>
--expect-current-validator <val|none> --expect-engine eng-N --expect-validator val-N`.
Under a **transaction-scoped lock**, verify: (a) current max `(engine,validator)` pair ==
expected-current; (b) local constants == the requested target; (c) **the security gate** —
zero validator-stale live **PASS** rows remain **AND** every coordinator run in the closure
batch set has **`Run.state=COMPLETE`**, a non-NULL matching **`decisions.published_at`**,
and **no pending/dead** decision callback (a dead callback is a **hard blocker** → requeue
or explicit platform reconciliation, never success — F3). Then insert the next pair row and
read back. Same target ⇒ idempotent; expected-predecessor→new-target ⇒ allowed (a future
`eng-3/val-2` is **not** blocked by `eng-2` active); other predecessor / race ⇒ refuse
without a write. The PR 6 `bundle_pinning_epoch` stays **immutable**. Alert: post-epoch live
**PASS** checks whose `validator_build_id` `IS DISTINCT FROM` the active validator.

### 10. Executable drained cutover (F2)

Not an arrow list — an ordered, fenced runbook (mirrors PR 6 §10):
1. Build + **publish + pin the reviewed image by digest**.
2. **`alembic upgrade head` (013) BEFORE starting the new image** (013 is backward-
   compatible; new code that writes `validator_build_id`/snapshots cannot run before it).
3. **Pause all event submission**; disable autoscaling/restarts.
4. **Hard-stop and confirm zero** old API + pipeline + dev-worker processes (orchestrator-
   level), then `ops.requeue_interrupted_jobs`.
5. Start **only** the digest-pinned PR6b image with `enforce_bundle_pinning` **unchanged**
   and `enforce_validator_freshness` **off**; drain ordinary work.
6. Run `ops.revalidate_backlog` (closure); **start/drain the outbox publisher** in the
   window and report undelivered case/run ids (the delivery witness, §9/F3).
7. `ops.activate_revalidation_epoch` pair-CAS (`--expect-current-* none --expect-engine
   eng-2 --expect-validator val-1`) — passes only when §9's gate (checks **and** delivery)
   is satisfied.
8. Roll/restart the API+worker processes with `enforce_validator_freshness` **on**;
   **direct-probe each process's pair attestation** before resuming traffic.
Rollback: freshness-off on the PR6b image; never delete epoch/snapshot/batch history. (The
zero-downtime alternative — every check/manual writer taking the same epoch fence — is
explicitly **out of scope** for 6b; the drained window is the contract.)

### 11. Docs / governance (F9)

**ADR-006** — revalidation (axes, fail-closed, snapshot codec + canonical digest,
coordinator-run batch + the internal use of `recalculate.requested`, pair activation +
delivery convergence, the drained cutover). **PR 10 → ADR-007** in ROADMAP §I **and** the
PR 10 detailed section (both already fixed); the ROADMAP guard is extended to assert the two
ADR refs agree. `DEPLOYMENT.md` §10, `RUNBOOK.md` (revalidate + activate + delivery-drain),
ROADMAP PR 6b → `shipped`/`013`, ADR-005/OVERVIEW cross-ref, `AUDIT_FINDINGS.md` (internal
`recalculate.requested`).

## Invariants

- NULL validator provenance is stale/unknown; never relabeled/backfilled.
- `enforce_validator_freshness` is a **separate** flag; `enforce_bundle_pinning` unchanged.
- The fail-closed projection never mutates a stored row; revalidation only supersedes +
  audits. Only stale live **PASS** rows change a decision or block activation.
- Revalidation runs under `Case FOR UPDATE`, dispatched from a durable batch, reselects the
  expected check id under the lock, never clobbers a newer check.
- Replay uses only immutable evidence + the **canonical-digest-verified** snapshot; a
  tampered binding/digest stays stale; zero adapter/network calls.
- Each per-case revalidation is a fresh coordinator run; the fused commit covers checks +
  decision + case projection + outbox; a fault rolls all back; retry ⇒ exactly one effect.
- **Activation requires both zero stale live PASSes and delivered fail-closed callbacks.**
- Flag-off (both flags) is a strict scoring no-op; golden cases byte-identical.
- `ENGINE_BUILD_ID=eng-2`, `VALIDATOR_BUILD_ID=val-1`; both guards re-pinned in-commit.
  `KYC_Tool_Build_Package/` + M2 untouched; validation boundary import-pure (2/0).

## Testing strategy (incl. every rev-3 named regression)

- **Flag matrix (F1):** bundle-on/freshness-off loads X + counts legacy PASS;
  bundle-on/freshness-on loads X + excludes it; freshness-on with no/mismatched pair ⇒ API +
  worker constructors refuse; mutation-test removal of the readiness pair comparison.
- **Cutover fence (F2):** hold an old-writer txn after insert, before commit, while
  activation runs ⇒ activation impossible until it's gone; an old inline manual approve
  during the pause never commits; the new image booted at rev 012 ⇒ controlled readiness
  failure, not serving.
- **Delivery convergence (F3):** callback 500 after the fused commit ⇒ activation refuses;
  requeue + deliver via `OutboxPublisher` ⇒ the exact coordinator run becomes COMPLETE +
  `published_at` set ⇒ activation may succeed; mutating the delivery predicate fails the test.
- **Coordinator batch (F4):** restart between enqueue and claim ⇒ recover the exact batch; a
  public empty recalc stays on the ordinary path; two concurrent internal creators ⇒ one
  gap-free event/run; replace expected check A with stale B before processing ⇒ A skips
  surgically and a second scan mints a different key/run closing B, while an exact B rescan
  replays one run.
- **Canonical digest (F5):** key-order, JSONB round-trip, `+00:00` vs `Z`, non-ASCII ⇒ the
  documented **same** digest (fixed vector); changing one typed field with the old digest ⇒
  fail closed, zero writes; DB rejects a non-64-hex digest.
- **Core (carried):** migration 013 up/down + forward-only + `NOT VALID`; validation-closure
  guard mutation set; snapshot byte-stable replay; broker-blocked capture seam;
  manual-approve freshness lock; backlog closure of a pre-PR3 permissive NULL PASS (no
  retro-stamp); replay determinism (consumed/`done`/aged POC+website ⇒ identical PASS;
  tampered ⇒ stale); document both run orders; cascade `needs_review` placeholder doesn't
  block; activation CAS matrix; concurrency barrier + fault rollback; ADR docs test.
- Full suite green on real Postgres; ruff clean; import-linter 2/0.

## Out of scope (YAGNI)

- No adapter re-fetching; no retro-stamp/backfill of pre-6b provenance; no zero-downtime
  per-writer epoch fence (the drained window is the contract); no multi-engine runtime
  beyond the two-identifier + fail-closed hook; no re-judging of the manual *decision*
  itself (only its buy-enablement freshness); no public revalidation route/UI.
