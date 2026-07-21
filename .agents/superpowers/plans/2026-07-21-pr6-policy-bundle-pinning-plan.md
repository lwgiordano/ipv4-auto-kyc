# PR 6 — Per-run policy bundle pinning Implementation Plan (rev 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Parent session is the SOLE committer/pusher/bus-writer (Claude⇄Codex bus discipline); subagents implement + hand diffs back.

**Goal:** Guarantee every run is scored, and its decision recorded, under exactly the policy bundle it was created with — durably reconstructable — and record which engine produced it, all behind a fail-closed `enforce_bundle_pinning` flag.

**Architecture:** A new `policy_bundles` table stores each bundle's raw files (base64) keyed by its `bundle_hash`, seeded at startup with read-back verification. The pipeline worker resolves each run's creation-pin bundle by hash **at job entry** (before any side effect); flag-on it scores under that bundle — re-pricing **every** live check's points/category from the resolved rubric at decision time — and writes bundle+engine provenance atomically without touching the immutable creation pin. A durable `bundle_pinning_epoch` record + drained worker-pool activation make the flag flip trustworthy.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 (sync psycopg), Postgres, Alembic, import-linter, pytest against ephemeral Postgres (`tests/pg.py`).

**Spec:** `.agents/superpowers/specs/2026-07-21-pr6-policy-bundle-pinning-design.md` (Codex AUDIT-CLEAN at rev 7, `41a3b3b`). Read the cited §sections for rationale.

**Rev 3 (Codex plan-review round 2, `6e45a63`):** every remaining test snippet is now runnable against the live schemas — event payloads use the real required fields (`email.verified`=email+domain+verified_at, `poc.submitted`=rir+poc_handle); migration/preflight fixtures insert valid FK chains (`events`+`runs.triggering_event_id`) and the real `jobs.payload_json`; `make_bundle_y` edits `scoring_rubric.json["items"]`; the callback assertion uses the real keys `type`/`points`; `PolicyBundle` imports from `policy.loader`; `_decode_files` is byte-level (key-set check moved to `load_bundle`) so the non-ASCII round-trip works; the framed-hash move test does a real equal-byte transfer + a non-`domain/` `broker_gate.py` case; and the attestation, activation-recovery, rollback, epoch-alert tests and the three ops CLIs are complete code (no `Add:`/"mirror" prose). **Rev 2 (`9d8b82e`):** removed `...`/`RAW=None`/`pytest.raises(Exception)`/"mirror"/"or inline"; preflight rejects NULL/absent/corrupt; strict base64; all downgrade surfaces; real `review-package` path.

## Global Constraints

- **Migration 011** is additive + hot-compatible; its downgrade is **forward-only after use** — it refuses once any `policy_bundles` row, any of the 3 provenance columns, or the `bundle_pinning_epoch` row is populated. Never make it destructive.
- **Flag-off is a strict no-op on scoring behavior.** Existing golden cases must not change.
- **`policy/` stays pure** — import-linter forbids `domain`/`validators`/`policy` from importing `sqlalchemy`/`db`/`storage`/`httpx`. The DB bundle store lives in the new non-pure `policy_store/`. Keep `lint-imports` at **2 kept / 0 broken**.
- **`KYC_Tool_Build_Package/` is immutable** — the 7 JSONs are read + stored verbatim; never edit them. Deviations → `AUDIT_FINDINGS.md`.
- **M2 untouched** — never touch `KYC_ENFORCE_POSITIVE_DECISIONS`.
- **`ENGINE_BUILD_ID`** matches `^eng-[1-9]\d*$` (e.g. `"eng-1"`); PR 6 ships as a single engine version `eng-1` — do NOT bump it mid-build. Never put the model identifier in it or any artifact.
- Commit trailer on every commit:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK
  ```
- Real-Postgres tests. `./manage.sh test` runs the WHOLE suite (ignores path args); targeted red→green uses `.venv/bin/pytest <selector>`. **Never** run `./manage.sh fmt`; `.venv/bin/ruff check .` is the lint gate.
- **Engine drift guard (Task 2) is added LAST-touching-src**, so it is pinned **once** against the finished tree — see Task 11. Tasks 3–10 do NOT re-pin it (it does not exist yet during them).

## Test infrastructure (real, from `tests/conftest.py`)

Session-scoped: `pg` (url str), `migrated` (url, schema at head), `engine`, `session_factory`, `policy` (loads `REPO_ROOT/"KYC_Tool_Build_Package"/"machine_readable"`). Function-scoped: `clean_db` (TRUNCATEs `_ALL_TABLES` + reseeds), `settings` (`enforce_positive_decisions=True`, `ui_enabled=True`), `app`, `client`, `post_event(case_id, event_type, payload, *, key=, actor=)`, `pipeline` (`Pipeline(session_factory, policy, FsStore, settings, adapters={})`), `worker` (`Worker(session_factory, {"run_transition": pipeline.handle_job}, backoff_base_seconds=0, on_dead_letter=pipeline.on_dead_letter)`), `publisher`/`callback_capture`. `REPO_ROOT` from `kyc_tool.config`. Migration tests use `pg` + local `_fresh_db(pg, name)` + `_config(url)` (see `tests/integration/test_migrations.py`).

**Shared PR-6 test helper** (add to `tests/integration/_bundle_helpers.py`, imported by the PR-6 integration tests):
```python
import json, shutil
from pathlib import Path
from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import read_policy_files, build_bundle, load_policy

NORMATIVE = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"

def raw_x() -> dict[str, bytes]:
    return read_policy_files(NORMATIVE)

def bundle_x():
    return build_bundle(raw_x())

def make_bundle_y(tmp_path: Path, *, check_type: str, points: int, category: str):
    """Copy the 7 normative files to tmp_path and rewrite one rubric item, yielding
    a DIFFERENT valid bundle Y (different bundle_hash). Returns (policy_dir, bundle)."""
    dst = tmp_path / "policy_y"
    shutil.copytree(NORMATIVE, dst)
    rubric = json.loads((dst / "scoring_rubric.json").read_bytes())
    # scoring_rubric.json top-level keys: version, threshold, hard_gates, items,
    # dynamic_rules (ScoringRubric.items: tuple[RubricItem]; RubricItem has
    # check_type/points/category — policy/types.py:14-16,23-40).
    for item in rubric["items"]:
        if item["check_type"] == check_type:
            item["points"], item["category"] = points, category
    (dst / "scoring_rubric.json").write_text(json.dumps(rubric, indent=2))
    return dst, load_policy(dst)
```

---

## File Structure

**Create:** `alembic/versions/011_policy_bundle_pinning.py`; `src/kyc_tool/policy_store/{__init__,repo}.py`; `src/kyc_tool/domain/engine.py`; `src/kyc_tool/ops/{verify_pinnable_backlog,seed_policy_bundle,activate_bundle_pinning_epoch}.py`; tests `tests/policy_driven/test_engine_build_id_guard.py`, `tests/unit/test_policy_store.py`, `tests/integration/{_bundle_helpers.py,test_bundle_pinning.py,test_bundle_pinning_ops.py,test_process_topology.py}`.

**Modify:** `src/kyc_tool/config.py`; `src/kyc_tool/policy/loader.py`; `src/kyc_tool/db/tables.py`; `src/kyc_tool/domain/scoring.py`; `src/kyc_tool/orchestration/pipeline.py`; `src/kyc_tool/checkstore/repo.py`; `src/kyc_tool/events/ingest.py`; `src/kyc_tool/api/app.py`; `src/kyc_tool/workers/{pipeline_worker,dev_worker}.py`; `tests/conftest.py` (`_ALL_TABLES`); `tests/integration/test_migrations.py` (`EXPECTED_TABLES`); docs.

**Reuse:** `db/session.{make_engine,make_session_factory,uow}`; the fixtures above; `ScoringRubric.item(check_type) -> RubricItem` (`policy/types.py:32`) + `.check_types` (`:39`); `ops/requeue_interrupted_jobs.py` one-shot shape.

---

## Task 1: Config flag `enforce_bundle_pinning`

**Files:** Modify `src/kyc_tool/config.py` (beside `enforce_positive_decisions`, `:94`); Test `tests/unit/test_production_config.py`.

**Interfaces — Produces:** `Settings.enforce_bundle_pinning: bool` (default `False`).

- [ ] **Step 1 — failing test** (append to `test_production_config.py`; use the module's existing `hardened()` helper — grep it in that file):
```python
def test_bundle_pinning_flag_defaults_off_and_toggles():
    from kyc_tool.config import Settings
    assert Settings().enforce_bundle_pinning is False
    assert Settings(enforce_bundle_pinning=True).enforce_bundle_pinning is True

def test_production_allows_pinning_off():
    # PR 6 is NOT boot-required in production; hardened() must still validate off.
    s = hardened(enforce_bundle_pinning=False)
    s.validate_for_production()   # must not raise
```
- [ ] **Step 2 — run red:** `.venv/bin/pytest tests/unit/test_production_config.py::test_bundle_pinning_flag_defaults_off_and_toggles -v` → FAIL: `TypeError: unexpected keyword argument 'enforce_bundle_pinning'`.
- [ ] **Step 3 — implement** in `Settings` beside `enforce_positive_decisions`:
```python
    # PR 6 (item 7A): when True, the pipeline worker loads each run's recorded
    # policy bundle by hash and scores under it, refusing if unloadable. Off by
    # default; activated via a drained worker-pool cutover (docs/DEPLOYMENT.md §…).
    enforce_bundle_pinning: bool = False
```
- [ ] **Step 4 — run green:** same selector + `test_production_allows_pinning_off` → PASS.
- [ ] **Step 5 — commit** `feat(pr6): enforce_bundle_pinning settings flag (off by default)`.

---

## Task 2: `domain/engine.py` — `ENGINE_BUILD_ID` (constant + regex invariant only)

Spec §6. Only the constant + its format invariant here; the whole-tree drift guard is Task 11 (pinned once against the finished tree).

**Files:** Create `src/kyc_tool/domain/engine.py`; add a test to `tests/unit/test_scoring.py` (create if absent).

**Interfaces — Produces:** `domain.engine.ENGINE_BUILD_ID: str`.

- [ ] **Step 1 — failing test:**
```python
import re
from kyc_tool.domain.engine import ENGINE_BUILD_ID

def test_engine_build_id_is_nonblank_versioned():
    assert re.fullmatch(r"eng-[1-9]\d*", ENGINE_BUILD_ID), ENGINE_BUILD_ID
```
- [ ] **Step 2 — run red:** `.venv/bin/pytest tests/unit/test_scoring.py::test_engine_build_id_is_nonblank_versioned -v` → FAIL (`ModuleNotFoundError`).
- [ ] **Step 3 — implement** `src/kyc_tool/domain/engine.py`:
```python
"""Engine identity (PR 6, item 7A). Bump ENGINE_BUILD_ID in a reviewed commit
when scoring/gate/validator/decision SEMANTICS change — the discipline each
policy JSON's `version` has (a whole-tree guard test forces a conscious
bump-or-repin on ANY src change). Format: eng-<positive int>."""

ENGINE_BUILD_ID = "eng-1"
```
- [ ] **Step 4 — run green** → PASS.
- [ ] **Step 5 — commit** `feat(pr6): ENGINE_BUILD_ID engine identity constant`.

---

## Task 3: Loader refactor — pure `read_policy_files` + `build_bundle`; widen `policy_dir`

Spec §4. Split the parse path; widen the frozen bundle's `policy_dir` to `Path | None`.

**Files:** Modify `src/kyc_tool/policy/loader.py`; Test create `tests/unit/test_policy_loader.py`.

**Interfaces — Produces:** `read_policy_files(policy_dir: Path) -> dict[str, bytes]`; `build_bundle(raw: dict[str, bytes], *, policy_dir: Path | None = None) -> PolicyBundle`; `PolicyBundle.policy_dir: Path | None`. `load_policy(policy_dir)` unchanged.

- [ ] **Step 1 — failing test** (`tests/unit/test_policy_loader.py`):
```python
from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import load_policy, read_policy_files, build_bundle

DIR = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"

def test_build_bundle_from_raw_matches_disk_load():
    disk = load_policy(DIR)
    rebuilt = build_bundle(read_policy_files(DIR), policy_dir=None)   # DB origin
    assert rebuilt.bundle_hash == disk.bundle_hash
    assert rebuilt.shas == disk.shas
    assert rebuilt.rubric == disk.rubric
    assert rebuilt.policy_dir is None and disk.policy_dir == DIR

def test_read_policy_files_returns_all_seven_as_bytes():
    from kyc_tool.policy.loader import POLICY_FILES
    raw = read_policy_files(DIR)
    assert set(raw) == set(POLICY_FILES)
    assert all(isinstance(v, bytes) for v in raw.values())
```
- [ ] **Step 2 — run red:** `.venv/bin/pytest tests/unit/test_policy_loader.py -v` → FAIL (`ImportError: cannot import name 'read_policy_files'`).
- [ ] **Step 3 — implement.** In `loader.py`: extract the read loop (`loader.py:48-53`) into `read_policy_files(policy_dir) -> dict[str,bytes]`; extract the sha/hash/parse body (`:55-74`) into `build_bundle(raw, *, policy_dir=None)` taking `raw` and passing `policy_dir` into the frozen `PolicyBundle(...)`; set `load_policy(policy_dir) = build_bundle(read_policy_files(policy_dir), policy_dir=policy_dir)`. Change the dataclass field (`:44`) to `policy_dir: Path | None`.
- [ ] **Step 4 — run green** → PASS; then `.venv/bin/pytest tests/policy_driven tests/golden -q` → unchanged (all call sites still work).
- [ ] **Step 5 — commit** `refactor(pr6): loader read_policy_files/build_bundle; policy_dir Path|None`.

---

## Task 4: Migration 011 + ORM (2 tables, 3 columns, parameterized forward-only downgrade)

Spec §3. Additive; downgrade refuses on EACH independent blocker.

**Files:** Create `alembic/versions/011_policy_bundle_pinning.py`; Modify `src/kyc_tool/db/tables.py`; add `"policy_bundles"` + `"bundle_pinning_epoch"` to `tests/conftest.py` `_ALL_TABLES` (`:33`) **and** `tests/integration/test_migrations.py` `EXPECTED_TABLES` (`:12`); Test extend `test_migrations.py`.

**Interfaces — Produces:** tables `policy_bundles(bundle_hash TEXT PK, files_json JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())`, `bundle_pinning_epoch(id INT PK DEFAULT 1 CHECK (id=1), activated_at TIMESTAMPTZ NOT NULL, bundle_hash TEXT NOT NULL REFERENCES policy_bundles(bundle_hash), engine_build_id TEXT NOT NULL CHECK (btrim(engine_build_id)<>''))`; columns `checks.policy_bundle_hash TEXT`, `runs.engine_build_id TEXT`, `decisions.engine_build_id TEXT` (each `CHECK (col IS NULL OR btrim(col)<>'')`). ORM: `PolicyBundleRow`, `BundlePinningEpochRow`, new mapped cols on `Check`/`Run`/`DecisionRow`.

- [ ] **Step 1 — failing tests** (append to `test_migrations.py`; reuse its `_fresh_db`/`_config`). First add the two names to `EXPECTED_TABLES`. Then:
```python
import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, text

_EPOCH_HASH = "a" * 64

# Each entry populates exactly ONE downgrade blocker; all 5 must independently
# refuse. Insert a bundle row first where an FK/hash is needed. Implementer
# confirms each table's NOT NULL columns from db/tables.py and adjusts the
# minimal INSERTs (the run/check/decision rows below list the columns those
# models require today; add any that are NOT NULL without a default).
_BUNDLE = "INSERT INTO policy_bundles (bundle_hash, files_json) VALUES ('h', '{}'::jsonb)"
@pytest.mark.parametrize("populate_sql, ids", [
    (_BUNDLE, "bundle_row"),
    (_BUNDLE + "; INSERT INTO bundle_pinning_epoch (id, activated_at, bundle_hash, "
     "engine_build_id) VALUES (1, now(), 'h', 'eng-1')", "epoch_row"),
    ("INSERT INTO cases (id) VALUES ('c'); "
     "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
     "payload_json) VALUES ('ev','c','k','ph','email.verified','{}'::jsonb,'{}'::jsonb); "
     "INSERT INTO runs (id, case_id, triggering_event_id, state, engine_build_id) "
     "VALUES ('r','c','ev','QUEUED','eng-1')", "run_engine_id"),   # runs.triggering_event_id NN FK
    ("INSERT INTO cases (id) VALUES ('c'); INSERT INTO checks (id, case_id, check_type, "
     "status, points_awarded, category, source, policy_bundle_hash) "
     "VALUES ('k','c','verified_email','pass',10,'x','seed','h')", "check_bundle_hash"),
    ("INSERT INTO cases (id) VALUES ('c'); INSERT INTO decisions (id, case_id, decision, "
     "score, gates_json, buy_enablement, policy_shas, manual, engine_build_id) "
     "VALUES ('d','c','manual_review_insufficient',0,'{}'::jsonb,'buy_locked_org_id_required',"
     "'{}'::jsonb,false,'eng-1')", "decision_engine_id"),
])
def test_011_downgrade_refuses_after_use(pg, populate_sql, ids):
    url = _fresh_db(pg, f"kyc_mig_011_{ids}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in populate_sql.split("; "):
            conn.execute(text(stmt))
    engine.dispose()
    with pytest.raises(Exception) as exc:      # alembic wraps the RuntimeError
        alembic_command.downgrade(cfg, "010")
    assert "forward-only" in str(exc.value).lower()

def test_011_downgrade_clean_when_unused(pg):
    url = _fresh_db(pg, "kyc_mig_011_clean")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "010")      # empty schema → clean
    alembic_command.upgrade(cfg, "head")
```
Add cases for `checks.policy_bundle_hash`, `decisions.engine_build_id`, and the epoch row to the parametrize list (5 blockers total) — the implementer writes the minimal valid INSERT for each from `db/tables.py` NOT NULL columns.
- [ ] **Step 2 — run red:** `.venv/bin/pytest tests/integration/test_migrations.py -k 011 -v` → FAIL (migration + `EXPECTED_TABLES` mismatch on the existing `test_upgrade_downgrade_upgrade`).
- [ ] **Step 3 — implement the migration.** `upgrade()`: `op.create_table` the two tables (named `ck_epoch_id`, `ck_epoch_engine_nonblank`, `fk_epoch_bundle_hash`); `op.add_column` the 3 nullable columns each with a named `CHECK`. `downgrade()`:
```python
def downgrade():
    conn = op.get_bind()
    used = conn.execute(sa.text(
        "SELECT (SELECT count(*) FROM policy_bundles) "
        "+ (SELECT count(*) FROM bundle_pinning_epoch) "
        "+ (SELECT count(*) FROM checks WHERE policy_bundle_hash IS NOT NULL) "
        "+ (SELECT count(*) FROM runs WHERE engine_build_id IS NOT NULL) "
        "+ (SELECT count(*) FROM decisions WHERE engine_build_id IS NOT NULL)"
    )).scalar_one()
    if used:
        raise RuntimeError(
            "migration 011 is forward-only after use: policy bundles / pinning "
            "provenance have been recorded on immutable rows; roll forward instead."
        )
    op.drop_column("decisions", "engine_build_id")
    op.drop_column("runs", "engine_build_id")
    op.drop_column("checks", "policy_bundle_hash")
    op.drop_table("bundle_pinning_epoch")   # epoch before policy_bundles (FK)
    op.drop_table("policy_bundles")
```
- [ ] **Step 4 — implement the ORM** in `db/tables.py`: two mapped classes + the 3 columns on `Check`/`Run`/`DecisionRow` (`Mapped[str | None] = mapped_column(Text)`). Add both tables to `_ALL_TABLES` (`conftest.py:33`).
- [ ] **Step 5 — run green:** `.venv/bin/pytest tests/integration/test_migrations.py -q` → all PASS (incl. the existing up/down/up now covering the 2 new tables via `EXPECTED_TABLES`).
- [ ] **Step 6 — commit** `feat(pr6): migration 011 policy_bundles + epoch + provenance columns`.

---

## Task 5: `policy_store` — store/load with strict-base64 read-back verify + epoch CAS

Spec §3 (base64/P1.1), §4, §7 (epoch/P2.2). All corruption normalizes to `BundleCorrupt`.

**Files:** Create `src/kyc_tool/policy_store/{__init__.py,repo.py}`; Test `tests/unit/test_policy_store.py` (real PG: uses `session_factory`, `clean_db`, `engine`).

**Interfaces — Produces:** `class BundleCorrupt(Exception)`; `store_bundle(session, raw: dict[str, bytes]) -> str`; `load_bundle(session, bundle_hash: str) -> PolicyBundle | None`; `activate_epoch(session, *, expect_bundle_hash: str, expect_engine: str) -> None`; `read_epoch(session) -> tuple[str, str] | None`; internal `_encode_files(raw)->dict[str,str]` / `_decode_files(files_json)->dict[str,bytes]`.

- [ ] **Step 1 — failing tests:**
```python
import base64, binascii, pytest
from sqlalchemy import text
from kyc_tool.policy.loader import POLICY_FILES
from kyc_tool.policy_store import repo as store
from tests.integration._bundle_helpers import raw_x, bundle_x

def test_encode_decode_roundtrip_non_ascii():
    raw = {"scoring_rubric.json": "café·任意".encode()}   # valid UTF-8, non-ASCII
    assert store._decode_files(store._encode_files(raw)) == raw

def test_decode_rejects_bad_base64():
    with pytest.raises(store.BundleCorrupt):
        store._decode_files({"scoring_rubric.json": "%%%not-base64%%%"})

def test_store_then_load_roundtrip(session_factory, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x()); s.commit()
    with session_factory() as s:
        b = store.load_bundle(s, h)
    assert b is not None and b.bundle_hash == h == bundle_x().bundle_hash
    assert b.policy_dir is None

def test_store_insert_path_verifies_and_returns_hash(session_factory, clean_db):
    with session_factory() as s:
        assert store.store_bundle(s, raw_x()) == bundle_x().bundle_hash   # read-back ran

def test_load_unknown_returns_none(session_factory, clean_db):
    with session_factory() as s:
        assert store.load_bundle(s, "0"*64) is None

def test_store_conflict_path_raises_on_tampered_row(session_factory, engine, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x()); s.commit()
    with engine.begin() as c:                              # tamper one file's base64
        c.execute(text("UPDATE policy_bundles SET files_json = "
                       "jsonb_set(files_json, '{scoring_rubric.json}', '\"%%%\"') "
                       "WHERE bundle_hash=:h"), {"h": h})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.store_bundle(s, raw_x())                    # conflict → read-back verifies

@pytest.mark.parametrize("mutate", [
    "files_json = files_json - 'scoring_rubric.json'",                    # missing key
    "files_json = jsonb_set(files_json, '{extra_file.json}', '\"eA==\"')",  # extra key
], ids=["missing", "extra"])
def test_load_raises_on_wrong_key_set(session_factory, engine, clean_db, mutate):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x()); s.commit()
    with engine.begin() as c:
        c.execute(text(f"UPDATE policy_bundles SET {mutate} WHERE bundle_hash=:h"), {"h": h})
    with session_factory() as s, pytest.raises(store.BundleCorrupt):
        store.load_bundle(s, h)

def test_store_idempotent(session_factory, clean_db):
    with session_factory() as s:
        store.store_bundle(s, raw_x()); store.store_bundle(s, raw_x()); s.commit()
        assert s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one() == 1

def test_activate_epoch_idempotent(session_factory, clean_db):
    with session_factory() as s:
        h = store.store_bundle(s, raw_x()); s.commit()
    for _ in range(2):
        with session_factory() as s:
            store.activate_epoch(s, expect_bundle_hash=h, expect_engine="eng-1"); s.commit()
    with session_factory() as s:
        assert store.read_epoch(s) == (h, "eng-1")

def test_activate_epoch_valid_y_after_x_fails_on_readback(session_factory, engine, clean_db, tmp_path):
    from tests.integration._bundle_helpers import make_bundle_y, bundle_x
    from kyc_tool.policy.loader import read_policy_files
    T = bundle_x().rubric.check_types[0]                    # a real rubric type
    with session_factory() as s:
        hx = store.store_bundle(s, raw_x())
        ydir, by = make_bundle_y(tmp_path, check_type=T, points=999, category="x")
        hy = store.store_bundle(s, read_policy_files(ydir)); s.commit()
    with session_factory() as s:                          # X activated first
        store.activate_epoch(s, expect_bundle_hash=hx, expect_engine="eng-1"); s.commit()
    with session_factory() as s, pytest.raises(store.BundleCorrupt):  # valid Y loses on read-back
        store.activate_epoch(s, expect_bundle_hash=hy, expect_engine="eng-1")
```
- [ ] **Step 2 — run red:** `.venv/bin/pytest tests/unit/test_policy_store.py -v` → FAIL (module missing).
- [ ] **Step 3 — implement `repo.py`:**
```python
import base64, binascii, json
from sqlalchemy import text
from kyc_tool.domain.engine import ENGINE_BUILD_ID
from kyc_tool.policy.loader import POLICY_FILES, build_bundle   # PolicyBundle also lives here

class BundleCorrupt(Exception): ...

def _encode_files(raw):                                   # raw: dict[str, bytes]
    return {n: base64.b64encode(b).decode() for n, b in raw.items()}

def _decode_files(files_json):                            # byte-level only; no key-set check
    out = {}
    for name, b64 in files_json.items():
        try:
            out[name] = base64.b64decode(b64, validate=True)   # strict: raises on non-b64
        except (binascii.Error, ValueError, TypeError) as e:
            raise BundleCorrupt(f"{name}: bad base64") from e
    return out

def store_bundle(session, raw) -> str:
    bundle_hash = build_bundle(raw).bundle_hash
    session.execute(text(
        "INSERT INTO policy_bundles (bundle_hash, files_json) VALUES (:h, :f) "
        "ON CONFLICT (bundle_hash) DO NOTHING"),
        {"h": bundle_hash, "f": json.dumps(_encode_files(raw))})
    verified = load_bundle(session, bundle_hash)          # read-back BOTH paths
    if verified is None or verified.bundle_hash != bundle_hash:
        raise BundleCorrupt(f"persisted bundle {bundle_hash} did not reconstruct")
    return bundle_hash

def load_bundle(session, bundle_hash):
    row = session.execute(text("SELECT files_json FROM policy_bundles WHERE bundle_hash=:h"),
                          {"h": bundle_hash}).first()
    if row is None:
        return None
    if set(row.files_json) != set(POLICY_FILES):          # exact 7-key set (missing OR extra)
        raise BundleCorrupt(f"{bundle_hash}: files_json keys {set(row.files_json)} "
                            f"!= {set(POLICY_FILES)}")
    try:
        bundle = build_bundle(_decode_files(row.files_json), policy_dir=None)
    except BundleCorrupt:
        raise
    except Exception as e:                                 # json/Pydantic → BundleCorrupt
        raise BundleCorrupt(f"{bundle_hash}: does not reconstruct") from e
    if bundle.bundle_hash != bundle_hash:
        raise BundleCorrupt(f"{bundle_hash}: reconstructed hash mismatch")
    return bundle

def activate_epoch(session, *, expect_bundle_hash, expect_engine) -> None:
    if expect_engine != ENGINE_BUILD_ID:
        raise BundleCorrupt(f"engine {expect_engine} != local {ENGINE_BUILD_ID}")
    if load_bundle(session, expect_bundle_hash) is None:
        raise BundleCorrupt(f"epoch bundle {expect_bundle_hash} not in store")
    session.execute(text(
        "INSERT INTO bundle_pinning_epoch (id, activated_at, bundle_hash, engine_build_id) "
        "VALUES (1, now(), :h, :e) ON CONFLICT (id) DO NOTHING"),
        {"h": expect_bundle_hash, "e": expect_engine})
    got = read_epoch(session)                              # read-back-compare
    if got != (expect_bundle_hash, expect_engine):
        raise BundleCorrupt(f"epoch already ({got}) != requested "
                            f"({expect_bundle_hash},{expect_engine})")

def read_epoch(session):
    row = session.execute(text(
        "SELECT bundle_hash, engine_build_id FROM bundle_pinning_epoch WHERE id=1")).first()
    return (row.bundle_hash, row.engine_build_id) if row else None
```
- [ ] **Step 4 — run green** → all PASS. `.venv/bin/lint-imports` → 2 kept / 0 broken (`policy_store` imports `policy`, not the reverse).
- [ ] **Step 5 — commit** `feat(pr6): policy_store strict-base64 store/load read-back + epoch CAS`.

---

## Task 6: Startup seeding + verification + attestation (API + pipeline/dev workers only)

Spec §5, §7 (P1.1/P2.4). One `seed_and_verify` interface; attest only after verify; `outbox`/`retention` stay policy-independent.

**Files:** Modify `src/kyc_tool/policy_store/repo.py` (add `seed_and_verify`), `src/kyc_tool/api/app.py`, `src/kyc_tool/workers/{pipeline_worker,dev_worker}.py`; Test `tests/integration/test_process_topology.py`, extend `tests/integration/test_bundle_pinning.py`.

**Interfaces — Produces:** `policy_store.seed_and_verify(session_factory, policy_dir) -> str` (stores + read-back-verifies; returns hash); `policy_store.attest(*, flag: bool, bundle_hash: str) -> None` (emits a structlog `bundle_pinning_ready` event with `flag`/`bundle_hash`/`engine_build_id=ENGINE_BUILD_ID`), called by the API + pipeline/dev startup **only after** a successful `seed_and_verify`.

- [ ] **Step 1 — failing tests:**
```python
# test_process_topology.py — ISOLATED interpreters (not shared sys.modules)
import subprocess, sys
def _imports_policy_store(module: str) -> bool:
    code = (f"import importlib,sys; importlib.import_module('{module}'); "
            "print('kyc_tool.policy_store' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    return out.stdout.strip() == "True"

def test_outbox_worker_does_not_import_policy_store():
    assert _imports_policy_store("kyc_tool.workers.outbox_worker") is False

def test_retention_does_not_import_policy_store():
    assert _imports_policy_store("kyc_tool.workers.retention") is False

# test_bundle_pinning.py — attestation both flag states + corrupt-startup
def test_attest_logs_flag_bundle_engine(caplog, session_factory, clean_db):
    import logging
    from kyc_tool.policy_store import repo as store
    from kyc_tool.config import REPO_ROOT
    d = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"
    for flag in (False, True):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            h = store.seed_and_verify(session_factory, d)          # seeds + read-back verifies
            store.attest(flag=flag, bundle_hash=h)                 # emits ONLY after verify
        rec = next(r for r in caplog.records if getattr(r, "msg", None) == "bundle_pinning_ready")
        assert rec.flag is flag and rec.engine_build_id == "eng-1" and rec.bundle_hash == h

def test_corrupt_seed_row_fails_startup_no_attestation(caplog, session_factory, engine, clean_db):
    import logging
    from sqlalchemy import text
    from kyc_tool.policy_store import repo as store
    from kyc_tool.policy.loader import read_policy_files
    from kyc_tool.config import REPO_ROOT
    d = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"
    with session_factory() as s:
        h = store.store_bundle(s, read_policy_files(d)); s.commit()
    with engine.begin() as c:                                      # tamper the persisted row
        c.execute(text("UPDATE policy_bundles SET files_json = jsonb_set("
                       "files_json,'{scoring_rubric.json}','\"%%%\"') WHERE bundle_hash=:h"), {"h": h})
    with caplog.at_level(logging.INFO), pytest.raises(store.BundleCorrupt):
        store.seed_and_verify(session_factory, d)                  # read-back → BundleCorrupt at startup
    assert not [r for r in caplog.records if getattr(r, "msg", None) == "bundle_pinning_ready"]
```
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement.** In `policy_store/repo.py`: `seed_and_verify(session_factory, policy_dir)` = `with uow(session_factory) as s: h = store_bundle(s, read_policy_files(policy_dir)); return h` (raises `BundleCorrupt` on a corrupt existing row → caller fails startup); `attest(*, flag, bundle_hash)` = `log.info("bundle_pinning_ready", flag=flag, bundle_hash=bundle_hash, engine_build_id=ENGINE_BUILD_ID)` (module `log = structlog.get_logger(__name__)`). In `pipeline_worker.py`/`dev_worker.py` `main()` and `api/app.py` factory: after `load_policy`, call `h = seed_and_verify(...)` then `attest(flag=settings.enforce_bundle_pinning, bundle_hash=h)`. Do **not** import `policy`/`policy_store` in `outbox_worker.py`/`retention.py`.
- [ ] **Step 4 — run green** → PASS.
- [ ] **Step 5 — commit** `feat(pr6): startup seed_and_verify + attestation (api/pipeline only)`.

---

## Task 7: Resolve the run's bundle at job entry; refuse before any side effect

Spec §5 (P1.3). Resolve FIRST; flag-on + absent → dead-letter, zero side effects.

**Files:** Modify `src/kyc_tool/orchestration/pipeline.py` (`handle_job` entry + `Pipeline.__init__`); Test extend `tests/integration/test_bundle_pinning.py`.

**Interfaces — Produces:** `Pipeline.resolve_bundle(session, run) -> PolicyBundle`; `class BundleUnavailable(Exception)`; an in-process `dict[str, PolicyBundle]` cache on the Pipeline.

- [ ] **Step 1 — failing test** (uses the real `worker`/`post_event` fixtures; a `poc.submitted` run would mint a token+email, so "zero side effects" is meaningful):
```python
def test_flag_on_absent_bundle_dead_letters_zero_side_effects(
        session_factory, policy, settings, tmp_path, engine, clean_db, post_event):
    from kyc_tool.orchestration.pipeline import Pipeline
    from kyc_tool.queue.worker import Worker
    from kyc_tool.storage.object_store import FsStore
    from sqlalchemy import text
    # ingest a run under the real policy (records its bundle_hash on the run)...
    post_event("case-absent", "poc.submitted", {"rir": "arin", "poc_handle": "POC-ACME"})
    # ...then point the run at a NOT-seeded hash + flag on
    with engine.begin() as c:
        c.execute(text("UPDATE runs SET policy_bundle_hash='deadbeef' WHERE case_id='case-absent'"))
        c.execute(text("TRUNCATE policy_bundles CASCADE"))
    pinned = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path/"e"), pinned, adapters={})
    w = Worker(session_factory, {"run_transition": pl.handle_job},
               backoff_base_seconds=0, on_dead_letter=pl.on_dead_letter)
    w.run_until_idle()
    with session_factory() as s:
        for tbl in ("adapter_results","review_tasks","poc_tokens","outbox","checks","decisions"):
            n = s.execute(text(f"SELECT count(*) FROM {tbl} WHERE case_id='case-absent'")).scalar_one()
            assert n == 0, f"{tbl} had side effects on an absent-bundle run"
        dead = s.execute(text("SELECT count(*) FROM jobs WHERE status='dead'")).scalar_one()
    assert dead >= 1
```
- [ ] **Step 2 — run red** → FAIL (side effects happen / no refusal today).
- [ ] **Step 3 — implement.** At the top of `Pipeline.handle_job` (before it dispatches the transition), load the `Run`, call `self.resolve_bundle(session, run)`: flag-off → `self.policy`; flag-on → `policy_store.load_bundle(session, run.policy_bundle_hash)`; `None` → raise `BundleUnavailable` (the worker's `except` path dead-letters via `jobs.fail`, rolling the transaction back so nothing commits). Cache by `run.policy_bundle_hash` on `self`. Thread the resolved bundle into the transition methods that read `self.policy`.
- [ ] **Step 4 — run green** → PASS; `.venv/bin/pytest tests/integration -k "pipeline or golden" -q` unchanged (flag-off path intact).
- [ ] **Step 5 — commit** `feat(pr6): resolve run bundle at job entry; refuse before side effects`.

---

## Task 8: Rubric-pinned decision-time scoring views (every live check) + callback

Spec §5 (rev-7 P1). Flag-on re-prices EVERY live check from the resolved rubric; the callback checks-summary uses the same views; flag-off unchanged.

**Files:** Modify `src/kyc_tool/domain/scoring.py`, `src/kyc_tool/orchestration/pipeline.py`; Test `tests/unit/test_scoring.py` + extend `tests/integration/test_bundle_pinning.py`.

**Interfaces — Produces:** `scoring.rubric_scoring_views(views: list[CheckView], rubric: ScoringRubric) -> list[CheckView]` — new `CheckView`s with `points_awarded`/`category` from `rubric.item(check_type)`; a type absent from `rubric.check_types` → `points_awarded=0, category=""`; `status`/`reason_codes`/`source`/`check_type` unchanged.

- [ ] **Step 1 — failing unit test:**
```python
from dataclasses import replace
from kyc_tool.domain.models import CheckView, CheckStatus
from kyc_tool.domain import scoring
from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import load_policy

RUBRIC = load_policy(REPO_ROOT/"KYC_Tool_Build_Package"/"machine_readable").rubric

def test_rubric_scoring_views_reprices_by_type():
    t = RUBRIC.check_types[0]
    item = RUBRIC.item(t)
    v = CheckView(check_type=t, status=CheckStatus.PASS, points_awarded=999,
                  category="stale", source="s", reason_codes=())
    out = scoring.rubric_scoring_views([v], RUBRIC)[0]
    assert out.points_awarded == item.points and out.category == item.category
    assert out.status is CheckStatus.PASS and out.check_type == t   # evidence unchanged

def test_rubric_scoring_views_zero_for_unknown_type():
    v = CheckView(check_type="not_in_rubric", status=CheckStatus.PASS,
                  points_awarded=50, category="c", source="s")
    out = scoring.rubric_scoring_views([v], RUBRIC)[0]
    assert out.points_awarded == 0 and out.category == ""
```
- [ ] **Step 2 — run red** → FAIL (`rubric_scoring_views` undefined).
- [ ] **Step 3 — implement** in `scoring.py`:
```python
def rubric_scoring_views(views, rubric):
    """Re-price each live check from `rubric` by check_type (PR 6 flag-on). A type
    absent from the rubric contributes no points/category. Evidence (status,
    reason_codes, source) is untouched — PASS/FAIL re-judgment is PR 6b."""
    out = []
    for v in views:
        try:
            item = rubric.item(v.check_type)
            pts, cat = item.points, item.category
        except KeyError:
            pts, cat = 0, ""
        out.append(replace(v, points_awarded=pts, category=cat))
    return out
```
In `pipeline.py` decide (`:392-393`), build `views = [checkstore.as_view(c) for c in live_checks]`, then **if `self.settings.enforce_bundle_pinning`**: `views = scoring.rubric_scoring_views(views, resolved_bundle.rubric)`. Pass `views` to `score()`, `evaluate_gates()`, **and** the callback checks-summary builder (grep the `_callback_body`/checks-summary construction in `pipeline.py`). Flag-off passes the stamped `views` unchanged.
- [ ] **Step 4 — failing integration test (mixed-era, score + callback).** The "era" is simulated by a live check whose **stamped** points (83) differ from the current rubric's points for that type — flag-on re-prices to the rubric value; flag-off keeps 83. `recalculate.requested` re-decides from live checks without re-fetching (RUNBOOK), so the seeded check survives into the decide:
```python
def test_mixed_era_reprices_score_and_callback(
        session_factory, policy, settings, tmp_path, engine, clean_db,
        post_event, publisher, callback_capture):
    from kyc_tool.checkstore import repo as checkstore
    from kyc_tool.domain.models import CheckStatus
    from kyc_tool.orchestration.pipeline import Pipeline
    from kyc_tool.queue.worker import Worker
    from kyc_tool.storage.object_store import FsStore
    from kyc_tool.policy_store import repo as store
    from tests.integration._bundle_helpers import raw_x
    from sqlalchemy import text
    T = policy.rubric.check_types[0]
    x_points = policy.rubric.item(T).points
    assert x_points != 83                                   # stamped 83 is a stale-era value
    with session_factory() as s:
        store.store_bundle(s, raw_x())
        s.execute(text("INSERT INTO cases (id) VALUES ('case-mix')"))
        checkstore.write_check(s, case_id="case-mix", check_type=T, status=CheckStatus.PASS,
                               points_awarded=83, category="control_proof", source="seed")
        s.commit()

    def _recalc_score(flag: bool) -> int:
        post_event("case-mix", "recalculate.requested", {})  # run pinned to app bundle X
        cfg = settings.model_copy(update={"enforce_bundle_pinning": flag})
        pl = Pipeline(session_factory, policy, FsStore(tmp_path / f"e{flag}"), cfg, adapters={})
        Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
               on_dead_letter=pl.on_dead_letter).run_until_idle()
        with session_factory() as s:
            return s.execute(text("SELECT score FROM decisions WHERE case_id='case-mix' "
                                  "ORDER BY decided_at DESC LIMIT 1")).scalar_one()

    assert _recalc_score(flag=True) == x_points             # re-priced to rubric X
    assert _recalc_score(flag=False) == 83                  # stamped value kept
    # callback checks-summary uses the same repriced views (last run above was flag-off;
    # do one more flag-on run and inspect the captured callback body).
    post_event("case-mix", "recalculate.requested", {})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "ecb"), cfg, adapters={})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter).run_until_idle()
    publisher.process_pending()
    # pipeline._callback_body (pipeline.py:510-519) emits body["checks"] = list of
    # {"type": check_type, "points": points_awarded if PASS else 0, ...} from the
    # SAME `views` list decide passes it — so flag-on repriced views show X's points.
    checks = callback_capture.requests[-1]["body"]["checks"]
    assert any(c["type"] == T and c["points"] == x_points for c in checks)
```
Every line is executable as written; the callback keys are the real ones. All four properties (flag-on score, flag-off score, callback points, and the surviving-check setup) are asserted.
- [ ] **Step 5 — run green** (unit + integration) → PASS; golden cases unchanged (flag off).
- [ ] **Step 6 — commit** `feat(pr6): rubric-pinned decision-time scoring views + callback (flag-on)`.

---

## Task 9: Atomic bundle+engine provenance; immutable run pin

Spec §5 (P1.1/P1.2, P3.8), §8.8/8.9/8.10/8.11.

**Files:** Modify `src/kyc_tool/orchestration/pipeline.py` (decide txn `:415-450` + broker short-circuit), `src/kyc_tool/checkstore/repo.py` (thread `policy_bundle_hash` into `write_check`/`apply_check_intents`/`supersede_without_replacement`/`supersede_stale_identity_proof`), `src/kyc_tool/events/ingest.py` (`:253-265` manual approve); Test extend `tests/integration/test_bundle_pinning.py`.

**Interfaces — Produces:** the checkstore write functions gain `policy_bundle_hash: str | None = None`, stamped onto the row. Decide writes `checks.policy_bundle_hash` (all run-created checks), `DecisionRow.policy_shas` = resolved `shas`, `DecisionRow.engine_build_id` = `runs.engine_build_id` = `ENGINE_BUILD_ID`, and a DECIDE-audit-detail `resolved_policy_bundle_hash` = resolved `bundle_hash`.

- [ ] **Step 1 — failing headline test** (cross-bundle; run pin immutable; typed provenance):
```python
def test_cross_bundle_pin_scores_x_and_records_x(
        session_factory, policy, settings, tmp_path, engine, clean_db, post_event):
    from tests.integration._bundle_helpers import make_bundle_y, bundle_x, raw_x
    from kyc_tool.policy.loader import read_policy_files
    from kyc_tool.policy_store import repo as store
    from kyc_tool.orchestration.pipeline import Pipeline
    from kyc_tool.queue.worker import Worker
    from kyc_tool.storage.object_store import FsStore
    from sqlalchemy import text
    bx = bundle_x()                                        # policy_dir=None (DB origin)
    T = bx.rubric.check_types[0]                           # a real rubric type so Y != X
    ydir, by = make_bundle_y(tmp_path, check_type=T, points=1, category="x")
    with session_factory() as s:
        store.store_bundle(s, raw_x())                    # X from the normative dir
        store.store_bundle(s, read_policy_files(ydir)); s.commit()
    # app ingests under X (records bx.bundle_hash on the run); worker process bundle = Y, flag on
    post_event("case-xy", "email.verified",
               {"email": "ops@acme.test", "domain": "acme.test",
                "verified_at": "2026-07-21T00:00:00Z"})
    pinned = settings.model_copy(update={"enforce_bundle_pinning": True})
    pl = Pipeline(session_factory, by, FsStore(tmp_path/"e"), pinned, adapters={})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter).run_until_idle()
    with session_factory() as s:
        run = s.execute(text("SELECT policy_bundle_hash, engine_build_id FROM runs "
                             "WHERE case_id='case-xy'")).first()
        assert run.policy_bundle_hash == bx.bundle_hash      # creation pin UNCHANGED
        assert run.engine_build_id == "eng-1"
        chk = s.execute(text("SELECT policy_bundle_hash FROM checks WHERE case_id='case-xy' "
                             "AND superseded_by_check_id IS NULL LIMIT 1")).first()
        assert chk.policy_bundle_hash == bx.bundle_hash      # scored/stamped under X, not Y
        dec = s.execute(text("SELECT policy_shas, engine_build_id FROM decisions "
                             "WHERE case_id='case-xy' ORDER BY decided_at DESC LIMIT 1")).first()
        assert dec.policy_shas == bx.shas and dec.engine_build_id == "eng-1"
        # reconstruct the bundle hash from the recorded shas → equals X
        # (loader.py:56-58 formula: sha256 of "".join(f"{name}:{sha};") over POLICY_FILES)
        import hashlib
        from kyc_tool.policy.loader import POLICY_FILES
        rebuilt = hashlib.sha256("".join(f"{n}:{dec.policy_shas[n]};"
                  for n in POLICY_FILES).encode()).hexdigest()
        assert rebuilt == bx.bundle_hash
```
Also add these sibling tests in the same file:
```python
def test_manual_approve_stamps_engine_build_id(session_factory, clean_db, post_event):
    # manual-approve writes DecisionRow(run_id=None) synchronously in the API
    # (events/ingest.py:_handle_manual_approve) — Task 9 stamps ENGINE_BUILD_ID there.
    post_event("c-ma", "reviewer.manual_approve", {"reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"})
    with session_factory() as s:
        eid = s.execute(text("SELECT engine_build_id FROM decisions WHERE case_id='c-ma' "
                             "AND manual=true ORDER BY decided_at DESC LIMIT 1")).scalar_one()
    assert eid == "eng-1"
```
- **Flag-off counterpart of the headline:** identical to `test_cross_bundle_pin_scores_x_and_records_x` but `enforce_bundle_pinning=False`; assert `runs.policy_bundle_hash == X.bundle_hash` (creation pin unchanged) while `checks.policy_bundle_hash` and `DecisionRow.policy_shas` record the **process** bundle **Y** (drift visible), and both engine-ID columns == `"eng-1"`.
- **Broker-blocked short-circuit:** seed the case's identifier onto a blocked `broker_entities` row (reuse PR 5b's `_seed_blocked_broker` in `tests/integration/test_review_completed_event.py`), drive an `email.verified` run flag-on, and assert `runs.engine_build_id == decisions.engine_build_id == "eng-1"`, `checks.policy_bundle_hash == X.bundle_hash`, and `decisions.decision == 'reject'` (the DECIDE short-circuit still stamps provenance).
- **Cascade provenance:** drive an `org_id.submitted` then `poc.token_verified` sequence so `checkstore.supersede_stale_identity_proof` writes a successor check; assert that successor's `checks.policy_bundle_hash == X.bundle_hash` under flag-on.
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement.** Add `policy_bundle_hash: str | None = None` to the four checkstore write fns; stamp it on the row (`checks.policy_bundle_hash`). In `pipeline.py` decide (incl. the broker-blocked DECIDE short-circuit): pass the resolved hash to every run-created check write; set `DecisionRow.policy_shas`/`.engine_build_id`, `runs.engine_build_id`, and the DECIDE-audit `resolved_policy_bundle_hash` from the resolved bundle + `ENGINE_BUILD_ID`, all inside the existing decide commit; **never** write `runs.policy_bundle_hash`. In `ingest._handle_manual_approve` (`:254`), add `engine_build_id=ENGINE_BUILD_ID` to `DecisionRow(...)`.
- [ ] **Step 4 — run green** → PASS; golden cases unchanged (flag off).
- [ ] **Step 5 — commit** `feat(pr6): atomic bundle+engine provenance; immutable run pin`.

---

## Task 10: `/readyz` bundle check + ops one-shots + epoch alert + recovery/rollback tests

Spec §7, §8.11–15. Preflight rejects NULL/absent/corrupt; epoch CLI compares local bundle; seed is compute→compare→store.

**Files:** Modify the `/readyz` handler (grep `readyz` — in `src/kyc_tool/api/routes_metrics.py` or `app.py`); Create the 3 ops modules; Test `tests/integration/test_bundle_pinning_ops.py`.

**Interfaces — Produces:**
- `verify_pinnable_backlog(session_factory) -> list[str]` — run_ids of every **runnable/requeueable `run_transition` job** (`jobs.status IN ('queued','running','dead')` JOIN `runs`) whose `runs.policy_bundle_hash` **IS NULL, or is absent from `policy_bundles`, or fails `load_bundle` (corrupt)**. Empty = OK.
- `seed_policy_bundle(session_factory, policy_dir, expect_hash) -> None` — `build_bundle` → **compare hash to `expect_hash` → raise `BundleCorrupt` (no row) on mismatch → only then `store_bundle`**.
- `activate_bundle_pinning_epoch` CLI `main()` — loads local policy, asserts `load_policy(policy_dir).bundle_hash == --expect-bundle-hash` and `ENGINE_BUILD_ID == --expect-engine`, then `store.activate_epoch(...)`.
- `post_epoch_null_provenance(session) -> dict` — checks by `created_at`, decisions by `decided_at` (see §7).

- [ ] **Step 1 — failing tests:**
```python
from sqlalchemy import text
from kyc_tool.ops.verify_pinnable_backlog import verify_pinnable_backlog
from kyc_tool.ops.seed_policy_bundle import seed_policy_bundle
from kyc_tool.policy_store import repo as store
from tests.integration._bundle_helpers import raw_x, bundle_x

def _queue_run_transition(engine, case, run_id, bundle_hash, status="queued", kind="run_transition"):
    with engine.begin() as c:
        c.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case})
        ev = f"{run_id}-ev"
        c.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, "
                       "event_type, actor_json, payload_json) VALUES (:e,:c,:e,'ph',"
                       "'recalculate.requested','{}'::jsonb,'{}'::jsonb)"), {"e": ev, "c": case})
        c.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state, "
                       "policy_bundle_hash) VALUES (:r,:c,:e,'QUEUED',:h)"),
                  {"r": run_id, "c": case, "e": ev, "h": bundle_hash})
        c.execute(text("INSERT INTO jobs (kind, payload_json, status, case_id, attempts) "
                       "VALUES (:k, CAST(:p AS jsonb), :s, :c, 0)"),
                  {"k": kind, "p": f'{{"run_id":"{run_id}"}}', "s": status, "c": case})

def test_verify_pinnable_backlog_flags_null_absent_corrupt(session_factory, engine, clean_db):
    with session_factory() as s:
        good = store.store_bundle(s, raw_x()); s.commit()
    with engine.begin() as c:                                        # a corrupt bundle row
        c.execute(text("INSERT INTO policy_bundles (bundle_hash, files_json) "
                       "VALUES ('corrupt', '{\"scoring_rubric.json\":\"%%%\"}'::jsonb)"))
    _queue_run_transition(engine, "c-ok",   "r-ok",   good)              # loads → OK
    _queue_run_transition(engine, "c-null", "r-null", None)              # NULL → flagged
    _queue_run_transition(engine, "c-abs",  "r-abs",  "0"*64)            # absent → flagged
    _queue_run_transition(engine, "c-cor",  "r-cor",  "corrupt")        # corrupt → flagged
    _queue_run_transition(engine, "c-dead", "r-dead", "0"*64, status="dead")  # dead-requeueable → flagged
    _queue_run_transition(engine, "c-done", "r-done", None, status="done")    # terminal → NOT flagged
    _queue_run_transition(engine, "c-oth",  "r-oth",  None, kind="outbox_delivery")  # non-pipeline → NOT flagged
    assert set(verify_pinnable_backlog(session_factory)) == {"r-null", "r-abs", "r-cor", "r-dead"}

def test_seed_policy_bundle_stores_on_match_no_row_on_mismatch(session_factory, engine, clean_db):
    from kyc_tool.config import REPO_ROOT
    d = REPO_ROOT/"KYC_Tool_Build_Package"/"machine_readable"
    with pytest.raises(store.BundleCorrupt):
        seed_policy_bundle(session_factory, d, expect_hash="0"*64)   # mismatch
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one() == 0
    seed_policy_bundle(session_factory, d, expect_hash=bundle_x().bundle_hash)   # match
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM policy_bundles")).scalar_one() == 1

def test_readyz_503_when_process_bundle_absent(client, engine, clean_db):
    assert client.get("/readyz").status_code == 200          # app seeded at startup
    with engine.begin() as c:
        c.execute(text("TRUNCATE policy_bundles CASCADE"))
    assert client.get("/readyz").status_code == 503
```
```python
def test_activation_recovery_requeues_final_attempt(session_factory, engine, clean_db):
    from kyc_tool.ops.requeue_interrupted_jobs import requeue_interrupted
    _queue_run_transition(engine, "c-rec", "r-rec", None)          # a queued run_transition job
    with engine.begin() as c:                                      # simulate crash mid-final-attempt
        c.execute(text("UPDATE jobs SET status='running', attempts=max_attempts, "
                       "locked_by='dead-worker' WHERE case_id='c-rec'"))
    assert requeue_interrupted(session_factory) == 1               # run AFTER all workers stopped
    with session_factory() as s:
        row = s.execute(text("SELECT status, attempts, max_attempts FROM jobs "
                             "WHERE case_id='c-rec'")).one()
        assert row.status == "queued" and row.attempts == row.max_attempts - 1  # forced-stop attempt returned
        assert s.execute(text("SELECT count(*) FROM jobs WHERE status='running'")).scalar_one() == 0

def test_rollback_flag_off_still_decides_with_provenance(
        session_factory, policy, settings, tmp_path, clean_db, post_event):
    # Flag-OFF rollback on the PR6 image: a normal run still decides, the creation
    # pin is intact, and bundle+engine provenance is written (no post-epoch NULL).
    from kyc_tool.orchestration.pipeline import Pipeline
    from kyc_tool.queue.worker import Worker
    from kyc_tool.storage.object_store import FsStore
    from kyc_tool.policy_store import repo as store
    from tests.integration._bundle_helpers import raw_x, bundle_x
    with session_factory() as s:
        store.store_bundle(s, raw_x()); s.commit()
    post_event("c-rb", "email.verified",
               {"email": "o@acme.test", "domain": "acme.test", "verified_at": "2026-07-21T00:00:00Z"})
    off = settings.model_copy(update={"enforce_bundle_pinning": False})
    pl = Pipeline(session_factory, policy, FsStore(tmp_path / "e"), off, adapters={})
    Worker(session_factory, {"run_transition": pl.handle_job}, backoff_base_seconds=0,
           on_dead_letter=pl.on_dead_letter).run_until_idle()
    with session_factory() as s:
        run = s.execute(text("SELECT policy_bundle_hash, engine_build_id FROM runs "
                             "WHERE case_id='c-rb'")).first()
        assert run.policy_bundle_hash == bundle_x().bundle_hash and run.engine_build_id == "eng-1"
        dec = s.execute(text("SELECT engine_build_id FROM decisions WHERE case_id='c-rb'")).first()
        assert dec.engine_build_id == "eng-1"

def test_post_epoch_null_alert(session_factory, engine, clean_db):
    from kyc_tool.ops.activate_bundle_pinning_epoch import post_epoch_null_provenance
    from kyc_tool.policy_store import repo as store
    from tests.integration._bundle_helpers import raw_x
    with session_factory() as s:
        h = store.store_bundle(s, raw_x())
        store.activate_epoch(s, expect_bundle_hash=h, expect_engine="eng-1"); s.commit()
    with engine.begin() as c:
        c.execute(text("INSERT INTO cases (id) VALUES ('c-al')"))
        c.execute(text("INSERT INTO decisions (id, case_id, decision, score, gates_json, "
                       "buy_enablement, policy_shas, manual) VALUES ('d-al','c-al','x',0,"
                       "'{}'::jsonb,'buy_locked_org_id_required','{}'::jsonb,false)"))  # decided_at=now()>epoch, engine NULL
        c.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                       "actor_json, payload_json) VALUES ('e-q','c-al','kq','ph','email.verified',"
                       "'{}'::jsonb,'{}'::jsonb)"))
        c.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                       "VALUES ('r-q','c-al','e-q','QUEUED')"))   # queued, NULL engine — legitimate
    with session_factory() as s:
        alert = post_epoch_null_provenance(s)
    assert "d-al" in alert["decisions"] and "r-q" not in alert.get("runs", [])
```
- [ ] **Step 2 — run red** → FAIL.
- [ ] **Step 3 — implement.** Each ops module mirrors `ops/requeue_interrupted_jobs.py` (function + `main()` + `python -m` entry via `make_session_factory(make_engine(get_settings().database_url))`):
```python
# ops/verify_pinnable_backlog.py
def verify_pinnable_backlog(session_factory) -> list[str]:
    bad = []
    with session_factory() as s:
        rows = s.execute(text(
            "SELECT DISTINCT r.id AS run_id, r.policy_bundle_hash AS h FROM jobs j "
            "JOIN runs r ON r.id = (j.payload_json->>'run_id') "
            "WHERE j.kind='run_transition' AND j.status IN ('queued','running','dead')")).all()
        for run_id, h in rows:
            if h is None:
                bad.append(run_id); continue
            try:
                if store.load_bundle(s, h) is None:
                    bad.append(run_id)
            except store.BundleCorrupt:
                bad.append(run_id)
    return bad

# ops/seed_policy_bundle.py  (compute → compare → store; NEVER store on mismatch)
def seed_policy_bundle(session_factory, policy_dir, expect_hash: str) -> None:
    raw = read_policy_files(policy_dir)
    computed = build_bundle(raw).bundle_hash
    if computed != expect_hash:
        raise store.BundleCorrupt(f"{policy_dir} hashes {computed} != expected {expect_hash}")
    with uow(session_factory) as s:
        store.store_bundle(s, raw)

# ops/activate_bundle_pinning_epoch.py
def post_epoch_null_provenance(session) -> dict:
    ep = session.execute(text("SELECT activated_at FROM bundle_pinning_epoch WHERE id=1")).first()
    if ep is None:
        return {"decisions": [], "checks": [], "runs": []}
    at = ep.activated_at
    dec = [r.id for r in session.execute(text("SELECT id FROM decisions WHERE decided_at > :at "
           "AND engine_build_id IS NULL"), {"at": at})]
    chk = [r.id for r in session.execute(text("SELECT id FROM checks WHERE created_at > :at "
           "AND policy_bundle_hash IS NULL"), {"at": at})]
    runs = [r.run_id for r in session.execute(text("SELECT DISTINCT run_id FROM decisions "
            "WHERE decided_at > :at AND engine_build_id IS NULL AND run_id IS NOT NULL"), {"at": at})]
    return {"decisions": dec, "checks": chk, "runs": runs}

def main() -> int:                       # activate_bundle_pinning_epoch CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-bundle-hash", required=True)
    ap.add_argument("--expect-engine", required=True)
    args = ap.parse_args()
    settings = get_settings()
    local = load_policy(settings.policy_dir)                       # compare LOCAL loaded bundle
    if local.bundle_hash != args.expect_bundle_hash:
        raise SystemExit(f"local bundle {local.bundle_hash} != --expect-bundle-hash")
    sf = make_session_factory(make_engine(settings.database_url))
    with uow(sf) as s:                                            # activate_epoch also checks
        store.activate_epoch(s, expect_bundle_hash=args.expect_bundle_hash,   # expect_engine==ENGINE_BUILD_ID
                             expect_engine=args.expect_engine)     # + read-back CAS
    print("epoch activated"); return 0
```
And the `/readyz` addition — after 011, **unconditionally** (grep the readyz handler, `api/routes_metrics.py` or `app.py`): compute the process bundle's `bundle_hash` (from the app's loaded policy) and return **503** if `store.load_bundle(session, that_hash) is None`, else keep the existing 200 path. Add a direct `store.activate_epoch(..., expect_engine="eng-999")` → `BundleCorrupt` test proving the local-engine mismatch rejection.
- [ ] **Step 4 — run green** → PASS.
- [ ] **Step 5 — commit** `feat(pr6): /readyz bundle check + verify/seed/activate ops + epoch alert`.

---

## Task 11: Engine drift guard (pinned once) + Docs + governance

The framed whole-tree guard is added HERE, after all `src/` changes, so it pins once against the finished tree.

**Files:** Create `tests/policy_driven/test_engine_build_id_guard.py`; `docs/architecture-decisions.md` (ADR-005), `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `docs/OVERVIEW.md`, `.agents/ROADMAP.md`, `AUDIT_FINDINGS.md`.

- [ ] **Step 1 — write the guard test** (framed records; rename/move/empty proofs):
```python
import hashlib, shutil
from pathlib import Path
SRC = Path(__file__).resolve().parents[2] / "src" / "kyc_tool"
EXPECTED_ENGINE_SOURCE_HASH = "<PIN AT STEP 2>"

def _framed_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts: continue
        rel = p.relative_to(root).as_posix().encode(); data = p.read_bytes()
        h.update(rel + b"\x00" + str(len(data)).encode() + b"\x00" + data)
    return h.hexdigest()

def test_engine_source_hash_pinned():
    assert _framed_hash(SRC) == EXPECTED_ENGINE_SOURCE_HASH, (
        "src/kyc_tool changed — if scoring/decision semantics changed bump "
        "ENGINE_BUILD_ID; re-pin EXPECTED_ENGINE_SOURCE_HASH in the SAME commit.")

def test_framing_catches_cross_file_move_rename_empty(tmp_path):
    # A real equal-byte transfer: move "MOVE\n" from a.py to b.py. The UNFRAMED
    # concat of sorted-path bytes is byte-IDENTICAL both ways; framing
    # (path\0len\0bytes) makes the two trees differ — proving boundary safety.
    d1 = tmp_path/"d1"; d1.mkdir()
    (d1/"a.py").write_bytes(b"AAAA\nMOVE\n"); (d1/"b.py").write_bytes(b"BBBB\n")
    d2 = tmp_path/"d2"; d2.mkdir()
    (d2/"a.py").write_bytes(b"AAAA\n"); (d2/"b.py").write_bytes(b"MOVE\nBBBB\n")
    assert b"AAAA\nMOVE\n"+b"BBBB\n" == b"AAAA\n"+b"MOVE\nBBBB\n"   # unframed concat identical
    assert _framed_hash(d1) != _framed_hash(d2)                     # framed differs
    d3 = tmp_path/"d3"; d3.mkdir(); (d3/"a.py").write_bytes(b"AAAA\n")
    d4 = tmp_path/"d4"; d4.mkdir(); (d4/"a.py").write_bytes(b"AAAA\n"); (d4/"z.py").write_bytes(b"")
    assert _framed_hash(d3) != _framed_hash(d4)                     # empty-file add
    d5 = tmp_path/"d5"; d5.mkdir(); (d5/"a.py").write_bytes(b"AAAA\n")
    d6 = tmp_path/"d6"; d6.mkdir(); (d6/"renamed.py").write_bytes(b"AAAA\n")
    assert _framed_hash(d5) != _framed_hash(d6)                     # rename (path change)

def test_non_domain_edit_trips_guard(tmp_path):
    # The whole src tree is in the closure, so a semantic edit OUTSIDE domain/
    # (broker_gate.py) changes the hash — no curated list can omit it.
    base = tmp_path/"src"; shutil.copytree(SRC, base)
    h0 = _framed_hash(base)
    bg = base/"orchestration"/"broker_gate.py"
    bg.write_bytes(bg.read_bytes() + b"\n# semantic change\n")
    assert _framed_hash(base) != h0
```
- [ ] **Step 2 — pin.** Run `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v`; copy the actual digest from the failure into `EXPECTED_ENGINE_SOURCE_HASH`; re-run → PASS.
- [ ] **Step 3 — docs.** ADR-005 (context = §1's 3 problems; decision = rubric-pinned scoring + bundle+engine provenance + durable store + framed guard + epoch; rollout = §7; consequences = flag-off no-op + forward-only downgrade), prepended above ADR-004. `DEPLOYMENT.md`: Phase 1 + `activate_bundle_pinning_epoch`; the drained activation (preflight `verify_pinnable_backlog` → confirm zero old workers → `requeue_interrupted_jobs` → flag-on workers + attestation → resume); the flag-only rollback. `RUNBOOK.md`: the flag, `verify_pinnable_backlog`, `seed_policy_bundle`, `/readyz`, epoch alert, reprocess-needs-seed. `OVERVIEW.md`: correct `:218-219` + `:411-413` → PR 6 boundary + **PR 8 immutable evidence / PR 6b revalidation / PR 10 broker-state**. `.agents/ROADMAP.md`: item 7A shipped; bump PR 10's reserved ADR-005 → **ADR-006**; 7B pending for M4.
- [ ] **Step 4 — verify + commit** `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -q` PASS; commit `feat(pr6): engine drift guard (pinned) + ADR-005 + deployment/runbook/overview/roadmap`.

---

## Task 12: Final verification + bus RELEASE

- [ ] **Step 1:** `.venv/bin/ruff check .` → clean.
- [ ] **Step 2:** `.venv/bin/lint-imports` → **2 kept / 0 broken**.
- [ ] **Step 3:** `./manage.sh test` → full suite green (existing 548 + PR 6 additions; flag-off golden cases unchanged).
- [ ] **Step 4:** dispatch the final whole-branch code reviewer (most capable model). Generate its package with the subagent-driven-development skill's helper at its real path — `"$(git rev-parse --show-toplevel)"` is NOT where it lives; run `/root/.claude/skills/subagent-driven-development/scripts/review-package <PRE_TASK1_BASE> HEAD` (BASE = the commit before Task 1, recorded in the progress ledger) and pass the printed path; or, if that script is unavailable, `git log --oneline <BASE>..HEAD`, `git diff --stat <BASE>..HEAD`, and `git diff -U10 <BASE>..HEAD` redirected to one file. Attention lens = the spec §-invariants. Fix Critical/Important via one fix subagent; re-review.
- [ ] **Step 5:** Parent posts the bus `RELEASE [CLAUDE]` with the §3 verification artifact (commands + results, DB witness / CI run, the headline proofs — cross-bundle rubric+provenance pinning, refuse-before-side-effects, mixed-era re-pricing, forward-only downgrade — the anchor SHA), pushes, and asks Codex to audit the PR 6 code range to `AUDIT-CLEAN`.

---

## Verification (whole PR)

- Per task: the task's own `.venv/bin/pytest <selector>` red → green against ephemeral Postgres, then commit.
- **Headline proofs:** `test_cross_bundle_pin_scores_x_and_records_x` (§8.8), `test_mixed_era_reprices_score_gate_and_callback` (§8.8b), `test_flag_on_absent_bundle_dead_letters_zero_side_effects` (§8.7), and the parameterized `test_011_downgrade_refuses_after_use` (§8.1) are load-bearing.
- Whole PR: `ruff check` clean, `lint-imports` 2 kept/0 broken, `./manage.sh test` green.
- `KYC_Tool_Build_Package/` untouched; M2 untouched; deviations (if any) in `AUDIT_FINDINGS.md`.
- Final: bus RELEASE → Codex audits the code range to `AUDIT-CLEAN`.
