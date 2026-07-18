"""Retiring POST /v1/review-tasks/{id}/complete (PR 5a §4): review completion is
now the keyed `website.review_completed` event, gated by a validation floor that
rejects an invalid task BEFORE any run/check — and rolls back so no orphan event
row survives (the _FloorReject design)."""

import uuid

import pytest
from sqlalchemy import text

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


def test_retired_route_is_gone(client, clean_db):
    assert client.post("/v1/review-tasks/whatever/complete").status_code == 404


def test_valid_completion_accepted(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-ok")
    resp, _ = post_event("case-ok", "website.review_completed", _wrc(task_id))
    assert resp.status_code == 202


def test_nonexistent_task_rejected_and_no_row_persists(client, engine, clean_db, post_event):
    resp, _ = post_event("case-ghost", "website.review_completed", _wrc("does-not-exist"))
    assert resp.status_code == 404
    with engine.connect() as conn:
        events = conn.execute(text("SELECT count(*) FROM events WHERE case_id = 'case-ghost'")).scalar_one()
        runs = conn.execute(text("SELECT count(*) FROM runs WHERE case_id = 'case-ghost'")).scalar_one()
    assert events == 0  # rolled back — no orphan event row
    assert runs == 0


def test_wrong_case_rejected(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-owner")
    resp, _ = post_event("case-other", "website.review_completed", _wrc(task_id))
    assert resp.status_code == 409


def test_wrong_type_rejected(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-poc", task_type="poc_email_unavailable")
    resp, _ = post_event("case-poc", "website.review_completed", _wrc(task_id))
    assert resp.status_code == 422


def test_closed_task_rejected(client, engine, clean_db, post_event):
    task_id = _seed_task(engine, "case-closed", status="done")
    resp, _ = post_event("case-closed", "website.review_completed", _wrc(task_id))
    assert resp.status_code == 409
