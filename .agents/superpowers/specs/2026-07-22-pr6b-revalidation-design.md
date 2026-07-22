# PR 6b — Revalidation under the pinned engine (item 7B) — design (rev 3)

Rev 3 folds the nine findings of Codex spec-review `AUDIT [CODEX] 0a44056..4209ffc`
(rev 2). The two biggest corrections: (a) existing checks carry **NULL** validator
provenance, which is **unknown, not `val-1`** — the pre-6b PASS backlog (some judged by
pre-PR3 *permissive* validators) must be **closed**, never grandfathered (F1); and (b)
verdict-shaping code spans more than `validators/` — dispatch, extras normalization, the
review-guard, and the new snapshot/replay code all change a PASS/FAIL — so rev 3 draws a
single cohesive **validation boundary** and hashes its whole closure (F6). Rev 3 also
gives revalidation a **first-class coordinator run** identity (F4), a **versioned snapshot
codec** (F2), a shared **freshness projection** used by manual-approve too (F5), a
**pair compare-and-swap** activation (F8), a **PASS-only** stale blocker (F7), a
one-seam snapshot capture covering the DECIDE short-circuit (F3), and the ADR fix (F9).

## Context

PR 6 (item 7A, converged) re-prices every live check under a run's pinned bundle but
leaves PASS/FAIL as originally judged. A check's PASS/FAIL is a **validator-code**
property; but the code that shapes a verdict is not only `validators/*.py` — it includes
the dispatch (`build_intents`), the DB→`extras` normalization, the website review-guard
eligibility, and (new here) the snapshot codec + replay binding. Rev 3 treats all of it
as **one pure validation boundary** identified by `VALIDATOR_BUILD_ID`.

Two axes: **bundle → pricing** (PR 6, already live) and **validator → PASS/FAIL trust**
(PR 6b: fail-closed on validator-stale PASSes + a deterministic replay revalidator + a
one-time closure of the untrusted legacy backlog).

## Decisions

1. **Fail-closed** — a live PASS whose `validator_build_id` `IS DISTINCT FROM` the target
   validator is excluded from score/gates (ephemeral projection, no row mutation).
2. **Two identifiers.** `ENGINE_BUILD_ID` **`eng-1`→`eng-2`** (scoring semantics changed;
   runs/decisions). New `VALIDATOR_BUILD_ID = "val-1"` on **checks**; staleness keys on it.
3. **NULL provenance is unknown ⇒ stale**, never relabeled `val-1` and never backfilled
   (F1). The pre-6b PASS backlog is **closed** at cutover.
4. **Deterministic replay from a versioned, digest-verified immutable snapshot** (F2), not
   live state.
5. **Revalidation is a per-case first-class coordinator run** through a fused
   score→decide→project→outbox primitive (F4).
6. **One hashed validation boundary** for the validator drift guard (F6).
7. **Build the full writer now** (closure lane + replay lane).

## Architecture

### 1. The validation boundary + guard (F6)

Consolidate every verdict-shaping unit under a cohesive **pure** surface. Preferred: move
`validators/*`, the intent dispatch, the pure DB→`extras` normalization, the website
eligibility rule (the *pure scalar* part of `review_guard`), the snapshot codec, and the
replay-binding helpers into a `src/kyc_tool/validation_engine/` package that imports no
impure infrastructure (enforced by an import-linter contract). `VALIDATOR_BUILD_ID` is
pinned by a **framed hash over the transitive closure** of that package's `*.py`
(computed mechanically from the validation entrypoint's imports within `src/kyc_tool`, so
a newly-introduced dependency automatically extends the hash and forces a conscious `val`
bump). If a full move proves too invasive in one PR, the fallback is an **explicit
manifest** of the closure files with a test that fails when the validation entrypoint
imports a module not in the manifest — the guard message alone is not coverage.
**Mutation-tested**: editing a website-eligibility branch, the POC extras decoder, the
replay binding, or an ordinary validator must each change the closure digest.

### 2. Data model — migration 013 (`down_revision='012'`)

- **`checks.validator_build_id TEXT NULL`** + nonblank `CHECK … NOT VALID`. Existing rows
  stay NULL — **unknown provenance** (decision 3). `CheckView` gains `validator_build_id`.
- **`run_validation_snapshots`** (append-only): `run_id TEXT PK → runs.id`,
  `snapshot_json JSONB NOT NULL`, `snapshot_digest TEXT NOT NULL`,
  `captured_at timestamptz NOT NULL DEFAULT now()`. The **versioned** frozen mutable
  inputs of that run's validation (§4).
- **`engine_activation_epoch`** (append-only *pair* history, distinct from PR 6's
  immutable singleton `bundle_pinning_epoch`): `id` DB-sequenced PK,
  `engine_build_id TEXT NOT NULL`, `validator_build_id TEXT NOT NULL`,
  `activated_at timestamptz NOT NULL`. Current active pair = max-id row.
- Forward-only-after-use downgrade (refuse once any validator_build_id / snapshot /
  engine-epoch row exists). Lineage guard: PR 6b = `013`, State `shipped`.

### 3. Identifiers

`domain/engine.py`: `ENGINE_BUILD_ID = "eng-2"` (bumped; runs/decisions; whole-tree drift
guard re-pinned **with** the bump). `VALIDATOR_BUILD_ID = "val-1"` (format `^val-[1-9]\d*$`;
checks; guarded by the §1 validation-closure hash).

### 4. Versioned snapshot codec (F2)

`ValidationSnapshotV1` — a Pydantic model of **JSON primitives** (no raw `dict`/dataclass/
enum/`datetime`): `schema_version`, `evaluated_at` (ISO-8601 **UTC**, strict tz-aware
decode), scalar website-guard fields, an explicit `CheckView` list (typed fields, status
as strings), the POC token record fields, and the `extras`/normalized inputs each affected
validator needs. Serialize with `model_dump(mode="json")`; **decode with `extra="forbid"`
and fail closed** on an unknown `schema_version` or a type/format error. Persist a
**canonical digest** (`snapshot_digest`) and **verify it before replay** — "immutable
snapshot" is an enforced integrity claim. `ValidationContext` gains `evaluated_at`; the
POC validator (and any clock reader) uses `ctx.evaluated_at` (live: real now; replay: the
snapshot's) so expiry is judged as of the original validation.

### 5. One snapshot-capture seam (F3)

Extract a single snapshot **builder + persister** used by **every** path that writes a
validator-produced check — the VALIDATE branch **and** the broker-blocked DECIDE
short-circuit (`website.review_completed` appends `website_intent` directly, `pipeline.py`
~413). Compute the guard/`evaluated_at` **once** and feed the same immutable scalar
snapshot to both intent creation and persistence, inside the fused transaction.
Flag-independent; the live path stays byte-identical (guard-tested).

### 6. Provenance stamping

Thread `validator_build_id` through the checkstore writers (as PR 6 threaded
`policy_bundle_hash`); runs/decisions stamp `ENGINE_BUILD_ID = eng-2`. Every new check is
born `val-1`.

### 7. Fail-closed freshness projection (F5, F7) — shared, used in TWO places

A pure `validator_fresh_views(views, target_validator_id)`: for each view whose
`validator_build_id IS DISTINCT FROM target`, **downgrade PASS→NEEDS_REVIEW** in the
ephemeral view (0 points; reason_codes retained → a stale hard-conflict still fires gate 5;
non-PASS rows untouched). Invoked, **only when `enforce_bundle_pinning` is on**, in **both**:
- `_decide_txn` (the automatic decision path), and
- `_handle_manual_approve` (`events/ingest.py`) — which today calls `org_id_check_passed`
  on **raw** views, so a stale ORG-ID PASS could still unlock buying (F5). Settings is
  passed **explicitly** into ingest (no hidden second global lookup). A manual case may
  remain `approved_manual`, but its buy state + manual `DecisionRow` are **locked**
  (`buy_locked_org_id_required`) until a current-validator ORG-ID PASS exists.

Only stale **PASS** rows change a decision or block activation (F7).

### 8. Revalidation writer — per-case coordinator run (F4, F1)

`ops.revalidate_backlog` finds every case with a validator-stale live **PASS**
(`validator_build_id IS DISTINCT FROM VALIDATOR_BUILD_ID`, **all statuses of case**,
F1/F7) and, per case, **atomically creates a coordinator run**: an internally-synthesised
**deterministic-idempotency** `recalculate.requested` event (no new public route), a
`Run` with its **own** creation-pin bundle, an explicit **target validator id** and the
**expected stale-check-id set**, plus its job — one transaction. The pipeline handles the
job under **`Case FOR UPDATE`** (existing lock order) and, in **one fused commit**:

1. For each expected stale live PASS, **reselect it by id + `created_by_run_id` under the
   lock**; skip if a newer run replaced it (surgical — never clobber a newer check).
2. **Replay lane** — if the *source* run has a valid, digest-verified snapshot: rebuild its
   `ValidationContext` from immutable evidence + the snapshot, run the validation-boundary
   `build_intents` under the **target validator**, **re-verify the immutable binding**
   (POC: persisted event `token_id`/`token_digest` + `(rir,poc,org,resource)`, treating the
   token's own successful consumption as proof — not reuse — expiry at the snapshot's
   `evaluated_at`; website: terminal task `case`/`type`/`reviewer` vs the persisted event,
   **without** requiring `open`). Stamp the successor `created_by` the **coordinator run**,
   `policy_bundle_hash` = the **source** run's creation pin, `validator_build_id` = target.
   A tampered binding ⇒ FAIL/stale, never a free PASS.
3. **Closure lane (F1)** — if no snapshot / evidence cannot prove the original context
   (every pre-6b check): supersede the PASS with
   `NEEDS_REVIEW`/`historical_validation_context_unavailable`, `validator_build_id` = target.
4. **Score the fused case decision under the *coordinator* run's creation pin** → decide →
   project (`cases.current_score`/`latest_decision`) → **enqueue the outbox callback** →
   complete the job → write a **revalidation audit record** (coordinator run id, per source
   run/check ids, old/new check ids, `validator_build_id`, `engine_build_id`).

Extract a reusable fused **`score→decide→project→outbox` primitive**; do **not** call
`_decide_txn` on an already-complete source run. The coordinator run is a real run, so the
callback carries a **fresh `run_id`** the platform won't dedupe against an old one, and
delivery marks *that* run/decision complete. Idempotent: the deterministic event key +
"already-target checks are skipped" make a re-run a no-op.

### 9. Activation — pair compare-and-swap (F8, F7)

`ops.activate_revalidation_epoch --expect-current-engine <eng|none>
--expect-current-validator <val|none> --expect-engine eng-N --expect-validator val-N`.
Under a **transaction-scoped lock**: verify (a) the current max `(engine,validator)` pair
== expected-current, (b) the **zero-stale-PASS** target set is empty, (c) the local
`ENGINE_BUILD_ID`/`VALIDATOR_BUILD_ID` == the requested target, then **insert** the next
pair row and **read back**. Same target ⇒ idempotent; expected-predecessor→new-target ⇒
allowed (so a future `eng-3/val-2` rollout is **not** blocked by `eng-2` being active); any
other predecessor / race ⇒ **refuse without a write**. The PR 6 `bundle_pinning_epoch` is
left **immutable**. Alert: post-epoch live **PASS** checks whose `validator_build_id`
`IS DISTINCT FROM` the active validator (not NULL-only).

### 10. Cutover (drained; F1 makes this a real closure, not a no-op)

deploy PR6b image (flag off) → `alembic upgrade head` (013) → new runs now capture
snapshots → **`ops.revalidate_backlog` closes the untrusted legacy backlog** (replay lane
where a snapshot/evidence exists; closure lane superseding the rest to `needs_review` +
re-decide + callback) → verify **zero stale live PASSes** → `activate_revalidation_epoch`
pair-CAS (`--expect-current-* none --expect-engine eng-2 --expect-validator val-1`) →
enable flag → resume. Rollback: flag-off on the PR6b image; never delete epoch/snapshot
history.

### 11. Docs / governance (F9)

**ADR-006** — revalidation. **PR 10 → ADR-007** everywhere: ROADMAP §I **and** the PR 10
detailed section (both fixed in this revision) — and **extend the ROADMAP guard** to assert
the detailed-unit ADR ref and the §I assignment name the **same** number (mutating either
back to 006 must fail the test). `DEPLOYMENT.md` §10; `RUNBOOK.md`; ROADMAP PR 6b →
`shipped`/`013`; ADR-005/OVERVIEW cross-ref.

## Invariants (Global Constraints + review rubric)

- NULL validator provenance is **stale/unknown**; never relabeled or backfilled.
- The fail-closed projection **never mutates** a stored row; revalidation only
  **supersedes** (append-only) + writes an audit record.
- Only stale live **PASS** rows change a decision or block activation.
- Revalidation runs under **`Case FOR UPDATE`**, reselects the expected check id under the
  lock, and never clobbers a newer run's check.
- Replay uses **only** immutable evidence + the digest-verified snapshot; a tampered
  binding stays stale; zero adapter/network calls.
- Each per-case revalidation is a **fresh coordinator run** (real `run_id`/event); the
  fused commit covers checks + decision + case projection + outbox; a fault rolls all back;
  a retry yields exactly one decision/outbox.
- Flag-off is a **strict scoring no-op**; golden cases byte-identical.
- `ENGINE_BUILD_ID = eng-2`, `VALIDATOR_BUILD_ID = val-1`; both guards re-pinned in-commit.
  `KYC_Tool_Build_Package/` + M2 untouched; validation boundary is import-pure (2/0).

## Testing strategy (incl. Codex's named regressions)

- **Migration 013**: up/down clean; forward-only downgrade refusal; nonblank `NOT VALID`
  (`convalidated=false`); lineage guard green.
- **Validation-closure guard**: mutating a validator, the review-guard eligibility, the
  extras decoder, or the replay binding each changes the digest and forces a `val` decision.
- **Snapshot codec**: real POC/website/document runs → flush → reload in a fresh session →
  decode → **byte-stable intent replay**; malformed version/date/enum/**digest** ⇒ reported
  stale with **no** decision/check write. Real Postgres, not helper `json.dumps`.
- **Capture seam**: a **broker-blocked** website completion has a snapshot for its producing
  run; synthetic-`val-2` revalidate after the task is `done` ⇒ same PASS.
- **Fail-closed + manual (F5)**: manual-approve with NULL/`val-0` ORG PASS ⇒
  `approved_manual` + `buy_locked_org_id_required`; `val-1` ⇒ enabled; flag-off unchanged;
  event/decision/audit atomic (real signed-event path).
- **Backlog closure (F1)**: seed a pre-PR3 permissive PASS (NULL id, no snapshot) ⇒
  activation refuses; run closure ⇒ old row superseded, score/gates/case/callback all
  fail-closed, **no row retro-stamped `val-1`**.
- **Coordinator run (F4)**: mixed-source-bundle stale checks ⇒ **one** coordinator run +
  **one** callback with a **new** `run_id` (not deduped as an old run); publisher marks that
  run/decision complete; re-run idempotent. Via `Pipeline.handle_job` + `OutboxPublisher`.
- **Replay determinism**: POC + website flows → token `consumed`/task `done`, **aged past
  expiry** ⇒ revalidate to **identical PASS**; tampered digest/binding/attribution ⇒ stays
  stale + CLI nonzero. Document/registry history revalidated in **both run orders** ⇒
  identical.
- **Cascade placeholder (F7)**: real ORG→POC `needs_review` cascade, synthetic-bump ⇒
  activation blocked **only** by stale PASSes; the placeholder is reported but contributes 0.
- **Activation CAS (F8)**: none→eng2/val1, idempotent repeat, eng2/val1→eng3/val2, wrong
  local validator, unexpected predecessor, stale target, two concurrent different targets —
  through the real CLI.
- **Concurrency/fault**: two PG sessions with a barrier between select and write ⇒ a newer
  normal-run check is never superseded; fault after staging ⇒ checks/decision/case/outbox
  all roll back, retry ⇒ exactly one effect.
- **ADR (F9)**: docs test fails if either the §I or PR 10 detailed ADR ref is mutated to 006.
- Full suite green on real Postgres; ruff clean; import-linter 2/0.

## Out of scope (YAGNI)

- No adapter re-fetching. No backfill/retro-stamp of pre-6b provenance (they're closed, not
  relabeled). No multi-engine runtime beyond the two-identifier + fail-closed hook. No
  re-judging of the manual *decision* itself (only its buy-enablement freshness). No public
  revalidation route/UI (ops-only).
