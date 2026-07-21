# PR 6 — Per-run policy bundle pinning (ROADMAP item 7A)

**Status:** DESIGN rev 4 — Codex rounds 1–3 folded; awaiting re-audit.
**ROADMAP:** item 7A (PR 6). PR 6b (revalidation, 7B), PR 8 (immutable evidence),
PR 10 (broker snapshots) are separate later units.
**Locked (human-approved):** Approach A (DB-backed bundle store, load-by-hash);
`ENGINE_BUILD_ID` = a deliberate domain constant guarded by a whole-tree drift test.

**Rev 4 changes (Codex `aa17ca6..0dc514d` review):** bundle files are stored
**base64** (JSONB can't hold `bytes`; byte-exact round-trip tested) (P1.1);
`Run.policy_bundle_hash` stays the **immutable creation-time pin** — the resolved
bundle drives checks/`DecisionRow.policy_shas`/a new `resolved_policy_bundle_hash`
DECIDE-audit detail, never the run row (P1.2); a **rollback** procedure mirrors
activation (P1.3); `requeue_interrupted_jobs` runs only after orchestrator-confirmed
**zero old workers** (P1.4); the provenance epoch gets a durable
**`ops.activate_bundle_pinning_epoch`** command and drops the "enforcement-off ⇒
harmless" rationale (manual approvals enforce independently) (P2.5); the engine
guard hashes **framed** per-file records (P2.6); `seed_policy_bundle` is
**compute→compare→store** (P2.7); a **worker-attestation log test** is added
(P3.8); roadmap attribution corrected — PR 8 evidence, PR 6b revalidation, PR 10
broker (P3.9).

---

## 1. Problem

A run records the policy it was created under (`runs.policy_bundle_hash`, written
**once** at ingest — `events/ingest.py:216-220`), but nothing guarantees the run
is *scored*, or its decision *recorded*, under that bundle:

1. **The worker scores and records provenance under its own process bundle.** It
   `load_policy(settings.policy_dir)` once at startup (`workers/pipeline_worker.py:83`),
   folds `self.policy`'s rubric into `score()`/`evaluate_gates()`
   (`pipeline.py:392-393`), and writes `DecisionRow.policy_shas = self.policy.shas`
   plus a decided-audit bundle hash (`pipeline.py:415-450`). If the on-disk policy
   changed, or API and workers run different deploys, both the score and its
   recorded provenance reflect a different bundle than the run's creation pin.
2. **The bundle exists only on disk** — once files change, the exact bytes are
   gone; the hash names but cannot reconstruct them.
3. **Engine code drift is invisible** — rubric points/categories/reason codes are
   baked into checks by validator/engine *code*, and nothing records which engine ran.

PR 6 pins the **rubric** each run is scored under, records **which bundle was
actually used** without disturbing the creation pin, makes the exact bundle
durably reconstructable, and records an `engine_build_id`. Non-goals: re-scoring
(PR 6b), immutable evidence (PR 8), broker-state reproducibility (PR 10) — §2.

## 2. Scope & non-goals

**In scope:** a durable bundle store; the worker resolves each run's creation-pin
bundle by hash and uses it for the rubric-consuming scoring steps and the
automatic decision/check provenance writes; refuse-if-unloadable behind a flag;
`engine_build_id` + a whole-tree drift guard; a two-phase rollout with a drained
atomic activation **and rollback**.

**Non-goals:** revalidation → PR 6b; immutable source evidence → PR 8;
broker-state reproducibility → PR 10 (`BrokerGate` reads live `broker_entities`,
`broker_gate.py:66-79`); pinning decision *rules* (`decide()` hard-codes priority,
`decision.py:48-89` — covered by `engine_build_id`); refuse-on-engine-mismatch;
check-level `engine_build_id`; backfilling pre-epoch rows; boot-required flag;
object-store storage.

## 3. Schema — migration 011

```
policy_bundles(
  bundle_hash  TEXT        PRIMARY KEY,   -- the sha256 loader already computes
  files_json   JSONB       NOT NULL,      -- {filename: base64(raw_bytes)} for all 7 POLICY_FILES
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
)
```
- **Base64 values (P1.1):** the loader reads `read_bytes()` because `bundle_hash`
  names exact bytes; JSONB cannot hold `bytes`, so each file's raw bytes are
  **base64-encoded** into `files_json` and decoded back to the identical bytes
  before `build_bundle` (tested with non-ASCII bytes + per-file SHA/hash equality
  after a round-trip). (BYTEA rows are the alternative; base64-in-JSONB keeps one
  table.)
- New nullable columns: `checks.policy_bundle_hash`, `runs.engine_build_id`,
  `decisions.engine_build_id`. **`runs.policy_bundle_hash` is unchanged and stays
  the immutable creation pin (P1.2).**
- **No `shas_json`** (redundant/unauthenticated); `decisions.policy_shas` remains
  the queryable per-file provenance.
- **Downgrade forward-only after use:** refuses once any `policy_bundles` row or
  provenance column is populated (like 010); empty schema still downgrades. Tested.

## 4. Modules & the purity boundary

`policy/` may not import SQLAlchemy, so the store lives outside it.
- **`policy/loader.py` (pure):** `read_policy_files(dir) -> dict[str,bytes]`;
  `build_bundle(raw: dict[str,bytes], *, policy_dir: Path | None = None)`;
  `load_policy(dir) = build_bundle(read_policy_files(dir), policy_dir=dir)`.
  `PolicyBundle.policy_dir` is deliberately widened `Path` → `Path | None`
  (recorded API change): disk passes the dir, DB passes `None` (used only for
  provenance display, never on the scoring path).
- **New `policy_store/repo.py` (non-pure):** `store_bundle(session, raw) -> str`
  (base64-encode → idempotent `ON CONFLICT DO NOTHING`); `load_bundle(session,
  hash) -> PolicyBundle | None` (base64-decode → `build_bundle(raw,
  policy_dir=None)` → assert recomputed `bundle_hash` == hash, else
  `BundleCorrupt`). `policy_store` imports `policy`; never the reverse.
- **`domain/engine.py` (new, pure):** `ENGINE_BUILD_ID: str`.

## 5. Worker: resolve the creation-pin bundle; keep the run pin immutable

`resolve_bundle(session, run) -> PolicyBundle`: flag off → the process bundle;
flag on → `load_bundle(session, run.policy_bundle_hash)`, `None` →
`BundleUnavailable` (run fails loudly, job dead-letters, no check/decision). Cached
by hash. **`run.policy_bundle_hash` is the resolve *input* and is never
rewritten** (P1.2).

**Exactly what the resolved bundle drives (P3.8):** the **check-type allow-list**
→ `validators/build.py::build_intents` (`:17-60`); each check's **points +
category** → `checkstore.apply_check_intents` (`checkstore/repo.py:250-279`); the
**score threshold** and **allowed broker statuses** → the pipeline's
`score()`/`evaluate_gates()` call (`pipeline.py:392-393`). Validator pass/fail
logic and reason codes are **code** (covered by `engine_build_id`), not rubric data.

**Provenance writes use the resolved bundle but NOT the run pin (P1.2).** The
resolved bundle's identity is recorded on: `checks.policy_bundle_hash`,
`DecisionRow.policy_shas`, and a **new `resolved_policy_bundle_hash` field in the
DECIDE audit-entry detail** (`pipeline.py:415-450`) — never on the immutable
`runs.policy_bundle_hash`. So flag-on records the resolved hash == the creation
pin; flag-off records creation-pin=X on the run but resolved=Y on the
check/decision/audit, keeping the drift **visible** rather than overwriting it.

**Every run-created check carries the resolved hash** incl. identity-invalidation
/ ORG-ID→POC successors via `write_check`/`supersede_without_replacement`
(`checkstore/repo.py:127-308`).

**Manual-approve decisions:** the API path (`events/ingest.py:241-266`) stamps
`ENGINE_BUILD_ID` on its `DecisionRow(run_id=None)`.

**Seeding:** API app factory + every worker `store_bundle(session,
read_policy_files(settings.policy_dir))` at startup before serving/claiming.

## 6. `engine_build_id` + framed whole-tree guard (P2.6)

`domain/engine.py::ENGINE_BUILD_ID`, bumped on decision-semantic changes. The
guard test (`tests/policy_driven/test_engine_build_id_guard.py`) sha256s **framed
records** — for each `src/kyc_tool/**/*.py` file (excluding `__pycache__`), in
sorted relative-path order, `relative_path + NUL + str(byte_length) + NUL + bytes`
— asserted equal to a pinned `EXPECTED_ENGINE_SOURCE_HASH`. Framing binds path +
boundary so a rename, a move of code between files, or a new empty `__init__.py`
all change the digest (not just content edits). Mechanically complete — no curated
list can omit `broker_gate.py`/`review_guard.py`/`triggers.py`/`loader.py`. On
mismatch: *"engine source changed — bump ENGINE_BUILD_ID if semantics changed;
re-pin the hash in the same commit."* Tests cover a content edit, a cross-file
move, a rename, and an empty-file add (all trip it), plus a non-`domain/` semantic
edit (`broker_gate.py` → CLEAR).

## 7. Rollout — epoch, drained activation, and rollback

`Settings.enforce_bundle_pinning: bool = False`.

- **Phase 1 — deploy 011 + the provenance epoch (P1.4/P2.5).** Migration, startup
  seeding, provenance writes, and (still process-bundle) scoring ship rolling.
  Coverage is **not** claimed universal: an old replica during the rollover can
  write a run/check/decision with NULL provenance — including an old API applying
  an **inline manual approval** with no `engine_build_id`, which is enforced
  independently of the M2 flag, so "enforcement is off" is *not* a safety
  argument. The epoch boundary is made trustworthy by a durable idempotent
  **`ops.activate_bundle_pinning_epoch`** one-shot (parallel to
  `ops.activate_hmac_v1_observation`): run **only after every old API and worker
  is confirmed gone**, it records the active `bundle_hash` + `ENGINE_BUILD_ID` +
  timestamp in a small `bundle_pinning_epoch` row. An audit query/alert then
  treats any provenance-NULL row **after** the recorded epoch as a defect (vs an
  accepted pre-epoch NULL). Idempotence + the post-epoch-NULL alert are tested.
- **Phase 2 — drained atomic worker-pool activation (P1.3/P1.4).**
  1. `ops.verify_pinnable_backlog` — every runnable/requeueable run's non-NULL
     hash loads; abort + seed on any gap.
  2. **Disable autoscaling/restarts and confirm at the orchestrator that zero old
     workers remain** (a row count cannot prove process quiescence, and lease
     fencing is PR 7a — an unfenced old handler could still commit,
     `queue/worker.py:42-75`).
  3. **`ops.requeue_interrupted_jobs`** (its shipped precondition is exactly "all
     workers confirmed stopped") recovers stop-interrupted jobs and asserts zero
     `running` rows.
  4. Enable the flag; start only flag-on workers, each emitting a startup
     **attestation** log (`flag/bundle_hash/engine_build_id`); deployment verifies
     every replica attests. API stays up throughout.
  5. Resume.
- **Rollback — mirrors activation (P1.3).** The repo's generic rollback (redeploy
  prior image + downgrade migration, `DEPLOYMENT.md:118-130`) would overlap
  flag-on and prior-image workers (the same race) and cannot downgrade a used 011.
  So: stop + orchestrator-confirm all workers exited → `requeue_interrupted_jobs`
  → disable pinning / deploy the compatible prior code → start only flag-off/prior
  workers; **retain migration 011** (do not downgrade). Documented + tested.
- **`ops.seed_policy_bundle --policy-dir <dir> --expect-hash <hash>` (P2.7):**
  read → `build_bundle` → **compare to `--expect-hash` → only then open the
  storing txn** (never insert on mismatch, since a wrong row makes 011
  forward-only). The only supported historical-bundle recovery; no ad-hoc DB edits.
- **Fail-closed:** flag-on + missing bundle → refuse, never the process bundle.
- **Readiness:** after 011, `/readyz` verifies the API process bundle is loadable
  **unconditionally** (confirms seeding on every API replica); the worker witness
  is the startup attestation.

## 8. Testing (real Postgres unless noted)

1. **011** up/down: empty-schema downgrade succeeds; downgrade **refuses** once a
   bundle row / provenance column is populated.
2. **`policy_store` round-trip incl. non-ASCII bytes (P1.1):** base64 store→load
   reproduces byte-identical files, per-file SHA + `bundle_hash` equal; unknown →
   `None`; tampered `files_json` → `BundleCorrupt`; idempotent.
3. **Reconstruction equivalence (unit):** DB vs disk equal on hash + models (not
   `policy_dir`).
4. **Framed engine guard (P2.6):** pins the framed whole-tree hash; a content
   edit, a cross-file move, a rename, an empty-file add, and a non-`domain/`
   semantic edit (`broker_gate.py`) each trip it.
5. **Flag off:** provenance columns populated; golden cases unchanged.
6. **Flag on, present:** run scores under its resolved bundle.
7. **Flag on, absent:** job dead-letters; no check/decision written.
8. **Headline — run-pin immutability + resolved provenance (P1.2, P2.4).** Seed X;
   create a run under X. Build Y differing in a **rubric** field, seed Y, point
   the worker's process bundle at Y. **Flag on:** score under **X**; `checks
   .policy_bundle_hash` / `DecisionRow.policy_shas` / the DECIDE-audit
   `resolved_policy_bundle_hash` all == **X**; `runs.policy_bundle_hash` still ==
   **X**. **Flag off:** `runs.policy_bundle_hash` == **X** (unchanged) while
   check/decision/audit provenance == **Y** (drift visible). A case `approve`
   under X but `manual_review_insufficient` under Y decides `approve` (flag on).
9. **Cascade provenance:** identity-invalidation + ORG-ID→POC successors carry the
   resolved hash (flag off/on).
10. **Manual-approve decision:** post-011 writes `DecisionRow.engine_build_id ==
    ENGINE_BUILD_ID`.
11. **Activation recovery (P1.4):** a final-attempt `running` job at the boundary
    is requeued (not dead-lettered) and later scored under pinning; the runbook/
    test encodes the zero-old-workers precondition before `requeue_interrupted_jobs`.
12. **`verify_pinnable_backlog`** flags an absent hash; **`seed_policy_bundle`**
    stores on match and **on mismatch adds no row** (P2.7).
13. **Epoch (P2.5):** `activate_bundle_pinning_epoch` is idempotent; a
    provenance-NULL row dated after the epoch is flagged by the audit query.
14. **Worker attestation (P3.8):** a captured-log test asserts the pipeline-worker
    startup emits structured `flag`, `bundle_hash`, `engine_build_id` (both flag
    states).
15. **`/readyz`** 503 when the API process bundle is absent, 200 when present.
16. SSOT/policy drift guard green; import-linter 2 kept / 0 broken.

## 9. Observability & docs

- **ADR-005 — "Per-run policy bundle pinning"** (004 = PR 5b; bump PR 10's
  reserved 005 → **ADR-006**): the rubric-only scope, run-pin immutability +
  `resolved_policy_bundle_hash`, the framed whole-tree guard, the forward-only
  downgrade, the epoch, and the drained activation + rollback.
- **`DEPLOYMENT.md`:** Phase 1 + `activate_bundle_pinning_epoch`; the Phase 2
  drained activation (preflight → confirm zero old workers → `requeue_interrupted_jobs`
  → flag-on workers + attestation → resume); the mirrored rollback.
  **`RUNBOOK.md`:** the flag, `verify_pinnable_backlog`, `seed_policy_bundle`,
  `/readyz`, epoch/alert, reprocess-needs-seed.
- **`OVERVIEW.md` — correct existing over-claims (P3.9).** `:218-219` and
  `:411-413` must state the PR 6 boundary and the *correct* roadmap attribution:
  the exact stored bundle + engine provenance are pinned now; **immutable evidence
  arrives with PR 8, revalidation with PR 6b, broker-state reproducibility with
  PR 10.** `.agents/ROADMAP.md`: item 7A shipped; 7B pending for M4.
- `AUDIT_FINDINGS.md`: record any build-time deviation.

## 10. Constraints

- `KYC_Tool_Build_Package/` immutable (JSONs read + stored verbatim);
  `HARD_CONFLICT_REASON_CODES` stays engine code (in the whole-tree closure).
- **M2 untouched.** `policy/` stays pure. Parent sole committer/pusher/bus-writer;
  subagent-driven build; DB tests red→green on real Postgres before commit.
