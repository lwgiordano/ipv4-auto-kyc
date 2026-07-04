"""Phase 0 acceptance: a run walks QUEUED→COMPLETE with a no-op adapter set,
and the decision callback fires with a well-formed body."""

import pytest
from sqlalchemy import text

from kyc_tool.api.schemas import DecisionCallback

pytestmark = pytest.mark.postgres


def test_run_walks_to_complete_and_callback_is_well_formed(
    client, engine, post_event, worker, publisher, callback_capture
):
    response, _ = post_event(
        "case-walk-1",
        "kyb.run_requested",
        {"company_legal_name": "Walk Co", "jurisdiction": "GB"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    processed = worker.run_until_idle()
    assert processed >= 1

    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["state"] == "PUBLISH_DECISION"

    delivered = publisher.process_pending()
    assert delivered == 1
    assert len(callback_capture.requests) == 1
    request = callback_capture.requests[0]
    assert request["url"].endswith("/kyc/decision")
    assert "x-kyc-signature" in request["headers"]

    body = DecisionCallback.model_validate(request["body"])
    assert body.case_id == "case-walk-1"
    assert body.run_id == run_id
    # no evidence, no checks → the catch-all holding decision at score 0
    assert body.decision == "manual_review_insufficient"
    assert body.score == 0
    assert body.gates.score_met is False
    assert body.gates.broker_ok is True  # clear by default
    assert body.gates.no_hard_conflict is True

    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["state"] == "COMPLETE"

    case = client.get("/v1/cases/case-walk-1").json()
    assert case["latest_decision"] == "manual_review_insufficient"
    assert case["status"] == "manual_review_insufficient"

    with engine.connect() as conn:
        stages = [
            r.action
            for r in conn.execute(
                text("SELECT action FROM audit_log WHERE case_id='case-walk-1' ORDER BY id")
            )
        ]
    # audit reconstructs the full walk, including the fused logical stages
    assert "event.received" in stages
    assert "run.created" in stages
    assert "run.broker_gate" in stages
    assert "run.decided" in stages


def test_replayed_event_does_not_rerun_pipeline(client, engine, post_event, worker, publisher):
    response, key = post_event(
        "case-walk-2",
        "kyb.run_requested",
        {"company_legal_name": "Walk2 Co"},
    )
    run_id = response.json()["run_id"]
    worker.run_until_idle()
    publisher.process_pending()

    replay, _ = post_event(
        "case-walk-2", "kyb.run_requested", {"company_legal_name": "Walk2 Co"}, key=key
    )
    assert replay.status_code == 200
    assert replay.json()["run_id"] == run_id

    worker.run_until_idle()
    with engine.connect() as conn:
        runs = conn.execute(text("SELECT count(*) FROM runs")).scalar_one()
        callbacks = conn.execute(text("SELECT count(*) FROM outbox")).scalar_one()
    assert runs == 1
    assert callbacks == 1


def test_recalculate_event_walks_without_adapters(client, post_event, worker, publisher, callback_capture):
    post_event("case-walk-3", "recalculate.requested", {})
    worker.run_until_idle()
    publisher.process_pending()
    assert len(callback_capture.requests) == 1
    assert callback_capture.requests[0]["body"]["decision"] == "manual_review_insufficient"
