"""Phase 0 acceptance: idempotent ingestion, auth, schema validation."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from tests.conftest import envelope, sign_headers

pytestmark = pytest.mark.postgres

KYB_PAYLOAD = {
    "company_legal_name": "Acme Networks Ltd",
    "jurisdiction": "GB",
    "website": "https://acme.example",
}


def test_double_post_same_key_yields_one_run_and_identical_responses(client, engine, post_event):
    first, key = post_event("case-idem-1", "kyb.run_requested", KYB_PAYLOAD)
    assert first.status_code == 202
    assert first.json()["status"] == "queued"

    replay, _ = post_event("case-idem-1", "kyb.run_requested", KYB_PAYLOAD, key=key)
    assert replay.status_code == 200
    assert replay.json() == first.json()

    with engine.connect() as conn:
        events = conn.execute(text("SELECT count(*) FROM events")).scalar_one()
        runs = conn.execute(text("SELECT count(*) FROM runs")).scalar_one()
    assert events == 1
    assert runs == 1


def test_same_key_different_payload_is_409(client, post_event):
    _, key = post_event("case-idem-2", "kyb.run_requested", KYB_PAYLOAD)
    conflict, _ = post_event(
        "case-idem-2",
        "kyb.run_requested",
        {**KYB_PAYLOAD, "jurisdiction": "US"},
        key=key,
        force_new_body=True,
    )
    assert conflict.status_code == 409
    assert "idempotency" in conflict.json()["error"]


def test_concurrent_duplicate_posts_race(client, engine, clean_db):
    """Architecture risk #2: two simultaneous posts with one key → one event."""
    body = json.dumps(envelope("kyb.run_requested", KYB_PAYLOAD)).encode()
    headers = sign_headers(body, key="race-key-1")

    def post():
        return client.post("/v1/cases/case-race/events", content=body, headers=headers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = list(pool.map(lambda _: post(), range(2)))

    assert {r1.status_code, r2.status_code} <= {200, 202}
    assert r1.json() == r2.json()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM events")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1


def test_schema_violation_is_422(client, clean_db):
    body = json.dumps(
        envelope("org_id.submitted", {"rir": "not-a-rir", "org_handle": "X"})
    ).encode()
    response = client.post(
        "/v1/cases/case-422/events", content=body, headers=sign_headers(body)
    )
    assert response.status_code == 422


def test_unknown_event_type_is_422(client, clean_db):
    body = json.dumps(envelope("nonsense.event", {})).encode()
    response = client.post(
        "/v1/cases/case-422b/events", content=body, headers=sign_headers(body)
    )
    assert response.status_code == 422


def test_missing_idempotency_key_is_400(client, clean_db):
    body = json.dumps(envelope("recalculate.requested", {})).encode()
    headers = sign_headers(body)
    del headers["Idempotency-Key"]
    response = client.post("/v1/cases/case-400/events", content=body, headers=headers)
    assert response.status_code == 400


def test_unauthenticated_event_rejected(client, clean_db):
    body = json.dumps(envelope("recalculate.requested", {})).encode()
    headers = sign_headers(body)
    headers["X-KYC-Signature"] = "0" * 64
    response = client.post("/v1/cases/case-401/events", content=body, headers=headers)
    assert response.status_code == 401
