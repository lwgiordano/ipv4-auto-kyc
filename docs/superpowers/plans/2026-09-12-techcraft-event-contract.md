# TechCraft Event Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the nine accepted platform events one discriminated, runtime-enforced
Pydantic contract and publish the live event endpoint's real request, header, and
response surface in OpenAPI without changing wire behavior.

**Architecture:** `EventEnvelope` becomes a compatibility wrapper around one
`event_type`-discriminated union whose variants use the existing payload models.
The endpoint authenticates the exact raw bytes first, then passes those bytes to
the union parser and converts the typed result through one explicit normalization
method that preserves today's replay hash. OpenAPI embeds JSON Schema generated
from that same union; it does not add a FastAPI body parameter that could parse the
body before authentication.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, pytest, PostgreSQL 16 integration
tests.

**Spec:** `docs/superpowers/specs/2026-09-12-production-readiness-design.md`

## Global Constraints

- The platform remains the hub; work starts only from platform events.
- Keep the current modular monolith and existing API, pipeline-worker, outbox,
  retention-worker, PostgreSQL, object-storage, and operations-console roles.
- Do not modify `KYC_Tool_Build_Package/`; its machine-readable JSON remains the
  business-policy authority, while OpenAPI is a derived transport contract.
- Preserve the existing authentication, authorization, evidence-containment,
  idempotency, audit, and immutable/supersedable-check controls.
- Authenticate the exact raw request bytes before any event-envelope or payload
  validation. Missing or malformed event data must not outrank a signature failure.
- Preserve the accepted compatibility asymmetry: top-level envelope extras are
  refused; actor and event-specific payload extras are accepted and preserved.
- Preserve `EventEnvelope.model_validate(...)`, attribute access used by the
  operations composer, and `validated_payload()` while making the discriminated
  union the only event-shape authority.
- Preserve the normalized envelope used by `payload_hash`: `occurred_at` continues
  through `datetime.isoformat()`, while actor and payload use Pydantic JSON-mode
  dumps. Do not change idempotent replay or same-key/different-payload behavior.
- Do not add a required wire-version header or a top-level/payload
  `schema_version`. HMAC v2's canonical first line is an authentication-protocol
  version, not an event-schema version.
- Positive enforcement remains off until the roadmap's M2 gate is satisfied.
- No new dependency, migration, provider, Salesforce write, deployment, secret,
  external webhook, or generated client is part of this slice.
- The parent agent is the only committer, pusher, and bus writer. Implementation
  and review agents edit only their assigned files and return evidence to the
  parent.

## Source Reconciliation and Bounded Follow-ons

This plan is the first independently buildable Unit 1 slice. It does **not** claim
that all of Unit 1 is complete.

1. The Unit 1 design now correctly says the request protocol has no wire-version
   field or header, but ROADMAP PR 9a still says `schema_version`. The live endpoint
   and signer accept neither. The actual inbound surface is `Idempotency-Key`,
   `X-KYC-Timestamp`, and either legacy `X-KYC-Signature` or the sticky v2 pair
   `X-KYC-Key-Id` plus `X-KYC-Signature-V2`. This plan publishes that surface and
   nothing else. Correct the stale roadmap wording before a versioned artifact
   export is planned.
2. A new review-task-change webhook cannot be implemented from the repository.
   The design itself assigns TechCraft the choice between a webhook and governed
   polling, including cursor/freshness/recovery semantics. Keep that as a separate,
   externally answer-dependent unit. Existing
   `GET /v1/review-tasks?status=open` and the signed
   `website.review_completed` event remain unchanged.
3. A committed/versioned request schema, callback schema, stable example bundle,
   HMAC vectors, and digest manifest need one agreed artifact version, filenames,
   location, and active-version authority. Plan that export separately after this
   runtime/OpenAPI slice. It must derive the request schema from `EventEnvelope`
   and the callback schema from `DecisionCallback`/`encode_decision_callback`; it
   must not copy field lists or introduce a wire field.
4. The current ingest registry omits a real 503 outcome, and prose tables do not
   completely describe review-task 404/409/422 variants. OpenAPI in this slice
   records the live codes `200, 202, 400, 401, 404, 409, 422, 503`. A later document
   reconciliation must derive from the executable surface rather than maintain a
   competing hand-written list.

---

### Task 1: Authoritative discriminated event parser

**Files:**

- Modify: `src/kyc_tool/api/schemas.py:8-125`
- Modify: `docs/contracts/authority.py:564-569`
- Modify: `docs/contracts/wire.py:1637-1647`
- Create: `tests/unit/test_event_contract.py`
- Modify: `tests/unit/test_contract_registry_authority.py`

**Interfaces:**

- Consumes: the existing `Actor`, nine payload model classes, and their current
  `extra` policies.
- Produces: `PlatformEvent`, a discriminated union of nine concrete event models;
  `EventEnvelope.model_validate(obj) -> EventEnvelope`;
  `EventEnvelope.model_validate_json(raw: bytes) -> EventEnvelope`;
  `EventEnvelope.normalized() -> dict[str, object]`; and the existing
  `EventEnvelope.validated_payload() -> dict[str, object]` compatibility method.
- Preserves: `EventType` and `PAYLOAD_MODELS` for policy/document authority tests,
  with `PLATFORM_EVENT_MODELS: tuple[type[BaseModel], ...]` exposing the concrete
  strict variants to the bounded document-authority checker. No assertion reads
  `EventEnvelope.model_config`, because Pydantic forbids setting `extra` on a
  `RootModel`.

- [ ] **Step 1: Write the nine-variant parser and compatibility RED tests**

Create `tests/unit/test_event_contract.py` with fixed, complete examples for every
existing payload model:

```python
import json
from typing import get_args

import pytest
from pydantic import ValidationError

from kyc_tool.api.schemas import EventEnvelope, EventType, PAYLOAD_MODELS

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
    "document.uploaded": {"object_ref": "uploads/acme/doc.json", "doc_type": "registration"},
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


@pytest.mark.parametrize("event_type", sorted(VALID_PAYLOADS))
def test_real_parser_selects_every_payload_variant(event_type):
    raw = json.dumps(event(event_type, VALID_PAYLOADS[event_type]))
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
```

- [ ] **Step 2: Run the tests and verify the intended RED**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_event_contract.py
```

Expected: collection or assertions fail because `EventEnvelope` still has a plain
`dict` payload, has no `.root` or `.normalized()`, and its schema has no
`event_type` discriminator/`oneOf` mapping.

- [ ] **Step 3: Implement the minimal discriminated union**

In `src/kyc_tool/api/schemas.py`, keep the nine payload models unchanged. Add one
strict base envelope, nine variants, one union, and make `EventEnvelope` a
`RootModel` compatibility wrapper. Use these exact shapes and do not add fields:

```python
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, RootModel


class _PlatformEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    occurred_at: datetime
    actor: Actor


class KybRunRequestedEvent(_PlatformEventBase):
    event_type: Literal["kyb.run_requested"]
    payload: KybRunPayload


class EmailVerifiedEvent(_PlatformEventBase):
    event_type: Literal["email.verified"]
    payload: EmailVerifiedPayload


class OrgIdSubmittedEvent(_PlatformEventBase):
    event_type: Literal["org_id.submitted"]
    payload: OrgIdSubmittedPayload


class PocSubmittedEvent(_PlatformEventBase):
    event_type: Literal["poc.submitted"]
    payload: PocSubmittedPayload


class PocTokenVerifiedEvent(_PlatformEventBase):
    event_type: Literal["poc.token_verified"]
    payload: PocTokenVerifiedPayload


class DocumentUploadedEvent(_PlatformEventBase):
    event_type: Literal["document.uploaded"]
    payload: DocumentUploadedPayload


class WebsiteReviewCompletedEvent(_PlatformEventBase):
    event_type: Literal["website.review_completed"]
    payload: WebsiteReviewCompletedPayload


class ReviewerManualApproveEvent(_PlatformEventBase):
    event_type: Literal["reviewer.manual_approve"]
    payload: ReviewerManualApprovePayload


class RecalculateRequestedEvent(_PlatformEventBase):
    event_type: Literal["recalculate.requested"]
    payload: RecalculatePayload = Field(default_factory=RecalculatePayload)


PlatformEvent = Annotated[
    KybRunRequestedEvent
    | EmailVerifiedEvent
    | OrgIdSubmittedEvent
    | PocSubmittedEvent
    | PocTokenVerifiedEvent
    | DocumentUploadedEvent
    | WebsiteReviewCompletedEvent
    | ReviewerManualApproveEvent
    | RecalculateRequestedEvent,
    Field(discriminator="event_type"),
]


def _event_variants() -> tuple[type[BaseModel], ...]:
    union = get_args(PlatformEvent)[0]
    return get_args(union)


def _event_type(model: type[BaseModel]) -> str:
    values = get_args(model.model_fields["event_type"].annotation)
    if len(values) != 1 or type(values[0]) is not str:
        raise TypeError(f"{model.__name__}.event_type must be one string Literal")
    return values[0]


PLATFORM_EVENT_MODELS: tuple[type[BaseModel], ...] = _event_variants()

PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    _event_type(model): model.model_fields["payload"].annotation
    for model in PLATFORM_EVENT_MODELS
}


class EventEnvelope(RootModel[PlatformEvent]):
    @property
    def event_type(self) -> EventType:
        return self.root.event_type

    @property
    def occurred_at(self) -> datetime:
        return self.root.occurred_at

    @property
    def actor(self) -> Actor:
        return self.root.actor

    @property
    def payload(self) -> dict[str, Any]:
        return self.root.payload.model_dump(mode="json")

    def validated_payload(self) -> dict[str, Any]:
        return self.payload

    def normalized(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "actor": self.actor.model_dump(mode="json"),
            "payload": self.validated_payload(),
        }
```

The helper above derives `PAYLOAD_MODELS` from the union instead of maintaining a
second event-to-payload table. Keep `EventType` for its existing public/document-
authority role, with the closed-set test above preventing drift. Do not dynamically
create model classes because stable model names are part of readable OpenAPI.

Update `WIRE.INGEST.EXTRA_FIELDS.authority` in `docs/contracts/wire.py` to name
`PLATFORM_EVENT_MODELS / PAYLOAD_MODELS`. In `docs/contracts/authority.py`, import
`PLATFORM_EVENT_MODELS` and replace the impossible wrapper-config assertion with
this bounded structural and behavioral authority:

```python
@verifies("WIRE.INGEST.EXTRA_FIELDS")
def _extra_field_policy_matches_the_models():
    claim = WIRE.value("WIRE.INGEST.EXTRA_FIELDS")
    assert claim["envelope"] == "forbid"
    assert PLATFORM_EVENT_MODELS
    for model in PLATFORM_EVENT_MODELS:
        assert model.model_config.get("extra") == claim["envelope"]
    for model in PAYLOAD_MODELS.values():
        assert model.model_config.get("extra") == claim["payload"] == "allow"

    specimen = {
        "event_type": "recalculate.requested",
        "occurred_at": "2026-09-12T12:00:00Z",
        "actor": {"type": "system", "id": "authority"},
        "payload": {},
        "top_level_extension": "refused",
    }
    with raises(PydanticValidationError):
        EventEnvelope.model_validate(specimen)
```

Extend `tests/unit/test_contract_registry_authority.py` with a mutation against the
same module binding the assembled verifier resolves:

```python
def test_event_envelope_authority_refuses_a_permissive_variant(monkeypatch):
    authority = _this_module()

    class PermissiveRecalculate(schemas.RecalculateRequestedEvent):
        model_config = {"extra": "allow"}

    mutated = tuple(
        PermissiveRecalculate
        if model is schemas.RecalculateRequestedEvent
        else model
        for model in authority.PLATFORM_EVENT_MODELS
    )
    monkeypatch.setattr(authority, "PLATFORM_EVENT_MODELS", mutated)
    assert any(
        problem.startswith("WIRE.INGEST.EXTRA_FIELDS:")
        for problem in authority.problems()
    )
```

Do not scan source or pretend the `RootModel` carries the variant configuration.

- [ ] **Step 4: Run focused schema and policy-authority tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_event_contract.py \
  tests/unit/test_contract_registry_authority.py \
  tests/policy_driven/test_policy_alignment.py
```

Expected: PASS. Confirm the generated schema has exactly nine discriminator
mappings, every variant has `additionalProperties: false`, every payload/actor
definition has `additionalProperties: true`, and the empty recalculate payload
still accepts extensions.

- [ ] **Step 5: Hand the task to its independent reviewer**

The reviewer must compare the diff against this task and run the same selectors.
They must specifically try: an unknown event type, a payload from the wrong event,
a top-level extra, nested extras, and missing `payload` for both recalculate and a
non-empty event. Return findings to the implementer; do not commit from either
agent.

---

### Task 2: Raw-byte runtime parsing and truthful OpenAPI operation

**Files:**

- Modify: `src/kyc_tool/api/auth.py:1-12,209-274`
- Modify: `src/kyc_tool/api/app.py:178-182`
- Modify: `src/kyc_tool/api/routes_events.py:1-52`
- Modify: `src/kyc_tool/api/schemas.py` (response models only)
- Modify: `tests/unit/test_event_contract.py`
- Modify: `tests/integration/test_ingest.py:20-103`
- Create: `tests/integration/test_event_contract.py`
- Modify: `tests/policy_driven/test_engine_build_id_guard.py:23`

**Interfaces:**

- Consumes: Task 1's `EventEnvelope.model_validate_json(raw)` and
  `EventEnvelope.normalized()`.
- Produces: exported header-name constants in `kyc_tool.api.auth`; an OpenAPI
  request body generated only by `EventEnvelope.model_json_schema()`; response
  models for the endpoint's existing bodies; and the unchanged live status codes.
- Does not change: `ingest_event(...)`, `IngestOutcome`, database rows, response
  snapshots, signature algorithms, v1/v2 selection, or any status/body branch.

- [ ] **Step 1: Add OpenAPI, ordering, and replay-preservation RED tests**

Append the following assertions to `tests/unit/test_event_contract.py`:

```python
from fastapi import FastAPI

from kyc_tool.api.routes_events import install_event_contract_openapi, router


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
        "200", "202", "400", "401", "404", "409", "422", "503"
    }
```

In the same file, bind the response models to the bodies the current route and
ingest branches already emit:

```python
from kyc_tool.api.schemas import (
    EventAcceptedResponse,
    EventFailureResponse,
    EventHttpErrorResponse,
    EventIngestErrorResponse,
    ManualApprovalEventResponse,
    QueuedEventResponse,
)


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
```

Add these integration tests to `tests/integration/test_ingest.py` using the
existing `sign_headers`, fixed bodies, and real endpoint:

```python
def test_signature_failure_wins_over_invalid_json(client, clean_db):
    body = b'{"not":'
    headers = sign_headers(body)
    headers["X-KYC-Signature"] = "0" * 64
    response = client.post("/v1/cases/auth-first/events", content=body, headers=headers)
    assert response.status_code == 401


def test_valid_signature_then_invalid_json_is_422(client, clean_db):
    body = b'{"not":'
    response = client.post(
        "/v1/cases/parse-second/events", content=body, headers=sign_headers(body)
    )
    assert response.status_code == 422


def test_semantically_equal_normalized_envelopes_replay(client, clean_db):
    first_body = json.dumps(
        {
            "event_type": "kyb.run_requested",
            "occurred_at": "2026-09-12T12:00:00Z",
            "actor": {"type": "system", "id": "techcraft", "actor_extension": "kept"},
            "payload": {"company_legal_name": "Acme", "payload_extension": "kept"},
        }
    ).encode()
    second_body = json.dumps(
        {
            "payload": {
                "company_legal_name": "Acme",
                "address": None,
                "registration_number": None,
                "jurisdiction": None,
                "website": None,
                "contact": None,
                "platform_account_id": None,
                "payload_extension": "kept",
            },
            "actor": {
                "actor_extension": "kept",
                "id": "techcraft",
                "type": "system",
            },
            "occurred_at": "2026-09-12T12:00:00+00:00",
            "event_type": "kyb.run_requested",
        },
        separators=(",", ":"),
    ).encode()
    key = "normalized-replay"
    first = client.post(
        "/v1/cases/normalized/events", content=first_body, headers=sign_headers(first_body, key=key)
    )
    replay = client.post(
        "/v1/cases/normalized/events", content=second_body, headers=sign_headers(second_body, key=key)
    )
    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json() == first.json()
```

Create `tests/integration/test_event_contract.py`. Its helper must validate each
real endpoint body with the status-indexed Pydantic model and prove the final
OpenAPI response points at that exact model component:

```python
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
    schema = document["paths"]["/v1/cases/{case_id}/events"]["post"]["responses"][
        str(status)
    ]["content"]["application/json"]["schema"]
    expected_ref = f"#/components/schemas/{model.__name__}"
    assert schema == {"$ref": expected_ref}
    assert document["components"]["schemas"][model.__name__]


def test_actual_200_through_422_bodies_match_the_published_status_models(
    client, clean_db, post_event
):
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
    missing_idem = client.post(
        "/v1/cases/contract-400/events", content=raw, headers=missing_idem_headers
    )
    bad_signature_headers = sign_headers(raw)
    bad_signature_headers["X-KYC-Signature"] = "0" * 64
    bad_signature = client.post(
        "/v1/cases/contract-401/events", content=raw, headers=bad_signature_headers
    )
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


def test_actual_503_body_matches_the_published_status_model(
    settings, session_factory, policy, clean_db
):
    baseline(session_factory, policy)
    client = make_client(
        settings, session_factory, policy, enforce_bundle_pinning=False
    )
    raw = json.dumps(envelope("recalculate.requested", {})).encode()
    response = client.post(
        "/v1/cases/contract-503/events", content=raw, headers=sign_headers(raw)
    )
    assert response.json() == {
        "error": "configuration_unavailable",
        "detail": "Configuration authority unavailable; retry safely.",
    }
    assert_response_contract(client, response, 503)
```

Keep the existing ingest, review-task, manual-approval, and configuration tests as
independent behavior witnesses. Do not invent a generic success envelope.

- [ ] **Step 2: Run the RED selectors**

Run:

```bash
.venv/bin/python -m pytest tests/unit/test_event_contract.py
.venv/bin/python -m pytest tests/integration/test_ingest.py
```

Expected: unit failures show no request body, only the optional idempotency header,
and incomplete response documentation. The runtime selector should remain green at
this point; it is a behavior-preservation suite, not evidence of the missing
OpenAPI contract.

- [ ] **Step 3: Centralize actual inbound header names without changing auth**

At the top of `src/kyc_tool/api/auth.py`, define and use these constants everywhere
the current verifier reads headers:

```python
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
TIMESTAMP_HEADER = "X-KYC-Timestamp"
V1_SIGNATURE_HEADER = "X-KYC-Signature"
KEY_ID_HEADER = "X-KYC-Key-Id"
V2_SIGNATURE_HEADER = "X-KYC-Signature-V2"
```

Replace only the matching string literals. Preserve sticky v2 detection by header
**presence**, including present-but-empty headers, and preserve v1 verification and
the durable witness ordering exactly.

- [ ] **Step 4: Publish response models that match existing bodies**

Add strict models in `src/kyc_tool/api/schemas.py` and use them only as the OpenAPI
surface in this task:

```python
class QueuedEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    status: Literal["queued"]


class ManualApprovalEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    case_status: Literal["approved_manual"]
    buy_status: Literal["buy_enabled", "buy_locked_org_id_required"]
    recorded: Literal[True]


class EventIngestErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str
    event_id: str | None = None
    task_id: str | None = None
    detail: str | None = None


class EventHttpErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str | list[dict[str, Any]]


class EventAcceptedResponse(RootModel[QueuedEventResponse | ManualApprovalEventResponse]):
    pass


class EventFailureResponse(RootModel[EventIngestErrorResponse | EventHttpErrorResponse]):
    pass


EVENT_RESPONSE_MODELS: dict[int, type[BaseModel]] = {
    200: EventAcceptedResponse,
    202: QueuedEventResponse,
    400: EventHttpErrorResponse,
    401: EventHttpErrorResponse,
    404: EventIngestErrorResponse,
    409: EventIngestErrorResponse,
    422: EventFailureResponse,
    503: EventFailureResponse,
}
```

The `200` schema is intentionally a union: a replay returns the stored original
body, so it can be the queued shape, while a new or replayed manual approval has
the manual shape. `202` is only `QueuedEventResponse`. `400`/`401` use
`EventHttpErrorResponse`; `404`/`409` use `EventIngestErrorResponse`; `422` and
`503` use `EventFailureResponse` because each can originate either in route/auth
handling or in `ingest_event`. This mapping is the single status-to-model authority
used by both the route decorator and contract tests. Do not refactor
`IngestOutcome` or stored response snapshots in this slice.

- [ ] **Step 5: Parse only after raw-byte authentication and derive OpenAPI**

In `src/kyc_tool/api/routes_events.py`:

1. Remove `json.loads` and its separate payload validation.
2. Keep `body = await request.body()` followed immediately by
   `require_valid_signature(..., body)`.
3. Read `Idempotency-Key` manually from `request.headers` after authentication so
   FastAPI cannot reject a missing header before the verifier runs.
4. Call `EventEnvelope.model_validate_json(body)` and pass
   `envelope.normalized()` to `ingest_event`.
5. Catch Pydantic `ValidationError` and continue returning 422. Preserve the
   current JSON-safe `detail` envelope; invalid JSON and schema validation may be
   distinguished from `exc.errors(include_url=False)` without a second parser.
6. Add `openapi_extra` with `requestBody.schema` equal to
   `EventEnvelope.model_json_schema()` and five header parameters named from the
   auth constants. Mark idempotency and timestamp required; document the legacy
   signature and sticky-v2 pair as alternatives, so no generated client is told
   to send all three signature headers.
7. Add FastAPI `responses={...}` using the response models and exact status
   descriptions above. Do not advertise only the decorator's default 200.

The operation setup should be a small pure helper called at import time:

```python
def _event_openapi_extra() -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": EventEnvelope.model_json_schema()}
            },
        },
        "parameters": [
            {
                "name": IDEMPOTENCY_KEY_HEADER,
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": (
                    "Unique per logical event; reuse the same value and "
                    "normalized event for retry."
                ),
            },
            {
                "name": TIMESTAMP_HEADER,
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": "Unix-seconds timestamp included in either signature protocol.",
            },
            {
                "name": V1_SIGNATURE_HEADER,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "Required only for a legacy v1-signed request with no v2 header.",
            },
            {
                "name": KEY_ID_HEADER,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "Required with X-KYC-Signature-V2; presence selects sticky v2 authentication.",
            },
            {
                "name": V2_SIGNATURE_HEADER,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "Required with X-KYC-Key-Id; presence selects sticky v2 authentication.",
            },
        ],
    }
```

An inline schema containing `#/$defs/...` references is invalid once placed under
the final OpenAPI root: those references resolve from the document root, where
`$defs` does not exist. In `routes_events.py`, add a bounded final-document
installer that hoists only the generated event definitions and rewrites every
reference string, including discriminator mappings:

```python
def _rewrite_event_schema_refs(value):
    if isinstance(value, dict):
        return {key: _rewrite_event_schema_refs(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_rewrite_event_schema_refs(child) for child in value]
    if isinstance(value, str) and value.startswith("#/$defs/"):
        return value.replace("#/$defs/", "#/components/schemas/", 1)
    return value


def _hoist_event_schema(document: dict[str, object]) -> None:
    operation = document["paths"]["/v1/cases/{case_id}/events"]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    definitions = schema.pop("$defs")
    components = document.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in definitions.items():
        rewritten = _rewrite_event_schema_refs(definition)
        if name in components and components[name] != rewritten:
            raise RuntimeError(f"OpenAPI component collision for {name}")
        components[name] = rewritten
    operation["requestBody"]["content"]["application/json"]["schema"] = (
        _rewrite_event_schema_refs(schema)
    )


def install_event_contract_openapi(app: FastAPI) -> None:
    default_openapi = app.openapi

    def openapi():
        document = default_openapi()
        operation = document["paths"]["/v1/cases/{case_id}/events"]["post"]
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        if "$defs" in request_schema:
            _hoist_event_schema(document)
        app.openapi_schema = document
        return document

    app.openapi = openapi
```

Import `FastAPI` for the annotation. In `create_app`, call
`install_event_contract_openapi(app)` after every route has been registered and
immediately before `return app`. The installer is deliberately limited to this
operation and the definitions generated from `EventEnvelope`; it is not a general
OpenAPI rewriter.

Attach the helper and response models directly to the existing operation:

```python
_EVENT_RESPONSE_DESCRIPTIONS = {
    200: "Stored replay response, or inline reviewer.manual_approve.",
    202: "New event accepted and queued.",
    400: "Idempotency-Key is missing.",
    401: "Signature is invalid or retired.",
    404: "Review task was not found.",
    409: "Idempotency or task-state conflict.",
    422: "Event or review validation failed.",
    503: "Required configuration or auth witness unavailable.",
}


@router.post(
    "/v1/cases/{case_id}/events",
    openapi_extra=_event_openapi_extra(),
    responses={
        code: {"model": model, "description": _EVENT_RESPONSE_DESCRIPTIONS[code]}
        for code, model in EVENT_RESPONSE_MODELS.items()
    },
)
async def post_event(case_id: str, request: Request) -> JSONResponse:
    body = await request.body()
    require_valid_signature(request.app.state.settings, request, body)
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    try:
        envelope = EventEnvelope.model_validate_json(body)
    except ValidationError as exc:
        errors = json.loads(exc.json(include_url=False))
        detail = f"invalid JSON: {errors[0]['msg']}" if errors[0]["type"] == "json_invalid" else errors
        raise HTTPException(status_code=422, detail=detail) from exc
    outcome = await run_in_threadpool(
        ingest_event,
        request.app.state.session_factory,
        request.app.state.policy,
        case_id=case_id,
        idempotency_key=idempotency_key,
        envelope=envelope.normalized(),
        settings=request.app.state.settings,
    )
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)
```

The route signature remains `(case_id: str, request: Request)`. Do not add a typed
body or required `Header(...)` parameter: FastAPI validates those before entering
the function, which would violate authentication-before-validation and change the
missing-header response order.

- [ ] **Step 6: Format only changed Python files, then run focused tests**

Run:

```bash
.venv/bin/python -m ruff format \
  src/kyc_tool/api/app.py \
  src/kyc_tool/api/auth.py \
  src/kyc_tool/api/routes_events.py \
  src/kyc_tool/api/schemas.py \
  docs/contracts/authority.py \
  docs/contracts/wire.py \
  tests/unit/test_event_contract.py \
  tests/unit/test_contract_registry_authority.py \
  tests/integration/test_event_contract.py \
  tests/integration/test_ingest.py \
  tests/policy_driven/test_engine_build_id_guard.py
.venv/bin/python -m pytest \
  tests/unit/test_event_contract.py \
  tests/unit/test_review_guard.py \
  tests/unit/test_contract_registry_authority.py
.venv/bin/python -m pytest \
  tests/integration/test_event_contract.py \
  tests/integration/test_ingest.py \
  tests/integration/test_hmac_dual_accept.py \
  tests/integration/test_review_completed_event.py \
  tests/integration/test_phase4_platform.py \
  tests/integration/test_configuration_runtime.py \
  tests/integration/test_ui.py
```

Expected: PASS. Inspect `/openapi.json` in the test to confirm all nine variants,
five real headers, and eight live status codes are present. The sensitive-event
actor floor remains a runtime rule in `review_guard`; do not encode its cross-field
equality as an unsupported static-schema claim.

- [ ] **Step 7: Re-pin the whole-source drift guard without changing engine identity**

After all scoped source formatting is complete, compute the final framed source
hash and update `EXPECTED_ENGINE_SOURCE_HASH` in
`tests/policy_driven/test_engine_build_id_guard.py`. In that guard file, permit only
the formatter's mechanical change plus the expected-hash re-pin. This transport-
contract change does not alter scoring/decision semantics, so do **not** change
`ENGINE_BUILD_ID`.

Run:

```bash
.venv/bin/python -m pytest tests/policy_driven/test_engine_build_id_guard.py
```

Expected: PASS after the exact new digest is pinned.

- [ ] **Step 8: Hand the task to its independent reviewer**

The reviewer reruns the Task 2 selectors and inspects the route in this exact
order: read raw bytes, authenticate, check idempotency presence, parse union,
normalize, ingest. They must compare auth constants to every lookup in
`require_valid_signature`, verify no new header/field/version exists, and compare
every documented status schema to a real test response. Return findings to the
implementer; neither agent commits.

---

### Task 3: Parent verification and whole-slice adversarial review

**Files:**

- Verify only; fix confirmed findings through the owning task agent before parent
  commit.

**Interfaces:**

- Consumes: Tasks 1 and 2 after independent review.
- Produces: a parent-verified, reviewable event-contract slice ready for one
  claimed commit and bus release.

- [ ] **Step 1: Run the complete local gate in the mandated order**

```bash
.venv/bin/python -m pytest \
  tests/unit/test_event_contract.py \
  tests/unit/test_review_guard.py \
  tests/unit/test_contract_registry_authority.py
.venv/bin/python -m pytest \
  tests/integration/test_event_contract.py \
  tests/integration/test_ingest.py \
  tests/integration/test_hmac_dual_accept.py \
  tests/integration/test_review_completed_event.py \
  tests/integration/test_phase4_platform.py \
  tests/integration/test_configuration_runtime.py \
  tests/integration/test_ui.py
.venv/bin/python -m pytest \
  tests/policy_driven/test_engine_build_id_guard.py \
  tests/policy_driven/test_policy_alignment.py
.venv/bin/python -m ruff format --check \
  src/kyc_tool/api/app.py \
  src/kyc_tool/api/auth.py \
  src/kyc_tool/api/routes_events.py \
  src/kyc_tool/api/schemas.py \
  docs/contracts/authority.py \
  docs/contracts/wire.py \
  tests/unit/test_event_contract.py \
  tests/unit/test_contract_registry_authority.py \
  tests/integration/test_event_contract.py \
  tests/integration/test_ingest.py \
  tests/policy_driven/test_engine_build_id_guard.py
./manage.sh lint
.venv/bin/lint-imports
./manage.sh test
git diff --check
```

Expected: every command exits 0; import-linter reports two kept and zero broken
contracts; the full suite runs rather than silently skipping PostgreSQL tests.

- [ ] **Step 2: Run one whole-slice adversarial review**

The reviewer receives the spec, this plan, and the exact diff. They must attempt
to falsify: union/payload closed-set parity; top-level strictness; nested-extension
preservation; raw-byte auth precedence; sticky-v2 behavior; normalized replay;
OpenAPI/runtime body parity; header names and requiredness; status/body parity;
`EventEnvelope` operations-composer compatibility; and absence of any
`schema_version` or wire-version header. Confirmed findings return to the owning
task and repeat its independent review plus the parent gates.

- [ ] **Step 3: Parent records the bounded result**

After all evidence is direct and green, the parent commits only the claimed files
and writes the bus release. The release must call this the runtime/OpenAPI event
contract slice, not completion of Unit 1 or production readiness, and must carry
the three explicit follow-ons above: artifact export/manifest, callback export,
and the TechCraft-chosen review-task-change contract.
