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
- **Delivery-layer only — NO scoring/gate/decision semantic change.** The callback HTTP body stays **byte-identical** to pre-7b: `decision_sequence` is written to the `decisions`/`outbox` **columns only**, never added to `payload_json` or the wire (that is 7b-activation / `014`).
- **Engine drift guard.** Every task that edits any file under `src/kyc_tool/**` MUST re-pin `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py` **in the same commit** (see the re-pin one-liner in "Test infrastructure" below). **Do NOT bump `ENGINE_BUILD_ID`** — this is not a scoring change. Tasks that touch only `alembic/`, docs, or `.agents/` do NOT re-pin (the guard closure is `src/kyc_tool/**/*.py` only).
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
- **Task 2** adds the legacy `decision_sequence` backfill (touches only legacy rows; no new-row CHECK yet).
- **Task 3** fences the claim + terminals (the `claim_token` is set by the claim and cleared by every terminal in the same step — they cannot split without violating the lifecycle CHECK).
- **Task 4** makes the pipeline allocate `decision_sequence` **and**, in the same step, adds the decision-identity constraints (kind/stream identity CHECK, `manual`/`automatic` CHECK, the three decisions uniques, the triple FK, the partial callback unique) — now both legacy (backfilled) and live (allocated) rows satisfy them.
- **Task 5** adds the local guard + `superseded` lifecycle wiring.
- **Task 6** hardens `013`'s downgrade (LOCK + `superseded` preflight).
- **Tasks 7–8** ship the two ops CLIs; **Task 9** ships docs + the ROADMAP flip.

Each task edits `alembic/versions/013_outbox_stream_separation.py` at clearly marked insertion points; every test run re-applies `013` from scratch on a fresh DB, so cross-task edits to one migration file are safe.

## File Structure

**Create:**
- `alembic/versions/013_outbox_stream_separation.py` — the single migration (`down_revision='012'`): all columns, backfills, constraints, index, and the race-safe downgrade. Authored across Tasks 1, 2, 4, 6.
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

def _seed_legacy_callback(engine, *, case_id, run_id, decision_id, ev_seq, decided_at=None):
    """Seed one valid case→event→run→automatic-decision→pending decision_callback
    chain at schema 012 (no ordering_stream / decision_sequence columns yet). The
    outbox.id order is enqueue order; decided_at defaults to txn-start now() unless
    pinned. Returns the freshly-allocated outbox id."""
    with engine.begin() as conn:
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
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES (:r,:c,:e,'PUBLISH_DECISION')"
            ),
            {"r": run_id, "c": case_id, "e": run_id + "-ev"},
        )
        if decided_at is None:
            conn.execute(
                text(
                    "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                    "buy_enablement, policy_shas, manual) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,"
                    "'enabled','{}'::jsonb,false)"
                ),
                {"d": decision_id, "c": case_id, "r": run_id},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                    "buy_enablement, policy_shas, manual, decided_at) VALUES (:d,:c,:r,'approve',10,"
                    "'{}'::jsonb,'enabled','{}'::jsonb,false,:t)"
                ),
                {"d": decision_id, "c": case_id, "r": run_id, "t": decided_at},
            )
        oid = conn.execute(
            text(
                "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                "VALUES ('decision_callback',:c,:r,'{}'::jsonb,'pending') RETURNING id"
            ),
            {"c": case_id, "r": run_id},
        ).scalar_one()
    return oid


def test_013_upgrade_sets_stream_and_notnull_metadata(pg):
    url = _fresh_db(pg, "kyc_mig_013_streams")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    _seed_legacy_callback(engine, case_id="c1", run_id="r1", decision_id="d1", ev_seq=1)
    # a poc_email (no run) and a manual decision must also survive
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO outbox (kind, case_id, payload_json, status) "
                "VALUES ('poc_email','c1','{\"to\":\"a@b\"}'::jsonb,'pending')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                "buy_enablement, policy_shas, manual) VALUES ('dm','c1',NULL,'approve',0,'{}'::jsonb,"
                "'enabled','{}'::jsonb,true)"
            )
        )

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
        ).scalar_one() == 0  # Task 2 backfill seeds this; Task 1 leaves the default
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

- [ ] **Step 9: Run the full suite to prove the live path still enqueues green**

Run: `./manage.sh test`
Expected: PASS. (Every decide enqueues a callback with `ordering_stream='decision'`; POC emails with `ordering_stream='email'`; the `NOT NULL` holds. `test_engine_source_hash_pinned` FAILS — expected, re-pinned next.)

- [ ] **Step 10: Re-pin the engine drift guard**

Run the re-pin one-liner (see Test infrastructure), paste the digest into `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py`, then:
Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → PASS.

- [ ] **Step 11: Lint + commit**

```bash
.venv/bin/ruff check . && .venv/bin/lint-imports
git add alembic/versions/013_outbox_stream_separation.py src/kyc_tool/db/tables.py \
        src/kyc_tool/outbox/publisher.py tests/integration/test_migrations.py \
        tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(013): outbox stream separation, case_id NOT NULL, lifecycle+claim CHECKs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

---

## Task 2: Migration 013 part B — legacy `decision_sequence` backfill (order authority = `outbox.id`)

Maps every `manual=false` decision to exactly one surviving `decision_callback` outbox row by `run_id` (refusing missing/orphan/duplicate with actionable ids), ranks per case by `outbox.id` (the under-lock enqueue order, **not** `decided_at`), stamps `decisions.decision_sequence` + copies it to the callback row, and seeds `cases.last_decision_sequence`. Migration-only — no `src/` change, so no drift re-pin.

**Files:**
- Modify: `alembic/versions/013_outbox_stream_separation.py` (the "Task 2 insertion point" in `upgrade`)
- Modify: `tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: schema from Task 1 (columns present, no decision-identity constraints yet).
- Produces: after upgrade, every automatic decision + its callback carry a per-case `decision_sequence = row_number() OVER (PARTITION BY case_id ORDER BY outbox.id)`; `cases.last_decision_sequence = per-case max`. Refuses (raises `RuntimeError`, rolls back) on any missing/orphan/duplicate mapping.

- [ ] **Step 1: Write the failing order-authority regression test** — append to `tests/integration/test_migrations.py`:

```python
def test_013_backfill_orders_by_outbox_id_not_decided_at(pg):
    """Two-connection inversion (rev-4 F1): B's decide txn starts FIRST (so B.decided_at
    is earlier) but A enqueues its callback FIRST (so A.outbox_id is lower). now() is
    txn-start time and _decide_txn opens its txn before taking the case FOR UPDATE, so
    decided_at can invert the true (lock-serialized) order — outbox.id cannot. The backfill
    must rank by outbox.id: A=seq 1, B=seq 2, counter=2. Ranking by decided_at would flip
    them and later suppress the actually-newer callback."""
    url = _fresh_db(pg, "kyc_mig_013_inversion")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")

    engineB = create_engine(url)
    engineA = create_engine(url)
    connB = engineB.connect()
    txB = connB.begin()  # B's txn starts first → B.decided_at (server_default now()) is earlier
    try:
        connB.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        connB.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES ('evB','c1','rB','h','x',"
                "'{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        connB.execute(
            text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('rB','c1','evB','PUBLISH_DECISION')")
        )
        connB.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                "buy_enablement, policy_shas, manual) VALUES ('dB','c1','rB','approve',10,'{}'::jsonb,"
                "'enabled','{}'::jsonb,false)"
            )
        )
        # A's whole decide happens AFTER B started but enqueues its callback FIRST
        with engineA.begin() as connA:
            connA.execute(
                text(
                    "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                    "actor_json, payload_json, event_sequence) VALUES ('evA','c1','rA','h','x',"
                    "'{}'::jsonb,'{}'::jsonb,2)"
                )
            )
            connA.execute(
                text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('rA','c1','evA','PUBLISH_DECISION')")
            )
            connA.execute(
                text(
                    "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                    "buy_enablement, policy_shas, manual) VALUES ('dA','c1','rA','approve',10,'{}'::jsonb,"
                    "'enabled','{}'::jsonb,false)"
                )
            )
            connA.execute(
                text(
                    "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                    "VALUES ('decision_callback','c1','rA','{}'::jsonb,'pending')"
                )
            )  # A's outbox row gets the LOWER id
        connB.execute(
            text(
                "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                "VALUES ('decision_callback','c1','rB','{}'::jsonb,'pending')"
            )
        )  # B's outbox row gets the HIGHER id
        txB.commit()
    finally:
        connB.close()

    with engineA.connect() as conn:
        a_dt, a_oid = conn.execute(
            text(
                "SELECT d.decided_at, o.id FROM decisions d JOIN outbox o ON o.run_id=d.run_id "
                "WHERE d.id='dA'"
            )
        ).one()
        b_dt, b_oid = conn.execute(
            text(
                "SELECT d.decided_at, o.id FROM decisions d JOIN outbox o ON o.run_id=d.run_id "
                "WHERE d.id='dB'"
            )
        ).one()
    assert b_dt < a_dt and a_oid < b_oid  # the inversion is real

    alembic_command.upgrade(cfg, "013")

    with engineA.connect() as conn:
        seqs = {
            r.id: r.decision_sequence
            for r in conn.execute(text("SELECT id, decision_sequence FROM decisions WHERE case_id='c1'"))
        }
        assert seqs == {"dA": 1, "dB": 2}  # by outbox.id, NOT decided_at
        assert conn.execute(text("SELECT last_decision_sequence FROM cases WHERE id='c1'")).scalar_one() == 2
        ob = {
            r.run_id: r.decision_sequence
            for r in conn.execute(text("SELECT run_id, decision_sequence FROM outbox WHERE case_id='c1'"))
        }
        assert ob == {"rA": 1, "rB": 2}
    engineA.dispose()
    engineB.dispose()


def test_013_backfill_refuses_missing_callback_byte_stable(pg):
    """A valid manual=false decision with NO surviving callback (a reachable state:
    retention prunes delivered outbox rows while the immutable decision lives forever) —
    the safe order cannot be guessed, so the migration fails closed. No decided_at fallback."""
    url = _fresh_db(pg, "kyc_mig_013_missing_cb")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES ('ev','c1','r','h','x',"
                "'{}'::jsonb,'{}'::jsonb,1)"
            )
        )
        conn.execute(
            text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r','c1','ev','PUBLISH_DECISION')")
        )
        conn.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                "buy_enablement, policy_shas, manual) VALUES ('d','c1','r','approve',10,'{}'::jsonb,"
                "'enabled','{}'::jsonb,false)"
            )
        )  # no outbox callback row
    engine.dispose()
    with pytest.raises(Exception) as exc:  # noqa: PT011 — alembic wraps the RuntimeError
        alembic_command.upgrade(cfg, "013")
    msg = str(exc.value)
    assert "without a surviving decision_callback" in msg and "d" in msg  # actionable ids
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_backfill" -v`
Expected: FAIL — the inversion test sees `dA=None, dB=None` (no backfill yet); the missing-callback test does NOT raise (upgrade succeeds).

- [ ] **Step 3: Add the backfill** — in `alembic/versions/013_outbox_stream_separation.py`, replace the `# === Task 2 insertion point: legacy decision_sequence backfill ===` line in `upgrade()` with:

```python
    # --- legacy decision_sequence backfill; ORDER AUTHORITY = outbox.id (rev-4 F1) ---
    # decided_at is txn-start now() and _decide_txn opens its txn before the case
    # FOR UPDATE, so two same-case decides can invert decided_at vs their lock-serialized
    # commit order. outbox.id is allocated at enqueue UNDER that lock, so it preserves the
    # true serialization. Ranking by decided_at would assign the newer callback the lower
    # sequence and the local guard would then suppress the actually-newer callback.
    missing = conn.execute(
        sa.text(
            "SELECT d.id AS decision_id, d.run_id FROM decisions d WHERE d.manual = false "
            "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.kind='decision_callback' AND o.run_id = d.run_id)"
        )
    ).fetchall()
    if missing:
        raise RuntimeError(
            "migration 013: automatic decision(s) without a surviving decision_callback — "
            "restore the callback from authoritative backup or remain on 012 "
            f"(BLOCKED_NO_AUTHORITATIVE_MAPPING); offenders: {[(r.decision_id, r.run_id) for r in missing]}"
        )
    orphan = conn.execute(
        sa.text(
            "SELECT o.id, o.run_id FROM outbox o WHERE o.kind='decision_callback' "
            "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.manual=false AND d.run_id = o.run_id)"
        )
    ).fetchall()
    if orphan:
        raise RuntimeError(
            f"migration 013: orphan decision_callback outbox row(s) (no automatic decision): "
            f"{[(r.id, r.run_id) for r in orphan]}"
        )
    dup_cb = conn.execute(
        sa.text(
            "SELECT run_id, count(*) AS c FROM outbox WHERE kind='decision_callback' "
            "GROUP BY run_id HAVING count(*) > 1"
        )
    ).fetchall()
    if dup_cb:
        raise RuntimeError(
            f"migration 013: multiple decision_callback rows per run: {[(r.run_id, r.c) for r in dup_cb]}"
        )
    bad_email = conn.execute(
        sa.text("SELECT id FROM outbox WHERE kind='poc_email' AND decision_sequence IS NOT NULL")
    ).fetchall()
    if bad_email:
        raise RuntimeError(f"migration 013: poc_email row(s) carry a decision_sequence: {[r.id for r in bad_email]}")

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
    # defensive: row_number() cannot produce a per-case duplicate, so this always holds.
    post_dup = conn.execute(
        sa.text(
            "SELECT case_id, decision_sequence FROM decisions WHERE decision_sequence IS NOT NULL "
            "GROUP BY case_id, decision_sequence HAVING count(*) > 1"
        )
    ).fetchall()
    if post_dup:
        raise RuntimeError(f"migration 013: post-backfill per-case sequence collision: {post_dup}")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_backfill or 013_upgrade_sets_stream" -v`
Expected: PASS (the counter now seeds to 2 in the inversion test and to 1 in `test_013_upgrade_sets_stream_and_notnull_metadata`; the missing-callback test raises byte-stably).

- [ ] **Step 5: Fix the stream/notnull test's counter assertion** — in `test_013_upgrade_sets_stream_and_notnull_metadata` change the final assertion from `== 0` to `== 1` (case `c1` now has one backfilled automatic decision), and rerun that test → PASS.

- [ ] **Step 6: Mutation check (manual, no commit)** — change the backfill's `ORDER BY o.id` to `ORDER BY d.decided_at, o.id`; rerun `.venv/bin/pytest tests/integration/test_migrations.py::test_013_backfill_orders_by_outbox_id_not_decided_at -v`; confirm it now FAILS (`dA=2, dB=1`). Restore `ORDER BY o.id`.

- [ ] **Step 7: Full gate + commit**

```bash
./manage.sh test
.venv/bin/ruff check . && .venv/bin/lint-imports
git add alembic/versions/013_outbox_stream_separation.py tests/integration/test_migrations.py
git commit -m "feat(013): legacy decision_sequence backfill ranked by outbox.id (fail-closed)

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
from sqlalchemy import text

pytestmark = pytest.mark.postgres

from kyc_tool.outbox.publisher import DECISION_CALLBACK  # noqa: E402


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


def test_stale_claimant_cannot_overwrite_reclaimers_delivered(session_factory, settings, publisher):
    """Defect 3: publisher A claims (token A), its lease expires, B reclaims (token B) and
    delivers. A resuming with token A must apply NOTHING (applied=false) — it cannot stamp
    dead/delivered over B's terminal, and process_once stays alive."""
    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="p@x")

    # A claims via the real claim SQL, then we expire its lease.
    from kyc_tool.outbox.publisher import _CLAIM_SQL

    with session_factory() as s:
        rowA = s.execute(_CLAIM_SQL, {"lease_seconds": 3600, "claimed_by": "A"}).first()
        s.commit()
    assert rowA is not None
    with session_factory() as s:
        s.execute(text("UPDATE outbox SET claim_lease_expires_at = now() - interval '1 second' WHERE id=:i"), {"i": rowA.id})
        s.commit()

    # B reclaims + delivers via the normal loop.
    assert publisher.process_pending() == 1
    with session_factory() as s:
        after_b = s.execute(text("SELECT status, claim_token FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert after_b.status == "delivered" and after_b.claim_token is None

    # A resumes with its STALE token — both a delivered and a dead attempt must no-op.
    publisher._record_delivered(rowA, rowA.claim_token)
    publisher._record_failure(rowA, "boom", rowA.claim_token)
    with session_factory() as s:
        final = s.execute(text("SELECT status, delivered_at, claim_token FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert final.status == "delivered" and final.delivered_at is not None and final.claim_token is None
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

- [ ] **Step 5: Add both-kinds barrier + POC-not-redacted loser tests** — append to `tests/integration/test_outbox_fencing.py`:

```python
def test_stale_final_attempt_failure_does_not_kill_worker_or_redact(session_factory, settings):
    """POC email: A claims, lease expires, B is about to send. A's stale FINAL-attempt
    failure must NOT redact the payload (byte-for-byte intact) or dead-letter it, and B
    can still send. Uses a publisher whose email sender fails A's path only."""
    from kyc_tool.outbox.publisher import OutboxPublisher, _CLAIM_SQL

    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="keep@x")
    s2 = settings.model_copy(update={"outbox_max_attempts": 1})  # next failure is terminal (dead)
    pub = OutboxPublisher(session_factory, s2, http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))

    with session_factory() as s:
        rowA = s.execute(_CLAIM_SQL, {"lease_seconds": 3600, "claimed_by": "A"}).first()
        s.commit()
    with session_factory() as s:
        s.execute(text("UPDATE outbox SET claim_lease_expires_at = now() - interval '1 second' WHERE id=:i"), {"i": rowA.id})
        s.commit()

    # B reclaims + delivers (redacts as the winner).
    assert pub.process_pending() == 1
    # A resumes with a stale terminal FAILURE — must be a no-op (payload already redacted by B,
    # but A must not have been the one to touch it; assert A leaves status/token unchanged).
    with session_factory() as s:
        before = s.execute(text("SELECT status, claim_token FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    pub._record_failure(rowA, "stale boom", rowA.claim_token)
    with session_factory() as s:
        after = s.execute(text("SELECT status, claim_token FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert (after.status, after.claim_token) == (before.status, before.claim_token) == ("delivered", None)
```

- [ ] **Step 6: Run + mutation checks (manual, no commit)**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v` → PASS.
Mutation (a): in `_record_delivered`, delete the `if applied is None: … return` early-return; rerun `test_stale_claimant_cannot_overwrite_reclaimers_delivered` → confirm it FAILS (A overwrites/duplicates the terminal). Restore.
Mutation (b): change the fenced `UPDATE … RETURNING id` result handling to raise on zero rows (`raise RuntimeError` instead of `return`); rerun → confirm the stale-loser tests FAIL (the raise escapes). Restore.

- [ ] **Step 7: Full gate, re-pin, commit**

```bash
./manage.sh test          # whole suite green (delivery still stamps published_at + run COMPLETE)
# re-pin EXPECTED_ENGINE_SOURCE_HASH (one-liner), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
.venv/bin/ruff check . && .venv/bin/lint-imports
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

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres


def test_two_decides_one_case_allocate_increasing_unique_sequences(
    client, session_factory, post_event, worker
):
    post_event("case-seq", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()
    post_event("case-seq", "email.verified", {"email": "a@b.com", "domain": "b.com", "verified_at": "2026-07-23T00:00:00Z"})
    worker.run_until_idle()

    with session_factory() as s:
        seqs = sorted(
            r.decision_sequence
            for r in s.execute(
                text("SELECT decision_sequence FROM decisions WHERE case_id='case-seq' AND manual=false")
            )
        )
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='case-seq'")).scalar_one()
        callbacks = sorted(
            r.decision_sequence
            for r in s.execute(
                text("SELECT decision_sequence FROM outbox WHERE case_id='case-seq' AND kind='decision_callback'")
            )
        )
    assert seqs == [1, 2]  # strictly increasing, unique
    assert counter == 2
    assert callbacks == [1, 2]  # each callback carries its decision's sequence


def test_manual_approve_allocates_no_sequence(client, session_factory, post_event, worker, sign):
    import json

    post_event("case-man", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()
    body = json.dumps({
        "event_type": "reviewer.manual_approve",
        "occurred_at": "2026-07-23T00:00:00Z",
        "actor": {"type": "reviewer", "id": "rev-1"},
        "payload": {"note": "ok"},
    }).encode()
    client.post("/v1/cases/case-man/events", content=body, headers=sign(body))

    with session_factory() as s:
        manual = s.execute(
            text("SELECT decision_sequence, run_id FROM decisions WHERE case_id='case-man' AND manual=true")
        ).one()
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='case-man'")).scalar_one()
    assert manual.decision_sequence is None and manual.run_id is None
    assert counter == 1  # only the one automatic decide bumped it; manual approve did not
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_decision_sequence.py -v`
Expected: FAIL — automatic decisions have `decision_sequence=None` (pipeline does not allocate yet), so `seqs == [None, None]`.

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

- [ ] **Step 6: Run allocation tests + full suite**

Run: `.venv/bin/pytest tests/integration/test_decision_sequence.py -v` → PASS.
Run: `./manage.sh test` → PASS (both legacy-backfilled and live-allocated rows satisfy the new constraints).

- [ ] **Step 7: Add the identity INSERT/UPDATE negatives** — append to `tests/integration/test_migrations.py`:

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
    # multiple manual NULL-sequence decisions remain legal
    with engine.begin() as conn:
        for i in range(2):
            conn.execute(
                text(
                    "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                    "buy_enablement, policy_shas, manual) VALUES (:d,'c1',NULL,'approve',0,'{}'::jsonb,"
                    "'enabled','{}'::jsonb,true)"
                ),
                {"d": f"dm{i}"},
            )
    engine.dispose()


_DECISION_IDENTITY_BAD = {
    # (manual/automatic CHECK) manual row carrying a run_id
    "manual_with_run": "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
        "buy_enablement, policy_shas, manual) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,true)",
    # (manual/automatic CHECK) automatic row with non-positive sequence
    "auto_zero_seq": "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
        "buy_enablement, policy_shas, manual, decision_sequence) VALUES ('d','c1','rX','approve',0,"
        "'{}'::jsonb,'enabled','{}'::jsonb,false,0)",
}


@pytest.mark.parametrize("case", list(_DECISION_IDENTITY_BAD))
def test_013_decision_identity_negatives(pg, case):
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
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('rX','c1','rX-ev','PUBLISH_DECISION')"))
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(_DECISION_IDENTITY_BAD[case]))
    engine.dispose()


_OUTBOX_BINDING_BAD = {
    # (kind/stream identity) callback with NULL run_id
    "callback_null_run": "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1',NULL,'decision',1,'pending')",
    # (kind/stream identity) callback with non-positive sequence
    "callback_zero_seq": "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',0,'pending')",
    # (kind/stream identity) sequenced email
    "email_sequenced": "INSERT INTO outbox (kind, case_id, ordering_stream, decision_sequence, status) "
        "VALUES ('poc_email','c1','email',1,'pending')",
    # (triple FK) callback claiming a (run,case,seq) with no matching decision
    "callback_wrong_seq": "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',2,'pending')",
    # (partial unique) two callbacks for the same run
    "dup_callback": "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',1,'pending'); "
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',1,'pending')",
}


@pytest.mark.parametrize("case", list(_OUTBOX_BINDING_BAD))
def test_013_outbox_binding_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_013_ob_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)  # (rA,c1,1) exists
    with pytest.raises(IntegrityError), engine.begin() as conn:
        for stmt in _OUTBOX_BINDING_BAD[case].split("; "):
            conn.execute(text(stmt))
    engine.dispose()
```

- [ ] **Step 8: Run negatives + mutation check (manual)**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_two_runs or 013_decision_identity or 013_outbox_binding" -v` → PASS.
Mutation: drop **only** `op.create_unique_constraint("uq_decisions_case_decision_sequence", ...)` from the migration; rerun `test_013_two_runs_same_case_sequence_rejected` → confirm it FAILS (the duplicate is no longer caught — proving the outbox partial index is NOT what backstops it). Restore.

- [ ] **Step 9: Full gate, re-pin, commit**

```bash
./manage.sh test
# re-pin EXPECTED_ENGINE_SOURCE_HASH, then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
.venv/bin/ruff check . && .venv/bin/lint-imports
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
"""PR 7b-core: best-effort local superseded guard + honest residual risk."""

import httpx
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.postgres

from kyc_tool.outbox.publisher import enqueue_decision_callback  # noqa: E402


def _seed_two_callbacks(session_factory):
    """Case with decisions seq 1 and seq 2, each with a pending callback. seq 2's decision
    is already locally-stamped published_at (the higher delivery landed)."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c1', 2)"))
        for i, (r, seq) in enumerate([("r1", 1), ("r2", 2)], start=1):
            s.execute(
                text(
                    "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                    "actor_json, payload_json, event_sequence) VALUES (:e,'c1',:k,'h','x','{}'::jsonb,"
                    "'{}'::jsonb,:s)"
                ),
                {"e": r + "-ev", "k": r, "s": i},
            )
            s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES (:r,'c1',:e,'PUBLISH_DECISION')"),
                      {"r": r, "e": r + "-ev"})
            pub = "now()" if seq == 2 else "NULL"
            s.execute(
                text(
                    f"INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                    f"buy_enablement, policy_shas, manual, decision_sequence, published_at) "
                    f"VALUES (:d,'c1',:r,'approve',10,'{{}}'::jsonb,'enabled','{{}}'::jsonb,false,:seq,{pub})"
                ),
                {"d": "d" + r, "r": r, "seq": seq},
            )
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"case_id": "c1"}, decision_sequence=1)
        s.commit()


def test_older_callback_superseded_when_higher_locally_stamped(session_factory, settings, publisher, callback_capture):
    _seed_two_callbacks(session_factory)
    assert publisher.process_pending() == 1  # the seq-1 row is claimed then superseded, never sent
    assert callback_capture.requests == []  # ZERO HTTP for the superseded callback
    with session_factory() as s:
        row = s.execute(text("SELECT status, resolved_at, delivered_at FROM outbox WHERE case_id='c1'")).one()
        run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
    assert row.status == "superseded" and row.resolved_at is not None and row.delivered_at is None
    assert run == "COMPLETE"


def test_normal_order_all_delivered(session_factory, settings, publisher, callback_capture):
    """No higher locally-stamped delivery → the guard does not fire; the callback is sent."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c2', 1)"))
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                       "actor_json, payload_json, event_sequence) VALUES ('e','c2','k','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r','c2','e','PUBLISH_DECISION')"))
        s.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                       "policy_shas, manual, decision_sequence) VALUES ('d','c2','r','approve',10,'{}'::jsonb,"
                       "'enabled','{}'::jsonb,false,1)"))
        enqueue_decision_callback(s, case_id="c2", run_id="r", body={"case_id": "c2"}, decision_sequence=1)
        s.commit()
    assert publisher.process_pending() == 1
    assert len(callback_capture.requests) == 1
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE case_id='c2'")).scalar_one() == "delivered"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_outbox_supersession.py::test_older_callback_superseded_when_higher_locally_stamped -v`
Expected: FAIL — the seq-1 callback is delivered (an HTTP request is captured) because the guard does not exist; `status == "delivered"`, not `superseded`.

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

Add `_record_superseded` immediately after `_record_failure`:

```python
    def _record_superseded(self, row, token) -> None:
        """A higher locally-stamped delivery proved this callback obsolete: terminally
        suppress it (zero sends), fenced like every other terminal. published_at stays NULL
        (never sent); the run still reaches COMPLETE via the legal PUBLISH_DECISION edge."""
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
            if row.run_id:
                session.execute(
                    text(
                        "UPDATE runs SET state='COMPLETE', finished_at=:now "
                        "WHERE id=:run_id AND state='PUBLISH_DECISION'"
                    ),
                    {"run_id": row.run_id, "now": now},
                )
        log.info(
            "outbox_superseded", outbox_id=row.id, case_id=row.case_id,
            decision_sequence=row.decision_sequence,
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
def test_residual_risk_send_before_stamp_reverts_expected(session_factory, settings):
    """Honest boundary (§5): seq 2 sent (HTTP 2xx) but its _record_delivered is NOT
    committed (fault injected), so decisions.published_at for seq 2 is still NULL. On
    restart the seq-1 callback's guard predicate is false and seq 1 IS sent — a revert
    that is EXPECTED pre-activation (7b-activation turns this into a high-water no-op)."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c3', 2)"))
        for i, (r, seq) in enumerate([("r1", 1), ("r2", 2)], start=1):
            s.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                           "actor_json, payload_json, event_sequence) VALUES (:e,'c3',:k,'h','x','{}'::jsonb,"
                           "'{}'::jsonb,:s)"), {"e": r + "-ev", "k": r, "s": i})
            s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES (:r,'c3',:e,'PUBLISH_DECISION')"),
                      {"r": r, "e": r + "-ev"})
            # seq 2 NOT stamped (published_at NULL) — simulates send-before-stamp
            s.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                           "buy_enablement, policy_shas, manual, decision_sequence) VALUES (:d,'c3',:r,'approve',"
                           "10,'{}'::jsonb,'enabled','{}'::jsonb,false,:seq)"), {"d": "d" + r, "r": r, "seq": seq})
        enqueue_decision_callback(s, case_id="c3", run_id="r1", body={"case_id": "c3"}, decision_sequence=1)
        s.commit()

    sent = []
    pub = OutboxPublisher(
        session_factory, settings,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(1) or httpx.Response(200))),
    )
    assert pub.process_pending() == 1
    assert sent == [1]  # seq 1 WAS sent — the documented, expected pre-activation revert
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE case_id='c3'")).scalar_one() == "delivered"


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


def test_a6_contract_higher_sent_lower_zero_http(session_factory, settings, publisher, callback_capture):
    """A6 amendment (F6): the eligible (higher) callback attempts >=1 HTTP; the superseded
    (lower) callback gets ZERO HTTP and the governed superseded terminal + audit."""
    _seed_two_callbacks(session_factory)  # seq 2 already stamped; only seq 1 is enqueued
    publisher.process_pending()
    assert callback_capture.requests == []  # the lower callback: zero HTTP
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE case_id='c1'")).scalar_one() == "superseded"
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

- [ ] **Step 7: Run + full gate + re-pin + commit**

Run: `.venv/bin/pytest tests/integration/test_outbox_supersession.py -v` → PASS.
```bash
./manage.sh test
# re-pin EXPECTED_ENGINE_SOURCE_HASH, then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
.venv/bin/ruff check . && .venv/bin/lint-imports
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
    url = _fresh_db(pg, "kyc_mig_013_down_refuse")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                          "VALUES ('poc_email','c1','email','superseded', now())"))
    engine.dispose()
    with pytest.raises(Exception) as exc:  # noqa: PT011 — alembic wraps the RuntimeError
        alembic_command.downgrade(cfg, "012")
    assert "superseded outbox row" in str(exc.value)


def test_013_downgrade_race_lock_blocks_concurrent_supersede(pg):
    """Two connections: hold the downgrade txn AFTER its ACCESS EXCLUSIVE lock; a concurrent
    pending→superseded transition must BLOCK on the lock, so it cannot slip between the
    preflight and the DDL. Mutation-removing the LOCK reproduces the stranded-status race."""
    url = _fresh_db(pg, "kyc_mig_013_down_race")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engineA = create_engine(url)
    engineB = create_engine(url)
    connA = engineA.connect()
    txA = connA.begin()
    try:
        connA.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        connA.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, next_attempt_at) "
                           "VALUES ('poc_email','c1','email','pending', now())"))
        connA.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))  # emulate downgrade's first stmt
        with engineB.connect() as connB:
            connB.execute(text("SET lock_timeout = '2s'"))
            with pytest.raises(Exception):  # blocked by A's ACCESS EXCLUSIVE → lock timeout
                connB.execute(text("UPDATE outbox SET status='superseded', resolved_at=now(), "
                                   "next_attempt_at=next_attempt_at WHERE case_id='c1'"))
    finally:
        txA.rollback()
        connA.close()
    engineA.dispose()
    engineB.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_downgrade_refuses_with_superseded or 013_downgrade_race" -v`
Expected: `test_013_downgrade_refuses_with_superseded_row` FAILS — the current (Task-1/4) downgrade drops columns without any preflight, so it does NOT raise and the `"superseded outbox row"` assertion never runs. (`test_013_downgrade_race_lock_blocks_concurrent_supersede` asserts the `ACCESS EXCLUSIVE` lock semantics directly and passes now; it is the mutation anchor that must fail if the LOCK is later removed/moved.)

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

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_downgrade or 013_up_down_up" -v` → PASS (refuse test raises the stable message; the up/down/up clean test still passes on a no-superseded DB).

- [ ] **Step 5: Mutation check (manual)** — move the `op.execute("LOCK TABLE ...")` to AFTER the `EXISTS` preflight (or delete it); the race test's intent (a concurrent supersede cannot commit before the DDL) is then violable — document by asserting in review that removing/moving the LOCK reproduces the stranded-status window. Restore.

- [ ] **Step 6: Commit**

```bash
./manage.sh test
.venv/bin/ruff check . && .venv/bin/lint-imports
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
"""PR 7b-core: schema-012 pre-window backfill diagnostic + retention-race DB-lock half."""

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.postgres

from kyc_tool.db.session import make_engine, make_session_factory  # noqa: E402
from kyc_tool.ops import verify_pr7b_core_backfill as diag  # noqa: E402
from tests.integration.test_migrations import _config, _fresh_db  # noqa: E402


def _sf(url):
    return make_session_factory(make_engine(url))


def _seed_healthy(engine):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                          "actor_json, payload_json, event_sequence) VALUES ('ev','c1','r','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r','c1','ev','PUBLISH_DECISION')"))
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                          "policy_shas, manual) VALUES ('d','c1','r','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                          "VALUES ('decision_callback','c1','r','{}'::jsonb,'delivered')"))


def test_diagnostic_green_on_healthy_012(pg):
    url = _fresh_db(pg, "kyc_diag_healthy")
    alembic = _config(url)
    from alembic import command
    command.upgrade(alembic, "012")  # runs on SCHEMA 012 — no 013 columns
    engine = create_engine(url)
    _seed_healthy(engine)
    engine.dispose()
    code, ids = diag.verify_backfill(_sf(url))
    assert code == 0 and ids == []


def test_diagnostic_blocked_no_backup_sentinel(pg, capsys):
    """Missing mapping with no restorable backup: real CLI exits nonzero with the EXACT
    BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel + ids, before maintenance, no writes, and no
    014 module is invoked (7b-activation is downstream)."""
    url = _fresh_db(pg, "kyc_diag_missing")
    from alembic import command
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                          "actor_json, payload_json, event_sequence) VALUES ('ev','c1','r','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES ('r','c1','ev','PUBLISH_DECISION')"))
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                          "policy_shas, manual) VALUES ('d','c1','r','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"))
        # no callback → missing mapping
    before = engine.connect().execute(text("SELECT count(*) FROM outbox")).scalar_one()
    code, ids = diag.verify_backfill(_sf(url))
    out = capsys.readouterr().out
    assert code != 0
    assert "BLOCKED_NO_AUTHORITATIVE_MAPPING" in out  # exact sentinel, one token, no whitespace
    assert "d" in ids and "r" in " ".join(ids)
    after = engine.connect().execute(text("SELECT count(*) FROM outbox")).scalar_one()
    assert before == after  # no writes
    engine.dispose()


def test_diagnostic_share_lock_waits_on_uncommitted_delete(pg):
    """Retention-race DB-lock half (the automatable proof): connection A performs the real
    retention callback DELETE and does NOT commit; the CLI's SHARE lock must WAIT on A's
    ROW EXCLUSIVE. After A commits, the mapping is missing → nonzero + ids. Rolling A back
    → green. Mutation removing the LOCK lets the CLI snapshot before A commits and report
    green — it must fail."""
    from kyc_tool.workers.retention import prune  # the real retention seam

    url = _fresh_db(pg, "kyc_diag_race")
    from alembic import command
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_healthy(engine)  # a delivered callback + its decision
    # A: delete the delivered callback via a hand-run DELETE identical to retention's, held open
    engineA = create_engine(url)
    connA = engineA.connect()
    txA = connA.begin()
    connA.execute(text("DELETE FROM outbox WHERE status='delivered'"))  # ROW EXCLUSIVE, uncommitted
    # B: the CLI must block on its LOCK TABLE outbox IN SHARE MODE (SHARE conflicts ROW EXCLUSIVE)
    connB = create_engine(url).connect()
    connB.execute(text("SET lock_timeout = '2s'"))
    with pytest.raises(Exception):  # blocked → lock timeout while A holds
        connB.execute(text("LOCK TABLE outbox IN SHARE MODE"))
    connB.close()
    txA.commit()  # now the callback is really gone
    code, ids = diag.verify_backfill(_sf(url))
    assert code != 0 and "d" in ids  # missing mapping surfaced
    connA.close()
    engineA.dispose()
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_verify_pr7b_core_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: kyc_tool.ops.verify_pr7b_core_backfill`.

- [ ] **Step 3: Write the CLI** — create `src/kyc_tool/ops/verify_pr7b_core_backfill.py`:

```python
"""Pre-window backfill diagnostic (PR 7b-core §Rollout step 0). Run BEFORE any outage,
with the retention schedule suspended AND every active retention task terminated
(orchestrator-attested zero-running) — see docs/RUNBOOK.md. Schema-012-compatible: raw
parameterized SQL, imports NO 013-only ORM columns.

It begins its transaction with `LOCK TABLE outbox IN SHARE MODE` BEFORE any SELECT
(defense in depth: the SHARE lock waits for any in-flight DELETE's ROW EXCLUSIVE to
resolve and blocks a new outbox delete/write from invalidating the snapshot until the
diagnostic commits), then runs the exact read-only 013 preflights at READ COMMITTED.
On any violation it prints actionable decision/run/outbox ids and exits nonzero; a
missing mapping additionally prints the exact BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel.
Recovery is restore-from-authoritative-backup or remain on 012 — 014 is downstream and
cannot repair this. It NEVER writes.

    python -m kyc_tool.ops.verify_pr7b_core_backfill
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory

_BLOCKED = "BLOCKED_NO_AUTHORITATIVE_MAPPING"


def verify_backfill(session_factory) -> tuple[int, list[str]]:
    """Return (exit_code, offending_ids). Takes SHARE lock first; no writes."""
    offenders: list[str] = []
    with session_factory() as s:
        s.execute(text("LOCK TABLE outbox IN SHARE MODE"))  # BEFORE any SELECT
        missing = s.execute(
            text(
                "SELECT d.id AS decision_id, d.run_id FROM decisions d WHERE d.manual = false "
                "AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.kind='decision_callback' "
                "AND o.run_id = d.run_id)"
            )
        ).fetchall()
        orphan = s.execute(
            text(
                "SELECT o.id, o.run_id FROM outbox o WHERE o.kind='decision_callback' "
                "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.manual=false AND d.run_id = o.run_id)"
            )
        ).fetchall()
        dup = s.execute(
            text(
                "SELECT run_id, count(*) AS c FROM outbox WHERE kind='decision_callback' "
                "GROUP BY run_id HAVING count(*) > 1"
            )
        ).fetchall()
        s.rollback()  # read-only: never hold locks past the check, never write

    if missing:
        ids = [str(r.decision_id) for r in missing] + [str(r.run_id) for r in missing]
        print(f"{_BLOCKED} missing decision_callback for decisions/runs: "
              f"{[(r.decision_id, r.run_id) for r in missing]}")
        offenders += ids
    if orphan:
        print(f"orphan decision_callback outbox rows (no automatic decision): "
              f"{[(r.id, r.run_id) for r in orphan]}")
        offenders += [str(r.id) for r in orphan]
    if dup:
        print(f"multiple decision_callback rows per run: {[(r.run_id, r.c) for r in dup]}")
        offenders += [str(r.run_id) for r in dup]
    return (1 if offenders else 0), offenders


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    code, offenders = verify_backfill(session_factory)
    if code == 0:
        print("verify_pr7b_core_backfill: OK (every automatic decision maps to one callback)")
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/integration/test_verify_pr7b_core_backfill.py -v` → PASS.

- [ ] **Step 5: Mutation check (manual)** — remove the `s.execute(text("LOCK TABLE outbox IN SHARE MODE"))` line; rerun `test_diagnostic_share_lock_waits_on_uncommitted_delete` and confirm the intent breaks (without the SHARE lock the CLI would not have to wait on A). Restore.

- [ ] **Step 6: Full gate, re-pin, commit**

```bash
./manage.sh test
# re-pin EXPECTED_ENGINE_SOURCE_HASH (new src file), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
.venv/bin/ruff check . && .venv/bin/lint-imports
git add src/kyc_tool/ops/verify_pr7b_core_backfill.py tests/integration/test_verify_pr7b_core_backfill.py \
        tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(ops): verify_pr7b_core_backfill pre-window diagnostic (SHARE-locked, schema-012)

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
"""PR 7b-core: post-013 reset_interrupted_outbox_claims + rollout-order invariants."""

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.postgres

from kyc_tool.db.session import make_engine, make_session_factory  # noqa: E402
from kyc_tool.ops import reset_interrupted_outbox_claims as resetter  # noqa: E402
from tests.integration.test_migrations import _config, _fresh_db  # noqa: E402


def _sf(url):
    return make_session_factory(make_engine(url))


def test_reset_refuses_pre_013_schema(pg):
    url = _fresh_db(pg, "kyc_reset_pre013")
    from alembic import command
    command.upgrade(_config(url), "012")  # no claim_token column
    with pytest.raises(Exception) as exc:  # noqa: PT011
        resetter.reset_claims(_sf(url))
    assert "pre-013" in str(exc.value).lower() or "claim_token" in str(exc.value).lower()


def test_reset_clears_only_claimed_preserves_next_attempt(pg):
    url = _fresh_db(pg, "kyc_reset_013")
    from alembic import command
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        # a claimed row (complete claim tuple) with a future next_attempt_at to preserve
        conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
            "claim_lease_expires_at, claimed_by, next_attempt_at) VALUES ('poc_email','c1','email','pending', "
            "gen_random_uuid(), now(), 'w1', now() + interval '5 minutes')"
        ))
        # a plain backoff row (no claim) whose next_attempt_at must NOT be touched
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','email','pending', now() + interval '9 minutes')"))
        na_before = [r.next_attempt_at for r in conn.execute(text("SELECT next_attempt_at FROM outbox ORDER BY id"))]

    n = resetter.reset_claims(_sf(url))
    assert n == 1  # only the claimed row was cleared
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT claim_token, claim_lease_expires_at, claimed_by, next_attempt_at "
                                 "FROM outbox ORDER BY id")).all()
    assert rows[0].claim_token is None and rows[0].claim_lease_expires_at is None and rows[0].claimed_by is None
    na_after = [r.next_attempt_at for r in rows]
    assert na_after == na_before  # next_attempt_at preserved on BOTH rows
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_reset_interrupted_outbox_claims.py -v`
Expected: FAIL — `ModuleNotFoundError: kyc_tool.ops.reset_interrupted_outbox_claims`.

- [ ] **Step 3: Write the CLI** — create `src/kyc_tool/ops/reset_interrupted_outbox_claims.py`:

```python
"""Post-013 outbox claim reset (PR 7b-core §Rollout). Run ONCE after ALL outbox
publishers are confirmed stopped and none have restarted, for post-013 future stops and
the post-013 rollback path ONLY. It refuses pre-013 schema (the claim columns don't exist
yet), clears only rows with the COMPLETE claim tuple, PRESERVES next_attempt_at (an
interrupted old claim simply waits until its already-recorded due time), and read-back-
asserts zero claim tuples. It MUST NOT run while any publisher is live.

    python -m kyc_tool.ops.reset_interrupted_outbox_claims
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def reset_claims(session_factory) -> int:
    """Clear every complete outbox claim tuple; return the count. Refuses pre-013."""
    with session_factory() as session:
        has_col = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='outbox' AND column_name='claim_token'"
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
        session.commit()
    if remaining != 0:
        raise RuntimeError(f"{remaining} outbox claim tuple(s) remain after reset — publishers not stopped?")
    return count


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    n = reset_claims(session_factory)
    print(f"reset {n} interrupted outbox claim(s); 0 claim tuples remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/integration/test_reset_interrupted_outbox_claims.py -v` → PASS.

- [ ] **Step 5: Full gate, re-pin, commit**

```bash
./manage.sh test
# re-pin EXPECTED_ENGINE_SOURCE_HASH (new src file), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
.venv/bin/ruff check . && .venv/bin/lint-imports
git add src/kyc_tool/ops/reset_interrupted_outbox_claims.py \
        tests/integration/test_reset_interrupted_outbox_claims.py tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(ops): reset_interrupted_outbox_claims (post-013-only; preserves next_attempt_at)

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

- [ ] **Step 4: Write `OVERVIEW.md`** — add a "PR 7b-core — outbox stream separation + local decision ordering" subsection covering: per-`(case_id, ordering_stream)` FIFO (a stuck stream never blocks the other); the internal `decision_sequence` (allocated under the Case lock, NOT on the wire — the callback body is byte-identical to pre-7b); the **best-effort** local `superseded` guard (fires only on a higher *locally-stamped* delivery); the fenced claim (`claim_token`); and the explicit boundary: **send-before-stamp and cross-replica reverts remain until 7b-activation (`014`)** — 7b-core does not close defect 2 fully.

- [ ] **Step 5: Write `RUNBOOK.md` + `DEPLOYMENT.md`** — add the full, non-abbreviated procedure (mirror the exact order in both):

  **Step 0 — pre-window (before any outage):** (i) **suspend the retention schedule**; (ii) **terminate/wait for every active retention task**; (iii) **capture target-orchestrator zero-running evidence** — the exact ECS/Fargate or EC2 command + expected output; if the production substrate is not yet chosen, this is a blocking **`TODO(integration)`** deployment prerequisite (a pytest does NOT prove it — record the exact command + output here when the substrate is fixed); (iv) run the digest-pinned `python -m kyc_tool.ops.verify_pr7b_core_backfill` with the schedule still suspended (valid ONLY while suspended AND the zero-running attestation holds); (v) **on failure, abort here — before stopping service.** **Recovery — restore-or-block:** restore the exact callback from authoritative backup and rerun clean, OR remain on 012 in `BLOCKED_NO_AUTHORITATIVE_MAPPING` (backup availability is an operator prerequisite; **014 is downstream and cannot repair this**; never fabricate a callback, delete a decision, or fall back to `decided_at`). On **every** abort path, explicitly re-enable or deliberately keep-frozen retention — never leave it silently disabled.

  **Cutover:** (1) pause submission, edge-block composer, disable autoscaling/restarts; (2) hard-stop API/pipeline/outbox/`dev_worker` (queue **and** outbox)/retention/every writer, attest zero at the orchestrator; (3) run the shipped `python -m kyc_tool.ops.requeue_interrupted_jobs` — **no outbox reset here** (pre-013 has no claim columns; an interrupted old claim waits until its recorded `next_attempt_at`); **preserve every pending row's `next_attempt_at`**; (4) `alembic upgrade head` (013) — repeats the §0 preflights under the zero-writer boundary (the authoritative fail-closed check); (5) start API only, probe `/readyz`, then start+attest the fenced workers — **no mutating prod smoke**; (6) resume.

  **Rollback — a full ordered maintenance procedure (as drained as the forward cutover):** (1) pause submissions, disable autoscaling/restarts; (2) hard-stop + orchestrator-attest zero API/pipeline/outbox/`dev_worker`/retention/every writer; (3) **while 013 still exists**, run `python -m kyc_tool.ops.reset_interrupted_outbox_claims` (post-013-only; clears complete claim tuples, preserves `next_attempt_at`, read-back-asserts zero) and verify zero claim tuples; (4) `alembic downgrade` (its `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE` + `superseded` preflight refuses byte-stably if any superseded row exists — then rollback stays on a 7b-core-compatible image and is a forward fix); (5) deploy the pre-7b image **only after** the downgrade succeeds. Redeploying the pre-7b image *before* 013 is applied is also safe. Include the preflight query `SELECT count(*) FROM outbox WHERE status='superseded'`.

- [ ] **Step 6: Extend `AUDIT_FINDINGS.md`** — confirm the A6 exception from Task 5 is present; add a short note that the 013 backfill's order authority is `outbox.id` (the under-lock enqueue serialization for rows the guard can deliver) — a **deterministic reconstruction, not proof of original publication order** — and that a legacy automatic decision without a surviving callback is a **fail-closed migration refusal** (restore-or-block; `BLOCKED_NO_AUTHORITATIVE_MAPPING`). State the local/cross-replica boundary explicitly.

- [ ] **Step 7: Full gate + commit**

```bash
./manage.sh test          # includes test_migration_lineage + the whole 013 suite
.venv/bin/ruff check . && .venv/bin/lint-imports
git add docs/OVERVIEW.md docs/RUNBOOK.md docs/DEPLOYMENT.md AUDIT_FINDINGS.md .agents/ROADMAP.md
git commit -m "docs(7b-core): stream-separation overview, cutover/rollback runbook, ROADMAP shipped

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

Every spec section maps to a task: §1 Migration 013 → Tasks 1 (columns/stream/case_id/lifecycle/index), 2 (backfill), 4 (identity constraints), 6 (race-safe downgrade); §2 fenced claim → Task 3; §3 decision-sequence allocation → Task 4; §4 guard + fenced terminals → Tasks 3 (terminals) + 5 (guard/superseded); §5 scope boundary + residual risk → Task 5 (residual-risk test) + Task 9 (docs); AUDIT:A6 amendment → Task 5; §Rollout (verify CLI, requeue, reset CLI, restore-or-block) → Tasks 7, 8, 9; §Testing strategy items → distributed across Tasks 1-8 with a mutation witness each; §Docs+governance → Task 9; §Out-of-scope respected (no wire field, no `ENGINE_BUILD_ID` bump, M2 untouched). The one deliberate structural deviation from the spec's §1 grouping — splitting the decision-identity constraints out of Task 1 into Task 4 (co-located with the pipeline allocation that satisfies them) — is required by the green-at-every-commit rule (`conftest.migrated` upgrades to head for the whole suite, so an identity CHECK cannot precede the code that writes `decision_sequence`); the migration file is still a single `013` and every constraint/backfill/downgrade element is present.
