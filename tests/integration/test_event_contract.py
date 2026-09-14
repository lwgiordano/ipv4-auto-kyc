import json

import pytest

from kyc_tool.api.schemas import EVENT_RESPONSE_MODELS
from tests.conftest import envelope, sign_headers
from tests.integration.test_configuration_api import make_client
from tests.integration.test_configuration_repo import baseline

pytestmark = pytest.mark.postgres


def assert_response_contract(client, response, status):
    assert response.status_code == status
    body = response.json()
    model = EVENT_RESPONSE_MODELS[status]
    assert model.model_validate(body).model_dump(mode="json", exclude_none=True) == body
    document = client.app.openapi()
    schema = document["paths"]["/v1/cases/{case_id}/events"]["post"]["responses"][str(status)]["content"][
        "application/json"
    ]["schema"]
    expected_ref = f"#/components/schemas/{model.__name__}"
    assert schema == {"$ref": expected_ref}
    assert document["components"]["schemas"][model.__name__]


def test_openapi_json_preserves_the_in_process_authoritative_event_schema(client, clean_db):
    in_process = client.app.openapi()
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json() == in_process
    kyb_payload = response.json()["components"]["schemas"]["KybRunPayload"]
    assert kyb_payload["properties"]["address"]["default"] is None


def test_actual_200_through_422_bodies_match_the_published_status_models(client, clean_db, post_event):
    queued, key = post_event("contract-queued", "recalculate.requested", {})
    replay, _ = post_event("contract-queued", "recalculate.requested", {}, key=key)
    manual, _ = post_event(
        "contract-manual",
        "reviewer.manual_approve",
        {"reviewer_id": "reviewer-1"},
        actor={"type": "reviewer", "id": "reviewer-1"},
    )

    raw = json.dumps(envelope("recalculate.requested", {})).encode()
    missing_idem_headers = sign_headers(raw)
    missing_idem_headers.pop("Idempotency-Key")
    missing_idem = client.post("/v1/cases/contract-400/events", content=raw, headers=missing_idem_headers)
    bad_signature_headers = sign_headers(raw)
    bad_signature_headers["X-KYC-Signature"] = "0" * 64
    bad_signature = client.post("/v1/cases/contract-401/events", content=raw, headers=bad_signature_headers)
    missing_task, _ = post_event(
        "contract-404",
        "website.review_completed",
        {"task_id": "absent", "result": "pass", "reviewer_id": "reviewer-1"},
        actor={"type": "reviewer", "id": "reviewer-1"},
    )
    _, conflict_key = post_event("contract-409", "recalculate.requested", {})
    conflict, _ = post_event(
        "contract-409",
        "kyb.run_requested",
        {"company_legal_name": "different"},
        key=conflict_key,
        force_new_body=True,
    )
    invalid, _ = post_event(
        "contract-422",
        "org_id.submitted",
        {"rir": "invalid", "org_handle": "ORG-1"},
    )

    assert queued.json().keys() == {"run_id", "status"}
    assert replay.json() == queued.json()
    assert manual.json() == {
        "case_id": "contract-manual",
        "case_status": "approved_manual",
        "buy_status": "buy_locked_org_id_required",
        "recorded": True,
    }
    assert missing_idem.json() == {"detail": "Idempotency-Key header is required"}
    assert bad_signature.json() == {"detail": "invalid signature"}
    assert missing_task.json() == {"error": "review task not found", "task_id": "absent"}
    assert conflict.json()["error"] == "idempotency key reuse with different payload"
    assert set(conflict.json()) == {"error", "event_id"}
    assert isinstance(invalid.json()["detail"], list)

    for response, status in (
        (queued, 202),
        (replay, 200),
        (manual, 200),
        (missing_idem, 400),
        (bad_signature, 401),
        (missing_task, 404),
        (conflict, 409),
        (invalid, 422),
    ):
        assert_response_contract(client, response, status)


def test_actual_503_body_matches_the_published_status_model(settings, session_factory, policy, clean_db):
    baseline(session_factory, policy)
    client = make_client(settings, session_factory, policy, enforce_bundle_pinning=False)
    raw = json.dumps(envelope("recalculate.requested", {})).encode()
    response = client.post("/v1/cases/contract-503/events", content=raw, headers=sign_headers(raw))
    assert response.json() == {
        "error": "configuration_unavailable",
        "detail": "Configuration authority unavailable; retry safely.",
    }
    assert_response_contract(client, response, 503)
