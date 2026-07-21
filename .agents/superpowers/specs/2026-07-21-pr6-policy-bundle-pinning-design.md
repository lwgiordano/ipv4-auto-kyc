# PR 6 — Per-run policy bundle pinning (ROADMAP item 7A)

**Status:** DESIGN rev 5 — Codex rounds 1–4 folded; awaiting re-audit.
**ROADMAP:** item 7A (PR 6). PR 6b (revalidation), PR 8 (immutable evidence),
PR 10 (broker snapshots) are separate later units.
**Locked (human-approved):** Approach A (DB-backed bundle store, load-by-hash);
`ENGINE_BUILD_ID` = a deliberate domain constant guarded by a framed whole-tree test.

**Rev 5 changes (Codex `0dc514d..c3b7544` review):** migration 011 gains the
singleton **`bundle_pinning_epoch`** table (P1.1); the authoritative decide txn
(incl. broker-blocked short-circuit) stamps **both** `runs.engine_build_id` and
`decisions.engine_build_id` atomically for automatic runs (P1.2); the bundle is
**resolved/refused at job entry — before any adapter/side-effect** (no POC email
on an absent-bundle run) (P1.3); normal rollback is **flag-off on the PR6
image**, never the pre-epoch writer (P1.4); the post-epoch-NULL alert uses
**per-surface timestamps** (P2.5); the headline test compares like types
(`policy_shas` is an object) + re-derives the hash from shas (P2.6); §8 adds a
**rollback acceptance test** (P3.7).

---

## 1. Problem

A run records the policy it was created under (`runs.policy_bundle_hash`, written
**once** at ingest — `events/ingest.py:216-220`), but nothing guarantees the run
is scored, or its decision recorded, under that bundle: the worker uses its own
process bundle for scoring and for the recorded provenance
(`pipeline.py:392-393,415-450`); the exact bytes live only on disk; and the
scoring *engine code* (which bakes points/categories/reason codes into checks) is
unrecorded. PR 6 pins the **rubric** each run is scored under, records which
bundle **and engine** actually ran without disturbing the creation pin, makes the
bundle durably reconstructable, and refuses cleanly when a pinned bundle is
missing. Non-goals: revalidation → PR 6b; immutable evidence → PR 8; broker-state
reproducibility → PR 10.

## 2. Scope & non-goals

**In scope:** a durable bundle store; the worker resolves each run's creation-pin
bundle by hash **at job entry** and uses it for scoring + all automatic
provenance (bundle *and* engine); refuse-if-unloadable behind a flag before any
side effect; `engine_build_id` + a framed whole-tree guard; a two-phase rollout
with a drained atomic activation, a durable epoch record, and a mirrored
flag-only rollback.

**Non-goals:** revalidation → PR 6b; immutable source evidence → PR 8;
broker-state reproducibility → PR 10 (`BrokerGate` reads live `broker_entities`,
`broker_gate.py:66-79`); pinning decision *rules* (`decide()` hard-codes priority,
`decision.py:48-89` — covered by `engine_build_id`); refuse-on-engine-mismatch;
check-level `engine_build_id`; backfilling pre-epoch rows; boot-required flag;
object-store storage.

## 3. Schema — migration 011

```
policy_bundles(
  bundle_hash  TEXT PRIMARY KEY,
  files_json   JSONB NOT NULL,   -- {filename: base64(raw_bytes)} for all 7 POLICY_FILES
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
bundle_pinning_epoch(            -- singleton; the activation record (P1.1)
  id               INT  PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  activated_at     TIMESTAMPTZ NOT NULL,
  bundle_hash      TEXT NOT NULL,
  engine_build_id  TEXT NOT NULL
)
```
- New nullable columns: `checks.policy_bundle_hash`, `runs.engine_build_id`,
  `decisions.engine_build_id`. `runs.policy_bundle_hash` unchanged = immutable
  creation pin.
- **`files_json` base64** (JSONB can't hold `bytes`; decoded to identical bytes
  before `build_bundle`; non-ASCII round-trip tested). No `shas_json`.
- **`bundle_pinning_epoch`** is written once by `ops.activate_bundle_pinning_epoch`
  via `INSERT … (id=1, …) ON CONFLICT (id) DO NOTHING` (compare-and-set: first
  activation wins, reruns/concurrent no-op, row immutable thereafter).
- **Downgrade forward-only after use:** refuses once any `policy_bundles` row, any
  provenance column, **or the epoch row** is populated; empty schema downgrades.
  Tested.

## 4. Modules & the purity boundary

`policy/` may not import SQLAlchemy.
- **`policy/loader.py` (pure):** `read_policy_files(dir) -> dict[str,bytes]`;
  `build_bundle(raw, *, policy_dir: Path | None = None)`; `load_policy(dir) =
  build_bundle(read_policy_files(dir), policy_dir=dir)`. `PolicyBundle.policy_dir`
  deliberately widened `Path` → `Path | None` (disk=dir, DB=None; provenance-only).
- **New `policy_store/repo.py` (non-pure):** `store_bundle(session, raw)` (base64
  → idempotent `ON CONFLICT DO NOTHING`); `load_bundle(session, hash) ->
  PolicyBundle | None` (base64-decode → `build_bundle(…, policy_dir=None)` →
  assert recomputed hash == hash else `BundleCorrupt`). `policy_store` imports
  `policy`, never the reverse.
- **New `policy_store` epoch helpers** (or `ops`): `activate_epoch`,
  `read_epoch`. **`domain/engine.py` (pure):** `ENGINE_BUILD_ID: str`.

## 5. Worker: resolve at job entry; stamp bundle AND engine; keep the run pin

**Resolve/refuse before any side effect (P1.3).** At the **start of handling any
run job** — before the transition's broker call, adapters, upstream requests, or
`SideEffects` (which can create review tasks or mint a POC token + enqueue its
email, `side_effects.py:56-164`) — the worker calls `resolve_bundle(session,
run)`: flag off → the process bundle; flag on → `load_bundle(session,
run.policy_bundle_hash)`, `None` → `BundleUnavailable` → the run fails loudly
(dead-letter) with **zero adapter calls, side effects, tokens, or outbox rows**.
The resolved bundle is threaded through every subsequent transition of that run
(cached by hash). `runs.policy_bundle_hash` is the resolve *input* and is never
rewritten (immutable creation pin).

**What the resolved bundle drives:** the check-type allow-list →
`validators/build.py::build_intents` (`:17-60`); per-check points+category →
`checkstore.apply_check_intents` (`checkstore/repo.py:250-279`); the score
threshold + allowed broker statuses → `score()`/`evaluate_gates()`
(`pipeline.py:392-393`). Validator pass/fail + reason codes are code
(`engine_build_id`).

**Automatic decision provenance — bundle AND engine, atomic (P1.2).** In the
authoritative decide transaction (incl. the broker-blocked DECIDE short-circuit,
`pipeline.py:301-450`), atomically with the checks/decision:
- `checks.policy_bundle_hash` = resolved `bundle_hash` (every run-created check,
  incl. identity-invalidation / ORG-ID→POC successors via `write_check`/
  `supersede_without_replacement`);
- `DecisionRow.policy_shas` = resolved `shas`; a DECIDE-audit-detail
  `resolved_policy_bundle_hash` = resolved `bundle_hash`;
- **`runs.engine_build_id` = `decisions.engine_build_id` = `ENGINE_BUILD_ID`.**
None of these touch `runs.policy_bundle_hash`. **Semantics:** `run.engine_build_id`
is set iff the run reaches the decide txn (produces a decision); a run that fails
before deciding keeps `NULL` (no decision was made under any engine).

**Manual-approve decisions:** the API path (`events/ingest.py:241-266`) stamps
`ENGINE_BUILD_ID` on its `DecisionRow(run_id=None)`.

**Seeding:** API app factory + every worker `store_bundle(session,
read_policy_files(settings.policy_dir))` at startup before serving/claiming.

## 6. `engine_build_id` + framed whole-tree guard

`domain/engine.py::ENGINE_BUILD_ID`, bumped on decision-semantic changes. The
guard test sha256s **framed records** — for each `src/kyc_tool/**/*.py`
(excluding `__pycache__`), in sorted relative-path order, `relative_path + NUL +
str(byte_length) + NUL + bytes` — asserted equal to a pinned
`EXPECTED_ENGINE_SOURCE_HASH`. Framing binds path+boundary (a rename, a cross-file
move, or an empty `__init__.py` all trip it). Mechanically complete — no curated
list can omit `broker_gate.py`/`review_guard.py`/`triggers.py`/`loader.py`. On
mismatch: bump `ENGINE_BUILD_ID` if semantics changed, re-pin the hash in the same
commit. Tests: content edit, cross-file move, rename, empty-file add, and a
non-`domain/` semantic edit (`broker_gate.py` → CLEAR) each trip it.

## 7. Rollout — epoch, drained activation, flag-only rollback

`Settings.enforce_bundle_pinning: bool = False`.

- **Phase 1 — deploy 011 + provenance epoch.** Migration, seeding, provenance
  writes, and (process-bundle) scoring ship rolling. Coverage is **not** universal:
  a rollover old replica can write NULL provenance — including an old API applying
  an inline manual approval with no `engine_build_id`, which is enforced
  independently of the M2 flag. The boundary is made trustworthy by the durable
  idempotent **`ops.activate_bundle_pinning_epoch`** — run **only after every old
  API and worker is confirmed gone**, it compare-and-sets the singleton
  `bundle_pinning_epoch` (active `bundle_hash` + `ENGINE_BUILD_ID` + timestamp).
- **Post-epoch-NULL alert — per-surface predicate (P2.5).** A queued run
  legitimately has `engine_build_id=NULL`, so the alert is pinned per surface:
  **`checks`** with `created_at` > epoch and NULL `policy_bundle_hash`;
  **`decisions`** (manual and automatic) with `decided_at` > epoch and NULL
  `engine_build_id`; **automatic runs** flagged only via a post-epoch decision
  (decide-time witness), so `QUEUED`/failed-without-decision runs are never
  flagged. Test: a legitimate queued run is not flagged; a pre-epoch-created,
  post-epoch-decided row missing its stamp **is** flagged.
- **Phase 2 — drained atomic worker-pool activation.**
  1. `ops.verify_pinnable_backlog` — every runnable/requeueable run's hash loads;
     abort + `seed_policy_bundle` on any gap.
  2. **Disable autoscaling/restarts; confirm at the orchestrator that zero old
     workers remain** (a row count ≠ process quiescence; fencing is PR 7a — an
     unfenced old handler could still commit, `queue/worker.py:42-75`).
  3. **`ops.requeue_interrupted_jobs`** (its precondition is "all workers
     confirmed stopped") recovers stop-interrupted jobs; asserts zero `running`.
  4. Enable the flag; start only flag-on workers, each logging a startup
     **attestation** (`flag/bundle_hash/engine_build_id`); verify every replica.
  5. Resume. (API stays up throughout.)
- **Rollback is flag-only on the PR6 image (P1.4).** The pre-PR6 image knows none
  of the new columns, so resuming production on it after the epoch writes
  permanent post-epoch NULLs (a defect). Normal rollback therefore **disables the
  flag on the PR6 provenance-writing image** at the same boundary (stop → confirm
  zero old workers → `requeue_interrupted_jobs` → start flag-**off** PR6 workers);
  the creation pin and provenance writing are preserved, scoring returns to the
  process bundle. Retain migration 011 (never downgrade after use). If the PR6
  code itself must be withdrawn, that requires a forward/backport fix retaining the
  011 schema, seeding, bundle+engine provenance, readiness, and epoch — **never**
  resume on the pre-epoch writer after activation.
- **`ops.seed_policy_bundle --policy-dir <dir> --expect-hash <hash>`:** read →
  `build_bundle` → **compare to `--expect-hash` → only then store** (never insert
  on mismatch; 011 is forward-only). Only supported historical recovery.
- **Fail-closed;** `/readyz` verifies the API process bundle loads **unconditionally**
  after 011; the worker witness is the startup attestation.

## 8. Testing (real Postgres unless noted)

1. **011** up/down: empty-schema downgrade succeeds; downgrade **refuses** once a
   bundle row, provenance column, **or the epoch row** is populated. Epoch table
   schema present.
2. **`policy_store` round-trip incl. non-ASCII bytes:** base64 store→load
   byte-identical, per-file SHA + `bundle_hash` equal; unknown → `None`; tampered
   → `BundleCorrupt`; idempotent.
3. **Reconstruction equivalence (unit):** DB vs disk equal on hash + models.
4. **Framed engine guard:** content edit / cross-file move / rename / empty-file
   add / non-`domain/` semantic edit each trip it.
5. **Flag off:** provenance columns populated (**incl. both engine-ID columns ==
   `ENGINE_BUILD_ID`**); golden cases unchanged.
6. **Flag on, present:** run scores under its resolved bundle.
7. **Flag on, absent (P1.3):** job dead-letters with **zero** adapter
   calls/results, review tasks, POC tokens, and outbox rows — not merely no
   check/decision.
8. **Headline — run-pin immutability + typed provenance (P2.6).** Seed X; create a
   run under X. Build Y differing in a rubric field; seed Y; process bundle = Y.
   **Flag on:** score under **X**; `checks.policy_bundle_hash` and the DECIDE-audit
   `resolved_policy_bundle_hash` == `X.bundle_hash`; `DecisionRow.policy_shas` ==
   `X.shas`; re-deriving the bundle hash from `policy_shas` == `X.bundle_hash`
   (reconstruction proof); `runs.policy_bundle_hash` == `X.bundle_hash` (unchanged);
   `runs.engine_build_id` == `decisions.engine_build_id` == `ENGINE_BUILD_ID`.
   **Flag off:** `runs.policy_bundle_hash` == X unchanged while check/decision/audit
   provenance == the Y values (drift visible); engine IDs still `ENGINE_BUILD_ID`.
   Cross-decision: `approve` under X but `manual_review_insufficient` under Y →
   decides `approve` (flag on).
9. **Broker-blocked short-circuit (P1.2):** stamps both engine-ID columns +
   resolved bundle atomically while the decision stays `reject`.
10. **Cascade provenance:** identity-invalidation + ORG-ID→POC successors carry the
    resolved hash.
11. **Manual-approve decision:** post-011 writes `DecisionRow.engine_build_id`.
12. **Activation recovery:** a final-attempt `running` job is requeued (not
    dead-lettered) after confirmed-zero-old-workers, then scored under pinning.
13. **Rollback acceptance (P3.7):** with a final-attempt in-flight job:
    confirmed-zero → requeue → flag-**off** restart on the PR6 image → one
    decision, immutable creation pin, resolved *process-bundle* provenance, and
    both engine IDs populated (no post-epoch NULL).
14. **`verify_pinnable_backlog`** flags an absent hash; **`seed_policy_bundle`**
    stores on match, **adds no row on mismatch**.
15. **Epoch:** `activate_bundle_pinning_epoch` compare-and-set is idempotent
    (rerun/concurrent no-op); the per-surface alert flags a post-epoch-decided
    missing stamp but not a legitimately-queued run.
16. **Worker attestation:** captured-log test asserts structured
    `flag`/`bundle_hash`/`engine_build_id` at pipeline-worker startup (both flag
    states).
17. **`/readyz`** 503 when the API process bundle is absent, 200 when present.
18. SSOT/policy drift guard green; import-linter 2 kept / 0 broken.

## 9. Observability & docs

- **ADR-005 — "Per-run policy bundle pinning"** (004 = PR 5b; bump PR 10's
  reserved 005 → **ADR-006**): rubric-only scope; run-pin immutability +
  `resolved_policy_bundle_hash`; bundle+engine automatic provenance; the framed
  guard; forward-only downgrade; the epoch table + per-surface alert; drained
  activation + flag-only rollback.
- **`DEPLOYMENT.md`:** Phase 1 + `activate_bundle_pinning_epoch`; the drained
  activation; the flag-only rollback. **`RUNBOOK.md`:** the flag,
  `verify_pinnable_backlog`, `seed_policy_bundle`, `/readyz`, epoch/alert,
  reprocess-needs-seed.
- **`OVERVIEW.md` — correct over-claims + roadmap attribution:** `:218-219`,
  `:411-413` state the PR 6 boundary; **immutable evidence → PR 8, revalidation →
  PR 6b, broker-state reproducibility → PR 10.** `.agents/ROADMAP.md`: 7A shipped;
  7B pending for M4.
- `AUDIT_FINDINGS.md`: record any build-time deviation.

## 10. Constraints

- `KYC_Tool_Build_Package/` immutable; `HARD_CONFLICT_REASON_CODES` stays engine
  code (in the whole-tree closure). **M2 untouched.** `policy/` stays pure. Parent
  sole committer/pusher/bus-writer; subagent-driven build; DB tests red→green on
  real Postgres before commit.
