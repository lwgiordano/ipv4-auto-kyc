# PR 6 — Per-run policy bundle pinning (ROADMAP item 7A)

**Status:** DESIGN — awaiting human review, then `writing-plans`.
**ROADMAP:** item 7A (PR 6). PR 6b (revalidation, item 7B) is a separate later unit.
**Design decisions locked (human-approved):** Approach A (DB-backed bundle store,
load-by-hash); `ENGINE_BUILD_ID` = a deliberate domain constant guarded by a
drift test.

---

## 1. Problem

A run records the policy it was created under (`runs.policy_bundle_hash`, set at
ingest — `events/ingest.py:219`), but nothing guarantees the run is *scored*
under that bundle:

1. **The worker scores under its own process bundle, not the run's.** Each worker
   calls `load_policy(settings.policy_dir)` once at startup
   (`workers/pipeline_worker.py:83`) and the `Pipeline` holds that single
   `self.policy`. It stamps `self.policy.bundle_hash` onto decisions
   (`orchestration/pipeline.py:449`) and folds `self.policy`'s rubric into
   `score()`/`evaluate_gates()` (`pipeline.py:392-393`). If the on-disk policy
   changed between run-creation and run-execution, or the API and workers run
   different deploys during a rolling release, a run is silently scored under a
   *different* bundle than it recorded. On a compliance-scoring engine this is an
   audit-integrity hole: the recorded provenance can disagree with what actually
   produced the decision.

2. **The bundle exists only on disk.** `load_policy` reads the seven normative
   JSON files from `policy_dir`. Once those files change (a deploy), the exact
   bytes a past run used are gone — only the `bundle_hash` string survives, so a
   past decision can be *named* but not *reconstructed* or re-scored.

3. **Engine code drift is invisible.** The rubric's point values, categories, and
   conflict reason-codes are baked **into each check** by validators at stamping
   time; `score()` just folds `check.points_awarded`, `has_hard_conflict()` reads
   stamped `reason_codes`, `evaluate_gates()` reads stamped `category`/`status`.
   So the scoring *code* (validators + `domain/scoring.py` incl.
   `HARD_CONFLICT_REASON_CODES`, `domain/decision.py`, `domain/reasons.py`) shapes
   the outcome independently of the JSON. Two runs with the same `bundle_hash`
   but different engine code can produce different decisions, and today nothing
   records which engine ran.

PR 6 closes (1) and (2) by pinning each run to its recorded bundle, loaded from a
durable store; and makes (3) auditable by recording an `engine_build_id`. It does
**not** re-score existing runs — that is PR 6b (revalidation).

## 2. Scope & non-goals

**In scope:** durable bundle store; worker resolves and scores each run under its
recorded bundle by hash; refuse-if-unloadable behind a flag; `engine_build_id`
provenance + a guard test; two-phase rollout.

**Non-goals (explicit):**
- **Revalidation / re-scoring existing runs or checks → PR 6b.** PR 6 only pins
  runs going forward and provides the load-by-hash machinery 6b will reuse.
- **Refuse-on-engine-mismatch.** A run records no "expected engine," so there is
  nothing to refuse against; `engine_build_id` is *recorded provenance* in PR 6,
  not an enforcement gate. (Engine-consistency enforcement, if wanted, is a later
  decision.)
- **Check-level `engine_build_id`.** Deferred to PR 6b unless revalidation proves
  it necessary; PR 6 adds only `checks.policy_bundle_hash` (per the ROADMAP).
- **Backfilling pre-migration checks/runs.** Rows created before migration 011
  keep `NULL` in the new columns; they are not retro-stamped.
- **Making `enforce_bundle_pinning` a production-required kill switch.** It rolls
  out flag-gated; graduating it into the boot-required set is a later follow-up.
- **Object-store bundle storage** (rejected Approach B) — storage stays in Postgres.

## 3. Schema — migration 011

New table:

```
policy_bundles(
  bundle_hash  TEXT        PRIMARY KEY,   -- the sha256 loader already computes
  files_json   JSONB       NOT NULL,      -- {filename: raw_file_text} for all 7 POLICY_FILES
  shas_json    JSONB       NOT NULL,      -- {filename: sha256} provenance/integrity
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
```

New columns (all nullable, additive — no backfill):
- `checks.policy_bundle_hash TEXT` — the bundle each check was stamped under.
- `runs.engine_build_id TEXT` — the engine that processed the run.
- `decisions.engine_build_id TEXT` — the engine that produced the decision.

(`runs.policy_bundle_hash` and `decisions.policy_shas` already exist.)

**Storage fidelity.** `files_json` holds each file's **raw text** (not
re-serialized JSON), so reconstruction reproduces byte-identical content and
therefore the identical `bundle_hash`. `load_bundle` re-computes the hash after
reconstruction and refuses on mismatch (§4), so any storage/transport corruption
fails closed rather than mis-loading.

**Down-migration** drops the three columns and the table. Fully reversible — no
immutable-audit-row hazard (unlike migration 010), nothing destructive.

**Indexes:** none beyond the PK; all lookups are `WHERE bundle_hash = :h` (the
PK). A `checks.policy_bundle_hash` index is deferred to PR 6b if its revalidation
queries need it.

## 4. Modules & the purity boundary

The import-linter contract *"pure core stays pure (domain/validators/policy)"*
forbids `policy/` from importing SQLAlchemy. The bundle **store touches the DB**,
so it cannot live in `policy/`. Split:

- **`policy/loader.py` (stays pure)** — refactor into two reusable primitives so
  disk-load and DB-reconstruction share one parse path:
  - `read_policy_files(policy_dir: Path) -> dict[str, bytes]` — read the 7 files.
  - `build_bundle(raw: dict[str, bytes]) -> PolicyBundle` — the existing
    parse/validate/hash logic, now taking raw bytes.
  - `load_policy(policy_dir)` becomes `build_bundle(read_policy_files(policy_dir))`
    — identical signature and behavior; all existing call sites unchanged.
  - `PolicyBundle`'s shape is **unchanged** (no `raw_files` field added — seeding
    passes the raw dict separately, below).

- **New `policy_store/repo.py` (non-pure, parallels `checkstore/`)** — the only
  new DB module:
  - `store_bundle(session, raw: dict[str, bytes]) -> str` — derive `bundle_hash` +
    `shas` via `build_bundle(raw)` (single source of the hash logic), then
    `INSERT INTO policy_bundles (…) VALUES (…) ON CONFLICT (bundle_hash) DO
    NOTHING`. Idempotent. Returns the hash.
  - `load_bundle(session, bundle_hash) -> PolicyBundle | None` — fetch the row;
    `None` if absent; else reconstruct `raw = {name: text.encode()}` from
    `files_json`, `bundle = build_bundle(raw)`, **assert
    `bundle.bundle_hash == bundle_hash`** (integrity — raise `BundleCorrupt` on
    mismatch), return `bundle`.
  - `policy_store` may import `policy` (pure ← impure is allowed); `policy` must
    never import `policy_store`. The import-linter run stays 2 kept / 0 broken.

- **`domain/engine.py` (new, pure)** — `ENGINE_BUILD_ID: str` (a short opaque
  identifier, e.g. `"eng-1"`), the deliberate engine-version constant.

## 5. Worker: resolve and score each run under its recorded bundle

The `Pipeline` keeps a **process bundle** (`self.policy`) — used for startup
seeding, flag-off scoring, and any non-run context — but gains a **per-run
resolver**:

```
resolve_bundle(session, run) -> PolicyBundle
```

- **Flag off** → returns the process bundle (`self.policy`). Behavior unchanged.
- **Flag on** → `policy_store.load_bundle(session, run.policy_bundle_hash)`; if
  `None` → raise `BundleUnavailable` (the run fails loudly via the normal
  transition-failure path → job dead-letters with a clear `last_error`; no check
  or decision is written). Results are cached in-process by hash (bundles are
  immutable, so the cache is safe and bounded by the handful of distinct bundles
  a process sees).

The **resolved** bundle — not `self.policy` — is then used for every
policy-consuming step of that run: broker-gate (`broker_policy`), validator
stamping in RUN_ADAPTERS/VALIDATE (rubric points/categories/reason-codes),
`score()` threshold, `evaluate_gates()` broker set, and the decision policy in
DECIDE. The run records its bundle at ingest, so the pin target is *"the bundle
in effect when the run was created."*

**Seeding guarantees availability.** Both the API app factory and every worker
call `policy_store.store_bundle(session, read_policy_files(settings.policy_dir))`
at startup (idempotent). Because any live API that can *record* a bundle on a run
has itself seeded that bundle, every hash a run can carry is present in the table
— so flag-on refusal only ever hits genuinely-unknown historical hashes.

**Provenance is written in both flag states.** Regardless of the flag, the worker
stamps `checks.policy_bundle_hash` = the hash of the bundle actually used to
stamp the check (= resolved bundle), and `engine_build_id` = `ENGINE_BUILD_ID` on
`runs` and `decisions`. In flag-off this can surface the very drift PR 6 fixes
(a run whose `runs.policy_bundle_hash` ≠ its checks' hash); flag-on makes them
equal by construction.

## 6. `engine_build_id` + guard test

- `domain/engine.py::ENGINE_BUILD_ID` is bumped in a reviewed commit whenever
  scoring/gate/validator/decision **semantics** change — the same discipline as
  each policy JSON's `version`.
- **Guard test** (`tests/policy_driven/test_engine_build_id_guard.py`, alongside
  the existing policy drift guard): sha256 over the concatenated source bytes of
  the engine-defining modules — `domain/scoring.py`, `domain/decision.py`,
  `domain/reasons.py`, `domain/engine.py`, and `validators/*.py` — asserted equal
  to a pinned `EXPECTED_ENGINE_SOURCE_HASH` in the test. On mismatch the test
  fails with: *"engine source changed — if scoring semantics changed, bump
  ENGINE_BUILD_ID; then re-pin EXPECTED_ENGINE_SOURCE_HASH in the same commit."*
- This deliberately fires on **any** edit to those modules (including comments/
  formatting). That is the intended forcing function: every engine-module change
  becomes a conscious choice — *(bump id + re-pin)* for a semantic change, or
  *(re-pin only)* for a cosmetic one — so a semantic change can never slip through
  without a decision. The module list is the SSOT for "what counts as the engine."

## 7. Rollout — `enforce_bundle_pinning` flag, two phases, fail-closed

`Settings.enforce_bundle_pinning: bool = False` (mirrors
`enforce_positive_decisions`, `config.py:94`).

- **Phase 1 — deploy, flag off.** Migration 011 applied; API + workers seed the
  on-disk bundle at startup; workers write the new provenance columns for all new
  runs; scoring still uses the process bundle → **zero behavior change**, while
  the table fills and provenance capture begins.
- **Phase 2 — flip the flag on.** Workers resolve each run's bundle by its
  recorded hash and score under it; unloadable hash → fail the run loudly (§5).
- **Fail-closed, never fall back.** Flag-on with a missing bundle row never
  silently uses the process bundle — it refuses. Correct, because a genuinely-
  unknown hash means a pre-migration run (or a lost bundle), which must not be
  mis-scored. Runbook: to reprocess such a run under pinning, seed its bundle
  first.
- **Readiness.** When `enforce_bundle_pinning` is on, `/readyz` additionally
  verifies the process bundle is loadable from `policy_bundles` (a mis-seeded
  instance drains rather than dead-lettering live traffic). Off → no new check.

## 8. Testing (all against real Postgres unless noted)

1. **Migration 011** up/down clean (extend `test_migrations`): columns + table
   created on upgrade, dropped on downgrade.
2. **`policy_store` round-trip:** `store_bundle` then `load_bundle` returns a
   bundle whose recomputed `bundle_hash` == the stored hash; unknown hash →
   `None`; a tampered `files_json` row → `load_bundle` raises `BundleCorrupt`;
   `store_bundle` is idempotent (second call no-ops, one row).
3. **Reconstruction equivalence (unit):** `build_bundle(read_policy_files(dir))`
   == the DB-reconstructed bundle — same `bundle_hash`, same parsed models.
4. **`ENGINE_BUILD_ID` guard** pins the engine-module hash (fails if engine
   source changes without re-pinning).
5. **Flag off:** provenance columns populated (`checks.policy_bundle_hash`,
   `runs`/`decisions.engine_build_id`); all existing golden cases unchanged.
6. **Flag on, bundle present:** a run whose recorded bundle is seeded scores
   correctly under it.
7. **Flag on, bundle absent:** a run recording an unseeded hash → job
   dead-letters with a clear error; **no** check or decision written.
8. **Headline proof — cross-bundle pinning.** Seed bundle X; create a case+run
   under X. Build a modified bundle Y (e.g. a higher `score_met` threshold or a
   changed check's points) and point the worker's *process* bundle at Y (and seed
   Y). Flag on → the worker scores the run under **X** (loaded by hash), and the
   decision reflects X's semantics, not Y's — e.g. a case that is `approve` under
   X but would be `manual_review_insufficient` under Y decides `approve`. This is
   the whole point of the PR, proven end-to-end on real Postgres. (Flag-off
   counterpart: same setup decides under Y — current behavior — with provenance
   columns recording Y.)
9. **`/readyz`** returns 503 when `enforce_bundle_pinning` is on and the process
   bundle is absent from `policy_bundles`; 200 when present.
10. SSOT / policy drift-guard suite stays green; import-linter 2 kept / 0 broken.

## 9. Observability & docs

- **Metrics:** surface `jobs_by_status.dead` already alerts on refused runs; add a
  count/label so a pinning refusal is distinguishable in `last_error` (no new
  metric endpoint required for PR 6).
- **Docs:** **ADR-005 — "Per-run policy bundle pinning"** (the next sequential
  number: current ADRs run 001–004, with 004 = PR 5b). The ROADMAP currently
  *reserves* ADR-005 for PR 10; PR 6 takes 005, so bump that reservation to
  **ADR-006** — the same renumber move PR 5b applied when it took 004. Also:
  `RUNBOOK.md` (the two-phase flag rollout, the reprocess-needs-seed note, the
  `/readyz` pinning check); `OVERVIEW.md` "How updates work" already distinguishes
  cutover types — add that a policy change is now durably pinned per run;
  `.agents/ROADMAP.md` mark item 7A shipped, note 7B (PR 6b) still pending for M4.
- `AUDIT_FINDINGS.md`: record any deviation discovered during build.

## 10. Constraints

- `KYC_Tool_Build_Package/` immutable (the 7 JSONs are read and stored verbatim;
  never edited). `HARD_CONFLICT_REASON_CODES` stays engine code (covered by
  `engine_build_id`), **not** forced into the immutable JSON.
- **M2 untouched** — do not touch `KYC_ENFORCE_POSITIVE_DECISIONS`.
- No new global purity violations; `policy/` stays pure.
- Parent is sole committer/pusher/bus-writer; subagent-driven build; every
  DB test runs red→green on real Postgres before commit.
