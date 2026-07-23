# PR 7b-core — Outbox stream separation + local decision ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The parent session is the SOLE committer; subagents implement + hand diffs back.

**Goal:** Separate the outbox claim into per-`(case_id, ordering_stream)` FIFO streams, add an internal per-case `decision_sequence`, fence every claim with a `claim_token`, add a best-effort local `superseded` guard, and lock all of it down with exhaustive relational integrity — shipped as migration `013` with a drained cutover and a reversible-before-first-supersession downgrade.

**Architecture:** Migration `013` (`down_revision='012'`) adds `outbox.ordering_stream`/`case_id NOT NULL`, a fenced-claim column set (`claim_token`/`claim_lease_expires_at`/`claimed_by`), `decisions.decision_sequence` + `cases.last_decision_sequence`, a triple FK binding each callback to its automatic decision, and exhaustive per-status lifecycle CHECKs. The publisher claims the min-id pending row of one `(case, stream)` under a fresh `claim_token`; every terminal (`_record_delivered`/`_record_failure`/`_record_superseded`) is a single fenced `UPDATE … WHERE id AND status='pending' AND claim_token=:token RETURNING id` whose winner performs all dependent writes in-transaction and whose stale loser emits `outbox_stale_claim_completion` and does nothing. The decide transaction allocates `decision_sequence` from the locked `cases.last_decision_sequence` counter; a local guard suppresses an older requeued callback only when a higher-sequence decision already carries a locally-stamped `published_at`.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 (sync psycopg), Postgres, Alembic, import-linter, pytest against ephemeral Postgres (`tests/pg.py`).

**Spec:** `.agents/superpowers/specs/2026-07-22-pr7b-core-outbox-stream-separation-design.md` (REVIEW-CLEAN rev 8). It is the authoritative contract — read the cited §sections for rationale; every CHECK, SQL, seam, and mutation witness below is drawn from it.

## Global Constraints

- **Python 3.11, FastAPI, SQLAlchemy 2 (sync psycopg), Postgres, Alembic.** Tests run against real ephemeral Postgres — the session-scoped `pg` fixture (`tests/pg.py`) self-provisions a cluster, so `.venv/bin/pytest <selector>` runs real-Postgres tests with no extra setup.
- **Test commands.** `./manage.sh test` runs the WHOLE suite (it ignores path args — `.substrate/lib.sh:214` `sub_test` → `test_python` runs bare `pytest`). Targeted red→green in each step uses **`.venv/bin/pytest <selector> -v`**. The final gate per task runs `./manage.sh test`.
- **Lint gate.** `.venv/bin/ruff check .` (rules `E,F,I,UP,B,SIM`; **no `;`/E702 multi-statement lines**) **and** `.venv/bin/lint-imports` (import-linter, **2 contracts kept / 0 broken**). New code lives in `db`/`outbox`/`orchestration`/`events`/`workers`/`api`/`ui`/`ops` — never add an import into `domain`/`validators`/`policy`/`adapters`, so both contracts stay green. **Never run `./manage.sh fmt`.**
- **CANONICAL CLOSE-OUT ORDER for any `src/kyc_tool/**`-touching task (findings 6):** the whole-source drift guard (`test_engine_source_hash_pinned`) fails on ANY `src/` edit, so `./manage.sh test` returns nonzero until the hash is re-pinned. Every such task's close-out therefore runs **in this exact order — never `./manage.sh test` before the re-pin**: (1) run the task's targeted `.venv/bin/pytest` selectors green; (2) run the drift-guard test once to observe it RED (the deliberately-red TDD step — label it RED, never a gate); (3) compute+paste the new `EXPECTED_ENGINE_SOURCE_HASH` and re-run the drift-guard test GREEN; (4) `./manage.sh test` → exit 0; (5) `.venv/bin/ruff check .` → exit 0; (6) `.venv/bin/lint-imports` → exit 0; (7) commit. Tasks that touch only `alembic/`, docs, or `.agents/` skip steps 2-3 and run `./manage.sh test` directly.
- **Exact exception types in tests (finding 9).** Never assert a bare `pytest.raises(Exception)` on an authoritative refusal. Use: `sqlalchemy.exc.IntegrityError` for CHECK / FK / unique / NOT-NULL violations; `sqlalchemy.exc.OperationalError` with `exc.value.orig.sqlstate == "55P03"` for a `lock_timeout` (psycopg3 exposes `.sqlstate` on `.orig`); `RuntimeError` for a migration/CLI fail-closed refusal raised in Python (`alembic.command.upgrade`/`downgrade` propagate the migration's `RuntimeError` unwrapped) — always paired with an assertion on the message substring. Match a `lock_timeout` on a real statement with `conn.execute(text("SET lock_timeout='2s'"))` first.
- **Delivery-layer only — NO scoring/gate/decision semantic change.** The callback HTTP body stays **byte-identical** to pre-7b: `decision_sequence` is written to the `decisions`/`outbox` **columns only**, never added to `payload_json` or the wire (that is 7b-activation / `014`).
- **Engine drift guard.** Every task that edits any file under `src/kyc_tool/**` MUST re-pin `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py` **in the same commit** (see the re-pin one-liner in "Test infrastructure" below). **Do NOT bump `ENGINE_BUILD_ID`** — this is not a scoring change. The src-touching tasks are **1, 2 (the shared `ops/backfill_parity.py`), 3, 4, 5, 7, 8** — each re-pins. Tasks that touch only `alembic/`, docs, or `.agents/` (parts of 6, 9) do NOT re-pin (the guard closure is `src/kyc_tool/**/*.py` only).
- **Do NOT edit `KYC_Tool_Build_Package/`** (normative spec, immutable) or anything touching **M2** / `KYC_ENFORCE_POSITIVE_DECISIONS` / `enforce_positive_decisions`.
- **ROADMAP lineage.** `.agents/ROADMAP.md §C` already reserves PR 7b-core = `013` (row 74, State `pending`) and 7b-activation = `014`. Do **not** renumber or edit the reservations except to flip 7b-core's State `pending → shipped` in the same PR that lands `013` (Task 9). `tests/unit/test_migration_lineage.py` must stay green.
- **Codex build constraints (carry verbatim):** migration/outbox/CLI mutation proofs run against **real Postgres and the real CLI entry points** (`main()` / `python -m …`), never a helper-only shim. The retention "zero active tasks before it deletes" attestation is a **runbook / `TODO(integration)`** deployment acceptance, **NOT** a pytest (retention is a scheduled one-shot with no in-repo liveness registry).
- **Branch:** work on `claude/project-setup-verify-kpfgjs`. **Never put any model identifier in artifacts.**
- **Commit trailer on EVERY commit:**
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK
  ```

## Test infrastructure (real, from `tests/conftest.py`)

Session-scoped fixtures: `pg` (url str), `migrated` (url, schema at head — `alembic upgrade head` on an empty DB), `engine`, `session_factory`, `policy`. Function-scoped: `clean_db` (`TRUNCATE _ALL_TABLES RESTART IDENTITY CASCADE` + reseeds the v1 witness + broker entities), `settings` (`enforce_positive_decisions=True`, `ui_enabled=True`, `platform_callback_url="http://platform.test"`), `app`, `client`, `post_event(case_id, event_type, payload, *, key=, actor=)` (returns `(response, key)`), `pipeline` (`Pipeline(session_factory, policy, FsStore, settings, adapters={})`), `worker` (`Worker(..., backoff_base_seconds=0, on_dead_letter=pipeline.on_dead_letter)`), `publisher` (`OutboxPublisher` with a `httpx.MockTransport` wired to `callback_capture`), `callback_capture` (`.requests` list, `.status_code`), `email_sender` (`LoggingEmailSender`). `REPO_ROOT` from `kyc_tool.config`.

Migration tests use `pg` + the module-level helpers already in `tests/integration/test_migrations.py`: `_fresh_db(pg, name)` (drops/creates a dedicated DB, returns its url) and `_config(url)` (an `AlembicConfig`). Drive migrations with `alembic.command.upgrade(cfg, "013")` / `downgrade(cfg, "012")` and assert with a fresh `create_engine(url)`.

**Engine-drift re-pin command** (run before the final commit of any `src/kyc_tool/**`-touching task; paste the printed digest into `EXPECTED_ENGINE_SOURCE_HASH`):
```bash
python - <<'PY'
import hashlib, pathlib
root = pathlib.Path("src/kyc_tool"); h = hashlib.sha256()
for p in sorted(root.rglob("*.py")):
    if "__pycache__" in p.parts:
        continue
    d = p.read_bytes()
    h.update(p.relative_to(root).as_posix().encode() + b"\x00" + str(len(d)).encode() + b"\x00" + d)
print(h.hexdigest())
PY
```

## Green-at-every-commit ordering (why the migration is built across tasks)

`conftest.migrated` upgrades a fresh DB to **head** for the whole suite, so any constraint migration `013` adds is live for every test. A migration constraint that *requires a column value* can only land once the code that *writes* that value is in place, and columns must exist before code writes them. Therefore:

- **Task 1** adds `013`'s columns + the outbox stream/case_id/lifecycle CHECKs (none reference `decision_sequence`) + updates the enqueue funcs to set `ordering_stream` — so the suite stays green.
- **Task 2** ships the shared parity matrix (`src/kyc_tool/ops/backfill_parity.py`, run by BOTH the migration and the Task-7 CLI) and the legacy `decision_sequence` backfill (touches only legacy rows; no new-row CHECK yet).
- **Task 3** fences the claim + terminals (the `claim_token` is set by the claim and cleared by every terminal in the same step — they cannot split without violating the lifecycle CHECK).
- **Task 4** makes the pipeline allocate `decision_sequence` **and**, in the same step, adds the decision-identity constraints (kind/stream identity CHECK, `manual`/`automatic` CHECK, the three decisions uniques, the triple FK, the partial callback unique) — now both legacy (backfilled) and live (allocated) rows satisfy them.
- **Task 5** adds the local guard + `superseded` lifecycle wiring.
- **Task 6** hardens `013`'s downgrade (LOCK + `superseded` preflight).
- **Tasks 7–8** ship the two ops CLIs; **Task 9** ships docs + the ROADMAP flip.

Each task edits `alembic/versions/013_outbox_stream_separation.py` at clearly marked insertion points; every test run re-applies `013` from scratch on a fresh DB, so cross-task edits to one migration file are safe.

## File Structure

**Create:**
- `alembic/versions/013_outbox_stream_separation.py` — the single migration (`down_revision='012'`): all columns, backfills, constraints, index, and the race-safe downgrade. Authored across Tasks 1, 2, 4, 6.
- `src/kyc_tool/ops/backfill_parity.py` — the shared schema-012 parity matrix (`PARITY_CHECKS` + `run_parity`) run by BOTH migration 013's preflight and the diagnostic CLI (Task 2).
- `src/kyc_tool/ops/verify_pr7b_core_backfill.py` — schema-012-compatible, `SHARE`-locked, read-only pre-window diagnostic CLI (Task 7).
- `src/kyc_tool/ops/reset_interrupted_outbox_claims.py` — post-013-only claim-tuple reset CLI (Task 8).
- `tests/integration/test_outbox_fencing.py` — stream-scoped fenced claim + fenced terminals (Task 3).
- `tests/integration/test_decision_sequence.py` — allocation + concurrency + manual-approve (Task 4).
- `tests/integration/test_outbox_supersession.py` — local guard, residual-risk, retention prune, metrics, UI-409, A6 (Task 5).
- `tests/integration/test_verify_pr7b_core_backfill.py` — the diagnostic CLI (Task 7).
- `tests/integration/test_reset_interrupted_outbox_claims.py` — the reset CLI + rollout order (Task 8).

**Modify:**
- `src/kyc_tool/db/tables.py` — `Outbox` (+`ordering_stream`, `decision_sequence`, `resolved_at`, `claim_lease_expires_at`, `claim_token`, `claimed_by`; `case_id` non-optional; `status` comment; `__table_args__`), `Case` (+`last_decision_sequence`), `DecisionRow` (+`decision_sequence`, `__table_args__` uniques). (Tasks 1, 4.)
- `src/kyc_tool/outbox/publisher.py` — `_CLAIM_SQL` (fenced, per-stream, `RETURNING` token/stream/sequence), `enqueue_decision_callback`/`enqueue_poc_email` (set `ordering_stream`, `decision_sequence`), `process_once` (thread token + guard), `_record_delivered`/`_record_failure` (fenced winner/loser), `_record_superseded` (new), module docstring (A6). (Tasks 1, 3, 5.)
- `src/kyc_tool/orchestration/pipeline.py` — `_decide_txn` allocates `decision_sequence` from `cases.last_decision_sequence` and passes it to `enqueue_decision_callback`. (Task 4.)
- `src/kyc_tool/workers/retention.py` — `prune` deletes `superseded` alongside `delivered`. (Task 5.)
- `src/kyc_tool/api/routes_metrics.py` — report `superseded` separately; keep it out of pending/dead alerting. (Task 5.)
- `tests/integration/test_migrations.py` — all `013` migration up/down/negative/backfill/downgrade-race tests (Tasks 1, 2, 4, 6).
- `tests/policy_driven/test_engine_build_id_guard.py` — re-pin `EXPECTED_ENGINE_SOURCE_HASH` (every src-touching task).
- `docs/OVERVIEW.md`, `docs/RUNBOOK.md`, `docs/DEPLOYMENT.md`, `AUDIT_FINDINGS.md`, `.agents/ROADMAP.md` — docs + governance (Task 9).

**Do NOT touch** (confirm unchanged): `src/kyc_tool/events/ingest.py` `_handle_manual_approve` (it already writes `run_id=None`, `manual=True`, no callback, and no `decision_sequence` — Task 4 only adds a test proving it allocates none); `src/kyc_tool/ui/routes.py` `requeue_outbox` (already 409s every non-`dead` row incl. `superseded` — Task 5 only adds a confirming test).

---

## Task 1: Migration 013 part A — outbox stream separation, `case_id NOT NULL`, lifecycle + claim CHECKs, ORM, enqueue `ordering_stream`

Adds every `013` column, backfills `ordering_stream` from `kind`, makes `case_id`/`ordering_stream` real `NOT NULL` columns, and lands the outbox CHECKs that do **not** reference `decision_sequence` (closed `kind` vocab, `ordering_stream` vocab, exhaustive per-status lifecycle + claim-tuple). The enqueue funcs set `ordering_stream` so the live suite stays green under the new `NOT NULL`. The decision-identity constraints and the `decision_sequence` backfill are deliberately deferred (Tasks 2, 4).

**Files:**
- Create: `alembic/versions/013_outbox_stream_separation.py`
- Modify: `src/kyc_tool/db/tables.py` (`Outbox` 261-279, `Case` 34-54, `DecisionRow` 211-227)
- Modify: `src/kyc_tool/outbox/publisher.py` (`enqueue_decision_callback`/`enqueue_poc_email` 52-63)
- Modify: `tests/integration/test_migrations.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `alembic` revision `013` (`down_revision='012'`) with columns `outbox.{ordering_stream TEXT NOT NULL, decision_sequence BIGINT NULL, resolved_at TIMESTAMPTZ NULL, claim_lease_expires_at TIMESTAMPTZ NULL, claim_token UUID NULL, claimed_by TEXT NULL}`, `outbox.case_id` now `NOT NULL` + FK `fk_outbox_case_id`, `decisions.decision_sequence BIGINT NULL`, `cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`; constraints `ck_outbox_kind_vocab`, `ck_outbox_ordering_stream_vocab`, `ck_outbox_status_lifecycle`; index `ix_outbox_stream_claim`.
- Produces: `enqueue_decision_callback(session, *, case_id, run_id, body)` and `enqueue_poc_email(session, *, case_id, to, subject, body)` now set `ordering_stream` (`'decision'`/`'email'`).
- Consumes: `tests/integration/test_migrations.py` helpers `_fresh_db`, `_config`.

- [ ] **Step 1: Write the failing test** — append to `tests/integration/test_migrations.py`:

```python
# --- PR 7b-core: migration 013 (outbox stream separation + local decision ordering) ---

def _seed_legacy_callback(conn, *, case_id, run_id, decision_id, ev_seq, status="pending", decided_at=None):
    """Seed one valid case→event→run→automatic-decision→decision_callback chain at schema
    012 (no ordering_stream / decision_sequence columns yet), the callback in `status`
    (pending | delivered | dead). outbox.id order is enqueue order; decided_at defaults to
    txn-start now() unless pinned. Runs inside the caller's transaction. Returns outbox id."""
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES (:e,:c,:k,'h','kyb.run_requested',"
            "'{}'::jsonb,'{}'::jsonb,:s)"
        ),
        {"e": run_id + "-ev", "c": case_id, "k": run_id, "s": ev_seq},
    )
    conn.execute(
        text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
        {"r": run_id, "c": case_id, "e": run_id + "-ev"},
    )
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
            "policy_shas, manual, decided_at) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,'enabled',"
            "'{}'::jsonb,false, COALESCE(:t, now()))"
        ),
        {"d": decision_id, "c": case_id, "r": run_id, "t": decided_at},
    )
    # a delivered callback carries delivered_at; pending/dead do not (012 has no lifecycle CHECK yet,
    # but seed the FINAL-schema-valid shape so later tasks' constraints stay green on this fixture).
    da = "now()" if status == "delivered" else "NULL"
    oid = conn.execute(
        text(
            f"INSERT INTO outbox (kind, case_id, run_id, payload_json, status, delivered_at) "
            f"VALUES ('decision_callback',:c,:r,'{{}}'::jsonb,:st,{da}) RETURNING id"
        ),
        {"c": case_id, "r": run_id, "st": status},
    ).scalar_one()
    return oid


def test_013_upgrade_sets_stream_and_notnull_metadata(pg):
    """Upgrade over the FULL legacy shape: pending + delivered(timestamped) + dead callbacks,
    a poc_email, and a manual decision — all must survive the SET NOT NULL + CHECKs."""
    url = _fresh_db(pg, "kyc_mig_013_streams")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_legacy_callback(conn, case_id="c1", run_id="r1", decision_id="d1", ev_seq=1, status="pending")
        _seed_legacy_callback(conn, case_id="c1", run_id="r2", decision_id="d2", ev_seq=2, status="delivered")
        _seed_legacy_callback(conn, case_id="c1", run_id="r3", decision_id="d3", ev_seq=3, status="dead")
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status) "
                          "VALUES ('poc_email','c1','{\"to\":\"a@b\"}'::jsonb,'pending')"))
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                          "buy_enablement, policy_shas, manual) VALUES ('dm','c1',NULL,'approve',0,'{}'::jsonb,"
                          "'enabled','{}'::jsonb,true)"))

    alembic_command.upgrade(cfg, "013")

    with engine.connect() as conn:
        streams = {
            r.kind: r.ordering_stream
            for r in conn.execute(text("SELECT kind, ordering_stream FROM outbox"))
        }
        assert streams == {"decision_callback": "decision", "poc_email": "email"}
        # real SET NOT NULL — the column metadata, not merely a value CHECK (F4)
        meta = {
            r.column_name: r.is_nullable
            for r in conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name='outbox' AND column_name IN ('ordering_stream','case_id')"
                )
            )
        }
        assert meta == {"ordering_stream": "NO", "case_id": "NO"}
        assert conn.execute(
            text("SELECT last_decision_sequence FROM cases WHERE id='c1'")
        ).scalar_one() == 0  # Task 1 leaves the default 0; Task 2's backfill Step changes this to 3
    engine.dispose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_migrations.py::test_013_upgrade_sets_stream_and_notnull_metadata -v`
Expected: FAIL — `alembic upgrade 013` errors with `Can't locate revision '013'` (the migration file does not exist yet).

- [ ] **Step 3: Write the migration (columns + stream/case_id/lifecycle + downgrade)** — create `alembic/versions/013_outbox_stream_separation.py`:

```python
"""outbox stream separation + local decision ordering (PR 7b-core)

Revision ID: 013
Revises: 012

Part A (this file, built across PR 7b-core tasks): adds ordering_stream (real
NOT NULL), the fenced-claim columns, case_id NOT NULL + FK, decisions.decision_sequence,
cases.last_decision_sequence, the closed kind/stream vocab CHECKs, and the exhaustive
per-status lifecycle + claim-tuple CHECK. The legacy decision_sequence backfill
(Task 2), the decision-identity constraints (Task 4), and the race-safe downgrade
(Task 6) are added at the marked insertion points.

Drained cutover: writers are stopped, so the plain SET NOT NULL scan is safe and no
CONCURRENTLY is needed. A DB default for ordering_stream is deliberately avoided — it
would mislabel emails; the value is derived from kind in two explicit branches.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

_LIFECYCLE_CHECK = """
    status IN ('pending','delivered','dead','superseded')
    AND (status <> 'pending' OR (
          delivered_at IS NULL AND resolved_at IS NULL AND (
            (claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL)
            OR (claim_token IS NOT NULL AND claim_lease_expires_at IS NOT NULL AND claimed_by IS NOT NULL))))
    AND (status <> 'delivered' OR (
          delivered_at IS NOT NULL AND resolved_at IS NULL
          AND claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL))
    AND (status <> 'dead' OR (
          delivered_at IS NULL AND resolved_at IS NULL
          AND claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL))
    AND (status <> 'superseded' OR (
          delivered_at IS NULL AND resolved_at IS NOT NULL
          AND claim_token IS NULL AND claim_lease_expires_at IS NULL AND claimed_by IS NULL))
"""


def upgrade() -> None:
    conn = op.get_bind()

    # === Task 2 insertion point A: shared parity preflight (runs BEFORE any DDL) ===

    # --- columns (all additive/nullable first) ---
    op.add_column("outbox", sa.Column("ordering_stream", sa.Text(), nullable=True))
    op.add_column("outbox", sa.Column("decision_sequence", sa.BigInteger(), nullable=True))
    op.add_column("outbox", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox", sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox", sa.Column("claim_token", postgresql.UUID(), nullable=True))
    op.add_column("outbox", sa.Column("claimed_by", sa.Text(), nullable=True))
    op.add_column("decisions", sa.Column("decision_sequence", sa.BigInteger(), nullable=True))
    op.add_column(
        "cases",
        sa.Column("last_decision_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )

    # --- ordering_stream backfill: two explicit branches, no DB default ---
    op.execute(
        """
        UPDATE outbox SET ordering_stream = CASE
            WHEN kind='poc_email' THEN 'email'
            WHEN kind='decision_callback' THEN 'decision'
            ELSE NULL END
        """
    )
    unmapped = conn.execute(
        sa.text("SELECT id, kind FROM outbox WHERE ordering_stream IS NULL")
    ).fetchall()
    if unmapped:
        raise RuntimeError(
            "migration 013: outbox rows with unmappable kind (cannot assign ordering_stream): "
            f"{[(r.id, r.kind) for r in unmapped]}"
        )

    # --- case_id NOT NULL + FK (both enqueue APIs require a case; removes the claim bypass) ---
    orphan_case = conn.execute(
        sa.text("SELECT id FROM outbox WHERE case_id IS NULL OR case_id NOT IN (SELECT id FROM cases)")
    ).fetchall()
    if orphan_case:
        raise RuntimeError(
            f"migration 013: outbox rows with NULL/orphan case_id: {[r.id for r in orphan_case]}"
        )
    op.alter_column("outbox", "case_id", existing_type=sa.Text(), nullable=False)
    op.create_foreign_key("fk_outbox_case_id", "outbox", "cases", ["case_id"], ["id"])

    # --- ordering_stream real SET NOT NULL + permanent vocabulary CHECK ---
    op.alter_column("outbox", "ordering_stream", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_outbox_ordering_stream_vocab", "outbox", "ordering_stream IN ('decision','email')"
    )

    # --- closed kind vocabulary CHECK (an unknown kind otherwise reaches _deliver's raise) ---
    op.create_check_constraint(
        "ck_outbox_kind_vocab", "outbox", "kind IN ('decision_callback','poc_email')"
    )

    # --- exhaustive per-status lifecycle + claim-tuple CHECK ---
    op.create_check_constraint("ck_outbox_status_lifecycle", "outbox", _LIFECYCLE_CHECK)

    # === Task 2 insertion point: legacy decision_sequence backfill ===

    # === Task 4 insertion point: decision-identity constraints ===

    # --- stream claim index ---
    op.create_index(
        "ix_outbox_stream_claim", "outbox",
        ["case_id", "ordering_stream", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    # Task 6 replaces this body with a LOCK TABLE + superseded preflight before the drops.
    op.drop_index("ix_outbox_stream_claim", table_name="outbox")
    # === Task 4 insertion point: drop decision-identity constraints (reverse order) ===
    op.drop_constraint("ck_outbox_status_lifecycle", "outbox", type_="check")
    op.drop_constraint("ck_outbox_kind_vocab", "outbox", type_="check")
    op.drop_constraint("ck_outbox_ordering_stream_vocab", "outbox", type_="check")
    op.drop_constraint("fk_outbox_case_id", "outbox", type_="foreignkey")
    op.alter_column("outbox", "case_id", existing_type=sa.Text(), nullable=True)
    op.drop_column("cases", "last_decision_sequence")
    op.drop_column("decisions", "decision_sequence")
    op.drop_column("outbox", "claimed_by")
    op.drop_column("outbox", "claim_token")
    op.drop_column("outbox", "claim_lease_expires_at")
    op.drop_column("outbox", "resolved_at")
    op.drop_column("outbox", "decision_sequence")
    op.drop_column("outbox", "ordering_stream")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_migrations.py::test_013_upgrade_sets_stream_and_notnull_metadata -v`
Expected: PASS.

- [ ] **Step 5: Write the up/down/up + INSERT/UPDATE lifecycle-negative tests** — append to `tests/integration/test_migrations.py`:

```python
def test_013_up_down_up_clean_no_supersession(pg):
    url = _fresh_db(pg, "kyc_mig_013_roundtrip")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    alembic_command.downgrade(cfg, "012")
    alembic_command.upgrade(cfg, "013")  # clean re-upgrade on a no-superseded DB


# INSERT negatives: each row is otherwise valid; only the constrained column is bad.
_OUTBOX_LIFECYCLE_BAD = {
    "unknown_kind": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('unknown','c1','decision','pending')",  # ck_outbox_kind_vocab
    "null_stream": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1',NULL,'pending')",  # NOT NULL
    "unknown_stream": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','carrier-pigeon','pending')",  # ck_outbox_ordering_stream_vocab
    "orphan_case": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','nope','email','pending')",  # fk_outbox_case_id
    "null_case": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email',NULL,'email','pending')",  # NOT NULL
    "pending_delivered_at": "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at) "
        "VALUES ('poc_email','c1','email','pending', now())",  # lifecycle: pending⇒delivered_at NULL
    "pending_resolved_at": "INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
        "VALUES ('poc_email','c1','email','pending', now())",  # lifecycle: pending⇒resolved_at NULL
    "delivered_no_delivered_at": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','email','delivered')",  # lifecycle: delivered⇒delivered_at NOT NULL
    "dead_with_claim": "INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
        "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','dead', gen_random_uuid(), "
        "now(), 'w1')",  # lifecycle: dead⇒claim all-NULL
    "orphan_claimed_by": "INSERT INTO outbox (kind, case_id, ordering_stream, status, claimed_by) "
        "VALUES ('poc_email','c1','email','pending','w1')",  # lifecycle: partial claim tuple
    "superseded_null_resolved": "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','email','superseded')",  # lifecycle: superseded⇒resolved_at NOT NULL
    "delivered_with_claim": "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at, "
        "claim_token, claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','delivered', "
        "now(), gen_random_uuid(), now(), 'w1')",  # lifecycle: delivered⇒claim all-NULL
}


@pytest.mark.parametrize("case", list(_OUTBOX_LIFECYCLE_BAD))
def test_013_outbox_lifecycle_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_013_neg_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(_OUTBOX_LIFECYCLE_BAD[case]))
    engine.dispose()


def test_013_outbox_lifecycle_update_negative(pg):
    """A live pending row cannot be UPDATEd into an illegal (superseded, NULL resolved_at)
    tuple — the lifecycle CHECK fires at commit on UPDATE too, not only INSERT."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_upd_neg")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(
            text(
                "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                "VALUES ('poc_email','c1','email','pending')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET status='superseded' WHERE case_id='c1'"))
    engine.dispose()
```

- [ ] **Step 6: Run the negatives**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_up_down_up or 013_outbox_lifecycle" -v`
Expected: PASS (all parametrizations).

- [ ] **Step 7: Mutation check (manual, no commit)** — temporarily delete `op.create_check_constraint("ck_outbox_status_lifecycle", ...)` from the migration, rerun `.venv/bin/pytest tests/integration/test_migrations.py -k 013_outbox_lifecycle -v`; confirm the `pending_delivered_at`/`dead_with_claim`/`superseded_null_resolved`/`delivered_with_claim` cases now FAIL (no `IntegrityError`). Restore the line. Do the same for `ck_outbox_kind_vocab` (→ `unknown_kind` fails) and the `SET NOT NULL` on `ordering_stream` (→ `is_nullable` metadata assertion fails).

- [ ] **Step 8: Update the ORM + enqueue funcs** — in `src/kyc_tool/db/tables.py`, add to `Case` (after `event_sequence`, line 45):

```python
    # PR 7b-core (migration 013): per-case decision-callback sequence counter,
    # incremented under the Case FOR UPDATE lock in _decide_txn (never max()+1).
    last_decision_sequence: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0"), default=0
    )
```

Add to `DecisionRow` (after `published_at`, line 225):

```python
    # PR 7b-core (migration 013): internal per-case ordinal for callback-emitting
    # (automatic) decisions; NULL for manual approvals. NOT on the wire.
    decision_sequence: Mapped[int | None] = mapped_column(BigInteger)
```

Replace the `Outbox` class body columns + `__table_args__` (lines 267-279) with:

```python
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text)  # decision_callback | poc_email
    case_id: Mapped[str] = mapped_column(Text, index=True)  # NOT NULL (migration 013)
    run_id: Mapped[str | None] = mapped_column(Text)
    # PR 7b-core: FIFO stream this row is claimed under (decision | email), NOT NULL.
    ordering_stream: Mapped[str] = mapped_column(Text)
    # Internal per-case ordinal for decision_callback rows (NULL for poc_email).
    decision_sequence: Mapped[int | None] = mapped_column(BigInteger)
    payload_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="pending")  # pending|delivered|dead|superseded
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # superseded terminal timestamp (never sent); mutually exclusive with delivered_at.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Per-claim fence: the three move together (all-NULL or all-non-NULL).
    claim_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    claimed_by: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        Index("ix_outbox_claim", "status", "next_attempt_at"),
        Index("ix_outbox_stream_claim", "case_id", "ordering_stream", "status", "next_attempt_at"),
    )
```

Add the `UUID` import to the postgresql-dialect import (line 22):

```python
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
```

In `src/kyc_tool/outbox/publisher.py`, update the enqueue funcs (lines 52-63):

```python
def enqueue_decision_callback(session: Session, *, case_id: str, run_id: str, body: dict) -> None:
    session.add(
        Outbox(
            kind=DECISION_CALLBACK,
            case_id=case_id,
            run_id=run_id,
            payload_json=body,
            ordering_stream="decision",
        )
    )


def enqueue_poc_email(session: Session, *, case_id: str, to: str, subject: str, body: str) -> None:
    session.add(
        Outbox(
            kind=POC_EMAIL,
            case_id=case_id,
            payload_json={"to": to, "subject": subject, "body": body},
            ordering_stream="email",
        )
    )
```

- [ ] **Step 9: RED drift-guard step (deliberately red)** — the src edits (ORM + enqueue) changed the source tree, so the whole-source guard now fails. Observe it:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v`
Expected: **FAIL** (`src/kyc_tool changed`). This is the RED step — do NOT run `./manage.sh test` yet (it would return nonzero on this same guard).

- [ ] **Step 10: Re-pin the engine drift guard → GREEN**

Run the re-pin one-liner (see Test infrastructure), paste the digest into `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py`, then:
Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → PASS.

- [ ] **Step 11: Full gate (now that the guard is re-pinned) + commit** — canonical close-out order:

```bash
./manage.sh test                       # whole suite, exit 0 (live decide enqueues ordering_stream='decision')
.venv/bin/ruff check .                 # exit 0
.venv/bin/lint-imports                 # 2 kept / 0 broken
git add alembic/versions/013_outbox_stream_separation.py src/kyc_tool/db/tables.py \
        src/kyc_tool/outbox/publisher.py tests/integration/test_migrations.py \
        tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(013): outbox stream separation, case_id NOT NULL, lifecycle+claim CHECKs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 2: Migration 013 part B — shared parity matrix + legacy `decision_sequence` backfill (order authority = `outbox.id`)

Creates the schema-012-compatible **shared parity matrix** (`src/kyc_tool/ops/backfill_parity.py`) that BOTH migration `013`'s preflight AND the `verify_pr7b_core_backfill` diagnostic (Task 7) run, then wires it into the migration: refuse (with actionable ids, `BLOCKED_NO_AUTHORITATIVE_MAPPING` on a missing mapping) BEFORE any DDL, else rank per case by `outbox.id`, stamp `decisions.decision_sequence` + copy to the callback, seed `cases.last_decision_sequence`. **Touches `src/` (the parity module) → re-pins the drift guard.**

**Files:**
- Create: `src/kyc_tool/ops/backfill_parity.py` (shared, raw-SQL parity matrix — no 013-only ORM)
- Modify: `alembic/versions/013_outbox_stream_separation.py` (insertion points A + B in `upgrade`)
- Modify: `tests/integration/test_migrations.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `PARITY_CHECKS: list[tuple[str, str]]` (name, schema-012 SQL selecting offending `a`,`b` id pairs); `run_parity(executor) -> list[tuple[str, list[tuple]]]` (checks with offenders); constants `MISSING_CALLBACK`, `BLOCKED_SENTINEL="BLOCKED_NO_AUTHORITATIVE_MAPPING"`. `run_parity` accepts anything with `.execute(text(...))` (an alembic `Connection` OR a `Session`).
- Produces: `_PARITY_BAD_SEEDS: dict[str, str]` in `test_migrations.py` (one invalid legacy state per key), imported by Task 7's CLI test.
- Consumes: schema from Task 1 (columns present, no decision-identity constraints yet).

- [ ] **Step 1: Create the shared parity matrix** — create `src/kyc_tool/ops/backfill_parity.py`:

```python
"""Shared schema-012-compatible parity matrix for the 7b-core backfill. Migration 013's
preflight AND the verify_pr7b_core_backfill diagnostic (ops CLI) both run THIS matrix, so
the two can never diverge. Raw SQL only — references NO 013-only columns (ordering_stream,
decision_sequence, claim_*). Each check SELECTs offending id pairs (a, b); empty ⇒ clean.
MISSING_CALLBACK is the fail-closed no-authoritative-mapping state (a decision whose callback
was pruned): the safe order cannot be reconstructed, so it maps to BLOCKED_NO_AUTHORITATIVE_MAPPING.
"""

from sqlalchemy import text

MISSING_CALLBACK = "missing_callback"
BLOCKED_SENTINEL = "BLOCKED_NO_AUTHORITATIVE_MAPPING"

PARITY_CHECKS: list[tuple[str, str]] = [
    (MISSING_CALLBACK,
     "SELECT d.id AS a, d.run_id AS b FROM decisions d WHERE d.manual=false AND NOT EXISTS "
     "(SELECT 1 FROM outbox o WHERE o.kind='decision_callback' AND o.run_id=d.run_id)"),
    ("orphan_callback",
     "SELECT o.id AS a, o.run_id AS b FROM outbox o WHERE o.kind='decision_callback' AND NOT EXISTS "
     "(SELECT 1 FROM decisions d WHERE d.manual=false AND d.run_id=o.run_id)"),
    ("null_run_callback",
     "SELECT o.id AS a, NULL AS b FROM outbox o WHERE o.kind='decision_callback' AND o.run_id IS NULL"),
    ("duplicate_callback_per_run",
     "SELECT run_id AS a, count(*) AS b FROM outbox WHERE kind='decision_callback' "
     "GROUP BY run_id HAVING count(*)>1"),
    ("duplicate_auto_decision_per_run",
     "SELECT run_id AS a, count(*) AS b FROM decisions WHERE manual=false AND run_id IS NOT NULL "
     "GROUP BY run_id HAVING count(*)>1"),
    ("null_or_orphan_outbox_case",
     "SELECT o.id AS a, o.case_id AS b FROM outbox o WHERE o.case_id IS NULL "
     "OR o.case_id NOT IN (SELECT id FROM cases)"),
    ("callback_case_ne_decision_case",
     "SELECT o.id AS a, o.run_id AS b FROM outbox o JOIN decisions d ON d.run_id=o.run_id AND d.manual=false "
     "WHERE o.kind='decision_callback' AND o.case_id <> d.case_id"),
    ("decision_case_ne_run_case",
     "SELECT d.id AS a, d.run_id AS b FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.case_id <> r.case_id"),
    ("unknown_kind",
     "SELECT id AS a, kind AS b FROM outbox WHERE kind NOT IN ('decision_callback','poc_email')"),
    ("poc_row_with_run",
     "SELECT id AS a, run_id AS b FROM outbox WHERE kind='poc_email' AND run_id IS NOT NULL"),
    ("manual_decision_with_run",
     "SELECT id AS a, run_id AS b FROM decisions WHERE manual=true AND run_id IS NOT NULL"),
    ("auto_decision_null_run",
     "SELECT id AS a, NULL AS b FROM decisions WHERE manual=false AND run_id IS NULL"),
]


def run_parity(executor) -> list[tuple[str, list[tuple]]]:
    """Return [(check_name, [(a, b), ...]), ...] for every check that found offenders.
    `executor` is any object with `.execute(text(sql))` — an alembic Connection or a Session."""
    found: list[tuple[str, list[tuple]]] = []
    for name, sql in PARITY_CHECKS:
        rows = executor.execute(text(sql)).fetchall()
        if rows:
            found.append((name, [(r.a, r.b) for r in rows]))
    return found
```

- [ ] **Step 2: Write the failing tests** — append to `tests/integration/test_migrations.py`:

```python
import json  # noqa: E402  (top-of-file import in the real edit)
import threading  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402
from kyc_tool.db.session import make_engine as _mk_engine  # noqa: E402
from kyc_tool.db.session import make_session_factory as _mk_sf  # noqa: E402
from kyc_tool.outbox.publisher import OutboxPublisher  # noqa: E402


def test_013_backfill_orders_by_outbox_id_and_delivers_without_false_supersession(pg, settings):
    """Threaded inversion (rev-4 F1): B's txn starts FIRST and fixes its decided_at via
    SELECT now(); A then runs fully and enqueues its callback FIRST (lower outbox.id) with a
    LATER decided_at; only then is B released to enqueue (higher outbox.id). So
    B.decided_at < A.decided_at while A.outbox_id < B.outbox_id. The backfill must rank by
    outbox.id (A=seq 1, B=seq 2); delivering both via the REAL publisher then sends A→B, both
    delivered, neither superseded, B last. Mutation ORDER BY d.decided_at reverses assignment
    and (at head, with the guard) supersedes B — failing this complete test."""
    url = _fresh_db(pg, "kyc_mig_013_inversion")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    eng = create_engine(url)
    with eng.begin() as conn:  # commit the case FIRST (both chains reference it)
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))

    clock_fixed = threading.Event()
    a_done = threading.Event()

    def run_b():
        cb = create_engine(url).connect()
        tx = cb.begin()
        cb.execute(text("SELECT now()"))  # fixes B's txn-start now() (B.decided_at) EARLY
        clock_fixed.set()
        a_done.wait(timeout=10)  # let A fully commit first (A gets the lower outbox.id)
        cb.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                        "actor_json, payload_json, event_sequence) VALUES ('evB','c1','rB','h','x',"
                        "'{}'::jsonb,'{}'::jsonb,2)"))
        cb.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                        "VALUES ('rB','c1','evB','PUBLISH_DECISION')"))
        cb.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                        "buy_enablement, policy_shas, manual) VALUES ('dB','c1','rB','approve',10,"
                        "'{}'::jsonb,'enabled','{}'::jsonb,false)"))
        cb.execute(text("INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                        "VALUES ('decision_callback','c1','rB', CAST('{\"run_id\":\"rB\"}' AS jsonb),'pending')"))
        tx.commit()
        cb.close()

    tb = threading.Thread(target=run_b)
    tb.start()
    clock_fixed.wait(timeout=10)
    time.sleep(0.1)  # ensure A's txn-start now() is strictly LATER than B's fixed clock
    with eng.begin() as connA:  # A runs fully + commits → lower outbox.id, later decided_at
        connA.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                           "actor_json, payload_json, event_sequence) VALUES ('evA','c1','rA','h','x',"
                           "'{}'::jsonb,'{}'::jsonb,1)"))
        connA.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                           "VALUES ('rA','c1','evA','PUBLISH_DECISION')"))
        connA.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                           "buy_enablement, policy_shas, manual) VALUES ('dA','c1','rA','approve',10,"
                           "'{}'::jsonb,'enabled','{}'::jsonb,false)"))
        connA.execute(text("INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                           "VALUES ('decision_callback','c1','rA', CAST('{\"run_id\":\"rA\"}' AS jsonb),'pending')"))
    a_done.set()
    tb.join(timeout=10)

    with eng.connect() as conn:
        a_dt, a_oid = conn.execute(text("SELECT d.decided_at, o.id FROM decisions d "
                                        "JOIN outbox o ON o.run_id=d.run_id WHERE d.id='dA'")).one()
        b_dt, b_oid = conn.execute(text("SELECT d.decided_at, o.id FROM decisions d "
                                        "JOIN outbox o ON o.run_id=d.run_id WHERE d.id='dB'")).one()
    assert b_dt < a_dt and a_oid < b_oid  # the inversion is real and deterministic

    alembic_command.upgrade(cfg, "013")

    with eng.connect() as conn:
        seqs = {r.id: r.decision_sequence
                for r in conn.execute(text("SELECT id, decision_sequence FROM decisions WHERE case_id='c1'"))}
        assert seqs == {"dA": 1, "dB": 2}  # by outbox.id, NOT decided_at
        assert conn.execute(text("SELECT last_decision_sequence FROM cases WHERE id='c1'")).scalar_one() == 2

    # deliver both via the REAL publisher against this dedicated DB; assert order + terminals
    order: list[str] = []

    def handler(request):
        order.append(json.loads(request.content).get("run_id"))
        return httpx.Response(200)

    pub = OutboxPublisher(_mk_sf(_mk_engine(url)), settings,
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert pub.process_pending() == 2
    assert order == ["rA", "rB"]  # A (seq 1) delivered before B (seq 2)
    with eng.connect() as conn:
        statuses = dict(conn.execute(text(
            "SELECT run_id, status FROM outbox WHERE case_id='c1' ORDER BY decision_sequence")).all())
    assert statuses == {"rA": "delivered", "rB": "delivered"}  # neither superseded; B last
    eng.dispose()


# One invalid legacy state per parity check (schema 012). Imported by Task 7's CLI test so the
# CLI and the migration are proven to refuse on the SAME matrix. Each is a complete INSERT set.
_BAD_CASE = "INSERT INTO cases (id) VALUES ('c1')"
_BAD_EV = ("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
           "payload_json, event_sequence) VALUES (:e,'c1',:e,'h','x','{}'::jsonb,'{}'::jsonb,:s)")
_BAD_RUN = "INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES (:r,'c1',:e,'PUBLISH_DECISION')"

_PARITY_BAD_SEEDS: dict[str, list[str]] = {
    "missing_callback": [_BAD_CASE, _BAD_EV, _BAD_RUN,  # decision, no callback
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"],
    "orphan_callback": [_BAD_CASE,  # callback whose run has no automatic decision
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1','ghost','{}'::jsonb,'pending')"],
    "duplicate_callback_per_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',:r,'{}'::jsonb,'pending')",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',:r,'{}'::jsonb,'pending')"],
    "duplicate_auto_decision_per_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d1','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d2','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',:r,'{}'::jsonb,'pending')"],
    "null_run_callback": [_BAD_CASE,  # callback with run_id NULL (also orphan — either name refuses)
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',NULL,'{}'::jsonb,'pending')"],
    "null_or_orphan_outbox_case": [_BAD_CASE,  # poc_email with case_id NULL
        "INSERT INTO outbox (kind, case_id, payload_json, status) VALUES ('poc_email',NULL,'{}'::jsonb,'pending')"],
    "callback_case_ne_decision_case": [_BAD_CASE, "INSERT INTO cases (id) VALUES ('c2')", _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "  # callback on c2, decision on c1
        "VALUES ('decision_callback','c2',:r,'{}'::jsonb,'pending')"],
    "decision_case_ne_run_case": [_BAD_CASE, "INSERT INTO cases (id) VALUES ('c2')", _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "  # run on c1, decision on c2
        "policy_shas, manual) VALUES ('d','c2',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c2',:r,'{}'::jsonb,'pending')"],
    "unknown_kind": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status) VALUES ('weird','c1','{}'::jsonb,'pending')"],
    "poc_row_with_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('poc_email','c1',:r,'{}'::jsonb,'pending')"],
    "manual_decision_with_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true)"],
    "auto_decision_null_run": [_BAD_CASE,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,false)"],
}


def _seed_parity_bad(engine, name):
    with engine.begin() as conn:
        for stmt in _PARITY_BAD_SEEDS[name]:
            conn.execute(text(stmt), {"e": "ev", "r": "r", "s": 1})


def test_013_backfill_refuses_missing_callback_byte_stable(pg):
    """A valid manual=false decision with NO surviving callback → fail closed with the exact
    BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel + ids (no decided_at fallback)."""
    url = _fresh_db(pg, "kyc_mig_013_missing_cb")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, "missing_callback")
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:  # migration raises RuntimeError; alembic propagates it
        alembic_command.upgrade(cfg, "013")
    msg = str(exc.value)
    assert "BLOCKED_NO_AUTHORITATIVE_MAPPING" in msg and "'d'" in msg  # sentinel + actionable id


@pytest.mark.parametrize("name", list(_PARITY_BAD_SEEDS))
def test_013_upgrade_refuses_parity_violation_before_ddl(pg, name):
    """Every invalid legacy state makes the 013 upgrade refuse BEFORE any DDL — the parity
    preflight runs first, so after the failure the 013 columns are absent (txn rolled back)."""
    url = _fresh_db(pg, f"kyc_mig_013_parity_{name}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, name)
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:
        alembic_command.upgrade(cfg, "013")
    assert name in str(exc.value) or "BLOCKED_NO_AUTHORITATIVE_MAPPING" in str(exc.value)
    engine = create_engine(url)
    with engine.connect() as conn:
        cols = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='outbox'"))}
    assert "claim_token" not in cols  # no DDL landed — refusal was before the column adds
    engine.dispose()
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_backfill or 013_upgrade_refuses_parity" -v`
Expected: FAIL — the inversion test sees `dA=None, dB=None`; the refusal tests do NOT raise (no parity preflight wired yet).

- [ ] **Step 4: Wire the parity preflight + the assignment into the migration** — in `alembic/versions/013_outbox_stream_separation.py`:

Replace `# === Task 2 insertion point A: shared parity preflight (runs BEFORE any DDL) ===` with:

```python
    # --- shared parity preflight (fail-closed BEFORE any DDL) ---
    from kyc_tool.ops.backfill_parity import BLOCKED_SENTINEL, MISSING_CALLBACK, run_parity

    violations = run_parity(conn)
    if violations:
        names = {n for n, _ in violations}
        detail = "; ".join(f"{n}: {ids}" for n, ids in violations)
        if MISSING_CALLBACK in names:
            raise RuntimeError(
                f"migration 013: {BLOCKED_SENTINEL} — automatic decision(s) without a surviving "
                f"decision_callback; restore from authoritative backup or remain on 012. {detail}"
            )
        raise RuntimeError(f"migration 013: backfill parity violation(s), fail-closed: {detail}")
```

Replace `# === Task 2 insertion point: legacy decision_sequence backfill ===` with (the mapping is now guaranteed 1:1 by the parity preflight above, so this is pure assignment):

```python
    # --- legacy decision_sequence assignment; ORDER AUTHORITY = outbox.id (rev-4 F1) ---
    # decided_at is txn-start now() and _decide_txn opens its txn before the case FOR UPDATE,
    # so two same-case decides can invert decided_at vs their lock-serialized commit order.
    # outbox.id is allocated at enqueue UNDER that lock, so it preserves the true serialization.
    op.execute(
        """
        WITH seq AS (
            SELECT d.id AS decision_id,
                   row_number() OVER (PARTITION BY d.case_id ORDER BY o.id) AS s
            FROM decisions d
            JOIN outbox o ON o.kind='decision_callback' AND o.run_id = d.run_id
            WHERE d.manual = false
        )
        UPDATE decisions d SET decision_sequence = seq.s FROM seq WHERE d.id = seq.decision_id
        """
    )
    op.execute(
        """
        UPDATE outbox o SET decision_sequence = d.decision_sequence
        FROM decisions d
        WHERE o.kind='decision_callback' AND o.run_id = d.run_id AND d.manual = false
        """
    )
    op.execute(
        """
        UPDATE cases c SET last_decision_sequence = COALESCE(
            (SELECT max(d.decision_sequence) FROM decisions d
             WHERE d.case_id = c.id AND d.decision_sequence IS NOT NULL), 0)
        """
    )
    post_dup = conn.execute(
        sa.text(
            "SELECT case_id, decision_sequence FROM decisions WHERE decision_sequence IS NOT NULL "
            "GROUP BY case_id, decision_sequence HAVING count(*) > 1"
        )
    ).fetchall()
    if post_dup:  # defensive: row_number() cannot produce a per-case duplicate
        raise RuntimeError(f"migration 013: post-backfill per-case sequence collision: {post_dup}")
```

- [ ] **Step 5: Run to verify pass + fix the Task-1 counter assertion**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_backfill or 013_upgrade_refuses_parity or 013_upgrade_sets_stream" -v`
Then, in `test_013_upgrade_sets_stream_and_notnull_metadata`, change the final counter assertion from `== 0` to `== 3` (case `c1` now has three backfilled automatic decisions — pending/delivered/dead) and rerun that test → PASS.

- [ ] **Step 6: Mutation checks (manual, no commit)** — (a) change the assignment `ORDER BY o.id` → `ORDER BY d.decided_at, o.id`; rerun `test_013_backfill_orders_by_outbox_id_and_delivers_without_false_supersession` → confirm it FAILS at `seqs == {"dA":1,"dB":2}` (and, at final head with the Task-5 guard, also at the delivery order / no-supersession assertions). Restore. (b) Delete the `run_parity` block; rerun `test_013_upgrade_refuses_parity_violation_before_ddl` → confirm every parametrization FAILS (no refusal). Restore.

- [ ] **Step 7: RED drift-guard step** — the new `src/kyc_tool/ops/backfill_parity.py` changed the source tree:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step; do NOT run `./manage.sh test` yet).

- [ ] **Step 8: Re-pin the drift guard → GREEN** — run the re-pin one-liner, paste into `EXPECTED_ENGINE_SOURCE_HASH`, rerun the guard test → PASS.

- [ ] **Step 9: Full gate + commit** (canonical close-out order)

```bash
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add src/kyc_tool/ops/backfill_parity.py alembic/versions/013_outbox_stream_separation.py \
        tests/integration/test_migrations.py tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(013): shared parity matrix + decision_sequence backfill ranked by outbox.id

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 3: Stream-scoped fenced claim + fenced terminals (winner/loser)

Rewrites `_CLAIM_SQL` to claim the min-id pending row of one `(case_id, ordering_stream)` under a fresh `claim_token`+lease (leaving `next_attempt_at` as the retry due time, removing the `case_id IS NULL` bypass), threads the token through `process_once`, and makes `_record_delivered`/`_record_failure` fenced: the winner (`applied=true`) does all dependent writes in-transaction; the stale loser (`applied=false`) does nothing and emits `outbox_stale_claim_completion`, non-raising. This is the single ownership gate that closes defect 3 and lets a stuck stream never block the other.

**Files:**
- Modify: `src/kyc_tool/outbox/publisher.py` (`_CLAIM_SQL` 29-49, `enqueue_decision_callback` 52-53, `OutboxPublisher.__init__` 67-78, `process_once` 143-158, `_record_delivered` 160-191, `_record_failure` 193-226)
- Create: `tests/integration/test_outbox_fencing.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `enqueue_decision_callback` from Task 1 (adds a `decision_sequence` kwarg here, default `None`).
- Produces: `_CLAIM_SQL` `RETURNING id, kind, case_id, run_id, payload_json, attempts, ordering_stream, decision_sequence, claim_token`; `process_once()` passes `token = row.claim_token` to every terminal; `_record_delivered(self, row, token)`, `_record_failure(self, row, error, token)` each begin with the fenced `UPDATE … WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id` and branch on `applied`; stale losers log `outbox_stale_claim_completion` (fields: `outbox_id`, `attempted`).

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_outbox_fencing.py`:

```python
"""PR 7b-core: stream-scoped fenced claim + fenced terminals (defect 3)."""

import httpx
import pytest
import structlog
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def _seed_case(session_factory, case_id="c1"):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.commit()


def _enqueue_email(session_factory, *, case_id, to, status="pending", next_at="now()"):
    with session_factory() as s:
        s.execute(
            text(
                f"INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status, next_attempt_at) "
                f"VALUES ('poc_email',:c,'email',CAST(:p AS jsonb),:st,{next_at})"
            ),
            {"c": case_id, "p": '{"to":"%s","subject":"s","body":"b"}' % to, "st": status},
        )
        s.commit()


def _pub_for(session_factory, settings):
    """A publisher bound to `session_factory` whose HTTP + email always succeed (200)."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    return OutboxPublisher(
        session_factory, settings,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def test_stuck_email_does_not_block_decision_callback(session_factory, settings, publisher, callback_capture):
    """A perpetually-'pending' POC email (future backoff) in the email stream must NOT
    block the same case's decision callback in the decision stream."""
    _seed_case(session_factory, "c1")
    # a stuck email far in the future (its stream is busy/backed-off)
    _enqueue_email(session_factory, case_id="c1", to="stuck@x", next_at="now() + interval '1 hour'")
    # a due decision callback for the same case — needs a real automatic decision for the triple FK
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    with session_factory() as s:
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
                "payload_json, event_sequence) VALUES ('ev1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r1','c1','ev1','PUBLISH_DECISION')"))
        s.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                "policy_shas, manual, decision_sequence) VALUES ('d1','c1','r1','approve',10,'{}'::jsonb,"
                "'enabled','{}'::jsonb,false,1)"
            )
        )
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"case_id": "c1"}, decision_sequence=1)
        s.commit()

    assert publisher.process_pending() == 1  # the callback delivers; the stuck email is skipped
    assert len(callback_capture.requests) == 1
    with session_factory() as s:
        statuses = dict(
            s.execute(text("SELECT kind, status FROM outbox WHERE case_id='c1' ORDER BY kind")).all()
        )
    assert statuses == {"decision_callback": "delivered", "poc_email": "pending"}


def _claim(session_factory, claimed_by):
    """Claim ONE row via the real _CLAIM_SQL; return the returned Row (carries the token)."""
    from kyc_tool.outbox.publisher import _CLAIM_SQL

    with session_factory() as s:
        row = s.execute(_CLAIM_SQL, {"lease_seconds": 3600, "claimed_by": claimed_by}).first()
        s.commit()
    return row


def _expire_lease(session_factory, oid):
    with session_factory() as s:
        s.execute(text("UPDATE outbox SET claim_lease_expires_at = now() - interval '1 second' WHERE id=:i"), {"i": oid})
        s.commit()


def test_stale_poc_loser_touches_nothing_before_reclaimer_sends(session_factory, settings, publisher):
    """Defect 3, POC: A claims (token A); A's lease expires; B RECLAIMS (token B) but does NOT
    terminalize yet. A resuming with token A — through BOTH stale success and stale FINAL-attempt
    failure — must touch nothing: payload byte-identical, status pending, B's complete claim tuple
    + attempts + next_attempt_at unchanged, and it emits only outbox_stale_claim_completion (no
    raise). THEN B delivers and redacts exactly once."""
    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="keep@x")
    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")  # B reclaims; does NOT terminalize
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    with session_factory() as s:
        before = s.execute(text("SELECT payload_json::text AS p, status, claim_token, claim_lease_expires_at, "
                                "claimed_by, attempts, next_attempt_at FROM outbox WHERE id=:i"), {"i": rowA.id}).one()

    s2 = settings.model_copy(update={"outbox_max_attempts": 1})  # A's failure would be terminal (dead)
    stale_pub = _pub_for(session_factory, s2)
    with structlog.testing.capture_logs() as logs:
        stale_pub._record_delivered(rowA, rowA.claim_token)          # stale success → no-op
        stale_pub._record_failure(rowA, "boom", rowA.claim_token)    # stale final-attempt failure → no-op
    assert [e for e in logs if e["event"] == "outbox_stale_claim_completion"]  # audited no-op, no raise

    with session_factory() as s:
        after = s.execute(text("SELECT payload_json::text AS p, status, claim_token, claim_lease_expires_at, "
                               "claimed_by, attempts, next_attempt_at FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert after == before  # A changed NOTHING — payload, status, B's whole claim tuple, attempts, clock

    publisher._record_delivered(rowB, rowB.claim_token)  # B (the real winner) delivers + redacts once
    with session_factory() as s:
        final = s.execute(text("SELECT status, payload_json FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert final.status == "delivered" and final.payload_json == {"redacted": True}


def test_stale_decision_loser_cannot_stamp_run_or_published_at(session_factory, settings, publisher, callback_capture):
    """Defect 3, decision callback: same A/B ordering. Stale A must change neither the outbox
    tuple nor runs.state (stays PUBLISH_DECISION) nor decisions.published_at (stays NULL). Then B
    alone stamps both (run COMPLETE, published_at set)."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_case(session_factory, "c1")
    with session_factory() as s:
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
                       "payload_json, event_sequence) VALUES ('ev1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r1','c1','ev1','PUBLISH_DECISION')"))
        s.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                       "policy_shas, manual, decision_sequence) VALUES ('d1','c1','r1','approve',10,'{}'::jsonb,"
                       "'enabled','{}'::jsonb,false,1)"))
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"run_id": "r1"}, decision_sequence=1)
        s.commit()

    rowA = _claim(session_factory, "A")
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")

    stale_pub = _pub_for(session_factory, settings.model_copy(update={"outbox_max_attempts": 1}))
    with structlog.testing.capture_logs() as logs:
        stale_pub._record_delivered(rowA, rowA.claim_token)
        stale_pub._record_failure(rowA, "boom", rowA.claim_token)
    assert [e for e in logs if e["event"] == "outbox_stale_claim_completion"]

    with session_factory() as s:
        run_state = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one()
        ob = s.execute(text("SELECT status, claim_token FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert run_state == "PUBLISH_DECISION" and pub_at is None  # A stamped NOTHING
    assert ob.status == "pending" and ob.claim_token == rowB.claim_token  # still B's

    publisher._record_delivered(rowB, rowB.claim_token)  # B alone stamps both
    with session_factory() as s:
        assert s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one() == "COMPLETE"
        assert s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one() is not None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v`
Expected: FAIL — `_CLAIM_SQL` binds `:claimed_by` which the current SQL does not accept; `enqueue_decision_callback` has no `decision_sequence` kwarg; `_record_delivered`/`_record_failure` take no `token`.

- [ ] **Step 3: Rewrite the claim + enqueue + terminals** — in `src/kyc_tool/outbox/publisher.py`:

Replace `_CLAIM_SQL` (lines 29-49) with:

```python
# Claim the min-id pending row of ONE (case_id, ordering_stream) FIFO stream, fencing it
# with a fresh claim_token + lease. next_attempt_at is left as the retry due time (the
# lease is separate). case_id is NOT NULL (013), so there is no case_id IS NULL bypass.
_CLAIM_SQL = text(
    """
    UPDATE outbox
    SET claim_token = gen_random_uuid(),
        claim_lease_expires_at = now() + make_interval(secs => :lease_seconds),
        claimed_by = :claimed_by
    WHERE id = (
        SELECT o.id FROM outbox o
        WHERE o.status = 'pending' AND o.next_attempt_at <= now()
          AND (o.claim_token IS NULL OR o.claim_lease_expires_at <= now())
          AND o.id = (
                SELECT min(o2.id) FROM outbox o2
                WHERE o2.case_id = o.case_id
                  AND o2.ordering_stream = o.ordering_stream
                  AND o2.status = 'pending'
            )
        ORDER BY o.id
        FOR UPDATE OF o SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, kind, case_id, run_id, payload_json, attempts, ordering_stream,
              decision_sequence, claim_token
    """
)
```

Change `enqueue_decision_callback` (lines 52-53) to accept `decision_sequence`:

```python
def enqueue_decision_callback(
    session: Session, *, case_id: str, run_id: str, body: dict, decision_sequence: int | None = None
) -> None:
    session.add(
        Outbox(
            kind=DECISION_CALLBACK,
            case_id=case_id,
            run_id=run_id,
            payload_json=body,
            ordering_stream="decision",
            decision_sequence=decision_sequence,
        )
    )
```

Add `import os` and `import socket` to the imports (near line 9-11), and set the claimant in `__init__` (after line 78 `self.email_sender = ...`):

```python
        self._claimant = f"{socket.gethostname()}:{os.getpid()}"
```

Replace `process_once` (lines 143-158) with (the guard block is added in Task 5 — leave the marker):

```python
    def process_once(self) -> bool:
        """Claim and deliver one pending row of one (case, stream). Returns False when idle."""
        lease = self.settings.outbox_backoff_base_seconds * (2 ** (self.settings.outbox_max_attempts - 1))
        with uow(self.session_factory) as session:
            row = session.execute(
                _CLAIM_SQL, {"lease_seconds": min(lease, 3600), "claimed_by": self._claimant}
            ).first()
        if row is None:
            return False
        token = row.claim_token

        # === Task 5 insertion point: best-effort local superseded guard ===

        try:
            self._deliver(row.kind, row.payload_json)
        except Exception as exc:  # noqa: BLE001 — a failed delivery must never kill the loop
            self._record_failure(row, str(exc), token)
            return True

        self._record_delivered(row, token)
        return True
```

Replace `_record_delivered` (lines 160-191) with:

```python
    def _record_delivered(self, row, token) -> None:
        now = datetime.now(UTC)
        with uow(self.session_factory) as session:
            applied = session.execute(
                text(
                    "UPDATE outbox SET status='delivered', delivered_at=:now, "
                    "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                    "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                ),
                {"id": row.id, "now": now, "token": token},
            ).first()
            if applied is None:
                # stale loser: its lease expired and a reclaimer already finished this row.
                log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="delivered")
                return
            if row.kind == POC_EMAIL:
                # the raw POC token existed only to be emailed; don't retain it at rest.
                session.execute(
                    text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:id"),
                    {"id": row.id, "p": json.dumps({"redacted": True})},
                )
            if row.kind == DECISION_CALLBACK and row.run_id:
                session.execute(
                    text(
                        "UPDATE runs SET state='COMPLETE', finished_at=:now "
                        "WHERE id=:run_id AND state='PUBLISH_DECISION'"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
                session.execute(
                    text(
                        "UPDATE decisions SET published_at=:now "
                        "WHERE run_id=:run_id AND published_at IS NULL"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
        log.info("outbox_delivered", outbox_id=row.id, kind=row.kind, case_id=row.case_id)
```

Replace `_record_failure` (lines 193-226) with:

```python
    def _record_failure(self, row, error: str, token) -> None:
        attempts = row.attempts + 1
        dead = attempts >= self.settings.outbox_max_attempts
        delay = self.settings.outbox_backoff_base_seconds * (2 ** (attempts - 1))
        with uow(self.session_factory) as session:
            if dead:
                applied = session.execute(
                    text(
                        "UPDATE outbox SET status='dead', attempts=:a, last_error=:e, "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                    ),
                    {"id": row.id, "a": attempts, "e": error[:2000], "token": token},
                ).first()
                if applied is None:
                    log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="dead")
                    return
                if row.kind == POC_EMAIL:
                    session.execute(
                        text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:id"),
                        {"id": row.id, "p": json.dumps({"redacted": True})},
                    )
            else:
                applied = session.execute(
                    text(
                        "UPDATE outbox SET attempts=:a, last_error=:e, "
                        "next_attempt_at = now() + make_interval(secs => :delay), "
                        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                        "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                    ),
                    {"id": row.id, "a": attempts, "e": error[:2000], "delay": delay, "token": token},
                ).first()
                if applied is None:
                    log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="retry")
                    return
        if dead:
            log.error("outbox_dead_letter", outbox_id=row.id, kind=row.kind, error=error)
        else:
            log.warning("outbox_retry", outbox_id=row.id, kind=row.kind, attempts=attempts)
```

- [ ] **Step 4: Run the fencing tests**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v`
Expected: PASS.

- [ ] **Step 5: Run all fencing tests + the two NAMED mutation witnesses (manual, no commit)**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v` → PASS (stuck-email + both stale POC/decision A/B tests).
**Mutation (a) — remove the loser early-return:** in `_record_delivered` delete the `if applied is None: … return` branch; rerun `.venv/bin/pytest tests/integration/test_outbox_fencing.py::test_stale_poc_loser_touches_nothing_before_reclaimer_sends -v` → it must FAIL (stale A's delivered UPDATE with a non-matching token now writes / raises on the `RETURNING`-less path). Restore.
**Mutation (b) — raise on zero rows:** change every terminal's `if applied is None: log.warning(...); return` to `if applied is None: raise RuntimeError("stale")`; rerun `test_stale_decision_loser_cannot_stamp_run_or_published_at` → it must FAIL (the raise escapes `_record_failure`/`_record_delivered` instead of a non-raising audited no-op). Restore.

- [ ] **Step 6: RED drift-guard step** — publisher.py changed:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step; do NOT run `./manage.sh test` yet).

- [ ] **Step 7: Re-pin the drift guard → GREEN** — run the re-pin one-liner, paste into `EXPECTED_ENGINE_SOURCE_HASH`, rerun the guard test → PASS.

- [ ] **Step 8: Full gate + commit** (canonical close-out order)

```bash
./manage.sh test          # whole suite green (delivery still stamps published_at + run COMPLETE)
.venv/bin/ruff check .
.venv/bin/lint-imports
git add src/kyc_tool/outbox/publisher.py tests/integration/test_outbox_fencing.py \
        tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(outbox): stream-scoped fenced claim + fenced winner/loser terminals

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 4: Decision-sequence allocation + decision-identity constraints

Makes `_decide_txn` increment `cases.last_decision_sequence` under the already-held Case `FOR UPDATE`, stamp `DecisionRow.decision_sequence`, and pass it to `enqueue_decision_callback`. In the **same** commit, adds `013`'s decision-identity constraints (the kind/stream identity CHECK, the `manual`/`automatic` CHECK, the three decisions uniques, the triple FK, the partial callback unique) — now both legacy (backfilled) and live (allocated) rows satisfy them, so the suite stays green.

**Files:**
- Modify: `src/kyc_tool/orchestration/pipeline.py` (`_decide_txn` 485-506; the Case is FOR UPDATE from `_load` at 362)
- Modify: `src/kyc_tool/db/tables.py` (`DecisionRow` — add `__table_args__`; `Outbox.__table_args__`)
- Modify: `alembic/versions/013_outbox_stream_separation.py` (Task 4 insertion points in `upgrade` + `downgrade`)
- Modify: `tests/integration/test_migrations.py` (identity negatives)
- Create: `tests/integration/test_decision_sequence.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `enqueue_decision_callback(..., decision_sequence=)` (Task 3); `cases.last_decision_sequence` (Task 1).
- Produces: constraints `uq_decisions_run_id`, `uq_decisions_case_decision_sequence`, `uq_decisions_run_case_sequence`, `ck_decisions_manual_sequence`, `ck_outbox_kind_stream_identity`, `fk_outbox_decision_triple`, `uq_outbox_decision_callback_run`; every automatic decide stamps a strictly-increasing per-case `decision_sequence`; manual approve allocates none.

- [ ] **Step 1: Write the failing allocation/concurrency tests** — create `tests/integration/test_decision_sequence.py`:

```python
"""PR 7b-core: per-case decision_sequence allocation under the Case FOR UPDATE lock."""

import json
import threading

import pytest
from sqlalchemy import text

from kyc_tool.domain.models import RunState

pytestmark = pytest.mark.postgres


def _seed_run_at_decide(session_factory, *, case_id, run_id, ev_seq):
    """Seed a case + one run poised at DECIDE (the broker-blocked short-circuit path decides
    from DECIDE) + its running run_transition job. Returns the job id."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
                       "payload_json, event_sequence) VALUES (:e,:c,:k,'h','kyb.run_requested','{}'::jsonb,"
                       "CAST(:p AS jsonb),:sq)"),
                  {"e": run_id + "-ev", "c": case_id, "k": run_id,
                   "p": '{"company_legal_name":"X","jurisdiction":"GB"}', "sq": ev_seq})
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state, input_snapshot_json) "
                       "VALUES (:r,:c,:e,'DECIDE', CAST(:p AS jsonb))"),
                  {"r": run_id, "c": case_id, "e": run_id + "-ev",
                   "p": '{"company_legal_name":"X","jurisdiction":"GB"}'})
        jid = s.execute(text("INSERT INTO jobs (kind, case_id, payload_json, status) VALUES "
                             "('run_transition',:c, CAST(:p AS jsonb), 'running') RETURNING id"),
                        {"c": case_id, "p": json.dumps({"run_id": run_id})}).scalar_one()
        s.commit()
    return jid


def test_concurrent_decides_serialize_via_case_lock(session_factory, pipeline, policy, monkeypatch):
    """Two REAL _decide_txn calls on ONE case, released simultaneously by a barrier placed
    immediately before _load (which takes the Case FOR UPDATE). The real row lock serializes
    them → sequences {1,2}, counter 2, no uniqueness error. Mutation: dropping with_for_update
    (or max()+1 allocation) lets both read the same counter → duplicate seq → uq violation."""
    jid_a = _seed_run_at_decide(session_factory, case_id="cc", run_id="ra", ev_seq=1)
    jid_b = _seed_run_at_decide(session_factory, case_id="cc", run_id="rb", ev_seq=2)

    barrier = threading.Barrier(2)
    orig_load = pipeline._load

    def barriered_load(session, run_id):
        barrier.wait()  # both threads poised BEFORE either takes the Case FOR UPDATE
        return orig_load(session, run_id)

    monkeypatch.setattr(pipeline, "_load", barriered_load)

    errors: list[Exception] = []

    def decide(run_id, jid):
        try:
            pipeline._decide_txn(run_id, from_state=RunState.DECIDE, job_id=jid, bundle=policy)
        except Exception as e:  # noqa: BLE001 — capture a uniqueness race for the assertion
            errors.append(e)

    ta = threading.Thread(target=decide, args=("ra", jid_a))
    tb = threading.Thread(target=decide, args=("rb", jid_b))
    ta.start()
    tb.start()
    ta.join(timeout=15)
    tb.join(timeout=15)

    assert errors == []  # no uq_decisions_case_decision_sequence violation
    with session_factory() as s:
        seqs = sorted(r.decision_sequence for r in s.execute(text(
            "SELECT decision_sequence FROM decisions WHERE case_id='cc' AND manual=false")))
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='cc'")).scalar_one()
        cbs = sorted(r.decision_sequence for r in s.execute(text(
            "SELECT decision_sequence FROM outbox WHERE case_id='cc' AND kind='decision_callback'")))
    assert seqs == [1, 2] and counter == 2 and cbs == [1, 2]


def test_manual_approve_allocates_no_sequence(client, session_factory, post_event, worker, sign):
    post_event("case-man", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()
    # ReviewerManualApprovePayload requires reviewer_id; review_guard requires actor.id == reviewer_id.
    body = json.dumps({
        "event_type": "reviewer.manual_approve",
        "occurred_at": "2026-07-23T00:00:00Z",
        "actor": {"type": "reviewer", "id": "rev-1"},
        "payload": {"reviewer_id": "rev-1", "note": "ok"},
    }).encode()
    resp = client.post("/v1/cases/case-man/events", content=body, headers=sign(body))
    assert resp.status_code == 200  # accepted (no 422)

    with session_factory() as s:
        manual = s.execute(text("SELECT decision_sequence, run_id FROM decisions "
                                "WHERE case_id='case-man' AND manual=true")).one()
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='case-man'")).scalar_one()
    assert manual.decision_sequence is None and manual.run_id is None  # manual allocates nothing
    assert counter == 1  # only the one automatic decide bumped it; manual approve did not
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_decision_sequence.py -v`
Expected: FAIL — automatic decisions have `decision_sequence=None` (pipeline does not allocate yet), so the `seqs == [1, 2]` assertion fails.

- [ ] **Step 3: Allocate in `_decide_txn`** — in `src/kyc_tool/orchestration/pipeline.py`, immediately before the `decision_row = DecisionRow(` construction (line 485), insert:

```python
            # PR 7b-core: allocate this callback-emitting decision's per-case ordinal from
            # the LOCKED counter (never max()+1). The Case is FOR UPDATE from _load above,
            # so this single writer of the counter is race-free.
            case.last_decision_sequence = (case.last_decision_sequence or 0) + 1
            decision_sequence = case.last_decision_sequence
```

Add `decision_sequence=decision_sequence,` to the `DecisionRow(...)` kwargs (alongside `engine_build_id=ENGINE_BUILD_ID,` at line 493):

```python
                engine_build_id=ENGINE_BUILD_ID,
                decision_sequence=decision_sequence,
```

Change the `enqueue_decision_callback` call (line 506) to pass the sequence:

```python
            enqueue_decision_callback(
                session, case_id=case.id, run_id=run_id, body=body, decision_sequence=decision_sequence
            )
```

- [ ] **Step 4: Add the identity constraints to the migration** — in `alembic/versions/013_outbox_stream_separation.py`, replace the `# === Task 4 insertion point: decision-identity constraints ===` line in `upgrade()` with:

```python
    # --- decisions identity + per-case ordering (F1) ---
    # UNIQUE(run_id): one automatic decision per run (multiple NULL manual rows stay legal).
    op.create_unique_constraint("uq_decisions_run_id", "decisions", ["run_id"])
    # THE per-case ordering invariant (manual rows keep decision_sequence NULL → many NULLs legal).
    op.create_unique_constraint(
        "uq_decisions_case_decision_sequence", "decisions", ["case_id", "decision_sequence"]
    )
    # The exact triple-FK target for the callback binding below.
    op.create_unique_constraint(
        "uq_decisions_run_case_sequence", "decisions", ["run_id", "case_id", "decision_sequence"]
    )
    op.create_check_constraint(
        "ck_decisions_manual_sequence", "decisions",
        "(NOT (manual = false) OR (run_id IS NOT NULL AND decision_sequence > 0)) "
        "AND (NOT (manual = true) OR (run_id IS NULL AND decision_sequence IS NULL))",
    )
    # --- outbox callback binding: exhaustive-OR shape + triple FK + one callback per run ---
    op.create_check_constraint(
        "ck_outbox_kind_stream_identity", "outbox",
        "(kind='decision_callback' AND ordering_stream='decision' AND run_id IS NOT NULL "
        "AND decision_sequence > 0) OR (kind='poc_email' AND ordering_stream='email' "
        "AND run_id IS NULL AND decision_sequence IS NULL)",
    )
    op.create_foreign_key(
        "fk_outbox_decision_triple", "outbox", "decisions",
        ["run_id", "case_id", "decision_sequence"], ["run_id", "case_id", "decision_sequence"],
    )
    op.create_index(
        "uq_outbox_decision_callback_run", "outbox", ["run_id"], unique=True,
        postgresql_where=sa.text("kind='decision_callback'"),
    )
```

Replace the `# === Task 4 insertion point: drop decision-identity constraints (reverse order) ===` line in `downgrade()` with:

```python
    op.drop_index("uq_outbox_decision_callback_run", table_name="outbox")
    op.drop_constraint("fk_outbox_decision_triple", "outbox", type_="foreignkey")
    op.drop_constraint("ck_outbox_kind_stream_identity", "outbox", type_="check")
    op.drop_constraint("ck_decisions_manual_sequence", "decisions", type_="check")
    op.drop_constraint("uq_decisions_run_case_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_case_decision_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_run_id", "decisions", type_="unique")
```

- [ ] **Step 5: Update the ORM `__table_args__`** — in `src/kyc_tool/db/tables.py`, add the `ForeignKeyConstraint` import (line 10-21 import block):

```python
    ForeignKeyConstraint,
```

Add a `__table_args__` to `DecisionRow` (after `reviewer_id`, line 227):

```python
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_decisions_run_id"),
        UniqueConstraint("case_id", "decision_sequence", name="uq_decisions_case_decision_sequence"),
        UniqueConstraint("run_id", "case_id", "decision_sequence", name="uq_decisions_run_case_sequence"),
    )
```

Extend `Outbox.__table_args__` (from Task 1) to include the callback binding:

```python
    __table_args__ = (
        Index("ix_outbox_claim", "status", "next_attempt_at"),
        Index("ix_outbox_stream_claim", "case_id", "ordering_stream", "status", "next_attempt_at"),
        Index(
            "uq_outbox_decision_callback_run", "run_id", unique=True,
            postgresql_where=text("kind='decision_callback'"),
        ),
        ForeignKeyConstraint(
            ["run_id", "case_id", "decision_sequence"],
            ["decisions.run_id", "decisions.case_id", "decisions.decision_sequence"],
            name="fk_outbox_decision_triple",
        ),
    )
```

- [ ] **Step 6: Run allocation tests + the concurrency mutation witness (manual, no `./manage.sh test` yet)**

Run: `.venv/bin/pytest tests/integration/test_decision_sequence.py -v` → PASS.
**Concurrency mutation:** in `pipeline._load` remove `with_for_update=True` from `session.get(Case, run.case_id, with_for_update=True)` (or substitute a `max(decision_sequence)+1` allocation for the locked-counter increment); rerun `test_concurrent_decides_serialize_via_case_lock` → it must FAIL (both threads read the same counter → duplicate seq → `uq_decisions_case_decision_sequence` violation captured in `errors`, or `seqs == [1, 1]`). Restore.

- [ ] **Step 7: Add the full identity INSERT/UPDATE negative cross-product** — append to `tests/integration/test_migrations.py`:

```python
def _mk_case(conn, c="c1"):
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": c})


def _mk_auto_decision(conn, *, d, c, r, seq, ev_seq):
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
            "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"
        ),
        {"e": r + "-ev", "c": c, "k": r, "s": ev_seq},
    )
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
                 {"r": r, "c": c, "e": r + "-ev"})
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
            "policy_shas, manual, decision_sequence) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,'enabled',"
            "'{}'::jsonb,false,:seq)"
        ),
        {"d": d, "c": c, "r": r, "seq": seq},
    )


def test_013_two_runs_same_case_sequence_rejected(pg):
    """Backstopped by uq_decisions_case_decision_sequence — NOT the outbox partial index.
    Two distinct valid runs/decisions at the same (case_id, positive decision_sequence)
    must fail at commit; multiple manual NULL-sequence decisions stay legal."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_dupseq")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _mk_auto_decision(conn, d="dB", c="c1", r="rB", seq=1, ev_seq=2)  # same case+seq, other run
    with engine.begin() as conn:  # multiple manual NULL-sequence decisions remain legal
        for i in range(2):
            conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                              "buy_enablement, policy_shas, manual) VALUES (:d,'c1',NULL,'approve',0,'{}'::jsonb,"
                              "'enabled','{}'::jsonb,true)"), {"d": f"dm{i}"})
    engine.dispose()


def test_013_two_decisions_same_run_rejected(pg):
    """uq_decisions_run_id: at most one automatic decision per run."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_duprun")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError), engine.begin() as conn:  # same run_id, distinct seq/id
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                          "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
                          "('dB','c1','rA','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,2)"))
    engine.dispose()


# INSERT negatives on `decisions` — each names the constraint it targets.
_DECISION_IDENTITY_BAD = {
    "manual_with_run": ("ck_decisions_manual_sequence",  # manual row carrying a run_id
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true)"),
    "manual_with_seq": ("ck_decisions_manual_sequence",  # manual row carrying a decision_sequence
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,true,1)"),
    "auto_zero_seq": ("ck_decisions_manual_sequence",  # automatic row with zero sequence
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,false,0)"),
    "auto_negative_seq": ("ck_decisions_manual_sequence",  # automatic row with negative sequence
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,false,-1)"),
    "auto_null_run": ("ck_decisions_manual_sequence",  # automatic row with NULL run
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,false,1)"),
}


@pytest.mark.parametrize("case", list(_DECISION_IDENTITY_BAD))
def test_013_decision_identity_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_013_di_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                          "actor_json, payload_json, event_sequence) VALUES ('rX-ev','c1','rX','h','x',"
                          "'{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('rX','c1','rX-ev','PUBLISH_DECISION')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(_DECISION_IDENTITY_BAD[case][1]))
    engine.dispose()


def test_013_decision_identity_update_negative(pg):
    """UPDATE variant: a valid automatic decision cannot be UPDATEd into a zero sequence."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_di_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE decisions SET decision_sequence=0 WHERE id='dA'"))
    engine.dispose()


# INSERT negatives on `outbox` — each names the constraint it targets.
_OUTBOX_BINDING_BAD = {
    "callback_null_run": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1',NULL,'decision',1,'pending')"),
    "callback_zero_seq": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',0,'pending')"),
    "callback_negative_seq": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',-1,'pending')"),
    "email_sequenced": ("ck_outbox_kind_stream_identity",  # email carrying a sequence
        "INSERT INTO outbox (kind, case_id, ordering_stream, decision_sequence, status) "
        "VALUES ('poc_email','c1','email',1,'pending')"),
    "email_with_run": ("ck_outbox_kind_stream_identity",  # email carrying a run_id
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','rA','email','pending')"),
    "callback_stream_swap": ("ck_outbox_kind_stream_identity",  # callback on the email stream
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','email',1,'pending')"),
    "email_stream_swap": ("ck_outbox_kind_stream_identity",  # email on the decision stream
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','decision','pending')"),
    "callback_wrong_seq": ("fk_outbox_decision_triple",  # (rA,c1,2) — no matching decision
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',2,'pending')"),
    "callback_wrong_case": ("fk_outbox_decision_triple",  # (rA,c2,1) — decision is (rA,c1,1)
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c2','rA','decision',1,'pending')"),
    "callback_wrong_run": ("fk_outbox_decision_triple",  # (ghost,c1,1) — no such decision
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','ghost','decision',1,'pending')"),
    "dup_callback": ("uq_outbox_decision_callback_run",  # two callbacks for the same run
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',1,'pending'); "
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',1,'pending')"),
}


@pytest.mark.parametrize("case", list(_OUTBOX_BINDING_BAD))
def test_013_outbox_binding_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_013_ob_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn, "c1")
        _mk_case(conn, "c2")  # for callback_wrong_case (a valid, different case)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)  # (rA,c1,1) exists
    with pytest.raises(IntegrityError), engine.begin() as conn:
        for stmt in _OUTBOX_BINDING_BAD[case][1].split("; "):
            conn.execute(text(stmt))
    engine.dispose()


def test_013_outbox_binding_update_negative(pg):
    """UPDATE variant: a valid callback cannot be UPDATEd onto the wrong (run,case,seq) —
    the triple FK fires on UPDATE too."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_ob_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
                          "VALUES ('decision_callback','c1','rA','decision',1,'pending')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET decision_sequence=9 WHERE run_id='rA'"))  # (rA,c1,9) absent
    engine.dispose()
```

- [ ] **Step 8: Run negatives + per-constraint INDEPENDENT mutation witnesses (manual, no commit)**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_two_runs or 013_two_decisions or 013_decision_identity or 013_outbox_binding" -v` → PASS.
Mutate each constraint away INDEPENDENTLY and confirm the NAMED test fails, then restore:
- drop `uq_decisions_case_decision_sequence` → `test_013_two_runs_same_case_sequence_rejected` FAILS (proving the outbox partial index is NOT the backstop).
- drop `uq_decisions_run_id` → `test_013_two_decisions_same_run_rejected` FAILS.
- drop `ck_decisions_manual_sequence` → `test_013_decision_identity_insert_negatives[auto_zero_seq]` + `[manual_with_run]` FAIL.
- drop `ck_outbox_kind_stream_identity` → `test_013_outbox_binding_insert_negatives[callback_null_run]` + `[callback_stream_swap]` + `[email_with_run]` FAIL.
- drop `fk_outbox_decision_triple` → `test_013_outbox_binding_insert_negatives[callback_wrong_case]` + `test_013_outbox_binding_update_negative` FAIL.
- drop `uq_outbox_decision_callback_run` → `test_013_outbox_binding_insert_negatives[dup_callback]` FAILS.

- [ ] **Step 9: RED drift-guard step** — pipeline.py + tables.py changed:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step).

- [ ] **Step 10: Re-pin the drift guard → GREEN** — re-pin one-liner → paste → rerun guard test → PASS.

- [ ] **Step 11: Full gate + commit** (canonical close-out order)

```bash
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add src/kyc_tool/orchestration/pipeline.py src/kyc_tool/db/tables.py \
        alembic/versions/013_outbox_stream_separation.py tests/integration/test_migrations.py \
        tests/integration/test_decision_sequence.py tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(013): per-case decision_sequence allocation + decision-identity constraints

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 5: Best-effort local `superseded` guard + lifecycle wiring (retention, metrics, UI-409, A6)

Adds the local guard to `process_once` (suppress an older decision-stream callback only when a higher-sequence decision already has a locally-stamped `published_at`), the fenced `_record_superseded` terminal, retention pruning of `superseded` alongside `delivered`, separate metrics reporting, a UI-409 confirmation, the honest residual-risk (send-before-stamp) test, and the AUDIT:A6 amendment. The guard is explicitly a best-effort optimization — send-before-stamp and cross-replica reverts remain until 7b-activation.

**Files:**
- Modify: `src/kyc_tool/outbox/publisher.py` (`process_once` guard insertion point; add `_record_superseded`; module docstring 1-7)
- Modify: `src/kyc_tool/workers/retention.py` (`prune` 29-35)
- Modify: `src/kyc_tool/api/routes_metrics.py` (line 60)
- Modify: `AUDIT_FINDINGS.md` (A6, lines 61-66)
- Create: `tests/integration/test_outbox_supersession.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `_record_superseded(self, row, token)` reuses the fenced pattern from Task 3; the guard reads `row.ordering_stream`, `row.decision_sequence`, `row.case_id`.
- Produces: `_record_superseded` sets `status='superseded'`, `resolved_at`, clears the claim tuple, moves the run `PUBLISH_DECISION→COMPLETE`, leaves `published_at` NULL; logs `outbox_superseded`. `prune` returns key `outbox_superseded`. Metrics expose `outbox_by_status` including `superseded` but the pending/dead alert set excludes it.

- [ ] **Step 1: Write the failing guard tests** — create `tests/integration/test_outbox_supersession.py`:

```python
"""PR 7b-core: best-effort local superseded guard + A6 + honest residual risk."""

import pytest
from sqlalchemy import text

from kyc_tool.outbox.publisher import enqueue_decision_callback

pytestmark = pytest.mark.postgres


def _seed_decisions(session_factory, case_id, seqs):
    """Seed case + one automatic decision (+ run at PUBLISH_DECISION) per seq in `seqs`;
    published_at stays NULL. Run id = f'{case_id}-r{seq}', decision id = f'{case_id}-r{seq}-d'."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES (:c,:m) "
                       "ON CONFLICT (id) DO UPDATE SET last_decision_sequence=:m"),
                  {"c": case_id, "m": max(seqs)})
        for seq in seqs:
            r = f"{case_id}-r{seq}"
            s.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                           "actor_json, payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,"
                           "'{}'::jsonb,:s)"), {"e": r + "-ev", "c": case_id, "k": r, "s": seq})
            s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                           "VALUES (:r,:c,:e,'PUBLISH_DECISION')"), {"r": r, "c": case_id, "e": r + "-ev"})
            s.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                           "buy_enablement, policy_shas, manual, decision_sequence) VALUES (:d,:c,:r,'approve',"
                           "10,'{}'::jsonb,'enabled','{}'::jsonb,false,:seq)"),
                      {"d": r + "-d", "c": case_id, "r": r, "seq": seq})
        s.commit()


def _enqueue_cb(session_factory, case_id, seq):
    r = f"{case_id}-r{seq}"
    with session_factory() as s:
        enqueue_decision_callback(s, case_id=case_id, run_id=r, body={"run_id": r}, decision_sequence=seq)
        s.commit()


def test_higher_delivered_then_older_superseded_a6(session_factory, settings, publisher, callback_capture):
    """A6 (F6) + local guard: the HIGHER callback (seq 2) is really SENT (>=1 HTTP) and stamps
    published_at; the OLDER requeued callback (seq 1) is then claimed, SUPERSEDED with ZERO HTTP,
    its run reaches COMPLETE, published_at stays NULL, and an audit_log row records BOTH the
    superseded and the superseding sequence."""
    _seed_decisions(session_factory, "c1", [1, 2])
    _enqueue_cb(session_factory, "c1", 2)
    assert publisher.process_pending() == 1        # seq 2 delivered (HTTP #1) + stamped
    assert len(callback_capture.requests) == 1
    _enqueue_cb(session_factory, "c1", 1)          # the older requeued callback
    assert publisher.process_pending() == 1        # seq 1 claimed → guard fires → superseded
    assert len(callback_capture.requests) == 1     # ZERO additional HTTP for the lower callback

    with session_factory() as s:
        row = s.execute(text("SELECT status, resolved_at, delivered_at FROM outbox WHERE run_id='c1-r1'")).one()
        run = s.execute(text("SELECT state FROM runs WHERE id='c1-r1'")).scalar_one()
        pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='c1-r1-d'")).scalar_one()
        aud = s.execute(text("SELECT detail_json FROM audit_log WHERE action='outbox.superseded'")).fetchall()
    assert row.status == "superseded" and row.resolved_at is not None and row.delivered_at is None
    assert run == "COMPLETE" and pub_at is None
    assert aud and aud[0].detail_json["superseded_sequence"] == 1
    assert aud[0].detail_json["superseding_sequence"] == 2  # both sequences recorded (durable audit)


def test_normal_1_2_3_all_delivered_in_order(session_factory, settings, publisher, callback_capture):
    """seq 1→2→3 each delivered before the next → none superseded (the guard suppresses only
    a still-pending OLDER callback once a higher delivery has stamped)."""
    _seed_decisions(session_factory, "cn", [1, 2, 3])
    for seq in (1, 2, 3):
        _enqueue_cb(session_factory, "cn", seq)
    assert publisher.process_pending() == 3
    assert len(callback_capture.requests) == 3
    with session_factory() as s:
        statuses = [r.status for r in s.execute(text(
            "SELECT status FROM outbox WHERE case_id='cn' ORDER BY decision_sequence"))]
    assert statuses == ["delivered", "delivered", "delivered"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_outbox_supersession.py::test_higher_delivered_then_older_superseded_a6 -v`
Expected: FAIL — with no guard, seq 1 is delivered too (2 captured HTTP requests, `status == "delivered"`).

- [ ] **Step 3: Add the guard + `_record_superseded`** — in `src/kyc_tool/outbox/publisher.py`, replace the `# === Task 5 insertion point: best-effort local superseded guard ===` line in `process_once` with:

```python
        # Best-effort local superseded guard (NOT an authority): suppress this older
        # decision-stream callback only when a HIGHER-sequence decision for the case already
        # has a locally-stamped published_at. If the higher callback was sent but its
        # _record_delivered had not committed (send-before-stamp), or a concurrent replica
        # sent it, this predicate is false and the older callback IS sent — a revert that
        # remains expected until 7b-activation's platform high-water (§5).
        if row.ordering_stream == "decision" and row.decision_sequence is not None:
            with uow(self.session_factory) as session:
                superseded = session.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM decisions WHERE case_id=:c "
                        "AND decision_sequence > :seq AND published_at IS NOT NULL)"
                    ),
                    {"c": row.case_id, "seq": row.decision_sequence},
                ).scalar_one()
            if superseded:
                self._record_superseded(row, token)
                return True
```

Add `from kyc_tool.db.audit import audit` to the imports (near line 18 `from kyc_tool import security`), then add `_record_superseded` immediately after `_record_failure`:

```python
    def _record_superseded(self, row, token) -> None:
        """A higher locally-stamped delivery proved this callback obsolete: terminally
        suppress it (zero sends), fenced like every other terminal. published_at stays NULL
        (never sent); the run still reaches COMPLETE via the legal PUBLISH_DECISION edge. The
        durable audit records BOTH the superseded and the superseding sequence (A6 evidence)."""
        now = datetime.now(UTC)
        with uow(self.session_factory) as session:
            applied = session.execute(
                text(
                    "UPDATE outbox SET status='superseded', resolved_at=:now, "
                    "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                    "WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id"
                ),
                {"id": row.id, "now": now, "token": token},
            ).first()
            if applied is None:
                log.warning("outbox_stale_claim_completion", outbox_id=row.id, attempted="superseded")
                return
            superseding = session.execute(
                text(
                    "SELECT min(decision_sequence) FROM decisions WHERE case_id=:c "
                    "AND decision_sequence > :seq AND published_at IS NOT NULL"
                ),
                {"c": row.case_id, "seq": row.decision_sequence},
            ).scalar()
            if row.run_id:
                session.execute(
                    text(
                        "UPDATE runs SET state='COMPLETE', finished_at=:now "
                        "WHERE id=:run_id AND state='PUBLISH_DECISION'"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
            audit(
                session, "outbox.superseded", case_id=row.case_id,
                outbox_id=row.id, superseded_sequence=row.decision_sequence,
                superseding_sequence=superseding,
            )
        log.info(
            "outbox_superseded", outbox_id=row.id, case_id=row.case_id,
            superseded_sequence=row.decision_sequence, superseding_sequence=superseding,
        )
```

- [ ] **Step 4: Run the guard tests**

Run: `.venv/bin/pytest tests/integration/test_outbox_supersession.py -v` → PASS.

- [ ] **Step 5: Wire retention + metrics + add residual-risk, A6, retention, UI tests** — in `src/kyc_tool/workers/retention.py`, replace the `outbox_delivered` prune (lines 29-35) with:

```python
        counts["outbox_delivered"] = session.execute(
            text(
                "DELETE FROM outbox WHERE status='delivered' "
                "AND delivered_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
        # PR 7b-core: superseded is a terminal (never sent), pruned like delivered.
        counts["outbox_superseded"] = session.execute(
            text(
                "DELETE FROM outbox WHERE status='superseded' "
                "AND resolved_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
```

In `src/kyc_tool/api/routes_metrics.py`, keep the `outbox_by_status` grouping (line 60, it already surfaces every status incl. `superseded`) and add an explicit alert-set comment/derived field right after it:

```python
            "outbox_by_status": _grouped(session, "SELECT status, count(*) FROM outbox GROUP BY status"),
            # PR 7b-core: superseded is a governed terminal (best-effort local suppression,
            # zero sends) — reported above but EXCLUDED from the pending/dead alert set below.
            "outbox_alerting": _grouped(
                session,
                "SELECT status, count(*) FROM outbox WHERE status IN ('pending','dead') GROUP BY status",
            ),
```

Append to `tests/integration/test_outbox_supersession.py`:

```python
def test_residual_risk_send_before_stamp_reverts_expected(session_factory, settings, publisher, callback_capture):
    """Honest boundary (§5): seq 2 is really SENT (HTTP #1) and stamped, but then its
    published_at is made NOT locally visible (published_at→NULL) — the exact observable of
    send-before-stamp (the stamp txn not committed here) or a cross-replica send. The seq-1
    guard predicate is then false and seq 1 IS sent (HTTP #2) — the revert that stays EXPECTED
    until 7b-activation turns the same replay into a platform high-water no-op."""
    _seed_decisions(session_factory, "c3", [1, 2])
    _enqueue_cb(session_factory, "c3", 2)
    assert publisher.process_pending() == 1              # seq 2 SENT (HTTP #1) + stamped
    assert len(callback_capture.requests) == 1
    with session_factory() as s:                          # inject: the higher stamp is not locally visible
        s.execute(text("UPDATE decisions SET published_at=NULL WHERE id='c3-r2-d'"))
        s.commit()
    _enqueue_cb(session_factory, "c3", 1)
    assert publisher.process_pending() == 1              # seq 1 guard predicate false → SENT
    assert len(callback_capture.requests) == 2           # HTTP #2 — the documented, expected revert
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE run_id='c3-r1'")).scalar_one() == "delivered"


def test_retention_prunes_superseded(session_factory):
    from kyc_tool.workers.retention import prune

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c4')"))
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                       "VALUES ('poc_email','c4','email','superseded', now() - interval '3000 days')"))
        s.commit()
    counts = prune(session_factory, 7 * 365)
    assert counts["outbox_superseded"] == 1
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM outbox WHERE case_id='c4'")).scalar_one() == 0


def test_ui_requeue_409s_superseded(client, session_factory):
    # conftest `settings` leaves ui_admin_token empty → the console is open in tests
    # (dev/test trust model; see tests/unit/test_ops_auth.py + tests/integration/test_ui.py),
    # so no auth header is sent and a non-dead row must 409 (never requeued).
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c5')"))
        oid = s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                             "VALUES ('poc_email','c5','email','superseded', now()) RETURNING id")).scalar_one()
        s.commit()
    resp = client.post(f"/ui/api/requeue/outbox/{oid}")
    assert resp.status_code == 409  # superseded is not 'dead' → requeue_outbox rejects it


def test_metrics_reports_superseded_out_of_the_alert_set(client, session_factory):
    """superseded shows in outbox_by_status but is EXCLUDED from the pending/dead alert set."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cm')"))
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) VALUES ('poc_email','cm','email','pending')"))
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) VALUES ('poc_email','cm','email','dead')"))
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                       "VALUES ('poc_email','cm','email','superseded', now())"))
        s.commit()
    body = client.get("/v1/metrics").json()
    assert {"pending", "dead", "superseded"} <= set(body["outbox_by_status"])
    assert "superseded" not in body["outbox_alerting"]  # governed terminal, not an alert
    assert set(body["outbox_alerting"]) <= {"pending", "dead"}
```

- [ ] **Step 6: Amend AUDIT:A6 + docstring** — in `AUDIT_FINDINGS.md`, replace the A6 **Resolution** (lines 65-66) with:

```markdown
- **Resolution**: at-least-once delivery from a transactional outbox; consumers dedupe.
  No component claims exactly-once. **PR 7b-core exception:** eligible non-superseded
  callbacks remain at-least-once and platform-deduped; a callback proven obsolete by a
  higher **locally-stamped** delivery is terminally suppressed (zero sends), audited, and
  retained under the governed `superseded` lifecycle. This is a **best-effort local
  suppression** — NOT exactly-once and NOT platform-authoritative (send-before-stamp and
  cross-replica reverts remain until 7b-activation).
```

In `src/kyc_tool/outbox/publisher.py`, insert these lines into the existing module docstring, immediately before its closing `"""` (line 7):

```python
    PR 7b-core: rows are claimed per (case_id, ordering_stream) under a fenced claim_token;
    a decision callback proven obsolete by a higher locally-stamped delivery is terminally
    `superseded` (zero sends) — a best-effort LOCAL suppression, not exactly-once and not
    platform-authoritative (send-before-stamp / cross-replica reverts remain for 7b-activation).
```

- [ ] **Step 7: Run all supersession tests, then RED guard → re-pin → full gate → commit**

```bash
.venv/bin/pytest tests/integration/test_outbox_supersession.py -v     # all pass
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v # RED (src changed) — expected
# re-pin EXPECTED_ENGINE_SOURCE_HASH (one-liner), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v # GREEN
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add src/kyc_tool/outbox/publisher.py src/kyc_tool/workers/retention.py \
        src/kyc_tool/api/routes_metrics.py AUDIT_FINDINGS.md \
        tests/integration/test_outbox_supersession.py tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(outbox): best-effort local superseded guard + lifecycle wiring (A6 exception)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 6: Migration 013 downgrade race-safety (LOCK + `superseded` preflight)

Hardens `013`'s `downgrade()`: the **first** statement is `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE`, then an `EXISTS(status='superseded')` preflight that refuses byte-stably before any DROP. A bare preflight is a TOCTOU race (its `ACCESS SHARE` is compatible with a publisher's `ROW EXCLUSIVE`, so a `superseded` row could commit between the check and the DDL, stranding an unknown terminal a pre-7b image cannot prune). Migration-only — no drift re-pin.

**Files:**
- Modify: `alembic/versions/013_outbox_stream_separation.py` (`downgrade` body)
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: the downgrade drop sequence from Tasks 1+4.
- Produces: `downgrade()` refuses with the stable message substring `"superseded outbox row"` if any `superseded` row exists; otherwise drops cleanly under `ACCESS EXCLUSIVE`.

- [ ] **Step 1: Write the failing tests** — append to `tests/integration/test_migrations.py`:

```python
def test_013_downgrade_refuses_with_superseded_row(pg):
    """Separate real refusal test: a seeded superseded row makes the real alembic downgrade
    refuse byte-stably (RuntimeError with the stable message)."""
    url = _fresh_db(pg, "kyc_mig_013_down_refuse")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                          "VALUES ('poc_email','c1','email','superseded', now())"))
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:  # migration raises RuntimeError; alembic propagates it
        alembic_command.downgrade(cfg, "012")
    assert "superseded outbox row" in str(exc.value)


def test_013_downgrade_lock_prevents_concurrent_supersede(pg):
    """Drive the REAL migration downgrade() in a thread; pause it with a global
    after_cursor_execute barrier fired at its superseded preflight — which runs AFTER the
    production LOCK TABLE ... ACCESS EXCLUSIVE. A concurrent pending→superseded UPDATE must
    then FAIL with a lock timeout (SQLSTATE 55P03): it cannot slip between the preflight and
    the DDL. MUTATION: moving/removing the LOCK (so the preflight holds only ACCESS SHARE)
    lets that UPDATE commit — the pytest.raises(OperationalError) then fails."""
    import threading

    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import OperationalError

    url = _fresh_db(pg, "kyc_mig_013_down_lock")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','email','pending', now())"))

    at_preflight = threading.Event()
    release = threading.Event()
    _PREFLIGHT = "from outbox where status='superseded'"  # the downgrade's count(*) preflight

    def _barrier(conn, cursor, statement, params, context, executemany):
        if _PREFLIGHT in statement.lower():
            at_preflight.set()          # downgrade holds ACCESS EXCLUSIVE here
            release.wait(timeout=15)    # hold the downgrade txn BETWEEN preflight and DDL

    event.listen(Engine, "after_cursor_execute", _barrier)
    down_err: list[Exception] = []

    def run_downgrade():
        try:
            alembic_command.downgrade(cfg, "012")
        except Exception as e:  # noqa: BLE001
            down_err.append(e)

    t = threading.Thread(target=run_downgrade)
    t.start()
    try:
        assert at_preflight.wait(timeout=15)  # paused right after the preflight
        with create_engine(url).connect() as connB:
            connB.execute(text("SET lock_timeout='2s'"))
            with pytest.raises(OperationalError) as exc:  # blocked by ACCESS EXCLUSIVE
                connB.execute(text("UPDATE outbox SET status='superseded', resolved_at=now() "
                                   "WHERE case_id='c1'"))
            assert exc.value.orig.sqlstate == "55P03"  # lock_not_available
    finally:
        release.set()
        t.join(timeout=15)
        event.remove(Engine, "after_cursor_execute", _barrier)
    assert down_err == []  # the no-superseded downgrade completed cleanly after release
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_downgrade_refuses_with_superseded or 013_downgrade_lock" -v`
Expected: `test_013_downgrade_refuses_with_superseded_row` FAILS — the current (Task-1/4) downgrade has no preflight, so it does NOT raise. `test_013_downgrade_lock_prevents_concurrent_supersede` also FAILS — with no `LOCK TABLE` yet, the preflight holds only ACCESS SHARE, the concurrent UPDATE commits (no 55P03), and `pytest.raises(OperationalError)` is unsatisfied.

- [ ] **Step 3: Harden the downgrade** — in `alembic/versions/013_outbox_stream_separation.py`, replace the entire `downgrade()` function (the Task-1 body, into which Task 4 inserted the identity-constraint drops) with this complete final body — the only change is the new `LOCK TABLE` + `superseded` preflight prepended before the (unchanged) drops:

```python
def downgrade() -> None:
    # Reversible only BEFORE the first local supersession. Lock FIRST: a bare
    # EXISTS preflight is a TOCTOU race (ACCESS SHARE is compatible with a publisher's
    # ROW EXCLUSIVE, so a superseded row could commit between the check and the DDL,
    # stranding an unknown, unprunable terminal a pre-7b image cannot interpret).
    op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")
    stranded = op.get_bind().execute(
        sa.text("SELECT count(*) FROM outbox WHERE status='superseded'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            "migration 013 downgrade refused: superseded outbox row(s) exist — a pre-7b "
            "image cannot interpret or prune them (roll forward instead)"
        )
    op.drop_index("ix_outbox_stream_claim", table_name="outbox")
    # decision-identity constraints (Task 4), reverse dependency order
    op.drop_index("uq_outbox_decision_callback_run", table_name="outbox")
    op.drop_constraint("fk_outbox_decision_triple", "outbox", type_="foreignkey")
    op.drop_constraint("ck_outbox_kind_stream_identity", "outbox", type_="check")
    op.drop_constraint("ck_decisions_manual_sequence", "decisions", type_="check")
    op.drop_constraint("uq_decisions_run_case_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_case_decision_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_run_id", "decisions", type_="unique")
    # outbox stream / case_id / lifecycle (Task 1)
    op.drop_constraint("ck_outbox_status_lifecycle", "outbox", type_="check")
    op.drop_constraint("ck_outbox_kind_vocab", "outbox", type_="check")
    op.drop_constraint("ck_outbox_ordering_stream_vocab", "outbox", type_="check")
    op.drop_constraint("fk_outbox_case_id", "outbox", type_="foreignkey")
    op.alter_column("outbox", "case_id", existing_type=sa.Text(), nullable=True)
    op.drop_column("cases", "last_decision_sequence")
    op.drop_column("decisions", "decision_sequence")
    op.drop_column("outbox", "claimed_by")
    op.drop_column("outbox", "claim_token")
    op.drop_column("outbox", "claim_lease_expires_at")
    op.drop_column("outbox", "resolved_at")
    op.drop_column("outbox", "decision_sequence")
    op.drop_column("outbox", "ordering_stream")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_downgrade or 013_up_down_up" -v` → PASS (refuse test raises the stable `RuntimeError`; the lock test's concurrent UPDATE now hits 55P03; up/down/up clean still passes on a no-superseded DB).

- [ ] **Step 5: Mutation check (manual, no commit)** — move `op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")` to AFTER the `EXISTS` preflight (or delete it); rerun `.venv/bin/pytest tests/integration/test_migrations.py::test_013_downgrade_lock_prevents_concurrent_supersede -v` → it must FAIL (the preflight now holds only ACCESS SHARE, so the concurrent `pending→superseded` UPDATE commits — no 55P03 — and `pytest.raises(OperationalError)` is unsatisfied). Restore.

- [ ] **Step 6: Commit** (migration-only — no drift re-pin; `./manage.sh test` runs directly)

```bash
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add alembic/versions/013_outbox_stream_separation.py tests/integration/test_migrations.py
git commit -m "feat(013): race-safe downgrade (ACCESS EXCLUSIVE lock + superseded preflight)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 7: `verify_pr7b_core_backfill` ops CLI (schema-012-compatible, `SHARE`-locked)

Ships the pre-window diagnostic: raw parameterized SQL that imports **no** 013-only ORM, begins its transaction with `LOCK TABLE outbox IN SHARE MODE` **before any SELECT**, runs the exact read-only 013 preflights at `READ COMMITTED`, prints actionable ids, exits nonzero on any violation, and on a missing mapping prints the exact `BLOCKED_NO_AUTHORITATIVE_MAPPING` sentinel — with **no writes**.

**Files:**
- Create: `src/kyc_tool/ops/verify_pr7b_core_backfill.py`
- Create: `tests/integration/test_verify_pr7b_core_backfill.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `verify_backfill(session_factory) -> tuple[int, list[str]]` (exit code, offending ids) — takes the `SHARE` lock, runs preflights, no writes; `main() -> int` (real entry point, reads `get_settings().database_url`). Prints `BLOCKED_NO_AUTHORITATIVE_MAPPING <ids>` and returns nonzero on a missing mapping.
- Consumes: `kyc_tool.config.get_settings`, `kyc_tool.db.session.{make_engine, make_session_factory}` (schema-012-safe raw SQL only).

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_verify_pr7b_core_backfill.py`:

```python
"""PR 7b-core: schema-012 pre-window backfill diagnostic — REAL CLI entry point (subprocess),
the shared parity matrix (CLI + 013 both refuse), and the retention-race DB-lock half."""

import inspect
import os
import subprocess
import sys
import threading
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops import verify_pr7b_core_backfill as diag
from kyc_tool.workers.retention import prune
from tests.integration.test_migrations import _PARITY_BAD_SEEDS, _config, _fresh_db, _seed_parity_bad

pytestmark = pytest.mark.postgres


def _sf(url):
    return make_session_factory(make_engine(url))


def _seed_healthy(engine, *, delivered_at="now()"):
    """A valid decision + its delivered callback (delivered_at real). Pass an old delivered_at
    to make retention prune the callback."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                          "actor_json, payload_json, event_sequence) VALUES ('ev','c1','r','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r','c1','ev','PUBLISH_DECISION')"))
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                          "policy_shas, manual) VALUES ('d','c1','r','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"))
        conn.execute(text(f"INSERT INTO outbox (kind, case_id, run_id, payload_json, status, delivered_at) "
                          f"VALUES ('decision_callback','c1','r','{{}}'::jsonb,'delivered',{delivered_at})"))


def _run_cli(url):
    """Invoke the REAL entry point `python -m kyc_tool.ops.verify_pr7b_core_backfill`."""
    return subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
        env={**os.environ, "KYC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )


def test_cli_green_on_healthy_012_subprocess(pg):
    url = _fresh_db(pg, "kyc_diag_healthy")
    command.upgrade(_config(url), "012")  # SCHEMA 012 — no 013 columns
    engine = create_engine(url)
    _seed_healthy(engine)
    engine.dispose()
    proc = _run_cli(url)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_blocked_no_backup_sentinel_subprocess(pg):
    """Missing mapping with no restorable backup: the REAL CLI exits nonzero with the EXACT
    BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel + decision/run ids, makes NO writes, and no 014
    module is referenced (7b-activation is downstream)."""
    url = _fresh_db(pg, "kyc_diag_missing")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, "missing_callback")  # decision 'd' / run 'r', no callback
    before = engine.connect().execute(text("SELECT count(*) FROM outbox")).scalar_one()

    proc = _run_cli(url)
    assert proc.returncode != 0
    assert "BLOCKED_NO_AUTHORITATIVE_MAPPING" in proc.stdout  # exact sentinel (one token)
    assert "'d'" in proc.stdout and "'r'" in proc.stdout       # actionable ids
    after = engine.connect().execute(text("SELECT count(*) FROM outbox")).scalar_one()
    assert before == after                                     # no writes
    assert "014" not in inspect.getsource(diag)                # no 014 substitute is invoked
    engine.dispose()


@pytest.mark.parametrize("name", list(_PARITY_BAD_SEEDS))
def test_cli_and_013_both_refuse_on_parity_state(pg, name):
    """The CLI and migration 013 share ONE parity matrix — both must refuse every invalid
    legacy state, the migration BEFORE any DDL (rolled back → no claim_token column)."""
    url = _fresh_db(pg, f"kyc_diag_parity_{name}")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, name)

    code, ids = diag.verify_backfill(_sf(url))  # the real CLI core (SHARE-locked, no writes)
    assert code != 0 and ids

    with pytest.raises(RuntimeError):           # the real 013 upgrade refuses
        command.upgrade(_config(url), "013")
    with engine.connect() as conn:
        cols = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='outbox'"))}
    assert "claim_token" not in cols            # refusal happened before the column adds
    engine.dispose()


def test_cli_share_lock_blocks_on_uncommitted_retention_delete(pg):
    """Retention-race DB-lock half (the automatable proof): the REAL retention `prune` runs in
    a thread, paused by a global after_cursor_execute barrier fired AFTER its outbox DELETE but
    BEFORE commit (holding ROW EXCLUSIVE). The CLI subprocess's `LOCK TABLE outbox IN SHARE
    MODE` must BLOCK while prune holds. On commit the callback is truly gone → CLI nonzero + ids.
    MUTATION removing the SHARE lock lets the CLI snapshot before commit → it would report green.
    (The external zero-retention-process attestation stays a runbook TODO(integration).)"""
    url = _fresh_db(pg, "kyc_diag_race")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_healthy(engine, delivered_at="now() - interval '3000 days'")  # old → prune deletes it

    at_delete = threading.Event()
    release = threading.Event()

    def _barrier(conn, cursor, statement, params, context, executemany):
        if "delete from outbox where status='delivered'" in statement.lower():
            at_delete.set()
            release.wait(timeout=30)  # hold prune's txn (ROW EXCLUSIVE) open, uncommitted

    event.listen(Engine, "after_cursor_execute", _barrier)
    prune_err: list[Exception] = []

    def run_prune():
        try:
            prune(_sf(url), 7 * 365)  # the REAL retention seam
        except Exception as e:  # noqa: BLE001
            prune_err.append(e)

    t = threading.Thread(target=run_prune)
    t.start()
    proc = None
    try:
        assert at_delete.wait(timeout=15)  # prune deleted the callback, holding ROW EXCLUSIVE
        proc = subprocess.Popen(
            [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
            env={**os.environ, "KYC_DATABASE_URL": url},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(3)
        assert proc.poll() is None  # STILL BLOCKED on the SHARE lock while prune holds
        release.set()
        t.join(timeout=15)
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode != 0 and "'d'" in out  # missing mapping surfaced after commit
    finally:
        release.set()
        if proc is not None and proc.poll() is None:
            proc.kill()
        t.join(timeout=15)
        event.remove(Engine, "after_cursor_execute", _barrier)
    assert prune_err == []
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_verify_pr7b_core_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: kyc_tool.ops.verify_pr7b_core_backfill`.

- [ ] **Step 3: Write the CLI (shared parity matrix)** — create `src/kyc_tool/ops/verify_pr7b_core_backfill.py`:

```python
"""Pre-window backfill diagnostic (PR 7b-core §Rollout step 0). Run BEFORE any outage, with
the retention schedule suspended AND every active retention task terminated (orchestrator-
attested zero-running) — see docs/RUNBOOK.md. Schema-012-compatible: it runs the SAME shared
parity matrix as migration 013 (kyc_tool.ops.backfill_parity), imports NO 013-only ORM.

It begins its transaction with `LOCK TABLE outbox IN SHARE MODE` BEFORE any SELECT (defense in
depth: the SHARE lock waits for any in-flight DELETE's ROW EXCLUSIVE to resolve and blocks a
new outbox delete/write from invalidating the snapshot until the diagnostic commits), then runs
the read-only parity checks at READ COMMITTED. On any violation it prints actionable decision/
run/outbox ids and exits nonzero; a missing mapping additionally prints the exact
BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel. Recovery is restore-from-authoritative-backup or
remain on 012 — 014 is downstream and cannot repair this. It NEVER writes.

    python -m kyc_tool.ops.verify_pr7b_core_backfill
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops.backfill_parity import BLOCKED_SENTINEL, MISSING_CALLBACK, run_parity


def verify_backfill(session_factory) -> tuple[int, list[str]]:
    """Return (exit_code, offending_ids). Takes the SHARE lock first, runs the shared parity
    matrix, and NEVER writes (rolls back before returning)."""
    with session_factory() as s:
        s.execute(text("LOCK TABLE outbox IN SHARE MODE"))  # BEFORE any SELECT
        violations = run_parity(s)
        s.rollback()  # read-only: never write, never hold the lock past the check
    if not violations:
        return 0, []
    offenders: list[str] = []
    names = {n for n, _ in violations}
    if MISSING_CALLBACK in names:
        missing = next(pairs for n, pairs in violations if n == MISSING_CALLBACK)
        print(f"{BLOCKED_SENTINEL} missing decision_callback (decision, run): {missing}")
        offenders += [str(x) for pair in missing for x in pair if x is not None]
    for name, pairs in violations:
        if name != MISSING_CALLBACK:
            print(f"parity violation {name}: {pairs}")
        offenders += [str(x) for pair in pairs for x in pair if x is not None]
    return 1, offenders


def main() -> int:
    code, _ = verify_backfill(make_session_factory(make_engine(get_settings().database_url)))
    if code == 0:
        print("verify_pr7b_core_backfill: OK (schema-012 parity matrix clean)")
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/integration/test_verify_pr7b_core_backfill.py -v` → PASS.

- [ ] **Step 5: Mutation check (manual, no commit)** — remove `s.execute(text("LOCK TABLE outbox IN SHARE MODE"))`; rerun `test_cli_share_lock_blocks_on_uncommitted_retention_delete` → it must FAIL (`proc.poll()` is not None after 3s — the CLI no longer blocks on prune and can report green before the delete commits). Restore.

- [ ] **Step 6: RED drift-guard step** — the new CLI changed `src/`:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step).

- [ ] **Step 7: Re-pin → GREEN, full gate + commit** (canonical close-out order)

```bash
# re-pin EXPECTED_ENGINE_SOURCE_HASH (one-liner), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add src/kyc_tool/ops/verify_pr7b_core_backfill.py tests/integration/test_verify_pr7b_core_backfill.py \
        tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(ops): verify_pr7b_core_backfill pre-window diagnostic (SHARE-locked, shared parity)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 8: `reset_interrupted_outbox_claims` ops CLI (post-013-only)

Ships the post-013 stop/rollback helper: it **refuses pre-013 schema** (no `claim_token` column), clears only rows with the **complete** claim tuple, **preserves `next_attempt_at`**, and read-back-asserts zero claim tuples. It is NOT used in the forward cutover (the pre-013 schema has no claim columns; an interrupted old claim is encoded only in `next_attempt_at` and simply waits until its recorded due time).

**Files:**
- Create: `src/kyc_tool/ops/reset_interrupted_outbox_claims.py`
- Create: `tests/integration/test_reset_interrupted_outbox_claims.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `reset_claims(session_factory) -> int` (count cleared; raises if any claim tuple remains, or if the schema is pre-013); `main() -> int`.
- Consumes: `kyc_tool.config.get_settings`, `kyc_tool.db.session`.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_reset_interrupted_outbox_claims.py`:

```python
"""PR 7b-core: post-013 reset_interrupted_outbox_claims — REAL CLI entry point (subprocess)
on schema 012 (refuses) and 013 (clears only complete tuples), plus atomic-rollback race."""

import os
import subprocess
import sys
import threading

import pytest
from alembic import command
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops import reset_interrupted_outbox_claims as resetter
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def _sf(url):
    return make_session_factory(make_engine(url))


def _run_reset(url):
    return subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.reset_interrupted_outbox_claims"],
        env={**os.environ, "KYC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )


def test_reset_refuses_pre_013_and_preserves_next_attempt_subprocess(pg):
    """On schema 012 the REAL CLI exits nonzero (no claim_token column) and writes NOTHING —
    two future-backoff rows' next_attempt_at are unchanged (rollout order: no pre-013 reset)."""
    url = _fresh_db(pg, "kyc_reset_pre013")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        # an old-claim-looking future row + a real backoff row (012 has no claim columns)
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'pending', now() + interval '7 minutes')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'pending', now() + interval '9 minutes')"))
        before = [r.next_attempt_at for r in conn.execute(text("SELECT next_attempt_at FROM outbox ORDER BY id"))]

    proc = _run_reset(url)
    assert proc.returncode != 0
    assert "pre-013" in proc.stderr.lower() or "claim_token" in proc.stderr.lower()
    with engine.connect() as conn:
        after = [r.next_attempt_at for r in conn.execute(text("SELECT next_attempt_at FROM outbox ORDER BY id"))]
    assert after == before  # nothing written
    engine.dispose()


def test_reset_clears_only_claimed_preserves_next_attempt_subprocess(pg):
    url = _fresh_db(pg, "kyc_reset_013")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
                          "claim_lease_expires_at, claimed_by, next_attempt_at) VALUES ('poc_email','c1','email',"
                          "'pending', gen_random_uuid(), now(), 'w1', now() + interval '5 minutes')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','email','pending', now() + interval '9 minutes')"))
        before = [r.next_attempt_at for r in conn.execute(text("SELECT next_attempt_at FROM outbox ORDER BY id"))]

    proc = _run_reset(url)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT claim_token, claim_lease_expires_at, claimed_by, next_attempt_at "
                                 "FROM outbox ORDER BY id")).all()
    assert rows[0].claim_token is None and rows[0].claim_lease_expires_at is None and rows[0].claimed_by is None
    assert [r.next_attempt_at for r in rows] == before  # next_attempt_at preserved on BOTH rows
    engine.dispose()


def test_reset_atomic_rollback_when_tuple_appears_before_readback(pg):
    """A concurrent writer inserts a NEW claimed row AFTER reset's UPDATE but BEFORE its
    read-back. reset must see remaining>0, ROLL BACK its UPDATE (no partial reset), and raise —
    the originally-claimed row keeps its tuple. Proves the rollback-BEFORE-commit ordering."""
    url = _fresh_db(pg, "kyc_reset_rollback")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
                          "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
                          "gen_random_uuid(), now(), 'w1')"))

    at_readback = threading.Event()
    go = threading.Event()

    def _barrier(conn, cursor, statement, params, context, executemany):
        if "count(*) from outbox where claim_token is not null" in statement.lower():
            at_readback.set()
            go.wait(timeout=15)  # pause BEFORE the read-back executes

    event.listen(Engine, "before_cursor_execute", _barrier)
    err: list[Exception] = []

    def run_reset():
        try:
            resetter.reset_claims(_sf(url))
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=run_reset)
    t.start()
    try:
        assert at_readback.wait(timeout=15)  # reset's UPDATE done, about to read back
        with engine.begin() as conn:          # concurrent writer commits a NEW claimed row
            conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
                              "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
                              "gen_random_uuid(), now(), 'w2')"))
        go.set()
        t.join(timeout=15)
    finally:
        go.set()
        event.remove(Engine, "before_cursor_execute", _barrier)
    assert err and isinstance(err[0], RuntimeError)  # reset raised
    with engine.connect() as conn:
        tuples = conn.execute(text("SELECT count(*) FROM outbox WHERE claim_token IS NOT NULL")).scalar_one()
    assert tuples == 2  # atomic rollback: w1's clear was undone → both rows keep their tuple
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_reset_interrupted_outbox_claims.py -v`
Expected: FAIL — `ModuleNotFoundError: kyc_tool.ops.reset_interrupted_outbox_claims`.

- [ ] **Step 3: Write the CLI** — create `src/kyc_tool/ops/reset_interrupted_outbox_claims.py`:

```python
"""Post-013 outbox claim reset (PR 7b-core §Rollout). Run ONCE after ALL outbox publishers
are confirmed stopped and none have restarted, for post-013 future stops and the post-013
rollback path ONLY. It refuses pre-013 schema (the claim columns don't exist yet), clears only
rows with the COMPLETE claim tuple, PRESERVES next_attempt_at (an interrupted old claim simply
waits until its already-recorded due time), and read-back-asserts zero claim tuples — rolling
back atomically (never a partial reset) if any remain. It MUST NOT run while a publisher is live.

    python -m kyc_tool.ops.reset_interrupted_outbox_claims
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def reset_claims(session_factory) -> int:
    """Clear every complete outbox claim tuple; return the count. Refuses pre-013; on a
    surviving tuple it rolls back BEFORE commit and raises (atomic — no partial reset)."""
    with session_factory() as session:
        has_col = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='outbox' AND column_name='claim_token'"
            )
        ).first()
        if has_col is None:
            raise RuntimeError(
                "reset_interrupted_outbox_claims refuses pre-013 schema: no claim_token column "
                "(an interrupted pre-013 claim is encoded only in next_attempt_at)"
            )
        count = session.execute(
            text(
                "UPDATE outbox SET claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                "WHERE status='pending' AND claim_token IS NOT NULL "
                "AND claim_lease_expires_at IS NOT NULL AND claimed_by IS NOT NULL"
            )
        ).rowcount
        remaining = session.execute(
            text(
                "SELECT count(*) FROM outbox WHERE claim_token IS NOT NULL "
                "OR claim_lease_expires_at IS NOT NULL OR claimed_by IS NOT NULL"
            )
        ).scalar_one()
        if remaining != 0:  # roll back the whole reset BEFORE committing — never a partial clear
            session.rollback()
            raise RuntimeError(f"{remaining} outbox claim tuple(s) remain — publishers not stopped?")
        session.commit()
    return count


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    n = reset_claims(session_factory)
    print(f"reset {n} interrupted outbox claim(s); 0 claim tuples remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass + rollback mutation (manual)**

Run: `.venv/bin/pytest tests/integration/test_reset_interrupted_outbox_claims.py -v` → PASS.
**Mutation:** move `session.commit()` BEFORE the `remaining`/rollback block (the pre-fix ordering); rerun `test_reset_atomic_rollback_when_tuple_appears_before_readback` → it must FAIL (`tuples == 1` — w1's clear was committed = a partial reset). Restore.

- [ ] **Step 5: RED drift-guard step** — new CLI changed `src/`:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step).

- [ ] **Step 6: Re-pin → GREEN, full gate + commit** (canonical close-out order)

```bash
# re-pin EXPECTED_ENGINE_SOURCE_HASH (one-liner), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add src/kyc_tool/ops/reset_interrupted_outbox_claims.py \
        tests/integration/test_reset_interrupted_outbox_claims.py tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(ops): reset_interrupted_outbox_claims (post-013-only; atomic; preserves next_attempt_at)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 9: Docs + governance + ROADMAP §C `shipped` flip

Documents stream separation + local ordering + the 7b-core/activation boundary in `OVERVIEW.md`; the full ordered step-0 pre-window + drained cutover + reversible-before-first-supersession rollback in `RUNBOOK.md`/`DEPLOYMENT.md`; the A6 exception + backfill-as-deterministic-reconstruction + local/cross-replica boundary in `AUDIT_FINDINGS.md`; and flips `.agents/ROADMAP.md §C` PR 7b-core State `pending → shipped` (keeping `tests/unit/test_migration_lineage.py` green). Docs-only — no `src/` change, no drift re-pin.

**Files:**
- Modify: `docs/OVERVIEW.md`, `docs/RUNBOOK.md`, `docs/DEPLOYMENT.md`
- Modify: `AUDIT_FINDINGS.md` (backfill provenance note; A6 already amended in Task 5 — confirm)
- Modify: `.agents/ROADMAP.md` (§C row 74 State)
- Verify: `tests/unit/test_migration_lineage.py` (green after the flip)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-8 (CLIs by name, migration behavior).
- Produces: the lineage invariant `head=013 ∈ shipped`, `pending[0]=014`.

- [ ] **Step 1: Write the failing lineage guard** — the live head is now `013`. Confirm the guard currently FAILS before the flip:

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py::test_roadmap_lineage_consistent_with_alembic -v`
Expected: FAIL — `AssertionError: live Alembic head 013 is not a shipped reservation` (7b-core row is still `pending`).

- [ ] **Step 2: Flip the ROADMAP State** — in `.agents/ROADMAP.md §C`, change the PR 7b-core row (line 74) State cell from `pending` to `shipped`:

```markdown
| PR 7b-core | 8 | shipped | 013 | `outbox.ordering_stream` (NOT NULL) + `case_id` NOT NULL + per-(case,stream) claim; `decisions.decision_sequence` + `cases.last_decision_sequence` + a **best-effort** local `superseded` guard (higher *locally-stamped* delivery only; send-before-stamp/cross-replica reverts remain for 7b-activation); identity + ordering (`UNIQUE decisions(case_id, decision_sequence)` [per-case namespace] + `UNIQUE(run_id)` + triple FK + partial callback index); fenced claim (`claim_token`); exhaustive per-status lifecycle CHECKs; `ordering_stream`/`case_id` real `SET NOT NULL` (drained cutover, **reversible-before-first-supersession** downgrade) |
```

(Leave 7b-activation `014` and every other row unchanged. Also update the prose note near line 12-14 if it says "7b-core is next" — change to "7b-core shipped (`013`); 7b-activation (`014`) is next".)

- [ ] **Step 3: Run the lineage guard to verify pass**

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py -v`
Expected: PASS (`head 013 ∈ shipped`; `pending[0]=014=head+1`; disjoint/exhaustive/contiguous all hold).

- [ ] **Step 4: Write `docs/OVERVIEW.md`** — insert this exact subsection **immediately before** `### Read endpoints (pull, on demand)` (currently line 184, inside `## 4. The integration contract`):

```markdown
### Outbox delivery ordering & local decision sequence (PR 7b-core)

Decision callbacks and POC emails are delivered from a transactional outbox. As of PR 7b-core the
publisher claims the oldest pending row **per `(case_id, ordering_stream)` FIFO stream** (`decision`
vs `email`), so a stuck POC email never blocks a case's decision callbacks. Every automatic decision
gets an internal per-case `decision_sequence`, allocated under the case's `FOR UPDATE` lock — it is
**not on the wire** (the callback body is byte-identical to pre-7b) and drives a **best-effort local
`superseded` guard**: an older requeued callback is suppressed only when a higher-sequence decision
already carries a locally-stamped `published_at`. Each claim is fenced by a `claim_token` so a stale
publisher cannot overwrite a reclaimer's terminal. **Boundary:** send-before-stamp and cross-replica
reverts are NOT closed here — they remain expected until 7b-activation (`014`) adds the platform
high-water mark. 7b-core does not claim exactly-once (see `AUDIT_FINDINGS.md` A6).
```

- [ ] **Step 5: Write the canonical cutover/rollback procedure into BOTH `docs/RUNBOOK.md` and `docs/DEPLOYMENT.md`** — paste the **identical** block below. In `docs/RUNBOOK.md` add it as a new top-level section immediately before `## Retention & compliance` (line 173). In `docs/DEPLOYMENT.md` add it as `## 11. PR 7b-core cutover — drained maintenance window` immediately after section 10 (before `## 7. Monitoring` if ordering differs, else at end of the cutover sections). Use the SAME numbered body in both (only the section header differs):

```markdown
## PR 7b-core cutover — drained maintenance window (migration 013)

**Step 0 — pre-window diagnostic (BEFORE any outage):**
0.1 Suspend the retention schedule.
0.2 Terminate and wait for every active retention task.
0.3 Capture target-orchestrator zero-running evidence. `TODO(integration)`: the exact ECS/Fargate
    `aws ecs list-tasks --cluster <c> --family retention` (or EC2 equivalent) command + its expected
    zero-task output MUST be recorded here once the production substrate is chosen. A pytest does NOT
    prove this — it is a deployment acceptance. Do not invent a substrate.
0.4 With the schedule still suspended, run the digest-pinned
    `python -m kyc_tool.ops.verify_pr7b_core_backfill`. The result is valid ONLY while retention stays
    suspended AND the 0.3 attestation holds.
0.5 On failure, ABORT here — before stopping service (no outage begun). Recovery is restore-or-block:
    restore the exact callback from authoritative backup and rerun 0.4 clean, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. 014 is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer;
   attest zero at the orchestrator.
3. Run the shipped `python -m kyc_tool.ops.requeue_interrupted_jobs`. NO outbox reset here — the
   pre-013 schema has no claim columns; an interrupted old claim simply waits until its already-
   recorded `next_attempt_at`. Preserve every pending row's `next_attempt_at`.
4. `alembic upgrade head` (013). This repeats the §0 parity preflights under the zero-writer boundary
   and is the authoritative fail-closed check (the pre-window diagnostic is an early detector, not a
   substitute).
5. Start API only, probe `/readyz`, then start + attest the fenced workers. No mutating prod smoke.
6. Resume submission and re-enable retention.

**Rollback — a full ordered maintenance procedure (as drained as the forward cutover):**
R1. Pause submissions, disable autoscaling/restarts.
R2. Hard-stop and orchestrator-attest zero API, pipeline, outbox, `dev_worker`, retention, every writer.
R3. While 013 still exists, run `python -m kyc_tool.ops.reset_interrupted_outbox_claims` (post-013-only;
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. `alembic downgrade` (its `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE` + preflight
    `SELECT count(*) FROM outbox WHERE status='superseded'` refuses byte-stably if any superseded row
    exists — then rollback stays on a 7b-core-compatible image and is a forward fix).
R5. Deploy the pre-7b image ONLY after the downgrade succeeds. Redeploying the pre-7b image BEFORE 013
    is applied is also safe.
```

- [ ] **Step 6: Extend `AUDIT_FINDINGS.md`** — confirm the A6 **Resolution** already carries the PR 7b-core exception from Task 5 (if not, re-apply it). Then add this exact item at the end of section **D. Design tightenings adopted** (after line 144):

```markdown
### 🔵 D-7bcore — Outbox local ordering (PR 7b-core) provenance boundary

- Migration 013's decision_sequence backfill orders each case by `outbox.id` (the under-lock enqueue
  serialization for rows the guard can deliver). This is a **deterministic reconstruction, not proof
  of original publication order** — `decided_at` is transaction-start time and can invert the true
  order, so it is never used.
- A legacy automatic decision without a surviving `decision_callback` is a **fail-closed migration
  refusal** (`BLOCKED_NO_AUTHORITATIVE_MAPPING`): restore from authoritative backup or remain on 012.
- The local `superseded` guard is **best-effort and single-replica**: it fires only on a higher
  *locally-stamped* `published_at`. Send-before-stamp and cross-replica reverts remain expected until
  7b-activation's platform high-water mark. 7b-core is not exactly-once and not platform-authoritative.
```

- [ ] **Step 7: Verify parity + full gate + commit**

```bash
# ROADMAP: 7b-core shipped/013, 7b-activation still pending/014, no contradictory allocation
rg -n "PR 7b-core \| 8 \| shipped \| 013" .agents/ROADMAP.md
rg -n "PR 7b-activation \| 8 \| pending \| 014" .agents/ROADMAP.md
# RUNBOOK.md and DEPLOYMENT.md carry the SAME numbered cutover/rollback body (parity)
diff <(rg -n "^(0\.[0-9]|[1-6]\.|R[1-5]\.) " docs/RUNBOOK.md | sed 's/^[0-9]*://') \
     <(rg -n "^(0\.[0-9]|[1-6]\.|R[1-5]\.) " docs/DEPLOYMENT.md | sed 's/^[0-9]*://')   # must be empty
# both docs reference both CLIs + the sentinel
rg -c "verify_pr7b_core_backfill" docs/RUNBOOK.md docs/DEPLOYMENT.md   # each >= 1
rg -c "BLOCKED_NO_AUTHORITATIVE_MAPPING" docs/RUNBOOK.md docs/DEPLOYMENT.md  # each >= 1
.venv/bin/pytest tests/unit/test_migration_lineage.py -v   # head 013 shipped, pending[0]=014
./manage.sh test          # whole 013 suite + lineage
.venv/bin/ruff check .
.venv/bin/lint-imports
git add docs/OVERVIEW.md docs/RUNBOOK.md docs/DEPLOYMENT.md AUDIT_FINDINGS.md .agents/ROADMAP.md
git commit -m "docs(7b-core): stream-separation overview, drained cutover/rollback, ROADMAP shipped

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Final gate (run after Task 9)

- [ ] `./manage.sh lint` (ruff) + `.venv/bin/lint-imports` → **2 contracts kept / 0 broken**.
- [ ] `.venv/bin/pytest tests/unit/test_migration_lineage.py -v` → green (post-flip).
- [ ] `./manage.sh test` → whole suite green.
- [ ] Confirm `rg -n "013" .agents/ROADMAP.md` shows no contradictory live allocation and 7b-core is `shipped`, 7b-activation `014` still `pending`.
- [ ] Confirm the callback HTTP body is byte-identical to pre-7b (no `decision_sequence` in `payload_json` / on the wire) — `tests/integration/test_callback_signing.py` + `test_run_walk.py` unchanged and green.
- [ ] Confirm `ENGINE_BUILD_ID` was NOT bumped and `EXPECTED_ENGINE_SOURCE_HASH` is re-pinned to the finished tree.

## Self-review notes (spec coverage)

Every spec section maps to a task: §1 Migration 013 → Tasks 1 (columns/stream/case_id/lifecycle/index), 2 (shared parity matrix + backfill), 4 (identity constraints), 6 (race-safe downgrade); §2 fenced claim → Task 3; §3 decision-sequence allocation → Task 4; §4 guard + fenced terminals → Tasks 3 (terminals) + 5 (guard/superseded); §5 scope boundary + residual risk → Task 5 (residual-risk test) + Task 9 (docs); AUDIT:A6 amendment → Task 5; §Rollout (verify CLI, requeue, reset CLI, restore-or-block) → Tasks 7, 8, 9; §Testing strategy items → distributed across Tasks 1-8 with a mutation witness each; §Docs+governance → Task 9; §Out-of-scope respected (no wire field, no `ENGINE_BUILD_ID` bump, M2 untouched). The one deliberate structural deviation from the spec's §1 grouping — splitting the decision-identity constraints out of Task 1 into Task 4 (co-located with the pipeline allocation that satisfies them) — is required by the green-at-every-commit rule (`conftest.migrated` upgrades to head for the whole suite, so an identity CHECK cannot precede the code that writes `decision_sequence`); the migration file is still a single `013` and every constraint/backfill/downgrade element is present.

## Codex plan-review round 1 — 10 findings closed (test/command defects only; decomposition + architecture ACCEPTED)

1. **Legacy-order regression (Task 2)** now commits the case first, drives B in a thread that fixes its `decided_at` via `SELECT now()` behind an event, releases A to enqueue first, and after upgrade delivers BOTH rows through the REAL publisher (order A→B, both `delivered`, neither `superseded`); the counter fix (`==3`) lands in the same red→green edit; the `ORDER BY d.decided_at` mutation reverses assignment and fails it.
2. **Stale-claim (Task 3)** now claims token A, expires it, and lets B RECLAIM without terminalizing; A runs stale success + stale final-attempt failure and must touch nothing (byte-identical payload, B's tuple/attempts/clock intact); the two named mutations (remove loser return; raise on zero rows) each fail a named test; unused `DECISION_CALLBACK` import removed.
3. **Concurrency + manual path (Task 4)** now calls the REAL `_decide_txn` from two threads with a barrier monkeypatched immediately before `_load`'s `FOR UPDATE`; dropping the lock fails it. Manual approve uses the required `reviewer_id` payload + matching actor and asserts HTTP 200.
4. **Downgrade TOCTOU (Task 6)** now drives the REAL migration `downgrade()` in a thread, paused by a global `after_cursor_execute` barrier at its superseded preflight (AFTER the production `LOCK TABLE`); the concurrent `pending→superseded` UPDATE fails with SQLSTATE `55P03`; moving/removing the lock fails the test.
5. **Diagnostic (Tasks 2+7)** now share ONE parity matrix (`backfill_parity.run_parity`) covering all 11 listed states; the migration refuses BEFORE any DDL and the CLI refuses via the real `python -m` subprocess (`KYC_DATABASE_URL`, exact exit/stdout/`BLOCKED_NO_AUTHORITATIVE_MAPPING`/no-writes); the retention race runs the REAL `prune` in a thread with a barrier after its outbox DELETE and asserts the CLI subprocess stays blocked until commit; removing the SHARE lock fails it. Healthy rows carry a real `delivered_at`.
6. **Command ordering** — every src-touching task (1, 2, 3, 4, 5, 7, 8) now runs targeted tests → RED drift-guard step → re-pin GREEN → `./manage.sh test` + ruff + import-linter, in that order; the deliberately-red guard is labeled a RED step, never a PASS/gate.
7. **Guard/A6/residual (Task 5)** now creates TWO real callbacks, delivers the higher one over the mock HTTP first (≥1 request) and asserts the lower gets zero HTTP + `superseded` + run COMPLETE + `published_at` NULL + an `audit_log` row carrying BOTH sequences (`_record_superseded` now `audit()`s them); adds a real seq 1→2→3 test, a real send-before-stamp revert (HTTP #1 then #2), and an `outbox_alerting` metrics assertion.
8. **Reset CLI (Task 8)** now probes `information_schema` with `table_schema=current_schema()`, rolls back BEFORE commit on a surviving tuple (atomic), and is exercised via `python -m` subprocess on 012 (refuses, timestamps unchanged) and 013 (clears only claimed, preserves `next_attempt_at`) plus a barrier race proving atomic rollback.
9. **Migration matrix (Tasks 1+4)** now seeds legacy pending/delivered(timestamped)/dead + both kinds + manual rows, adds the full INSERT+UPDATE identity cross-product (null/zero/negative sequence, wrong case/run, stream swaps, email-with-run, dup callback, run-id uniqueness), and mutates each CHECK/FK/unique/partial-index/`SET NOT NULL` independently against a NAMED test; authority cases assert exact `RuntimeError`/`IntegrityError`/`OperationalError`+`55P03`.
10. **Finish-time (Tasks 2/7/10)** — unused example-code imports removed; Task 9 is copy-ready (exact OVERVIEW subsection + anchor, one canonical numbered cutover/rollback block pasted verbatim into RUNBOOK.md and DEPLOYMENT.md with a `diff` parity check + `rg` sentinel/lineage checks, honest blocking `TODO(integration)` for the orchestrator attestation).

**Adaptations to the real fixtures (where Codex's prescription needed adjustment):** (a) the send-before-stamp revert is single-publisher-deterministic by delivering seq 2 for real then resetting its `decisions.published_at` to NULL — the exact observable of "stamp not locally committed" — because the fenced FIFO otherwise prevents a single publisher from sending seq 1 while seq 2's row is still pending; (b) the TOCTOU and retention-race barriers use a global `after_cursor_execute`/`before_cursor_execute` listener on `sqlalchemy.engine.Engine` (removed in `finally`) since the project's `alembic/env.py` builds its own connection and cannot be handed an injected one; (c) migration-refusal assertions use `pytest.raises(RuntimeError)` on the basis that `alembic.command` propagates the migration's `RuntimeError` unwrapped — if a given environment wraps it, widen to the wrapper in-cycle. **Not executed:** per the coordinator, the named `.venv/bin/pytest`/subprocess selectors are build-cycle steps, not run here (no local Postgres).
