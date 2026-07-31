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
