# PR 6 — Per-run policy bundle pinning (ROADMAP item 7A)

**Status:** DESIGN rev 2 — Codex round-1 findings (9) folded; awaiting re-audit.
**ROADMAP:** item 7A (PR 6). PR 6b (revalidation, 7B) and PR 10 (broker snapshots)
are separate later units.
**Locked (human-approved):** Approach A (DB-backed bundle store, load-by-hash);
`ENGINE_BUILD_ID` = a deliberate domain constant guarded by a drift test.

**Rev 2 changes (Codex `768e1fc..18b9f8b` review):** 011 downgrade is
forward-only-after-use (P1.1); the engine guard hashes the full decision-shaping
closure, not a hand-picked list (P1.2); flag activation is an atomic worker-pool
cutover with a backlog preflight (P1.3); pinning scope narrowed to the **rubric**,
broker reproducibility deferred to PR 10 (P2.4); manual-approve decisions stamp
`engine_build_id` in the API (P2.5); `build_bundle` takes an explicit
`policy_dir` origin (P2.6); `shas_json` removed as redundant/unauthenticated
(P2.7); every run-created check (incl. cascades) carries the resolved hash (P3.8).

---

## 1. Problem

A run records the policy it was created under (`runs.policy_bundle_hash`, set at
ingest — `events/ingest.py:219`), but nothing guarantees the run is *scored*
under that bundle:

1. **The worker scores under its own process bundle, not the run's.** Each worker
   `load_policy(settings.policy_dir)` once at startup
   (`workers/pipeline_worker.py:83`); the `Pipeline` holds that single
   `self.policy` and folds its rubric into `score()`/`evaluate_gates()`
   (`pipeline.py:392-393`), stamping `self.policy.bundle_hash` onto decisions
   (`:449`). If the on-disk policy changed between run-creation and execution, or
   API and workers run different deploys mid-rollout, a run is silently scored
   under a **different rubric** than it recorded — an audit-integrity hole on a
   compliance-scoring engine.

2. **The bundle exists only on disk.** `load_policy` reads the seven JSON files
   from `policy_dir`; once they change, the exact bytes a past run used are gone —
   only the `bundle_hash` string survives, so a past decision can be *named* but
   not *reconstructed* or re-scored.

3. **Engine code drift is invisible.** Rubric point values/categories/conflict
   codes are baked **into each check** by validators at stamping time; `score()`
   folds `check.points_awarded`, `has_hard_conflict()` reads stamped
   `reason_codes`, `evaluate_gates()` reads stamped `category`/`status`. So the
   scoring *code* shapes the outcome independently of the JSON, and today nothing
   records which engine ran.

PR 6 pins the **rubric** each run is scored under (1), makes the exact bundle
durably reconstructable (2), and records an `engine_build_id` for the scoring
code (3). It does **not** re-score existing runs (PR 6b) and does **not** change
broker-state reproducibility (PR 10) — see §2.

## 2. Scope & non-goals

**In scope:** a durable bundle store; the worker resolves each run's bundle by
hash and scores its **rubric-consuming** steps under it; refuse-if-unloadable
behind a flag; `engine_build_id` provenance + a drift guard; a two-phase rollout
with an atomic activation cutover.

**Non-goals (explicit):**
- **Revalidation / re-scoring existing runs or checks → PR 6b.** PR 6 pins new
  runs forward and provides the load-by-hash machinery 6b reuses.
- **Broker-state reproducibility → PR 10.** `BrokerGate` reads the live
  `broker_entities` table (`orchestration/broker_gate.py:66-79`), not
  `PolicyBundle.broker_policy`; PR 10 owns immutable full-list broker snapshots.
  PR 6 stores the whole bundle (incl. `broker_policy.json`) for provenance/future
  use but does **not** rewire the broker gate.
- **Pinning the decision *rules*.** `decide()` takes no policy and hard-codes the
  JSON-described priority (`domain/decision.py:48-89`); those rules are covered by
  `engine_build_id` (they're engine code), not by bundle consumption.
- **Refuse-on-engine-mismatch.** A run records no "expected engine";
  `engine_build_id` is recorded provenance, not an enforcement gate.
- **Check-level `engine_build_id`** and **backfilling pre-migration rows** →
  deferred; new rows only.
- **Making `enforce_bundle_pinning` boot-required in production** → later follow-up.
- **Object-store bundle storage** (rejected Approach B) — storage stays in Postgres.

## 3. Schema — migration 011

New table:

```
policy_bundles(
  bundle_hash  TEXT        PRIMARY KEY,   -- the sha256 loader already computes
  files_json   JSONB       NOT NULL,      -- {filename: raw_file_text} for all 7 POLICY_FILES
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

New columns (all nullable, additive — no backfill):
- `checks.policy_bundle_hash TEXT` — the bundle each check was stamped under.
- `runs.engine_build_id TEXT` · `decisions.engine_build_id TEXT`.

(`runs.policy_bundle_hash` and `decisions.policy_shas` already exist.)

**No `shas_json`** (P2.7): per-file shas derive from `files_json` via
`build_bundle`, and the `bundle_hash` PK already commits to them (tampering any
file changes the recomputed hash → load fails, §4). A stored sha map would be
redundant provenance that `load_bundle` did not authenticate — so it is omitted
rather than left as an un-checked trust surface. `decisions.policy_shas` (written
at decide time) remains the queryable per-file provenance.

**Storage fidelity.** `files_json` holds each file's **raw text**, so
reconstruction reproduces byte-identical content and therefore the identical
`bundle_hash`; `load_bundle` re-computes and verifies it (§4).

**Downgrade is forward-only after use (P1.1).** `policy_bundles.files_json` is the
*only* retained copy of historical policy bytes, and the provenance columns sit on
otherwise-immutable `checks`/`runs`/`decisions` rows — dropping them after live
use permanently erases audit evidence PR 6 exists to preserve. So 011's downgrade
**refuses** (raises, like migration 010) once any `policy_bundles` row exists or
any of the three provenance columns is populated; a never-used schema (all empty)
still downgrades cleanly. Roll forward instead of down after use; a reviewed
export is the escape hatch. The refusal is tested.

**Indexes:** none beyond the PK (all lookups are `WHERE bundle_hash = :h`); a
`checks.policy_bundle_hash` index is deferred to PR 6b if needed.

## 4. Modules & the purity boundary

The import-linter contract *"pure core stays pure (domain/validators/policy)"*
forbids `policy/` from importing SQLAlchemy, so the DB-touching store lives
outside it:

- **`policy/loader.py` (stays pure)** — split into reusable primitives so disk and
  DB share one parse path:
  - `read_policy_files(policy_dir: Path) -> dict[str, bytes]`.
  - `build_bundle(raw: dict[str, bytes], *, policy_dir: Path | None = None) ->
    PolicyBundle` — the existing parse/validate/hash logic; the **explicit
    `policy_dir` origin** (P2.6) fills the frozen bundle's required `policy_dir`
    field (`loader.py:44`): disk load passes the real dir, DB load passes `None`.
  - `load_policy(policy_dir) = build_bundle(read_policy_files(policy_dir),
    policy_dir=policy_dir)` — identical signature/behavior; existing call sites
    unchanged. The `PolicyBundle` dataclass shape is unchanged; only its
    `policy_dir` may now be `None` for DB-sourced bundles (used solely for
    scoring, which never dereferences it — the plan confirms no scoring-path
    consumer requires it).

- **New `policy_store/repo.py` (non-pure, parallels `checkstore/`)**:
  - `store_bundle(session, raw: dict[str, bytes]) -> str` — derive `bundle_hash`
    via `build_bundle(raw)` (single hash source), `INSERT … ON CONFLICT
    (bundle_hash) DO NOTHING`. Idempotent; returns the hash.
  - `load_bundle(session, bundle_hash) -> PolicyBundle | None` — fetch row; `None`
    if absent; else reconstruct `raw = {name: text.encode()}` from `files_json`,
    `build_bundle(raw, policy_dir=None)`, **assert `bundle.bundle_hash ==
    bundle_hash`** (raise `BundleCorrupt` on mismatch — the bundle_hash commits to
    every file's bytes, so single-file tampering is caught).
  - `policy_store` imports `policy` (pure ← impure allowed); `policy` never
    imports `policy_store`. Import-linter stays 2 kept / 0 broken.

- **`domain/engine.py` (new, pure)** — `ENGINE_BUILD_ID: str`.

## 5. Worker: resolve and score each run under its recorded bundle

The `Pipeline` keeps a **process bundle** (`self.policy`) for startup seeding,
flag-off scoring, and non-run contexts, and gains a **per-run resolver**:

```
resolve_bundle(session, run) -> PolicyBundle
```
- **Flag off** → the process bundle (behavior unchanged).
- **Flag on** → `policy_store.load_bundle(session, run.policy_bundle_hash)`;
  `None` → raise `BundleUnavailable` (run fails loudly via the normal
  transition-failure path → job dead-letters with a clear `last_error`; no check
  or decision written). Cached in-process by hash (bundles are immutable).

**What the resolved bundle drives (narrowed — P2.4).** The **rubric** — the only
part of the bundle the engine actually consumes: validator point/category/reason
stamping in RUN_ADAPTERS/VALIDATE, the `score()` threshold, and
`evaluate_gates()` categories/threshold. It does **not** rewire `BrokerGate`
(live `broker_entities` → PR 10) or `decide()` (hard-coded priority → covered by
`engine_build_id`). The **whole** bundle is still stored (all 7 files) for
provenance and reuse by PR 6b/PR 10. The pin target is "the rubric in effect when
the run was created."

**Every run-created check carries the resolved hash (P3.8).** Not only
`apply_check_intents`, but also the successor checks written by identity
invalidation and the ORG-ID→POC cascade via `checkstore.write_check` /
`supersede_without_replacement` (`checkstore/repo.py:127-308`) — the resolved
`bundle_hash` is threaded to all run-created writes, so no immutable successor is
left `NULL`.

**Provenance written in both flag states.** `checks.policy_bundle_hash` = the
resolved bundle's hash; `engine_build_id` = `ENGINE_BUILD_ID` on `runs` and
`decisions`. In flag-off this can surface the drift PR 6 fixes; flag-on makes
run-hash and check-hash equal by construction.

**Manual-approve decisions (P2.5).** `reviewer.manual_approve` creates
`DecisionRow(run_id=None)` synchronously in the API
(`events/ingest.py:241-266`), and the engine still computes ORG-ID/buy-enablement
there — so that path **also stamps `ENGINE_BUILD_ID`** on its decision row (the
one non-worker stamper). Post-011 decision rows never carry accidental,
path-dependent provenance.

**Seeding guarantees availability.** The API app factory and every worker call
`policy_store.store_bundle(session, read_policy_files(settings.policy_dir))` at
startup (idempotent, before serving/claiming), so every hash a run can record is
present; flag-on refusal only ever hits genuinely-unknown historical hashes.

## 6. `engine_build_id` + the decision-shaping guard (P1.2)

- `domain/engine.py::ENGINE_BUILD_ID` is bumped in a reviewed commit whenever
  decision **semantics** change — the discipline each policy JSON's `version` has.
- **Guard test** (`tests/policy_driven/test_engine_build_id_guard.py`): sha256
  over the **full decision-shaping source closure**, not a hand-picked subset.
  The closure is defined broadly and stably as:
  - **entire packages** `src/kyc_tool/domain/**.py` and
    `src/kyc_tool/validators/**.py` (globbed, so new files auto-join), plus
  - the named impure modules that shape a decision or a check's stamping:
    `orchestration/pipeline.py` (bundle/rubric selection, the enforcement overlay,
    projection), `checkstore/repo.py` (points/category stamping, supersession/
    cascade), `events/ingest.py` (the manual-decision path).
  The concatenated sorted source is asserted equal to a pinned
  `EXPECTED_ENGINE_SOURCE_HASH`. On mismatch: *"engine source changed — if
  decision semantics changed, bump ENGINE_BUILD_ID; re-pin
  EXPECTED_ENGINE_SOURCE_HASH in the same commit."*
- **Coverage proof:** a test asserts a semantic edit **outside `domain/`** — e.g.
  changing the positive-decision enforcement overlay in `pipeline.py` — trips the
  guard (demonstrated by including `pipeline.py` in the manifest, verified by a
  manifest-membership assertion listing every hashed path).
- **Completeness invariant:** the pure packages are globbed (can't silently drop a
  file); the impure manifest is the reviewed SSOT for "decision logic outside the
  pure core," and a meta-assertion fails if a `domain/` or `validators/` file is
  missing from the hash set. Adding decision logic in a *new* impure module is a
  reviewed manifest change (called out in ADR-005). The guard fires on any edit to
  the closure (incl. cosmetic) — the intended forcing function: every change is a
  conscious *(bump id + re-pin)* or *(re-pin only)* decision.

## 7. Rollout — `enforce_bundle_pinning`, two phases, atomic activation (P1.3)

`Settings.enforce_bundle_pinning: bool = False` (mirrors
`enforce_positive_decisions`, `config.py:94`), read per process at startup.

- **Phase 1 — deploy, flag off.** Migration 011 applied; API + workers seed the
  on-disk bundle at startup; workers write the new provenance columns for all new
  runs; scoring still uses the process bundle → **zero behavior change**, while
  the store fills and provenance capture begins.
- **Phase 2 — activation is an atomic worker-pool cutover, not a rolling flip.**
  Because `Settings` is per-process, "turn the flag on" is a restart; a *rolling*
  restart would let an old flag-off worker and a new flag-on worker both claim the
  same run kind and score it under different bundles (nondeterministic). The
  activation contract:
  1. **Backlog preflight** — run `ops.verify_pinnable_backlog` (new one-shot,
     parallels `ops.requeue_interrupted_jobs`): for every runnable/requeueable run
     (queued, running, and dead-but-requeueable) assert its non-NULL
     `policy_bundle_hash` loads from `policy_bundles`; it reports any gap. Abort
     activation and seed the missing bundle(s) first if any run fails.
  2. **Stop the entire pipeline-worker pool** (API ingestion stays up and keeps
     queuing — no full maintenance window).
  3. Enable `enforce_bundle_pinning`; **start only flag-on workers**; attest the
     config (all workers report the flag on).
  4. Resume.
  This is narrower than PR 5b's full window (workers only). Documented in
  `DEPLOYMENT.md` as the PR 6 activation procedure.
- **Fail-closed, never fall back.** Flag-on + missing bundle → refuse (§5), never
  the process bundle. Runbook: reprocessing a pre-migration run under pinning
  requires seeding its bundle first.
- **Readiness.** With the flag on, `/readyz` also verifies the process bundle is
  loadable from `policy_bundles` (a mis-seeded instance drains). This is a
  per-instance health check — **not** the backlog/worker gate above.

## 8. Testing (real Postgres unless noted)

1. **Migration 011** up/down: upgrade creates table+columns; **downgrade on a
   never-used schema succeeds**; **downgrade refuses once a `policy_bundles` row
   or any provenance column is populated** (P1.1).
2. **`policy_store` round-trip:** store→load reconstructs a bundle whose recomputed
   `bundle_hash` == stored; unknown hash → `None`; tampered `files_json` →
   `BundleCorrupt`; `store_bundle` idempotent (one row on repeat).
3. **Reconstruction equivalence (unit):** DB-reconstructed bundle == disk
   `load_policy` on `bundle_hash` + parsed models (**not** `policy_dir`, which is
   `None` vs a path) (P2.6).
4. **`ENGINE_BUILD_ID` guard** (P1.2): pins the closure hash; a semantic edit
   **outside `domain/`** (in `pipeline.py`) trips it; the manifest-membership
   assertion lists every hashed path; a missing `domain/`/`validators/` file fails
   the meta-assertion.
5. **Flag off:** provenance columns populated; existing golden cases unchanged.
6. **Flag on, bundle present:** the run scores correctly under its resolved bundle.
7. **Flag on, bundle absent:** job dead-letters with a clear error; no check/
   decision written.
8. **Headline — cross-bundle *rubric* pinning (P2.4).** Seed bundle X; create a
   case+run under X. Build a modified bundle Y differing in a **rubric** field (a
   higher `score_met` threshold or a changed check's points), seed Y, point the
   worker's *process* bundle at Y. Flag on → the run is scored under **X** (loaded
   by hash): a case that is `approve` under X but `manual_review_insufficient`
   under Y decides `approve`. (Flag-off counterpart decides under Y with
   provenance recording Y.) Broker/decision-rule differences are **not** part of
   this proof (deferred, §2).
9. **Cascade provenance (P3.8):** identity-invalidation and ORG-ID→POC cascade
   successor checks carry the resolved `policy_bundle_hash` (flag off and on).
10. **Manual-approve decision (P2.5):** a `reviewer.manual_approve` after 011
    writes `DecisionRow.engine_build_id == ENGINE_BUILD_ID` (not NULL).
11. **`/readyz`** 503 when the flag is on and the process bundle is absent from
    `policy_bundles`; 200 when present.
12. **`ops.verify_pinnable_backlog`** (P1.3): reports zero gaps when all runnable
    runs' bundles are seeded; flags a run whose hash is absent.
13. SSOT/policy drift guard green; import-linter 2 kept / 0 broken.

## 9. Observability & docs

- **Metrics:** a pinning refusal is visible via `jobs_by_status.dead` +
  `last_error`; no new metric endpoint for PR 6.
- **Docs:** **ADR-005 — "Per-run policy bundle pinning"** (next sequential; ADRs
  run 001–004, 004 = PR 5b). The ROADMAP currently reserves ADR-005 for PR 10 —
  PR 6 takes 005, so bump that reservation to **ADR-006** (the renumber move PR 5b
  applied for 004). ADR-005 records the rubric-only scope, the engine-guard
  closure + completeness invariant, and the forward-only-after-use downgrade.
  `DEPLOYMENT.md`: the Phase-1 deploy + the Phase-2 atomic worker-pool activation
  (preflight → stop pool → flag on → start flag-on workers → resume).
  `RUNBOOK.md`: the flag, the reprocess-needs-seed note, the `/readyz` check, the
  `ops.verify_pinnable_backlog` preflight. `OVERVIEW.md` "How updates work": a
  policy change is now durably pinned per run. `.agents/ROADMAP.md`: item 7A
  shipped, 7B (PR 6b) still pending for M4.
- `AUDIT_FINDINGS.md`: record any build-time deviation.

## 10. Constraints

- `KYC_Tool_Build_Package/` immutable (the 7 JSONs are read and stored verbatim).
  `HARD_CONFLICT_REASON_CODES` stays engine code (in the guard closure), not
  forced into the immutable JSON.
- **M2 untouched** — do not touch `KYC_ENFORCE_POSITIVE_DECISIONS`.
- `policy/` stays pure; import-linter unchanged.
- Parent is sole committer/pusher/bus-writer; subagent-driven build; DB tests run
  red→green on real Postgres before commit.
