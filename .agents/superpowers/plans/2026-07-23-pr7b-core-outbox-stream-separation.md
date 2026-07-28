# PR 7b-core — Outbox stream separation + local decision ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The parent session is the SOLE committer; subagents implement + hand diffs back.

> **Plan revision: rev 6 (2026-07-24)** — folds Codex's rev-5 complete-unit re-review @ `0ca264b`
> (4 findings) plus 2 task-review items; see "Codex plan-review round 6" at the bottom. Rev 5
> folded all 10 findings of the Codex complete-unit re-audit @ `eac3035` (`AGENT_BUS.md` PLAN-REVIEW 2026-07-24): restore acceptance contract (F1), composite decisions→runs case FK (F2), the four affected EXISTING test files claimed + adapted (F3), the third residual (manual-current vs late automatic callback) + its real test (F4), downgrade-TOCTOU autobegin fix (F5), same-stream FIFO-under-backoff proof (F6), retry-branch fencing proof (F7), `superseded` is decision-only (F8), ORM FK parity for `fk_outbox_case_id` (F9), required-keyword `decision_sequence` final signature (F10). See "Codex plan-review round 5" at the bottom.

**Goal:** Separate the outbox claim into per-`(case_id, ordering_stream)` FIFO streams, add an internal per-case `decision_sequence`, fence every claim with a `claim_token`, add a best-effort local `superseded` guard, and lock all of it down with exhaustive relational integrity — shipped as migration `013` (+ witness repair `014`) with a drained cutover and a downgrade that is reversible only before the first supersession AND before any wire witness exists.

**Architecture:** Migration `013` (`down_revision='012'`) adds `outbox.ordering_stream`/`case_id NOT NULL`, a fenced-claim column set (`claim_token`/`claim_lease_expires_at`/`claimed_by`), `decisions.decision_sequence` + `cases.last_decision_sequence`, a triple FK binding each callback to its automatic decision, and exhaustive per-status lifecycle CHECKs. The publisher claims the min-id pending row of one `(case, stream)` under a fresh `claim_token`; every terminal (`_record_delivered`/`_record_failure`/`_record_superseded`) is a single fenced `UPDATE … WHERE id AND status='pending' AND claim_token=:token RETURNING id` whose winner performs all dependent writes in-transaction and whose stale loser emits `outbox_stale_claim_completion` and does nothing. The decide transaction allocates `decision_sequence` from the locked `cases.last_decision_sequence` counter; a local guard suppresses an older requeued callback only when a higher-sequence decision already carries a locally-stamped `published_at`.

**Tech Stack:** Python 3.11, FastAPI (sync + threadpool), SQLAlchemy 2 (sync psycopg), Postgres, Alembic, import-linter, pytest against ephemeral Postgres (`tests/pg.py`).

**Spec:** `.agents/superpowers/specs/2026-07-22-pr7b-core-outbox-stream-separation-design.md` (rev 9 — rev 8 REVIEW-CLEAN + the rev-5 re-audit amendments for findings 1, 2, 4, 8). It is the authoritative contract — read the cited §sections for rationale; every CHECK, SQL, seam, and mutation witness below is drawn from it.

## Global Constraints

- **Python 3.11, FastAPI, SQLAlchemy 2 (sync psycopg), Postgres, Alembic.** Tests run against real ephemeral Postgres — the session-scoped `pg` fixture (`tests/pg.py`) self-provisions a cluster, so `.venv/bin/pytest <selector>` runs real-Postgres tests with no extra setup.
- **Test commands.** `./manage.sh test` runs the WHOLE suite (it ignores path args — `.substrate/lib.sh:214` `sub_test` → `test_python` runs bare `pytest`). Targeted red→green in each step uses **`.venv/bin/pytest <selector> -v`**. The final gate per task runs `./manage.sh test`.
- **Lint gate.** `.venv/bin/ruff check .` (rules `E,F,I,UP,B,SIM`; **no `;`/E702 multi-statement lines**) **and** `.venv/bin/lint-imports` (import-linter, **2 contracts kept / 0 broken**). New code lives in `db`/`outbox`/`orchestration`/`events`/`workers`/`api`/`ui`/`ops` — never add an import into `domain`/`validators`/`policy`/`adapters`, so both contracts stay green. **Never run `./manage.sh fmt`.**
- **CANONICAL CLOSE-OUT ORDER for any `src/kyc_tool/**`-touching task (findings 6):** the whole-source drift guard (`test_engine_source_hash_pinned`) fails on ANY `src/` edit, so `./manage.sh test` returns nonzero until the hash is re-pinned. Every such task's close-out therefore runs **in this exact order — never `./manage.sh test` before the re-pin**: (1) run the task's targeted `.venv/bin/pytest` selectors green; (2) run the drift-guard test once to observe it RED (the deliberately-red TDD step — label it RED, never a gate); (3) compute+paste the new `EXPECTED_ENGINE_SOURCE_HASH` and re-run the drift-guard test GREEN; (4) `./manage.sh test` → exit 0; (5) `.venv/bin/ruff check .` → exit 0; (6) `.venv/bin/lint-imports` → exit 0; (7) **checkpoint or commit — see below**. **Tasks 1-6 are worktree CHECKPOINTS that end at step (6) with `git status` (NO commit); the single atomic `013`+runtime commit is made at the end of Task 6 (F1).** Tasks 7-9 (and Task 6's final step) commit normally. Tasks that touch only `alembic/`, docs, or `.agents/` skip steps 2-3 and run `./manage.sh test` directly.
- **Exact exception types in tests (finding 9).** Never assert a bare `pytest.raises(Exception)` on an authoritative refusal. Use: `sqlalchemy.exc.IntegrityError` for CHECK / FK / unique / NOT-NULL violations; `sqlalchemy.exc.OperationalError` with `exc.value.orig.sqlstate == "55P03"` for a `lock_timeout` (psycopg3 exposes `.sqlstate` on `.orig`); `RuntimeError` for a migration/CLI fail-closed refusal raised in Python (`alembic.command.upgrade`/`downgrade` propagate the migration's `RuntimeError` unwrapped) — always paired with an assertion on the message substring. Match a `lock_timeout` on a real statement with `conn.execute(text("SET lock_timeout='2s'"))` first.
- **Delivery-layer only — NO scoring/gate/decision semantic change.** The callback HTTP body stays **byte-identical** to pre-7b: `decision_sequence` is written to the `decisions`/`outbox` **columns only**, never added to `payload_json` or the wire (that is 7b-activation / `020`).
- **Engine drift guard.** Every task that edits any file under `src/kyc_tool/**` MUST re-pin `EXPECTED_ENGINE_SOURCE_HASH` in `tests/policy_driven/test_engine_build_id_guard.py` **in the same commit** (see the re-pin one-liner in "Test infrastructure" below). **Do NOT bump `ENGINE_BUILD_ID`** — this is not a scoring change. The src-touching tasks are **1, 2 (the frozen `migration_contracts/v013_backfill.py`), 3, 4, 5, 7, 8** — each re-pins. Tasks that touch only `alembic/`, docs, or `.agents/` (parts of 6, 9) do NOT re-pin (the guard closure is `src/kyc_tool/**/*.py` only).
- **Do NOT edit `KYC_Tool_Build_Package/`** (normative spec, immutable) or anything touching **M2** / `KYC_ENFORCE_POSITIVE_DECISIONS` / `enforce_positive_decisions`.
- **ROADMAP lineage.** `.agents/ROADMAP.md §C` reserves PR 7b-core = `013` (shipped), the witness repair = `014` (shipped), the witness-authority hardening = `015` (shipped), the admission provenance = `016` (shipped, re-audit `0c46443`), the authority boundary = `017` (shipped, re-audit `15d875d`), the transition authority = `018` (shipped, re-audit `cbb783b`), and 7b-activation = `020` (five forced renumbers: `013` was amended in place after commit → repair `014`; `014`'s authority gaps were then repaired in `015` because a published revision is never edited — the same rule both times). Do **not** renumber further, which `tests/unit/test_migration_lineage.py` forces as soon as `alembic/versions/013_*.py` exists on disk: make the flip in **Task 1 Step 3b** and commit it in **Task 6's atomic `013` commit** (both files are read off disk, and CI checks out the commit, not the worktree). `tests/unit/test_migration_lineage.py` must stay green from Task 1's checkpoint onward.
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

## Tasks 1-6 — BUILT. The code is the artifact; this section is its index.

Tasks 1-6 are implemented, green, and committed as the single atomic `013` commit required by
F1 (see "Build order" above). **The source of truth for what they do is the code itself**, listed
below. This section deliberately does NOT reproduce that code.

**Why it no longer does.** Earlier revisions of this plan carried a complete transcription of every
file Tasks 1-6 produce — roughly 3,000 lines duplicating source that also exists in `src/` and
`tests/`. That is two sources of truth for one system, and it drifted exactly as such a structure
must: a design change (recording the wire digest at send time) landed in the specs and in the code
but not in this plan's copy of the code, and a downgrade correction landed in the migration but not
in this plan's copy of the downgrade. Both were then correctly reported as review findings against
the plan while the code was already right. Deleting the duplicate removes the drift class rather
than re-syncing it by hand a fourth time. Tasks 7-9 keep their code blocks because they are not
built yet — for them the plan is still the instruction, not a second copy.

**How to review Tasks 1-6:** read the diff of the atomic `013` commit, and the files below. Not
this document.

### What each task established, and where it lives

| Task | Invariant it establishes | Where it lives now |
|---|---|---|
| 1 | A row's ordering stream is a **function of its kind**, enforced by the database (`ck_outbox_kind_stream_identity`), not by convention. `outbox.case_id` becomes `NOT NULL` with a real FK. An exhaustive per-status lifecycle CHECK makes every illegal (status, timestamp, claim-tuple) combination unrepresentable. Claim indexes become partial to `status='pending'` so the claim scan never walks terminal history. | `alembic/versions/013_outbox_stream_separation.py` (upgrade part A); `src/kyc_tool/db/tables.py`; `src/kyc_tool/outbox/publisher.py` (`enqueue_*`) |
| 2 | The legacy `decision_sequence` backfill runs from a **frozen, importable contract** rather than SQL buried in a migration, and refuses before any DDL if the existing data violates parity. Order authority is `outbox.id` — enqueue order — never `decided_at`. | `src/kyc_tool/migration_contracts/v013_backfill.py`; migration 013 preflight |
| 3 | Claims are **stream-scoped and fenced**: every terminal and every retry UPDATE carries `AND claim_token=:token`, so a claimant whose lease expired can write nothing at all — not the terminal, not the backoff, not the claim tuple. FIFO within a stream holds even when the head of the stream is backed off. | `src/kyc_tool/outbox/publisher.py` (`_CLAIM_SQL`, `_record_delivered`, `_record_failure`, `_record_superseded`); `tests/integration/test_outbox_fencing.py` |
| 4 | Decision sequences are allocated under a **per-case row lock**, so concurrent decides serialize rather than colliding, and the decision→outbox identity triple is bound by FK and unique constraints that make a mismatched (case, run, sequence) unrepresentable. | `src/kyc_tool/orchestration/pipeline.py`; migration 013 decision-identity constraints; `tests/integration/test_decision_sequence.py` |
| 5 | An older decision callback is suppressed **only** when a higher-sequence decision for the case already has a locally-stamped `published_at` — a best-effort local guard, explicitly NOT an authority. Retention, metrics and the UI-409 path are wired to the new lifecycle. | `src/kyc_tool/outbox/publisher.py` (`process_once`); `src/kyc_tool/workers/retention.py`; `src/kyc_tool/api/routes_metrics.py`; `tests/integration/test_outbox_supersession.py` |
| 6 | `downgrade()` takes `ACCESS EXCLUSIVE` on `outbox` **before** its `superseded` preflight, closing the TOCTOU window in which a concurrent publisher could commit a superseded row between the check and the DDL and strand a terminal a pre-7b image cannot interpret. The downgrade restores schema 012 exactly, including 006's full-form `ix_outbox_claim`. | `alembic/versions/013_outbox_stream_separation.py` (`downgrade`) |

### Files Tasks 1-6 produced or changed

Created: `alembic/versions/013_outbox_stream_separation.py`,
`src/kyc_tool/migration_contracts/{__init__,v013_backfill}.py`,
`tests/integration/{test_decision_sequence,test_outbox_fencing,test_outbox_supersession}.py`,
`tests/unit/test_migration_contract_v013.py`.

Modified: `src/kyc_tool/{db/tables,outbox/publisher,orchestration/pipeline,workers/retention,api/routes_metrics}.py`,
`tests/conftest.py`, `tests/integration/{test_migrations,test_bundle_pinning_ops,test_phase4_platform,test_ui}.py`,
`tests/policy_driven/test_engine_build_id_guard.py`, `.agents/ROADMAP.md`, `AUDIT_FINDINGS.md`,
`docs/RUNBOOK.md`.

### Mutation proofs — the durable part, preserved verbatim

These are the reason to trust the tests. Each names a specific line to break and the specific test
that must go red when it is broken; a test that stays green under its mutation is not testing what
it claims. They are recorded here because they exist nowhere in the code — they are procedures, not
assertions. Each is manual, verify-then-restore, and must never be committed in its mutated state.

**Task 1 — the lifecycle CHECK.** Temporarily delete
`op.create_check_constraint("ck_outbox_status_lifecycle", ...)` from the migration, rerun
`.venv/bin/pytest tests/integration/test_migrations.py -k "013_outbox_lifecycle or 013_superseded_is_decision_only" -v`;
confirm the `pending_delivered_at` / `dead_with_claim` / `superseded_null_resolved` /
`delivered_with_claim` cases AND both decision-only negatives now FAIL (no `IntegrityError`).
Restore. Do the same for `ck_outbox_kind_vocab` (→ `unknown_kind` fails) and the `SET NOT NULL` on
`ordering_stream` (→ the `is_nullable` metadata assertion fails). **Named witness:** remove ONLY the
final `AND (status <> 'superseded' OR kind = 'decision_callback')` conjunct from `_LIFECYCLE_CHECK`,
leaving every other branch; rerun
`.venv/bin/pytest tests/integration/test_migrations.py -k 013_superseded_is_decision_only -v` →
BOTH negatives must FAIL (the shape-valid superseded email now passes) while every other lifecycle
negative stays green. Restore.

**Task 2 — the pre-DDL parity refusal.** Delete the `invalid_legacy_outbox_lifecycle` entry from
`PARITY_CHECKS` in `v013_backfill.py`; rerun
`.venv/bin/pytest tests/integration/test_migrations.py -k "013_upgrade_refuses_parity_violation_before_ddl and lifecycle" -v`
→ those four params must FAIL: the preflight no longer refuses, so the violation surfaces later at
CHECK creation — i.e. *after* the outage would already have begun. Restore. Note this changes
`v013_backfill.py` bytes, so the frozen SHA is computed only once this entry is final.

**Task 2 — the order authority.** (a) Change the assignment `ORDER BY o.id` →
`ORDER BY d.decided_at, o.id`; rerun
`test_013_backfill_orders_by_outbox_id_and_delivers_without_false_supersession` → it must FAIL at
`seqs == {"dA":1,"dB":2}`. Restore. (b) Delete the `run_parity` block; rerun
`test_013_upgrade_refuses_parity_violation_before_ddl` → every parametrization must FAIL (no
refusal). Restore.

**Task 3 — the four fencing witnesses.** (a) In `_record_delivered` delete the
`if applied is None: … return` branch; rerun
`test_stale_poc_loser_touches_nothing_before_reclaimer_sends` → must FAIL. Restore. (b) Change every
terminal's `if applied is None: log.warning(...); return` to `raise RuntimeError("stale")`; rerun
`test_stale_decision_loser_cannot_stamp_run_or_published_at` → must FAIL (the raise escapes instead
of a non-raising audited no-op). Restore. (c) In `_CLAIM_SQL`, add `AND o2.next_attempt_at <= now()`
to the INNER `SELECT min(o2.id)` head-of-stream subquery ONLY, leaving outer eligibility untouched;
rerun `test_same_stream_fifo_holds_under_backoff` → must FAIL (seq2 leapfrogs its backed-off elder
and HTTP occurs). Restore. (d) In `_record_failure`, remove `AND claim_token=:token` from the
NONTERMINAL retry `UPDATE` only, leaving the dead-branch fence intact; rerun
`test_stale_retry_failure_cannot_touch_reclaimed_row` → must FAIL at the byte-for-byte assertion.
Restore.

**Task 4 — the per-case lock.** In `pipeline._load` remove `with_for_update=True` from
`session.get(Case, run.case_id, with_for_update=True)` (or substitute a `max(decision_sequence)+1`
allocation for the locked-counter increment); rerun `test_concurrent_decides_serialize_via_case_lock`
→ it must FAIL **deterministically** at `assert not b_returned_from_load.wait(timeout=2)`: with no
lock, B returns from `_load` while A is held regardless of commit order, and the run also trips
`uq_decisions_case_decision_sequence`. Restore.

**Task 6 — the downgrade fence.** First run the selector green:
`.venv/bin/pytest tests/integration/test_migrations.py::test_013_downgrade_lock_prevents_concurrent_supersede -v`.
Then move `op.execute("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE")` to AFTER the `count(*)`
preflight (or delete it) and rerun the same selector → it must FAIL **because the concurrent
supersession actually COMMITS**: the preflight now holds only `ACCESS SHARE`, B's
`pending→superseded` UPDATE succeeds with no `55P03`, the test commits the stranded row, and the
explicit `pytest.fail(...)` fires. Restore.

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

<!-- complete-file: tests/integration/test_verify_pr7b_core_backfill.py -->
```python
"""PR 7b-core: schema-012 pre-window backfill diagnostic — REAL CLI entry point (subprocess),
the shared parity matrix (CLI + 013 both refuse), and the retention-race DB-lock half."""

import os
import subprocess
import sys
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

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

<!-- complete-file: src/kyc_tool/ops/verify_pr7b_core_backfill.py -->
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
remain on 012 — activation (`020`) is downstream and cannot repair this. It NEVER writes.

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
    "WHERE o.id = :original_outbox_id AND o.kind = :original_kind "
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
        # A non-empty, NESTED, non-ASCII body: with '{}' the restore round-trip proved nothing,
        # because any empty payload matches any other (re-review 6a408a3 F6). Non-ASCII also
        # exercises the digest's UTF-8 handling.
        payload_a = json.dumps({
            "decision": "approve", "buy_enablement": "enabled",
            "gates": {"score_met": True, "legal_proof": True, "no_hard_conflict": True},
            "checks": [{"type": "website_verified", "source": "reviewer:José Ω"}],
        }, ensure_ascii=False)
        oid_a = _seed_legacy_callback(conn, case_id="c1", run_id="rA", decision_id="dA",
                                      ev_seq=1, status="delivered", payload=payload_a)
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
                       "original_created_at": r.created_at, "original_kind": r.kind,
                       "_payload": r.payload_json}
            for r, d in zip(
                conn.execute(text(
                    "SELECT id, kind, run_id, case_id, status, delivered_at, attempts, "
                    "next_attempt_at, last_error, created_at, payload_json, "
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
            "VALUES (:i,:k,:c,:r,CAST(:p AS jsonb),:st,:da,:at,:na,:le,:ca)"
        ), {"i": ev["original_outbox_id"], "k": ev["original_kind"],
            "p": json.dumps(ev["_payload"]), "c": ev["case_id"], "r": ev["run_id"],
            "st": ev["original_status"], "da": ev["original_delivered_at"],
            "at": ev["original_attempts"], "na": ev["original_next_attempt_at"],
            "le": ev["original_last_error"], "ca": ev["original_created_at"]})
        assert _accepted(conn, ev) == [(1,)]  # ACCEPTED: exactly one row, every component matched
        # the body really round-tripped — not an empty placeholder that would match anything
        stored = conn.execute(text("SELECT payload_json FROM outbox WHERE id=:i"),
                              {"i": ev["original_outbox_id"]}).scalar_one()
        assert stored == ev["_payload"] and stored["checks"][0]["source"] == "reviewer:José Ω"

        # (6) every evidence component is separately load-bearing: mutate one at a time, and
        # the acceptance predicate must reject the (unchanged, correctly restored) row.
        mutations = {
            "decision_id": "d-nope",
            "run_id": "r-nope",
            "case_id": "c-nope",
            "original_outbox_id": ev["original_outbox_id"] + 1000,
            "body_digest": "0" * 64,
            "original_kind": "poc_email",
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

        # a TAMPERED stored body must be rejected BEFORE the exact backup body is accepted —
        # otherwise the digest column proves nothing about what was actually restored.
        conn.execute(text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:i"),
                     {"i": ev["original_outbox_id"],
                      "p": json.dumps({**ev["_payload"], "decision": "reject"})})
        assert _accepted(conn, ev) == []            # rejected: stored body no longer matches
        conn.execute(text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:i"),
                     {"i": ev["original_outbox_id"], "p": json.dumps(ev["_payload"])})
        assert _accepted(conn, ev) == [(1,)]        # exact backup body restored -> accepted again

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

- [ ] **Step 4c: Listener-leak regression (re-review `6a408a3` F9)** — append to `tests/integration/test_migrations.py`. The two barrier tests register a PROCESS-WIDE hook; this proves a failure between registration and start cannot leak it:

```python
def test_barrier_listener_never_leaks_when_thread_start_raises(pg, monkeypatch):
    """F9: `Thread.join()` on a never-started thread raises RuntimeError. If cleanup joined
    unconditionally, that raise would abort the `finally` BEFORE `event.remove()` and the
    process-wide barrier would contaminate every later test. Force the exact failure and prove
    registration is still undone."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    def _boom(self):
        raise RuntimeError("injected: Thread.start() failed after listener registration")

    monkeypatch.setattr(threading.Thread, "start", _boom)
    url = _fresh_db(pg, "kyc_mig_013_leak_guard")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")

    def _barrier(conn, cursor, statement, params, context, executemany):  # pragma: no cover
        pass

    registered = started = False
    t = threading.Thread(target=lambda: None)
    with pytest.raises(RuntimeError, match="injected"):
        try:
            event.listen(Engine, "after_cursor_execute", _barrier)
            registered = True
            t.start()
            started = True
        finally:
            try:
                if started:
                    t.join(timeout=5)
            finally:
                if registered:
                    event.remove(Engine, "after_cursor_execute", _barrier)

    # the whole point: the hook is gone despite the raise, and nothing was left running
    assert not event.contains(Engine, "after_cursor_execute", _barrier)
    assert not t.is_alive()
```

Run: `.venv/bin/pytest tests/integration/test_migrations.py -k barrier_listener_never_leaks -v` → PASS.
**Mutation:** replace the inner `try/finally` with a bare unconditional `t.join(timeout=5)` → the
test must FAIL, because `join()` raises before `event.remove()` runs and `event.contains(...)`
is then still true. Restore.

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

## Task 8: ops CLIs — `reset_interrupted_outbox_claims` (post-013) + `repair_outbox_sequence` (drained)

Ships the post-013 stop/rollback helper: it **refuses pre-013 schema** (no `claim_token` column), clears only rows with the **complete** claim tuple, **preserves `next_attempt_at`**, and read-back-asserts zero claim tuples. It is NOT used in the forward cutover (the pre-013 schema has no claim columns; an interrupted old claim is encoded only in `next_attempt_at` and simply waits until its recorded due time).

**Files:**
- Create: `src/kyc_tool/ops/reset_interrupted_outbox_claims.py`
- Create: `tests/integration/test_reset_interrupted_outbox_claims.py`
- Create: `src/kyc_tool/ops/repair_outbox_sequence.py` (re-review `6a408a3` F7)
- Create: `tests/integration/test_repair_outbox_sequence.py`
- Re-pin: `tests/policy_driven/test_engine_build_id_guard.py`

**Interfaces:**
- Produces: `reset_claims(session_factory) -> int` (count cleared; raises if any claim tuple remains, or if the schema is pre-013); `main() -> int`.
- Produces: `repair_sequence(session_factory) -> int` (the value the next allocation will take; takes the ACCESS EXCLUSIVE fence, restarts the sequence, fail-closed read-back, raises on mismatch); `main() -> int`. NEVER calls `nextval`.
- Consumes: `kyc_tool.config.get_settings`, `kyc_tool.db.session`.

- [ ] **Step 1: Write the failing tests** — create `tests/integration/test_reset_interrupted_outbox_claims.py`:

<!-- complete-file: tests/integration/test_reset_interrupted_outbox_claims.py -->
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
    # Identical cleanup discipline to Task 6 (re-review 6a408a3 F9): registration and start are
    # tracked separately, because join() on a never-started thread raises and would otherwise
    # abort cleanup before event.remove() — leaking this process-wide hook into later tests.
    registered = started = False
    try:
        event.listen(Engine, "before_cursor_execute", _barrier)
        registered = True
        t.start()
        started = True
        assert at_readback.wait(timeout=15)  # reset's UPDATE done, about to read back
        with engine.begin() as conn:          # concurrent writer commits a NEW claimed row
            conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
                              "claim_lease_expires_at, claimed_by) VALUES "
                              "('poc_email','c1','email','pending', "
                              "gen_random_uuid(), now(), 'w2')"))
        go.set()
        t.join(timeout=15)
    finally:
        go.set()                                   # unconditional: never strand a waiting barrier
        try:
            if started:
                t.join(timeout=15)
                assert not t.is_alive(), "reset thread outlived the test"
        finally:                                   # a join/assert failure cannot skip removal
            if registered:
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

<!-- complete-file: src/kyc_tool/ops/reset_interrupted_outbox_claims.py -->
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

- [ ] **Step 5: Ship `repair_outbox_sequence` (re-review `6a408a3` F7)** — create `src/kyc_tool/ops/repair_outbox_sequence.py`:

<!-- complete-file: src/kyc_tool/ops/repair_outbox_sequence.py -->
```python
"""Drained outbox-sequence repair (PR 7b-core RUNBOOK step 0.6d escape hatch).

Run ONLY inside the drained maintenance window, after every writer is hard-stopped and attested
at zero. It exists because the step-0 precondition can legitimately fail when a restored id is
NOT below the sequence high-water (a restore from a divergent lineage rather than a prune).

Two things make this a shipped CLI rather than pasted SQL. `ALTER SEQUENCE ... RESTART WITH`
takes a LITERAL, not an expression, so the obvious one-liner is invalid SQL discovered mid-outage.
And the check must be fail-closed: a psql script that SELECTs a boolean still exits 0 when that
boolean is false, so a bad repair reports success. Here the exit status IS the result.

It never calls nextval() to probe: consuming an id and setval-ing it back is itself a write to
the object under repair. The read-back reads last_value/is_called from the sequence relation.

    python -m kyc_tool.ops.repair_outbox_sequence
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow

_SEQUENCE = "outbox_id_seq"


def repair_sequence(session_factory) -> int:
    """Restart the outbox id sequence at max(id)+1. Returns the value the NEXT allocation takes.

    Raises RuntimeError (rolling the transaction back) if the post-restart read-back is not
    exactly (next_id, is_called=false) — the caller must treat that as a failed repair.
    """
    with uow(session_factory) as session:
        # The fence. Writers are already stopped by the runbook; this makes that a guarantee
        # rather than an assumption, and ALTER SEQUENCE (unlike setval) excludes concurrent
        # nextval for the duration of the transaction.
        session.execute(text("LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE"))
        next_id = session.execute(text("SELECT COALESCE(max(id), 0) + 1 FROM outbox")).scalar_one()
        if not isinstance(next_id, int) or next_id < 1:  # never interpolate an untrusted value
            raise RuntimeError(f"refusing to restart {_SEQUENCE}: computed next_id={next_id!r}")
        session.execute(text(f"ALTER SEQUENCE {_SEQUENCE} RESTART WITH {next_id}"))
        last_value, is_called = session.execute(
            text(f"SELECT last_value, is_called FROM {_SEQUENCE}")
        ).one()
        if last_value != next_id or is_called:
            raise RuntimeError(
                f"{_SEQUENCE} repair FAILED read-back: expected ({next_id}, False), "
                f"got ({last_value}, {is_called}) — transaction rolled back, sequence unchanged"
            )
        return next_id


def main() -> int:
    settings = get_settings()
    try:
        next_id = repair_sequence(make_session_factory(make_engine(settings.database_url)))
    except RuntimeError as exc:
        print(f"repair_outbox_sequence: FAILED — {exc}", file=sys.stderr)
        return 1
    print(f"repair_outbox_sequence: OK — next allocation will be {next_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Its test** — create `tests/integration/test_repair_outbox_sequence.py`:

<!-- complete-file: tests/integration/test_repair_outbox_sequence.py -->
```python
"""PR 7b-core: the drained sequence repair, through its REAL entry point, on a divergent lineage."""

import os
import subprocess
import sys
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops import repair_outbox_sequence as repairer
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def _seed_rows(engine, n):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        for _ in range(n):
            conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                              "VALUES ('poc_email','c1','email','pending')"))
        return conn.execute(text("SELECT max(id) FROM outbox")).scalar_one()


def test_repair_restarts_divergent_sequence_and_next_allocation_is_exact(pg):
    """Divergent lineage: rows exist above the sequence's position (what a restore from another
    lineage leaves behind). The REAL CLI must move the sequence past every existing id, and the
    NEXT allocation must be exactly max(id)+1 — checked on this disposable DB by consuming one."""
    url = _fresh_db(pg, "kyc_seq_repair")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    max_id = _seed_rows(engine, 5)
    with engine.begin() as conn:      # rewind BELOW the data: the divergent-lineage state
        conn.execute(text("ALTER SEQUENCE outbox_id_seq RESTART WITH 1"))

    proc = subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.repair_outbox_sequence"],
        env={**os.environ, "KYC_DATABASE_URL": url}, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"next allocation will be {max_id + 1}" in proc.stdout

    with engine.begin() as conn:      # disposable DB: consuming one id here is the proof
        assert conn.execute(text("SELECT nextval('outbox_id_seq')")).scalar_one() == max_id + 1
    engine.dispose()


def test_repair_is_fail_closed_on_a_bad_readback(pg, monkeypatch):
    """The read-back is the whole safety property: if the sequence is not exactly where the
    repair intended, it must RAISE and roll back rather than report success. The earlier psql
    block printed a boolean and exited 0, so a bad repair looked like a good one."""
    url = _fresh_db(pg, "kyc_seq_repair_failclosed")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    _seed_rows(engine, 3)
    sf = make_session_factory(make_engine(url))
    real_text = repairer.text

    def _poison(sql):  # make the ALTER a no-op so the read-back cannot match
        return real_text("SELECT 1") if sql.startswith("ALTER SEQUENCE") else real_text(sql)

    monkeypatch.setattr(repairer, "text", _poison)
    with pytest.raises(RuntimeError, match="FAILED read-back"):
        repairer.repair_sequence(sf)
    engine.dispose()


def test_repair_takes_the_access_exclusive_fence(pg):
    """The drain fence is part of the procedure. A second connection holding ROW EXCLUSIVE must
    block the repair — proving the LOCK is real and not decorative."""
    url = _fresh_db(pg, "kyc_seq_repair_fence")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    _seed_rows(engine, 2)
    blocker = create_engine(url)
    conn = blocker.connect()
    proc = None
    try:
        tx = conn.begin()
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                          "VALUES ('poc_email','c1','email','pending')"))  # holds ROW EXCLUSIVE
        proc = subprocess.Popen(
            [sys.executable, "-m", "kyc_tool.ops.repair_outbox_sequence"],
            env={**os.environ, "KYC_DATABASE_URL": url},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(3)
        assert proc.poll() is None      # BLOCKED on ACCESS EXCLUSIVE
        tx.rollback()
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, out + err
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)
        conn.close()
        blocker.dispose()
        engine.dispose()
```

Run: `.venv/bin/pytest tests/integration/test_repair_outbox_sequence.py -v` → PASS (3 tests).
**Mutations (manual, verify-then-restore):** (a) delete the `LOCK TABLE ... ACCESS EXCLUSIVE` line → the fence test's `assert proc.poll() is None` must FAIL (the repair no longer waits). (b) delete the read-back `raise` → `test_repair_is_fail_closed_on_a_bad_readback` must FAIL (a poisoned ALTER now reports success). (c) replace the read-back with `SELECT nextval(...)` → the divergent-lineage test's exact-allocation assertion must FAIL, because probing consumed the id the next writer was owed.

- [ ] **Step 7: RED drift-guard step** — new CLI changed `src/`:

Run: `.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v` → **FAIL** (RED step).

- [ ] **Step 8: Re-pin → GREEN, full gate + commit** (canonical close-out order)

```bash
# re-pin EXPECTED_ENGINE_SOURCE_HASH (one-liner), then:
.venv/bin/pytest tests/policy_driven/test_engine_build_id_guard.py -v
./manage.sh test
.venv/bin/ruff check .
.venv/bin/lint-imports
# BOTH CLIs and BOTH test suites (re-audit 1f8412e F5 — the earlier form staged only the reset
# CLI, so repair_outbox_sequence and its tests existed in the worktree, passed locally, and were
# silently absent from the commit and from CI while Task 9's docs cited the CLI as shipped):
git add src/kyc_tool/ops/reset_interrupted_outbox_claims.py \
        src/kyc_tool/ops/repair_outbox_sequence.py \
        tests/integration/test_reset_interrupted_outbox_claims.py \
        tests/integration/test_repair_outbox_sequence.py \
        tests/policy_driven/test_engine_build_id_guard.py
git commit -m "feat(ops): reset_interrupted_outbox_claims + repair_outbox_sequence (post-013 recovery CLIs)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015B9mwM3CytAHNuR3bxnXTK"

# POST-COMMIT MANIFEST ASSERTIONS (re-audit 1f8412e F5) — a worktree-green/commit-absent split is
# invisible to every local test run, so check the COMMIT, not the tree:
git show --name-only --format= HEAD | sort   # must list exactly the five files staged above
git status --porcelain -- src/kyc_tool/ops tests/integration   # must print NOTHING (no intended
                                                               # file left untracked/unstaged)
```

---

## Task 9: Docs + governance

Documents stream separation + local ordering + the 7b-core/activation boundary in `OVERVIEW.md`; the full ordered step-0 pre-window + drained cutover + witness-aware forward-only rollback (supersession OR any attempt/digest refuses; flag/image rollback thereafter) in `RUNBOOK.md`/`DEPLOYMENT.md`; and the A6 exception + backfill-as-deterministic-reconstruction + local/cross-replica boundary in `AUDIT_FINDINGS.md`. Docs-only — no `src/` change, no drift re-pin. **The `.agents/ROADMAP.md §C` `shipped` flip is NOT here** — `test_migration_lineage.py` asserts `authored_reserved == shipped` by reading BOTH the ROADMAP and `alembic/versions/` off disk, so the flip is forced the moment `013_outbox_stream_separation.py` exists (Task 1 Step 3b) and must ride the SAME atomic commit as the migration (Task 6). Deferring it to Task 9 would leave Task 6's commit red in CI, which checks out the commit rather than the worktree.

**Files:**
- Modify: `docs/OVERVIEW.md`, `docs/RUNBOOK.md`, `docs/DEPLOYMENT.md`
- Modify: `AUDIT_FINDINGS.md` (backfill provenance note; A6 already amended in Task 5 — confirm)
- Create: `tests/unit/test_docs_cutover_parity.py` (RUNBOOK/DEPLOYMENT full-body parity)
- Create: `tests/integration/test_rollback_command.py` (the exact `alembic … downgrade 012` command)
- Verify: `tests/unit/test_migration_lineage.py` (already green — flipped in Task 1, committed in Task 6)

**Interfaces:**
- Consumes: everything shipped in Tasks 1-8 (CLIs by name, migration behavior).
- Produces: docs + governance only. The lineage invariant (head ∈ shipped, `pending[0]` = head+1) was established in Task 1; head is now `019` (the payload repair) and `pending[0]=020` (7b-activation).

- [ ] **Step 1: Confirm the lineage guard is already green** — it was satisfied in Task 1 Step 3b and committed atomically with `013` in Task 6; this step only proves no later task regressed it:

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py -v`
Expected: PASS.

- [ ] **Step 2: Confirm the ROADMAP row committed in Task 6 reads exactly this** (do not re-edit it here — it is already in the atomic `013` commit):

```markdown
| PR 7b-core | 8 | shipped | 013 | `outbox.ordering_stream` (NOT NULL) + `case_id` NOT NULL + per-(case,stream) claim; `decisions.decision_sequence` + `cases.last_decision_sequence` + a **best-effort** local `superseded` guard (higher *locally-stamped* delivery only; send-before-stamp / cross-replica / manual-current-then-late-automatic-callback reverts remain for 7b-activation); identity + ordering (`UNIQUE decisions(case_id, decision_sequence)` [per-case namespace] + `UNIQUE(run_id)` + triple FK + composite `decisions(run_id,case_id)→runs(id,case_id)` FK + partial callback index); fenced claim (`claim_token`); exhaustive per-status lifecycle CHECKs (`superseded` decision-only); `ordering_stream`/`case_id` real `SET NOT NULL` (drained cutover, **reversible-before-first-supersession** downgrade) |
```

(Leave 7b-activation `020` and every other row unchanged. Also update the prose note near line 12-14 if it says "7b-core is next" — change to "7b-core shipped (`013`-`018`); 7b-activation (`020`) is next".)

- [ ] **Step 3: Re-run the lineage guard after the docs edits**

Run: `.venv/bin/pytest tests/unit/test_migration_lineage.py -v`
Expected: PASS (`head 019 ∈ shipped`; `pending[0]=020=head+1`; disjoint/exhaustive/contiguous all hold).

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
closed here and remain expected until 7b-activation (`020`) adds the platform high-water mark:
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
    `BLOCKED_NO_AUTHORITATIVE_MAPPING`. Backup availability is an operator prerequisite. Activation (`019`) is
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
         WHERE o.id = :original_outbox_id AND o.kind = :original_kind
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
        **Run the SHIPPED, TESTED ops CLI — do not paste SQL mid-outage:**
        `python -m kyc_tool.ops.repair_outbox_sequence`
        It takes `LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE` inside one transaction (the drain
        fence is part of the procedure, not advice), computes `COALESCE(max(id),0)+1`, performs
        the `ALTER SEQUENCE … RESTART WITH <literal>` (`RESTART WITH` takes a literal, never an
        expression), and then **fail-closed reads back** `last_value`/`is_called` from the sequence
        relation, rolling back and exiting **nonzero** unless they are exactly `(next_id, false)`.
        It never calls `nextval` to probe: consuming an id to check the sequence, then `setval`-ing
        it back, is itself a write to the object being repaired. Its exit status is the result —
        a printed boolean is not, which is why the earlier psql block could report success after a
        bad repair.
    (e) Only then rerun 0.4 (it must be clean — it also proves existence/1:1 of every mapping).

**Cutover (only after 0.4 is green):**
1. Pause submission, edge-block the composer, disable autoscaling/restarts.
2. Hard-stop API, pipeline, outbox, `dev_worker` (queue AND outbox), retention, and every writer;
   attest zero at the orchestrator.
3. Run the shipped `python -m kyc_tool.ops.requeue_interrupted_jobs`. NO outbox reset here — the
   pre-013 schema has no claim columns; an interrupted old claim simply waits until its already-
   recorded `next_attempt_at`. Preserve every pending row's `next_attempt_at`.
4. Run `python -m alembic -c alembic.ini upgrade head` (the chain `013`→`014`→`015`→`016`→`017`→`018`→`019`) — the deployment image runs its exact
   equivalent. This repeats the §0 parity preflights under the zero-writer boundary and is the
   authoritative fail-closed check (the pre-window diagnostic is an early detector, not a substitute).
   `017` additionally machine-checks the drain: it refuses with `MIGRATION_017_PREFLIGHT_LIVE_CLAIMS`
   while any live (unexpired) outbox claim exists — leases must expire or be reset first.
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
R4. **With `019` installed there is no schema-downgrade path**: `018` and `019` refuse unconditionally
    (`MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY`) because walking below it would restore
    search-path-vulnerable authority functions, so rollback goes straight to R5 (image-only on
    schema `018`). The walk below is the HISTORICAL path, reachable only on a schema that never
    reached `018`/`019`: run `python -m alembic -c alembic.ini downgrade 012` (the revision is a
    REQUIRED positional argument — a bare `alembic downgrade` exits with a usage error
    mid-outage). That walk is `017 → 016 → 015 → 014 → 013 → 012`, and EACH revision preflights
    under
    `LOCK TABLE ... ACCESS EXCLUSIVE` (child-first from `015` on; `017` first takes the shared
    maintenance/writer advisory fence EXCLUSIVE, so it queues behind live witness writers instead
    of reasoning about their lock order). Sentinels in execution order:
    - `017` refuses — `MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE` — when ANY attempt row,
      terminal wire digest, or `attempt_v1` decision callback exists. NEGATIVE evidence counts:
      an attempt-regime row with no attempt is the durable proof nothing was staged.
    - `016` refuses — `MIGRATION_016_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same rule one revision
      down (defense in depth below `017`), including the `attempt_v1` negative-evidence case.
    - `015` refuses — `MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE` — on any attempt row or
      terminal digest (child-first lock order; cannot deadlock a live writer).
    - `014` refuses — `MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE` — same witness rule.
    - `013` refuses on a `superseded` row, a surviving terminal digest
      (`MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE`), or the attempt table under a bare `013`
      stamp (`MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY`).
R5. ROLLBACK OUTCOME A — downgrade REFUSED (any sentinel above): the DB stays on the
    witness-authority schema, so KEEP or redeploy the reviewed **`019`-COMPATIBLE image** digest —
    an older publisher lacks the receipt/terminal contract and MUST NOT run against preserved
    evidence; the pre-7b image is PROHIBITED outright. Rollback after first witness use is a
    FLAG/IMAGE rollback on the compatible schema, never a schema downgrade. A pre-7b image is
    permitted ONLY after the entire walk reaches `012` (outcome B). Verify `/readyz`, start + attest its fenced workers, then
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
- **🔵 RETENTION — VERIFY `AUDIT_FINDINGS.md` D9; do NOT append a second retention block
  (re-audit `1f8412e` F8).** The governed decision already exists as D9: the decision_callback ROW
  survives as the durable ordering authority; its BODY (which carried reviewer-derived
  `checks[].source`) is redacted past `KYC_RETENTION_DAYS`; the surviving remainder (ids, ordinals,
  digest) is pseudonymous and its retention is governed, with the deployer's data controller
  accountable and pre-redaction backups aging out on their own schedule. Task 9's job here is to
  CONFIRM D9, ROADMAP, RUNBOOK, and the module docstring still agree — an appended near-duplicate
  block would be a second source of truth that drifts. Attempt rows are pruned only where the
  terminal digest makes them redundant; for any other row they are sole evidence and retention
  never touches them.
  - **Capacity is part of the contract, not an afterthought:** the claim indexes are **partial to
    `status='pending'`**, the exact live alerting count is served by `014`'s
    `ix_outbox_live_status` (pending, dead), and `/v1/metrics` reports terminal history as a
    planner-statistics ESTIMATE (`outbox_terminal_total_estimate`) rather than any count that scans
    the unbounded terminal tail.
  - The POC token — the genuinely sensitive outbox body — is still destroyed twice over: redacted at
    delivery (`publisher.py:176-182`) and pruned on schedule.
```

- [ ] **Step 7: Add the docs-contract test + the exact-rollback-command acceptance test** — create `tests/unit/test_docs_cutover_parity.py`:

<!-- complete-file: tests/unit/test_docs_cutover_parity.py -->
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
        "python -m alembic -c alembic.ini downgrade 012", "R6.",
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
        "SEPARATE DRAINED action", "python -m kyc_tool.ops.repair_outbox_sequence",
        "LOCK TABLE outbox IN ACCESS EXCLUSIVE MODE",
        "COALESCE(max(id), 0) + 1",
        # the repair is the SHIPPED fail-closed CLI, not a psql block that exits 0 on a
        # false boolean. Assert the CLI and its readback contract, not the deleted recipe:
        "repair_outbox_sequence: OK", "repair_outbox_sequence: FAILED",
        "exit status IS the result",
        "substituting `now()` for `delivered_at` is prohibited",
    ):
        assert token in rb  # safety-critical details survive, not just the numbered leaders
    assert "setval(pg_get_serial_sequence" not in rb  # the live sequence write must stay deleted
    assert "RESTART WITH <" not in rb  # no pseudocode placeholder survives into the runbook
    assert rb.count("edge-block the composer") == 2  # forward + rollback both establish the fence
    assert rb.count("remove the composer edge block") == 3  # forward + both rollback outcomes clear it
```

Create `tests/integration/test_rollback_command.py`:

<!-- complete-file: tests/integration/test_rollback_command.py -->
```python
"""The EXACT documented rollback command must be valid against a real HEAD database.

Upgrading only to `013` and rolling back would never execute the repair (`014`) and hardening
(`015`) downgrades a production head actually walks (re-audit `4dfdf8a` F6) — the unused path,
the witness refusal, and the sentinel all have to come from the real multi-revision command.

`alembic.ini` ships an empty `sqlalchemy.url`, so env.py (`_database_url`) falls through to
`KYC_DATABASE_URL`; the command below runs from REPO_ROOT with that env pointing at the dedicated
DB."""

import os
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from kyc_tool.config import REPO_ROOT
from tests.integration.test_migrations import _fresh_db

pytestmark = pytest.mark.postgres

# EXACTLY the argv the docs prescribe: `python -m alembic -c alembic.ini downgrade 012`
# (run from REPO_ROOT). Invoked through the RUNNING interpreter, never a hardcoded
# ".venv/bin/alembic": CI installs with `pip install -e '.[dev]'` and has no .venv, so the
# hardcoded path raises FileNotFoundError there while passing locally.
_ALEMBIC = [sys.executable, "-m", "alembic"]


def _head_db(pg, name):
    url = _fresh_db(pg, name)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    eng.dispose()
    assert head == "019", f"the documented rollback must start from the REAL head, got {head}"
    return url


def test_documented_rollback_command_refuses_at_the_forward_only_head(pg):
    """Even a WITNESS-FREE database refuses: `018` is forward-only, so the exact documented
    command stops at the outermost authority rather than walking the chain. This is the case
    `017` would have walked cheerfully — straight into unqualified, caller-search-path
    authority functions (re-audit `cbb783b` F3)."""
    url = _head_db(pg, "kyc_rollback_cmd")
    env = {**os.environ, "KYC_DATABASE_URL": url}
    refused = subprocess.run([*_ALEMBIC, "-c", "alembic.ini", "downgrade", "012"],
                             cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0
    assert "MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY" in refused.stdout + refused.stderr
    eng = create_engine(url)
    with eng.connect() as conn:  # the schema does not move; rollback is image-only from here
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "019"
    eng.dispose()

    # the SAME argv without the positional revision is the invalid form the docs must never use
    bad = subprocess.run([*_ALEMBIC, "-c", "alembic.ini", "downgrade"],
                         cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode != 0


def test_documented_rollback_command_refuses_on_witness_evidence(pg):
    """With one staged attempt on the books, the SAME documented command must refuse at the
    FIRST downgrade (017) with its stable sentinel, leaving the schema at head — the flag/image
    rollback branch of the runbook, proven through the real command."""
    url = _head_db(pg, "kyc_rollback_cmd_witness")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('e1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('r1','c1','e1','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('d1','c1','r1','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,1)"))
        # born unclaimed (017's lifecycle INSERT guard), then claimed — the legal road
        oid = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status) VALUES ('decision_callback','c1','r1','decision',1,'pending') "
            "RETURNING id")).scalar_one()
        row = conn.execute(text(
            "UPDATE outbox SET claim_token = gen_random_uuid(), "
            "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
            "WHERE id = :i RETURNING id, claim_token"), {"i": oid}).one()
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})

    env = {**os.environ, "KYC_DATABASE_URL": url}
    refused = subprocess.run([*_ALEMBIC, "-c", "alembic.ini", "downgrade", "012"],
                             cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0
    assert "MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY" in refused.stdout + refused.stderr
    with eng.connect() as conn:  # still at head; nothing was dropped underneath the evidence
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "019"
        assert conn.execute(text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one() == 1
    eng.dispose()
```

- [ ] **Step 8: Verify + full gate + commit**

```bash
# ROADMAP: 7b-core shipped/013-019, 7b-activation still pending/020, no contradictory allocation
rg -n "PR 7b-core \| 8 \| shipped \| 013" .agents/ROADMAP.md
rg -n "PR 7b-activation \| 8 \| pending \| 019" .agents/ROADMAP.md
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
- [ ] Confirm `rg -n "013" .agents/ROADMAP.md` shows no contradictory live allocation and the 7b-core chain (`013`-`019`) is `shipped`, 7b-activation `020` still `pending`.
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
7. **(P2) Valid rollback command + full docs parity.** Both docs now carry `python -m alembic -c alembic.ini downgrade 012` (positional revision); a rollback-command acceptance test runs that exact form (and proves the bare form fails); a docs-contract test compares the FULL RUNBOOK/DEPLOYMENT section bodies byte-for-byte (continuation lines included) + asserts the safety tokens.
8. **(P3) Bus lifecycle + gates.** Added the parent-only Step 0 CLAIM (read `AGENT_BUS.md`/`ROADMAP`, verify no conflicting CLAIM, post+push the final file set) and the post-Task-9 RELEASE (anchor, commands/results, migration witnesses, residual-risk boundary, "M2/normative untouched", `turn: CODEX`, hold for `AUDIT-CLEAN`), and stated the human plan-approval gate precedes execution.

## Codex plan-review round 3 — 6 findings closed (all real; REVIEW-CLEAN boundary intact, no 014 pulled in)

1. **(P1) Rollback/forward resume.** The canonical RUNBOOK/DEPLOYMENT block is now a two-branch state machine: forward step 6 restores all five paused controls; rollback R4 branches to **R5 (downgrade REFUSED → keep the reviewed head-compatible image (`019` today), prohibit pre-7b, restore all five OR declare an incident)** and **R6 (downgrade SUCCEEDED → deploy prior digest, `/readyz` + workers, restore all five)**. "All five" includes removing the composer edge block in addition to retention, autoscaling, restarts, and submissions. `test_docs_cutover_parity.py` requires both `ROLLBACK OUTCOME A/B`, `PROHIBIT the pre-7b image`, the resume token, two edge-block establishments, and three edge-block removals — beside the full-body byte-identical compare.
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
