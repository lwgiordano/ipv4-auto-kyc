# PR 6b — Revalidation under the pinned engine (item 7B) — design (rev 2)

Rev 2 folds the six findings of Codex spec-review `AUDIT [CODEX] eb5af90..3354aab`
(rev 1). The rev-1 premise "same validators ⇒ same outcome, so replay the live context"
was **wrong**: the validators read *mutable current state* (POC token `consumed_at`/
expiry via `datetime.now`; the website review task's `open` status; the *live*
`official_registry_match` cross-check), so replaying a once-valid PASS through live
context yields FAIL/no-intent. Rev 2 replaces live-context replay with an **immutable
per-run validation snapshot**, runs revalidation as a **fused per-case re-decide**, and
splits provenance into a **validator axis** (checks) and an **engine axis** (decisions).

## Context

PR 6 (item 7A, converged) resolves each run's creation-pin bundle and, flag-on, re-prices
every live check's points/category. It left validator PASS/FAIL untouched. The KYC
validators (`validators/*.py`, dispatched by `build_intents`) are pure over
`(normalized adapter output, case_snapshot, live_checks, extras)` — no bundle/rubric arg
— so a check's PASS/FAIL is a **validator-code** property, versioned separately here.
But those inputs include mutable state, so re-judging requires the state **as of the
original validation**, not the live world.

Two axes:
- **Bundle drift → pricing** — already re-priced live by PR 6. No PR 6b work.
- **Validator drift → PASS/FAIL trust** — PR 6b: fail-closed on validator-stale PASSes +
  a deterministic replay revalidator.

## Decisions (brainstorming + rev-1 review)

1. **Fail-closed** — an un-revalidated (validator-stale) live PASS is excluded from
   score/gates until a pinned successor exists (ephemeral projection, no row mutation).
2. **Two identifiers (finding 1).** `ENGINE_BUILD_ID` bumps **`eng-1` → `eng-2`** because
   PR 6b changes *scoring/decision* semantics (the flag-on fail-closed projection);
   runs/decisions stamp `eng-2`, and the decision-provenance collision rev 1 would have
   caused is fixed. A **new** `VALIDATOR_BUILD_ID = "val-1"` (validators unchanged in
   6b) is what **checks** stamp, and **staleness keys on the validator axis**. Because no
   validator changed, **today's live backlog is not stale** — no mass revalidation at the
   6b cutover; the writer is proven forward-safe via a synthetic validator bump in tests.
3. **Deterministic replay from an immutable snapshot (finding 2)** — not live context.
4. **Fused per-case re-decide (finding 3)** — revalidation runs through the pipeline's
   decide transaction under the Case row lock; it never writes bare superseding checks.
5. **Build the full writer now** (snapshot capture + revalidator + ops + activation gate).

## Architecture

### 1. Data model — migration 013 (`down_revision='012'`)

- **`checks.validator_build_id TEXT NULL`** + nonblank `CHECK … NOT VALID` (hot-compat;
  enforces new rows; existing rows all-NULL). This is the staleness key. (Checks keep
  PR 6's `policy_bundle_hash`; they get **no** `engine_build_id` — the engine lives on
  runs/decisions.) `CheckView` gains `validator_build_id`; `as_view` reads it.
- **`run_validation_snapshots`** (append-only): `run_id TEXT PK → runs.id`,
  `snapshot_json JSONB NOT NULL`, `captured_at timestamptz NOT NULL DEFAULT now()`. The
  frozen **mutable** inputs of that run's VALIDATE stage — the resolved `extras`, the
  `live_checks` views used, and the `evaluated_at` clock — enough to re-run `build_intents`
  deterministically. (The *immutable* inputs — `adapter_results.normalized_json`,
  `runs.input_snapshot_json`, the triggering event — are already durable and are not
  copied.) One row per run that produced checks.
- **`engine_activation_epoch`** (append-only engine history, distinct from PR 6's
  singleton `bundle_pinning_epoch`, finding 4): `id` DB-sequenced PK,
  `engine_build_id TEXT NOT NULL UNIQUE`, `validator_build_id TEXT NOT NULL`,
  `activated_at timestamptz NOT NULL`. The current active engine is the max-id row.
- Downgrade: forward-only-after-use (refuse once any `validator_build_id` / snapshot /
  engine-epoch row exists), mirroring 011.
- The lineage guard already requires PR 6b = `013`, State `shipped`.

### 2. Identifiers + guards

- `domain/engine.py`: `ENGINE_BUILD_ID = "eng-2"` (bumped); **new**
  `VALIDATOR_BUILD_ID = "val-1"` (format `^val-[1-9]\d*$`).
- The framed whole-tree drift guard is re-pinned **with** the `eng-2` bump (scoring
  changed — this is exactly the conscious bump the guard exists to force).
- **New validator-closure guard**: a second framed sha256 over `src/kyc_tool/validators/
  **/*.py` pinned to `EXPECTED_VALIDATOR_SOURCE_HASH`, whose failure message is "if
  validator *judgment* changed, bump `VALIDATOR_BUILD_ID`; re-pin either way." This makes
  a future validator edit force a conscious `val-N` bump — the trigger the whole staleness
  mechanism depends on. (Shared pure deps that alter judgment, e.g. `domain/reasons.py`,
  are called out in the doc; the whole-tree guard still forces a decision on any src edit.)

### 3. Provenance stamping

Thread `validator_build_id` through the checkstore writers (as PR 6 threaded
`policy_bundle_hash`): `write_check`, `apply_check_intents`, the supersede/cascade paths.
The pipeline passes `VALIDATOR_BUILD_ID` at each check write; runs/decisions now stamp
`ENGINE_BUILD_ID = eng-2` (PR 6's columns, new value). Flag-independent — every new check
is born `val-1`.

### 4. Snapshot capture (flag-independent)

In the pipeline VALIDATE stage, after assembling the `ValidationContext`, persist the
`run_validation_snapshots` row: the `extras`, the `live_checks` views, and an
`evaluated_at` timestamp. `ValidationContext` gains `evaluated_at: datetime`; the POC
validator (and any other clock reader) uses `ctx.evaluated_at` instead of
`datetime.now(UTC)` (live path passes real now; replay passes the snapshot's value) — so
expiry is judged **as of the original validation**. No behaviour change on the live path
(guarded by test).

### 5. Fail-closed scoring (flag-on only)

Extend the decision-time view transform: in addition to re-pricing, any live check whose
`validator_build_id != VALIDATOR_BUILD_ID` (the deciding validator version) is
**downgraded `PASS → NEEDS_REVIEW` in the ephemeral view** (0 points; `reason_codes`
retained, so a stale hard-conflict still fires gate 5 — fail-*safe*). The stored row is
never mutated. Flag-off: not applied → strict scoring no-op (golden cases unchanged),
guard-tested.

### 6. Revalidation writer — fused per-case re-decide (findings 2 + 3)

Revalidation is **not** a bare check-writer. `ops.revalidate_backlog` enqueues, per case
with a validator-stale live check, an internal **revalidation job** the pipeline runs
through its existing fused decide transaction:

1. Acquire the **Case row `FOR UPDATE`** in the pipeline's lock order (`_load`), never an
   advisory lock.
2. Under the lock, for each validator-stale live check: load its `created_by_run_id`
   run, rebuild that run's `ValidationContext` from immutable evidence + the run's
   **snapshot** (frozen `extras`/`live_checks`/`evaluated_at`), run `build_intents` under
   the pinned validator/engine, and take the intent for the check's type.
3. **Re-verify the immutable binding** against persisted records (defence in depth beyond
   the snapshot): POC — the persisted event's `token_id`/`token_digest` and the
   `(rir,poc,org,resource)` binding, treating the token's own successful consumption as
   proof (not reuse), expiry judged at the snapshot's `evaluated_at`; website — the
   terminal task's `case`/`type`/`reviewer` against the persisted event, **without**
   requiring `open`. A tampered digest/binding/attribution ⇒ the successor is FAIL/stale,
   never a free PASS.
4. **Reselect the exact expected live-check id + `created_by_run_id` under the lock**
   before writing; stage a successor **only** if it still matches (surgical — never
   clobber a newer run's check that a concurrent normal run installed).
5. In the **one commit**: stage the matching successors, **score → decide → project the
   case (`cases.current_score`/`latest_decision`) → enqueue the outbox callback →
   complete the job**, plus an explicit **revalidation audit record** (old/new check ids,
   `validator_build_id`, `engine_build_id`) — not a fake re-run of the original run.
6. Idempotent (a check already `val-1`/current is skipped); fail-closed + reported if a
   run's evidence/snapshot can't reproduce the check.

Because a re-decide can flip a case's decision (a validator-stale PASS that re-judges to
FAIL), emitting the callback keeps checks, decision, case projection, and platform view
fused — the normative rule (`AGENTS.md`, `state_machine.json`).

### 7. Ops + cutover

- **`ops.revalidate_backlog`**: enqueue the revalidation job for every case with a
  validator-stale live check (**all cases, any status** — finding 5); drain; return the
  case/check ids that could not be revalidated (nonzero exit blocks cutover).
- **`ops.activate_revalidation_epoch --expect-engine eng-2`** (finding 4): verify the
  local `ENGINE_BUILD_ID`/`VALIDATOR_BUILD_ID` **and** a zero-validator-stale preflight
  **atomically**, then append + read-back the `engine_activation_epoch` row; refuse if a
  different engine is already the active max-id row or any stale check remains. The PR 6
  `bundle_pinning_epoch` is left **immutable**.
- **Alert**: the post-epoch check surface flags any live check whose `validator_build_id`
  `IS DISTINCT FROM` the active validator version (not NULL-only — catches a post-cutover
  old writer stamping a wrong non-NULL value).
- **Cutover** (drained): deploy PR6b image (flag off) → migrate 013 → snapshots now
  captured for new runs → (backlog is non-stale under the split, so `revalidate_backlog`
  is a **no-op** today; run it anyway to prove zero) → `verify` zero stale →
  `activate_revalidation_epoch --expect-engine eng-2` → enable flag → resume. Rollback:
  flag-off on the PR6b image; never delete epoch/snapshot history.

### 8. Docs / governance

- **ADR-006** — revalidation (validator vs engine axis, fail-closed, snapshot replay,
  fused re-decide, engine activation epoch). ROADMAP §I already updated: PR 6b = ADR-006,
  **PR 10 shifted to ADR-007** (finding 6). The docs task asserts exactly one ADR-006
  heading and PR 10 named ADR-007.
- `DEPLOYMENT.md` §10 cutover; `RUNBOOK.md` (revalidate + activate commands, alert);
  `.agents/ROADMAP.md` PR 6b → `shipped`/`013`; ADR-005/OVERVIEW cross-ref.

## Known boundary (documented, not a defect)

The snapshot is captured from PR 6b onward. A **pre-6b** live check has no snapshot; under
the split it is `val-1` and never validator-stale *through* 6b. If a *future* validator
bump (`val-2`) makes such a surviving pre-6b check stale, it cannot be deterministically
replayed (no snapshot) → it stays **fail-closed excluded** and is **reported** for
operator re-drive. This is safe (never a silent stale PASS) and bounded (pre-6b live
checks age out). Backfilling pre-6b snapshots is explicitly out of scope.

## Invariants (Global Constraints + review rubric)

- Flag-off is a **strict scoring no-op**; golden cases byte-identical.
- The fail-closed rule **never mutates** a stored row (ephemeral view); revalidation only
  **supersedes** (append-only) + writes an audit record.
- Revalidation runs under **`Case FOR UPDATE`** (pipeline lock order), reselects the
  expected live-check id under the lock, and **never clobbers a newer run's check**.
- Replay uses **only** immutable evidence + the immutable snapshot — zero live mutable
  reads, zero adapter/network calls; a tampered binding stays stale.
- A re-decide is a **single fused commit** (checks + decision + case projection + outbox);
  a fault after staging rolls all of it back; a retry yields exactly one decision/outbox.
- A check that can't be revalidated is **left stale + reported**, never silently passed.
- `ENGINE_BUILD_ID = eng-2` (bumped, runs/decisions); `VALIDATOR_BUILD_ID = val-1`
  (checks); both guards re-pinned in-commit. `KYC_Tool_Build_Package/` + M2 untouched;
  `policy/` pure (import-linter 2/0).

## Testing strategy

- **Migration 013**: up/down clean; forward-only-after-use downgrade refusal
  (validator_build_id / snapshot / engine-epoch populated); nonblank CHECK rejects blank;
  `NOT VALID` (`convalidated=false`); lineage guard green (`013`, `shipped`).
- **Identifiers/guards**: whole-tree guard pins `eng-2`; validator-closure guard trips on
  a validator edit without a `val` bump.
- **Provenance/snapshot**: every write stamps `val-1`; a snapshot row is written per
  producing run; the live path is byte-identical with `evaluated_at` threaded.
- **Fail-closed scoring**: a live PASS with `validator_build_id` NULL / `val-0` is
  excluded from score + legal/control gates + org-id-unlock flag-on, but its hard-conflict
  code still trips gate 5; flag-off counts it. Mutation-resistant.
- **Revalidation (Codex's rev-1 regressions):** real successful **POC** and **website**
  flows → confirm token `consumed`/task `done`, **age past expiry**, then revalidate and
  require **identical PASS** successors; **tampered digest/binding/task attribution** stays
  stale + CLI nonzero. Revalidate the same **document/registry** history in **both run
  orders** → identical result. **Concurrency**: two real PG sessions with a barrier between
  select and write → a newer normal-run check is never superseded. **Fault**: fault after
  staging successors → checks/decision/case/outbox all roll back; retry ⇒ exactly one
  decision/outbox; the emitted callback matches the new live-check set.
- **Ops**: `revalidate_backlog` clears stale + reports failures on `account_approved` /
  `approved_manual` / `rejected` cases (finding 5); `activate_revalidation_epoch` refuses
  while any stale check exists on the real seeded PR6 `(bundle,eng-1)` epoch, succeeds
  after the sweep without touching the bundle epoch, is idempotent, and flags a later
  wrong-validator writer via `IS DISTINCT FROM`.
- Full suite green on real Postgres; ruff clean; import-linter 2/0.

## Out of scope (YAGNI)

- No adapter re-fetching. No backfill of pre-6b snapshots. No multi-engine runtime beyond
  the two-identifier provenance + fail-closed hook. No re-judging of manual-approve (a
  decision, engine-stamped, not a check). No UI/event surface for revalidation (ops-only).
