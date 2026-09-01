"""Cutover recovery one-shot (PR 5b §10 step 3): requeues every `running` job
after all pipeline workers are confirmed stopped, without consuming the
forced-stop attempt the claim already counted."""

import pytest
from sqlalchemy import text

from kyc_tool.ops.requeue_interrupted_jobs import requeue_interrupted

pytestmark = pytest.mark.postgres


def _insert_job(session_factory, *, status, attempts, max_attempts=5, lease="now() - interval '1 hour'"):
    with session_factory() as s:
        jid = s.execute(
            text(
                f"INSERT INTO jobs (kind, payload_json, status, attempts, max_attempts, "
                f"run_after, lease_expires_at, created_at, updated_at) "
                f"VALUES ('run_transition', '{{}}'::jsonb, :st, :a, :m, now(), {lease}, now(), now()) "
                "RETURNING id"
            ),
            {"st": status, "a": attempts, "m": max_attempts},
        ).scalar_one()
        s.commit()
    return jid


def test_final_attempt_running_is_requeued_not_deadlettered(session_factory, clean_db):
    jid = _insert_job(session_factory, status="running", attempts=5, max_attempts=5)
    assert requeue_interrupted(session_factory) == 1
    with session_factory() as s:
        row = s.execute(text("SELECT status, attempts FROM jobs WHERE id=:i"), {"i": jid}).one()
        assert row.status == "queued" and row.attempts == 4  # forced-stop attempt returned


def test_unexpired_lease_running_is_requeued(session_factory, clean_db):
    jid = _insert_job(
        session_factory, status="running", attempts=1, lease="now() + interval '1 hour'"
    )  # STILL VALID lease
    assert requeue_interrupted(session_factory) == 1
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": jid}).scalar_one() == "queued"


def test_idempotent_and_zero_running_after(session_factory, clean_db):
    _insert_job(session_factory, status="running", attempts=1)
    requeue_interrupted(session_factory)
    assert requeue_interrupted(session_factory) == 0  # nothing running the 2nd time
    with session_factory() as s:
        assert s.execute(text("SELECT count(*) FROM jobs WHERE status='running'")).scalar_one() == 0
