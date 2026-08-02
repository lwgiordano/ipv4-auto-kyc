"""Re-audit `5b0f0b8..b75a320` R4-F3: the queue's open time domains. A nonpositive/overflowing lease
defeats per-case serialization or raises DatetimeFieldOverflow at claim; a negative/oversized backoff
overflowed at fail; a bad poll kills the worker; a nonpositive token TTL mints an expired token. Every
invalid case now fails closed and leaves durable state unchanged; the real DB sinks are driven."""

import pytest
from sqlalchemy import text

from kyc_tool.db.session import uow
from kyc_tool.queue import jobs
from kyc_tool.queue.worker import Worker

pytestmark = pytest.mark.postgres


def _enqueue(session_factory, *, case_id="c", kind="run_transition"):
    with uow(session_factory) as s:
        return jobs.enqueue(s, kind, {}, case_id=case_id).id


@pytest.mark.parametrize("bad_lease", [-1, 0, 2_592_001, 10**20])
def test_claim_refuses_out_of_domain_lease_without_touching_the_job(
    session_factory, clean_db, bad_lease
):
    _enqueue(session_factory)
    with uow(session_factory) as s, pytest.raises(ValueError):
        jobs.claim(s, ["run_transition"], "w", bad_lease)
    with session_factory() as s:
        st = s.execute(text("SELECT status, attempts FROM jobs")).one()
    assert (st.status, st.attempts) == ("queued", 0)  # not claimed, no attempt burned


def test_two_workers_cannot_both_run_the_same_case_job(session_factory, clean_db):
    """Per-case serialization holds with a VALID lease: while A holds the claim, B claims nothing for
    the same case. The reported double-claim came from a -1 lease minting an already-expired claim
    that the reaper immediately requeued — now impossible, the lease floor is 1."""
    _enqueue(session_factory, case_id="cx")
    with uow(session_factory) as sa:
        a = jobs.claim(sa, ["run_transition"], "worker-a", 120)
    assert a is not None
    with uow(session_factory) as sb:
        b = jobs.claim(sb, ["run_transition"], "worker-b", 120)
    assert b is None  # serialized while A's claim is live


def test_fail_refuses_a_negative_base_leaving_the_job_unchanged(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        claimed = jobs.claim(s, ["run_transition"], "w", 120)
    with uow(session_factory) as s, pytest.raises(ValueError):
        jobs.fail(s, claimed, "boom", -1)
    with session_factory() as s:
        st = s.execute(text("SELECT status FROM jobs")).one()
    assert st.status == "running"  # unchanged; the reaper handles it, no immediate no-throttle requeue


def test_fail_at_a_high_attempt_count_requeues_without_overflow(session_factory, clean_db):
    _enqueue(session_factory)
    with uow(session_factory) as s:
        claimed = jobs.claim(s, ["run_transition"], "w", 120)
    high = jobs.ClaimedJob(
        id=claimed.id, kind=claimed.kind, case_id=claimed.case_id,
        payload={}, attempts=63, max_attempts=100,
    )
    with uow(session_factory) as s:
        assert jobs.fail(s, high, "boom", 10) is False  # requeued (no DatetimeFieldOverflow)
    with session_factory() as s:
        st = s.execute(text("SELECT status, run_after > now() AS future FROM jobs")).one()
    assert st.status == "queued" and st.future is True


@pytest.mark.parametrize("bad_poll", [-1, 0, float("nan"), float("inf")])
def test_worker_refuses_a_bad_poll_at_construction(session_factory, bad_poll):
    with pytest.raises(ValueError):
        Worker(session_factory, {"run_transition": lambda j: None}, poll_seconds=bad_poll)


def _seed_case_and_run(session_factory, case_id):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES (:c)"), {"c": case_id})
        s.execute(
            text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                 "actor_json, payload_json, event_sequence) VALUES "
                 "(:e, :c, :k, 'h', 'x', '{}'::jsonb, '{}'::jsonb, 1)"),
            {"e": f"e-{case_id}", "c": case_id, "k": f"k-{case_id}"},
        )
        s.execute(
            text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                 "VALUES (:r, :c, :e, 'PUBLISH_DECISION')"),
            {"r": f"r-{case_id}", "c": case_id, "e": f"e-{case_id}"},
        )
        s.commit()


_NORMALIZED = {
    "send_token": True, "poc_handle": "PH", "rir": "arin",
    "org_handle": "ORG", "resource": "RES", "rir_listed_email": "a@x",
}


def test_poc_mint_refuses_a_nonpositive_ttl_without_minting_or_emailing(
    session_factory, clean_db, settings
):
    from kyc_tool.db.tables import Case, Run
    from kyc_tool.orchestration.side_effects import SideEffects

    _seed_case_and_run(session_factory, "pt")
    se = SideEffects(settings.model_copy(update={"poc_token_ttl_hours": -1}))
    with session_factory() as s:
        case, run = s.get(Case, "pt"), s.get(Run, "r-pt")
        with pytest.raises(ValueError):
            se._poc_effects(s, run, case, dict(_NORMALIZED))
        s.rollback()
    with session_factory() as s:
        tokens = s.execute(text("SELECT count(*) FROM poc_tokens WHERE case_id='pt'")).scalar_one()
        emails = s.execute(
            text("SELECT count(*) FROM outbox WHERE case_id='pt' AND kind='poc_email'")
        ).scalar_one()
    assert (tokens, emails) == (0, 0)  # nothing minted, nothing emailed


def test_poc_mint_with_a_valid_ttl_sets_a_future_expiry(session_factory, clean_db, settings):
    from kyc_tool.db.tables import Case, Run
    from kyc_tool.orchestration.side_effects import SideEffects

    _seed_case_and_run(session_factory, "pv")
    se = SideEffects(settings.model_copy(update={"poc_token_ttl_hours": 72}))
    with session_factory() as s:
        case, run = s.get(Case, "pv"), s.get(Run, "r-pv")
        se._poc_effects(s, run, case, dict(_NORMALIZED))
        s.commit()
    with session_factory() as s:
        future = s.execute(
            text("SELECT expired_at > now() AS f FROM poc_tokens WHERE case_id='pv'")
        ).scalar_one()
    assert future is True
