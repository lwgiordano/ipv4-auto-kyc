# PR 6 — Per-run policy bundle pinning (ROADMAP item 7A)

**Status:** DESIGN rev 3 — Codex rounds 1–2 folded; awaiting re-audit.
**ROADMAP:** item 7A (PR 6). PR 6b (revalidation, 7B) and PR 10 (broker snapshots)
are separate later units.
**Locked (human-approved):** Approach A (DB-backed bundle store, load-by-hash);
`ENGINE_BUILD_ID` = a deliberate domain constant guarded by a whole-tree drift test.

**Rev 3 changes (Codex `18b9f8b..aa17ca6` review):** all automatic **decision
provenance writes** (`DecisionRow.policy_shas`, the decided `policy_bundle_hash`,
DECIDE audit shas) use the resolved bundle, not the process bundle, and the
headline test asserts them (P1.1); the engine guard hashes the **whole
`src/kyc_tool/**/*.py`**, not a curated list (P1.2); activation runs
`ops.requeue_interrupted_jobs` after a hard stop so no in-flight job dead-letters
unscored (P1.3); Phase 1 defines an honest **provenance epoch** rather than
claiming universal coverage under a rolling deploy (P1.4); a new
`ops.seed_policy_bundle` one-shot makes absent historical bundles recoverable
(P2.5); `/readyz`'s bundle check is unconditional after 011 and workers emit a
startup attestation (P2.6); `PolicyBundle.policy_dir` is deliberately widened to
`Path | None` (P2.7); §5 pins the **exact** consumed rubric fields (P3.8); the doc
sweep corrects `OVERVIEW.md`'s existing reconstruction over-claims (P3.9).
**Rev 2 (closed in round 2):** forward-only-after-use downgrade; `shas_json`
removed; manual-decision `engine_build_id`; cascade-check provenance; rubric-scope
narrowing; RELEASE process.

---

## 1. Problem

A run records the policy it was created under (`runs.policy_bundle_hash`, set at
ingest — `events/ingest.py:219`), but nothing guarantees the run is *scored* or
its decision *recorded* under that bundle:

1. **The worker scores and records provenance under its own process bundle, not
   the run's.** Each worker `load_policy(settings.policy_dir)` once at startup
   (`workers/pipeline_worker.py:83`); the `Pipeline` folds `self.policy`'s rubric
   into `score()`/`evaluate_gates()` (`pipeline.py:392-393`) and writes
   `DecisionRow.policy_shas = self.policy.shas` / the decided
   `policy_bundle_hash = self.policy.bundle_hash` (`pipeline.py:415-450`). If the
   on-disk policy changed between run-creation and execution, or API and workers
   run different deploys mid-rollout, both the **score** and its **recorded
   provenance** silently reflect a different bundle than the run recorded.

2. **The bundle exists only on disk** — once the files change, the exact bytes a
   past run used are gone; the `bundle_hash` names but cannot reconstruct them.

3. **Engine code drift is invisible** — rubric points/categories/reason codes are
   baked into checks by validator/engine *code*, and today nothing records which
   engine ran.

PR 6 pins the **rubric** each run is scored under, makes **every automatic
decision-provenance write** use that same bundle, makes the exact bundle durably
reconstructable, and records an `engine_build_id`. Non-goals: re-scoring existing
runs (PR 6b), broker-state reproducibility (PR 10) — see §2.

## 2. Scope & non-goals

**In scope:** a durable bundle store; the worker resolves each run's bundle by
hash and uses it for the rubric-consuming scoring steps **and all automatic
decision-provenance writes**; refuse-if-unloadable behind a flag;
`engine_build_id` provenance + a whole-tree drift guard; a two-phase rollout with
an atomic, drained activation cutover.

**Non-goals (explicit):**
- **Revalidation / re-scoring existing runs or checks → PR 6b.**
- **Broker-state reproducibility → PR 10.** `BrokerGate` reads the live
  `broker_entities` table (`orchestration/broker_gate.py:66-79`), not the bundle;
  PR 6 stores `broker_policy.json` for provenance but does not rewire the gate.
- **Pinning the decision *rules*.** `decide()` takes no policy and hard-codes the
  priority (`domain/decision.py:48-89`); covered by `engine_build_id`.
- **Refuse-on-engine-mismatch;** check-level `engine_build_id`; backfilling
  pre-epoch rows; making the flag boot-required; object-store storage — all deferred.

## 3. Schema — migration 011

```
policy_bundles(
  bundle_hash  TEXT        PRIMARY KEY,   -- the sha256 loader already computes
  files_json   JSONB       NOT NULL,      -- {filename: raw_file_text} for all 7 POLICY_FILES
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
```
New nullable columns (additive): `checks.policy_bundle_hash`,
`runs.engine_build_id`, `decisions.engine_build_id`. (`runs.policy_bundle_hash`,
`decisions.policy_shas` already exist.)

**No `shas_json`** — redundant with `files_json`+`bundle_hash` and would be an
un-authenticated trust surface; per-file shas derive via `build_bundle`.
`decisions.policy_shas` remains the queryable per-file provenance.

**Storage fidelity** — `files_json` is raw text, so reconstruction reproduces the
identical `bundle_hash`, re-verified on load (§4).

**Downgrade is forward-only after use** — `files_json` is the only retained copy
of historical policy bytes and the provenance columns sit on immutable rows, so
011's downgrade **refuses** (like 010) once any `policy_bundles` row exists or any
provenance column is populated; a never-used (empty) schema still downgrades. The
refusal is tested. No index beyond the PK.

## 4. Modules & the purity boundary

`policy/` may not import SQLAlchemy (import-linter), so the store lives outside it.

- **`policy/loader.py` (pure)** — split into `read_policy_files(policy_dir) ->
  dict[str,bytes]` and `build_bundle(raw, *, policy_dir: Path | None = None) ->
  PolicyBundle`; `load_policy(dir) = build_bundle(read_policy_files(dir),
  policy_dir=dir)` (unchanged behavior, all call sites unchanged). **The frozen
  `PolicyBundle.policy_dir` field is deliberately widened `Path` → `Path | None`**
  (a small, recorded API change, not "unchanged"): disk load passes the real dir,
  DB reconstruction passes `None`. `policy_dir` is used only for provenance
  display, never on the scoring path (the plan verifies no scoring consumer
  dereferences it).
- **New `policy_store/repo.py` (non-pure, parallels `checkstore/`)** —
  `store_bundle(session, raw) -> str` (idempotent `ON CONFLICT (bundle_hash) DO
  NOTHING`); `load_bundle(session, bundle_hash) -> PolicyBundle | None`
  (reconstruct via `build_bundle(raw, policy_dir=None)`, assert recomputed
  `bundle_hash` matches → `BundleCorrupt` on mismatch). `policy_store` imports
  `policy`; never the reverse. Import-linter stays 2 kept / 0 broken.
- **`domain/engine.py` (new, pure)** — `ENGINE_BUILD_ID: str`.

## 5. Worker: resolve the run's bundle; use it for scoring AND provenance

`resolve_bundle(session, run) -> PolicyBundle`: flag off → the process bundle
(unchanged); flag on → `policy_store.load_bundle(session,
run.policy_bundle_hash)`, `None` → raise `BundleUnavailable` (run fails loudly,
job dead-letters, no check/decision written). Cached in-process by hash.

**Exactly what the resolved bundle drives (P3.8 — precise).** The bundle's
consumed fields, threaded to their current readers:
- the **check-type allow-list** → `validators/build.py::build_intents`
  (`build.py:17-60`);
- each check's **points + category** → `checkstore.apply_check_intents`
  (`checkstore/repo.py:250-279`);
- the **score threshold** and **allowed broker statuses** → the pipeline's
  `score()`/`evaluate_gates()` call (`pipeline.py:392-393`).
Validator **pass/fail logic and reason codes are code**, not rubric data — covered
by `engine_build_id`, not by bundle consumption. (So the reconstruction claim is:
rubric fields above + engine code, not "the rubric drives validation".)

**All automatic decision-provenance writes use the resolved bundle (P1.1).**
`DecisionRow.policy_shas`, the decided `run.policy_bundle_hash` write, and any
DECIDE audit entry embedding shas (`pipeline.py:415-450`) take the **resolved**
bundle's `shas`/`bundle_hash` — the same object as scoring — never `self.policy`.
Otherwise flag-on would score under X but record Y, recreating §1 in the record.

**Every run-created check carries the resolved hash (incl. cascades)** —
`apply_check_intents` and the identity-invalidation / ORG-ID→POC successor writes
via `write_check`/`supersede_without_replacement` (`checkstore/repo.py:127-308`).

**Manual-approve decisions** — `reviewer.manual_approve` writes
`DecisionRow(run_id=None)` in the API (`events/ingest.py:241-266`) and the engine
computes buy-enablement there, so that path stamps `ENGINE_BUILD_ID` on its row.

**Seeding** — the API app factory and every worker call `store_bundle(session,
read_policy_files(settings.policy_dir))` at startup, before serving/claiming.

## 6. `engine_build_id` + whole-tree guard (P1.2)

`domain/engine.py::ENGINE_BUILD_ID` is bumped in a reviewed commit when decision
semantics change. The **guard test** (`tests/policy_driven/test_engine_build_id_guard.py`)
hashes the sha256 over the concatenated sorted source of **the entire
`src/kyc_tool/**/*.py` tree** (excluding `__pycache__`; tests are not shipped
engine), asserted equal to a pinned `EXPECTED_ENGINE_SOURCE_HASH`. This is
mechanically complete — no hand-curated list can omit a decision-shaping module
(`broker_gate.py`, `review_guard.py`, `triggers.py`, `loader.py`, … are all in
the tree). On mismatch: *"engine source changed — if decision semantics changed,
bump ENGINE_BUILD_ID; re-pin EXPECTED_ENGINE_SOURCE_HASH in the same commit."*
Every source change is thus a conscious *(bump id + re-pin)* or *(re-pin only,
cosmetic)* decision — the accepted trade-off is that unrelated edits re-pin the
hash without bumping the id. A test proves a semantic edit **outside `domain/`**
(e.g. `broker_gate.py` returning CLEAR) trips the guard.

## 7. Rollout — two phases, drained atomic activation

`Settings.enforce_bundle_pinning: bool = False` (mirrors `enforce_positive_decisions`).

- **Phase 1 — deploy 011 (provenance epoch, P1.4).** The migration, startup
  seeding, provenance writes, and the (still process-bundle) scoring ship as a
  normal deploy. Because a rolling overlap lets an old replica complete a run or
  an inline manual approval **without** the new columns, provenance is **not**
  claimed universal: it is guaranteed only for runs/decisions produced entirely by
  post-011 replicas — the *provenance epoch*. Overlap-era NULLs are acceptable
  (enforcement is off and they predate the epoch) and are **not** backfilled.
  Operators who want zero NULLs may worker-cutover Phase 1 too and pause
  manual-approve during the API rollout; not required. The epoch boundary is
  recorded (deploy timestamp) so audit can distinguish "pre-epoch NULL" from a bug.
- **Phase 2 — drained atomic worker-pool activation (P1.3).** Because `Settings`
  is per-process, activation is a restart; a rolling one would let old/new workers
  score the same run differently. Contract:
  1. **Backlog preflight** — `ops.verify_pinnable_backlog` asserts every
     runnable/requeueable run's non-NULL hash loads; abort + seed on any gap.
  2. **Hard-stop the whole pipeline-worker pool** (API stays up, queuing).
  3. **`ops.requeue_interrupted_jobs`** (the PR 5b one-shot) recovers any job the
     stop left `running` (attempts−1, so the forced stop isn't charged) and
     **asserts zero `running` rows** — otherwise the reaper would dead-letter an
     interrupted final-attempt job unscored (`queue/jobs.py:128-150`; workers have
     no SIGTERM drain, `worker.py:84-88`).
  4. Enable the flag; **start only flag-on workers**, each emitting a startup
     **attestation** log line (`flag=on bundle_hash=… engine_build_id=…`);
     deployment verifies every replica attests before resuming.
  5. Resume.
- **Seeding an absent historical bundle (P2.5)** — new idempotent one-shot
  `ops.seed_policy_bundle --policy-dir <historical-release-dir> --expect-hash
  <run-hash>`: reads that dir, `store_bundle`s it, and **refuses** if the computed
  hash ≠ `--expect-hash` (never stores a wrong/corrupt bundle). This is the only
  supported way to recover a pre-epoch run for pinned reprocessing (no ad-hoc DB
  edits).
- **Fail-closed** — flag-on + missing bundle → refuse, never the process bundle.
- **Readiness/attestation (P2.6)** — after 011, `/readyz` verifies the API's
  process bundle is loadable from `policy_bundles` **unconditionally** (not gated
  on any flag — it confirms seeding on every API replica); the **worker**
  verification surface is the startup attestation log above. This resolves the
  API-holds-`/readyz` / workers-hold-the-flag split.

## 8. Testing (real Postgres unless noted)

1. **Migration 011** up/down: downgrade on empty schema succeeds; downgrade
   **refuses** once a bundle row or any provenance column is populated.
2. **`policy_store` round-trip:** reconstruct verifies `bundle_hash`; unknown →
   `None`; tampered `files_json` → `BundleCorrupt`; `store_bundle` idempotent.
3. **Reconstruction equivalence (unit):** DB vs disk equal on `bundle_hash` +
   parsed models (not `policy_dir`: `None` vs a `Path`) (P2.7).
4. **Engine guard (P1.2):** pins the whole-tree hash; a semantic edit **outside
   `domain/`** (`broker_gate.py`) trips it.
5. **Flag off:** provenance columns populated; golden cases unchanged.
6. **Flag on, present:** run scores under its resolved bundle.
7. **Flag on, absent:** job dead-letters, no check/decision written.
8. **Headline — cross-bundle *rubric + provenance* pinning (P1.1, P2.4).** Seed X;
   create a run under X. Build Y differing in a **rubric** field (higher
   `score_met` threshold or a check's points), seed Y, point the worker's process
   bundle at Y, flag on → the run is scored under **X** *and* its
   `DecisionRow.policy_shas` / decided `policy_bundle_hash` / DECIDE-audit shas all
   record **X**, not Y (a case `approve` under X but `manual_review_insufficient`
   under Y decides `approve`). Flag-off counterpart records Y throughout.
9. **Cascade provenance (P3.8):** identity-invalidation + ORG-ID→POC successor
   checks carry the resolved hash (flag off/on).
10. **Manual-approve decision (P2.5):** post-011 `reviewer.manual_approve` writes
    `DecisionRow.engine_build_id == ENGINE_BUILD_ID`.
11. **Activation recovery (P1.3):** a job left `running` on its final attempt at
    the stop is requeued (not dead-lettered) by `ops.requeue_interrupted_jobs`,
    then scored under pinning; zero `running` rows asserted at the boundary.
12. **`ops.verify_pinnable_backlog`** flags a run whose hash is absent; passes when
    all seeded. **`ops.seed_policy_bundle`** stores on a matching `--expect-hash`
    and **refuses** on mismatch (P2.5).
13. **`/readyz`** 503 when the API process bundle is absent from `policy_bundles`
    (unconditional after 011), 200 when present (P2.6).
14. SSOT/policy drift guard green; import-linter 2 kept / 0 broken.

## 9. Observability & docs

- **ADR-005 — "Per-run policy bundle pinning"** (next sequential; 004 = PR 5b;
  bump PR 10's reserved 005 → **ADR-006**). Records the rubric-only scope, the
  whole-tree engine guard, the forward-only-after-use downgrade, the provenance
  epoch, and the drained activation.
- **`DEPLOYMENT.md`** — Phase 1 (epoch) + the Phase 2 drained atomic worker-pool
  activation (preflight → hard-stop → `requeue_interrupted_jobs` → flag-on workers
  + attestation → resume). **`RUNBOOK.md`** — the flag, `verify_pinnable_backlog`,
  `seed_policy_bundle`, `/readyz`, reprocess-needs-seed.
- **`OVERVIEW.md` — correct the existing over-claims (P3.9).** `:218-219` ("every
  decision fully reconstructs end-to-end") and `:411-413` ("policy files
  drive/explain every decision") must state the PR 6 boundary: the exact stored
  bundle + engine provenance are pinned now; full broker-state and evidence
  reproducibility arrive with PR 10 / PR 6b. Add that a policy change is durably
  pinned per run. `.agents/ROADMAP.md`: item 7A shipped; 7B pending for M4.
- `AUDIT_FINDINGS.md`: record any build-time deviation.

## 10. Constraints

- `KYC_Tool_Build_Package/` immutable (JSONs read + stored verbatim);
  `HARD_CONFLICT_REASON_CODES` stays engine code (in the whole-tree closure).
- **M2 untouched.** `policy/` stays pure. Parent sole committer/pusher/bus-writer;
  subagent-driven build; DB tests red→green on real Postgres before commit.
