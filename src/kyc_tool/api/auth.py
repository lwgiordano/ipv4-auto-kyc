"""Inbound request authentication — HMAC signed bearer.

Dual-accept (PR 5a): v2 is path-bound and STICKY — if a request carries any v2
header (`X-KYC-Signature-V2` or `X-KYC-Key-Id`) it is evaluated v2-only, with NO
fallback to the path-unbound v1 scheme. A request with no v2 header may still
authenticate with v1, but only before `hmac_v1_inbound_sunset_at`, and every
accepted v1 request is recorded in the fail-closed durable witness (§6).

v1 headers:  X-KYC-Timestamp, X-KYC-Signature = HMAC(secret, "{ts}.{body}")
v2 headers:  X-KYC-Timestamp, X-KYC-Key-Id, X-KYC-Signature-V2 = HMAC(secret,
             canonical_v2(...))  — binds method + path + case.
"""

import hmac
import os
from collections import Counter
from datetime import UTC, datetime

from fastapi import HTTPException

from kyc_tool import security
from kyc_tool.api import hmac_witness
from kyc_tool.config import Settings, parse_sunset

# Process-local diagnostic counters (v2_accepted | rejected). Deliberately NOT database-backed
# (re-audit `d569a15..4938840` F1): a rejected (401) request must not open a session or persist
# telemetry, or an unauthenticated caller — and the browser sidebar polling unsigned — could drive
# synchronous DB writes on the rejection path. Nothing reads these yet; a future metrics endpoint may
# expose them. The distinct FAIL-CLOSED v1-acceptance witness (`_record_v1`) stays durable.
_DIAGNOSTIC_COUNTS: Counter = Counter()
# Process identity + start epoch so a reader can never mistake this replica's counters for a fleet
# total (re-audit `8aba2df..2cee937` R3-F2). datetime.now at import time is the process start.
_PROCESS_STARTED_AT = datetime.now(UTC)


def _sunset_passed(iso: str, now: datetime) -> bool:
    # Exact-type gate BEFORE the parser sees it (re-audit `4c3015a..cccd5f7` F1). A correctly
    # signed v1 request reaches this line, so anything raised here is a 500 on an AUTHENTICATED
    # request — the worst place to be non-total. A hostile `str` subclass reaching `parse_sunset`
    # dispatches its `strip`/`__eq__` inside the parser; a non-str reaches a comparison that has no
    # defined answer. Neither is a sunset that has passed, so both mean "not passed".
    if type(iso) is not str:
        return False
    try:
        dt = parse_sunset(iso)
    except ValueError:
        # A malformed date fails the production kill switch at boot; in dev we
        # must not 500 mid-request — treat an unparseable date as "not passed".
        return False
    return dt is not None and now >= dt


def _raw_path_qs(request) -> str:
    """The LITERAL request target the client signed: ASGI ``raw_path`` (percent-
    encoding preserved) + raw ``query_string`` — NOT ``request.url.path``, which
    the framework percent-decodes, so a signature over the raw target (what an
    independent signer puts on the wire, per PLATFORM_INTEGRATION §2) would be
    rejected for any encoded case id."""
    scope = request.scope
    raw = scope.get("raw_path")
    path = raw.decode("latin-1") if raw else request.url.path
    query = scope.get("query_string", b"").decode("latin-1")
    return path + (f"?{query}" if query else "")


def _inbound_v1_zero(session_factory, window_days: int, now: datetime) -> bool:
    """True only when the durable witness CONFIRMS zero v1 across the window. A
    missing factory or an unreadable witness returns False (never a false
    "zero"), so a scheduled sunset date can never cut off v1 on an unproven
    signal — the request then falls through to the fail-closed ``_record_v1``
    write, which 503s if the DB is genuinely down."""
    if session_factory is None:
        return False
    try:
        with session_factory() as s:
            return hmac_witness.inbound_v1_zero(s, window_days, now)
    except Exception:  # noqa: BLE001 — an unreadable witness must not force a cutoff
        return False


def _inbound_secret(settings: Settings, key_id: str) -> str:
    """Resolve the v2 verification secret for a presented key id. FAILS CLOSED to '' on any
    malformed rotation mapping (re-audit `6feca36..4f23f23` F2): an unvalidated
    `model_copy(update={...: None})` used to raise AttributeError here, turning a signature check
    into a 500 instead of a controlled 401."""
    # EVERY operand is exact-type gated BEFORE it is compared, hashed, or looked up (re-audit
    # `4c3015a..cccd5f7` F1). The previous version gated the returned secret but still compared the
    # presented key id against `settings.hmac_inbound_key_id` with `==`, so a hostile `__eq__` on
    # either side ran attacker code during the comparison and escaped as a RuntimeError — a 500
    # where 401 is the honest answer. `isinstance` cannot help here: a `str` subclass passes it and
    # is exactly the thing being defended against.
    if type(key_id) is not str or not key_id:
        return ""
    active_id = settings.hmac_inbound_key_id
    if type(active_id) is str and key_id == active_id:
        active = settings.hmac_inbound_secret
        return active if type(active) is str else ""
    extra = settings.hmac_inbound_extra_keys
    if type(extra) is not dict:
        return ""
    # Not `extra.get(...)`: a hostile mapping's `get`/`__hash__`/`__eq__` would be dispatched by
    # the lookup itself. Walking items compares only values already proven to be exact `str`.
    for candidate_id, secret in extra.items():
        if type(candidate_id) is str and type(secret) is str and candidate_id == key_id:
            return secret
    return ""


def _session_factory(request):
    return getattr(request.app.state, "session_factory", None)


def _bump(key: str) -> None:
    """Diagnostic v2/rejected counter — process-local, NO database access (re-audit
    `d569a15..4938840` F1). Incrementing an in-memory counter cannot be turned into an
    unauthenticated write amplifier the way the previous session+commit could."""
    _DIAGNOSTIC_COUNTS[key] += 1


def diagnostic_counts() -> dict[str, int]:
    """Raw process-local diagnostic auth counters (v2_accepted | rejected)."""
    return dict(_DIAGNOSTIC_COUNTS)


def diagnostics_snapshot() -> dict:
    """Namespaced, self-describing auth-diagnostics block (re-audit `8aba2df..2cee937` R3-F2). The
    counters moved from durable fleet totals to process-local under the SAME key names, so exposing
    them bare let a legacy consumer read a per-replica value as the fleet total. This block carries
    an explicit scope, this process's identity + start epoch, and zero-filled keys, so it can only be
    read as what it is."""
    return {
        "scope": "process_local",
        "process_id": os.getpid(),
        "process_started_at": _PROCESS_STARTED_AT.isoformat(),
        "counts": {
            "v2_accepted": int(_DIAGNOSTIC_COUNTS.get("v2_accepted", 0)),  # zero-filled
            "rejected": int(_DIAGNOSTIC_COUNTS.get("rejected", 0)),
        },
    }


def _record_v1(session_factory) -> None:
    """FAIL-CLOSED: an accepted v1 request that cannot be witnessed is rejected,
    so real v1 traffic is never silently invisible to the sunset gate."""
    if session_factory is None:
        return
    try:
        with session_factory() as s:
            hmac_witness.record_v1_accepted(s)
            s.commit()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="v1 witness unavailable") from exc


def require_valid_signature(settings: Settings, request, body: bytes) -> None:
    if settings.auth_disabled:
        return
    headers = request.headers
    session_factory = _session_factory(request)
    # slot: idempotency key for event POSTs, else empty (reads/callbacks).
    slot = headers.get("Idempotency-Key", "")

    if "X-KYC-Signature-V2" in headers or "X-KYC-Key-Id" in headers:
        # v2 asserted by PRESENCE of any v2 header ⇒ v2-only, no fallback to the
        # path-unbound v1 scheme — a present-but-empty v2 header still locks v2
        # (the contract is header presence, not a truthy value).
        key_id = headers.get("X-KYC-Key-Id", "")
        secret = _inbound_secret(settings, key_id)
        path_qs = _raw_path_qs(request)
        ok = bool(secret) and security.verify_v2(
            secret,
            headers.get("X-KYC-Signature-V2", ""),
            max_skew_seconds=settings.hmac_max_skew_seconds,
            key_id=key_id,
            direction=security.DIRECTION_INBOUND,
            method=request.method,
            path_qs=path_qs,
            timestamp=headers.get("X-KYC-Timestamp", ""),
            slot=slot,
            body=body,
        )
        if not ok:
            _bump("rejected")
            raise HTTPException(status_code=401, detail="invalid v2 signature")
        _bump("v2_accepted")
        return

    # v1 path. VERIFY THE SIGNATURE BEFORE ANY DATABASE ACCESS (re-audit `8aba2df..2cee937` R3-F1):
    # the durable zero-witness read must never be reachable by an unauthenticated caller. Previously,
    # once the sunset date had passed, an unsigned/invalid v1 request drove the witness SELECT (and
    # even received "retired" without its signature being checked) — an unauthenticated DB-availability
    # amplifier that survived the F1 diagnostics fix. So: reject an unconfigured secret, then verify;
    # an invalid signature is refused with ZERO DB access.
    # Exact-type gate the legacy secret before it is truth-tested or hashed. An int reached
    # `sign`'s `.encode()` and raised AttributeError (re-audit `4c3015a..cccd5f7` F1); a `str`
    # subclass would run its own `__bool__` on the line below. A secret that is not exactly a
    # string is not a configured secret.
    legacy_secret = settings.platform_hmac_secret
    if type(legacy_secret) is not str or not legacy_secret:
        raise HTTPException(status_code=401, detail="authentication not configured")
    if not security.verify(
        legacy_secret,
        headers.get("X-KYC-Timestamp", ""),
        body,
        headers.get("X-KYC-Signature", ""),
        max_skew_seconds=settings.hmac_max_skew_seconds,
    ):
        _bump("rejected")
        raise HTTPException(status_code=401, detail="invalid signature")
    # Cryptographically valid v1 past this point. v1 is retired only when the sunset date has passed
    # AND the durable witness confirms zero v1 across the observation window — the date alone must
    # never cut off live v1 traffic (ADR-003 / DEPLOYMENT §2): a scheduled date takes effect only once
    # the witness is green, so the operator's activation + zero-window is load-bearing, not decorative.
    # Only a valid request consults the witness, and only a valid non-retired request is recorded.
    now = datetime.now(UTC)
    if _sunset_passed(settings.hmac_v1_inbound_sunset_at, now) and _inbound_v1_zero(
        session_factory, settings.hmac_v1_observation_window_days, now
    ):
        raise HTTPException(status_code=401, detail="v1 signatures retired (inbound sunset)")
    _record_v1(session_factory)  # FAIL-CLOSED witness write


def require_read_access(settings: Settings, request, body: bytes = b"") -> None:
    """Gate a read endpoint. Off by default (open in dev/test); when
    read_auth_required is set — which validate_for_production() forces in
    production — the caller must present a valid platform signature. Reads carry
    an empty v2 slot (no idempotency key)."""
    if not settings.read_auth_required:
        return
    require_valid_signature(settings, request, body)


def require_admin(settings: Settings, headers) -> None:
    """Gate a mutating ops-console endpoint with the operator bearer token.

    `Authorization: Bearer <ui_admin_token>`, constant-time compared. When no
    token is configured the console stays open — production forbids
    ui_enabled without a token (validate_for_production), so an empty token can
    only mean dev/test, matching the console's prior local trust model."""
    if settings.auth_disabled:
        return
    token = settings.ui_admin_token
    if not token:
        return
    header = headers.get("Authorization", "")
    provided = header[7:] if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(provided, token):
        raise HTTPException(status_code=401, detail="invalid or missing admin credential")
