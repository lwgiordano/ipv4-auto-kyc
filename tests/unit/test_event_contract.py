import json
from typing import get_args

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from kyc_tool.api.routes_events import install_event_contract_openapi, router
from kyc_tool.api.schemas import (
    PAYLOAD_MODELS,
    EventAcceptedResponse,
    EventEnvelope,
    EventFailureResponse,
    EventHttpErrorResponse,
    EventIngestErrorResponse,
    EventType,
    ManualApprovalEventResponse,
    QueuedEventResponse,
)

VALID_PAYLOADS = {
    "kyb.run_requested": {"company_legal_name": "Acme Networks Ltd"},
    "email.verified": {
        "email": "ops@acme.example",
        "domain": "acme.example",
        "verified_at": "2026-09-12T12:00:00Z",
    },
    "org_id.submitted": {"rir": "arin", "org_handle": "ORG-ACME-1"},
    "poc.submitted": {"rir": "arin", "poc_handle": "POC-ACME"},
    "poc.token_verified": {
        "token_id": "token-1",
        "token": "raw-token",
        "verified_at": "2026-09-12T12:00:00Z",
    },
    "document.uploaded": {
        "object_ref": "uploads/acme/doc.json",
        "doc_type": "registration",
    },
    "website.review_completed": {
        "task_id": "task-1",
        "result": "pass",
        "reviewer_id": "reviewer-1",
    },
    "reviewer.manual_approve": {"reviewer_id": "reviewer-1"},
    "recalculate.requested": {},
}


def event(event_type: str, payload: dict) -> dict:
    return {
        "event_type": event_type,
        "occurred_at": "2026-09-12T12:00:00Z",
        "actor": {"type": "system", "id": "techcraft"},
        "payload": payload,
    }


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("event_type", sorted(VALID_PAYLOADS))
def test_real_parser_selects_every_payload_variant(event_type, as_bytes):
    raw = json.dumps(event(event_type, VALID_PAYLOADS[event_type]))
    if as_bytes:
        raw = raw.encode()
    parsed = EventEnvelope.model_validate_json(raw)
    assert type(parsed.root.payload) is PAYLOAD_MODELS[event_type]
    assert parsed.event_type == event_type
    assert parsed.validated_payload() == parsed.payload


def test_union_event_set_stays_closed_and_matches_compatibility_authorities():
    schema = EventEnvelope.model_json_schema()
    assert set(schema["discriminator"]["mapping"]) == set(get_args(EventType))
    assert set(get_args(EventType)) == set(PAYLOAD_MODELS)


def test_event_envelope_compatibility_and_normalization_are_stable():
    source = event("recalculate.requested", {"future_payload_key": "kept"})
    source["actor"]["future_actor_key"] = "kept"
    parsed = EventEnvelope.model_validate(source)
    assert parsed.actor.id == "techcraft"
    assert parsed.payload == {"future_payload_key": "kept"}
    assert parsed.normalized() == {
        "event_type": "recalculate.requested",
        "occurred_at": "2026-09-12T12:00:00+00:00",
        "actor": {"type": "system", "id": "techcraft", "future_actor_key": "kept"},
        "payload": {"future_payload_key": "kept"},
    }


def test_top_level_extensions_are_refused_but_nested_extensions_survive():
    source = event("org_id.submitted", {"rir": "arin", "org_handle": "ORG-1", "future": 1})
    source["top_level_future"] = 1
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EventEnvelope.model_validate(source)
    source.pop("top_level_future")
    assert EventEnvelope.model_validate(source).payload["future"] == 1


def test_discriminator_selects_the_payload_contract():
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event("org_id.submitted", {"company_legal_name": "wrong"}))


def test_missing_payload_remains_compatible_only_for_empty_recalculate_payload():
    recalculate = event("recalculate.requested", {})
    recalculate.pop("payload")
    assert EventEnvelope.model_validate(recalculate).payload == {}
    kyb = event("kyb.run_requested", {})
    kyb.pop("payload")
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(kyb)


def event_document() -> dict:
    app = FastAPI()
    app.include_router(router)
    install_event_contract_openapi(app)
    return app.openapi()


def event_operation(document: dict) -> dict:
    return document["paths"]["/v1/cases/{case_id}/events"]["post"]


def local_refs(value):
    if isinstance(value, dict):
        if "$ref" in value:
            yield value["$ref"]
        for child in value.values():
            yield from local_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from local_refs(child)


def resolve_local_ref(document: dict, ref: str):
    assert ref.startswith("#/")
    value = document
    for token in ref[2:].split("/"):
        value = value[token.replace("~1", "/").replace("~0", "~")]
    return value


def rewrite_defs_refs(value):
    if isinstance(value, dict):
        return {key: rewrite_defs_refs(child) for key, child in value.items()}
    if isinstance(value, list):
        return [rewrite_defs_refs(child) for child in value]
    if isinstance(value, str) and value.startswith("#/$defs/"):
        return value.replace("#/$defs/", "#/components/schemas/", 1)
    return value


def test_openapi_uses_the_authoritative_union_for_the_required_body():
    document = event_document()
    operation = event_operation(document)
    published = operation["requestBody"]["content"]["application/json"]["schema"]
    authoritative = EventEnvelope.model_json_schema()
    assert operation["requestBody"]["required"] is True
    assert set(published["discriminator"]["mapping"]) == set(VALID_PAYLOADS)
    assert "$defs" not in published
    assert set(authoritative["$defs"]) <= set(document["components"]["schemas"])
    for ref in published["discriminator"]["mapping"].values():
        assert ref.startswith("#/components/schemas/")
        assert resolve_local_ref(document, ref) is not None
    for name, definition in authoritative["$defs"].items():
        assert document["components"]["schemas"][name] == rewrite_defs_refs(definition)


def test_every_local_ref_in_the_final_openapi_document_resolves():
    document = event_document()
    refs = tuple(local_refs(document))
    assert refs
    for ref in refs:
        assert resolve_local_ref(document, ref) is not None


def test_openapi_publishes_only_the_real_headers_and_all_live_statuses():
    operation = event_operation(event_document())
    headers = {p["name"]: p for p in operation["parameters"] if p["in"] == "header"}
    assert set(headers) == {
        "Idempotency-Key",
        "X-KYC-Timestamp",
        "X-KYC-Signature",
        "X-KYC-Key-Id",
        "X-KYC-Signature-V2",
    }
    assert headers["Idempotency-Key"]["required"] is True
    assert headers["X-KYC-Timestamp"]["required"] is True
    assert headers["X-KYC-Signature"]["required"] is False
    assert headers["X-KYC-Key-Id"]["required"] is False
    assert headers["X-KYC-Signature-V2"]["required"] is False
    assert set(operation["responses"]) == {
        "200",
        "202",
        "400",
        "401",
        "404",
        "409",
        "422",
        "503",
    }


@pytest.mark.parametrize(
    ("model", "body"),
    [
        (QueuedEventResponse, {"run_id": "run-1", "status": "queued"}),
        (
            ManualApprovalEventResponse,
            {
                "case_id": "case-1",
                "case_status": "approved_manual",
                "buy_status": "buy_locked_org_id_required",
                "recorded": True,
            },
        ),
        (EventAcceptedResponse, {"run_id": "run-1", "status": "queued"}),
        (EventHttpErrorResponse, {"detail": "invalid signature"}),
        (
            EventHttpErrorResponse,
            {"detail": [{"type": "missing", "loc": ["payload", "rir"], "msg": "Field required"}]},
        ),
        (EventIngestErrorResponse, {"error": "review task not found", "task_id": "task-1"}),
        (
            EventIngestErrorResponse,
            {"error": "idempotency key reuse with different payload", "event_id": "event-1"},
        ),
        (
            EventFailureResponse,
            {
                "error": "configuration_unavailable",
                "detail": "Configuration authority unavailable; retry safely.",
            },
        ),
    ],
)
def test_published_response_models_accept_the_existing_exact_shapes(model, body):
    assert model.model_validate(body).model_dump(mode="json", exclude_none=True) == body
