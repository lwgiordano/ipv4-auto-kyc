# PR 6 — Per-run policy bundle pinning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Parent session is the SOLE committer/pusher/bus-writer (Claude⇄Codex bus discipline); subagents implement + hand diffs back.

**Goal:** Guarantee every run is scored, and its decision recorded, under exactly the policy bundle it was created with — durably reconstructable — and record which engine produced it, all behind a fail-closed `enforce_bundle_pinning` flag.

**Architecture:** A new `policy_bundles` table stores each bundle's raw files (base64) keyed by its `bundle_hash`, seeded at startup with read-back verification. The pipeline worker resolves each run's creation-pin bundle by hash **at job entry** (before any side effect); flag-on it scores under that bundle — re-pricing **every** live check's points/category from the resolved rubric at decision time — and writes bundle+engine provenance atomically without touching the immutable creation pin. A durable `bundle_pinning_epoch` record + drained worker-pool activation make the flag flip trustworthy.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 (sync psycopg), Postgres, Alembic, import-linter, pytest against ephemeral Postgres (`tests/pg.py`).

**Spec:** `.agents/superpowers/specs/2026-07-21-pr6-policy-bundle-pinning-design.md` (Codex AUDIT-CLEAN at rev 7, `41a3b3b`). Every task references its spec sections; read them for rationale.

## Global Constraints

- **No migration hazard beyond design:** migration 011 is additive + hot-compatible; its downgrade is **forward-only after use** (refuses once any `policy_bundles` row, any provenance column, or the epoch row is populated). Never make it destructive.
- **Flag-off is a strict no-op on scoring behavior.** Phase 1 (flag off) only adds provenance writes + seeding; scoring reads the stamped row values exactly as today. Existing golden cases must not change.
- **`policy/` stays pure** — import-linter forbids `domain`/`validators`/`policy` from importing `sqlalchemy`/`db`/`storage`/`httpx`. The DB bundle store lives in the new non-pure `policy_store/`. Keep the run `lint-imports` at **2 kept / 0 broken**.
- **`KYC_Tool_Build_Package/` is immutable** — the 7 JSONs are read and stored verbatim; never edit them. Deviations → `AUDIT_FINDINGS.md`.
- **M2 untouched** — do not touch `KYC_ENFORCE_POSITIVE_DECISIONS` / the enforcement kill switch.
- **`ENGINE_BUILD_ID`** is a nonblank, trimmed, versioned identifier (`eng-<positive int>`, e.g. `"eng-1"`). Never put the model identifier in it or any artifact.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK
  ```
- Tests run against real Postgres. `./manage.sh test` runs the WHOLE suite (ignores path args); for targeted red→green use `.venv/bin/pytest <selector>`. **Never** run `./manage.sh fmt` (reformats the whole tree — the PR 4/5a noise trap); `.venv/bin/ruff check .` is the lint gate.

---

## File Structure

**Create:**
- `alembic/versions/011_policy_bundle_pinning.py` — the migration.
- `src/kyc_tool/policy_store/__init__.py`, `src/kyc_tool/policy_store/repo.py` — the non-pure DB bundle store + epoch helpers + `BundleCorrupt`.
- `src/kyc_tool/domain/engine.py` — `ENGINE_BUILD_ID` (pure).
- `src/kyc_tool/ops/verify_pinnable_backlog.py`, `src/kyc_tool/ops/seed_policy_bundle.py`, `src/kyc_tool/ops/activate_bundle_pinning_epoch.py` — one-shots (mirror `ops/requeue_interrupted_jobs.py`).
- Tests: `tests/policy_driven/test_engine_build_id_guard.py`, `tests/unit/test_policy_store.py`, `tests/integration/test_bundle_pinning.py`, `tests/integration/test_bundle_pinning_ops.py`, `tests/unit/test_process_topology.py`.

**Modify:**
- `src/kyc_tool/config.py` — `enforce_bundle_pinning` flag.
- `src/kyc_tool/policy/loader.py` — `read_policy_files` / `build_bundle(raw, *, policy_dir=None)`; widen `PolicyBundle.policy_dir` to `Path | None`.
- `src/kyc_tool/db/tables.py` — 2 tables + 3 columns.
- `src/kyc_tool/domain/scoring.py` — the pure `rubric_scoring_views` re-pricing helper.
- `src/kyc_tool/orchestration/pipeline.py` — resolve-at-entry, flag-on scoring views, atomic bundle+engine provenance.
- `src/kyc_tool/checkstore/repo.py` — thread the resolved `bundle_hash` to every run-created check write.
- `src/kyc_tool/events/ingest.py` — stamp `ENGINE_BUILD_ID` on the manual-approve `DecisionRow`.
- `src/kyc_tool/api/app.py` — startup seed + `/readyz` unconditional bundle check.
- `src/kyc_tool/workers/pipeline_worker.py`, `src/kyc_tool/workers/dev_worker.py` — startup seed + attestation log.
- Docs: `docs/architecture-decisions.md` (ADR-005), `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `docs/OVERVIEW.md`, `.agents/ROADMAP.md`, `AUDIT_FINDINGS.md`.

**Reuse:** `db/session.{make_engine,make_session_factory,uow}`; the `migrated`/`clean_db`/`settings`/`engine`/`session_factory`/`policy`/`client`/`post_event` fixtures; `checkstore` helpers; `ScoringRubric.item(check_type) -> RubricItem(.points,.category)` (`policy/types.py:32`) and `.check_types` (`:39`); the `ops/requeue_interrupted_jobs.py` one-shot shape.

---

## Task 1: Config flag `enforce_bundle_pinning`

**Files:** Modify `src/kyc_tool/config.py` (near `enforce_positive_decisions`, `:94`); Test `tests/unit/test_production_config.py`.

**Interfaces:** Produces `Settings.enforce_bundle_pinning: bool` (default `False`).

- [ ] **Step 1 — failing test.** In `test_production_config.py`, add:
```python
def test_bundle_pinning_flag_defaults_off_and_toggles():
    from kyc_tool.config import Settings
    assert Settings().enforce_bundle_pinning is False
    assert Settings(enforce_bundle_pinning=True).enforce_bundle_pinning is True
```
Also assert a production-hardened settings object (mirror the existing `hardened()` helper in this file) still validates with the flag **off** — production is NOT required to enable it in PR 6.
- [ ] **Step 2 — run red:** `.venv/bin/pytest tests/unit/test_production_config.py::test_bundle_pinning_flag_defaults_off_and_toggles -v` → FAIL (unknown field).
- [ ] **Step 3 — implement.** Add to `Settings` beside `enforce_positive_decisions`:
```python
    # PR 6 (item 7A): when True, the pipeline worker loads each run's recorded
    # policy bundle by hash and scores under it, refusing if unloadable. Off by
    # default; activated via a drained worker-pool cutover (docs/DEPLOYMENT.md).
    enforce_bundle_pinning: bool = False
```
- [ ] **Step 4 — run green.** Same selector → PASS.
- [ ] **Step 5 — commit** (`feat(pr6): enforce_bundle_pinning settings flag (off by default)`).

---

## Task 2: `domain/engine.py` + the framed whole-tree `ENGINE_BUILD_ID` guard

Spec §6. `ENGINE_BUILD_ID` is a nonblank versioned constant; the guard hashes **framed records** over the whole `src/kyc_tool/**/*.py` tree so any source change forces a conscious bump-or-repin.

**Files:** Create `src/kyc_tool/domain/engine.py`, `tests/policy_driven/test_engine_build_id_guard.py`.

**Interfaces:** Produces `domain.engine.ENGINE_BUILD_ID: str`.

- [ ] **Step 1 — failing test** (`test_engine_build_id_guard.py`):
```python
import hashlib
from pathlib import Path

from kyc_tool.domain.engine import ENGINE_BUILD_ID

SRC = Path(__file__).resolve().parents[2] / "src" / "kyc_tool"
# Bump ENGINE_BUILD_ID (domain/engine.py) AND re-pin EXPECTED below in the SAME
# commit whenever any src/kyc_tool source changes: a semantic change needs a new
# id; a cosmetic change re-pins the hash only. Framing binds path + length so a
# rename / cross-file move / empty-file add all trip this.
EXPECTED_ENGINE_SOURCE_HASH = "<PIN AFTER FIRST GREEN RUN>"

def _engine_source_hash() -> str:
    h = hashlib.sha256()
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC).as_posix().encode()
        data = path.read_bytes()
        h.update(rel + b"\x00" + str(len(data)).encode() + b"\x00" + data)
    return h.hexdigest()

def test_engine_build_id_is_nonblank_versioned():
    assert ENGINE_BUILD_ID.strip() == ENGINE_BUILD_ID and ENGINE_BUILD_ID
    assert ENGINE_BUILD_ID.startswith("eng-")

def test_engine_source_hash_pinned():
    assert _engine_source_hash() == EXPECTED_ENGINE_SOURCE_HASH, (
        "engine source changed — if decision semantics changed, bump "
        "ENGINE_BUILD_ID; re-pin EXPECTED_ENGINE_SOURCE_HASH in the same commit."
    )
```
- [ ] **Step 2 — run red** → FAIL (import + placeholder).
- [ ] **Step 3 — implement** `domain/engine.py`:
```python
"""Engine identity (PR 6, item 7A). Bump ENGINE_BUILD_ID in a reviewed commit
whenever scoring/gate/validator/decision SEMANTICS change — the discipline each
policy JSON's `version` has. A whole-tree guard test (tests/policy_driven/
test_engine_build_id_guard.py) forces a conscious bump-or-repin on ANY source
change. Nonblank, trimmed, versioned: eng-<positive int>."""

ENGINE_BUILD_ID = "eng-1"
```
- [ ] **Step 4 — pin the hash.** Run `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v`; `test_engine_source_hash_pinned` fails with the actual digest — copy it into `EXPECTED_ENGINE_SOURCE_HASH`. Re-run → both PASS. (Add a one-line note in the test's top comment: any later task that edits a `src/kyc_tool/*.py` file must re-pin this in the same commit — implementers will hit it.)
- [ ] **Step 5 — coverage proof.** Add `test_non_domain_semantic_edit_would_trip(tmp_path)` that copies the SRC tree, flips a byte in `orchestration/broker_gate.py` inside the copy, recomputes the framed hash against the copy, and asserts it differs from `EXPECTED_ENGINE_SOURCE_HASH` (proves a non-`domain/` change trips the guard). Run → PASS.
- [ ] **Step 6 — commit** (`feat(pr6): ENGINE_BUILD_ID + framed whole-tree drift guard`).

> **Note for all later tasks that touch `src/kyc_tool/*.py`:** they will fail `test_engine_source_hash_pinned` — re-pin `EXPECTED_ENGINE_SOURCE_HASH` (and bump `ENGINE_BUILD_ID` only if scoring/decision semantics changed) in the same commit. Call this out in each such task's dispatch.

---

## Task 3: Loader refactor — pure `read_policy_files` + `build_bundle`; widen `policy_dir`

Spec §4. Split the parse path so disk-load and DB-reconstruction share one function; widen the frozen bundle's `policy_dir` to `Path | None`.

**Files:** Modify `src/kyc_tool/policy/loader.py`; Test `tests/unit/test_policy_loader.py` (create if absent).

**Interfaces:** Produces `read_policy_files(policy_dir: Path) -> dict[str, bytes]`; `build_bundle(raw: dict[str, bytes], *, policy_dir: Path | None = None) -> PolicyBundle`; `PolicyBundle.policy_dir: Path | None`. `load_policy(policy_dir)` unchanged signature/behavior.

- [ ] **Step 1 — failing test:**
```python
from pathlib import Path
from kyc_tool.config import get_settings
from kyc_tool.policy.loader import load_policy, read_policy_files, build_bundle

POLICY_DIR = get_settings().policy_dir

def test_build_bundle_from_raw_equals_disk_load():
    disk = load_policy(POLICY_DIR)
    raw = read_policy_files(POLICY_DIR)
    rebuilt = build_bundle(raw, policy_dir=None)          # DB origin
    assert rebuilt.bundle_hash == disk.bundle_hash
    assert rebuilt.shas == disk.shas
    assert rebuilt.rubric == disk.rubric                  # parsed models equal
    assert rebuilt.policy_dir is None and disk.policy_dir == POLICY_DIR
```
- [ ] **Step 2 — run red** → FAIL (`read_policy_files`/`build_bundle` undefined).
- [ ] **Step 3 — implement.** Refactor `loader.py`: extract the file-read loop into `read_policy_files(policy_dir) -> dict[str,bytes]`; extract the sha/hash/parse into `build_bundle(raw, *, policy_dir=None)` (the current body of `load_policy` from line 55 onward, taking `raw` as input and passing `policy_dir` through to the frozen `PolicyBundle(...)`); make `load_policy(policy_dir) = build_bundle(read_policy_files(policy_dir), policy_dir=policy_dir)`. Change the dataclass field to `policy_dir: Path | None`.
- [ ] **Step 4 — run green** → PASS. Also run the existing policy/golden suites (`.venv/bin/pytest tests/policy_driven tests/golden -q`) → unchanged (all call sites still work). Re-pin the engine guard hash (loader.py changed) — no `ENGINE_BUILD_ID` bump (no semantic change).
- [ ] **Step 5 — commit** (`refactor(pr6): loader read_policy_files/build_bundle; policy_dir Path|None`).

---

## Task 4: Migration 011 + ORM (2 tables, 3 columns, forward-only downgrade)

Spec §3. Additive, hot-compatible; downgrade refuses after use.

**Files:** Create `alembic/versions/011_policy_bundle_pinning.py`; Modify `src/kyc_tool/db/tables.py`; Test `tests/integration/test_migrations.py`; add the two new tables to the truncation set in `tests/conftest.py` (mirror how PR 5a added `hmac_signature_stats`).

**Interfaces:** Produces tables `policy_bundles(bundle_hash PK, files_json JSONB, created_at)`, `bundle_pinning_epoch(id PK CHECK=1, activated_at, bundle_hash FK→policy_bundles, engine_build_id CHECK nonblank)`; columns `checks.policy_bundle_hash`, `runs.engine_build_id` + `decisions.engine_build_id` (each `CHECK (… IS NULL OR btrim(…) <> '')`). ORM: `PolicyBundleRow`, `BundlePinningEpochRow`, and the new mapped columns on `Check`/`Run`/`DecisionRow`.

- [ ] **Step 1 — failing migration test** (extend `test_migrations.py`, mirror the `migrated` conftest pattern — `AlembicConfig(REPO_ROOT/"alembic.ini")` + set `script_location`/`sqlalchemy.url`):
```python
def test_011_downgrade_clean_on_empty_schema(alembic_config, clean_engine):
    # upgrade head → downgrade 010 → upgrade head, all clean, when unused
    ...

def test_011_downgrade_refuses_after_use(alembic_config, clean_engine):
    # upgrade head; insert one policy_bundles row; downgrade 011 must RAISE
    ...
```
(Follow the exact style of PR 5a's `test_010_downgrade_refuses_after_cross_case_reuse`.)
- [ ] **Step 2 — run red** → FAIL (migration 011 absent).
- [ ] **Step 3 — implement the migration.** `upgrade()`: `create_table` `policy_bundles` + `bundle_pinning_epoch` (with the FK + CHECKs); `add_column` the three nullable columns with their `CHECK` constraints. `downgrade()`: raise `RuntimeError` if `SELECT count(*) FROM policy_bundles`, or any of the provenance columns are non-null, or `bundle_pinning_epoch` is non-empty — else drop columns + tables. Name constraints explicitly (`ck_...`, `fk_...`, `uq_...`).
- [ ] **Step 4 — implement the ORM** in `db/tables.py`: two new mapped classes + the three columns on `Check`/`Run`/`DecisionRow`. Add `policy_bundles` and `bundle_pinning_epoch` to `tests/conftest.py`'s `_ALL_TABLES` truncation list.
- [ ] **Step 5 — run green** → both migration tests PASS; `.venv/bin/pytest tests/integration/test_migrations.py -q` clean. Re-pin the engine guard hash (`tables.py` changed; no `ENGINE_BUILD_ID` bump).
- [ ] **Step 6 — commit** (`feat(pr6): migration 011 policy_bundles + epoch + provenance columns`).

---

## Task 5: `policy_store` — store/load with read-back verify + epoch helpers

Spec §4, §5, §7. Base64 storage; read-back verification on store; the epoch compare-and-set with validation.

**Files:** Create `src/kyc_tool/policy_store/__init__.py`, `src/kyc_tool/policy_store/repo.py`; Test `tests/unit/test_policy_store.py` (integration-style, real PG via `session_factory`/`engine`/`clean_db`).

**Interfaces:** Produces `class BundleCorrupt(Exception)`; `store_bundle(session, raw: dict[str,bytes]) -> str`; `load_bundle(session, bundle_hash: str) -> PolicyBundle | None`; `activate_epoch(session, *, expect_bundle_hash: str, expect_engine: str) -> None`; `read_epoch(session) -> tuple[str, str, datetime] | None` (bundle_hash, engine_build_id, activated_at). Consumes `policy.loader.build_bundle`, `read_policy_files`.

- [ ] **Step 1 — failing tests:**
```python
import base64, pytest
from kyc_tool.policy.loader import read_policy_files, build_bundle
from kyc_tool.policy_store import repo as store
from kyc_tool.config import get_settings

RAW = None  # set in a fixture: read_policy_files(get_settings().policy_dir)

def test_store_then_load_roundtrip(session_factory, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, RAW); s.commit()
    with session_factory() as s:
        b = store.load_bundle(s, h)
    assert b is not None and b.bundle_hash == h and b.policy_dir is None

def test_load_unknown_returns_none(session_factory, clean_db):
    with session_factory() as s:
        assert store.load_bundle(s, "0"*64) is None

def test_store_read_back_raises_on_tampered_row(session_factory, engine, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, RAW); s.commit()
    # corrupt one file's base64 in place
    with engine.begin() as c:
        c.execute(text("UPDATE policy_bundles SET files_json = jsonb_set("
                       "files_json, '{scoring_rubric.json}', to_jsonb('%%%'::text)) "
                       "WHERE bundle_hash=:h"), {"h": h})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.store_bundle(s, RAW)   # conflict path must read back + verify

def test_store_idempotent(session_factory, clean_db):
    with session_factory() as s:
        store.store_bundle(s, RAW); store.store_bundle(s, RAW); s.commit()
        n = s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one()
    assert n == 1

def test_activate_epoch_idempotent_and_mismatch(session_factory, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, RAW); s.commit()
    with session_factory() as s:
        store.activate_epoch(s, expect_bundle_hash=h, expect_engine="eng-1"); s.commit()
    with session_factory() as s:  # same-value rerun no-ops
        store.activate_epoch(s, expect_bundle_hash=h, expect_engine="eng-1"); s.commit()
    with session_factory() as s, pytest.raises(Exception):  # different value fails
        store.activate_epoch(s, expect_bundle_hash="0"*64, expect_engine="eng-1")
```
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement `repo.py`:**
  - `store_bundle`: `files_json = {name: base64.b64encode(b).decode() for name,b in raw.items()}`; compute `bundle_hash = build_bundle(raw).bundle_hash`; `INSERT … ON CONFLICT (bundle_hash) DO NOTHING`; then **on both paths** `verified = load_bundle(session, bundle_hash)` and raise `BundleCorrupt` if `verified is None or verified.bundle_hash != bundle_hash`; return `bundle_hash`.
  - `load_bundle`: SELECT the row; `None` if absent; `raw = {name: base64.b64decode(v) for name,v in files_json.items()}`; `bundle = build_bundle(raw, policy_dir=None)`; raise `BundleCorrupt` if `bundle.bundle_hash != bundle_hash`; return it.
  - `activate_epoch`: validate `expect_engine == ENGINE_BUILD_ID` and `expect_bundle_hash` loads (`load_bundle` not None); `INSERT INTO bundle_pinning_epoch (id, activated_at, bundle_hash, engine_build_id) VALUES (1, now(), :h, :e) ON CONFLICT (id) DO NOTHING`; **read the row back** and raise if `(bundle_hash, engine_build_id) != (expect_bundle_hash, expect_engine)`.
  - `read_epoch`: SELECT the singleton or `None`.
- [ ] **Step 4 — run green** → all PASS. Confirm `import policy_store` does not break `lint-imports` (it imports `policy`, `db`, not the reverse): `.venv/bin/lint-imports` → 2 kept / 0 broken. Re-pin engine guard (new source files).
- [ ] **Step 5 — commit** (`feat(pr6): policy_store store/load read-back-verify + epoch helpers`).

---

## Task 6: Startup seeding + verification + attestation (API + pipeline/dev workers only)

Spec §5, §7. Seed at startup with read-back verify; attest after verify; keep `outbox`/`retention` policy-independent.

**Files:** Modify `src/kyc_tool/api/app.py`, `src/kyc_tool/workers/pipeline_worker.py`, `src/kyc_tool/workers/dev_worker.py`; Test `tests/unit/test_process_topology.py`, extend `tests/integration/test_bundle_pinning.py`.

**Interfaces:** Produces a shared `policy_store.seed_and_verify(session_factory, bundle) -> None` (or inline) called by API + pipeline + dev startup; a structured `worker_started` attestation with `flag`, `bundle_hash`, `engine_build_id`.

- [ ] **Step 1 — failing tests:**
```python
def test_outbox_and_retention_do_not_import_policy_store():
    import importlib, sys
    for mod in ("kyc_tool.workers.outbox_worker", "kyc_tool.workers.retention"):
        importlib.import_module(mod)
    # neither pulls policy_store into sys.modules via its own import graph
    # (assert by inspecting the module's referenced names / a construct call)
    ...

def test_pipeline_worker_startup_attests(caplog, ...):
    # build the pipeline worker; assert the startup log carries
    # flag / bundle_hash / engine_build_id (structlog capture)
    ...
```
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement.** In the API app factory (`app.py`) and `pipeline_worker.py` + `dev_worker.py` startup (after `load_policy`), call `store_bundle(session, read_policy_files(settings.policy_dir))` inside a `uow` — its read-back verify makes a corrupt row fail startup. Emit the attestation (extend the `worker_started` log at `queue/worker.py:85`, or log in the pipeline worker's `main()` after verify) with `flag=settings.enforce_bundle_pinning`, `bundle_hash`, `engine_build_id=ENGINE_BUILD_ID`. Do **not** add any policy import to `outbox_worker.py`/`retention.py`.
- [ ] **Step 4 — run green** → PASS. Re-pin engine guard.
- [ ] **Step 5 — commit** (`feat(pr6): startup seed+verify+attestation (api/pipeline only)`).

---

## Task 7: Resolve the run's bundle at job entry; refuse before any side effect

Spec §5 (P1.3). The pipeline handler resolves the bundle FIRST; flag-on + absent → dead-letter with zero side effects.

**Files:** Modify `src/kyc_tool/orchestration/pipeline.py` (the run-transition handler entry + `Pipeline.__init__`); Test extend `tests/integration/test_bundle_pinning.py`.

**Interfaces:** Produces `Pipeline.resolve_bundle(session, run) -> PolicyBundle` (flag-off → `self.policy`; flag-on → `policy_store.load_bundle`, `None` → raise `BundleUnavailable`); an in-process `{bundle_hash: PolicyBundle}` cache. `BundleUnavailable(Exception)`.

- [ ] **Step 1 — failing test** (headline refuse-before-side-effects):
```python
def test_flag_on_absent_bundle_dead_letters_with_zero_side_effects(...):
    # create a run whose runs.policy_bundle_hash is a NOT-seeded hash (e.g. a
    # poc.submitted run so side effects WOULD mint a token+email), flag on;
    # drive the worker → job dead-letters; assert:
    #   zero rows in adapter_results, review_tasks, poc_tokens, outbox for the case
    #   no checks, no decision
    ...
```
- [ ] **Step 2 — run red** → FAIL (scores/side-effects happen today).
- [ ] **Step 3 — implement.** At the very start of the run-transition handler (before `_resolve_inputs`/broker/adapters/`SideEffects`), call `self.resolve_bundle(session, run)`; store the resolved bundle for the rest of the run's transitions (cache keyed by `run.policy_bundle_hash`). Flag-on unloadable → raise `BundleUnavailable` (the worker's `except` path dead-letters via `jobs.fail`, rolling back the transaction so no side effect commits). Thread the resolved bundle to the transition methods that currently read `self.policy`.
- [ ] **Step 4 — run green** → PASS; existing pipeline integration tests still green (flag-off path unchanged). Re-pin engine guard.
- [ ] **Step 5 — commit** (`feat(pr6): resolve run bundle at job entry; refuse before side effects`).

---

## Task 8: Rubric-pinned decision-time scoring views (every live check)

Spec §5 (P1). Flag-on: re-price every live check's points/category from the resolved rubric X; flag-off unchanged.

**Files:** Modify `src/kyc_tool/domain/scoring.py` (new pure helper), `src/kyc_tool/orchestration/pipeline.py` (use it at decide); Test extend `tests/integration/test_bundle_pinning.py` (the mixed-era case) + a `tests/unit/test_scoring.py` unit for the helper.

**Interfaces:** Produces `scoring.rubric_scoring_views(views: list[CheckView], rubric: ScoringRubric) -> list[CheckView]` — returns new `CheckView`s where `points_awarded`/`category` come from `rubric.item(check_type)` (a `check_type` not in `rubric.check_types` → `points_awarded=0`, `category=""`; PASS/FAIL `status`, `reason_codes`, `source` unchanged). Consumes `ScoringRubric.item`/`.check_types`.

- [ ] **Step 1 — failing tests.** Unit:
```python
def test_rubric_scoring_views_reprices_by_type():
    # a CheckView(check_type=X, status=PASS, points_awarded=83, category="control_proof")
    # + a rubric where X = 10 pts / "account_access" → view has 10 / account_access,
    #   status/reason_codes unchanged; a type absent from the rubric → 0 / "".
    ...
```
Integration (mixed-era, §8.8b): seed bundle X and a modified bundle Y (Y gives some check_type 83 pts/`control_proof`; X gives it 10 pts/`account_access`). Create a live PASS of that type stamped under Y (drive a run under process-bundle Y, or write the check directly), then process a NEW decide under an X-pinned run **without replacing** that check. **Flag on:** `score()` counts 10 and the control gate loses that check; **flag off:** counts 83.
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement.** Add `rubric_scoring_views` to `scoring.py` (pure; uses `rubric.item(check_type)` in a try/except KeyError → 0/""). In `pipeline.py` decide (`:392-393`), build `views = [as_view(c) for c in live_checks]`; **if `enforce_bundle_pinning`**, `views = rubric_scoring_views(views, resolved_bundle.rubric)`; pass `views` to `score()`, `evaluate_gates()`, **and** the callback checks-summary builder. Flag-off passes the stamped `views` unchanged (strict no-op). Do **not** modify the check rows or re-run validators (PASS/FAIL re-judgment is PR 6b).
- [ ] **Step 4 — run green** → PASS; golden cases unchanged (flag off). Re-pin the engine guard hash. **Do NOT bump `ENGINE_BUILD_ID`** — all of PR 6 ships as a single engine version (`eng-1`); the re-pricing is part of that one version, not a change *from* a released prior engine. (The bump discipline applies to future post-PR-6 semantic changes.)
- [ ] **Step 5 — commit** (`feat(pr6): rubric-pinned decision-time scoring views (flag-on)`).

---

## Task 9: Atomic bundle+engine provenance; immutable run pin

Spec §5 (P1.1/P1.2, P3.8), §8.8/8.9/8.10. Write provenance from the resolved bundle; never rewrite `runs.policy_bundle_hash`.

**Files:** Modify `src/kyc_tool/orchestration/pipeline.py` (decide txn `:415-450` + broker short-circuit), `src/kyc_tool/checkstore/repo.py` (thread `bundle_hash` into every run-created write), `src/kyc_tool/events/ingest.py` (`:241-266` manual approve); Test extend `tests/integration/test_bundle_pinning.py`.

**Interfaces:** `checkstore.write_check`/`apply_check_intents`/`supersede_without_replacement`/`supersede_stale_identity_proof` gain a `policy_bundle_hash: str | None = None` param, stamped onto the row. The decide txn sets `checks.policy_bundle_hash` (all run-created checks), `DecisionRow.policy_shas` = resolved `shas`, `DecisionRow.engine_build_id` = `runs.engine_build_id` = `ENGINE_BUILD_ID`, and a DECIDE-audit-detail `resolved_policy_bundle_hash` = resolved `bundle_hash`.

- [ ] **Step 1 — failing test** (headline cross-bundle, §8.8): create a run under X; build/seed Y differing in a rubric field; process-bundle = Y. **Flag on:** decision under X; `checks.policy_bundle_hash` + DECIDE-audit `resolved_policy_bundle_hash` == `X.bundle_hash`; `DecisionRow.policy_shas` == `X.shas`; re-derive the bundle hash from `policy_shas` == `X.bundle_hash`; `runs.policy_bundle_hash` == `X.bundle_hash` (unchanged); `runs.engine_build_id` == `decisions.engine_build_id` == `ENGINE_BUILD_ID`. **Flag off:** `runs.policy_bundle_hash` == X unchanged, check/decision/audit provenance == Y values. Plus: broker-blocked short-circuit stamps both engine IDs (§8.9); a cascade successor check carries the resolved hash (§8.10); a `reviewer.manual_approve` writes `DecisionRow.engine_build_id` (§8.11).
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement.** Thread the resolved `bundle_hash` param through the `checkstore` write functions (default `None` for non-run callers). In the decide txn (incl. the broker-blocked DECIDE short-circuit): stamp checks with the resolved hash; write `DecisionRow.policy_shas`/`.engine_build_id`, `runs.engine_build_id`, and the DECIDE-audit `resolved_policy_bundle_hash` — all from the resolved bundle + `ENGINE_BUILD_ID`, atomic with the existing decide commit; **never** write `runs.policy_bundle_hash`. In `ingest._handle_manual_approve`, add `engine_build_id=ENGINE_BUILD_ID` to the `DecisionRow(...)`.
- [ ] **Step 4 — run green** → PASS; golden cases unchanged (flag off). Re-pin engine guard (no `ENGINE_BUILD_ID` bump — provenance writes aren't a scoring-semantic change).
- [ ] **Step 5 — commit** (`feat(pr6): atomic bundle+engine provenance; immutable run pin`).

---

## Task 10: `/readyz` bundle check + ops one-shots + epoch alert + recovery tests

Spec §7, §8.11–15. The operator surfaces + the activation/rollback recovery proofs.

**Files:** Modify the `/readyz` handler (`src/kyc_tool/api/app.py` or `routes_metrics.py` — grep `def readyz`/`/readyz`); Create `ops/verify_pinnable_backlog.py`, `ops/seed_policy_bundle.py`, `ops/activate_bundle_pinning_epoch.py`; Test `tests/integration/test_bundle_pinning_ops.py`.

**Interfaces:** `verify_pinnable_backlog(session_factory) -> list[str]` (run_ids whose non-NULL hash does NOT load; empty = OK); `seed_policy_bundle(session_factory, policy_dir: Path, expect_hash: str) -> None` (build → **compare to expect_hash → only then store**; raise on mismatch, no row); `activate_bundle_pinning_epoch(session_factory, *, expect_bundle_hash, expect_engine)`; each with a `main()` + `python -m` entry mirroring `ops/requeue_interrupted_jobs.py`. A `post_epoch_null_provenance(session) -> {...}` alert query (per-surface: `checks.created_at`/`decisions.decided_at` > epoch with NULL provenance).

- [ ] **Step 1 — failing tests:** `/readyz` 503 when the process bundle is absent from `policy_bundles`, 200 when present; `verify_pinnable_backlog` flags a run whose hash is absent, passes when seeded; `seed_policy_bundle` stores on a matching `expect_hash` and **adds no row on mismatch** (assert count unchanged + raises); the epoch alert flags a post-epoch-decided NULL row but not a legitimately-queued run; the **activation recovery** proof (§8.11 — a final-attempt `running` job requeued by `requeue_interrupted_jobs` then scored) and the **rollback acceptance** proof (§8.13 — flag-off restart on the same image → one decision, immutable pin, process-bundle provenance, both engine IDs).
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement** the `/readyz` addition (unconditional after 011: `load_bundle(session, current_bundle_hash)` must not be None else 503), the three ops one-shots (compute→compare→store ordering for seed), and the alert query.
- [ ] **Step 4 — run green** → PASS. Re-pin engine guard.
- [ ] **Step 5 — commit** (`feat(pr6): /readyz bundle check + verify/seed/activate ops + epoch alert`).

---

## Task 11: Docs + governance

Spec §9. No code — docs/ROADMAP/ADR/AUDIT_FINDINGS only.

**Files:** `docs/architecture-decisions.md` (prepend **ADR-005 — Per-run policy bundle pinning**), `docs/DEPLOYMENT.md` (Phase 1 + `activate_bundle_pinning_epoch`; the drained atomic worker-pool activation; the flag-only rollback), `docs/RUNBOOK.md` (the flag, `verify_pinnable_backlog`, `seed_policy_bundle`, `/readyz`, epoch alert, reprocess-needs-seed), `docs/OVERVIEW.md` (correct `:218-219` + `:411-413` over-claims → PR 6 boundary + **PR 8 = immutable evidence, PR 6b = revalidation, PR 10 = broker-state**), `.agents/ROADMAP.md` (item 7A shipped; bump PR 10's reserved ADR-005 → **ADR-006**; 7B pending for M4), `AUDIT_FINDINGS.md` (any build deviation).

- [ ] **Step 1 — write ADR-005** (context = the 3 problems from §1; decision = the rubric-pinned scoring + bundle+engine provenance + durable store + framed guard + epoch; rollout = §7; consequences = flag-off no-op, forward-only downgrade). Prepend above ADR-004 (newest-first).
- [ ] **Step 2 — DEPLOYMENT/RUNBOOK/OVERVIEW/ROADMAP** edits per §9. Verify no stray ADR number collision (grep `ADR-005`/`ADR-006`).
- [ ] **Step 3 — verify** the doc changes touch no code (`git diff --stat` = docs + ROADMAP only); re-pin engine guard NOT needed (no `src/` change).
- [ ] **Step 4 — commit** (`docs(pr6): ADR-005 + deployment/runbook/overview/roadmap`).

---

## Task 12: Final verification + bus RELEASE

- [ ] **Step 1:** `.venv/bin/ruff check .` → clean.
- [ ] **Step 2:** `.venv/bin/lint-imports` → **2 kept / 0 broken** (pure core stays pure; `policy_store` outside it).
- [ ] **Step 3:** `./manage.sh test` → full suite green (existing 548 + the PR 6 additions; flag-off golden cases unchanged).
- [ ] **Step 4:** dispatch the final whole-branch `code-reviewer` subagent (most capable model) via `scripts/review-package <pre-Task-1 base> HEAD`, with the spec's §-invariants as the attention lens; fix Critical/Important via one fix subagent; re-review.
- [ ] **Step 5:** Parent posts the bus `RELEASE [CLAUDE]` with the §3 verification artifact (commands + results, DB witness / CI run, the headline proofs — cross-bundle rubric+provenance pinning, refuse-before-side-effects, mixed-era re-pricing, forward-only downgrade — and the anchor SHA), pushes, and asks Codex to audit the PR 6 code range to `AUDIT-CLEAN`.

---

## Verification (whole PR)

- Per task: the task's own `.venv/bin/pytest <selector>` goes red → green against ephemeral Postgres, then commit.
- **Headline proofs:** the cross-bundle rubric+provenance test (§8.8), the mixed-era re-pricing test (§8.8b), the refuse-before-side-effects test (§8.7), and the forward-only-downgrade-refusal test (§8.1) are the load-bearing assertions.
- Whole PR: `ruff check` clean, `lint-imports` 2 kept/0 broken, `./manage.sh test` green.
- `KYC_Tool_Build_Package/` untouched; M2 untouched; deviations (if any) in `AUDIT_FINDINGS.md`.
- Final: bus RELEASE → Codex audits the code range to `AUDIT-CLEAN`.
