"""Shared HMAC request signing (platform ↔ tool, both directions).

Scheme (04 §5 'signed bearer'): signature = hex(HMAC_SHA256(secret,
f"{timestamp}.{raw_body}")). Timestamp must be within the configured skew.
"""

import hashlib
import hmac
import math
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
    if not math.isfinite(ts):  # 'nan'/'inf' parse fine but defeat the skew check
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature or "")


# -- v2: path-bound canonical signing (PR 5a) --------------------------------
#
# v1 signs only "{timestamp}.{body}", so case_id (in the URL path) is unsigned
# and a captured signature can be redirected to another case. v2 binds the
# method, full path+query, direction, key_id, timestamp, and idempotency slot
# into the signed value, closing that redirect.

DIRECTION_INBOUND = "platform->tool"
DIRECTION_OUTBOUND = "tool->platform"
_V2_DIRECTIONS = frozenset({DIRECTION_INBOUND, DIRECTION_OUTBOUND})


def canonical_v2(
    *,
    key_id: str,
    direction: str,
    method: str,
    path_qs: str,
    timestamp: str,
    slot: str,
    body: bytes,
) -> str:
    """The exact newline-joined value that gets HMAC'd. `slot` is the
    idempotency key for event POSTs, else empty (reads/callbacks)."""
    if direction not in _V2_DIRECTIONS:
        raise ValueError(f"bad direction {direction!r}")
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join(["v2", key_id, direction, method, path_qs, timestamp, slot, body_hash])


def sign_v2(secret: str, **fields) -> str:
    message = canonical_v2(**fields).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_v2(
    secret: str,
    signature: str,
    *,
    max_skew_seconds: int = 300,
    now: float | None = None,
    **fields,
) -> bool:
    try:
        ts = float(fields["timestamp"])
    except (TypeError, ValueError, KeyError):
        return False
    if not math.isfinite(ts):  # 'nan'/'inf' parse but defeat the skew check
        return False
    current = time.time() if now is None else now
    if abs(current - ts) > max_skew_seconds:
        return False
    try:
        expected = sign_v2(secret, **fields)
    except (ValueError, KeyError):
        return False
    return hmac.compare_digest(expected, signature or "")
