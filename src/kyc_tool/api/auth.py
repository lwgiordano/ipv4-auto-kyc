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
from datetime import UTC, datetime

from fastapi import HTTPException

from kyc_tool import security
from kyc_tool.api import hmac_witness
from kyc_tool.config import Settings, parse_sunset


def _sunset_passed(iso: str, now: datetime) -> bool:
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
    if key_id and key_id == settings.hmac_inbound_key_id:
        return settings.hmac_inbound_secret
    return settings.hmac_inbound_extra_keys.get(key_id, "")


def _session_factory(request):
    return getattr(request.app.state, "session_factory", None)


def _bump(session_factory, key: str) -> None:
    """Diagnostic v2/rejected counter — best-effort (never blocks the request)."""
    if session_factory is None:
        return
    try:
        with session_factory() as s:
            hmac_witness.bump_stat(s, key)
            s.commit()
    except Exception:  # noqa: BLE001 — diagnostics must never fail the request
        pass


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
            _bump(session_factory, "rejected")
            raise HTTPException(status_code=401, detail="invalid v2 signature")
        _bump(session_factory, "v2_accepted")
        return

    # v1 path — retired only when the sunset date has passed AND the durable
    # witness confirms zero v1 across the observation window. The date alone must
    # never cut off live v1 traffic (ADR-003 / DEPLOYMENT §2): a scheduled date
    # takes effect only once the witness is green, so the operator's activation +
    # zero-window is load-bearing, not decorative.
    now = datetime.now(UTC)
    if _sunset_passed(settings.hmac_v1_inbound_sunset_at, now) and _inbound_v1_zero(
        session_factory, settings.hmac_v1_observation_window_days, now
    ):
        raise HTTPException(status_code=401, detail="v1 signatures retired (inbound sunset)")
    if not settings.platform_hmac_secret:
        raise HTTPException(status_code=401, detail="authentication not configured")
    if not security.verify(
        settings.platform_hmac_secret,
        headers.get("X-KYC-Timestamp", ""),
        body,
        headers.get("X-KYC-Signature", ""),
        max_skew_seconds=settings.hmac_max_skew_seconds,
    ):
        _bump(session_factory, "rejected")
        raise HTTPException(status_code=401, detail="invalid signature")
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
