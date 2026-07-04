"""Shared HMAC request signing (platform ↔ tool, both directions).

Scheme (04 §5 'signed bearer'): signature = hex(HMAC_SHA256(secret,
f"{timestamp}.{raw_body}")). Timestamp must be within the configured skew.
"""

import hashlib
import hmac
import time


def sign(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    max_skew_seconds: int = 300,
    now: float | None = None,
) -> bool:
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature or "")
