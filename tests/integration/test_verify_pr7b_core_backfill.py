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


def test_cli_refuses_when_decisions_relation_is_dropped(pg):
    """Re-audit `8aba2df..2cee937` R3-F7: dropping `decisions` at stamp 012 left the parity matrix to
    traceback UndefinedTable. The shared PR7B_CORE_PREWINDOW shape contract requires the relation, so
    the diagnostic now refuses (governed) before taking the SHARE lock or reading data."""
    url = _fresh_db(pg, "kyc_backfill_no_decisions")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE decisions CASCADE"))
    engine.dispose()

    proc = _run_cli(url)
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "OPS_COMMAND_SCHEMA_REFUSED" in out
    assert "decisions" in out
    assert "Traceback" not in proc.stderr
    assert "UndefinedTable" not in out
