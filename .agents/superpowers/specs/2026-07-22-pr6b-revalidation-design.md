# PR 6b — Revalidation under the pinned engine (item 7B) — design

## Context

PR 6 (policy bundle pinning, item 7A, converged + audit-clean at `7059f7f`) made
each run resolve its **creation-pin bundle** at job entry and, flag-on, re-price
every live check's points/category from that resolved rubric across
`score()`/`evaluate_gates()`/the callback. It deliberately left one boundary: a
check's **PASS/FAIL judgment** is untouched (ADR-005: "immutable check rows and
validator PASS/FAIL untouched — re-judging evidence stays PR 6b";
`scoring.rubric_scoring_views` re-prices points/category only).

This is the gap PR 6b closes. The KYC validators
(`src/kyc_tool/validators/*.py`, dispatched by `validators/build.py::build_intents`)
are **pure over engine/code**: they take `(normalized adapter output, case_snapshot,
live_checks, extras)` and return `CheckIntent`s — **no** `policy`/`rubric`/`bundle`
argument (only `build_intents` reads the rubric, and only to assert the produced
`check_type`s exist). Therefore:

- **Bundle drift → pricing.** Points, category, threshold, gate config live in the
  bundle. PR 6 already re-prices these live for every live check under the deciding
  run's resolved bundle. No further work in PR 6b.
- **Engine drift → validation trust.** A check's PASS/FAIL is determined entirely by
  the validator *code*, versioned as `ENGINE_BUILD_ID` (`domain/engine.py`, `eng-1`).
  A live check judged under a *different* engine than the run being decided is not
  trustworthy for that decision.

Today there is exactly one engine (`eng-1`), so re-judging never *changes* an outcome
yet. PR 6b's concrete, shippable value is threefold: (1) a **fail-closed** rule so a
PASS only counts when it was judged by the pinned engine; (2) a **replay-based
revalidation** that re-runs the validators over each run's *immutable stored evidence*
under the pinned engine, writing engine-stamped superseding checks; (3) an
**activation gate** so the pinning epoch cannot be activated while any live check is
engine-stale. It is the forward-safe hook for the day a second engine ships.

## Decisions locked in brainstorming

1. **Staleness is fail-closed (do not count).** Post-epoch, an un-revalidated live
   PASS is excluded from score/gates until a pinned-engine successor exists — not
   downgraded to a written `needs_review` placeholder, not "provenance-only".
2. **Staleness keys on the engine axis** (`checks.engine_build_id`), not the bundle —
   bundle drift is already neutralised by PR 6's live re-pricing; validators are
   bundle-independent.
3. **Replay-based ops batch** revalidation (not an event-driven `revalidate.requested`
   run, not operator re-drive): reuse the pipeline's existing evidence reconstruction
   to replay each contributing run's validation offline under the pinned engine.

## Architecture

Six coordinated pieces, all behind `enforce_bundle_pinning` for the scoring change;
provenance stamping is flag-independent (like PR 6's bundle stamping).

### 1. Data model — migration 013 (`down_revision='012'`)

- Add `checks.engine_build_id TEXT NULL`.
- Add a nonblank guard `CHECK (engine_build_id IS NULL OR btrim(engine_build_id) <> '')`
  created **`NOT VALID`** — metadata-only, no scan of the (large, append-only) `checks`
  table, and it still enforces every new/updated row. All existing rows are `NULL`, so
  the predicate is trivially satisfied; a follow-on `VALIDATE CONSTRAINT` is **not**
  required for correctness (and no spare revision is reserved for one — PR 6b owns only
  `013`; validating pre-existing all-NULL rows is a no-op we can fold into any later
  migration if ever wanted). Downgrade is **forward-only-after-use**, mirroring 011:
  refuse if any check has recorded `engine_build_id`, else drop the column.
- ORM (`db/tables.py`): `Check.engine_build_id: Mapped[str | None]`.
- `CheckView` (`domain/models.py`) gains `engine_build_id: str | None`;
  `checkstore.as_view` reads it. (`CheckView` is the pure scoring input; it must carry
  the engine to let scoring judge staleness.)

The migration renumbering guard (`tests/unit/test_migration_lineage.py`) already
requires PR 6b to be `013` with the ROADMAP row flipped to `shipped` in the same PR.

### 2. Engine provenance stamping

Thread `engine_build_id` through the checkstore writers exactly as PR 6 threaded
`policy_bundle_hash`: `write_check`, `apply_check_intents`, `supersede_without_replacement`,
`_supersede_on_identity_change`, `supersede_stale_identity_proof`. The pipeline passes
`ENGINE_BUILD_ID` at every call site where it already passes `bundle.bundle_hash`
(`orchestration/pipeline.py`). `events/ingest.py::_handle_manual_approve` already stamps
`decisions.engine_build_id` (PR 6) — manual approve writes a *decision*, not a check, so
it is outside the check-revalidation set.

Result: every check the flag-on OR flag-off pipeline writes is stamped `eng-1`, so new
checks are **born non-stale**. Only pre-013 checks (and any written by a hypothetical
old replica) carry `NULL`.

### 3. Fail-closed scoring (flag-on only)

Extend the decision-time view transform. `rubric_scoring_views(views, rubric)` currently
re-prices. PR 6b makes it (or a thin wrapper it calls) also take the **resolved engine**
— the deciding process's `ENGINE_BUILD_ID` (one engine today, so always `eng-1`;
forward-safe if a second ships) — and, for any view whose `engine_build_id !=
resolved_engine`, **downgrade its status `PASS → NEEDS_REVIEW` in the ephemeral view**
(points 0; `reason_codes` retained). The stored check row is never mutated — this is a
pure scoring-time projection, exactly like re-pricing.

Consequences, all fail-*safe*:
- `score()` counts only PASS → a stale check contributes 0.
- `evaluate_gates()` `legal_proof`/`control_proof` consider only PASS → a stale check
  can't satisfy them.
- `org_id_check_passed()` needs a live PASS `org_id_match` → a stale one can't unlock buy.
- `has_hard_conflict()` scans `reason_codes` over **all** live checks regardless of
  status → a stale check's conflict signal is **retained**, so a stale hard-conflict
  still blocks approval. (Staleness removes trust in a *positive* verdict but never
  suppresses a *safety* signal.)

Flag-off: the transform is not applied → strict scoring no-op, byte-identical to today
(golden cases unaffected). This is asserted by a guard test.

### 4. Revalidation module (`src/kyc_tool/revalidation/…`)

**Shared reconstruction.** The pipeline's VALIDATE stage
(`pipeline.py:385-409`) already rebuilds a `ValidationContext` from durable storage:
`adapter_results.normalized_json` (by `run_id`, `status='ok'`), `runs.input_snapshot_json`
(frozen inputs, PR 2, via `_run_snapshot`), the triggering event's type/payload,
`live_checks`, and DB-fetched `extras` (`_validation_extras`: poc token record / website
guard). Extract this into a shared pure-ish helper
`build_validation_context(session, run, case, event, *, website_guard=None) -> ValidationContext`
that **both** the pipeline and the revalidator call — a targeted refactor, no behaviour
change to the pipeline (verified by the existing suite).

**Revalidate one run.** `revalidate_run(session, run, *, engine, bundle) -> list[Check]`:
reconstruct the run's context, run `intent_builder(bundle, ctx)`, and for each produced
intent whose **current live check of that type is still `created_by_run_id == run.id`
and is engine-stale**, write a superseding check carrying the re-judged status +
`policy_bundle_hash`/`engine_build_id` stamps. Intents for types where the live check is
now owned by a *newer* run are **skipped** (never clobber a newer judgment). This
surgical rule is the core correctness invariant.

**Idempotent.** A check already stamped with `engine` is skipped, so re-running the
sweep is a no-op once converged.

**Determinism / evidence completeness.** Replay reads only immutable stored evidence
(no adapter re-fetch). `extras` come from durable records (`poc_tokens` rows are
immutable once created; a `website.review_completed` check implies its review task
reached a terminal completion). If a run's evidence can't reproduce a live check its
`created_by_run_id` points at (e.g. missing `adapter_results`, or `intent_builder`
yields no intent for that type), the check is **left stale** and surfaced in the sweep's
report — fail-closed, never a silent pass.

### 5. Ops + cutover

- **`ops.revalidate_backlog`** (new `python -m` entry): find every engine-stale live
  check (`engine_build_id IS NULL OR <> ENGINE_BUILD_ID`) on non-terminal cases, group
  by `created_by_run_id`, and `revalidate_run` each under that run's resolved
  bundle+engine. Prints/returns the run_ids that could not be fully revalidated (nonzero
  exit blocks the cutover). Per-case advisory lock while writing, like the pipeline.
- **Activation gate.** `ops.activate_bundle_pinning_epoch` gains a precondition —
  `verify_revalidated_backlog(session_factory)` returning the engine-stale live-check ids
  — and **refuses** to activate while it is nonempty ("block rollout activation until
  successors exist"). Sits beside the existing `verify_pinnable_backlog` preflight.
- **Alert.** `ops.activate_bundle_pinning_epoch::post_epoch_null_provenance` extends its
  `checks` surface to also flag post-epoch live checks with `engine_build_id IS NULL`
  (today it flags `policy_bundle_hash IS NULL`).
- **Cutover order** (drained, per PR 6 §10): deploy PR6b image (flag off) → `alembic
  upgrade head` (013) → `verify_pinnable_backlog` → drain/confirm zero old workers →
  `requeue_interrupted_jobs` → **`revalidate_backlog`** → confirm zero stale →
  `activate_bundle_pinning_epoch` → enable flag → resume. Rollback: flag-off on the PR6b
  image (the stamped `engine_build_id` columns are additive and harmless flag-off).

### 6. Docs / governance

- **ADR-006** — revalidation (the engine-trust axis, fail-closed rule, replay
  mechanism, activation gate). (PR 6 reserved ADR-006 for a PR 10 item; if still
  reserved, this takes the next number and the reservation shifts — resolve at write
  time.)
- `docs/DEPLOYMENT.md` §10 cutover updated with the revalidate step.
- `docs/RUNBOOK.md`: `revalidate_backlog` command + the engine-stale alert.
- `.agents/ROADMAP.md`: PR 6b row → `shipped`, migration `013` (lineage guard enforces
  the State partition).
- `docs/OVERVIEW.md` / ADR-005 cross-reference: PR 6b lands the re-judging PR 6 deferred.
- `AUDIT_FINDINGS.md`: only if an implementation deviation arises.

### On `ENGINE_BUILD_ID` — why PR 6b keeps `eng-1` (flag for review)

`checks.engine_build_id` records **which validator code judged this check's PASS/FAIL**.
PR 6b does **not** touch any validator (`validators/*.py` are unchanged), so an `eng-1`
check remains correctly judged by `eng-1` validators. The new fail-closed rule is
scoring-*aggregation* infrastructure, gated behind `enforce_bundle_pinning`, and its
first *meaningful* trigger is a **future** validator change (which will bump the engine
and legitimately mark old checks stale). Two independent reasons not to bump now:

1. **It would be self-defeating.** Bumping to `eng-2` would mark every existing `eng-1`
   check stale under `eng-2`, forcing a full-backlog revalidation whose re-run uses the
   *same unchanged validators* and therefore yields identical PASS/FAIL — pure churn.
2. **Steady-state decision output is unchanged.** With all live checks `eng-1`
   (guaranteed after the cutover sweep, and for every newly-written check), the
   fail-closed downgrade never fires, so a post-PR6b decision equals a pre-PR6b decision
   for the same checks. The rule only bites on engine-stale checks, which the cutover
   eliminates; flag-off is byte-identical regardless.

So the drift guard is re-pinned (source changed) with `ENGINE_BUILD_ID` unchanged. This
is the single most likely point of reviewer disagreement and is called out here
deliberately; if review concludes the flag-on aggregation change is itself an
engine-semantic change, the alternative is to bump **and** make the cutover sweep the
mandatory mechanism that revalidates the whole backlog before the flag flips — at the
cost of the churn in (1). Recommendation: **no bump**, for the reasons above.

## Invariants (for the plan's Global Constraints + review rubric)

- Flag-off is a **strict scoring no-op**; golden cases stay byte-identical.
- The stored check row is **never mutated** by the fail-closed rule (ephemeral view only);
  revalidation only ever *supersedes* (append-only), never edits.
- Revalidation **never clobbers a newer run's live check** (surgical per-type rule).
- Revalidation reads **only immutable stored evidence** — zero adapter/network calls.
- A check that cannot be revalidated is **left stale + reported**, never silently passed.
- `engine_build_id` matches `^eng-[1-9]\d*$`; PR 6b **keeps it `eng-1`** (see the
  dedicated note below). The framed whole-tree drift guard is re-pinned in each
  src-touching commit **without** a bump.
- `KYC_Tool_Build_Package/` immutable; M2 / `enforce_positive_decisions` untouched;
  `policy/` stays pure (import-linter 2 kept / 0 broken).

## Testing strategy

- **Migration 013**: up/down clean; forward-only-after-use downgrade refusal (populated
  `checks.engine_build_id`); nonblank CHECK rejects blank; column added `NOT VALID`
  (`pg_constraint.convalidated=false`) — extend the migration suite. Lineage guard
  already covers the single-head + `shipped 013` reservation.
- **Provenance**: every flag-on/flag-off write stamps `eng-1` (checkstore + pipeline).
- **Fail-closed scoring**: a live PASS with `engine_build_id` NULL / `eng-0` is excluded
  from score + legal/control gates + org-id-unlock under flag-on, but its hard-conflict
  reason code still trips gate 5; flag-off counts it (no-op). Mutation-resistant.
- **Revalidation**: replay re-judges a stale check to a pinned successor; the surgical
  rule leaves a newer run's check untouched; idempotent second sweep is a no-op; an
  unreconstructable check is left stale + reported. Uses the real ephemeral Postgres +
  the `_bundle_helpers`.
- **Ops**: `revalidate_backlog` clears the backlog and reports failures;
  `activate_bundle_pinning_epoch` refuses while stale checks exist and succeeds after the
  sweep; the post-epoch alert flags NULL-engine checks.
- Full suite green on real Postgres; ruff clean; import-linter 2/0.

## Out of scope (YAGNI)

- No adapter re-fetching — revalidation is over immutable stored evidence only.
- No multi-engine runtime — one engine (`eng-1`); PR 6b is the forward-safe provenance +
  fail-closed hook, not an engine-migration framework.
- No re-judging of manual-approve (it writes a decision, already engine-stamped, not a
  check) and no change to decisions' engine stamping (PR 6 did it).
- No event-driven revalidation surface, no UI.

## Open questions (non-blocking; resolve in plan or note as TODO(integration))

- **ADR number** — 006 vs the next free number if PR 6 already consumed the reservation.
  Mechanical; resolve when writing docs.
- **`verify_revalidated_backlog` scope** — "non-terminal cases" definition: exclude cases
  whose only runs are `COMPLETE` with a terminal decision? Simplest safe scope: any case
  with a live check that is engine-stale. Confirm in plan.
