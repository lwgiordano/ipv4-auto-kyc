"""Authenticated configuration editing; responses describe one committed snapshot."""

import hmac
import json
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from kyc_tool.api.auth import require_admin, require_read_access
from kyc_tool.configuration import repo
from kyc_tool.configuration.models import (
    MAX_REQUEST_BYTES,
    ConfigurationConflict,
    ConfigurationInvalid,
    ConfigurationRequestConflict,
    ConfigurationUnavailable,
    brokers_json,
)
from kyc_tool.db.session import uow
from kyc_tool.orchestration.broker_gate import broker_overlaps
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES

router = APIRouter()


def edit_disabled_reason(settings, active):
    if active is None:
        return "Live configuration has not been activated."
    if settings.enforce_bundle_pinning is not True:
        return "Live configuration requires per-run bundle pinning."
    token = settings.ui_admin_token
    if type(token) is not str or not token.strip():
        return "Live configuration requires a configured admin credential."
    return None


def configuration_view(session, policy, settings, *, active):
    bundle = active.bundle if active else policy
    reason = edit_disabled_reason(settings, active)
    brokers = active.brokers if active else repo.legacy_brokers(session)
    body = {
        "active": active is not None,
        "revision": str(active.revision) if active else None,
        "bundle_hash": bundle.bundle_hash,
        "threshold": bundle.rubric.threshold,
        "points": {item.check_type: item.points for item in bundle.rubric.items},
        "brokers": brokers_json(brokers),
        "broker_overlaps": broker_overlaps(brokers),
        "mappings": active.mappings if active else {key: key for key in FIELD_SOURCES},
        "can_edit": reason is None,
    }
    if reason:
        body["reason"] = reason
    return body


def error(status, code, detail, **extra):
    return JSONResponse(status_code=status, content={"error": code, "detail": detail, **extra})


@router.get("/ui/api/configuration")
def get_configuration(request: Request):
    try:
        require_read_access(request.app.state.settings, request)
        with request.app.state.session_factory() as session:
            active = repo.get_active(session)
            return configuration_view(
                session, request.app.state.policy, request.app.state.settings, active=active
            )
    except HTTPException as exc:
        return error(exc.status_code, "unauthorized", "Configuration read access refused.")
    except (ConfigurationUnavailable, ConfigurationInvalid, DBAPIError):
        return error(503, "configuration_unavailable", "Configuration is unavailable; retry safely.")


def _authorize(request):
    settings = request.app.state.settings
    token = settings.ui_admin_token
    header = request.headers.get("Authorization", "")
    # require_admin deliberately permits dev-open routes; configuration never does.
    if (
        type(token) is not str
        or not token.strip()
        or not hmac.compare_digest(header.encode(), ("Bearer " + token).encode())
    ):
        raise HTTPException(401, "Invalid or missing admin credential.")
    require_admin(settings, request.headers)
    origin = request.headers.get("Origin")
    if origin is not None and origin != str(request.url.replace(path="", query="", fragment="")).rstrip("/"):
        raise HTTPException(403, "Configuration writes require the same origin.")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationInvalid("Duplicate JSON keys are not accepted.")
        result[key] = value
    return result


def _reject_constant(value):
    raise ConfigurationInvalid("Non-finite JSON numbers are not accepted.")


async def _read_input(request):
    if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ConfigurationInvalid("Content-Type must be application/json.")
    length = request.headers.get("Content-Length")
    if length is not None and (
        len(length) > 10 or not length.isascii() or not length.isdecimal() or int(length) > MAX_REQUEST_BYTES
    ):
        raise ConfigurationInvalid("Configuration request exceeds the 32 MiB bound.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise ConfigurationInvalid("Configuration request exceeds the 32 MiB bound.")
        body.extend(chunk)
    try:
        data = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigurationInvalid("Malformed JSON configuration request.") from exc
    required = {"expected_revision", "request_id", "value"}
    if type(data) is not dict or not required <= set(data) or set(data) - required - {"operator_label"}:
        raise ConfigurationInvalid("Configuration request has missing or unknown fields.")
    revision = data["expected_revision"]
    if (
        type(revision) is not str
        or re.fullmatch(r"[1-9][0-9]{0,18}", revision) is None
        or int(revision) > 9223372036854775807
    ):
        raise ConfigurationInvalid("Expected revision must be a positive decimal revision string.")
    if type(data["request_id"]) is not str:
        raise ConfigurationInvalid("Request ID must be a UUID string.")
    try:
        data["request_id"] = UUID(data["request_id"])
    except ValueError as exc:
        raise ConfigurationInvalid("Request ID must be a UUID string.") from exc
    return data


def _save(request, section, data):
    state = request.app.state
    try:
        with uow(state.session_factory) as session:
            # Bound even the first relation/authority read, before save_section's
            # exclusive pointer lock. Do not take a shared row lock then upgrade it.
            repo.transaction_limits(session)
            active = repo.get_active(session)
            if edit_disabled_reason(state.settings, active):
                raise ConfigurationUnavailable("Configuration editing is unavailable.")
            try:
                result = repo.save_section(
                    session,
                    section=section,
                    expected_revision=int(data["expected_revision"]),
                    request_id=data["request_id"],
                    value=data["value"],
                    actor=data.get("operator_label", "unspecified operator"),
                )
            except (ConfigurationConflict, ConfigurationRequestConflict) as exc:
                # save_section holds the pointer lock here; this pair is one known state.
                current = repo.get_active(session)
                return error(
                    409,
                    "request_id_conflict"
                    if isinstance(exc, ConfigurationRequestConflict)
                    else "configuration_conflict",
                    "Configuration changed or the request ID was reused; reload before saving.",
                    expected_revision=data["expected_revision"],
                    current_revision=str(current.revision),
                )
            active = repo.get_revision(session, int(result["current_revision"]))
            result["configuration"] = configuration_view(session, state.policy, state.settings, active=active)
        return result  # only after uow commit; never refresh across the commit boundary
    except ConfigurationInvalid as exc:
        return error(422, "invalid_configuration", str(exc))
    except (ConfigurationUnavailable, DBAPIError):
        return error(
            503,
            "configuration_unavailable",
            "Configuration save is unavailable; retry the same request safely.",
        )


@router.put("/ui/api/configuration/{section}")
async def put_configuration(section: str, request: Request):
    try:
        _authorize(request)  # no body read or database activity before authentication
        data = await _read_input(request)
    except HTTPException as exc:
        return error(
            exc.status_code, "unauthorized" if exc.status_code == 401 else "foreign_origin", exc.detail
        )
    except ConfigurationInvalid as exc:
        return error(422, "invalid_configuration", str(exc))
    return await run_in_threadpool(_save, request, section, data)
