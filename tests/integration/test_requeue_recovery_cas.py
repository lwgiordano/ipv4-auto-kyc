"""Re-audit `7d1c435..827bc0f` F2/F4 REDs (CAS + fixed grant) extended per `750630c..ca85355`
F1/F6: recovery is a CASE-ORDERING authority — only the LATEST job of a case, with nothing
running beside it, and only onto a verified FAILED run of the SAME case. Anything else refuses
with a governed 409 and mutates nothing (job stays dead, victim runs untouched, no audit row)."""

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


def _seed_case_run(session_factory, case_id, run_id, *, state="FAILED", seq=1):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        s.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "(:e, :c, :k, 'h', 'x', '{}'::jsonb, '{}'::jsonb, :sq)"
            ),
            {"e": f"e-{run_id}", "c": case_id, "k": f"k-{run_id}", "sq": seq},
        )
        s.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES (:r, :c, :e, :st)"
            ),
            {"r": run_id, "c": case_id, "e": f"e-{run_id}", "st": state},
        )
        s.commit()


def _dead_job(session_factory, *, case_id="rc", run_id="rc-r", run_state="FAILED", max_attempts=1):
    """A dead job whose payload names a run of its case (the worker's real shape)."""
    _seed_case_run(session_factory, case_id, run_id, state=run_state)
    with uow(session_factory) as s:
        jid = jobs.enqueue(
            s, "run_transition", {"run_id": run_id}, case_id=case_id, max_attempts=max_attempts
        ).id
    with uow(session_factory) as s:
        claimed = jobs.claim(s, ["run_transition"], "worker-a", 120)
    assert claimed is not None and claimed.id == jid
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' "
                       "WHERE id=:i"), {"i": jid})
    with uow(session_factory) as s:
        jobs.reap_expired(s)
    with uow(session_factory) as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": jid}).scalar_one() == "dead"
    return jid


def _job_row(session_factory, jid):
    with session_factory() as s:
        return s.execute(
            text("SELECT status, attempts, max_attempts, locked_by FROM jobs WHERE id=:i"),
            {"i": jid},
        ).one()


def _requeued_audits(session_factory):
    with session_factory() as s:
        return s.execute(
            text("SELECT count(*) FROM audit_log WHERE action='job.requeued'")
        ).scalar_one()


# ── R9-F2/F4: CAS + fixed bounded grant ───────────────────────────────────────────────────────────
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
    row = _job_row(session_factory, jid)
    assert (row.status, row.max_attempts) == ("queued", row.attempts + GRANT)  # granted ONCE
    assert _requeued_audits(session_factory) == 1  # the loser wrote no duplicate audit evidence


def test_recovery_racing_a_worker_claim_cannot_erase_the_live_owner(session_factory, clean_db):
    """R9-F2 witness: A pre-reads 'dead', B recovers and a worker claims; A's UPDATE must now be
    a no-op 409 — never `locked_by=NULL` over a running claim."""
    jid = _dead_job(session_factory)
    requeue_dead_job(session_factory, jid, attempt_grant=GRANT)  # B's recovery wins first
    with uow(session_factory) as s:
        live = jobs.claim(s, ["run_transition"], "worker-live", 120)
    assert live is not None
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, jid, attempt_grant=GRANT)  # A's stale recovery
    assert exc.value.status_code == 409
    row = _job_row(session_factory, jid)
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
    row = _job_row(session_factory, jid)
    # governed refusal: no job, run, or audit mutation — not an integer-out-of-range 500
    assert (row.status, row.attempts, row.max_attempts) == ("dead", PG_INT4_MAX, PG_INT4_MAX)
    assert _requeued_audits(session_factory) == 0


def test_repeated_recovery_grants_exactly_n_each_cycle_not_doubling(session_factory, clean_db):
    """R9-F4 growth witness (5 → 10 → 20 → 40 remaining) must flatten to exactly N per cycle:
    max_attempts always lands at attempts + N, never attempts + old ceiling."""
    jid = _dead_job(session_factory)
    requeue_dead_job(session_factory, jid, attempt_grant=GRANT)
    with session_factory() as s:
        c1 = s.execute(text("SELECT attempts, max_attempts FROM jobs WHERE id=:i"), {"i": jid}).one()
    assert c1.max_attempts == c1.attempts + GRANT  # cycle 1: 1 + 5

    # exhaust the granted budget (simulated: counter reaches the ceiling), dead-letter again, and
    # re-FAIL the run the way the worker's dead-letter path would
    with uow(session_factory) as s:
        assert jobs.claim(s, ["run_transition"], "worker-b", 120) is not None
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET attempts = max_attempts, "
                       "lease_expires_at = now() - interval '1 second' WHERE id=:i"), {"i": jid})
    with uow(session_factory) as s:
        jobs.reap_expired(s)
    with uow(session_factory) as s:
        s.execute(text("UPDATE runs SET state='FAILED' WHERE id='rc-r'"))
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
    assert _job_row(session_factory, jid).status == "dead"


# ── R10-F1: recovery is a CASE-ORDERING authority ─────────────────────────────────────────────────
def test_recovery_refuses_an_old_dead_job_beside_a_running_newer_sibling(session_factory, clean_db):
    """The audit's double-claim witness: old dead job 1 + newer running job 2 for the same case —
    recovering job 1 made claim() hand it out too (it IS the case minimum), two nonces running at
    once. Recovery must refuse and the case must end this test with exactly ONE running row."""
    old = _dead_job(session_factory, case_id="co", run_id="co-r1")
    _seed_case_run(session_factory, "co", "co-r2", seq=2)
    with uow(session_factory) as s:
        jobs.enqueue(s, "run_transition", {"run_id": "co-r2"}, case_id="co")
    with uow(session_factory) as s:
        assert jobs.claim(s, ["run_transition"], "worker-new", 120) is not None  # newer job runs

    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, old, attempt_grant=GRANT)
    assert exc.value.status_code == 409 and "recalculate.requested" in exc.value.detail
    with session_factory() as s:
        running = s.execute(
            text("SELECT count(*) FROM jobs WHERE case_id='co' AND status='running'")
        ).scalar_one()
    assert running == 1  # never a second concurrent claim for the case
    assert _job_row(session_factory, old).status == "dead"


def test_recovery_refuses_when_any_newer_sibling_exists_even_done(session_factory, clean_db):
    """A newer COMPLETED job also blocks recovery: replaying the old frozen evidence after newer
    work is retrograde regardless of the sibling's current status — recalc is the remedy."""
    old = _dead_job(session_factory, case_id="cd", run_id="cd-r1")
    _seed_case_run(session_factory, "cd", "cd-r2", seq=2)
    with uow(session_factory) as s:
        jobs.enqueue(s, "run_transition", {"run_id": "cd-r2"}, case_id="cd")
    with uow(session_factory) as s:
        newer = jobs.claim(s, ["run_transition"], "worker-new", 120)
    with uow(session_factory) as s:
        assert jobs.complete(s, newer) is True  # newer work is DONE

    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, old, attempt_grant=GRANT)
    assert exc.value.status_code == 409
    assert _job_row(session_factory, old).status == "dead"


# ── R10-F6: the run reset is BOUND, not fire-and-forget ───────────────────────────────────────────
def test_recovery_refuses_a_job_whose_run_is_complete(session_factory, clean_db):
    """The audit's lie witness: a dead job pointing at a COMPLETE run reported run_reset while
    the run stayed COMPLETE. Now the whole recovery rolls back — the job STAYS DEAD."""
    jid = _dead_job(session_factory, case_id="cc1", run_id="cc1-r", run_state="COMPLETE")
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, jid, attempt_grant=GRANT)
    assert exc.value.status_code == 409
    with session_factory() as s:
        run_state = s.execute(text("SELECT state FROM runs WHERE id='cc1-r'")).scalar_one()
    assert run_state == "COMPLETE"
    assert _job_row(session_factory, jid).status == "dead"  # CAS rolled back with the refusal
    assert _requeued_audits(session_factory) == 0


def test_recovery_cannot_reset_another_cases_run(session_factory, clean_db):
    """The audit's cross-case witness: a dead job under case A naming case B's FAILED run reset
    the victim to QUEUED. The bound UPDATE requires runs.case_id = jobs.case_id."""
    _seed_case_run(session_factory, "victim", "victim-r")  # case B's FAILED run
    jid = _dead_job(session_factory, case_id="ca", run_id="ca-r")
    with uow(session_factory) as s:  # corrupt the payload to point across cases
        s.execute(text("UPDATE jobs SET payload_json = '{\"run_id\": \"victim-r\"}'::jsonb "
                       "WHERE id=:i"), {"i": jid})
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, jid, attempt_grant=GRANT)
    assert exc.value.status_code == 409
    with session_factory() as s:
        victim = s.execute(text("SELECT state FROM runs WHERE id='victim-r'")).scalar_one()
    assert victim == "FAILED"  # the victim run was never touched
    assert _job_row(session_factory, jid).status == "dead"


def test_recovery_refuses_a_missing_run_and_a_payload_without_run_id(session_factory, clean_db):
    ghost = _dead_job(session_factory, case_id="cg", run_id="cg-r")
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET payload_json = '{\"run_id\": \"no-such-run\"}'::jsonb "
                       "WHERE id=:i"), {"i": ghost})
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, ghost, attempt_grant=GRANT)
    assert exc.value.status_code == 409
    assert _job_row(session_factory, ghost).status == "dead"

    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET payload_json = '{}'::jsonb WHERE id=:i"), {"i": ghost})
    with pytest.raises(HTTPException) as exc:
        requeue_dead_job(session_factory, ghost, attempt_grant=GRANT)
    assert exc.value.status_code == 409 and "run_id" in exc.value.detail
    assert _job_row(session_factory, ghost).status == "dead"
