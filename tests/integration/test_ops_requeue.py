"""Re-audit `f2929f8..6a4cd87` F3: the dead-letter recovery path exists in the SECURE production
configuration — always-mounted /v1/ops requeue endpoints behind the operator bearer token, sharing
the console's transactional service (UI disabled keeps /ui 404 while recovery still works)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from kyc_tool.api.app import create_app

pytestmark = pytest.mark.postgres

_TOKEN = "t" * 32


def _app(settings, session_factory, policy):
    hardened = settings.model_copy(update={"ui_enabled": False, "ui_admin_token": _TOKEN})
    return TestClient(create_app(hardened, session_factory=session_factory, policy=policy))


def _seed_dead_job(session_factory):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('rq')"))
        s.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('rq-e','rq','rq-k','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        s.execute(text(
            "INSERT INTO runs (id, case_id, triggering_event_id, state, error) "
            "VALUES ('rq-r','rq','rq-e','FAILED','boom')"))
        job_id = s.execute(text(
            "INSERT INTO jobs (kind, case_id, status, payload_json, max_attempts) VALUES "
            "('run_transition','rq','dead','{\"run_id\": \"rq-r\"}'::jsonb, 5) RETURNING id"
        )).scalar_one()
        s.commit()
    return job_id


def test_ops_requeue_works_with_the_ui_disabled(settings, session_factory, policy, clean_db):
    job_id = _seed_dead_job(session_factory)
    tc = _app(settings, session_factory, policy)
    assert tc.post(f"/ui/api/requeue/job/{job_id}").status_code == 404  # console absent

    r = tc.post(f"/v1/ops/requeue/job/{job_id}",
                headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 200
    # attempts_granted = the fixed bounded recovery grant (R9-F4), default 5
    assert r.json() == {"requeued": job_id, "run_reset": "rq-r", "attempts_granted": 5}
    with session_factory() as s:
        job = s.execute(text("SELECT status, attempts FROM jobs WHERE id=:i"), {"i": job_id}).one()
        run = s.execute(text("SELECT state, error FROM runs WHERE id='rq-r'")).one()
        audited = s.execute(text(
            "SELECT count(*) FROM audit_log WHERE action='job.requeued'")).scalar_one()
    assert (job.status, job.attempts) == ("queued", 0)
    assert (run.state, run.error) == ("QUEUED", None)  # atomic run reset
    assert audited == 1


def test_ops_requeue_refuses_a_bad_token(settings, session_factory, policy, clean_db):
    job_id = _seed_dead_job(session_factory)
    tc = _app(settings, session_factory, policy)
    assert tc.post(f"/v1/ops/requeue/job/{job_id}",
                   headers={"Authorization": "Bearer wrong"}).status_code == 401
    with session_factory() as s:
        assert s.execute(text("SELECT status FROM jobs WHERE id=:i"), {"i": job_id}).scalar_one() == "dead"


def test_ops_requeue_preserves_the_redaction_409(settings, session_factory, policy, clean_db):
    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('rx')"))
        outbox_id = s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, status, payload_json) VALUES "
            "('poc_email','rx','email','dead','{\"redacted\": true}'::jsonb) RETURNING id"
        )).scalar_one()
        s.commit()
    tc = _app(settings, session_factory, policy)
    r = tc.post(f"/v1/ops/requeue/outbox/{outbox_id}",
                headers={"Authorization": f"Bearer {_TOKEN}"})
    assert r.status_code == 409 and "redacted" in r.json()["detail"]


def test_ops_requeue_routes_are_mounted_with_the_ui_disabled(settings, session_factory, policy):
    """F14: executable route semantics — with the UI disabled, the ops requeue routes ANSWER (401
    for a bad token, not 404-absent) while the /ui variants are absent (404)."""
    tc = _app(settings, session_factory, policy)
    assert tc.post("/v1/ops/requeue/job/1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.post("/v1/ops/requeue/outbox/1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.post("/ui/api/requeue/job/1").status_code == 404
    assert tc.post("/ui/api/requeue/outbox/1").status_code == 404
