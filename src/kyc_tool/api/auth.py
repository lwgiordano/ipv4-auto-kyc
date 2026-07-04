"""Inbound request authentication — HMAC signed bearer (04 §5).

Headers: X-KYC-Timestamp (unix seconds) and X-KYC-Signature
(hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")).
"""

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
