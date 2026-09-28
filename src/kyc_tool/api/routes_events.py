"""POST /v1/cases/{case_id}/events — the only way work starts."""

import json

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from kyc_tool.api.auth import (
    IDEMPOTENCY_KEY_HEADER,
    KEY_ID_HEADER,
    TIMESTAMP_HEADER,
    V1_SIGNATURE_HEADER,
    V2_SIGNATURE_HEADER,
    require_valid_signature,
)
from kyc_tool.api.schemas import EVENT_RESPONSE_MODELS, EventEnvelope
from kyc_tool.events.ingest import ingest_event

router = APIRouter()


def _event_openapi_extra() -> dict[str, object]:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": EventEnvelope.model_json_schema()}},
        },
        "parameters": [
            {
                "name": IDEMPOTENCY_KEY_HEADER,
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
                "description": (
                    "Unique per logical event; reuse the same value and normalized event for retry."
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
                "description": (
                    "Required with X-KYC-Signature-V2; presence selects sticky v2 authentication."
                ),
            },
            {
                "name": V2_SIGNATURE_HEADER,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": ("Required with X-KYC-Key-Id; presence selects sticky v2 authentication."),
            },
        ],
    }


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
    # FastAPI JSON-encodes ``openapi_extra`` with ``exclude_none=True`` before
    # this installer receives the final document. Generate once more here so
    # optional-field ``default: null`` values remain identical to the runtime
    # model's authoritative JSON Schema.
    schema = EventEnvelope.model_json_schema()
    definitions = schema.pop("$defs")
    components = document.setdefault("components", {}).setdefault("schemas", {})
    for name, definition in definitions.items():
        rewritten = _rewrite_event_schema_refs(definition)
        if name in components and components[name] != rewritten:
            raise RuntimeError(f"OpenAPI component collision for {name}")
        components[name] = rewritten
    operation["requestBody"]["content"]["application/json"]["schema"] = _rewrite_event_schema_refs(schema)


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
