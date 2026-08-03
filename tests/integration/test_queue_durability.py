"""PR 7a migration-free slice: claim-generation fencing + lease heartbeat. `jobs.attempts`
increments on every claim, so it IS the claim generation: a stale worker (lease expired, reaped,
reclaimed) matches zero rows on complete/fail/heartbeat and can commit nothing, while the live
claim is never clobbered. The heartbeat keeps a legitimately-slow handler's claim alive so the
reaper cannot requeue it mid-execution (the long-job double-execution residual)."""

import time

import pytest
from sqlalchemy import text

from kyc_tool.db.session import uow
from kyc_tool.queue import jobs
from kyc_tool.queue.worker import Worker

pytestmark = pytest.mark.postgres


def _enqueue(session_factory):
    with uow(session_factory) as s:
        return jobs.enqueue(s, "run_transition", {}, case_id="dur").id


def _expire_and_reap(session_factory):
    with uow(session_factory) as s:
        s.execute(text("UPDATE jobs SET lease_expires_at = now() - interval '1 second'"))
    with uow(session_factory) as s:
        jobs.reap_expired(s)


def test_stale_worker_completes_nothing_and_live_claim_wins(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)
    _expire_and_reap(session_factory)
    with uow(session_factory) as s:
        live = jobs.claim(s, ["run_transition"], "worker-b", 120)
    assert live.attempts == stale.attempts + 1  # a new claim generation

    with uow(session_factory) as s:
        assert jobs.complete(s, stale) is False  # stale generation commits nothing
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs")).scalar_one() == "running"  # live untouched
    with uow(session_factory) as s:
        assert jobs.complete(s, live) is True
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs")).scalar_one() == "done"


def test_stale_fail_cannot_clobber_the_live_claim(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)
    _expire_and_reap(session_factory)
    with uow(session_factory) as s:
        jobs.claim(s, ["run_transition"], "worker-b", 120)

    with uow(session_factory) as s:
        jobs.fail(s, stale, "stale boom", 5)  # fenced: requeues nothing
    with session_factory() as s:
        st = s.execute(text("SELECT status, last_error FROM jobs")).one()
    assert st.status == "running" and st.last_error is None  # live claim intact


def test_stale_heartbeat_reports_lost(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        stale = jobs.claim(s, ["run_transition"], "worker-a", 120)
    _expire_and_reap(session_factory)
    with uow(session_factory) as s:
        jobs.claim(s, ["run_transition"], "worker-b", 120)
    with uow(session_factory) as s:
        assert jobs.heartbeat(s, stale, 120) is False


def test_heartbeat_keeps_a_slow_handler_alive_past_its_lease(session_factory, clean_db):
    """The residual itself: lease 2s, handler sleeps 5s. Without the heartbeat the reaper requeues
    mid-execution and a second claim double-executes; with it, the mid-run reap is a no-op and the
    job completes exactly once."""
    _enqueue(session_factory)
    reap_results = []

    def slow_handler(job):
        for _ in range(2):
            time.sleep(2.5)
            with uow(session_factory) as s:  # the reaper runs WHILE the handler is executing
                jobs.reap_expired(s)
                reap_results.append(
                    s.execute(text("SELECT status FROM jobs")).scalar_one()
                )

    worker = Worker(
        session_factory, {"run_transition": slow_handler},
        lease_seconds=2, backoff_base_seconds=0, poll_seconds=0.05,
    )
    assert worker.run_once() is True
    assert reap_results == ["running", "running"]  # never reaped mid-run — heartbeat held the lease
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM jobs")).one()
    assert (st.status, st.attempts) == ("done", 1)  # exactly one execution
