"""Re-audit `7d1c435..827bc0f` F2/F4 REDs: manual dead-job recovery is ONE conditional UPDATE
(status CAS + int4-guarded fixed grant) — a recovery racing another recovery or a live worker claim
mutates nothing, and repeated exhaust/recover cycles grant exactly N further attempts instead of
doubling the ceiling."""

import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from kyc_tool.config import PG_INT4_MAX
from kyc_tool.db.session import uow
from kyc_tool.ops.requeue_service import requeue_dead_job
from kyc_tool.queue import jobs

pytestmark = pytest.mark.postgres

GRANT = 5


def _dead_job(session_factory, *, max_attempts=1):
    with uow(session_factory) as s:
        jid = jobs.enqueue(s, "run_transition", {}, case_id="rc", max_attempts=max_attempts).id
    with uow(session_factory) as s:
        assert jobs.claim(s, ["run_transition"], "worker-a", 120) is not None
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    with uow(session_factory) as s:
        jobs.reap_expired(s)
    with uow(session_factory) as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": jid}).scalar_one() == "dead"
    return jid


def test_concurrent_recoveries_produce_one_winner_one_409_one_audit(session_factory, clean_db):
    jid = _dead_job(session_factory)
    barrier = threading.Barrier(2)
    outcomes = []

    def recover():
        barrier.wait()
        try:
            outcomes.append(requeue_dead_job(session_factory, jid, attempt_grant=GRANT))
        except HTTPException as exc:
            outcomes.append(exc.status_code)

    threads = [threading.Thread(target=recover) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    wins = [o for o in outcomes if isinstance(o, dict)]
    losses = [o for o in outcomes if o == 409]
    assert len(wins) == 1 and len(losses) == 1  # exactly one authority, one stable refusal
    with session_factory() as s:
        row = s.execute(text("SELECT status, attempts, max_attempts FROM jobs WHERE id=:i"),
                        {"i": jid}).one()
        audits = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action='job.requeued'")
        ).scalar_one()
    assert (row.status, row.max_attempts) == ("queued", row.attempts + GRANT)  # granted ONCE
    assert audits == 1  # the loser wrote no duplicate audit evidence


def test_recovery_racing_a_worker_claim_cannot_erase_the_live_owner(session_factory, clean_db):
    """The audit's F2 witness: A pre-reads 'dead', B recovers and a worker claims; A's UPDATE must
    now be a no-op 409 — never `locked_by=NULL` over a running claim."""
    jid = _dead_job(session_factory)
    requeue_dead_job(session_factory, jid, attempt_grant=GRANT)  # B's recovery wins first
    with uow(session_factory) as s:
        live = jobs.claim(s, ["run_transition"], "worker-live", 120)
    assert live is not None
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, jid, attempt_grant=GRANT)  # A's stale recovery
    assert exc.value.status_code == 409
    with session_factory() as s:
        row = s.execute(text("SELECT status, locked_by, max_attempts FROM jobs WHERE id=:i"),
                        {"i": jid}).one()
    assert (row.status, row.locked_by) == ("running", live.claim_nonce)  # live owner untouched
    assert row.max_attempts == live.max_attempts  # and no second budget grant slipped in


def test_recovery_refuses_at_the_int4_ceiling_without_mutation(session_factory, clean_db):
    jid = _dead_job(session_factory)
    with uow(session_factory) as s:  # a legal row whose counter cannot accept another grant
        s.execute(text("UPDATE jobs SET attempts=:m, max_attempts=:m WHERE id=:i"),
                  {"m": PG_INT4_MAX, "i": jid})
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, jid, attempt_grant=GRANT)
    assert exc.value.status_code == 409 and "int4" in exc.value.detail
    with session_factory() as s:
        row = s.execute(text("SELECT status, attempts, max_attempts, locked_by FROM jobs "
                             "WHERE id=:i"), {"i": jid}).one()
        audits = s.execute(
            text("SELECT count(*) FROM audit_log WHERE action='job.requeued'")
        ).scalar_one()
    # governed refusal: no job, run, or audit mutation — not an integer-out-of-range 500
    assert (row.status, row.attempts, row.max_attempts, audits) == ("dead", PG_INT4_MAX, PG_INT4_MAX, 0)


def test_repeated_recovery_grants_exactly_n_each_cycle_not_doubling(session_factory, clean_db):
    """The audit's F4 growth witness (5 → 10 → 20 → 40 remaining) must flatten to exactly N per
    cycle: max_attempts always lands at attempts + N, never attempts + old ceiling."""
    jid = _dead_job(session_factory)
    requeue_dead_job(session_factory, jid, attempt_grant=GRANT)
    with session_factory() as s:
        c1 = s.execute(text("SELECT attempts, max_attempts FROM jobs WHERE id=:i"), {"i": jid}).one()
    assert c1.max_attempts == c1.attempts + GRANT  # cycle 1: 1 + 5

    # exhaust the granted budget (simulated: the counter reaches the ceiling) and dead-letter again
    with uow(session_factory) as s:
        assert jobs.claim(s, ["run_transition"], "worker-b", 120) is not None
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET attempts = max_attempts, "
                       "lease_expires_at = now() - interval '1 second' WHERE id=:i"), {"i": jid})
    with uow(session_factory) as s:
        jobs.reap_expired(s)
    requeue_dead_job(session_factory, jid, attempt_grant=GRANT)
    with session_factory() as s:
        c2 = s.execute(text("SELECT attempts, max_attempts FROM jobs WHERE id=:i"), {"i": jid}).one()
    assert c2.max_attempts == c2.attempts + GRANT  # cycle 2: still exactly +5 remaining
    assert c2.max_attempts - c1.max_attempts == GRANT  # ceiling grew by N, not by itself


def test_bad_grant_refuses_before_any_db_write(session_factory, clean_db):
    jid = _dead_job(session_factory)
    for bad in (0, -1, True, 1.5, 1001):
        with pytest.raises(ValueError):
            requeue_dead_job(session_factory, jid, attempt_grant=bad)
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": jid}).scalar_one() == "dead"
