# PR 7b-core — Outbox stream separation + local decision ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The parent session is the SOLE committer; subagents implement + hand diffs back.

> **Plan revision: rev 6 (2026-07-24)** — folds Codex's rev-5 complete-unit re-review @ `0ca264b`
> (4 findings) plus 2 task-review items; see "Codex plan-review round 6" at the bottom. Rev 5
> folded all 10 findings of the Codex complete-unit re-audit @ `eac3035` (`AGENT_BUS.md` PLAN-REVIEW 2026-07-24): restore acceptance contract (F1), composite decisions→runs case FK (F2), the four affected EXISTING test files claimed + adapted (F3), the third residual (manual-current vs late automatic callback) + its real test (F4), downgrade-TOCTOU autobegin fix (F5), same-stream FIFO-under-backoff proof (F6), retry-branch fencing proof (F7), `superseded` is decision-only (F8), ORM FK parity for `fk_outbox_case_id` (F9), required-keyword `decision_sequence` final signature (F10). See "Codex plan-review round 5" at the bottom.

**Goal:** Separate the outbox claim into per-`(case_id, ordering_stream)` FIFO streams, add an internal per-case `decision_sequence`, fence every claim with a `claim_token`, add a best-effort local `superseded` guard, and lock all of it down with exhaustive relational integrity — shipped as migration `013` with a drained cutover and a reversible-before-first-supersession downgrade.

**Architecture:** Migration `013` (`down_revision='012'`) adds `outbox.ordering_stream`/`case_id NOT NULL`, a fenced-claim column set (`claim_token`/`claim_lease_expires_at`/`claimed_by`), `decisions.decision_sequence` + `cases.last_decision_sequence`, a triple FK binding each callback to its automatic decision, and exhaustive per-status lifecycle CHECKs. The publisher claims the min-id pending row of one `(case, stream)` under a fresh `claim_token`; every terminal (`_record_delivered`/`_record_failure`/`_record_superseded`) is a single fenced `UPDATE … WHERE id AND status='pending' AND claim_token=:token RETURNING id` whose winner performs all dependent writes in-transaction and whose stale loser emits `outbox_stale_claim_completion` and does nothing. The decide transaction allocates `decision_sequence` from the locked `cases.last_decision_sequence` counter; a local guard suppresses an older requeued callback only when a higher-sequence decision already carries a locally-stamped `published_at`.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 (sync psycopg), Postgres, Alembic, import-linter, pytest against ephemeral Postgres (`tests/pg.py`).

**Spec:** `.agents/superpowers/specs/2026-07-22-pr7b-core-outbox-stream-separation-design.md` (rev 9 — rev 8 REVIEW-CLEAN + the rev-5 re-audit amendments for findings 1, 2, 4, 8). It is the authoritative contract — read the cited §sections for rationale; every CHECK, SQL, seam, and mutation witness below is drawn from it.

## Global Constraints

- **Python 3.11, FastAPI, SQLAlchemy 2 (sync psycopg), Postgres, Alembic.** Tests run against real ephemeral Postgres — the session-scoped `pg` fixture (`tests/pg.py`) self-provisions a cluster, so `.venv/bin/pytest <selector>` runs real-Postgres tests with no extra setup.
- **Test commands.** `./manage.sh test` runs the WHOLE suite (it ignores path args — `.substrate/lib.sh:214` `sub_test` → `test_python` runs bare `pytest`). Targeted red→green in each step uses **`.venv/bin/pytest <selector> -v`**. The final gate per task runs `./manage.sh test`.
- **Lint gate.** `.venv/bin/ruff check .` (rules `E,F,I,UP,B,SIM`; **no `;`/E702 multi-statement lines**) **and** `.venv/bin/lint-imports` (import-linter, **2 contracts kept / 0 broken**). New code lives in `db`/`outbox`/`orchestration`/`events`/`workers`/`api`/`ui`/`ops` — never add an import into `domain`/`validators`/`policy`/`adapters`, so both contracts stay green. **Never run `./manage.sh fmt`.**
- **CANONICAL CLOSE-OUT ORDER for any `src/kyc_tool/**`-touching task (findings 6):** the whole-source drift guard (`test_engine_source_hash_pinned`) fails on ANY `src/` edit, so `./manage.sh test` returns nonzero until the hash is re-pinned. Every such task's close-out therefore runs **in this exact order — never `./manage.sh test` before the re-pin**: (1) run the task's targeted `.venv/bin/pytest` selectors green; (2) run the drift-guard test once to observe it RED (the deliberately-red TDD step — label it RED, never a gate); (3) compute+paste the new `EXPECTED_ENGINE_SOURCE_HASH` and re-run the drift-guard test GREEN; (4) `./manage.sh test` → exit 0; (5) `.venv/bin/ruff check .` → exit 0; (6) `.venv/bin/lint-imports` → exit 0; (7) **checkpoint or commit — see below**. **Tasks 1-6 are worktree CHECKPOINTS that end at step (6) with `git status` (NO commit); the single atomic `013`+runtime commit is made at the end of Task 6 (F1).** Tasks 7-9 (and Task 6's final step) commit normally. Tasks that touch only `alembic/`, docs, or `.agents/` skip steps 2-3 and run `./manage.sh test` directly.
- **Exact exception types in tests (finding 9).** Never assert a bare `pytest.raises(Exception)` on an authoritative refusal. Use: `sqlalchemy.exc.IntegrityError` for CHECK / FK / unique / NOT-NULL violations; `sqlalchemy.exc.OperationalError` with `exc.value.orig.sqlstate == "55P03"` for a `lock_timeout` (psycopg3 exposes `.sqlstate` on `.orig`); `RuntimeError` for a migration/CLI fail-closed refusal raised in Python (`alembic.command.upgrade`/`downgrade` propagate the migration's `RuntimeError` unwrapped) — always paired with an assertion on the message substring. Match a `lock_timeout` on a real statement with `conn.execute(text("SET lock_timeout='2s'"))` first.
- **Delivery-layer only — NO scoring/gate/decision semantic change.** The callback HTTP body stays **byte-identical** to pre-7b: `decision_sequence` is written to the `decisions`/`outbox` **columns only**, never added to `payload_json` or the wire (that is 7b-activation / `014`).
- **Engine drift guard.** Every task that edits any file under `src/kyc_tool/**` MUST re-pin `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py` **in the same commit** (see the re-pin one-liner in "Test infrastructure" below). **Do NOT bump `ENGINE_BUILD_ID`** — this is not a scoring change. The src-touching tasks are **1, 2 (the frozen `migration_contracts/v013_backfill.py`), 3, 4, 5, 7, 8** — each re-pins. Tasks that touch only `alembic/`, docs, or `.agents/` (parts of 6, 9) do NOT re-pin (the guard closure is `src/kyc_tool/**/*.py` only).
- **Do NOT edit `KYC_Tool_Build_Package/`** (normative spec, immutable) or anything touching **M2** / `KYC_ENFORCE_POSITIVE_DECISIONS` / `enforce_positive_decisions`.
- **ROADMAP lineage.** `.agents/ROADMAP.md §C` already reserves PR 7b-core = `013` (row 74, State `pending`) and 7b-activation = `014`. Do **not** renumber or edit the reservations except to flip 7b-core's State `pending → shipped`, which `tests/unit/test_migration_lineage.py` forces as soon as `alembic/versions/013_*.py` exists on disk: make the flip in **Task 1 Step 3b** and commit it in **Task 6's atomic `013` commit** (both files are read off disk, and CI checks out the commit, not the worktree). `tests/unit/test_migration_lineage.py` must stay green from Task 1's checkpoint onward.
- **Codex build constraints (carry verbatim):** migration/outbox/CLI mutation proofs run against **real Postgres and the real CLI entry points** (`main()` / `python -m …`), never a helper-only shim. The retention "zero active tasks before it deletes" attestation is a **runbook / `TODO(integration)`** deployment acceptance, **NOT** a pytest (retention is a scheduled one-shot with no in-repo liveness registry).
- **Branch:** work on `claude/project-setup-verify-kpfgjs`. **Never put any model identifier in artifacts.**
- **Commit trailer on EVERY commit:**
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK
  ```

## Execution protocol (parent session — bus lifecycle & gates, F8)

**Human plan-approval gate (required first).** PLAN-CLEAN / AUDIT-CLEAN on this plan is a *plan* gate, **not** implementation permission. Do **not** begin the sequence below until the human explicitly approves execution.

- [ ] **Step 0 (parent-only, BEFORE Task 1) — CLAIM.** `git pull`; read `AGENT_BUS.md` and `.agents/ROADMAP.md`; verify **no conflicting active Claude CLAIM** overlaps this work. Then post + push **one** bus CLAIM covering the final file set — the migration `alembic/versions/013_outbox_stream_separation.py`, `src/kyc_tool/migration_contracts/**`, `src/kyc_tool/{db/tables.py,outbox/publisher.py,orchestration/pipeline.py,workers/retention.py,api/routes_metrics.py,ops/verify_pr7b_core_backfill.py,ops/reset_interrupted_outbox_claims.py}`, the new/edited `tests/**` — **naming explicitly (re-audit F3) the four EXISTING files this unit adapts: `tests/integration/test_ui.py`, `tests/integration/test_phase4_platform.py`, `tests/integration/test_migrations.py`, `tests/integration/test_bundle_pinning_ops.py`, plus `tests/conftest.py` (shared valid-chain helper)** — `docs/{OVERVIEW,RUNBOOK,DEPLOYMENT}.md`, `AUDIT_FINDINGS.md`, `.agents/ROADMAP.md`, and the re-pinned `tests/policy_driven/test_engine_build_id_guard.py`. Only then begin Task 1.
- The **parent session is the sole committer/pusher/bus-writer.** Subagents implement and hand diffs back; Tasks 1-6 accumulate one commit (end of Task 6); Tasks 7-9 commit normally (see each task).
- [ ] **After Task 9 — RELEASE + audit hold.** Commit the code anchor first; rerun the exact **Final gate** commands on that SHA; then post a **separate** bus RELEASE that names: the literal anchor commit/range, the commands + results, the migration witnesses (single-`013`-commit finish gate; up/down/up; backfill order authority; downgrade race), the honest residual-risk boundary (send-before-stamp / cross-replica / manual-current-then-late-automatic-callback remain until 7b-activation), and "**M2 / `KYC_Tool_Build_Package/` untouched**"; set `turn: CODEX`. Push and **hold for `AUDIT-CLEAN`** before considering the work done.

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

## Build order: Tasks 1-6 accumulate ONE atomic `013` commit (F1)

Migration `013` is built up across Tasks 1-6 in the **worktree**, but is **committed to git exactly once — at the end of Task 6** — as a single atomic *schema + runtime* commit. This is mandatory: Alembic records a revision by its id, so if Task 1 committed `013` in its blank-insertion-point form and a persistent DB applied it, later tasks editing the SAME revision id would leave that DB at `current==head==013` and it would `alembic upgrade head` to a **no-op**, silently missing the backfill / triple FK / race-safe downgrade. Fresh ephemeral DBs hide this (each test replays the final file from `012`); a real deployment would not.

Therefore **Tasks 1-6 are non-committing worktree TDD/review checkpoints** (each still runs the full close-out gates green so a reviewer can inspect the accumulated diff), and the parent makes the **single** `013` + runtime commit at the end of Task 6. Tasks 7, 8, 9 commit normally. `conftest.migrated` upgrades a fresh DB to **head** for the whole suite, so any constraint `013` adds is live for every test; a constraint that *requires a column value* can only land once the code that *writes* that value is in place, and columns must exist first. Hence the accumulation order:

- **Task 1** (checkpoint) — `013`'s columns + the outbox stream/case_id/lifecycle CHECKs (none reference `decision_sequence`) + enqueue funcs set `ordering_stream`; ORM columns. Suite green.
- **Task 2** (checkpoint) — the FROZEN backfill contract (`src/kyc_tool/migration_contracts/v013_backfill.py`, run by BOTH the migration and the Task-7 CLI) + the legacy `decision_sequence` backfill (touches only legacy rows; no new-row CHECK yet).
- **Task 3** (checkpoint) — fences the claim + terminals (the `claim_token` is set by the claim and cleared by every terminal in the same step — they cannot split without violating the lifecycle CHECK).
- **Task 4** (checkpoint) — pipeline allocates `decision_sequence` **and**, in the same step, adds the decision-identity constraints (kind/stream identity CHECK, `manual`/`automatic` CHECK, the three decisions uniques, the triple FK, the partial callback unique) — now both legacy (backfilled) and live (allocated) rows satisfy them.
- **Task 5** (checkpoint) — local guard + `superseded` lifecycle wiring.
- **Task 6** (**the single commit**) — hardens `013`'s downgrade (LOCK + `superseded` preflight), then commits everything accumulated in Tasks 1-6 as one atomic schema+runtime commit + runs the finish gate.
- **Tasks 7–8** commit the two ops CLIs normally; **Task 9** commits docs only (the ROADMAP flip already rode Task 6's atomic `013` commit — see Task 9's intro).

Each checkpoint edits `alembic/versions/013_outbox_stream_separation.py` at clearly marked insertion points; every test run re-applies `013` from scratch on a fresh DB, so cross-checkpoint edits to one migration file are safe, and the single final commit is the only `013` in git history. **The engine-drift guard is re-pinned at each checkpoint too** (it is a worktree edit, not a commit), so `./manage.sh test` stays green for review; the final re-pinned hash rides the Task-6 commit.

## File Structure

**Create:**
- `alembic/versions/013_outbox_stream_separation.py` — the single migration (`down_revision='012'`): all columns, backfills, constraints, index, and the race-safe downgrade. Authored across Tasks 1, 2, 4, 6.
- `src/kyc_tool/migration_contracts/v013_backfill.py` — the shared schema-012 parity matrix (`PARITY_CHECKS` + `run_parity`) run by BOTH migration 013's preflight and the diagnostic CLI (Task 2).
- `src/kyc_tool/ops/verify_pr7b_core_backfill.py` — schema-012-compatible, `SHARE`-locked, read-only pre-window diagnostic CLI (Task 7).
- `src/kyc_tool/ops/reset_interrupted_outbox_claims.py` — post-013-only claim-tuple reset CLI (Task 8).
- `tests/integration/test_outbox_fencing.py` — stream-scoped fenced claim + fenced terminals (Task 3).
- `tests/integration/test_decision_sequence.py` — allocation + concurrency + manual-approve (Task 4).
- `tests/integration/test_outbox_supersession.py` — local guard, residual-risk, retention prune, metrics, UI-409, A6 (Task 5).
- `tests/integration/test_verify_pr7b_core_backfill.py` — the diagnostic CLI (Task 7).
- `tests/integration/test_reset_interrupted_outbox_claims.py` — the reset CLI + rollout order (Task 8).

**Modify:**
- `src/kyc_tool/db/tables.py` — `Outbox` (+`ordering_stream`, `decision_sequence`, `resolved_at`, `claim_lease_expires_at`, `claim_token`, `claimed_by`; `case_id` non-optional + named FK `fk_outbox_case_id` (F9); `status` comment; `__table_args__`), `Case` (+`last_decision_sequence`), `Run` (+`__table_args__` with `uq_runs_id_case_id` — F2), `DecisionRow` (+`decision_sequence`, `__table_args__` uniques + `fk_decisions_run_case` — F2). (Tasks 1, 4.)
- `src/kyc_tool/outbox/publisher.py` — `_CLAIM_SQL` (fenced, per-stream, `RETURNING` token/stream/sequence), `enqueue_decision_callback`/`enqueue_poc_email` (set `ordering_stream`, `decision_sequence`), `process_once` (thread token + guard), `_record_delivered`/`_record_failure` (fenced winner/loser), `_record_superseded` (new), module docstring (A6). (Tasks 1, 3, 5.)
- `src/kyc_tool/orchestration/pipeline.py` — `_decide_txn` allocates `decision_sequence` from `cases.last_decision_sequence` and passes it to `enqueue_decision_callback`. (Task 4.)
- `src/kyc_tool/workers/retention.py` — `prune`'s outbox delete narrows to `kind='poc_email'`; `decision_callback` rows become the non-prunable durable ordering authority. (Task 5.)
- `src/kyc_tool/api/routes_metrics.py` — report `superseded` separately; keep it out of pending/dead alerting. (Task 5.)
- `tests/integration/test_migrations.py` — all `013` migration up/down/negative/backfill/downgrade-race tests + the schema-012 restore acceptance test (Tasks 1, 2, 4, 6, 7) + the F3 legacy-fixture flips (`manual=true`, named nonblank constraints) (Task 1).
- `tests/integration/test_ui.py` — F3: the raw dead-POC fixture INSERT gains `ordering_stream='email'` (Task 1).
- `tests/integration/test_phase4_platform.py` — F3: the redelivery test becomes the real send-before-stamp fault injection (interim legalization Task 1; final form Task 3).
- `tests/conftest.py` — F3: shared `seed_automatic_decision` valid-chain helper (Task 4).
- `tests/integration/test_bundle_pinning_ops.py` — F3: the three automatic-decision fixtures gain valid sequences + case counters via the shared helper (Task 4).
- `tests/policy_driven/test_engine_build_id_guard.py` — re-pin `EXPECTED_ENGINE_SOURCE_HASH` (every src-touching task).
- `docs/OVERVIEW.md`, `docs/RUNBOOK.md`, `docs/DEPLOYMENT.md`, `AUDIT_FINDINGS.md` — docs + governance (Task 9). `.agents/ROADMAP.md` — §C State flip (edited Task 1 Step 3b, committed Task 6).

**Do NOT touch** (confirm unchanged): `src/kyc_tool/events/ingest.py` `_handle_manual_approve` (it already writes `run_id=None`, `manual=True`, no callback, and no `decision_sequence` — Task 4 only adds a test proving it allocates none); `src/kyc_tool/ui/routes.py` `requeue_outbox` (already 409s every non-`dead` row incl. `superseded` — Task 5 only adds a confirming test).

---

## Task 1: Migration 013 part A — outbox stream separation, `case_id NOT NULL`, lifecycle + claim CHECKs, ORM, enqueue `ordering_stream`

Adds every `013` column, backfills `ordering_stream` from `kind`, makes `case_id`/`ordering_stream` real `NOT NULL` columns, and lands the outbox CHECKs that do **not** reference `decision_sequence` (closed `kind` vocab, `ordering_stream` vocab, exhaustive per-status lifecycle + claim-tuple). The enqueue funcs set `ordering_stream` so the live suite stays green under the new `NOT NULL`. The decision-identity constraints and the `decision_sequence` backfill are deliberately deferred (Tasks 2, 4).

**Files:**
- Create: `alembic/versions/013_outbox_stream_separation.py`
- Modify: `src/kyc_tool/db/tables.py` (`Outbox` 261-279, `Case` 34-54, `DecisionRow` 211-227)
- Modify: `src/kyc_tool/outbox/publisher.py` (`enqueue_decision_callback`/`enqueue_poc_email` 52-63)
- Modify: `tests/integration/test_migrations.py` (013 tests + the F3 legacy-fixture flips, Step 8b)
- Modify: `tests/integration/test_ui.py` (F3: `ordering_stream` on the raw dead-POC INSERT, Step 8b)
- Modify: `tests/integration/test_phase4_platform.py` (F3: interim lifecycle-legal redelivery rewrite, Step 8b — replaced by the real fault injection in Task 3 Step 3b)
- Modify: `.agents/ROADMAP.md` (§C State flip, Step 3b — forced by the new migration file; committed in Task 6)
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `alembic` revision `013` (`down_revision='012'`) with columns `outbox.{ordering_stream TEXT NOT NULL, decision_sequence BIGINT NULL, resolved_at TIMESTAMPTZ NULL, claim_lease_expires_at TIMESTAMPTZ NULL, claim_token UUID NULL, claimed_by TEXT NULL}`, `outbox.case_id` now `NOT NULL` + FK `fk_outbox_case_id`, `decisions.decision_sequence BIGINT NULL`, `cases.last_decision_sequence BIGINT NOT NULL DEFAULT 0`; constraints `ck_outbox_kind_vocab`, `ck_outbox_ordering_stream_vocab`, `ck_outbox_status_lifecycle`; claim indexes `ix_outbox_stream_claim` and (replacing 006's full form) `ix_outbox_claim`, both **partial** to `status='pending'`.
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
        text("INSERT INTO runs (id, case_id, triggering_event_id, "
             "state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
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
                          "buy_enablement, policy_shas, manual) VALUES "
                          "('dm','c1',NULL,'approve',0,'{}'::jsonb,"
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
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

# superseded is a DECISION-ONLY terminal (re-audit F8): production supersedes only decision
# callbacks; A6's zero-send exception and the ordering proof are callback-specific, so a
# poc_email must never be able to enter 'superseded' (not even by operator UPDATE).
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
    AND (status <> 'superseded' OR kind = 'decision_callback')
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

    # --- claim indexes: PARTIAL to the claimable set (re-review 99df3d2 F3) ---
    # 013 makes decision_callback rows non-prunable (the durable ordering authority), so
    # delivered/superseded terminals accumulate without bound. A full index would grow forever and
    # drag that dead weight into every claim plan. `status` leaves the key because the predicate
    # pins it. The legacy 006 index is replaced on the same reasoning.
    op.create_index(
        "ix_outbox_stream_claim", "outbox",
        ["case_id", "ordering_stream", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_outbox_claim", table_name="outbox")
    op.create_index(
        "ix_outbox_claim", "outbox", ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    # Task 6 replaces this body with a LOCK TABLE + superseded preflight before the drops.
    op.drop_index("ix_outbox_stream_claim", table_name="outbox")
    op.drop_index("ix_outbox_claim", table_name="outbox")   # restore 006's full form
    op.create_index("ix_outbox_claim", "outbox", ["status", "next_attempt_at"])
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

- [ ] **Step 3b: Flip the ROADMAP §C State (forced by the new file on disk)** — `tests/unit/test_migration_lineage.py::test_roadmap_lineage_consistent_with_alembic` asserts `authored_reserved == shipped` and `head ∈ shipped`, reading BOTH `.agents/ROADMAP.md` and `alembic/versions/` off disk. Creating `013_outbox_stream_separation.py` in Step 3 therefore breaks it immediately — it is not deferrable to Task 9. Observe the failure, then flip:

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py::test_roadmap_lineage_consistent_with_alembic -v`
Expected: **FAIL** — `live Alembic head 013 is not a shipped reservation` (the 7b-core row still reads `pending`).

In `.agents/ROADMAP.md §C`, change the PR 7b-core row's State cell `pending → shipped` (see Task 9 Step 2 for the exact final row text; leave 7b-activation `014` and every other row untouched). Also update the §C prose note near lines 12-14 if it says "7b-core is next" → "7b-core shipped (`013`); 7b-activation (`014`) is next".

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py -v`
Expected: PASS.

**This file is staged in Task 6's atomic `013` commit, not Task 9's** — the guard reads the worktree, but CI checks out the commit, so a ROADMAP flip landing in a later commit than the migration leaves Task 6's commit red.

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

def test_013_claim_indexes_are_partial_to_the_claimable_set(pg):
    """re-review 0ca264b F3: 013 makes decision_callback rows non-prunable, so `outbox` grows
    without bound. Both claim indexes must therefore be PARTIAL to `status='pending'` — otherwise
    an ever-growing tail of delivered/superseded terminals enters the claim path's index and its
    plan. Pins the predicates from pg_indexes so a full index cannot creep back, and asserts the
    downgrade restores 006's full form."""
    url = _fresh_db(pg, "kyc_mig_013_partial_idx")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.connect() as conn:
        defs = {
            r.indexname: r.indexdef
            for r in conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='outbox'"))
        }
    for name in ("ix_outbox_claim", "ix_outbox_stream_claim"):
        assert "WHERE (status = 'pending'::text)" in defs[name], defs[name]
    # status is pinned by the predicate, so it must not also sit in the key
    assert "status" not in defs["ix_outbox_stream_claim"].split(" WHERE ")[0]

    alembic_command.downgrade(cfg, "012")
    with engine.connect() as conn:
        legacy = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename='outbox' "
            "AND indexname='ix_outbox_claim'")).scalar_one()
    assert "WHERE" not in legacy and "status" in legacy  # 006's full (status, next_attempt_at)
    engine.dispose()


# INSERT negatives: each row is otherwise valid; only the constrained column is bad. Each case
# pins the constraint (or NOT NULL message) it is INTENDED to trip — same discipline as
# _NONBLANK_SURFACES. NOTE (Task 4): ck_outbox_kind_stream_identity pins kind AND ordering_stream
# to literals in both branches, so it SUBSUMES both vocab CHECKs — no seed row can violate a vocab
# CHECK alone, and Postgres reports the alphabetically-first name. The two vocab cases therefore
# pin the identity constraint. Consequence recorded for final review: the vocab CHECKs are now
# unguarded by any test.
# Without the pin, a later-task constraint that rejects the row for an
# unrelated reason (Task 4's kind/stream identity CHECK rejects every ('poc_email','decision')
# shape) would keep these green while the constraint under test silently disappeared.
_OUTBOX_LIFECYCLE_BAD = {
    "unknown_kind": ("ck_outbox_kind_stream_identity",  # subsumes ck_outbox_kind_vocab (Task 4)
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('unknown','c1','decision','pending')"),
    "null_stream": ('null value in column "ordering_stream"',
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1',NULL,'pending')"),
    "unknown_stream": ("ck_outbox_kind_stream_identity",  # subsumes the stream vocab CHECK (Task 4)
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','carrier-pigeon','pending')"),
    "orphan_case": ("fk_outbox_case_id",
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','nope','email','pending')"),
    "null_case": ('null value in column "case_id"',
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email',NULL,'email','pending')"),
    "pending_delivered_at": ("ck_outbox_status_lifecycle",  # pending ⇒ delivered_at NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at) "
        "VALUES ('poc_email','c1','email','pending', now())"),
    "pending_resolved_at": ("ck_outbox_status_lifecycle",  # pending ⇒ resolved_at NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
        "VALUES ('poc_email','c1','email','pending', now())"),
    "delivered_no_delivered_at": ("ck_outbox_status_lifecycle",  # delivered ⇒ delivered_at NOT NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','email','delivered')"),
    "dead_with_claim": ("ck_outbox_status_lifecycle",  # dead ⇒ claim tuple all-NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
        "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','dead', "
        "gen_random_uuid(), now(), 'w1')"),
    "orphan_claimed_by": ("ck_outbox_status_lifecycle",  # partial claim tuple
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, claimed_by) "
        "VALUES ('poc_email','c1','email','pending','w1')"),
    "superseded_null_resolved": ("ck_outbox_status_lifecycle",  # superseded ⇒ resolved_at NOT NULL
        # (also trips the F8 decision-only conjunct; the dedicated F8 negatives below isolate it)
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','email','superseded')"),
    "delivered_with_claim": ("ck_outbox_status_lifecycle",  # delivered ⇒ claim tuple all-NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at, "
        "claim_token, claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email',"
        "'delivered', now(), gen_random_uuid(), now(), 'w1')"),
}


@pytest.mark.parametrize("case", list(_OUTBOX_LIFECYCLE_BAD))
def test_013_outbox_lifecycle_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    expected, sql = _OUTBOX_LIFECYCLE_BAD[case]
    url = _fresh_db(pg, f"kyc_mig_013_neg_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(sql))
    assert expected in str(exc.value)  # the INTENDED constraint, not an unrelated later one
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
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET status='superseded' WHERE case_id='c1'"))
    assert "ck_outbox_status_lifecycle" in str(exc.value)  # the INTENDED constraint, by name
    engine.dispose()


def test_013_superseded_is_decision_only_insert_negative(pg):
    """Re-audit F8: a poc_email cannot be INSERTed as 'superseded' even with an otherwise
    VALID superseded shape (resolved_at set, claim tuple all-NULL) — only the decision-only
    kind conjunct of ck_outbox_status_lifecycle rejects it, asserted by name."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_sup_poc_ins")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                          "VALUES ('poc_email','c1','email','superseded', now())"))
    assert "ck_outbox_status_lifecycle" in str(exc.value)
    engine.dispose()


def test_013_superseded_is_decision_only_update_negative(pg):
    """Re-audit F8 UPDATE variant: a live pending poc_email cannot be UPDATEd into
    'superseded' even when resolved_at is set in the same statement (the future-bug /
    operator-UPDATE path that would silently zero-send an email AND make downgrade refuse)."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_sup_poc_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                          "VALUES ('poc_email','c1','email','pending')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET status='superseded', resolved_at=now() "
                          "WHERE case_id='c1'"))
    assert "ck_outbox_status_lifecycle" in str(exc.value)
    engine.dispose()
```

- [ ] **Step 6: Run the negatives**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_up_down_up or 013_outbox_lifecycle or 013_superseded_is_decision_only" -v`
Expected: PASS (all parametrizations + both F8 decision-only negatives).

- [ ] **Step 7: Mutation check (manual, no commit)** — temporarily delete `op.create_check_constraint("ck_outbox_status_lifecycle", ...)` from the migration, rerun `.venv/bin/pytest tests/integration/test_migrations.py -k "013_outbox_lifecycle or 013_superseded_is_decision_only" -v`; confirm the `pending_delivered_at`/`dead_with_claim`/`superseded_null_resolved`/`delivered_with_claim` cases AND both F8 decision-only negatives now FAIL (no `IntegrityError`). Restore the line. Do the same for `ck_outbox_kind_vocab` (→ `unknown_kind` fails) and the `SET NOT NULL` on `ordering_stream` (→ `is_nullable` metadata assertion fails). **F8 named witness:** remove ONLY the final `AND (status <> 'superseded' OR kind = 'decision_callback')` conjunct from `_LIFECYCLE_CHECK` (leave every other branch), rerun `.venv/bin/pytest tests/integration/test_migrations.py -k 013_superseded_is_decision_only -v` → BOTH negatives must FAIL (the shape-valid superseded email now passes) while every other lifecycle negative stays green. Restore.

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
    # NOT NULL + named FK (migration 013). The FK lives in ORM metadata too (re-audit F9):
    # Base.metadata is Alembic's comparison target, so a live-only FK would report drift.
    case_id: Mapped[str] = mapped_column(
        Text, ForeignKey("cases.id", name="fk_outbox_case_id"), index=True
    )
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
        # PR 7b-core (013): partial to the claimable set — decision_callback terminals are never
        # pruned, so a full index would grow without bound and enter every claim plan.
        Index("ix_outbox_claim", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index(
            "ix_outbox_stream_claim", "case_id", "ordering_stream", "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
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

- [ ] **Step 8b: Adapt the affected EXISTING fixtures (re-audit F3, part 1 — required for a green Step 11)** — three existing files carry fixtures that 013's new columns/CHECKs invalidate. Without these edits `./manage.sh test` cannot pass at this checkpoint (the advertised atomic gate would be false). Apply exactly:

**(i) `tests/integration/test_ui.py` — `test_requeue_refuses_redacted_dead_poc_email` (~line 167):** the raw dead-POC INSERT omits the now-NOT-NULL stream. Replace the statement text with:

```python
                "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status, attempts) "
                "VALUES ('poc_email', 'ui-redacted', 'email', CAST(:p AS jsonb), 'dead', 8) "
                "RETURNING id"
```

(`dead` + `delivered_at NULL` + claim all-NULL satisfies the lifecycle CHECK; `poc_email`+`email`+no run/sequence satisfies the Task-4 identity CHECK.)

**(ii) `tests/integration/test_phase4_platform.py` — `test_redelivery_carries_identical_dedupe_key` (~84-97):** the raw `UPDATE outbox SET status='pending' …` retains `delivered_at`, which the lifecycle CHECK now rejects. **INTERIM form for this checkpoint only** (the fenced claim does not exist until Task 3; Task 3 Step 3b replaces this body with the REAL send-before-stamp fault injection, and the single atomic 013 commit ships ONLY that final form). Replace the raw UPDATE with the lifecycle-legal rewrite:

```python
    # simulate a crash after delivery but before the delivered-mark landed
    # (INTERIM, Task 1: under ck_outbox_status_lifecycle a pending row must have
    # delivered_at NULL, so the rewrite clears it. Task 3 Step 3b replaces this whole
    # body with the real send-before-stamp fault injection — the committed unit ships
    # only that form.)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE outbox SET status='pending', next_attempt_at=now(), delivered_at=NULL "
                "WHERE case_id='case-redeliver'"
            )
        )
```

**(iii) `tests/integration/test_migrations.py` — legacy `manual=false` decision fixtures + named nonblank negatives:** three existing fixtures insert `manual=false` decisions with NO run/sequence; the Task-2 parity preflight (`auto_decision_null_run`) and the Task-4 `ck_decisions_manual_sequence` shape CHECK both reject that. They are genuinely manual-shaped rows — flip each to `manual=true` (`engine_build_id` semantics unchanged):

- `test_011_downgrade_refuses_after_use`, the `decision_engine_id` parametrization (~124-127): change `'{}'::jsonb,false,'eng-1')` → `'{}'::jsonb,true,'eng-1')`.
- `_NONBLANK_SURFACES["decision_engine"]` (~164-166): change `'{}'::jsonb,false,:blank)` → `'{}'::jsonb,true,:blank)`.
- `test_011_checks_not_valid_then_012_validates`, the decision INSERT populated at revision 010 (~234-237): change `'{}'::jsonb,false)` → `'{}'::jsonb,true)` (this DB is later upgraded to head, so the 013 parity preflight must pass over it).

**Named nonblank negatives (F3):** every nonblank negative must assert its intended constraint so 013's new shape CHECK can never produce a false green. Restructure `_NONBLANK_SURFACES` to map each surface to `(constraint_name, sql)` — the names are from `alembic/versions/011_policy_bundle_pinning.py` (validated by `012_validate_pinning_check_constraints.py`):

```python
_NONBLANK_SURFACES = {
    "epoch_engine": ("ck_epoch_engine_nonblank", _VALID_BUNDLE + "; INSERT INTO bundle_pinning_epoch "
        "(id, activated_at, bundle_hash, engine_build_id) VALUES (1, now(), 'h', :blank)"),
    "run_engine": ("ck_runs_engine_build_id_nonblank", _VALID_CASE + "; " + _VALID_EVENT
        + "; INSERT INTO runs (id, case_id, triggering_event_id, state, engine_build_id) "
        "VALUES ('r','c','ev','QUEUED',:blank)"),
    "decision_engine": ("ck_decisions_engine_build_id_nonblank", _VALID_CASE
        + "; INSERT INTO decisions (id, case_id, decision, score, "
        "gates_json, buy_enablement, policy_shas, manual, engine_build_id) VALUES ('d','c','x',0,"
        "'{}'::jsonb,'buy_locked_org_id_required','{}'::jsonb,true,:blank)"),
    "check_bundle": ("ck_checks_policy_bundle_hash_nonblank", _VALID_CASE
        + "; INSERT INTO checks (id, case_id, check_type, status, "
        "points_awarded, category, source, policy_bundle_hash) "
        "VALUES ('k','c','verified_email','pass',10,'x','seed',:blank)"),
}


@pytest.mark.parametrize("surface", list(_NONBLANK_SURFACES))
@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "ws"])
def test_011_nonblank_checks_reject_blank(pg, surface, blank):
    from sqlalchemy.exc import IntegrityError

    constraint_name, sql = _NONBLANK_SURFACES[surface]
    url = _fresh_db(pg, f"kyc_mig_011_nb_{surface}_{len(blank)}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    engine = create_engine(url)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        for stmt in sql.split("; "):
            conn.execute(text(stmt), {"blank": blank})  # extra param ignored where unused
    assert constraint_name in str(exc.value)  # F3: the INTENDED btrim CHECK, not a 013 shape CHECK
    engine.dispose()
```

Also make the inline nonblank negative at the end of `test_011_checks_not_valid_then_012_validates` (~257-264) assert its name — wrap it as `with pytest.raises(IntegrityError) as exc, engine.begin() as conn:` and add `assert "ck_checks_policy_bundle_hash_nonblank" in str(exc.value)` after the block.

Run: `.venv/bin/pytest tests/integration/test_ui.py tests/integration/test_phase4_platform.py::test_redelivery_carries_identical_dedupe_key -v` and `.venv/bin/pytest tests/integration/test_migrations.py -k "011 or 010 or 012" -v`
Expected: PASS (the adapted fixtures are valid under the accumulated 013 schema, and every nonblank negative pins its intended constraint).

- [ ] **Step 9: RED drift-guard step (deliberately red)** — the src edits (ORM + enqueue) changed the source tree, so the whole-source guard now fails. Observe it:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v`
Expected: **FAIL** (`src/kyc_tool changed`). This is the RED step — do NOT run `./manage.sh test` yet (it would return nonzero on this same guard).

- [ ] **Step 10: Re-pin the engine drift guard → GREEN**

Run the re-pin one-liner (see Test infrastructure), paste the digest into `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py`, then:
Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → PASS.

- [ ] **Step 11: CHECKPOINT — full gate green, NO git commit (F1)**

`013` is committed once at the end of Task 6. Run the gates and hand the accumulated worktree diff to review; do **not** `git commit`:
```bash
./manage.sh test                       # whole suite, exit 0 (live decide enqueues ordering_stream='decision')
.venv/bin/ruff check .                 # exit 0
.venv/bin/lint-imports                 # 2 kept / 0 broken
git status                             # review the worktree diff — do NOT commit yet
```

---

## Task 2: Migration 013 part B — frozen backfill contract + legacy `decision_sequence` backfill (order authority = `outbox.id`)

Creates the schema-012-compatible **frozen migration contract** (`src/kyc_tool/migration_contracts/v013_backfill.py`, raw SQL + stable constants ONLY — no other imports) that BOTH migration `013`'s preflight AND the `verify_pr7b_core_backfill` diagnostic (Task 7) run, then wires it into the migration: refuse (with actionable ids, `BLOCKED_NO_AUTHORITATIVE_MAPPING` on a missing mapping) BEFORE any DDL, else rank per case by `outbox.id`, stamp `decisions.decision_sequence` + copy to the callback, seed `cases.last_decision_sequence`. The contract is **immutable history** — a dedicated frozen-SHA test forbids ever re-pinning it (any later semantics get a new `v014_…` module). **Touches `src/` (the contract module) → re-pins the whole-source drift guard (separately from the frozen-SHA test).**

**Files:**
- Create: `src/kyc_tool/migration_contracts/__init__.py` (empty package marker)
- Create: `src/kyc_tool/migration_contracts/v013_backfill.py` (raw-SQL parity matrix + constants — imports ONLY `sqlalchemy.text`)
- Create: `tests/unit/test_migration_contract_v013.py` (the frozen-SHA guard — NEVER re-pinned)
- Modify: `alembic/versions/013_outbox_stream_separation.py` (insertion points A + B in `upgrade`)
- Modify: `tests/integration/test_migrations.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py` (whole-source guard only — NOT the frozen-SHA test)

**Interfaces:**
- Produces: `PARITY_CHECKS: list[tuple[str, str]]` (name, schema-012 SQL selecting offending `a`,`b` id pairs); `run_parity(executor) -> list[tuple[str, list[tuple]]]` (checks with offenders); constants `MISSING_CALLBACK`, `BLOCKED_SENTINEL="BLOCKED_NO_AUTHORITATIVE_MAPPING"`. `run_parity` accepts anything with `.execute(text(...))` (an alembic `Connection` OR a `Session`). The module imports ONLY `sqlalchemy.text` (dependency-light, freezable).
- Produces: `_PARITY_BAD_SEEDS: dict[str, list[str]]` in `test_migrations.py` (one invalid legacy state per key), imported by Task 7's CLI test.
- Consumes: schema from Task 1 (columns present, no decision-identity constraints yet).

- [ ] **Step 1: Create the frozen migration contract** — create `src/kyc_tool/migration_contracts/__init__.py` (empty), then `src/kyc_tool/migration_contracts/v013_backfill.py`:

```python
"""FROZEN migration contract for the 7b-core 012→013 backfill (historical & immutable).

Migration 013's preflight AND the verify_pr7b_core_backfill diagnostic both run THIS matrix,
so the two can never diverge. This module is deliberately dependency-light — it imports ONLY
`sqlalchemy.text` — and is pinned by a frozen-SHA test (tests/unit/test_migration_contract_v013.py)
that must NEVER be re-pinned: changing this file changes how a historical 012→013 upgrade behaves.
For any later backfill semantics, add a NEW `v014_*.py` contract instead of editing this one.

Raw SQL only — references NO 013-only columns (ordering_stream, decision_sequence, claim_*). Each
check SELECTs offending id pairs (a, b); empty ⇒ clean. MISSING_CALLBACK is the fail-closed
no-authoritative-mapping state (a decision whose callback was pruned): the safe order cannot be
reconstructed, so it maps to BLOCKED_NO_AUTHORITATIVE_MAPPING.
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
     "SELECT d.id AS a, d.run_id AS b FROM decisions d JOIN runs r ON r.id=d.run_id "
     "WHERE d.case_id <> r.case_id"),
    ("unknown_kind",
     "SELECT id AS a, kind AS b FROM outbox WHERE kind NOT IN ('decision_callback','poc_email')"),
    ("poc_row_with_run",
     "SELECT id AS a, run_id AS b FROM outbox WHERE kind='poc_email' AND run_id IS NOT NULL"),
    ("manual_decision_with_run",
     "SELECT id AS a, run_id AS b FROM decisions WHERE manual=true AND run_id IS NOT NULL"),
    ("auto_decision_null_run",
     "SELECT id AS a, NULL AS b FROM decisions WHERE manual=false AND run_id IS NULL"),
    # Pre-013 projection of the FINAL ck_outbox_status_lifecycle (schema 012 has no resolved_at /
    # claim_* columns, so a legacy row can only violate via status vocabulary or the delivered_at
    # shape). A valid pending/dead has delivered_at NULL; a valid delivered has it NOT NULL;
    # 'superseded' and any unknown status are impossible pre-013 and fail the 013 CHECK.
    ("invalid_legacy_outbox_lifecycle",
     "SELECT id AS a, status AS b FROM outbox WHERE "
     "status NOT IN ('pending','delivered','dead') "
     "OR (status='pending' AND delivered_at IS NOT NULL) "
     "OR (status='delivered' AND delivered_at IS NULL) "
     "OR (status='dead' AND delivered_at IS NOT NULL)"),
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

- [ ] **Step 1b: Pin the frozen contract SHA (separate from the whole-source drift guard)** — write `src/kyc_tool/migration_contracts/v013_backfill.py` **byte-for-byte from the Step-1 block** (LF line endings, a single trailing newline), then create `tests/unit/test_migration_contract_v013.py`. The SHA below was computed during plan authoring over exactly those bytes (the Step-1 block content + one trailing `\n`):

```python
"""FROZEN historical migration contract guard. NEVER re-pin V013_BACKFILL_SHA — a change to
v013_backfill.py alters how a historical 012→013 upgrade behaves on a persistent DB. For any
later backfill semantics, add src/kyc_tool/migration_contracts/v014_*.py and pin it separately.
(This is distinct from the whole-source engine drift guard, which stays re-pinnable.)"""

import hashlib

from kyc_tool.config import REPO_ROOT

_CONTRACT = REPO_ROOT / "src" / "kyc_tool" / "migration_contracts" / "v013_backfill.py"
# sha256 of the exact Step-1 module bytes (write the file verbatim: LF endings, one trailing newline).
V013_BACKFILL_SHA = "bdd2342be673c2b324af02cf00644ecde70739a233e7c7cb39670123fb6d1c04"


def test_v013_backfill_contract_is_frozen():
    got = hashlib.sha256(_CONTRACT.read_bytes()).hexdigest()
    assert got == V013_BACKFILL_SHA, (
        "v013_backfill.py is a FROZEN historical migration contract — do NOT re-pin this hash; "
        "create migration_contracts/v014_*.py for any later semantics."
    )
```

Verify it matches the file you wrote: `python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('src/kyc_tool/migration_contracts/v013_backfill.py').read_bytes()).hexdigest())"` must print `bdd2342be673c2b324af02cf00644ecde70739a233e7c7cb39670123fb6d1c04`; then `.venv/bin/pytest tests/unit/test_migration_contract_v013.py -v` → PASS. If the printed hash differs, your file bytes differ from the block (a stray trailing space / CRLF / missing final newline) — fix the FILE to match the block, do NOT re-pin the SHA. (A full empty `base→head` upgrade is already exercised by `test_upgrade_downgrade_upgrade`; a populated `012→013` upgrade importing only the installed package is exercised by the Step-2 backfill tests — both run migration 013's `from kyc_tool.migration_contracts.v013_backfill import …` with no test-only or mutable dependency.)

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
                        "VALUES ('decision_callback','c1','rB', "
                        "CAST('{\"run_id\":\"rB\"}' AS jsonb),'pending')"))
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
                           "VALUES ('decision_callback','c1','rA', "
                           "CAST('{\"run_id\":\"rA\"}' AS jsonb),'pending')"))
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
        "INSERT INTO outbox (kind, case_id, payload_json, "
        "status) VALUES ('poc_email',NULL,'{}'::jsonb,'pending')"],
    "callback_case_ne_decision_case": [_BAD_CASE, "INSERT INTO cases (id) VALUES ('c2')", _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "  # callback on c2, decision on c1
        "VALUES ('decision_callback','c2',:r,'{}'::jsonb,'pending')"],
    "decision_case_ne_run_case": [_BAD_CASE, "INSERT INTO cases (id) VALUES ('c2')", _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, "
        "gates_json, buy_enablement, "  # run on c1, decision on c2
        "policy_shas, manual) VALUES ('d','c2',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c2',:r,'{}'::jsonb,'pending')"],
    "unknown_kind": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, "
        "status) VALUES ('weird','c1','{}'::jsonb,'pending')"],
    "poc_row_with_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('poc_email','c1',:r,'{}'::jsonb,'pending')"],
    "manual_decision_with_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true)"],
    "auto_decision_null_run": [_BAD_CASE,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,false)"],
    # Legacy lifecycle rows (valid case/kind, no run) that pass every OTHER parity check but the
    # final ck_outbox_status_lifecycle rejects — the exact "step-0 green, migration fails mid-outage"
    # gap (F2). Each targets invalid_legacy_outbox_lifecycle.
    "lifecycle_pending_delivered_at": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
        "VALUES ('poc_email','c1','{}'::jsonb,'pending', now())"],
    "lifecycle_delivered_null_at": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
        "VALUES ('poc_email','c1','{}'::jsonb,'delivered', NULL)"],
    "lifecycle_dead_delivered_at": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
        "VALUES ('poc_email','c1','{}'::jsonb,'dead', now())"],
    "lifecycle_unknown_status": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status) "
        "VALUES ('poc_email','c1','{}'::jsonb,'weird_status')"],
}

# Seed key → the PARITY_CHECKS name that MUST appear in the refusal (some rows trip several checks;
# this pins the one each seed targets). missing_callback additionally prints the BLOCKED sentinel.
_PARITY_SEED_VIOLATION = {
    "missing_callback": "missing_callback",
    "orphan_callback": "orphan_callback",
    "duplicate_callback_per_run": "duplicate_callback_per_run",
    "duplicate_auto_decision_per_run": "duplicate_auto_decision_per_run",
    "null_run_callback": "null_run_callback",
    "null_or_orphan_outbox_case": "null_or_orphan_outbox_case",
    "callback_case_ne_decision_case": "callback_case_ne_decision_case",
    "decision_case_ne_run_case": "decision_case_ne_run_case",
    "unknown_kind": "unknown_kind",
    "poc_row_with_run": "poc_row_with_run",
    "manual_decision_with_run": "manual_decision_with_run",
    "auto_decision_null_run": "auto_decision_null_run",
    "lifecycle_pending_delivered_at": "invalid_legacy_outbox_lifecycle",
    "lifecycle_delivered_null_at": "invalid_legacy_outbox_lifecycle",
    "lifecycle_dead_delivered_at": "invalid_legacy_outbox_lifecycle",
    "lifecycle_unknown_status": "invalid_legacy_outbox_lifecycle",
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
    msg = str(exc.value)
    assert _PARITY_SEED_VIOLATION[name] in msg or "BLOCKED_NO_AUTHORITATIVE_MAPPING" in msg
    engine = create_engine(url)
    with engine.connect() as conn:
        cols = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='outbox'"))}
    assert "claim_token" not in cols  # no DDL landed — refusal was before the column adds
    engine.dispose()
```

- [ ] **Step 2c: Prove valid lifecycle rows PASS + the lifecycle mutation witness** — append to `tests/integration/test_migrations.py`:

```python
def test_013_valid_lifecycle_rows_upgrade_clean(pg):
    """Valid pending / delivered(timestamped) / dead poc_email rows are NOT flagged by the
    lifecycle parity check and survive the 013 upgrade + its ck_outbox_status_lifecycle."""
    url = _fresh_db(pg, "kyc_mig_013_valid_lifecycle")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'pending')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'delivered', now())"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'dead')"))
    engine.dispose()
    alembic_command.upgrade(cfg, "013")  # no refusal; all three legacy rows are valid
```

Mutation witness (manual, no commit): delete the `invalid_legacy_outbox_lifecycle` entry from `PARITY_CHECKS` in `v013_backfill.py`; rerun `.venv/bin/pytest tests/integration/test_migrations.py -k "013_upgrade_refuses_parity_violation_before_ddl and lifecycle" -v` → those four params must FAIL (the preflight no longer refuses; the failure now only surfaces later at the CHECK creation, i.e. after the outage would have begun). Restore. **NOTE:** because this changes `v013_backfill.py` bytes, the frozen SHA (Task 2 Step 1b) is computed only after this entry is final.

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_backfill or 013_upgrade_refuses_parity" -v`
Expected: FAIL — the inversion test sees `dA=None, dB=None`; the refusal tests do NOT raise (no parity preflight wired yet).

- [ ] **Step 4: Wire the parity preflight + the assignment into the migration** — in `alembic/versions/013_outbox_stream_separation.py`:

Replace `# === Task 2 insertion point A: shared parity preflight (runs BEFORE any DDL) ===` with:

```python
    # --- shared parity preflight (fail-closed BEFORE any DDL) ---
    from kyc_tool.migration_contracts.v013_backfill import BLOCKED_SENTINEL, MISSING_CALLBACK, run_parity

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

- [ ] **Step 7: RED drift-guard step** — the new `src/kyc_tool/migration_contracts/v013_backfill.py` changed the source tree:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step; do NOT run `./manage.sh test` yet).

- [ ] **Step 8: Re-pin the drift guard → GREEN** — run the re-pin one-liner, paste into `EXPECTED_ENGINE_SOURCE_HASH`, rerun the guard test → PASS.

- [ ] **Step 9: CHECKPOINT — full gate green, NO git commit (F1)** — also run the frozen-contract SHA test:

```bash
.venv/bin/pytest tests/unit/test_migration_contract_v013.py -v   # frozen contract pinned (Step 1b)
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git status                             # review the accumulated worktree diff — do NOT commit yet
```

---

## Task 3: Stream-scoped fenced claim + fenced terminals (winner/loser)

Rewrites `_CLAIM_SQL` to claim the min-id pending row of one `(case_id, ordering_stream)` under a fresh `claim_token`+lease (leaving `next_attempt_at` as the retry due time, removing the `case_id IS NULL` bypass), threads the token through `process_once`, and makes `_record_delivered`/`_record_failure` fenced: the winner (`applied=true`) does all dependent writes in-transaction; the stale loser (`applied=false`) does nothing and emits `outbox_stale_claim_completion`, non-raising. This is the single ownership gate that closes defect 3 and lets a stuck stream never block the other.

**Files:**
- Modify: `src/kyc_tool/outbox/publisher.py` (`_CLAIM_SQL` 29-49, `enqueue_decision_callback` 52-53, `OutboxPublisher.__init__` 67-78, `process_once` 143-158, `_record_delivered` 160-191, `_record_failure` 193-226)
- Create: `tests/integration/test_outbox_fencing.py`
- Modify: `tests/integration/test_phase4_platform.py` (Step 3b — the FINAL send-before-stamp redelivery form, re-audit F3)
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `enqueue_decision_callback` from Task 1 (adds a `decision_sequence` kwarg here, default `None` — a TEMPORARY staging form; Task 4 Step 8b tightens it to a REQUIRED keyword, and the single atomic 013 commit ships only the required form — re-audit F10).
- Produces: `_CLAIM_SQL` `RETURNING id, kind, case_id, run_id, payload_json, attempts, ordering_stream, decision_sequence, claim_token`; `process_once()` passes `token = row.claim_token` to every terminal; `_record_delivered(self, row, token)`, `_record_failure(self, row, error, token)` each begin with the fenced `UPDATE … WHERE id=:id AND status='pending' AND claim_token=:token RETURNING id` and branch on `applied`; stale losers log `outbox_stale_claim_completion` (fields: `outbox_id`, `attempted`).

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_outbox_fencing.py`:

(Rev 6 correction: the first three tests take `clean_db` like the last two. They share the
hard-coded ids `c1`/`ev1`/`r1`/`d1`, so without it test 3 collides on the `events` primary key and
test 1's hour-backoff stuck email leaks into test 2's claim — the file could not pass as a file,
contradicting Step 4's own "Expected: PASS".)

```python
"""PR 7b-core: stream-scoped fenced claim + fenced terminals (defect 3)."""

import json

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
            {
                "c": case_id,
                "p": json.dumps({"to": to, "subject": "s", "body": "b"}),
                "st": status,
            },
        )
        s.commit()


def _pub_for(session_factory, settings):
    """A publisher bound to `session_factory` whose HTTP + email always succeed (200)."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    return OutboxPublisher(
        session_factory, settings,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )


def test_stuck_email_does_not_block_decision_callback(
    session_factory, settings, publisher, callback_capture, clean_db
):
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
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                       "state) VALUES ('r1','c1','ev1','PUBLISH_DECISION')"))
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
        s.execute(text("UPDATE outbox SET claim_lease_expires_at = now() "
                       "- interval '1 second' WHERE id=:i"), {"i": oid})
        s.commit()


def test_stale_poc_loser_touches_nothing_before_reclaimer_sends(
    session_factory, settings, publisher, clean_db
):
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
        before = s.execute(text("SELECT payload_json::text AS p, status, "
                                "claim_token, claim_lease_expires_at, "
                                "claimed_by, attempts, next_attempt_at FROM "
                                "outbox WHERE id=:i"), {"i": rowA.id}).one()

    s2 = settings.model_copy(update={"outbox_max_attempts": 1})  # A's failure would be terminal (dead)
    stale_pub = _pub_for(session_factory, s2)
    with structlog.testing.capture_logs() as logs:
        stale_pub._record_delivered(rowA, rowA.claim_token)          # stale success → no-op
        stale_pub._record_failure(rowA, "boom", rowA.claim_token)    # stale final-attempt failure → no-op
    assert [e for e in logs if e["event"] == "outbox_stale_claim_completion"]  # audited no-op, no raise

    with session_factory() as s:
        after = s.execute(text("SELECT payload_json::text AS p, status, claim_token, claim_lease_expires_at, "
                               "claimed_by, attempts, next_attempt_at FROM "
                               "outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert after == before  # A changed NOTHING — payload, status, B's whole claim tuple, attempts, clock

    publisher._record_delivered(rowB, rowB.claim_token)  # B (the real winner) delivers + redacts once
    with session_factory() as s:
        final = s.execute(text("SELECT status, payload_json FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert final.status == "delivered" and final.payload_json == {"redacted": True}


def test_stale_decision_loser_cannot_stamp_run_or_published_at(
    session_factory, settings, publisher, callback_capture, clean_db
):
    """Defect 3, decision callback: same A/B ordering. Stale A must change neither the outbox
    tuple nor runs.state (stays PUBLISH_DECISION) nor decisions.published_at (stays NULL). Then B
    alone stamps both (run COMPLETE, published_at set)."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_case(session_factory, "c1")
    with session_factory() as s:
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, "
                       "payload_hash, event_type, actor_json, "
                       "payload_json, event_sequence) VALUES "
                       "('ev1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                       "state) VALUES ('r1','c1','ev1','PUBLISH_DECISION')"))
        s.execute(text("INSERT INTO decisions (id, case_id, run_id, "
                       "decision, score, gates_json, buy_enablement, "
                       "policy_shas, manual, decision_sequence) VALUES "
                       "('d1','c1','r1','approve',10,'{}'::jsonb,"
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


def _seed_decision_chain(session_factory, *, case_id, run_id, decision_id, seq, ev_seq):
    """One valid case→event→run→automatic-decision chain (decision carries `seq`); the
    caller enqueues its callback separately."""
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
                "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"
            ),
            {"e": run_id + "-ev", "c": case_id, "k": run_id, "s": ev_seq},
        )
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                       "state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
                  {"r": run_id, "c": case_id, "e": run_id + "-ev"})
        s.execute(
            text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
                "policy_shas, manual, decision_sequence) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,"
                "'enabled','{}'::jsonb,false,:seq)"
            ),
            {"d": decision_id, "c": case_id, "r": run_id, "seq": seq},
        )
        s.commit()


def test_same_stream_fifo_holds_under_backoff(
    session_factory, settings, publisher, callback_capture, clean_db
):
    """Re-audit F6: strict same-stream FIFO survives retry backoff. One case, decision
    stream: seq1 (older outbox id) sits in FUTURE backoff; seq2 (newer id) is due NOW.
    The claim's inner min(o2.id) head-of-stream subquery deliberately ignores due-time/
    lease eligibility, so the backed-off head BLOCKS its whole stream: process_once() is
    idle (False), ZERO HTTP occurred, and seq2 is untouched (still pending, no claim).
    Once seq1 is due again, delivery order is exactly [seq1, seq2].
    MUTATION WITNESS (named, Step 5c): adding due/lease filtering (e.g.
    `AND o2.next_attempt_at <= now()`) to the INNER `min(o2.id)` subquery of _CLAIM_SQL
    lets seq2 leapfrog its backed-off elder — this test then FAILS."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_decision_chain(session_factory, case_id="cf", run_id="cf-r1", decision_id="cf-d1",
                         seq=1, ev_seq=1)
    _seed_decision_chain(session_factory, case_id="cf", run_id="cf-r2", decision_id="cf-d2",
                         seq=2, ev_seq=2)
    with session_factory() as s:  # enqueue seq1 FIRST (lower outbox.id), then seq2
        enqueue_decision_callback(s, case_id="cf", run_id="cf-r1", body={"run_id": "cf-r1"},
                                  decision_sequence=1)
        s.commit()
    with session_factory() as s:
        enqueue_decision_callback(s, case_id="cf", run_id="cf-r2", body={"run_id": "cf-r2"},
                                  decision_sequence=2)
        s.commit()
    with session_factory() as s:  # push ONLY seq1 into future backoff; seq2 stays due
        s.execute(text("UPDATE outbox SET next_attempt_at = now() + interval '1 hour' "
                       "WHERE case_id='cf' AND decision_sequence=1"))
        s.commit()

    assert publisher.process_once() is False       # idle: the stream head is backed off
    assert callback_capture.requests == []         # ZERO HTTP — seq2 did NOT leapfrog
    with session_factory() as s:
        row2 = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by "
                              "FROM outbox WHERE case_id='cf' AND decision_sequence=2")).one()
    assert row2.status == "pending"                # seq2 untouched: pending, never claimed
    assert row2.claim_token is None and row2.claim_lease_expires_at is None
    assert row2.claimed_by is None

    with session_factory() as s:                   # make seq1 due again
        s.execute(text("UPDATE outbox SET next_attempt_at = now() "
                       "WHERE case_id='cf' AND decision_sequence=1"))
        s.commit()
    assert publisher.process_pending() == 2
    assert [r["body"]["run_id"] for r in callback_capture.requests] == ["cf-r1", "cf-r2"]


def test_stale_retry_failure_cannot_touch_reclaimed_row(session_factory, settings, clean_db):
    """Re-audit F7: the fenced NONTERMINAL (retry) failure branch, proven stale-winner/loser.
    outbox_max_attempts=3 so a failure is a RETRY, not dead. A claims; A's lease expires;
    B reclaims (and does NOT terminalize). A's stale _record_failure must leave B's ENTIRE
    claim tuple (claim_token, claim_lease_expires_at, claimed_by), attempts, next_attempt_at,
    and last_error byte-for-byte unchanged, emitting outbox_stale_claim_completion with
    attempted="retry" (the nonterminal branch). Then B's OWN _record_failure increments
    attempts to rowB.attempts+1 (its claim snapshot), schedules backoff per the
    base*2^(attempts-1) formula, and clears ONLY B's claim tuple — status stays pending.
    MUTATION WITNESS (named, Step 5d): removing `claim_token=:token` from ONLY the
    nonterminal retry UPDATE in _record_failure lets stale A rewrite B's attempts/backoff/
    claim — this test then FAILS at the byte-for-byte assertion."""
    _seed_case(session_factory, "c1")
    _enqueue_email(session_factory, case_id="c1", to="keep@x")
    retry_settings = settings.model_copy(
        update={"outbox_max_attempts": 3, "outbox_backoff_base_seconds": 100}
    )
    pub = _pub_for(session_factory, retry_settings)

    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")             # B reclaims; does NOT terminalize
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    with session_factory() as s:
        before = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by, "
                                "attempts, next_attempt_at, last_error "
                                "FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    with structlog.testing.capture_logs() as logs:
        pub._record_failure(rowA, "boom", rowA.claim_token)   # stale RETRY-branch loser
    stale = [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert stale and stale[0]["attempted"] == "retry"          # the nonterminal branch ran
    with session_factory() as s:
        after = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by, "
                               "attempts, next_attempt_at, last_error "
                               "FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
    assert after == before  # B's ENTIRE claim tuple + attempts + retry clock: untouched

    pub._record_failure(rowB, "boom", rowB.claim_token)        # B's OWN retry failure
    with session_factory() as s:
        final = s.execute(text("SELECT status, claim_token, claim_lease_expires_at, claimed_by, "
                               "attempts FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        backoff_ok = s.execute(text(
            "SELECT next_attempt_at > now() + interval '50 seconds' "
            "AND next_attempt_at <= now() + interval '150 seconds' "
            "FROM outbox WHERE id=:i"), {"i": rowA.id}).scalar_one()
    assert final.status == "pending"                # nonterminal: NOT dead, NOT delivered
    assert final.attempts == rowB.attempts + 1      # == 1 — relative to B's claim snapshot
    assert final.claim_token is None                # only B's tuple cleared, under B's token
    assert final.claim_lease_expires_at is None and final.claimed_by is None
    assert backoff_ok  # rescheduled per the formula: 100 * 2**0 = 100s into the future


def test_stale_loser_cannot_supersede_reclaimed_row(session_factory, settings, publisher, clean_db):
    """Sibling of the delivered/retry-failure fencing tests, for the SUPERSEDED terminal: same
    A/B ordering. A claims; A's lease expires; B reclaims (and does NOT terminalize). Stale A's
    _record_superseded must be a complete no-op: the outbox row's status/resolved_at/claim tuple
    stay exactly as B left them (still pending, still B's claim), runs.state stays
    PUBLISH_DECISION, decisions.published_at stays NULL, and no audit_log row is written — only
    outbox_stale_claim_completion (attempted="superseded") is logged. Then B alone legitimately
    supersedes: status->superseded, resolved_at set, claim tuple cleared, run reaches COMPLETE,
    and exactly one audit_log row is written.
    MUTATION WITNESS: removing `claim_token=:token` from the leading UPDATE in _record_superseded
    (or its early return on no-match) lets stale A supersede the row out from under B — this test
    then FAILS."""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    _seed_decision_chain(session_factory, case_id="c1", run_id="r1", decision_id="d1", seq=1, ev_seq=1)
    with session_factory() as s:
        enqueue_decision_callback(s, case_id="c1", run_id="r1", body={"run_id": "r1"},
                                  decision_sequence=1)
        s.commit()

    rowA = _claim(session_factory, "A")
    assert rowA is not None
    _expire_lease(session_factory, rowA.id)
    rowB = _claim(session_factory, "B")  # B reclaims; does NOT terminalize
    assert rowB is not None and rowB.claim_token != rowA.claim_token

    stale_pub = _pub_for(session_factory, settings)
    with session_factory() as s:
        before_ob = s.execute(text("SELECT status, resolved_at, claim_token, claim_lease_expires_at, "
                                   "claimed_by FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        before_run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        before_pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one()
        before_audit = s.execute(text("SELECT count(*) FROM audit_log WHERE case_id='c1'")).scalar_one()

    with structlog.testing.capture_logs() as logs:
        stale_pub._record_superseded(rowA, rowA.claim_token)  # stale loser: fenced no-op
    stale = [e for e in logs if e["event"] == "outbox_stale_claim_completion"]
    assert stale and stale[0]["attempted"] == "superseded"

    with session_factory() as s:
        after_ob = s.execute(text("SELECT status, resolved_at, claim_token, claim_lease_expires_at, "
                                  "claimed_by FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        after_run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        after_pub_at = s.execute(text("SELECT published_at FROM decisions WHERE id='d1'")).scalar_one()
        after_audit = s.execute(text("SELECT count(*) FROM audit_log WHERE case_id='c1'")).scalar_one()
    assert after_ob == before_ob  # status, resolved_at, and B's WHOLE claim tuple: untouched
    assert after_ob.status == "pending" and after_ob.claim_token == rowB.claim_token  # still B's
    assert before_run == "PUBLISH_DECISION" and after_run == "PUBLISH_DECISION"
    assert before_pub_at is None and after_pub_at is None
    assert after_audit == before_audit  # no audit_log row written

    publisher._record_superseded(rowB, rowB.claim_token)  # B (the rightful claimant) supersedes
    with session_factory() as s:
        final_ob = s.execute(text("SELECT status, resolved_at, claim_token, claim_lease_expires_at, "
                                  "claimed_by FROM outbox WHERE id=:i"), {"i": rowA.id}).one()
        final_run = s.execute(text("SELECT state FROM runs WHERE id='r1'")).scalar_one()
        final_audit = s.execute(text("SELECT count(*) FROM audit_log WHERE case_id='c1'")).scalar_one()
    assert final_ob.status == "superseded" and final_ob.resolved_at is not None
    assert final_ob.claim_token is None and final_ob.claim_lease_expires_at is None
    assert final_ob.claimed_by is None
    assert final_run == "COMPLETE"
    assert final_audit == before_audit + 1  # exactly one audit_log row written
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v`
Expected: FAIL — `_CLAIM_SQL` binds `:claimed_by` which the current SQL does not accept; `enqueue_decision_callback` has no `decision_sequence` kwarg; `_record_delivered`/`_record_failure` take no `token`. The F6 FIFO-under-backoff and F7 retry-fencing tests fail on the same missing seams.

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

Change `enqueue_decision_callback` (lines 52-53) to accept `decision_sequence` (**TEMPORARY optional form** — this checkpoint stages it as `int | None = None` so the pipeline caller can be updated in Task 4; Task 4 Step 8b tightens it to a REQUIRED keyword `decision_sequence: int` after every caller passes it, and the single atomic 013 commit ships only the required form — re-audit F10):

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

- [ ] **Step 3b: Replace the phase-4 redelivery test with the REAL send-before-stamp fault (re-audit F3, final form)** — in `tests/integration/test_phase4_platform.py`, replace the ENTIRE body of `test_redelivery_carries_identical_dedupe_key` (including the Task-1 interim rewrite) with this final form. `process_once` calls `_record_delivered` OUTSIDE its delivery `try/except` (see the Step-3 `process_once` block), so an injected stamp fault propagates out of `process_pending` and the test catches it at that boundary. The name and dedupe-key intent are unchanged:

```python
def test_redelivery_carries_identical_dedupe_key(
    client, engine, post_event, worker, publisher, callback_capture, monkeypatch
):
    """At-least-once means duplicates happen; the platform dedupes on
    (case_id, run_id) — both fields must be identical across redeliveries.
    PR 7b-core (re-audit F3): the old raw delivered→pending rewrite is impossible under
    ck_outbox_status_lifecycle (it retained delivered_at), so this drives the REAL
    send-before-stamp gap: HTTP #1 succeeds, the delivered-stamp raises BEFORE its
    transaction, the row stays pending under its claim; the lease is expired; a reclaim
    under a NEW token resends the identical dedupe body (HTTP #2) and stamps for real."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    post_event("case-redeliver", "recalculate.requested", {})
    worker.run_until_idle()

    real_record_delivered = OutboxPublisher._record_delivered
    calls: list[int] = []

    class _StampFault(RuntimeError):
        pass

    def _fail_first(self, row, token):
        calls.append(1)
        if len(calls) == 1:  # the HTTP send already happened; the terminal stamp fails ONCE
            raise _StampFault("send-before-stamp: HTTP sent, delivered-stamp not committed")
        return real_record_delivered(self, row, token)

    monkeypatch.setattr(OutboxPublisher, "_record_delivered", _fail_first)
    # process_once invokes _record_delivered outside its delivery try/except, so the
    # injected fault escapes process_pending — the honest crash boundary.
    with pytest.raises(_StampFault):
        publisher.process_pending()
    assert len(callback_capture.requests) == 1  # HTTP #1 really happened
    with engine.connect() as conn:
        row = conn.execute(text("SELECT status, claim_token FROM outbox "
                                "WHERE case_id='case-redeliver'")).one()
    assert row.status == "pending" and row.claim_token is not None  # claim survived the crash

    with engine.begin() as conn:  # expire ONLY the lease — the reclaim path, not a raw rewrite
        conn.execute(text("UPDATE outbox SET claim_lease_expires_at = now() - interval '1 second' "
                          "WHERE case_id='case-redeliver'"))
    publisher.process_pending()  # reclaim under a NEW token → HTTP #2 → real stamp (call #2)

    bodies = [r["body"] for r in callback_capture.requests]
    assert len(bodies) == 2
    assert bodies[0]["case_id"] == bodies[1]["case_id"]
    assert bodies[0]["run_id"] == bodies[1]["run_id"]
    assert bodies[0]["decision"] == bodies[1]["decision"]
    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM outbox "
                                   "WHERE case_id='case-redeliver'")).scalar_one()
    assert status == "delivered"
```

Run: `.venv/bin/pytest tests/integration/test_phase4_platform.py::test_redelivery_carries_identical_dedupe_key -v`
Expected: PASS (HTTP #1, stamp fault, lease expiry, fenced reclaim, HTTP #2, identical dedupe key, final `delivered`).

- [ ] **Step 4: Run the fencing tests**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v`
Expected: PASS.

- [ ] **Step 5: Run all fencing tests + the four NAMED mutation witnesses (manual, no commit)**

Run: `.venv/bin/pytest tests/integration/test_outbox_fencing.py -v` → PASS (stuck-email + both stale POC/decision A/B tests + FIFO-under-backoff + retry-branch fencing).
**Mutation (a) — remove the loser early-return:** in `_record_delivered` delete the `if applied is None: … return` branch; rerun `.venv/bin/pytest tests/integration/test_outbox_fencing.py::test_stale_poc_loser_touches_nothing_before_reclaimer_sends -v` → it must FAIL (stale A's delivered UPDATE with a non-matching token now writes / raises on the `RETURNING`-less path). Restore.
**Mutation (b) — raise on zero rows:** change every terminal's `if applied is None: log.warning(...); return` to `if applied is None: raise RuntimeError("stale")`; rerun `test_stale_decision_loser_cannot_stamp_run_or_published_at` → it must FAIL (the raise escapes `_record_failure`/`_record_delivered` instead of a non-raising audited no-op). Restore.
**Mutation (c) — inner-min due/lease filter (re-audit F6):** in `_CLAIM_SQL`, add `AND o2.next_attempt_at <= now()` (or any due/lease eligibility predicate) to the INNER `SELECT min(o2.id) FROM outbox o2 …` head-of-stream subquery ONLY (leave the outer eligibility untouched); rerun `.venv/bin/pytest tests/integration/test_outbox_fencing.py::test_same_stream_fifo_holds_under_backoff -v` → it must FAIL (seq2 leapfrogs its backed-off elder: `process_once()` returns True and HTTP occurs while seq1 is still backed off). Restore.
**Mutation (d) — unfence ONLY the retry branch (re-audit F7):** in `_record_failure`, remove `AND claim_token=:token` from the NONTERMINAL (else/retry) `UPDATE` only — leave the dead-branch fence intact; rerun `.venv/bin/pytest tests/integration/test_outbox_fencing.py::test_stale_retry_failure_cannot_touch_reclaimed_row -v` → it must FAIL at the byte-for-byte assertion (stale A's retry UPDATE now matches B's row and rewrites attempts/backoff/claim). Restore.

- [ ] **Step 6: RED drift-guard step** — publisher.py changed:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step; do NOT run `./manage.sh test` yet).

- [ ] **Step 7: Re-pin the drift guard → GREEN** — run the re-pin one-liner, paste into `EXPECTED_ENGINE_SOURCE_HASH`, rerun the guard test → PASS.

- [ ] **Step 8: CHECKPOINT — full gate green, NO git commit (F1)**

```bash
./manage.sh test          # whole suite green (delivery still stamps published_at + run COMPLETE)
.venv/bin/ruff check .
.venv/bin/lint-imports
git status                # review the accumulated worktree diff — do NOT commit yet
```

---

## Task 4: Decision-sequence allocation + decision-identity constraints

Makes `_decide_txn` increment `cases.last_decision_sequence` under the already-held Case `FOR UPDATE`, stamp `DecisionRow.decision_sequence`, and pass it to `enqueue_decision_callback`. In the **same** checkpoint, adds `013`'s decision-identity constraints (the kind/stream identity CHECK, the `manual`/`automatic` CHECK, the three decisions uniques, the triple FK, the partial callback unique, **and — re-audit F2 — the composite `decisions(run_id, case_id) → runs(id, case_id)` FK over a new named `runs(id, case_id)` unique**, so a post-migration decision can never cite a run under the wrong case) — now both legacy (backfilled) and live (allocated) rows satisfy them, so the suite stays green. Adapts the bundle-pinning fixtures via a shared valid-chain helper (F3, part 2), and ends by tightening `enqueue_decision_callback` to its FINAL required-keyword signature (F10).

**Files:**
- Modify: `src/kyc_tool/orchestration/pipeline.py` (`_decide_txn` 485-506; the Case is FOR UPDATE from `_load` at 362)
- Modify: `src/kyc_tool/db/tables.py` (`Run` — add `__table_args__`; `DecisionRow` — add `__table_args__`; `Outbox.__table_args__`)
- Modify: `src/kyc_tool/outbox/publisher.py` (Step 8b — `enqueue_decision_callback` FINAL required signature, re-audit F10)
- Modify: `alembic/versions/013_outbox_stream_separation.py` (Task 4 insertion points in `upgrade` + `downgrade`)
- Modify: `tests/integration/test_migrations.py` (identity negatives + composite-FK negatives + ORM/live FK parity)
- Modify: `tests/conftest.py` (Step 7b — `seed_automatic_decision` shared valid-chain helper, re-audit F3)
- Modify: `tests/integration/test_bundle_pinning_ops.py` (Step 7b — the three automatic-decision fixtures, re-audit F3)
- Create: `tests/integration/test_decision_sequence.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `enqueue_decision_callback(..., decision_sequence=)` (Task 3); `cases.last_decision_sequence` (Task 1).
- Produces: constraints `uq_runs_id_case_id`, `fk_decisions_run_case`, `uq_decisions_run_id`, `uq_decisions_case_decision_sequence`, `uq_decisions_run_case_sequence`, `ck_decisions_manual_sequence`, `ck_outbox_kind_stream_identity`, `fk_outbox_decision_triple`, `uq_outbox_decision_callback_run`; every automatic decide stamps a strictly-increasing per-case `decision_sequence`; manual approve allocates none; the FINAL `enqueue_decision_callback(session, *, case_id, run_id, body, decision_sequence: int)` (required keyword — F10).

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
        s.execute(text("INSERT INTO events (id, case_id, idempotency_key, "
                       "payload_hash, event_type, actor_json, "
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
    """Deterministic lock-contention proof (F3). A `_load` wrapper keyed by run id: A takes the
    Case FOR UPDATE (real `_load`), signals `a_has_case_lock` AFTER it returns, and is HELD before
    allocation/commit; B signals `b_entered_load` before its `_load` and `b_returned_from_load`
    only AFTER it returns. With the real FOR UPDATE, B must block INSIDE `_load` while A holds —
    so `b_returned_from_load` stays unset. Releasing A → A=1, B=2, counter=2, two bound callbacks.
    MUTATION: removing `with_for_update=True` (or substituting `max()+1`) lets B return while A is
    held → `assert not b_returned_from_load.wait(2)` fails deterministically, regardless of commit
    order. Both threads must terminate (a timeout must not masquerade as success)."""
    jid_a = _seed_run_at_decide(session_factory, case_id="cc", run_id="ra", ev_seq=1)
    jid_b = _seed_run_at_decide(session_factory, case_id="cc", run_id="rb", ev_seq=2)

    a_has_case_lock = threading.Event()
    release_a = threading.Event()
    b_entered_load = threading.Event()
    b_returned_from_load = threading.Event()
    orig_load = pipeline._load

    def wrapped_load(session, run_id):
        if run_id == "ra":
            result = orig_load(session, run_id)   # A acquires the Case FOR UPDATE here
            a_has_case_lock.set()
            release_a.wait(timeout=20)            # hold A (with the lock) before allocation/commit
            return result
        b_entered_load.set()
        result = orig_load(session, run_id)       # B blocks here on the FOR UPDATE while A holds
        b_returned_from_load.set()
        return result

    monkeypatch.setattr(pipeline, "_load", wrapped_load)
    errors: list[Exception] = []

    def decide(run_id, jid):
        try:
            pipeline._decide_txn(run_id, from_state=RunState.DECIDE, job_id=jid, bundle=policy)
        except Exception as e:  # noqa: BLE001 — capture a uniqueness race for the assertion
            errors.append(e)

    ta = threading.Thread(target=decide, args=("ra", jid_a))
    ta.start()
    assert a_has_case_lock.wait(timeout=15)                 # A holds the case lock
    tb = threading.Thread(target=decide, args=("rb", jid_b))
    tb.start()
    assert b_entered_load.wait(timeout=15)                  # B has entered _load
    assert not b_returned_from_load.wait(timeout=2)         # ... and is BLOCKED on the FOR UPDATE
    release_a.set()
    ta.join(timeout=15)
    tb.join(timeout=15)
    assert not ta.is_alive() and not tb.is_alive()         # both terminated (no timeout-as-success)

    assert errors == []  # no uq_decisions_case_decision_sequence violation
    with session_factory() as s:
        by_run = {r.run_id: r.decision_sequence for r in s.execute(text(
            "SELECT run_id, decision_sequence FROM decisions WHERE case_id='cc' AND manual=false"))}
        counter = s.execute(text("SELECT last_decision_sequence FROM cases WHERE id='cc'")).scalar_one()
        cbs = sorted(r.decision_sequence for r in s.execute(text(
            "SELECT decision_sequence FROM outbox WHERE case_id='cc' AND kind='decision_callback'")))
    assert by_run == {"ra": 1, "rb": 2}  # A (first under the lock) = 1, B = 2
    assert counter == 2 and cbs == [1, 2]


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
    # --- runs relational identity target + composite decisions→runs FK (re-audit F2) ---
    # The single-column FKs bind decisions.case_id and decisions.run_id INDEPENDENTLY, so a
    # post-migration INSERT/UPDATE could still pair run r1 (case c1) with case c2 — the exact
    # tuple the parity preflight classifies as decision_case_ne_run_case. The named unique
    # target MUST exist before the composite FK that references it.
    op.create_unique_constraint("uq_runs_id_case_id", "runs", ["id", "case_id"])
    # run_id is nullable → MATCH SIMPLE semantics: a manual decision (run_id NULL) is exempt
    # by design, while every automatic decision (run_id NOT NULL) is fully bound to its run's
    # OWN case. This is the intended semantics, not an accident.
    op.create_foreign_key(
        "fk_decisions_run_case", "decisions", "runs", ["run_id", "case_id"], ["id", "case_id"]
    )
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
    # NULL-explicit exhaustive row shapes (F2). An implication form like
    # `NOT(manual=false) OR (run_id IS NOT NULL AND decision_sequence>0)` evaluates to UNKNOWN
    # (and thus PASSES) for (manual=false, run_id set, decision_sequence NULL) — a hole. Spell
    # every column's NULL-ness in each branch so a NULL sequence on an automatic row is rejected.
    op.create_check_constraint(
        "ck_decisions_manual_sequence", "decisions",
        "((manual = false AND run_id IS NOT NULL AND decision_sequence IS NOT NULL "
        "AND decision_sequence > 0) "
        "OR (manual = true AND run_id IS NULL AND decision_sequence IS NULL))",
    )
    # --- outbox callback binding: NULL-explicit exhaustive shape + triple FK + one callback per run ---
    op.create_check_constraint(
        "ck_outbox_kind_stream_identity", "outbox",
        "((kind='decision_callback' AND ordering_stream='decision' AND run_id IS NOT NULL "
        "AND decision_sequence IS NOT NULL AND decision_sequence > 0) "
        "OR (kind='poc_email' AND ordering_stream='email' AND run_id IS NULL "
        "AND decision_sequence IS NULL))",
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

Replace the `# === Task 4 insertion point: drop decision-identity constraints (reverse order) ===` line in `downgrade()` with (dependency-safe: the composite FK drops BEFORE the unique target it references — re-audit F2):

```python
    op.drop_index("uq_outbox_decision_callback_run", table_name="outbox")
    op.drop_constraint("fk_outbox_decision_triple", "outbox", type_="foreignkey")
    op.drop_constraint("ck_outbox_kind_stream_identity", "outbox", type_="check")
    op.drop_constraint("ck_decisions_manual_sequence", "decisions", type_="check")
    op.drop_constraint("uq_decisions_run_case_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_case_decision_sequence", "decisions", type_="unique")
    op.drop_constraint("uq_decisions_run_id", "decisions", type_="unique")
    op.drop_constraint("fk_decisions_run_case", "decisions", type_="foreignkey")
    op.drop_constraint("uq_runs_id_case_id", "runs", type_="unique")
```

- [ ] **Step 5: Update the ORM `__table_args__`** — in `src/kyc_tool/db/tables.py`, add the `ForeignKeyConstraint` import (line 10-21 import block):

```python
    ForeignKeyConstraint,
```

Add a `__table_args__` to `Run` (after `error`, line 99 — `Run` currently has NO `__table_args__`; this creates it — re-audit F2):

```python
    # PR 7b-core (migration 013): named composite target for fk_decisions_run_case —
    # decisions(run_id, case_id) must cite a run under its OWN case.
    __table_args__ = (UniqueConstraint("id", "case_id", name="uq_runs_id_case_id"),)
```

Add a `__table_args__` to `DecisionRow` (after `reviewer_id`, line 227). The composite FK mirrors the migration exactly; `run_id` is nullable → MATCH SIMPLE, so manual rows (`run_id NULL`) are exempt and automatic rows are fully bound (intended — re-audit F2):

```python
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_decisions_run_id"),
        UniqueConstraint("case_id", "decision_sequence", name="uq_decisions_case_decision_sequence"),
        UniqueConstraint("run_id", "case_id", "decision_sequence", name="uq_decisions_run_case_sequence"),
        ForeignKeyConstraint(
            ["run_id", "case_id"], ["runs.id", "runs.case_id"], name="fk_decisions_run_case"
        ),
    )
```

Extend `Outbox.__table_args__` (from Task 1) to include the callback binding:

```python
    __table_args__ = (
        # PR 7b-core (013): partial to the claimable set — decision_callback terminals are never
        # pruned, so a full index would grow without bound and enter every claim plan.
        Index("ix_outbox_claim", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index(
            "ix_outbox_stream_claim", "case_id", "ordering_stream", "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
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
**Concurrency mutation:** in `pipeline._load` remove `with_for_update=True` from `session.get(Case, run.case_id, with_for_update=True)` (or substitute a `max(decision_sequence)+1` allocation for the locked-counter increment); rerun `test_concurrent_decides_serialize_via_case_lock` → it must FAIL **deterministically** at `assert not b_returned_from_load.wait(timeout=2)` — with no lock, B returns from `_load` while A is held (regardless of commit order), and the run also trips `uq_decisions_case_decision_sequence`. Restore.

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
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                      "state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
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
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        _mk_auto_decision(conn, d="dB", c="c1", r="rB", seq=1, ev_seq=2)  # same case+seq, other run
    assert "uq_decisions_case_decision_sequence" in str(exc.value)
    with engine.begin() as conn:  # multiple manual NULL-sequence decisions remain legal
        for i in range(2):
            conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                              "buy_enablement, policy_shas, manual) VALUES "
                              "(:d,'c1',NULL,'approve',0,'{}'::jsonb,"
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
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:  # same run_id, distinct seq/id
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                          "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
                          "('dB','c1','rA','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,2)"))
    assert "uq_decisions_run_id" in str(exc.value)
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
    "auto_null_sequence": ("ck_decisions_manual_sequence",  # automatic row, run set, sequence NULL (F2 hole)
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled','{}'::jsonb,false)"),
}


@pytest.mark.parametrize("case", list(_DECISION_IDENTITY_BAD))
def test_013_decision_identity_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    expected, sql = _DECISION_IDENTITY_BAD[case]
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
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(sql))
    assert expected in str(exc.value)
    engine.dispose()


@pytest.mark.parametrize("bad", ["0", "NULL"], ids=["zero", "auto_null_sequence"])
def test_013_decision_identity_update_negative(pg, bad):
    """UPDATE variants: a valid automatic decision cannot be UPDATEd into a zero OR NULL
    sequence — ck_decisions_manual_sequence fires on UPDATE too (F2)."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_013_di_upd_{bad.lower()}")  # Postgres folds unquoted "NULL" -> "null"
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(f"UPDATE decisions SET decision_sequence={bad} WHERE id='dA'"))
    assert "ck_decisions_manual_sequence" in str(exc.value)
    engine.dispose()


# INSERT negatives on `outbox` — each names the constraint it targets.
_OUTBOX_BINDING_BAD = {
    "callback_null_run": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1',NULL,'decision',1,'pending')"),
    # ck_outbox_kind_stream_identity, verified against REAL Postgres (see task-4-report.md
    # follow-up fix): a callback row with decision_sequence 0 or negative structurally violates
    # BOTH this CHECK and fk_outbox_decision_triple (no decisions row with a non-positive
    # sequence can ever exist, since ck_decisions_manual_sequence requires decision_sequence > 0
    # on every automatic row) — but Postgres validates CHECK/NOT NULL constraints synchronously
    # in the executor BEFORE a FK's AFTER-ROW trigger ever runs, so for a row violating both, the
    # CHECK's error is what Postgres actually raises; the FK trigger never gets a chance to fire.
    # Confirmed by running these two cases: psycopg.errors.CheckViolation, "violates check
    # constraint \"ck_outbox_kind_stream_identity\"" — not a ForeignKeyViolation. Pinning this
    # name (not fk_outbox_decision_triple) is what closes the false-green hole: disabling this
    # CHECK in the migration flips these two cases to failing (proven by the mutation test),
    # because the only thing left to reject the row is a ForeignKeyViolation whose message never
    # contains "ck_outbox_kind_stream_identity".
    "callback_zero_seq": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',0,'pending')"),
    "callback_negative_seq": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',-1,'pending')"),
    "callback_null_sequence": ("ck_outbox_kind_stream_identity",  # callback, run set, sequence NULL (F2 hole)
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, status) "
        "VALUES ('decision_callback','c1','rA','decision','pending')"),
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

    expected, sql = _OUTBOX_BINDING_BAD[case]
    url = _fresh_db(pg, f"kyc_mig_013_ob_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn, "c1")
        _mk_case(conn, "c2")  # for callback_wrong_case (a valid, different case)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)  # (rA,c1,1) exists
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        for stmt in sql.split("; "):
            conn.execute(text(stmt))
    assert expected in str(exc.value)
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
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, "
                          "ordering_stream, decision_sequence, status) "
                          "VALUES ('decision_callback','c1','rA','decision',1,'pending')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET decision_sequence=9 WHERE run_id='rA'"))  # (rA,c1,9) absent
    assert "fk_outbox_decision_triple" in str(exc.value)
    engine.dispose()


def test_013_outbox_callback_null_sequence_update_negative(pg):
    """UPDATE variant (F2): a valid callback cannot be UPDATEd to a NULL decision_sequence —
    ck_outbox_kind_stream_identity requires the sequence IS NOT NULL for a callback row."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_ob_upd_null")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, "
                          "ordering_stream, decision_sequence, status) "
                          "VALUES ('decision_callback','c1','rA','decision',1,'pending')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET decision_sequence=NULL WHERE run_id='rA'"))
    assert "ck_outbox_kind_stream_identity" in str(exc.value)
    engine.dispose()


def _seed_two_cases_two_runs(conn):
    """Re-audit F2 seed: two REAL cases with valid runs/decisions, plus a third run (rX,
    under c1) that carries NO decision yet — so the composite-FK negatives below are
    otherwise valid (no uq_decisions_run_id / per-case-sequence collision can mask the FK)."""
    _mk_case(conn, "c1")
    _mk_case(conn, "c2")
    _mk_auto_decision(conn, d="d1", c="c1", r="r1", seq=1, ev_seq=1)   # r1 belongs to c1
    _mk_auto_decision(conn, d="d2", c="c2", r="r2", seq=2, ev_seq=1)   # r2 belongs to c2
    conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                      "actor_json, payload_json, event_sequence) VALUES ('rX-ev','c1','rX','h','x',"
                      "'{}'::jsonb,'{}'::jsonb,2)"))
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                      "VALUES ('rX','c1','rX-ev','PUBLISH_DECISION')"))  # rX belongs to c1


def test_013_decision_case_must_match_run_case_insert_negative(pg):
    """Re-audit F2: fk_decisions_run_case binds decisions(run_id, case_id) → runs(id, case_id).
    A decision citing run rX (which belongs to c1) under case c2 passes BOTH single-column FKs
    and every uniqueness constraint — only the composite FK rejects it, asserted BY NAME."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_runcase_ins")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_two_cases_two_runs(conn)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('dx','c2','rX','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,3)"))
    assert "fk_decisions_run_case" in str(exc.value)
    engine.dispose()


def test_013_decision_case_must_match_run_case_update_negative(pg):
    """Re-audit F2 UPDATE variant: re-pointing a valid automatic decision at the OTHER real
    case (its run stays r1, which belongs to c1) must fail on fk_decisions_run_case — the
    seeds' distinct sequences guarantee no unique constraint can mask it."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_runcase_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_two_cases_two_runs(conn)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE decisions SET case_id='c2' WHERE id='d1'"))
    assert "fk_decisions_run_case" in str(exc.value)
    engine.dispose()


def test_013_orm_and_live_fk_parity(pg):
    """Re-audit F2+F9: the named relational constraints exist BOTH in ORM metadata
    (Base.metadata is Alembic's comparison target — a live-only FK reports drift and hides
    dependency ordering) AND in the live migrated DB: fk_outbox_case_id,
    fk_outbox_decision_triple, fk_decisions_run_case, uq_runs_id_case_id."""
    from kyc_tool.db.tables import DecisionRow, Outbox, Run

    orm_outbox_fks = {fk.constraint.name for fk in Outbox.__table__.foreign_keys}
    assert {"fk_outbox_case_id", "fk_outbox_decision_triple"} <= orm_outbox_fks
    orm_decision_fks = {fk.constraint.name for fk in DecisionRow.__table__.foreign_keys}
    assert "fk_decisions_run_case" in orm_decision_fks
    assert any(c.name == "uq_runs_id_case_id" for c in Run.__table__.constraints)

    url = _fresh_db(pg, "kyc_mig_013_fk_parity")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    insp = inspect(engine)
    live_outbox = {fk["name"] for fk in insp.get_foreign_keys("outbox")}
    assert {"fk_outbox_case_id", "fk_outbox_decision_triple"} <= live_outbox
    live_decisions = {fk["name"] for fk in insp.get_foreign_keys("decisions")}
    assert "fk_decisions_run_case" in live_decisions
    live_runs_uniques = {u["name"] for u in insp.get_unique_constraints("runs")}
    assert "uq_runs_id_case_id" in live_runs_uniques
    engine.dispose()
```

- [ ] **Step 7b: Shared valid-chain helper + bundle-pinning fixture adaptation (re-audit F3, part 2)** — the three automatic decisions in `tests/integration/test_bundle_pinning_ops.py::test_post_epoch_null_alert` (`d-a` ~225-229, `d-b` ~249-253, `d-c` ~273-277) insert `manual=false` with NO `decision_sequence`, which `ck_decisions_manual_sequence` now rejects. Add ONE shared valid-chain helper to `tests/conftest.py` (module level, near `sign_headers`) and use it at all three sites — the pinning tests need no callbacks, so the helper seeds only the decision + the matching case counter:

```python
def seed_automatic_decision(conn, *, case_id, run_id, decision_id, seq, engine_build_id=None):
    """PR 7b-core (re-audit F3): insert an automatic decision valid under 013's NULL-explicit
    shape CHECK — a positive per-case decision_sequence plus the matching
    cases.last_decision_sequence counter bump. The caller must already have seeded the case
    and the run. No callback row is created (tests that exercise delivery enqueue their own)."""
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, engine_build_id, decision_sequence) VALUES "
            "(:d,:c,:r,'x',0,'{}'::jsonb,'buy_locked_org_id_required','{}'::jsonb,false,:e,:s)"
        ),
        {"d": decision_id, "c": case_id, "r": run_id, "e": engine_build_id, "s": seq},
    )
    conn.execute(
        text("UPDATE cases SET last_decision_sequence = GREATEST(last_decision_sequence, :s) "
             "WHERE id = :c"),
        {"c": case_id, "s": seq},
    )
```

In `tests/integration/test_bundle_pinning_ops.py`, add the import (top of file): `from tests.conftest import seed_automatic_decision`, then replace each of the three raw decision INSERT `c.execute(text("INSERT INTO decisions …"))` calls with the helper (each case is distinct, so `seq=1` is a valid unique per-case sequence with a matching counter):

```python
        seed_automatic_decision(c, case_id="c-a", run_id="r-a", decision_id="d-a", seq=1,
                                engine_build_id="eng-1")   # (a) decision stamped / run NULL
```
```python
        seed_automatic_decision(c, case_id="c-b", run_id="r-b", decision_id="d-b", seq=1)
        # (b) decision engine NULL (helper default) / run stamped eng-1
```
```python
        seed_automatic_decision(c, case_id="c-c", run_id="r-c", decision_id="d-c", seq=1,
                                engine_build_id="eng-1")   # (c) both stamped
```

(The flagged/not-flagged expectations are unchanged — the helper preserves each site's `engine_build_id` value exactly; only the now-required sequence/counter shape is added.)

Run: `.venv/bin/pytest tests/integration/test_bundle_pinning_ops.py -v`
Expected: PASS (all fixtures valid under `ck_decisions_manual_sequence`; assertions unchanged).

- [ ] **Step 8: Run negatives + per-constraint INDEPENDENT mutation witnesses (manual, no commit)**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_two_runs or 013_two_decisions or 013_decision_identity or 013_outbox_binding or 013_decision_case_must_match_run_case or 013_orm_and_live_fk_parity" -v` → PASS.
Mutate each constraint away INDEPENDENTLY and confirm the NAMED test fails, then restore:
- drop `uq_decisions_case_decision_sequence` → `test_013_two_runs_same_case_sequence_rejected` FAILS (proving the outbox partial index is NOT the backstop).
- drop `uq_decisions_run_id` → `test_013_two_decisions_same_run_rejected` FAILS.
- drop `ck_decisions_manual_sequence` → `test_013_decision_identity_insert_negatives[auto_zero_seq]` + `[manual_with_run]` FAIL.
- remove **only** `AND decision_sequence IS NOT NULL` from `ck_decisions_manual_sequence` (the F2 NULL hole) → `test_013_decision_identity_insert_negatives[auto_null_sequence]` + `test_013_decision_identity_update_negative[auto_null_sequence]` FAIL (a NULL-sequence automatic row now passes the UNKNOWN implication).
- drop `ck_outbox_kind_stream_identity` → `test_013_outbox_binding_insert_negatives[callback_null_run]` + `[callback_stream_swap]` + `[email_with_run]` FAIL.
- remove **only** `AND decision_sequence IS NOT NULL` from `ck_outbox_kind_stream_identity` (the F2 NULL hole) → `test_013_outbox_binding_insert_negatives[callback_null_sequence]` + `test_013_outbox_callback_null_sequence_update_negative` FAIL.
- drop `fk_outbox_decision_triple` → `test_013_outbox_binding_insert_negatives[callback_wrong_case]` + `test_013_outbox_binding_update_negative` FAIL.
- drop `uq_outbox_decision_callback_run` → `test_013_outbox_binding_insert_negatives[dup_callback]` FAILS.
- **(re-audit F2) remove ONLY `fk_decisions_run_case` from the migration** (keep `uq_runs_id_case_id` and every other constraint) → `test_013_decision_case_must_match_run_case_insert_negative` AND `test_013_decision_case_must_match_run_case_update_negative` BOTH FAIL (the cross-case decision commits — every remaining constraint passes it). Restore.

- [ ] **Step 8b: Tighten `enqueue_decision_callback` to the FINAL required-keyword signature (re-audit F10)** — every caller now passes `decision_sequence` (the pipeline from Step 3; every plan-block test/helper — `test_outbox_fencing.py` passes it everywhere, Task 5's `test_outbox_supersession.py` `_enqueue_cb` is authored passing it, the Task-2 backfill test uses raw SQL; the existing `test_review_completed_event.py` seam wraps with `*args, **kwargs` and is unaffected). The Task-3 optional default was a staging convenience only. First the failing test — append to `tests/integration/test_decision_sequence.py`:

```python
def test_enqueue_decision_callback_requires_sequence_kwarg():
    """Re-audit F10: the FINAL 013 signature makes decision_sequence a REQUIRED keyword —
    omitting it fails at the Python boundary (TypeError), not late at commit on the
    ck_outbox_kind_stream_identity CHECK. (TypeError is raised at bind time, before the
    session argument is ever touched.)"""
    from kyc_tool.outbox.publisher import enqueue_decision_callback

    with pytest.raises(TypeError, match="decision_sequence"):
        enqueue_decision_callback(None, case_id="c1", run_id="r1", body={})
```

Run: `.venv/bin/pytest tests/integration/test_decision_sequence.py::test_enqueue_decision_callback_requires_sequence_kwarg -v`
Expected: FAIL — the Task-3 staging signature still defaults `decision_sequence=None`, so no `TypeError` is raised.

Then, in `src/kyc_tool/outbox/publisher.py`, change the signature to its FINAL form (this is the form the single atomic 013 commit ships — the optional staging form must NOT survive to the commit):

```python
def enqueue_decision_callback(
    session: Session, *, case_id: str, run_id: str, body: dict, decision_sequence: int
) -> None:
```

(body unchanged). Run: `.venv/bin/pytest tests/integration/test_decision_sequence.py tests/integration/test_outbox_fencing.py -v` → PASS (the TypeError test goes green; every other caller already passes the keyword; Task 5's `test_outbox_supersession.py` is authored against this final form).

- [ ] **Step 9: RED drift-guard step** — pipeline.py + tables.py + publisher.py (Step 8b) changed:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step).

- [ ] **Step 10: Re-pin the drift guard → GREEN** — re-pin one-liner → paste → rerun guard test → PASS.

- [ ] **Step 11: CHECKPOINT — full gate green, NO git commit (F1)**

```bash
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git status                # review the accumulated worktree diff — do NOT commit yet
```

---

## Task 5: Best-effort local `superseded` guard + lifecycle wiring (retention, metrics, UI-409, A6)

Adds the local guard to `process_once` (suppress an older decision-stream callback only when a higher-sequence decision already has a locally-stamped `published_at`), the fenced `_record_superseded` terminal, the retention narrowing that makes `decision_callback` rows the durable ordering authority 7b-activation reconciles against, separate metrics reporting, a UI-409 confirmation, the honest residual-risk tests (send-before-stamp AND — re-audit F4 — manual-current-then-late-automatic-callback), and the AUDIT:A6 amendment. The guard is explicitly a best-effort optimization — THREE residual reverts remain until 7b-activation: (1) send-before-stamp, (2) cross-replica, (3) a queued automatic callback delivered AFTER a later manual approval (manual rows have `run_id NULL`, no callback, and no sequence, so the local guard sees no higher locally-published automatic sequence).

**Files:**
- Modify: `src/kyc_tool/outbox/publisher.py` (`process_once` guard insertion point; add `_record_superseded`; module docstring 1-7)
- Modify: `src/kyc_tool/workers/retention.py` (`prune` 29-35)
- Modify: `src/kyc_tool/api/routes_metrics.py` (line 60)
- Modify: `AUDIT_FINDINGS.md` (A6, lines 61-66)
- Create: `tests/integration/test_outbox_supersession.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Consumes: `_record_superseded(self, row, token)` reuses the fenced pattern from Task 3; the guard reads `row.ordering_stream`, `row.decision_sequence`, `row.case_id`.
- Produces: `_record_superseded` sets `status='superseded'`, `resolved_at`, clears the claim tuple, moves the run `PUBLISH_DECISION→COMPLETE`, leaves `published_at` NULL; logs `outbox_superseded`. `prune` returns key `outbox_poc_email` (renamed; its outbox delete is now `kind='poc_email'`-scoped and no `superseded` prune is added). Metrics expose `outbox_by_status` including `superseded` but the pending/dead alert set excludes it.

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
                           "buy_enablement, policy_shas, manual, "
                           "decision_sequence) VALUES (:d,:c,:r,'approve',"
                           "10,'{}'::jsonb,'enabled','{}'::jsonb,false,:seq)"),
                      {"d": r + "-d", "c": case_id, "r": r, "seq": seq})
        s.commit()


def _enqueue_cb(session_factory, case_id, seq):
    r = f"{case_id}-r{seq}"
    with session_factory() as s:
        enqueue_decision_callback(s, case_id=case_id, run_id=r, body={"run_id": r}, decision_sequence=seq)
        s.commit()


def test_higher_delivered_then_older_superseded_a6(
    session_factory, settings, publisher, callback_capture, clean_db
):
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
        row = s.execute(text("SELECT status, resolved_at, delivered_at "
                             "FROM outbox WHERE run_id='c1-r1'")).one()
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
        # sent it, or the case's current state is a MANUAL approval (run_id NULL, no
        # callback, no sequence — nothing here compares higher), this predicate is false
        # and the older callback IS sent — reverts that remain expected until
        # 7b-activation's platform high-water (§5).
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
        counts["outbox_poc_email"] = session.execute(
            text(
                "DELETE FROM outbox WHERE kind='poc_email' AND status='delivered' "
                "AND delivered_at < now() - make_interval(days => :d)"
            ),
            {"d": retention_days},
        ).rowcount
```

and extend the module docstring's existing "deliberately NOT pruned" sentence (line 7) — same file, same idiom, because this is the durable-authority contract the spec's §Rollout defines:

```python
"""Retention job: `python -m kyc_tool.workers.retention`.

Prunes, per compliance policy (default 7 years, KYC_RETENTION_DAYS):
- audit_log rows past retention
- delivered poc_email outbox rows past retention (decision_callback rows are NEVER
  pruned — they are the durable ordering authority; see below)
- expired, never-verified poc_tokens past retention

Raw evidence objects and check rows are deliberately NOT pruned here — they are the
decision record. PR 7b-core adds decision_callback outbox rows to that list: the row IS
the durable ordering authority (id = order, status = local_status, payload_json = body)
that 7b-activation reconciles the platform against, and its body is a projection of the
never-pruned decisions/checks record, so retaining it retains nothing new. Only poc_email
rows are prunable here — their sensitive body (the raw POC token) is already destroyed at
delivery by publisher._record_delivered. A poc_email can never reach 'superseded' (that
terminal is decision-callback-only, ck_outbox_status_lifecycle), so the one outbox
statement below covers every prunable outbox terminal and no superseded prune is added.
"""
```

(Keep the docstring's existing wording for the first sentence; append from "PR 7b-core adds…". The `outbox_delivered` count key is renamed `outbox_poc_email` because it no longer means "all delivered rows"; nothing consumes it but the log line.)

In `src/kyc_tool/api/routes_metrics.py`, keep the `outbox_by_status` grouping (line 60, it already surfaces every status incl. `superseded`) and add an explicit alert-set comment/derived field right after it:

```python
            # PR 7b-core: decision_callback rows are never pruned, so `outbox` grows without
            # bound. Report the LIVE statuses exactly — those are what an operator acts on, and
            # they stay small — and the terminal history as one bounded count, so this endpoint's
            # cost does not grow with retained history. A GROUP BY over the whole table would.
            "outbox_by_status": _grouped(
                session,
                "SELECT status, count(*) FROM outbox "
                "WHERE status IN ('pending','dead') GROUP BY status",
            ),
            "outbox_terminal_total": session.execute(
                text("SELECT count(*) FROM outbox WHERE status IN ('delivered','superseded')")
            ).scalar_one(),
            # superseded is a governed terminal (best-effort local suppression, zero sends) — it is
            # counted in outbox_terminal_total and EXCLUDED from the pending/dead alert set below.
            "outbox_alerting": _grouped(
                session,
                "SELECT status, count(*) FROM outbox WHERE status IN ('pending','dead') GROUP BY status",
            ),
```

Append to `tests/integration/test_outbox_supersession.py`:

```python
class _InjectedFault(RuntimeError):
    pass


def test_residual_risk_send_before_stamp_reverts_expected(
    client, session_factory, settings, publisher, callback_capture, monkeypatch
):
    """Honest boundary (§5) via the REACHABLE lifecycle: seq 1 is an OLDER dead callback (lower
    outbox id); seq 2 is pending. The publisher SENDS seq 2 (HTTP #1, 2xx) but a fault injected
    in `_record_delivered` BEFORE its transaction leaves seq 2 pending/claimed and its decision
    UNSTAMPED (the real HTTP→terminal gap). seq 1 is requeued through the REAL UI endpoint; on the
    next pass its local guard predicate is false (seq 2 unstamped) and seq 1 IS sent (HTTP #2) —
    the exact residual revert 7b-activation later turns into a platform high-water no-op."""
    _seed_decisions(session_factory, "c3", [1, 2])  # decisions/runs at PUBLISH_DECISION, unstamped
    with session_factory() as s:  # seq 1 callback as an OLDER dead row (lower id)
        seq1_id = s.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
            "VALUES ('decision_callback','c3','c3-r1','decision',1,'dead') RETURNING id")).scalar_one()
        s.commit()
    _enqueue_cb(session_factory, "c3", 2)  # seq 2 pending, higher id

    def _boom(self, row, token):  # raise BEFORE any terminal transaction starts
        raise _InjectedFault("send-before-stamp: HTTP sent, terminal not committed")

    monkeypatch.setattr(type(publisher), "_record_delivered", _boom)
    with pytest.raises(_InjectedFault):
        publisher.process_once()  # claims seq 2 → _deliver (HTTP #1) → _boom
    assert len(callback_capture.requests) == 1  # seq 2 was really SENT
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE run_id='c3-r2'")).scalar_one() == "pending"
        assert s.execute(text("SELECT published_at FROM decisions WHERE id='c3-r2-d'")).scalar_one() is None

    monkeypatch.undo()  # restore the real _record_delivered
    resp = client.post(f"/ui/api/requeue/outbox/{seq1_id}")  # REAL UI requeue (dead → pending)
    assert resp.status_code == 200

    assert publisher.process_pending() >= 1     # seq 1 (lower id) claimed; guard predicate false
    assert len(callback_capture.requests) == 2  # HTTP #2 — the documented, expected revert
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM outbox WHERE run_id='c3-r1'")).scalar_one() == "delivered"


def test_guard_predicate_only_unit(session_factory, settings, publisher, callback_capture):
    """Named unit companion: with a higher decision UNSTAMPED (published_at NULL), the guard
    predicate is false, so an older callback is sent — isolates the predicate without the HTTP
    gap (kept separate from the lifecycle proof above, not a substitute)."""
    _seed_decisions(session_factory, "cu", [1, 2])  # both unstamped
    _enqueue_cb(session_factory, "cu", 1)
    assert publisher.process_pending() == 1
    assert len(callback_capture.requests) == 1  # seq 1 sent (no higher stamped delivery)


def _superseded_callback(session_factory, case_id, *, resolved_at="now()"):
    """Re-audit F8: 'superseded' is decision-only (lifecycle CHECK kind conjunct), so every
    fixture that needs a superseded row must build a VALID automatic decision+callback chain
    first — seq 1 on `case_id` via _seed_decisions — then insert its callback already
    superseded. Returns the outbox id."""
    _seed_decisions(session_factory, case_id, [1])
    r = f"{case_id}-r1"
    with session_factory() as s:
        oid = s.execute(
            text(
                "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
                "status, resolved_at) VALUES ('decision_callback',:c,:r,'decision',1,"
                f"'superseded', {resolved_at}) RETURNING id"
            ),
            {"c": case_id, "r": r},
        ).scalar_one()
        s.commit()
    return oid


def test_retention_keeps_decision_callbacks_and_prunes_poc_email(session_factory, clean_db):
    """PR 7b-core durable ordering authority (re-review 0ca264b P1/F4): retention's outbox
    prune is narrowed to kind='poc_email'. A retention-old delivered decision_callback — and
    an equally-old superseded one — SURVIVE with every manifest field intact, while an
    equally-old delivered poc_email is still deleted (proving a narrowing, not a disablement)."""
    from kyc_tool.workers.retention import prune

    old = "now() - interval '3000 days'"
    _superseded_callback(session_factory, "c4", resolved_at=old)
    # fk_outbox_decision_triple (013/Task 4) requires a matching decisions row for any
    # outbox row carrying a non-NULL (run_id, case_id, decision_sequence) triple — seed the
    # c4-r9/seq-9 decision the same way every other test in this file does before referencing
    # it from outbox, brief defect (evidence: ForeignKeyViolation on fk_outbox_decision_triple,
    # Key (run_id, case_id, decision_sequence)=(c4-r9, c4, 9) not present in "decisions").
    _seed_decisions(session_factory, "c4", [9])
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c4') ON CONFLICT DO NOTHING"))
        s.execute(text(
            f"INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            f"status, delivered_at, payload_json) VALUES ('decision_callback','c4','c4-r9',"
            f"'decision',9,'delivered',{old},'{{\"decision\":\"approve\"}}'::jsonb)"))
        s.execute(text(
            f"INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at) "
            f"VALUES ('poc_email','c4','email','delivered',{old})"))
        s.commit()

    counts = prune(session_factory, 7 * 365)

    assert counts["outbox_poc_email"] == 1  # the email IS pruned
    assert "outbox_superseded" not in counts  # no superseded prune exists (decision-only terminal)
    with session_factory() as s:
        rows = s.execute(text(
            "SELECT status, run_id, decision_sequence, delivered_at, payload_json "
            "FROM outbox WHERE case_id='c4' ORDER BY id")).all()
        assert [r.status for r in rows] == ["superseded", "delivered"]  # both callbacks survive
        assert s.execute(text(
            "SELECT count(*) FROM outbox WHERE case_id='c4' AND kind='poc_email'")).scalar_one() == 0
        # every field 7b-activation's manifest reads is still derivable from the surviving row
        delivered = rows[1]
        assert delivered.run_id == "c4-r9" and delivered.decision_sequence == 9
        assert delivered.delivered_at is not None  # the ORIGINAL timestamp, not now()
        assert delivered.payload_json == {"decision": "approve"}  # body intact, never redacted
        digest = s.execute(text(
            "SELECT encode(sha256(convert_to(payload_json::text,'UTF8')),'hex') FROM outbox "
            "WHERE case_id='c4' AND status='delivered'")).scalar_one()
        assert len(digest) == 64  # callback_body_digest is computable on demand — no stored column


def test_retention_still_prunes_its_other_targets(session_factory, clean_db):
    """The narrowing is scoped to the outbox: audit_log and poc_tokens pruning is unchanged."""
    from kyc_tool.workers.retention import prune

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('c4b') ON CONFLICT DO NOTHING"))
        s.execute(text(
            "INSERT INTO audit_log (case_id, action, actor, detail_json, at) "
            "VALUES ('c4b','x','system','{}'::jsonb, now() - interval '3000 days')"))
        s.commit()
    counts = prune(session_factory, 7 * 365)
    assert counts["audit_log"] == 1


def test_ui_requeue_409s_superseded(client, session_factory):
    # conftest `settings` leaves ui_admin_token empty → the console is open in tests
    # (dev/test trust model; see tests/unit/test_ops_auth.py + tests/integration/test_ui.py),
    # so no auth header is sent and a non-dead row must 409 (never requeued).
    oid = _superseded_callback(session_factory, "c5")
    resp = client.post(f"/ui/api/requeue/outbox/{oid}")
    assert resp.status_code == 409  # superseded is not 'dead' → requeue_outbox rejects it


def test_metrics_reports_superseded_out_of_the_alert_set(client, session_factory):
    """superseded is counted as terminal history, never in the pending/dead alert set — and the
    endpoint stays bounded: PR 7b-core never prunes decision callbacks, so `outbox` grows without
    limit and a GROUP BY over the whole table would make this endpoint's cost grow with it."""
    _superseded_callback(session_factory, "cm")  # a VALID superseded decision callback (F8)
    with session_factory() as s:
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, "
                       "status) VALUES ('poc_email','cm','email','pending')"))
        s.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, "
                       "status) VALUES ('poc_email','cm','email','dead')"))
        s.commit()
    body = client.get("/v1/metrics").json()
    # live statuses reported exactly; terminals are NOT enumerated per-status
    assert {"pending", "dead"} <= set(body["outbox_by_status"])
    assert "superseded" not in body["outbox_by_status"]
    assert "delivered" not in body["outbox_by_status"]
    assert body["outbox_terminal_total"] >= 1          # the superseded row is counted here
    assert "superseded" not in body["outbox_alerting"]  # governed terminal, not an alert


def test_manual_current_then_late_automatic_callback_sent_expected_pre_activation(
    client, session_factory, post_event, worker, publisher, callback_capture, sign
):
    """Re-audit F4 — the THIRD honest residual, real end-to-end: ingest → worker drives an
    automatic decision whose callback is ENQUEUED but NOT yet processed; a reviewer manual
    approval then becomes the case's current state (manual rows: run_id NULL, no callback,
    no decision_sequence); the publisher then runs. The local guard sees NO higher
    locally-published automatic sequence (manual allocates none), so the OLD automatic
    callback IS sent AFTER the manual approval — EXPECTED pre-activation behavior, closed
    only by 7b-activation's platform high-water (the strengthened 014 acceptance makes an
    unaccepted older callback a sticky no-op against a manual-current source)."""
    post_event("case-mc", "kyb.run_requested", {"company_legal_name": "A", "jurisdiction": "GB"})
    worker.run_until_idle()          # automatic decision made; callback enqueued, NOT processed
    with session_factory() as s:
        queued = s.execute(text("SELECT status, run_id FROM outbox "
                                "WHERE case_id='case-mc' AND kind='decision_callback'")).one()
    assert queued.status == "pending"  # the automatic callback is still queued

    body = json.dumps({
        "event_type": "reviewer.manual_approve",
        "occurred_at": "2026-07-24T00:00:00Z",
        "actor": {"type": "reviewer", "id": "rev-1"},
        "payload": {"reviewer_id": "rev-1", "note": "manual current"},
    }).encode()
    resp = client.post("/v1/cases/case-mc/events", content=body, headers=sign(body))
    assert resp.status_code == 200   # manual approval recorded — the case's CURRENT state

    assert publisher.process_pending() == 1          # the old automatic callback is claimed
    assert len(callback_capture.requests) == 1       # ... and the HTTP send REALLY happened
    assert callback_capture.requests[0]["body"]["run_id"] == queued.run_id
    with session_factory() as s:
        status = s.execute(text("SELECT status FROM outbox WHERE case_id='case-mc' "
                                "AND kind='decision_callback'")).scalar_one()
    assert status == "delivered"     # delivered AFTER manual approval — the documented residual
```

- [ ] **Step 6: Amend AUDIT:A6 + docstring** — in `AUDIT_FINDINGS.md`, replace the A6 **Resolution** (lines 65-66) with:

```markdown
- **Resolution**: at-least-once delivery from a transactional outbox; consumers dedupe.
  No component claims exactly-once. **PR 7b-core exception:** eligible non-superseded
  callbacks remain at-least-once and platform-deduped; a callback proven obsolete by a
  higher **locally-stamped** delivery is terminally suppressed (zero sends), audited, and
  retained under the governed `superseded` lifecycle (decision callbacks ONLY — a
  `poc_email` can never enter `superseded`; DB-enforced). This is a **best-effort local
  suppression** — NOT exactly-once and NOT platform-authoritative. THREE residual reverts
  remain until 7b-activation: send-before-stamp; cross-replica; and a queued automatic
  callback delivered AFTER a later manual approval (manual rows carry `run_id NULL`, no
  callback, and no sequence, so the local guard sees no higher locally-published
  automatic sequence).
```

In `src/kyc_tool/outbox/publisher.py`, insert these lines into the existing module docstring, immediately before its closing `"""` (line 7):

```python
    PR 7b-core: rows are claimed per (case_id, ordering_stream) under a fenced claim_token;
    a decision callback proven obsolete by a higher locally-stamped delivery is terminally
    `superseded` (zero sends; decision callbacks only — DB-enforced) — a best-effort LOCAL
    suppression, not exactly-once and not platform-authoritative. Three residual reverts
    remain for 7b-activation: (1) send-before-stamp; (2) cross-replica; (3) a queued
    automatic callback may be delivered AFTER a later manual approval — manual rows have
    run_id NULL, no callback, and no sequence, so the guard sees no higher locally-
    published automatic sequence. All three are expected pre-activation.
```

- [ ] **Step 7: CHECKPOINT — RED guard → re-pin → full gate green, NO git commit (F1)**

```bash
.venv/bin/pytest tests/integration/test_outbox_supersession.py -v     # all pass
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v # RED (src changed) — expected
# re-pin EXPECTED_ENGINE_SOURCE_HASH (one-liner), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v # GREEN
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git status                # review the accumulated worktree diff — do NOT commit yet (Task 6 commits)
```

---

## Task 6: Migration 013 downgrade race-safety + THE single atomic `013` commit (F1)

Hardens `013`'s `downgrade()`: the **first** statement is `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE`, then an `EXISTS(status='superseded')` preflight that refuses byte-stably before any DROP. A bare preflight is a TOCTOU race (its `ACCESS SHARE` is compatible with a publisher's `ROW EXCLUSIVE`, so a `superseded` row could commit between the check and the DDL, stranding an unknown terminal a pre-7b image cannot prune). **This task ends with the ONE commit of everything accumulated in Tasks 1-6** (schema + runtime + tests + the final re-pinned drift hash) — see the finish gate.

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
    refuse byte-stably (RuntimeError with the stable message). Re-audit F8: superseded is
    decision-only, so the seed is a VALID automatic decision+callback chain whose callback
    is superseded — a superseded poc_email is impossible by CHECK."""
    url = _fresh_db(pg, "kyc_mig_013_down_refuse")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, ordering_stream, "
                          "decision_sequence, status, resolved_at) VALUES "
                          "('decision_callback','c1','rA','decision',1,'superseded', now())"))
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:  # migration raises RuntimeError; alembic propagates it
        alembic_command.downgrade(cfg, "012")
    assert "superseded outbox row" in str(exc.value)


def test_013_downgrade_lock_prevents_concurrent_supersede(pg):
    """Drive the REAL migration downgrade() in a thread; pause it with a global
    after_cursor_execute barrier fired at its superseded preflight — which runs AFTER the
    production LOCK TABLE ... ACCESS EXCLUSIVE. A concurrent pending→superseded UPDATE must
    then FAIL with a lock timeout (SQLSTATE 55P03): it cannot slip between the preflight and
    the DDL. The seed is a VALID pending decision callback (re-audit F8 — the concurrent
    supersession must be lifecycle-legal, or the mutation witness would be masked by the
    kind conjunct instead of proving the race). MUTATION: moving/removing the LOCK (so the
    preflight holds only ACCESS SHARE) lets that UPDATE COMMIT — the except-OperationalError
    branch is never entered and the explicit pytest.fail fires."""
    import threading

    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import OperationalError

    url = _fresh_db(pg, "kyc_mig_013_down_lock")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, ordering_stream, "
                          "decision_sequence, status, next_attempt_at) VALUES "
                          "('decision_callback','c1','rA','decision',1,'pending', now())"))

    at_preflight = threading.Event()
    release = threading.Event()
    _PREFLIGHT = "from outbox where status='superseded'"  # the downgrade's count(*) preflight

    def _barrier(conn, cursor, statement, params, context, executemany):
        if _PREFLIGHT in statement.lower():
            at_preflight.set()          # downgrade holds ACCESS EXCLUSIVE here
            release.wait(timeout=15)    # hold the downgrade txn BETWEEN preflight and DDL

    down_err: list[Exception] = []

    def run_downgrade():
        try:
            alembic_command.downgrade(cfg, "012")
        except Exception as e:  # noqa: BLE001
            down_err.append(e)

    t = threading.Thread(target=run_downgrade)
    engineB = create_engine(url)
    # `event.listen` is process-wide, so register it INSIDE the try whose finally removes
    # it: a raise from Thread.start() would otherwise leak the barrier into every later
    # test in the session.
    event.listen(Engine, "after_cursor_execute", _barrier)
    try:
        t.start()
        assert at_preflight.wait(timeout=15)  # paused right after the preflight
        connB = engineB.connect()
        # Re-audit F5: begin B's transaction BEFORE any execute — SQLAlchemy 2 autobegin
        # would otherwise already own the transaction and connB.begin() would raise
        # InvalidRequestError before the test ever reached the lock. SET LOCAL is used
        # because the setting is now transaction-scoped by design.
        txB = connB.begin()  # manage B's txn EXPLICITLY so an unexpected success is COMMITTED
        connB.execute(text("SET LOCAL lock_timeout='2s'"))
        try:
            connB.execute(text("UPDATE outbox SET status='superseded', resolved_at=now() "
                               "WHERE case_id='c1'"))
        except OperationalError as exc:
            assert exc.orig.sqlstate == "55P03"  # correct code: blocked by ACCESS EXCLUSIVE
            txB.rollback()
        else:
            # MUTATION path (lock removed): the UPDATE slipped in — COMMIT it so the stranded
            # superseded state is really created, then fail the test.
            txB.commit()
            pytest.fail("concurrent pending→superseded committed between preflight and DDL — "
                        "the ACCESS EXCLUSIVE lock did not hold")
        finally:
            connB.close()
    finally:
        release.set()
        t.join(timeout=15)
        event.remove(Engine, "after_cursor_execute", _barrier)
        engineB.dispose()
    assert not t.is_alive()  # the downgrade thread terminated (a hung timeout must NOT pass)
    assert down_err == []    # ... and completed cleanly on the no-superseded DB
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k "013_downgrade_refuses_with_superseded or 013_downgrade_lock" -v`
Expected: `test_013_downgrade_refuses_with_superseded_row` FAILS — the current (Task-1/4) downgrade has no preflight, so it does NOT raise. `test_013_downgrade_lock_prevents_concurrent_supersede` also FAILS — the current downgrade never executes the preflight statement, so the barrier never fires and `assert at_preflight.wait(timeout=15)` fails.

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
    # composite decisions→runs case FK (re-audit F2): FK before its unique target
    op.drop_constraint("fk_decisions_run_case", "decisions", type_="foreignkey")
    op.drop_constraint("uq_runs_id_case_id", "runs", type_="unique")
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

- [ ] **Step 5: Mutation check (manual, no commit — re-audit F5)** — first run the exact test green: `.venv/bin/pytest tests/integration/test_migrations.py::test_013_downgrade_lock_prevents_concurrent_supersede -v`. Then, as the mutation witness, move `op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")` to AFTER the `count(*)` preflight (or delete it) and rerun the same selector → it must FAIL **because the concurrent supersession actually COMMITS**: the preflight now holds only ACCESS SHARE, B's `pending→superseded` UPDATE succeeds (no 55P03), the test COMMITS the stranded row, and the explicit `pytest.fail(...)` fires. Restore.

- [ ] **Step 6: THE single atomic `013` commit (everything from Tasks 1-6)**

The downgrade harden is migration-only, but the drift guard is already green from the Task-5 re-pin (kept green across this checkpoint). **Verification order (re-audit F3): run the four adapted EXISTING files' targeted selectors FIRST, then the whole suite** — a regression in an adapted fixture must surface by name, not inside the bulk run. Then make the ONE commit of the entire accumulated worktree (migration + all runtime + the frozen contract + all tests, including the four adapted existing files + the conftest helper + the final re-pinned hash):

```bash
.venv/bin/pytest tests/integration/test_ui.py tests/integration/test_phase4_platform.py \
                 tests/integration/test_migrations.py tests/integration/test_bundle_pinning_ops.py -v
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
git add alembic/versions/013_outbox_stream_separation.py \
        src/kyc_tool/migration_contracts/__init__.py src/kyc_tool/migration_contracts/v013_backfill.py \
        src/kyc_tool/db/tables.py src/kyc_tool/outbox/publisher.py src/kyc_tool/orchestration/pipeline.py \
        src/kyc_tool/workers/retention.py src/kyc_tool/api/routes_metrics.py AUDIT_FINDINGS.md \
        tests/integration/test_migrations.py tests/integration/test_outbox_fencing.py \
        tests/integration/test_decision_sequence.py tests/integration/test_outbox_supersession.py \
        tests/integration/test_ui.py tests/integration/test_phase4_platform.py \
        tests/integration/test_bundle_pinning_ops.py tests/conftest.py \
        tests/unit/test_migration_contract_v013.py tests/policy_driven/test_engine_build_id_guard.py \
        .agents/ROADMAP.md
git commit -m "feat(013): outbox stream separation + local decision ordering (schema + runtime, atomic)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"
```

- [ ] **Step 7: Finish gate — prove exactly ONE `013` commit + a real `012→013` upgrade of the complete file**

```bash
# exactly one 7b-core commit touched the 013 revision (no incomplete intermediate forms in history)
test "$(git log --format=%H -- alembic/versions/013_outbox_stream_separation.py | wc -l | tr -d ' ')" = "1"
```
Then, on a **dedicated DB held at 012**, upgrade once to the complete `013` and re-assert the final metadata/backfill/constraints (the persistent-DB hazard F1 guards against):
```bash
.venv/bin/pytest "tests/integration/test_migrations.py::test_013_upgrade_sets_stream_and_notnull_metadata" \
                 "tests/integration/test_migrations.py::test_013_backfill_orders_by_outbox_id_and_delivers_without_false_supersession" \
                 tests/unit/test_migration_contract_v013.py -v
```
(These upgrade a fresh `012` DB straight to the final single-file `013`; there is no intermediate `013` revision to land on.)

---

## Task 7: `verify_pr7b_core_backfill` ops CLI (schema-012-compatible, `SHARE`-locked)

Ships the pre-window diagnostic: raw parameterized SQL that imports **no** 013-only ORM, begins its transaction with `LOCK TABLE outbox IN SHARE MODE` **before any SELECT**, runs the exact read-only 013 preflights at `READ COMMITTED`, prints actionable ids, exits nonzero on any violation, and on a missing mapping prints the exact `BLOCKED_NO_AUTHORITATIVE_MAPPING` sentinel — with **no writes**.

**Files:**
- Create: `src/kyc_tool/ops/verify_pr7b_core_backfill.py`
- Create: `tests/integration/test_verify_pr7b_core_backfill.py`
- Modify: `tests/integration/test_migrations.py` (Step 4b — the schema-012 restore acceptance test, re-audit F1; it lives with the migration tests but lands HERE because it drives the real diagnostic CLI this task creates)
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `verify_backfill(session_factory) -> tuple[int, list[str]]` (exit code, offending ids) — takes the `SHARE` lock, runs preflights, no writes; `main() -> int` (real entry point, reads `get_settings().database_url`). Prints `BLOCKED_NO_AUTHORITATIVE_MAPPING <ids>` and returns nonzero on a missing mapping.
- Consumes: `kyc_tool.config.get_settings`, `kyc_tool.db.session.{make_engine, make_session_factory}` (schema-012-safe raw SQL only).

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_verify_pr7b_core_backfill.py`:

```python
"""PR 7b-core: schema-012 pre-window backfill diagnostic — REAL CLI entry point (subprocess),
the shared parity matrix (CLI + 013 both refuse), and the retention-race DB-lock half."""

import os
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from kyc_tool.db.session import make_engine, make_session_factory
from tests.integration.test_migrations import (
    _PARITY_BAD_SEEDS,
    _PARITY_SEED_VIOLATION,
    _config,
    _fresh_db,
    _seed_parity_bad,
)

pytestmark = pytest.mark.postgres


def _sf(url):
    return make_session_factory(make_engine(url))


def _seed_healthy(engine, *, delivered_at="now()"):
    """A valid decision + its delivered callback (delivered_at real). Pass an old delivered_at
    to make retention prune the callback."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                          "actor_json, payload_json, event_sequence) VALUES "
                          "('ev','c1','r','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                          "state) VALUES ('r','c1','ev','PUBLISH_DECISION')"))
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, "
                          "decision, score, gates_json, buy_enablement, "
                          "policy_shas, manual) VALUES "
                          "('d','c1','r','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"))
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
    BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel + decision/run ids, and leaves the DB on 012 with
    NO 013 columns and byte/count unchanged (proving the 014-downstream boundary behaviorally —
    no substitute migration ran)."""
    url = _fresh_db(pg, "kyc_diag_missing")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, "missing_callback")  # decision 'd' / run 'r', no callback
    before = engine.connect().execute(text("SELECT count(*) FROM outbox")).scalar_one()

    proc = _run_cli(url)
    assert proc.returncode != 0
    assert "BLOCKED_NO_AUTHORITATIVE_MAPPING" in proc.stdout  # exact sentinel (one token)
    assert "'d'" in proc.stdout and "'r'" in proc.stdout       # actionable ids
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "012"
        cols = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='outbox'"))}
        assert "claim_token" not in cols  # no 013 (or 014) migration ran
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == before  # no writes
    engine.dispose()


@pytest.mark.parametrize("name", list(_PARITY_BAD_SEEDS))
def test_cli_and_013_both_refuse_on_parity_state(pg, name):
    """The CLI and migration 013 share ONE frozen contract — both must refuse every invalid
    legacy state. The CLI is exercised through its REAL subprocess entry point (nonzero exit +
    the exact violation name / sentinel in stdout); the migration refuses BEFORE any DDL."""
    url = _fresh_db(pg, f"kyc_diag_parity_{name}")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, name)

    proc = _run_cli(url)  # the REAL entry point, not the helper
    assert proc.returncode != 0
    assert (_PARITY_SEED_VIOLATION[name] in proc.stdout) or (
        "BLOCKED_NO_AUTHORITATIVE_MAPPING" in proc.stdout
    )

    with pytest.raises(RuntimeError):           # the real 013 upgrade refuses too
        command.upgrade(_config(url), "013")
    with engine.connect() as conn:
        cols = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='outbox'"))}
    assert "claim_token" not in cols            # refusal happened before the column adds
    engine.dispose()


# The schema-012 retention DELETE, frozen as a literal. The hazard is an OLD retention process
# still in flight during cutover — its SQL is fixed by the already-deployed pre-013 image, so this
# string must NOT track the current retention module. (Task 5 narrowed the live prune() to
# kind='poc_email'; test_retention_keeps_decision_callbacks_and_prunes_poc_email covers that the
# NEW worker never deletes a callback, which is why the new prune() cannot produce this state.)
_LEGACY_012_RETENTION_DELETE = (
    "DELETE FROM outbox WHERE status='delivered' "
    "AND delivered_at < now() - make_interval(days => :d)"
)


@pytest.mark.parametrize("mode", ["commit", "rollback"])
def test_cli_share_lock_blocks_until_legacy_retention_resolves(pg, mode):
    """Retention-race DB-lock half (the automatable proof), for BOTH resolutions.

    Connection A plays the pre-013 retention worker: it runs the FROZEN schema-012 delivered-outbox
    DELETE and holds the transaction open (ROW EXCLUSIVE) without resolving. The CLI subprocess's
    `LOCK TABLE outbox IN SHARE MODE` must BLOCK while A holds — in BOTH modes. On **commit** the
    callback is truly gone -> CLI nonzero + offending ids. On **rollback** it survives -> CLI exit 0.

    Driving A as an explicit second connection (rather than threading the real worker behind a
    global `after_cursor_execute` barrier) is deliberate: it models the actual hazard, and it means
    this test registers no process-wide listener and starts no thread, so it cannot leak either into
    the rest of the session. MUTATION removing the SHARE lock fails the blocked assertion in both
    modes AND the commit-mode nonzero outcome.
    (The external zero-retention-process attestation stays a runbook TODO(integration).)"""
    url = _fresh_db(pg, f"kyc_diag_race_{mode}")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_healthy(engine, delivered_at="now() - interval '3000 days'")  # old → legacy prune deletes it

    proc = None
    engineA = create_engine(url)
    connA = engineA.connect()
    try:
        txA = connA.begin()                       # explicit: hold ROW EXCLUSIVE open, unresolved
        deleted = connA.execute(text(_LEGACY_012_RETENTION_DELETE), {"d": 7 * 365}).rowcount
        assert deleted == 1, "the legacy DELETE must actually remove the seeded callback"

        proc = subprocess.Popen(
            [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
            env={**os.environ, "KYC_DATABASE_URL": url},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(3)
        assert proc.poll() is None  # STILL BLOCKED on the SHARE lock in BOTH modes

        if mode == "commit":
            txA.commit()
        else:
            txA.rollback()

        out, err = proc.communicate(timeout=30)
        if mode == "commit":
            assert proc.returncode != 0 and "'d'" in out, out + err   # callback gone → missing mapping
        else:
            assert proc.returncode == 0, out + err                    # callback restored → parity clean
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)          # reap: kill alone leaves a zombie
        assert proc is None or proc.poll() is not None, "CLI subprocess outlived the test"
        connA.close()
        engineA.dispose()
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
parity matrix as migration 013 (kyc_tool.migration_contracts.v013_backfill), imports NO 013-only ORM.

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
from kyc_tool.migration_contracts.v013_backfill import BLOCKED_SENTINEL, MISSING_CALLBACK, run_parity


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

- [ ] **Step 4b: The schema-012 restore acceptance test (re-audit F1)** — the runbook's restore-or-block recovery (Task 9 Step 5, step 0.5) is now an EXECUTABLE identity contract: a restore must re-insert the ORIGINAL `outbox.id` with the recorded evidence tuple `(decision_id, run_id, case_id, original_outbox_id, body digest)`; a default-id INSERT is prohibited (it silently reverses the legacy order authority: the pruned OLDER callback would be re-ranked NEWER). This test executes that exact contract end-to-end on schema 012 — append to `tests/integration/test_migrations.py`:

```python
# The runbook's executable restore acceptance predicate (Task 9 Step 5, step 0.6c). POSITIVE and
# fail-closed: it asserts the restored row EXISTS and matches every recorded component, and must
# return EXACTLY ONE row. A negative "select the mismatches, expect zero rows" formulation is
# prohibited — an absent row, or one restored under the wrong run_id, matches nothing and is then
# indistinguishable from an exact match (re-review 0ca264b P2).
# EVERY schema-012 outbox column is recorded, restored and positively compared. The procedure runs
# BEFORE 013, so it must not name a 013-only column: `resolved_at` and the claim tuple do not exist
# yet, while `attempts`, `next_attempt_at`, `last_error` and `created_at` DO and are part of the row.
# `IS NOT DISTINCT FROM` throughout so a NULL matches a NULL rather than yielding UNKNOWN.
_RESTORE_ACCEPTANCE_SQL = text(
    "SELECT 1 AS accepted FROM outbox o JOIN decisions d ON d.id = :decision_id "
    "WHERE o.id = :original_outbox_id AND o.kind = 'decision_callback' "
    "AND o.case_id = :case_id AND o.run_id IS NOT DISTINCT FROM :run_id "
    "AND d.case_id = o.case_id AND d.run_id IS NOT DISTINCT FROM o.run_id "
    "AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest "
    "AND o.status = :original_status "
    "AND o.delivered_at IS NOT DISTINCT FROM :original_delivered_at "
    "AND o.attempts = :original_attempts "
    "AND o.next_attempt_at IS NOT DISTINCT FROM :original_next_attempt_at "
    "AND o.last_error IS NOT DISTINCT FROM :original_last_error "
    "AND o.created_at IS NOT DISTINCT FROM :original_created_at"
)

# Read-only, monotonic sequence precondition (step 0.6d). The id the sequence would hand the NEXT
# writer must already be PAST the restored id. is_called is load-bearing: on a never-called sequence
# last_value is the id nextval will RETURN, not one already consumed. This never writes — a live
# `setval(GREATEST(max(id), last_value))` can rewind the sequence under a concurrent nextval (a
# non-transactional object; LOCK TABLE does not fence it) and is prohibited (re-review 0ca264b P1).
_SEQ_HIGH_WATER_SQL = text(
    "SELECT last_value + (CASE WHEN is_called THEN 1 ELSE 0 END) AS next_id FROM outbox_id_seq"
)


def test_012_restore_acceptance_rejects_default_id_then_accepts_original(pg):
    """Re-audit F1 (+ re-review 0ca264b P1/P2) — the restore acceptance contract, on schema 012
    with the REAL diagnostic CLI and the REAL 013 upgrade:
    (1) two callbacks (ids captured), full evidence tuples recorded (simulating the backup);
    (2) the OLDER callback is deleted (simulating a retention prune);
    (3) the read-only sequence precondition holds — a pruned historical id is BELOW the
        high-water mark, so no sequence write is needed (and none is performed);
    (4) an ABSENT row is REJECTED (the fail-open hole the negative predicate had);
    (5) a DEFAULT-id INSERT restore is REJECTED (its id differs — silent order reversal);
    (6) each evidence component, mutated alone, is REJECTED (every one is load-bearing);
    (7) restored with the EXACT original id AND original lifecycle fields → exactly one
        accepted row, the real diagnostic CLI runs clean, 013 upgrades, and old/new map to
        decision_sequence 1/2 (order authority preserved)."""
    import datetime as _dt
    import os
    import subprocess
    import sys

    url = _fresh_db(pg, "kyc_mig_012_restore_acceptance")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        oid_a = _seed_legacy_callback(conn, case_id="c1", run_id="rA", decision_id="dA",
                                      ev_seq=1, status="delivered")
        oid_b = _seed_legacy_callback(conn, case_id="c1", run_id="rB", decision_id="dB",
                                      ev_seq=2, status="pending")
        # A is a PREVIOUSLY RETRIED callback: without attempts/next_attempt_at/last_error/created_at
        # in the evidence tuple, a restore that silently reset its retry clock and error history
        # would still pass the predicate (re-review 0ca264b F4).
        conn.execute(text(
            "UPDATE outbox SET attempts=3, last_error='upstream 503', "
            "next_attempt_at=now() - interval '2 days' WHERE id=:i"), {"i": oid_a})
        assert oid_a < oid_b  # A is the authoritative OLDER callback
        # the evidence tuple the operator captures FROM BACKUP before restoring — identity,
        # body digest, AND the original lifecycle fields (substituting now() is prohibited)
        evidence = {
            r.run_id: {"decision_id": d, "run_id": r.run_id, "case_id": r.case_id,
                       "original_outbox_id": r.id, "body_digest": r.digest,
                       "original_status": r.status, "original_delivered_at": r.delivered_at,
                       "original_attempts": r.attempts,
                       "original_next_attempt_at": r.next_attempt_at,
                       "original_last_error": r.last_error,
                       "original_created_at": r.created_at}
            for r, d in zip(
                conn.execute(text(
                    "SELECT id, run_id, case_id, status, delivered_at, attempts, "
                    "next_attempt_at, last_error, created_at, "
                    "encode(sha256(convert_to(payload_json::text,'UTF8')),'hex') AS digest "
                    "FROM outbox ORDER BY id")).all(),
                ["dA", "dB"], strict=True,
            )
        }
    ev = evidence["rA"]
    assert ev["original_status"] == "delivered" and ev["original_delivered_at"] is not None
    with engine.begin() as conn:  # simulate the retention prune of the OLD delivered callback
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": oid_a})

    def _accepted(conn, e):
        return conn.execute(_RESTORE_ACCEPTANCE_SQL, e).fetchall()

    with engine.begin() as conn:
        # (3) READ-ONLY precondition: the next id the sequence would hand out is already past
        # the restored id, so the gap is safe to fill and NO sequence write is required.
        next_id = conn.execute(_SEQ_HIGH_WATER_SQL).scalar_one()
        assert next_id > ev["original_outbox_id"]
        # (4) fail-closed on an ABSENT row: the pre-rev-10 negative predicate returned zero
        # mismatches here and would have called this "accepted".
        assert _accepted(conn, ev) == []

    with engine.begin() as conn:  # (5) the PROHIBITED default-id restore
        bad_id = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, payload_json, status, delivered_at) "
            "VALUES ('decision_callback','c1','rA','{}'::jsonb,'delivered', now()) RETURNING id"
        )).scalar_one()
        assert bad_id > oid_b  # a fresh id — the restored row would rank NEWER than rB
        assert _accepted(conn, ev) == []  # REJECTED: the id does not match the evidence
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": bad_id})  # operator undoes it

    with engine.begin() as conn:  # (7a) the governed restore: EXACT id AND exact lifecycle
        conn.execute(text(
            "INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, delivered_at, "
            "attempts, next_attempt_at, last_error, created_at) "
            "VALUES (:i,'decision_callback',:c,:r,'{}'::jsonb,:st,:da,:at,:na,:le,:ca)"
        ), {"i": ev["original_outbox_id"], "c": ev["case_id"], "r": ev["run_id"],
            "st": ev["original_status"], "da": ev["original_delivered_at"],
            "at": ev["original_attempts"], "na": ev["original_next_attempt_at"],
            "le": ev["original_last_error"], "ca": ev["original_created_at"]})
        assert _accepted(conn, ev) == [(1,)]  # ACCEPTED: exactly one row, every component matched

        # (6) every evidence component is separately load-bearing: mutate one at a time, and
        # the acceptance predicate must reject the (unchanged, correctly restored) row.
        mutations = {
            "decision_id": "d-nope",
            "run_id": "r-nope",
            "case_id": "c-nope",
            "original_outbox_id": ev["original_outbox_id"] + 1000,
            "body_digest": "0" * 64,
            "original_status": "pending",
            "original_delivered_at": ev["original_delivered_at"] + _dt.timedelta(seconds=1),
            "original_attempts": ev["original_attempts"] + 1,
            "original_next_attempt_at": (
                ev["original_next_attempt_at"] + _dt.timedelta(seconds=1)
                if ev["original_next_attempt_at"] is not None
                else _dt.datetime.now(_dt.UTC)
            ),
            "original_last_error": "not the recorded error",
            "original_created_at": ev["original_created_at"] + _dt.timedelta(seconds=1),
        }
        for key, bad in mutations.items():
            assert _accepted(conn, {**ev, key: bad}) == [], f"{key} is not load-bearing"

    proc = subprocess.run(  # the REAL diagnostic must now be clean
        [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
        env={**os.environ, "KYC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    alembic_command.upgrade(cfg, "013")  # the real migration ranks by outbox.id
    with engine.connect() as conn:
        seqs = {r.id: r.decision_sequence for r in conn.execute(
            text("SELECT id, decision_sequence FROM decisions WHERE case_id='c1'"))}
    assert seqs == {"dA": 1, "dB": 2}  # the restored OLD callback keeps sequence 1
    engine.dispose()
```

Run: `.venv/bin/pytest tests/integration/test_migrations.py::test_012_restore_acceptance_rejects_default_id_then_accepts_original -v`
Expected: PASS. **Mutation witnesses (manual):** (a) change the governed restore INSERT to omit the explicit `id` → the `== [(1,)]` assertion must FAIL (the predicate rejects it), and — if the predicate were also skipped — the final assertion would fail with `{"dA": 2, "dB": 1}` (the silent order reversal this contract exists to block). (b) Replace `_RESTORE_ACCEPTANCE_SQL` with the pre-rev-10 negative form (`SELECT ... WHERE ... AND (o.id <> :original_outbox_id OR ...)`, zero rows = accepted) → the absent-row assertion at (4) must FAIL, proving the fail-open hole is what the positive predicate closes. Restore both.

- [ ] **Step 5: Mutation check (manual, no commit)** — remove `s.execute(text("LOCK TABLE outbox IN SHARE MODE"))`; rerun `.venv/bin/pytest tests/integration/test_verify_pr7b_core_backfill.py::test_cli_share_lock_blocks_until_legacy_retention_resolves -v` → BOTH params must FAIL the `assert proc.poll() is None` blocked check (the CLI no longer waits on connection A's uncommitted legacy DELETE), and the `[commit]` param additionally fails its nonzero-outcome assertion (it reads the still-visible callback before A commits). Restore.

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
        tests/integration/test_migrations.py \
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
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from alembic import command
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
        before = [r.next_attempt_at for r in conn.execute(
            text("SELECT next_attempt_at FROM outbox ORDER BY id"))]

    proc = _run_reset(url)
    assert proc.returncode != 0
    assert "pre-013" in proc.stderr.lower() or "claim_token" in proc.stderr.lower()
    with engine.connect() as conn:
        after = [r.next_attempt_at for r in conn.execute(
            text("SELECT next_attempt_at FROM outbox ORDER BY id"))]
    assert after == before  # nothing written
    engine.dispose()


def test_reset_clears_only_claimed_preserves_next_attempt_subprocess(pg):
    url = _fresh_db(pg, "kyc_reset_013")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
                          "claim_lease_expires_at, claimed_by, "
                          "next_attempt_at) VALUES ('poc_email','c1','email',"
                          "'pending', gen_random_uuid(), now(), 'w1', now() + interval '5 minutes')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','email','pending', now() + interval '9 minutes')"))
        before = [r.next_attempt_at for r in conn.execute(
            text("SELECT next_attempt_at FROM outbox ORDER BY id"))]

    proc = _run_reset(url)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT claim_token, claim_lease_expires_at, claimed_by, next_attempt_at "
                                 "FROM outbox ORDER BY id")).all()
    assert rows[0].claim_token is None
    assert rows[0].claim_lease_expires_at is None and rows[0].claimed_by is None
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

    err: list[Exception] = []

    def run_reset():
        try:
            resetter.reset_claims(_sf(url))
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=run_reset)
    # `event.listen` is process-wide and `Thread.start()` can raise: register and start INSIDE the
    # try whose finally releases the barrier, removes the listener, and joins — otherwise a raise
    # between them leaks the hook (and possibly a live thread) into every later test.
    try:
        event.listen(Engine, "before_cursor_execute", _barrier)
        t.start()
        assert at_readback.wait(timeout=15)  # reset's UPDATE done, about to read back
        with engine.begin() as conn:          # concurrent writer commits a NEW claimed row
            conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
                              "claim_lease_expires_at, claimed_by) VALUES "
                              "('poc_email','c1','email','pending', "
                              "gen_random_uuid(), now(), 'w2')"))
        go.set()
        t.join(timeout=15)
    finally:
        go.set()                                   # always release, even on an early failure
        t.join(timeout=15)
        event.remove(Engine, "before_cursor_execute", _barrier)
        assert not t.is_alive(), "reset thread outlived the test"  # a hung thread must not pass
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

## Task 9: Docs + governance

Documents stream separation + local ordering + the 7b-core/activation boundary in `OVERVIEW.md`; the full ordered step-0 pre-window + drained cutover + reversible-before-first-supersession rollback in `RUNBOOK.md`/`DEPLOYMENT.md`; and the A6 exception + backfill-as-deterministic-reconstruction + local/cross-replica boundary in `AUDIT_FINDINGS.md`. Docs-only — no `src/` change, no drift re-pin. **The `.agents/ROADMAP.md §C` `shipped` flip is NOT here** — `test_migration_lineage.py` asserts `authored_reserved == shipped` by reading BOTH the ROADMAP and `alembic/versions/` off disk, so the flip is forced the moment `013_outbox_stream_separation.py` exists (Task 1 Step 3b) and must ride the SAME atomic commit as the migration (Task 6). Deferring it to Task 9 would leave Task 6's commit red in CI, which checks out the commit rather than the worktree.

**Files:**
- Modify: `docs/OVERVIEW.md`, `docs/RUNBOOK.md`, `docs/DEPLOYMENT.md`
- Modify: `AUDIT_FINDINGS.md` (backfill provenance note; A6 already amended in Task 5 — confirm)
- Create: `tests/unit/test_docs_cutover_parity.py` (RUNBOOK/DEPLOYMENT full-body parity)
- Create: `tests/integration/test_rollback_command.py` (the exact `alembic … downgrade 012` command)
- Verify: `tests/unit/test_migration_lineage.py` (already green — flipped in Task 1, committed in Task 6)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-8 (CLIs by name, migration behavior).
- Produces: docs + governance only. The lineage invariant `head=013 ∈ shipped`, `pending[0]=014` was established in Task 1 and committed in Task 6.

- [ ] **Step 1: Confirm the lineage guard is already green** — it was satisfied in Task 1 Step 3b and committed atomically with `013` in Task 6; this step only proves no later task regressed it:

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py -v`
Expected: PASS.

- [ ] **Step 2: Confirm the ROADMAP row committed in Task 6 reads exactly this** (do not re-edit it here — it is already in the atomic `013` commit):

```markdown
| PR 7b-core | 8 | shipped | 013 | `outbox.ordering_stream` (NOT NULL) + `case_id` NOT NULL + per-(case,stream) claim; `decisions.decision_sequence` + `cases.last_decision_sequence` + a **best-effort** local `superseded` guard (higher *locally-stamped* delivery only; send-before-stamp / cross-replica / manual-current-then-late-automatic-callback reverts remain for 7b-activation); identity + ordering (`UNIQUE decisions(case_id, decision_sequence)` [per-case namespace] + `UNIQUE(run_id)` + triple FK + composite `decisions(run_id,case_id)→runs(id,case_id)` FK + partial callback index); fenced claim (`claim_token`); exhaustive per-status lifecycle CHECKs (`superseded` decision-only); `ordering_stream`/`case_id` real `SET NOT NULL` (drained cutover, **reversible-before-first-supersession** downgrade) |
```

(Leave 7b-activation `014` and every other row unchanged. Also update the prose note near line 12-14 if it says "7b-core is next" — change to "7b-core shipped (`013`); 7b-activation (`014`) is next".)

- [ ] **Step 3: Re-run the lineage guard after the docs edits**

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
publisher cannot overwrite a reclaimer's terminal. **Boundary:** THREE residual reverts are NOT
closed here and remain expected until 7b-activation (`014`) adds the platform high-water mark:
send-before-stamp; cross-replica; and a queued automatic callback delivered AFTER a later manual
approval (manual approvals carry no run, no callback, and no sequence, so the local guard has no
higher locally-published automatic sequence to compare). 7b-core does not claim exactly-once (see
`AUDIT_FINDINGS.md` A6).
```

- [ ] **Step 4b: Correct `docs/RUNBOOK.md`'s existing `## Retention & compliance` section (re-review 0ca264b F3)** — it still promises that *all* delivered outbox rows prune, which 013 makes false. Replace the prune sentence with the `poc_email`-scoped one and add the `decision_callback` non-pruning paragraph naming it a recorded retention deviation whose erasure scope includes the callback snapshot. Do NOT merely append the cutover section below and leave the stale text in place — the docs-parity test pins the new wording.

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
    restore from authoritative backup the EXACT callback row, OR remain on 012 in
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. 014 is
    downstream and cannot repair this. Never fabricate a callback, delete a decision, or fall back to
    `decided_at`. On EVERY abort path, explicitly re-enable OR deliberately keep-frozen retention.
0.6 RESTORE ACCEPTANCE CONTRACT (the restore in 0.5 is an executable identity requirement, not
    advice — the backfill ranks by `outbox.id`, so a wrong id silently reverses the legacy order):
    (a) BEFORE restoring, record from the backup the authoritative evidence tuple per missing
        callback: `decision_id` plus **every schema-012 `outbox` column** —
        `(id, kind, case_id, run_id, payload_json, status, attempts, next_attempt_at,
        delivered_at, last_error, created_at)` — with `body_digest` computed ON THE BACKUP ROW as
        `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`. `md5(...)` is prohibited.
        This procedure runs BEFORE 013, so it must name NO 013-only column: `resolved_at`,
        `ordering_stream`, `decision_sequence` and the claim tuple do not exist yet. Omitting the
        retry/audit columns is what makes "exact" false — a previously retried callback restored
        with a reset `attempts`/`next_attempt_at`/`last_error` is NOT the row that was pruned.
    (b) The restore MUST re-insert the ORIGINAL primary key AND every other recorded column:
        `INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, attempts,
         next_attempt_at, delivered_at, last_error, created_at) VALUES (<original_outbox_id>, ...)`
         — every value from the evidence tuple, none defaulted. A
        default-id INSERT is prohibited (it allocates a fresh id and re-ranks the restored older
        callback as newer), and substituting `now()` for `delivered_at` is prohibited (it falsifies
        the audit record). If the original id is unavailable, do NOT restore: remain
        `BLOCKED_NO_AUTHORITATIVE_MAPPING` on 012.
    (c) ACCEPTANCE PREDICATE — POSITIVE and fail-closed. Run per restored callback; it MUST return
        EXACTLY ONE row before proceeding. ZERO rows = still blocked. Do NOT invert it into a
        "select the mismatches, expect zero rows" form: an absent row (or one restored under the
        wrong `run_id`) matches nothing and would read as accepted.
        `SELECT 1 AS accepted FROM outbox o JOIN decisions d ON d.id = :decision_id
         WHERE o.id = :original_outbox_id AND o.kind = 'decision_callback'
           AND o.case_id = :case_id AND o.run_id = :run_id
           AND d.case_id = o.case_id AND d.run_id = o.run_id
           AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest
           AND o.status = :original_status
           AND o.delivered_at IS NOT DISTINCT FROM :original_delivered_at
           AND o.attempts = :original_attempts
           AND o.next_attempt_at IS NOT DISTINCT FROM :original_next_attempt_at
           AND o.last_error IS NOT DISTINCT FROM :original_last_error
           AND o.created_at IS NOT DISTINCT FROM :original_created_at;`
        Every schema-012 column is compared, so dropping any one of them from the restore fails
        the predicate. `IS NOT DISTINCT FROM` is used for nullables so NULL matches NULL.
    (d) SEQUENCE PRECONDITION — READ-ONLY. Step 0 runs with API/pipeline/outbox writers LIVE (only
        retention is suspended; the first hard-stop is cutover step 2), so NO sequence write happens
        here. `setval(...)` is prohibited on this path: a sequence is a non-transactional object,
        `LOCK TABLE outbox IN SHARE MODE` does NOT fence it, and a read-modify-write can rewind it
        below an already-allocated id under a concurrent `nextval` → duplicate primary key. It is
        also unnecessary: a retention-pruned id fills a gap BELOW the advanced sequence. Instead,
        BEFORE restoring, confirm the id the sequence would hand the next writer is already past it
        (`pg_get_serial_sequence('outbox','id')` names the sequence; expected `public.outbox_id_seq`):
        `SELECT last_value + (CASE WHEN is_called THEN 1 ELSE 0 END) > <original_outbox_id> AS ok
         FROM outbox_id_seq;`
        It MUST be `true` — the quantity only ever increases under `nextval`, so observing it once
        with writers live is durable. If it is `false` the row is NOT a prune (it is a restore from a
        divergent lineage): ABORT step 0. A genuine sequence repair is a SEPARATE DRAINED action —
        run it only after cutover step 2 has hard-stopped and attested every writer at zero, using a
        sequence-serializing statement (`ALTER SEQUENCE`, which excludes concurrent `nextval`, unlike
        `setval`), then read the value back before any writer restarts:
        **Executable — `RESTART WITH` takes a literal, not an expression, so `<max(id)+1>` is
        invalid SQL and must never be pasted mid-outage. Compute it into a psql variable first:**
        ```
        \set ON_ERROR_STOP on
        BEGIN;
        LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE;   -- writers are already stopped; this is the fence
        SELECT COALESCE(max(id), 0) + 1 AS next_id FROM outbox \gset
        ALTER SEQUENCE outbox_id_seq RESTART WITH :next_id;
        SELECT last_value, is_called FROM outbox_id_seq;   -- expect (:next_id, f)
        COMMIT;
        -- read back BEFORE any writer restarts: the next allocation must be exactly :next_id
        SELECT nextval('outbox_id_seq') = :next_id AS allocation_ok;
        SELECT setval('outbox_id_seq', :next_id, false);   -- undo the probe's consumption
        ```
        Run it through the shipped ops entry point where one exists; the drain fence above is part
        of the procedure, not advice — a run without it must fail.
    (e) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer;
   attest zero at the orchestrator.
3. Run the shipped `python -m kyc_tool.ops.requeue_interrupted_jobs`. NO outbox reset here — the
   pre-013 schema has no claim columns; an interrupted old claim simply waits until its already-
   recorded `next_attempt_at`. Preserve every pending row's `next_attempt_at`.
4. Run `.venv/bin/alembic -c alembic.ini upgrade head` (013) — the deployment image runs its exact
   equivalent. This repeats the §0 parity preflights under the zero-writer boundary and is the
   authoritative fail-closed check (the pre-window diagnostic is an early detector, not a substitute).
5. Start API only, probe `/readyz`, then start + attest the fenced workers. No mutating prod smoke.
6. RESUME (forward completion): re-enable retention, autoscaling/restarts, and submissions, and
   remove the composer edge block. The window is NOT closed until all five paused controls
   (retention, autoscaling, restarts, submissions, composer edge block) are restored or removed.

**Rollback — a two-branch maintenance state machine (as drained as the forward cutover). BOTH branches
end in a full resume — never leave the system stopped or retention frozen:**
R1. Pause submissions, edge-block the composer, disable autoscaling/restarts.
R2. Hard-stop and orchestrator-attest zero API, pipeline, outbox, `dev_worker`, retention, every writer.
R3. While 013 still exists, run `python -m kyc_tool.ops.reset_interrupted_outbox_claims` (post-013-only;
    clears complete claim tuples, preserves `next_attempt_at`, atomically read-back-asserts zero) and
    verify zero claim tuples.
R4. Run `.venv/bin/alembic -c alembic.ini downgrade 012` (the revision is a REQUIRED positional
    argument — a bare `alembic downgrade` exits with a usage error mid-outage). Its
    `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE` + preflight
    `SELECT count(*) FROM outbox WHERE status='superseded'` decide the branch below.
R5. ROLLBACK OUTCOME A — downgrade REFUSED (a `superseded` row exists): the pre-7b image is unsafe (it
    cannot interpret or prune `superseded`), so KEEP or redeploy the reviewed 013-COMPATIBLE image
    digest — PROHIBIT the pre-7b image. Verify `/readyz`, start + attest its fenced workers, then
    re-enable retention, autoscaling/restarts, and submissions and remove the composer edge block —
    OR remain in a DELIBERATELY DECLARED maintenance incident while the forward fix is applied. Do
    not end stopped.
R6. ROLLBACK OUTCOME B — downgrade SUCCEEDED: deploy the recorded prior-image digest; start API, probe
    `/readyz`, then start + attest its workers; attest image digest + running processes; then re-enable
    retention, autoscaling/restarts, and submissions and remove the composer edge block. Redeploying
    the pre-7b image BEFORE 013 is applied is also safe.
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
  A restore is an **executable identity contract** (RUNBOOK step 0.6): re-insert the EXACT original
  `outbox.id` AND the original lifecycle fields, with the recorded `(decision_id, run_id, case_id,
  original_outbox_id, body_digest, original_status, original_delivered_at)` evidence, and pass a
  **positive** acceptance predicate that must return exactly one row — a default-id INSERT is
  prohibited (it silently reverses the legacy order), `now()` for `delivered_at` is prohibited (it
  falsifies the record), and a "zero mismatches = accepted" formulation is prohibited (an absent row
  would read as accepted). If the original id is unavailable, remain blocked. No sequence is written
  on this path: the precondition is a read-only check that the next id the sequence would allocate is
  already past the restored id, and a genuine sequence repair is a separate drained `ALTER SEQUENCE`.
- **🔵 RETENTION DEVIATION (governed, requires an owner) — retention no longer prunes
  `decision_callback` outbox rows.** The outbox delete is scoped to `kind='poc_email'`; every
  decision callback is kept indefinitely because the row is the durable ordering authority
  (`id` = order, `status` = local delivery outcome, `payload_json` = the body that was sent) that
  7b-activation reconciles the platform against, and because an exact restore would otherwise be
  re-pruned on the next retention run.
  - It adds **no new data category** — the body projects the `decisions`/`checks` record, which is
    already deliberately never pruned. It IS, however, an **additional durable representation**, and
    it carries `checks[].source`, which is reviewer-derived and reachable as `reviewer:<reviewer_id>`
    (`validators/website.py:13-20`). "Retains nothing new" is too strong and must not be used to
    skip governance.
  - **Consequences that need an owner named at sign-off:** backup scope and size, erasure — an
    erasure request must reach the callback snapshot as well as the decision record — privacy
    review, and the compliance decision itself. `KYC_RETENTION_DAYS` no longer bounds outbox growth.
  - **Capacity is part of the contract, not an afterthought:** both claim indexes are **partial to
    `status='pending'`** so the unbounded terminal tail never enters the claim path or its plan, and
    `/v1/metrics` reports live statuses exactly plus terminal history as one bounded count rather
    than grouping the whole table on every request.
  - The POC token — the genuinely sensitive outbox body — is still destroyed twice over: redacted at
    delivery (`publisher.py:176-182`) and pruned on schedule.
```

- [ ] **Step 7: Add the docs-contract test + the exact-rollback-command acceptance test** — create `tests/unit/test_docs_cutover_parity.py`:

```python
"""The PR 7b-core cutover/rollback procedure must be byte-identical in RUNBOOK.md and
DEPLOYMENT.md (only the section-header line may differ), including wrapped continuation lines."""

from pathlib import Path

from kyc_tool.config import REPO_ROOT

_HEADING = "PR 7b-core cutover"


def _body(path: Path) -> str:
    """Section body from the '## ... PR 7b-core cutover ...' heading (EXCLUSIVE of the heading
    line) to the next top-level '## ' — every wrapped continuation line included."""
    lines = path.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## ") and _HEADING in ln)
    out = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        out.append(ln)
    return "\n".join(out).strip("\n")


def test_runbook_and_deployment_cutover_bodies_identical():
    rb = _body(REPO_ROOT / "docs" / "RUNBOOK.md")
    dp = _body(REPO_ROOT / "docs" / "DEPLOYMENT.md")
    assert rb == dp  # FULL bodies identical (mutating any continuation line in either fails this)
    for token in (
        "restore from authoritative backup", "BLOCKED_NO_AUTHORITATIVE_MAPPING",
        "zero at the orchestrator", "reset_interrupted_outbox_claims",
        ".venv/bin/alembic -c alembic.ini downgrade 012", "R6.",
        # both rollback branches present AND both end in a full resume (no ending-stopped, no
        # frozen retention) — the F1 completeness the earlier first-line-only diff missed:
        "ROLLBACK OUTCOME A", "ROLLBACK OUTCOME B", "PROHIBIT the pre-7b image",
        "re-enable retention, autoscaling/restarts, and submissions",
        "remove the composer edge block",
        # re-audit F1 — the restore acceptance contract is executable, not advisory:
        "RESTORE ACCEPTANCE CONTRACT", "original_outbox_id",
        "default-id INSERT is prohibited", "ACCEPTANCE PREDICATE",
        "every schema-012 `outbox` column", "o.attempts = :original_attempts",
        "o.created_at IS NOT DISTINCT FROM :original_created_at",
        "pg_get_serial_sequence('outbox','id')",
        # re-review 0ca264b P1/P2 — the predicate is POSITIVE and the sequence is READ-ONLY:
        "MUST return", "EXACTLY ONE row", "ZERO rows = still blocked",
        "SEQUENCE PRECONDITION", "`setval(...)` is prohibited on this path",
        "SEPARATE DRAINED action", "ALTER SEQUENCE outbox_id_seq RESTART WITH :next_id",
        "COALESCE(max(id), 0) + 1", "\\gset", "allocation_ok",
        "substituting `now()` for `delivered_at` is prohibited",
    ):
        assert token in rb  # safety-critical details survive, not just the numbered leaders
    assert "setval(pg_get_serial_sequence" not in rb  # the live sequence write must stay deleted
    assert "RESTART WITH <" not in rb  # no pseudocode placeholder survives into the runbook
    assert rb.count("edge-block the composer") == 2  # forward + rollback both establish the fence
    assert rb.count("remove the composer edge block") == 3  # forward + both rollback outcomes clear it
```

Create `tests/integration/test_rollback_command.py`:

```python
"""The EXACT documented rollback command must be valid against a real 013 DB.

`alembic.ini` ships an empty `sqlalchemy.url`, so env.py (`_database_url`) falls through to
`KYC_DATABASE_URL`; the command below runs from REPO_ROOT with that env pointing at the dedicated DB."""

import os
import subprocess

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from kyc_tool.config import REPO_ROOT
from tests.integration.test_migrations import _fresh_db

pytestmark = pytest.mark.postgres

# EXACTLY the argv the docs prescribe: `.venv/bin/alembic -c alembic.ini downgrade 012` (run from REPO_ROOT).
_ALEMBIC = str(REPO_ROOT / ".venv" / "bin" / "alembic")


def test_documented_rollback_command_downgrades_013(pg):
    url = _fresh_db(pg, "kyc_rollback_cmd")
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "013")  # bring the dedicated DB up programmatically

    env = {**os.environ, "KYC_DATABASE_URL": url}
    ok = subprocess.run([_ALEMBIC, "-c", "alembic.ini", "downgrade", "012"],
                        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "012"
    eng.dispose()

    # the SAME argv without the positional revision is the invalid form the docs must never use
    bad = subprocess.run([_ALEMBIC, "-c", "alembic.ini", "downgrade"],
                         cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode != 0
```

- [ ] **Step 8: Verify + full gate + commit**

```bash
# ROADMAP: 7b-core shipped/013, 7b-activation still pending/014, no contradictory allocation
rg -n "PR 7b-core \| 8 \| shipped \| 013" .agents/ROADMAP.md
rg -n "PR 7b-activation \| 8 \| pending \| 014" .agents/ROADMAP.md
.venv/bin/pytest tests/unit/test_docs_cutover_parity.py tests/unit/test_migration_lineage.py \
                 tests/integration/test_rollback_command.py -v
./manage.sh test          # whole 013 suite + lineage + docs parity
.venv/bin/ruff check .
.venv/bin/lint-imports
git add docs/OVERVIEW.md docs/RUNBOOK.md docs/DEPLOYMENT.md AUDIT_FINDINGS.md \
        tests/unit/test_docs_cutover_parity.py tests/integration/test_rollback_command.py
git commit -m "docs(7b-core): stream-separation overview, drained cutover/rollback, audit findings

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
5. **Diagnostic (Tasks 2+7)** now share ONE parity matrix (`migration_contracts.v013_backfill.run_parity`) covering all 11 listed states; the migration refuses BEFORE any DDL and the CLI refuses via the real `python -m` subprocess (`KYC_DATABASE_URL`, exact exit/stdout/`BLOCKED_NO_AUTHORITATIVE_MAPPING`/no-writes); the retention race runs the REAL `prune` in a thread with a barrier after its outbox DELETE and asserts the CLI subprocess stays blocked until commit; removing the SHARE lock fails it. Healthy rows carry a real `delivered_at`.
6. **Command ordering** — every src-touching task (1, 2, 3, 4, 5, 7, 8) now runs targeted tests → RED drift-guard step → re-pin GREEN → `./manage.sh test` + ruff + import-linter, in that order; the deliberately-red guard is labeled a RED step, never a PASS/gate.
7. **Guard/A6/residual (Task 5)** now creates TWO real callbacks, delivers the higher one over the mock HTTP first (≥1 request) and asserts the lower gets zero HTTP + `superseded` + run COMPLETE + `published_at` NULL + an `audit_log` row carrying BOTH sequences (`_record_superseded` now `audit()`s them); adds a real seq 1→2→3 test, a real send-before-stamp revert (HTTP #1 then #2), and an `outbox_alerting` metrics assertion.
8. **Reset CLI (Task 8)** now probes `information_schema` with `table_schema=current_schema()`, rolls back BEFORE commit on a surviving tuple (atomic), and is exercised via `python -m` subprocess on 012 (refuses, timestamps unchanged) and 013 (clears only claimed, preserves `next_attempt_at`) plus a barrier race proving atomic rollback.
9. **Migration matrix (Tasks 1+4)** now seeds legacy pending/delivered(timestamped)/dead + both kinds + manual rows, adds the full INSERT+UPDATE identity cross-product (null/zero/negative sequence, wrong case/run, stream swaps, email-with-run, dup callback, run-id uniqueness), and mutates each CHECK/FK/unique/partial-index/`SET NOT NULL` independently against a NAMED test; authority cases assert exact `RuntimeError`/`IntegrityError`/`OperationalError`+`55P03`.
10. **Finish-time (Tasks 2, 7, 9 — this plan has nine tasks)** — unused example-code imports removed; Task 9 is copy-ready (exact OVERVIEW subsection + anchor, one canonical numbered cutover/rollback block pasted verbatim into RUNBOOK.md and DEPLOYMENT.md with a full-body parity test + `rg` sentinel/lineage checks, honest blocking `TODO(integration)` for the orchestrator attestation).

**Adaptations to the real fixtures (round 1) (where Codex's prescription needed adjustment):** (a) [SUPERSEDED by round-2 F5 — the send-before-stamp revert now uses the reachable lifecycle: seq 1 as an older `dead` callback, a fault injected in `_record_delivered` before its txn, and the REAL UI requeue]; (b) the TOCTOU and retention-race barriers use a global `after_cursor_execute`/`before_cursor_execute` listener on `sqlalchemy.engine.Engine` (removed in `finally`) since the project's `alembic/env.py` builds its own connection and cannot be handed an injected one; (c) migration-refusal assertions use `pytest.raises(RuntimeError)` on the basis that `alembic.command` propagates the migration's `RuntimeError` unwrapped — if a given environment wraps it, widen to the wrapper in-cycle. **Not executed:** per the coordinator, the named `.venv/bin/pytest`/subprocess selectors are build-cycle steps, not run here (no local Postgres).

## Codex plan-review round 2 — 8 findings closed (all real; decomposition + REVIEW-CLEAN boundary preserved)

1. **(P1) One atomic `013` commit.** Tasks 1-6 are now **worktree checkpoints** (full gates green, `git status`, NO commit); the parent makes the single schema+runtime `013` commit at the end of Task 6, with a finish gate asserting `git log … -- 013_…py` shows exactly one commit and a dedicated `012` DB upgrades once to the complete `013`. Tasks 7-9 commit normally. The build-order section, Global Constraints close-out, and every task commit step were rewritten to match.
2. **(P1) NULL-hole CHECKs.** Both identity CHECKs are now NULL-explicit OR-of-shapes (`… decision_sequence IS NOT NULL AND decision_sequence > 0 …`); added named `auto_null_sequence` / `callback_null_sequence` INSERT **and** UPDATE negatives, and a mutation that removes only `AND decision_sequence IS NOT NULL` from each CHECK → the matching named test fails.
3. **(P1) Deterministic concurrency.** The `_load` wrapper is now thread-identity-keyed with explicit `a_has_case_lock` / `b_entered_load` / `b_returned_from_load` events; with the real lock B must block INSIDE `_load` (`assert not b_returned_from_load.wait(2)`), both threads must terminate, and removing `with_for_update` fails that assertion deterministically regardless of commit order.
4. **(P2) Task 7 real-CLI matrix.** Deleted the guaranteed-fail `inspect.getsource` assertion (replaced with a behavioral boundary: DB stays on `012`, no `013` columns, count unchanged); the 12-state matrix now invokes the real `_run_cli` subprocess (nonzero + name/sentinel) beside the real 013 refusal; the retention race is parametrized `[commit, rollback]` (rollback injects a fault so `uow()` rolls back → CLI exit 0; commit → nonzero + ids).
5. **(P2) Reachable send-before-stamp.** Seeds seq 1 as an older `dead` callback, sends seq 2 for real then raises an injected fault in `_record_delivered` before its txn (seq 2 stays pending/unstamped), requeues seq 1 through the REAL UI endpoint, and asserts HTTP #2 — plus a separately-named predicate-only unit companion.
6. **(P2) Frozen migration contract.** Moved the matrix to `src/kyc_tool/migration_contracts/v013_backfill.py` (imports ONLY `sqlalchemy.text`), imported by both the migration and the CLI, with a dedicated frozen-SHA test (`V013_BACKFILL_SHA`, "never re-pin — create `v014_*` for later semantics"), distinct from the re-pinnable whole-source guard.
7. **(P2) Valid rollback command + full docs parity.** Both docs now carry `.venv/bin/alembic -c alembic.ini downgrade 012` (positional revision); a rollback-command acceptance test runs that exact form (and proves the bare form fails); a docs-contract test compares the FULL RUNBOOK/DEPLOYMENT section bodies byte-for-byte (continuation lines included) + asserts the safety tokens.
8. **(P3) Bus lifecycle + gates.** Added the parent-only Step 0 CLAIM (read `AGENT_BUS.md`/`ROADMAP`, verify no conflicting CLAIM, post+push the final file set) and the post-Task-9 RELEASE (anchor, commands/results, migration witnesses, residual-risk boundary, "M2/normative untouched", `turn: CODEX`, hold for `AUDIT-CLEAN`), and stated the human plan-approval gate precedes execution.

## Codex plan-review round 3 — 6 findings closed (all real; REVIEW-CLEAN boundary intact, no 014 pulled in)

1. **(P1) Rollback/forward resume.** The canonical RUNBOOK/DEPLOYMENT block is now a two-branch state machine: forward step 6 restores all five paused controls; rollback R4 branches to **R5 (downgrade REFUSED → keep the 013-compatible image, prohibit pre-7b, restore all five OR declare an incident)** and **R6 (downgrade SUCCEEDED → deploy prior digest, `/readyz` + workers, restore all five)**. "All five" includes removing the composer edge block in addition to retention, autoscaling, restarts, and submissions. `test_docs_cutover_parity.py` requires both `ROLLBACK OUTCOME A/B`, `PROHIBIT the pre-7b image`, the resume token, two edge-block establishments, and three edge-block removals — beside the full-body byte-identical compare.
2. **(P2) Lifecycle-gap in the shared preflight.** Added `invalid_legacy_outbox_lifecycle` to the frozen contract (the schema-012 projection of `ck_outbox_status_lifecycle`: status vocabulary + delivered_at shape); four `_PARITY_BAD_SEEDS` (`lifecycle_pending_delivered_at`/`_delivered_null_at`/`_dead_delivered_at`/`_unknown_status`) prove the real CLI **and** the real 013 upgrade refuse each before DDL, a `test_013_valid_lifecycle_rows_upgrade_clean` proves valid pending/delivered/dead pass, and a named mutation removing the check is the witness. A `_PARITY_SEED_VIOLATION` map pins the exact violation name per seed.
3. **(P2) Exact rollback command.** `test_documented_rollback_command_downgrades_013` now runs **exactly** `[str(REPO_ROOT/'.venv/bin/alembic'), '-c', 'alembic.ini', 'downgrade', '012']` with `cwd=REPO_ROOT` + `KYC_DATABASE_URL` (`alembic.ini` ships an empty `sqlalchemy.url`, so env.py falls to that var), asserts head `012`, and asserts the same argv without `012` exits nonzero — identical to the documented command.
4. **(P2) Ruff-clean copy-ready Python.** Wrapped all 39 over-110 lines (string implicit-concatenation / arg breaks / split asserts) → **0** lines >110; self-review actually ran `.venv/bin/ruff check --select E,W` on every fenced block (0 E501/whitespace/indent findings) and `compile()` on every real module block (0 SyntaxErrors); the two residual ruff flags are provable false positives on non-code insertion fragments (a docstring-text snippet, the `ARRAY, JSONB, UUID` import line used in the full `tables.py`).
5. **(P2) TOCTOU determinism.** The downgrade race test now drives B's txn via an explicit `connB.begin()`: on `55P03` it asserts SQLSTATE + rolls back; on an unexpected successful UPDATE (lock removed) it **commits** the stranded `superseded` row and `pytest.fail`s; and it asserts `not t.is_alive()` (downgrade thread terminated) before `down_err == []`, so a hung timeout cannot pass.
6. **(P3) Real frozen SHA + label.** Computed `V013_BACKFILL_SHA = bdd2342be673c2b324af02cf00644ecde70739a233e7c7cb39670123fb6d1c04` (sha256 over the exact Step-1 `v013_backfill.py` bytes: block content + one LF) and pasted the literal 64-char lowercase hex; added the byte-convention note (fix the file, never re-pin the SHA); strict placeholder scan (`<sha256`/`<...>`/`TBD`) is clean; fixed the stale "Tasks 2/7/10" label (this plan has nine tasks).

**Fixture adaptation (round 3):** the rollback-command test relies on `alembic.ini`'s empty `sqlalchemy.url` so env.py's `_database_url()` honors `KYC_DATABASE_URL`; the frozen SHA is computed over the plan's exact create-file block bytes (LF, single trailing newline) and the note tells the implementer to fix the FILE (not the SHA) on any mismatch. **Not executed:** the named `.venv/bin/pytest`/subprocess selectors remain build-cycle steps (no local Postgres); the ruff/compile self-review WAS run against every fenced block during this revision.

## Codex plan-review round 4 — 2 residual findings implemented directly

1. **(P2) Composer fence lifecycle.** Forward step 1 explicitly installed a composer edge block, but
   the rev-4 forward and both rollback completion branches restored only retention, autoscaling,
   restarts, and submissions. Rollback also lacked the composer fence before stopping the old
   processes. The final state machine now establishes the fence in both forward and rollback entry,
   removes it on forward success and both rollback-success outcomes, and calls the controls five
   rather than four. The docs parity test asserts the exact establishment/removal counts so a branch
   cannot silently leave the operator surface blocked or writable during the drain.
2. **(P2) Full Ruff contract.** Rev 4 ran only `ruff --select E,W`; the repository actually enforces
   `E,F,I,UP,B,SIM`. The advertised `test_outbox_fencing.py` create-file block still failed `UP031`
   because it built JSON with percent formatting. It now uses `json.dumps`, and every complete
   create-file block is compiled and checked under its advertised path against the full project
   selector set. The exact frozen-contract SHA remains unchanged and verified.

## Codex plan-review round 5 — complete-unit re-audit @ `eac3035`, 10 findings folded (rev 5, 2026-07-24)

1. **(P1 F1) Restore acceptance contract.** RUNBOOK/DEPLOYMENT step 0.5 gains an executable step 0.6:
   the restore must re-insert the ORIGINAL `outbox.id` plus the recorded
   `(decision_id, run_id, case_id, original_outbox_id, body_digest)` evidence; a default-id INSERT is
   prohibited; original id unavailable → remain `BLOCKED_NO_AUTHORITATIVE_MAPPING`; an acceptance
   predicate (SQL, zero mismatches) gates proceeding; after any explicit-id restore the outbox id
   sequence is advanced via `setval(...)` before writers resume. **⚠️ The zero-mismatch predicate and
   the `setval` in this paragraph were both found defective in round 6 and are SUPERSEDED — see
   "Codex plan-review round 6" below. Do not implement them.** Task 7 Step 4b adds the full schema-012
   recovery acceptance
   test (prune old id, prove the default-id restore is REJECTED by the predicate, restore the exact
   id, predicate passes, real diagnostic CLI clean, real 013 upgrade maps old/new to sequences 1/2).
   The frozen contract module `v013_backfill.py` is UNCHANGED (the contract stays historical; the
   acceptance predicate lives in the runbook + test), so `V013_BACKFILL_SHA` is unchanged. The docs
   parity test pins the new tokens. D-7bcore records the identity-restore rule.
2. **(P1 F2) Composite decisions→runs case FK.** Migration 013 (Task 4) creates the named unique
   `uq_runs_id_case_id` on `runs(id, case_id)` BEFORE the composite
   `fk_decisions_run_case FOREIGN KEY (run_id, case_id) REFERENCES runs(id, case_id)`; downgrade
   drops FK before unique (both downgrade texts agree). `run_id` nullable → MATCH SIMPLE: manual
   rows exempt, automatic rows fully bound (stated as intended in a migration comment). ORM mirror:
   `Run.__table_args__` (created — Run had none) + `ForeignKeyConstraint` in
   `DecisionRow.__table_args__`. INSERT and UPDATE negatives use two real cases/runs with
   otherwise-valid rows (a third run under c1 avoids masking by `uq_decisions_run_id`; distinct
   sequences avoid masking by the per-case unique) and assert the exact string
   `fk_decisions_run_case`; the named mutation removes ONLY this FK → both negatives fail.
3. **(P1 F3) The four affected existing files are in the unit.** `test_ui.py`,
   `test_phase4_platform.py`, `test_migrations.py`, `test_bundle_pinning_ops.py` (+
   `tests/conftest.py`) are now named in the Step-0 CLAIM, File Structure, the responsible tasks
   (Task 1 Step 8b, Task 3 Step 3b, Task 4 Step 7b), and the Task-6 staged set. The UI fixture gains
   `ordering_stream='email'`. The impossible raw `delivered→pending` rewrite becomes the REAL
   send-before-stamp fault injection (first-call-only `_record_delivered` fault, caught at the
   `process_pending` boundary since `process_once` calls it outside its try/except; lease expired by
   a raw `claim_lease_expires_at` UPDATE; fenced reclaim resends the identical dedupe body) — an
   interim lifecycle-legal rewrite exists only inside the Task 1-2 checkpoint window; the committed
   unit ships the fault-injection form. The three `manual=false` no-run migration fixtures flip to
   `manual=true`; EVERY nonblank negative asserts its intended 011 constraint name
   (`ck_epoch_engine_nonblank`, `ck_runs_engine_build_id_nonblank`,
   `ck_decisions_engine_build_id_nonblank`, `ck_checks_policy_bundle_hash_nonblank`). The three
   bundle-pinning automatic decisions go through ONE shared `tests/conftest.py`
   `seed_automatic_decision` helper (valid sequence + counter bump). Task 6 runs the four files'
   selectors FIRST, then `./manage.sh test`. (Adjacent fix while editing that block: the docs-parity
   token `restore from authoritative backup` did not literally occur in the rev-4 runbook body —
   the reworded 0.5 now contains it verbatim.)
4. **(P2 F4) Third residual: manual-current vs late automatic callback.** The Task-5 module
   docstring, A6 Resolution, D-7bcore, OVERVIEW boundary, ROADMAP row, task intro, guard comment,
   and the RELEASE bus entry all enumerate THREE residuals (send-before-stamp; cross-replica; a
   queued automatic callback delivered AFTER a later manual approval — manual rows have `run_id
   NULL`, no callback, no sequence, so the guard sees no higher locally-published automatic
   sequence). Task 5 adds the REAL ingest→worker→manual-approve→publisher test
   `test_manual_current_then_late_automatic_callback_sent_expected_pre_activation` (callback
   enqueued, NOT processed; `reviewer.manual_approve` ingested; `process_pending()` really sends the
   old callback). 013 does NOT sequence manual rows. Core spec §5 gains the same residual with its
   trigger sequence (rev 9); the activation spec's reconciliation/acceptance section is strengthened
   so an unaccepted pending/dead callback older than a manual-current source cannot replace it.
5. **(P2 F5) Downgrade-TOCTOU autobegin.** `txB = connB.begin()` now precedes ANY execute on connB,
   and the timeout is set with `SET LOCAL lock_timeout='2s'` INSIDE that transaction (SQLAlchemy 2
   autobegin otherwise owns the txn and `connB.begin()` raises `InvalidRequestError` before the lock
   is ever exercised). Rollback-on-55P03, commit-before-fail on an unexpected UPDATE success, thread
   liveness assertions, and listener cleanup are preserved. Step 5 now runs the exact test first,
   then the move/delete-the-lock mutation and requires the failure mode "the concurrent supersession
   actually COMMITS".
6. **(P2 F6) Same-stream FIFO under backoff.** New `test_same_stream_fifo_holds_under_backoff`
   (Task 3): one case, decision stream, seq1 in future backoff, seq2 due → `process_once()` idle,
   ZERO HTTP, seq2 untouched (pending, unclaimed); seq1 made due → order `[seq1, seq2]`. Named
   mutation (Step 5c): adding due/lease eligibility to the INNER `min(o2.id)` subquery of
   `_CLAIM_SQL` → the test fails (leapfrog).
7. **(P2 F7) Retry-branch fencing proof.** New `test_stale_retry_failure_cannot_touch_reclaimed_row`
   (Task 3) with `outbox_max_attempts=3`: A claims, lease expires, B reclaims (no terminalize); A's
   stale `_record_failure` leaves B's ENTIRE claim tuple + attempts + `next_attempt_at` +
   `last_error` byte-for-byte unchanged and logs `outbox_stale_claim_completion` with
   `attempted="retry"`; B's own failure increments attempts to `rowB.attempts+1` (claim snapshot),
   schedules backoff per `base*2^(attempts-1)` (window-asserted), clears only B's tuple, status
   stays pending. Named mutation (Step 5d): remove `claim_token=:token` from ONLY the nonterminal
   retry UPDATE → the test fails.
8. **(P2 F8) `superseded` is decision-only.** `_LIFECYCLE_CHECK` gains the conjunct
   `AND (status <> 'superseded' OR kind = 'decision_callback')` (same constraint name
   `ck_outbox_status_lifecycle`; the schema-012 parity projection is unaffected — `superseded`
   already fails its status vocabulary). Every fixture that seeded a superseded `poc_email`
   (Task 5 retention/UI-409/metrics via the new `_superseded_callback` valid-chain helper; Task 6
   downgrade refuse + lock tests via `_mk_auto_decision`) now builds a valid automatic
   decision+callback chain — the downgrade tests keep real superseded/pending DECISION rows, so
   both the refusal and the mutation-path commit stay reachable. Dedicated INSERT and UPDATE
   negatives (`test_013_superseded_is_decision_only_*`) use an otherwise-valid superseded shape and
   assert the constraint name; the named mutation removes ONLY the conjunct → both fail.
9. **(P3 F9) ORM FK parity.** `Outbox.case_id` gains `ForeignKey("cases.id",
   name="fk_outbox_case_id")` in the Task-1 `tables.py` block (column stays non-optional, matching
   013's SET NOT NULL). New `test_013_orm_and_live_fk_parity` asserts `fk_outbox_case_id` +
   `fk_outbox_decision_triple` via BOTH `Outbox.__table__.foreign_keys` AND the live inspector
   (plus `fk_decisions_run_case` and `uq_runs_id_case_id` for F2).
10. **(P3 F10) Required-keyword sequence.** Task 4 Step 8b — AFTER every caller passes it — changes
    `enqueue_decision_callback` to the FINAL `decision_sequence: int` (required keyword, no
    default), with a `TypeError` assertion test; the Task-3 checkpoint explicitly keeps the optional
    form as staging only, and the single atomic 013 commit ships the required form. All plan-block
    callers pass the keyword; the `test_review_completed_event.py` seam forwards `*args, **kwargs`.

**Response-order note (Codex's required rev-5 order):** (a) the CLAIM/File-Structure extension, (b)
the spec amendments (core rev 9: findings 1, 2, 4, 8; activation spec: the manual-current acceptance
strengthening), and (c) the plan block revisions for all ten findings are ALL contained in this
revision; the verification matrix (full-selector Ruff + compile on every complete create-file block,
`bash -n` on shell blocks, frozen-SHA verification, docs parity/lineage, targeted selectors) was
rerun over the revised blocks. **Not executed here:** the named `.venv/bin/pytest`/subprocess
selectors remain build-cycle steps (no local Postgres at plan time).

## Codex plan-review round 6 — complete-unit re-review @ `0ca264b`, 4 findings folded + 2 reviewer items (rev 6, 2026-07-24)

All four Codex findings were independently re-verified against the live plan/spec/code before folding
— none was taken on the review's word, none was rebutted. Two of the four (F2, F4) named an
alternative that would need a product decision; the **recommended** branch was taken in both, so no
decision is deferred into the build.

1. **(P1 F1) The pre-window `setval` could rewind the outbox id sequence.** Step 0 runs with
   API/pipeline/outbox writers LIVE (only retention is suspended; the first hard-stop is cutover
   step 2), and `setval(GREATEST((SELECT max(id) FROM outbox), (SELECT last_value FROM outbox_id_seq)))`
   is a read-modify-write on a **non-transactional** object: `max(id)` sees only the txn's MVCC
   snapshot, `last_value` is read at a different instant from the write, and `LOCK TABLE outbox IN
   SHARE MODE` does not fence a sequence. A concurrent `nextval` is invisible to both → the sequence
   can be set below an already-allocated id → duplicate primary key. **Folded as deletion, not
   hardening:** an exact restore of a retention-pruned *historical* id fills a gap **below** the
   already-advanced sequence, so the write was never needed. Runbook 0.6(d) and the Task-7 test now
   carry a **read-only** precondition (`last_value + (is_called ? 1 : 0) > original_outbox_id`,
   monotonic, therefore durable once observed) that aborts **before** the outage; a genuine repair is
   a **separate drained action** using `ALTER SEQUENCE … RESTART WITH` (which does exclude concurrent
   `nextval`) with read-back. `_SEQ_HIGH_WATER_SQL` is asserted in the test; the docs-parity test
   pins the new tokens **and** asserts `setval(pg_get_serial_sequence` no longer appears.
2. **(P2 F1) The restore acceptance predicate was fail-open.** "Select the mismatching rows, expect
   zero" made an **absent** row — and one restored under the wrong `run_id` — indistinguishable from
   an exact match, and never used the recorded `decision_id` at all. **Folded:** one **positive**
   assertion returning **exactly one** row, joining `outbox` → `decisions` on the full recorded
   identity (`o.id`, `kind`, `o.case_id`, `o.run_id`, `d.id = decision_id`, `d.case_id = o.case_id`,
   `d.run_id = o.run_id`), the body digest, and the **original** lifecycle fields. `md5` → SHA-256;
   `delivered_at = now()` → the recorded timestamp (the plan's `now()` was itself masking finding 4).
   The Task-7 test adds an absent-row rejection, a default-id rejection, and a **per-component**
   mutation loop proving each identity element is separately load-bearing.
3. **(P1 F2, activation-owned) A bare `s>h(c)` receiver would let a late automatic callback silently
   override a manual approval.** Verified real at `events/ingest.py:242-267`: `_handle_manual_approve`
   writes a decision with `run_id=None`, no `decision_sequence` and no outbox enqueue, so it can never
   advance `h(c)`. Folded into the **activation** spec (rev 3): while a case's effective source is
   `manual:<event>`, sequenced automatic callbacks are acknowledged, recorded for dedupe, terminalized
   locally, and may advance `h(c)` — but **must not become the effective source** without an explicit
   authenticated platform-owned release/override. Manual-wins is stated explicitly; the inverse is a
   product decision and is never inferred from `s>h(c)`. 7b-core is unchanged by this.
4. **(P1 F4) Retention deleted the very rows 014's manifest is built from.** `retention.py:29-35`
   deletes delivered outbox rows past the window, so an exact restore (which reinstates the original
   7-year-old `delivered_at`) is re-deleted on the next retention run — the plan's `delivered_at=now()`
   hid this, and 014 then had no durable source for per-callback `(body_digest, local_status)`.
   **Folded root-cause-first, and deliberately NOT by Codex's suggested mechanism** (a second store):
   retention's outbox delete is **narrowed to `kind='poc_email'`**, making the existing `outbox` row
   itself the durable authority. One added predicate; no new table, no new column, no dual write,
   therefore no drift class to guard — `outbox.id` IS the order and `outbox.status` IS `local_status`.
   It retains nothing new (the callback body is a projection of the never-pruned `decisions`/`checks`
   record) and the POC token stays destroyed twice over. A stored `body_sha256` column was designed
   and then **rejected** for the same reason a second table was: it duplicates an existing fact and
   would force one canonical encoder to be reproduced in both Python and SQL. The digest is instead a
   single SQL expression, `encode(sha256(convert_to(payload_json::text,'UTF8')),'hex')`, shared
   verbatim by the restore predicate and 014's manifest. The `superseded` prune added in rev 5 is
   dropped with it: `superseded` is decision-callback-only, so it would delete nothing under the
   narrowed rule, and one rule ("decision callbacks are never pruned") is the maintainable form.

**Reviewer items folded alongside (Task-1 task review, verdict Approved):**

5. **Name-pin the 013 lifecycle negatives.** `_OUTBOX_LIFECYCLE_BAD` becomes a
   `case → (expected_constraint_or_message, sql)` map and the test asserts the expected string —
   the same discipline the plan already applies to `_NONBLANK_SURFACES`, whose new comment states the
   principle ("so 013's new shape CHECK can never produce a false green") but was not carried to the
   twelve lifecycle negatives. It bites concretely at Task 4, whose kind/stream identity CHECK rejects
   every `('poc_email','decision')` shape and would keep these green with the lifecycle CHECK deleted.
   Adjudicated **FIX** rather than a plan conflict: the plan states the principle itself and simply
   failed to apply it; the constraint names were already in trailing comments on every entry.
6. **The ROADMAP §C flip moves from Task 9 to Task 1 Step 3b (committed in Task 6).**
   `test_migration_lineage.py` asserts `authored_reserved == shipped` reading BOTH `.agents/ROADMAP.md`
   and `alembic/versions/` off disk, so creating `013_outbox_stream_separation.py` in Task 1 breaks it
   immediately — it is not deferrable. Leaving the flip in Task 9 would also make Task 6's atomic
   commit red in CI, which checks out the commit rather than the worktree; `.agents/ROADMAP.md` is
   therefore added to Task 6's `git add` set and removed from Task 9's.

**Not executed in this revision:** the named `.venv/bin/pytest` selectors remain build-cycle steps.
The ruff/compile self-review WAS run against every fenced create-file block, under its advertised
path and the full `E,F,I,UP,B,SIM` selector set. The frozen `V013_BACKFILL_SHA` is **unchanged** —
none of these edits touch `v013_backfill.py` (the acceptance predicate and the retention narrowing
both live outside the frozen contract).
