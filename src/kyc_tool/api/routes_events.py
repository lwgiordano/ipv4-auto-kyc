"""POST /v1/cases/{case_id}/events — the only way work starts."""

import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from kyc_tool.api.auth import require_valid_signature
from kyc_tool.api.schemas import EventEnvelope
from kyc_tool.events.ingest import ingest_event

router = APIRouter()


@router.post("/v1/cases/{case_id}/events")
async def post_event(
    case_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    body = await request.body()
    require_valid_signature(request.app.state.settings, request, body)

    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    try:
        envelope = EventEnvelope.model_validate(json.loads(body))
        payload = envelope.validated_payload()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"invalid JSON: {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc

    envelope_dict = {
        "event_type": envelope.event_type,
        "occurred_at": envelope.occurred_at.isoformat(),
        "actor": envelope.actor.model_dump(mode="json"),
        "payload": payload,
    }
    outcome = await run_in_threadpool(
        ingest_event,
        request.app.state.session_factory,
        request.app.state.policy,
        case_id=case_id,
        idempotency_key=idempotency_key,
        envelope=envelope_dict,
        settings=request.app.state.settings,
    )
    return JSONResponse(status_code=outcome.status_code, content=outcome.body)
