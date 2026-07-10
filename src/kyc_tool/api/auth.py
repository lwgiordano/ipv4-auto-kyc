"""Inbound request authentication — HMAC signed bearer (04 §5).

Headers: X-KYC-Timestamp (unix seconds) and X-KYC-Signature
(hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")).
"""

import hmac

from fastapi import HTTPException

from kyc_tool import security
from kyc_tool.config import Settings


def require_valid_signature(settings: Settings, headers, body: bytes) -> None:
    if settings.auth_disabled:
        return
    if not settings.platform_hmac_secret:
        # fail closed: no secret configured and auth not explicitly disabled
        raise HTTPException(status_code=401, detail="authentication not configured")
    timestamp = headers.get("X-KYC-Timestamp", "")
    signature = headers.get("X-KYC-Signature", "")
    if not security.verify(
        settings.platform_hmac_secret,
        timestamp,
        body,
        signature,
        max_skew_seconds=settings.hmac_max_skew_seconds,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")


def require_read_access(settings: Settings, headers, body: bytes = b"") -> None:
    """Gate a read endpoint. Off by default (open in dev/test); when
    read_auth_required is set — which validate_for_production() forces in
    production — the caller must present a valid platform signature."""
    if not settings.read_auth_required:
        return
    require_valid_signature(settings, headers, body)


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
