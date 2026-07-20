"""Phase 4 acceptance: callback delivery semantics (retry/backoff, dedupe-key
stability), and the full staging scenario G3 → G7a → G7b with website review
and manual-approve interplay."""

import json

import httpx
import pytest
from sqlalchemy import text

from kyc_tool.outbox.publisher import OutboxPublisher
from tests.conftest import envelope
from tests.integration.shared import ACME_KYB_WITH_CONTACT

pytestmark = pytest.mark.postgres


class FlakyPlatform:
    """Fails the first N deliveries with 500, then accepts."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if len(self.requests) <= self.failures:
            return httpx.Response(500)
        return httpx.Response(200)


def test_callback_retries_on_5xx_then_delivers(client, engine, post_event, worker, session_factory, settings):
    platform = FlakyPlatform(failures=2)
    retry_settings = settings.model_copy(update={"outbox_backoff_base_seconds": 0})
    publisher = OutboxPublisher(
        session_factory,
        retry_settings,
        http_client=httpx.Client(transport=httpx.MockTransport(platform.handler)),
    )
    response, _ = post_event("case-retry", "recalculate.requested", {})
    run_id = response.json()["run_id"]
    worker.run_until_idle()

    # attempt 1 (500), attempt 2 (500), attempt 3 (200)
    for _ in range(3):
        publisher.process_pending()
    assert len(platform.requests) == 3
    assert [r["run_id"] for r in platform.requests] == [run_id] * 3  # stable dedupe key

    with engine.connect() as conn:
        status, attempts = conn.execute(
            text("SELECT status, attempts FROM outbox WHERE run_id=:r"), {"r": run_id}
        ).one()
    assert status == "delivered"
    assert attempts == 2  # two recorded failures before success
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "COMPLETE"


def test_callback_dead_letters_after_max_attempts(
    client, engine, post_event, worker, session_factory, settings
):
    dead_settings = settings.model_copy(update={"outbox_max_attempts": 2, "outbox_backoff_base_seconds": 0})
    platform = FlakyPlatform(failures=99)
    publisher = OutboxPublisher(
        session_factory,
        dead_settings,
        http_client=httpx.Client(transport=httpx.MockTransport(platform.handler)),
    )
    response, _ = post_event("case-dead", "recalculate.requested", {})
    run_id = response.json()["run_id"]
    worker.run_until_idle()

    for _ in range(4):
        publisher.process_pending()

    with engine.connect() as conn:
        status = conn.execute(text("SELECT status FROM outbox WHERE run_id=:r"), {"r": run_id}).scalar_one()
    assert status == "dead"
    # the run stays observable in PUBLISH_DECISION — ops can see the stuck callback
    assert client.get(f"/v1/runs/{run_id}").json()["state"] == "PUBLISH_DECISION"


def test_redelivery_carries_identical_dedupe_key(
    client, engine, post_event, worker, publisher, callback_capture
):
    """At-least-once means duplicates happen; the platform dedupes on
    (case_id, run_id) — both fields must be identical across redeliveries."""
    post_event("case-redeliver", "recalculate.requested", {})
    worker.run_until_idle()
    publisher.process_pending()
    # simulate a crash after delivery but before the delivered-mark landed
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE outbox SET status='pending', next_attempt_at=now() WHERE case_id='case-redeliver'")
        )
    publisher.process_pending()

    bodies = [r["body"] for r in callback_capture.requests]
    assert len(bodies) == 2
    assert bodies[0]["case_id"] == bodies[1]["case_id"]
    assert bodies[0]["run_id"] == bodies[1]["run_id"]
    assert bodies[0]["decision"] == bodies[1]["decision"]


def test_full_staging_scenario_g3_to_approval(
    client, engine, post_event, phase3_worker, publisher, callback_capture, evidence_store, sign
):
    """The acceptance journey: registration-time KYB (insufficient) → evidence
    accumulates through the platform loop (registry, email, website reviewer,
    document) → approve_buy_locked → ORG-ID passes → approve. Every decision
    flows through the callback with the audit trail intact."""
    case_id = "case-staging"

    post_event(case_id, "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    phase3_worker.run_until_idle()
    publisher.process_pending()
    assert callback_capture.requests[-1]["body"]["decision"] == "manual_review_insufficient"

    post_event(
        case_id,
        "email.verified",
        {"email": "ops@acme.example", "domain": "acme.example", "verified_at": "2026-07-04T10:00:00Z"},
    )
    phase3_worker.run_until_idle()
    publisher.process_pending()

    # human completes the website task through the review queue
    tasks = client.get("/v1/review-tasks?status=open").json()["tasks"]
    website_task = next(t for t in tasks if t["case_id"] == case_id and t["task_type"] == "website")
    # review completion is the keyed website.review_completed event (PR 5a §4)
    review_body = json.dumps(
        envelope(
            "website.review_completed",
            {"task_id": website_task["id"], "result": "pass", "reviewer_id": "rev-9"},
            {"type": "reviewer", "id": "rev-9"},
        )
    ).encode()
    client.post(f"/v1/cases/{case_id}/events", content=review_body, headers=sign(review_body))
    phase3_worker.run_until_idle()
    publisher.process_pending()

    doc_ref = evidence_store.put(
        "uploads/staging-cert.json",
        json.dumps(
            {
                "fields": {
                    "name": "ACME NETWORKS LTD",
                    "address": "1 Main Street, London, EC1A 1AA",
                    "number": "12345678",
                    "jurisdiction": "GB",
                }
            }
        ).encode(),
    )
    post_event(case_id, "document.uploaded", {"object_ref": doc_ref, "doc_type": "registration_certificate"})
    phase3_worker.run_until_idle()
    publisher.process_pending()

    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["latest_decision"] == "approve_buy_locked"
    assert case["status"] == "account_approved"
    assert case["buy_status"] == "buy_locked_org_id_required"

    post_event(case_id, "org_id.submitted", {"rir": "arin", "org_handle": "ORG-ACME-1"})
    phase3_worker.run_until_idle()
    publisher.process_pending()

    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["latest_decision"] == "approve"
    assert case["buy_status"] == "buy_enabled"

    decisions = [r["body"]["decision"] for r in callback_capture.requests if r["body"]["case_id"] == case_id]
    assert decisions[0] == "manual_review_insufficient"
    assert decisions[-2:] == ["approve_buy_locked", "approve"]

    # audit trail reconstructs the whole journey
    with engine.connect() as conn:
        actions = [
            r.action
            for r in conn.execute(
                text("SELECT action FROM audit_log WHERE case_id=:c ORDER BY id"), {"c": case_id}
            )
        ]
    for expected in (
        "event.received",
        "run.created",
        "run.broker_gate",
        "adapter.recorded",
        "check.created",
        "review_task.created",
        "review_task.completed",
        "run.decided",
    ):
        assert expected in actions, f"audit trail missing {expected}"


def test_manual_approve_then_org_id_enables_buying(client, post_event, phase3_worker, publisher):
    """Manual approve sticks while buy enablement still tracks ORG-ID: the
    later ORG-ID pass upgrades buying without touching approved_manual."""
    case_id = "case-manual-upgrade"
    post_event(case_id, "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    phase3_worker.run_until_idle()

    response, _ = post_event(
        case_id,
        "reviewer.manual_approve",
        {"reviewer_id": "rev-1", "note": "vip"},
        actor={"type": "reviewer", "id": "rev-1"},
    )
    assert response.json()["case_status"] == "approved_manual"
    assert response.json()["buy_status"] == "buy_locked_org_id_required"

    post_event(case_id, "org_id.submitted", {"rir": "arin", "org_handle": "ORG-ACME-1"})
    phase3_worker.run_until_idle()
    publisher.process_pending()

    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["status"] == "approved_manual"  # sticky — platform enforced it
    assert case["buy_status"] == "buy_enabled"  # ORG-ID unlocked buying


# --- PR 5b final-review fix (spec §8.7): manual-approve actor-floor matrix --
#
# reviewer.manual_approve is enforced by the SAME reviewer_actor_reason floor
# as website.review_completed (events/ingest.py:201-203), but until now only
# the happy path above was tested. These mirror the website matrix in
# test_review_completed_event.py (test_wrong_actor_type_rejected_422 /
# test_actor_id_mismatch_rejected_422). A fresh case_id per case keeps the
# no-rows assertion in test_manual_approve_rejected_leaves_no_rows clean; no
# prior events are needed since manual-approve doesn't require a run.


@pytest.mark.parametrize(
    "case_id, actor, reviewer_id",
    [
        pytest.param(
            "case-manual-bad-type", {"type": "system", "id": "rev-1"}, "rev-1", id="wrong_actor_type"
        ),
        pytest.param(
            "case-manual-bad-mismatch", {"type": "reviewer", "id": "rev-1"}, "rev-2", id="actor_id_mismatch"
        ),
        pytest.param(
            "case-manual-bad-blank", {"type": "reviewer", "id": ""}, "", id="both_blank_equal"
        ),
        pytest.param(
            "case-manual-bad-whitespace", {"type": "reviewer", "id": "  "}, "  ", id="both_whitespace_equal"
        ),
    ],
)
def test_manual_approve_bad_actor_rejected_422(client, post_event, case_id, actor, reviewer_id):
    """actor.type != reviewer, actor.id != payload.reviewer_id, and
    blank/whitespace ids that are EQUAL to each other (still invalid post-strip
    — the blank guard fires independently of the equality check) are all
    rejected 422 by reviewer_actor_reason before any decision is recorded."""
    response, _ = post_event(
        case_id,
        "reviewer.manual_approve",
        {"reviewer_id": reviewer_id, "note": "vip"},
        actor=actor,
    )
    assert response.status_code == 422


def test_manual_approve_rejected_leaves_no_rows(client, engine, post_event):
    """The _FloorReject rolls the WHOLE ingest txn back on a rejected
    manual-approve — no orphan event row, no decision row, no
    reviewer.manual_approve audit entry — mirroring
    test_nonexistent_task_rejected_and_no_row_persists for the website path."""
    case_id = "case-manual-rollback"
    response, _ = post_event(
        case_id,
        "reviewer.manual_approve",
        {"reviewer_id": "rev-2", "note": "vip"},
        actor={"type": "reviewer", "id": "rev-1"},  # id mismatch -> 422
    )
    assert response.status_code == 422
    with engine.connect() as conn:
        events = conn.execute(
            text("SELECT count(*) FROM events WHERE case_id=:c"), {"c": case_id}
        ).scalar_one()
        decisions = conn.execute(
            text("SELECT count(*) FROM decisions WHERE case_id=:c"), {"c": case_id}
        ).scalar_one()
        manual_audits = conn.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE case_id=:c "
                "AND action='reviewer.manual_approve'"
            ),
            {"c": case_id},
        ).scalar_one()
    assert events == 0  # rolled back — no orphan event row
    assert decisions == 0
    assert manual_audits == 0


def test_manual_approve_valid_reviewer_actor_persists_stripped_reviewer_id(client, engine, post_event):
    """Happy path (keep/confirm): a valid reviewer actor still 200s and writes
    the manual decision row with the actor-derived reviewer id.

    FIX 5: actor.id and payload.reviewer_id are the SAME raw string WITH a
    trailing space — both pass the floor (reviewer_actor_reason strips both
    sides before comparing) — so the PERSISTED DecisionRow.reviewer_id must
    ALSO be stripped ("rev-9", not "rev-9 "), matching the website path's
    actor-derived-stripped persistence (review_guard.py:64 vs
    ingest.py:_handle_manual_approve). A bare `== "rev-9"` on an unpadded id
    would prove nothing; the trailing space is the point — mirrors the
    technique in test_persists_actor_derived_reviewer_not_payload."""
    case_id = "case-manual-strip"
    response, _ = post_event(
        case_id,
        "reviewer.manual_approve",
        {"reviewer_id": "rev-9 ", "note": "vip"},
        actor={"type": "reviewer", "id": "rev-9 "},
    )
    assert response.status_code == 200
    assert response.json()["case_status"] == "approved_manual"
    with engine.connect() as conn:
        reviewer_id = conn.execute(
            text(
                "SELECT reviewer_id FROM decisions WHERE case_id=:c "
                "ORDER BY decided_at DESC LIMIT 1"
            ),
            {"c": case_id},
        ).scalar_one()
    assert reviewer_id == "rev-9"  # stripped, not "rev-9 "
