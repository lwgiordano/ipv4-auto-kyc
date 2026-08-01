"""PR 7b-core: post-013 reset_interrupted_outbox_claims — REAL CLI entry point (subprocess)
on schema 012 (refuses) and 013 (clears only complete tuples), plus atomic-rollback race."""

import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.db.session import make_engine, make_session_factory
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
    """On schema 012 the REAL CLI exits nonzero (below the required revision 013) and writes
    NOTHING — two future-backoff rows' next_attempt_at are unchanged (rollout order: no pre-013
    reset). F6 (`42e1c7d..b39b82a`): the refusal is now a GOVERNED sentinel, not a bare traceback."""
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
    assert "OPS_COMMAND_SCHEMA_REFUSED" in proc.stderr and "013" in proc.stderr
    assert "Traceback" not in proc.stderr  # governed refusal, not a crash
    with engine.connect() as conn:
        after = [r.next_attempt_at for r in conn.execute(
            text("SELECT next_attempt_at FROM outbox ORDER BY id"))]
    assert after == before  # nothing written
    engine.dispose()


def test_reset_refuses_a_spoofed_unknown_revision_that_string_sorts_above_013_subprocess(pg):
    """F8 (`b39b82a..b53daf4`): a spoofed/unknown alembic_version like '999' string-compares
    >= '013' but is NOT a descendant of 013 in the migration graph. The old `version < min_revision`
    string test let it through, so the reset then tracebacked on the claim columns 012 lacks. bind()
    now checks real lineage: '999' is refused as a governed sentinel, nothing written, no crash."""
    url = _fresh_db(pg, "kyc_reset_spoof999")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='999'"))  # spoof a high revision
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status, next_attempt_at) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'pending', now() + interval '7 minutes')"))
        before = [r.next_attempt_at for r in conn.execute(
            text("SELECT next_attempt_at FROM outbox ORDER BY id"))]

    proc = _run_reset(url)
    assert proc.returncode != 0
    assert "OPS_COMMAND_SCHEMA_REFUSED" in proc.stderr           # governed lineage refusal
    assert "Traceback" not in proc.stderr                        # not a crash on a missing column
    with engine.connect() as conn:
        after = [r.next_attempt_at for r in conn.execute(
            text("SELECT next_attempt_at FROM outbox ORDER BY id"))]
    assert after == before                                       # nothing written
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


# SUPERSEDED (Codex re-audit `f495de8` F3): `test_reset_atomic_rollback_when_tuple_appears_
# before_readback` pinned rollback-on-detection — but detection was the whole guarantee, and a
# claimant committing in the read-back→commit window slipped past it: the command reported
# "0 claim tuples remain" while one existed. The reset now holds ACCESS EXCLUSIVE on
# public.outbox from before its UPDATE through commit, so that claimant BLOCKS instead of
# falsifying the statement; under the fence this test's mid-window insert can no longer happen
# and its assertions describe an unreachable interleaving. The replacing contract lives in
# tests/integration/test_ops_binding.py::
# test_reset_fence_blocks_a_claimant_arriving_in_the_readback_window (barrier AFTER the zero
# read; the late claim lands strictly after commit), alongside the shadow-schema and
# lock-timeout proofs for every ops command.
