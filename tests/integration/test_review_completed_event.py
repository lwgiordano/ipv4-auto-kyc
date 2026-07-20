"""Retiring POST /v1/review-tasks/{id}/complete (PR 5a §4): review completion is
now the keyed `website.review_completed` event, gated by a validation floor that
rejects an invalid task BEFORE any run/check — and rolls back so no orphan event
row survives (the _FloorReject design).

PR 5b adds the AUTHORITATIVE decide-txn guard
(`review_guard.evaluate_website_completion`): the ingest floor above is a fast
reject at write time, but the guard re-validates the PERSISTED event's actor
inside the decide txn, under the task's `FOR UPDATE` lock, so an event that was
queued before this deploy — and so never saw the floor — still can't drain
through unchecked.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from kyc_tool.checkstore import repo as checkstore
from kyc_tool.db.tables import AuditLog, DecisionRow, ReviewTask
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue import jobs
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore

pytestmark = pytest.mark.postgres


def _seed_task(engine, case_id: str, *, task_type: str = "website", status: str = "open") -> str:
    task_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        conn.execute(
            text("INSERT INTO review_tasks (id, case_id, task_type, status) VALUES (:id, :c, :tt, :st)"),
            {"id": task_id, "c": case_id, "tt": task_type, "st": status},
        )
    return task_id


def _wrc(task_id: str, *, result: str = "pass", reviewer: str = "rev-1") -> dict:
    return {"task_id": task_id, "result": result, "reviewer_id": reviewer}


def _seed_blocked_broker(engine, case_id: str) -> None:
    """Seed (or update) a case directly as broker-BLOCKED, bypassing the fuzzy
    BrokerGate matcher entirely. `_worker_for` below builds a Pipeline with no
    `broker_matcher`, so `_broker_gate` re-uses this stored status rather than
    re-matching it — the run then short-circuits straight to DECIDE."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, broker_status) VALUES (:c, 'blocked') "
                "ON CONFLICT (id) DO UPDATE SET broker_status='blocked'"
            ),
            {"c": case_id},
        )


def test_retired_route_is_gone(client, clean_db):
    assert client.post("/v1/review-tasks/whatever/complete").status_code == 404


_REVIEWER = {"type": "reviewer", "id": "rev-1"}  # matches _wrc's default reviewer="rev-1"


def test_valid_completion_accepted(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-ok")
    resp, _ = post_event("case-ok", "website.review_completed", _wrc(task_id), actor=_REVIEWER)
    assert resp.status_code == 202


def test_nonexistent_task_rejected_and_no_row_persists(client, engine, clean_db, post_event):
    resp, _ = post_event(
        "case-ghost", "website.review_completed", _wrc("does-not-exist"), actor=_REVIEWER
    )
    assert resp.status_code == 404
    with engine.connect() as conn:
        events = conn.execute(text("SELECT count(*) FROM events WHERE case_id = 'case-ghost'")).scalar_one()
        runs = conn.execute(text("SELECT count(*) FROM runs WHERE case_id = 'case-ghost'")).scalar_one()
    assert events == 0  # rolled back — no orphan event row
    assert runs == 0


def test_wrong_case_rejected(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-owner")
    resp, _ = post_event("case-other", "website.review_completed", _wrc(task_id), actor=_REVIEWER)
    assert resp.status_code == 409


def test_wrong_type_rejected(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-poc", task_type="poc_email_unavailable")
    resp, _ = post_event(
        "case-poc", "website.review_completed", _wrc(task_id), actor=_REVIEWER
    )
    assert resp.status_code == 422


def test_closed_task_rejected(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-closed", status="done")
    resp, _ = post_event("case-closed", "website.review_completed", _wrc(task_id), actor=_REVIEWER)
    assert resp.status_code == 409


def test_wrong_actor_type_rejected_422(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-a")  # existing helper; task_type=website, open
    resp, _ = post_event(
        "case-a", "website.review_completed", _wrc(task_id, reviewer="rev-1"),
        actor={"type": "system", "id": "rev-1"},
    )
    assert resp.status_code == 422


def test_actor_id_mismatch_rejected_422(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-a")
    resp, _ = post_event(
        "case-a", "website.review_completed", _wrc(task_id, reviewer="rev-1"),
        actor={"type": "reviewer", "id": "someone-else"},
    )
    assert resp.status_code == 422


def test_valid_reviewer_actor_accepted(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-a")
    resp, _ = post_event(
        "case-a", "website.review_completed", _wrc(task_id, reviewer="rev-1"),
        actor={"type": "reviewer", "id": "rev-1"},
    )
    assert resp.status_code == 202


# --- PR 5b: authoritative decide-txn guard ----------------------------------
#
# The tests above exercise the ingest-time floor only. The tests below drive
# an event all the way through the decide txn, so they need a worker — built
# here rather than reused from tests/conftest.py's `worker`/`pipeline`
# fixtures because _seed_completion_run bypasses post_event/client entirely
# (it seeds the event+run rows directly, as a PR 5a-era write path would have
# left them), mirroring the phase2/phase3 `*_worker.run_until_idle()` pattern
# used throughout tests/integration/test_phase2_adapters.py.


def _seed_completion_run(engine, case_id: str, task_id: str, *, actor: dict, payload: dict) -> str:
    """Insert a website.review_completed event + run + queued run_transition
    job DIRECTLY — bypassing ingest_event's reviewer-actor floor entirely —
    reproducing an event admitted under PR 5a (before the floor existed) that
    only now reaches the decide-txn guard added in PR 5b. Lazily creates the
    case row (AUDIT:C1 semantics) so `case_id` need not equal `task_id`'s
    owning case — the wrong_case skip_reason test drives a run whose case
    never had a task of its own."""
    event_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
        seq = conn.execute(
            text(
                "UPDATE cases SET event_sequence = event_sequence + 1 "
                "WHERE id=:c RETURNING event_sequence"
            ),
            {"c": case_id},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, "
                "event_type, actor_json, payload_json, event_sequence, run_id) "
                "VALUES (:id, :c, :key, 'h', 'website.review_completed', "
                "CAST(:actor AS jsonb), CAST(:payload AS jsonb), :seq, :run_id)"
            ),
            {
                "id": event_id,
                "c": case_id,
                "key": uuid.uuid4().hex,
                "seq": seq,
                "actor": json.dumps(actor),
                "payload": json.dumps(payload),
                "run_id": run_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, "
                "input_snapshot_json, state) "
                "VALUES (:id, :c, :eid, CAST('{}' AS jsonb), 'QUEUED')"
            ),
            {"id": run_id, "c": case_id, "eid": event_id},
        )
        conn.execute(
            text(
                "INSERT INTO jobs (kind, case_id, payload_json) "
                "VALUES ('run_transition', :c, CAST(:payload AS jsonb))"
            ),
            {"c": case_id, "payload": json.dumps({"run_id": run_id})},
        )
    return run_id


def _worker_for(session_factory, policy, settings, tmp_path) -> Worker:
    pipeline = Pipeline(
        session_factory, policy, FsStore(tmp_path / "evidence"), settings, adapters={}
    )
    return Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
    )


def _drive_run(session_factory, policy, settings, tmp_path, run_id: str) -> None:
    """Run the pipeline worker to completion for a run seeded directly via
    _seed_completion_run (not through post_event)."""
    _worker_for(session_factory, policy, settings, tmp_path).run_until_idle()


def _force_deadletter_first_job(engine, case_id: str) -> None:
    """Simulate a worker crash that burned through every attempt on the
    earliest queued run_transition job for this case: mark it 'running' with
    an already-expired lease and attempts at the cap, then let the real
    `jobs.reap_expired` — the exact function the live worker's idle path
    calls — dead-letter it. The job never reaches the handler, so it never
    touches the task; the FIFO claim predicate only blocks a case's later job
    while the earlier one is queued/running (queue/jobs.py), so once this one
    is 'dead' the next job is immediately claimable."""
    with Session(engine) as session:
        job_id = session.execute(
            text(
                "SELECT id FROM jobs WHERE case_id=:c AND kind='run_transition' "
                "AND status='queued' ORDER BY id ASC LIMIT 1"
            ),
            {"c": case_id},
        ).scalar_one()
        session.execute(
            text(
                "UPDATE jobs SET status='running', attempts=max_attempts, "
                "lease_expires_at = now() - interval '1 second' WHERE id=:id"
            ),
            {"id": job_id},
        )
        session.commit()
        jobs.reap_expired(session)
        session.commit()


def _drain(session_factory, policy, settings, tmp_path, case_id: str) -> None:
    """Run the pipeline worker to completion for a run created through the
    real post_event/ingest path."""
    _worker_for(session_factory, policy, settings, tmp_path).run_until_idle()


def _live_check(session, case_id: str, check_type: str):
    return next(
        (c for c in checkstore.live_checks(session, case_id) if c.check_type == check_type),
        None,
    )


def _audit_reason(session, case_id: str, action: str) -> str | None:
    row = session.execute(
        select(AuditLog)
        .where(AuditLog.case_id == case_id, AuditLog.action == action)
        .order_by(AuditLog.id.desc())
    ).scalars().first()
    return (row.detail_json or {}).get("reason") if row is not None else None


def _latest_decision(session, case_id: str) -> DecisionRow | None:
    return session.execute(
        select(DecisionRow)
        .where(DecisionRow.case_id == case_id)
        .order_by(DecisionRow.decided_at.desc())
    ).scalars().first()


def test_pre_upgrade_bad_actor_skipped_at_decide(
    engine, clean_db, session_factory, policy, settings, tmp_path
):
    """A website.review_completed admitted UNDER PR 5a (no actor floor) with a
    system actor must be SKIPPED by the decide-txn guard: no check, task stays
    open, completion_skipped audited, run still completes."""
    task_id = _seed_task(engine, "case-x")
    # seed the event+run directly, bypassing the floor (as a PR 5a-era API would)
    run_id = _seed_completion_run(engine, "case-x", task_id,
                                  actor={"type": "system", "id": "sys"},
                                  payload={"task_id": task_id, "result": "pass", "reviewer_id": "sys"})
    _drive_run(session_factory, policy, settings, tmp_path, run_id)  # run the worker to completion
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "open"                         # not closed
        assert _live_check(s, "case-x", "website_verified") is None   # no +10
        assert _audit_reason(s, "case-x", "review_task.completion_skipped") == "actor_invalid"


def test_pre_upgrade_wrong_type_skipped_at_decide(
    engine, clean_db, session_factory, policy, settings, tmp_path
):
    """A pre-upgrade completion whose task_id resolves to a non-website task
    must be SKIPPED by the decide-txn guard with wrong_type: no check, the
    (non-website) task is left exactly as seeded, completion_skipped audited."""
    task_id = _seed_task(engine, "case-y", task_type="poc_email_unavailable")
    run_id = _seed_completion_run(engine, "case-y", task_id,
                                  actor={"type": "reviewer", "id": "rev-1"},
                                  payload={"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"})
    _drive_run(session_factory, policy, settings, tmp_path, run_id)
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "open" and task.task_type == "poc_email_unavailable"
        assert _live_check(s, "case-y", "website_verified") is None   # no +10
        assert _audit_reason(s, "case-y", "review_task.completion_skipped") == "wrong_type"


def test_pre_upgrade_wrong_case_skipped_at_decide(
    engine, clean_db, session_factory, policy, settings, tmp_path
):
    """A pre-upgrade completion whose task belongs to a DIFFERENT case must be
    SKIPPED by the decide-txn guard with wrong_case: no check, the task (still
    owned by its real case) is left open, completion_skipped audited against
    the run's (wrong) case."""
    task_id = _seed_task(engine, "case-owner")
    run_id = _seed_completion_run(engine, "case-other", task_id,
                                  actor={"type": "reviewer", "id": "rev-1"},
                                  payload={"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"})
    _drive_run(session_factory, policy, settings, tmp_path, run_id)
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "open" and task.case_id == "case-owner"
        assert _live_check(s, "case-other", "website_verified") is None   # no +10
        assert _audit_reason(s, "case-other", "review_task.completion_skipped") == "wrong_case"


def test_persists_actor_derived_reviewer_not_payload(
    engine, clean_db, session_factory, policy, settings, tmp_path, post_event
):
    task_id = _seed_task(engine, "case-p")
    # actor.id and payload.reviewer_id are the SAME raw string, WITH trailing
    # whitespace — both pass the floor's stripped-equality check
    # (reviewer_actor_reason strips both sides before comparing). The guard's
    # actor-derived value is ALSO `.strip()`ped (review_guard.py:
    # `str(actor.get("id")).strip()`), so the stored value must be "rev-9"
    # with no trailing space. A regression that persisted the raw payload
    # field (`event_payload["reviewer_id"]`, never stripped) instead of the
    # guard's reviewer_id would store "rev-9 " and fail this assertion — a
    # bare `== "rev-9"` on an unpadded id would pass either way and prove
    # nothing about which source was actually used.
    post_event("case-p", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-9 "},
               actor={"type": "reviewer", "id": "rev-9 "})
    _drain(session_factory, policy, settings, tmp_path, "case-p")
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "done" and task.reviewer_id == "rev-9"
        chk = _live_check(s, "case-p", "website_verified")
        assert chk.source == "reviewer:rev-9"
        # automatic decision row is NOT a manual row
        dec = _latest_decision(s, "case-p")
        assert dec.manual is False and dec.reviewer_id is None


# --- PR 5b Task 3: broker-blocked short-circuit + concurrency/dead-letter ---


def test_blocked_broker_completion_closes_task_and_writes_check(
    engine, clean_db, session_factory, policy, settings, tmp_path, post_event
):
    """A website.review_completed on a broker-BLOCKED case short-circuits
    BROKER_GATE straight to DECIDE (no VALIDATE stage). The eligible
    completion must still close the task AND write the +10 check atomically
    with the decide txn, even though the decision stays reject."""
    _seed_blocked_broker(engine, "case-b")  # put case on the blocklist first
    task_id = _seed_task(engine, "case-b")
    post_event("case-b", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"})
    _drain(session_factory, policy, settings, tmp_path, "case-b")
    with session_factory() as s:
        assert s.get(ReviewTask, task_id).status == "done"  # closed
        assert _live_check(s, "case-b", "website_verified") is not None  # +10 written
        assert _latest_decision(s, "case-b").decision == "reject"  # still blocked


def test_two_completions_one_close_one_skip(
    engine, clean_db, session_factory, policy, settings, tmp_path, post_event
):
    """Two completions admitted (distinct idempotency keys) BEFORE either
    drains: FIFO claims the lower job id first. The first ELIGIBLE decide txn
    that commits wins; the second's guard re-check sees the now-closed task
    and skips without flipping the already-live check."""
    task_id = _seed_task(engine, "case-c")
    post_event("case-c", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"}, key="k1")
    post_event("case-c", "website.review_completed",
               {"task_id": task_id, "result": "fail", "reviewer_id": "rev-2"},
               actor={"type": "reviewer", "id": "rev-2"}, key="k2")
    _drain(session_factory, policy, settings, tmp_path, "case-c")  # FIFO: k1 then k2
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "done" and task.reviewer_id == "rev-1"  # first eligible wins
        chk = _live_check(s, "case-c", "website_verified")
        assert chk.status == "pass"  # not flipped by k2
        assert _audit_reason(s, "case-c", "review_task.completion_skipped") == "task_not_open"


def test_winner_deadletters_then_second_wins(
    engine, clean_db, session_factory, policy, settings, tmp_path, post_event
):
    """FIFO only blocks a case's later job while the earlier one is
    queued/running (queue/jobs.py claim predicate): once the earlier job
    dead-letters, that stops. seq-1 never reaches the handler (so it never
    touches the task); seq-2 legitimately claims and closes it."""
    task_id = _seed_task(engine, "case-d")
    post_event("case-d", "website.review_completed",
               {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
               actor={"type": "reviewer", "id": "rev-1"}, key="k1")
    post_event("case-d", "website.review_completed",
               {"task_id": task_id, "result": "fail", "reviewer_id": "rev-2"},
               actor={"type": "reviewer", "id": "rev-2"}, key="k2")
    _force_deadletter_first_job(engine, "case-d")  # set attempts>=max on seq-1's job
    _drain(session_factory, policy, settings, tmp_path, "case-d")
    with session_factory() as s:
        task = s.get(ReviewTask, task_id)
        assert task.status == "done" and task.result == "fail"  # seq-2 legitimately closed it
