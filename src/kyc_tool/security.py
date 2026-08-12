"""Shared HMAC request signing (platform ↔ tool, both directions).

Scheme (04 §5 'signed bearer'): signature = hex(HMAC_SHA256(secret,
f"{timestamp}.{raw_body}")). Timestamp must be within the configured skew.
"""

import hashlib
import hmac
import math
import time

# The ONE governed signed-request replay window (PLATFORM_INTEGRATION §skew). `config` imports this
# for the setting domain; `verify`/`verify_v2` fail closed on any skew outside (0, MAX] so a widened
# window cannot enlarge the acceptance window even on a direct call (re-audit `5b0f0b8..b75a320`
# R4-F1, consumer layer).
MAX_HMAC_SKEW_SECONDS = 300


def _skew_in_contract(max_skew_seconds) -> bool:
    # EXACT type first, before any comparison (gate finding 2). `isinstance` admitted an `int`
    # SUBCLASS, and the very next line compared it — dispatching the subclass's `__ge__`/`__le__`,
    # which raised out of verification as a 500. `type(x) is int` also subsumes the old bool
    # exclusion, since `bool` is a subclass and no longer passes.
    if type(max_skew_seconds) is not int:
        return False
    return 1 <= max_skew_seconds <= MAX_HMAC_SKEW_SECONDS


def sign(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def _exact_text(*values) -> bool:
    """True only if every value is an EXACT built-in `str`.

    `isinstance` is not sufficient at a verification boundary (re-audit `4c3015a..cccd5f7` F1). A
    `str` SUBCLASS satisfies isinstance while overriding `__eq__`, `__hash__`, `encode` or `strip`,
    so the very act of checking it dispatches to attacker-supplied code — and an exception raised
    there escapes as a 500 on a request that deserved a controlled 401. Exact-type gating is the
    only form that cannot be subverted by the object being gated.
    """
    return all(type(value) is str for value in values)


def verify(
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    max_skew_seconds: int = MAX_HMAC_SKEW_SECONDS,
    now: float | None = None,
) -> bool:
    # Defense in depth: the caller is expected to gate these, and this function refuses anyway. A
    # verification primitive that trusts its inputs makes every future caller a potential 500.
    if not _exact_text(secret, timestamp, signature) or type(body) is not bytes:
        return False
    if not _skew_in_contract(max_skew_seconds):  # fail closed on an out-of-contract window
        return False
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
    max_skew_seconds: int = MAX_HMAC_SKEW_SECONDS,
    now: float | None = None,
    **fields,
) -> bool:
    # Every canonical field is gated before it reaches `canonical_v2`, which joins them and hashes
    # the body. A hostile `encode` on any one of them raised RuntimeError out of `sign_v2` and
    # turned signature verification into a 500 (re-audit `4c3015a..cccd5f7` F1).
    if not _exact_text(secret, signature):
        return False
    if set(fields) != {"key_id", "direction", "method", "path_qs", "timestamp", "slot", "body"}:
        return False  # an unexpected or missing field would reach canonical_v2 as a TypeError
    if not _exact_text(*(fields[name] for name in
                         ("key_id", "direction", "method", "path_qs", "timestamp", "slot"))):
        return False
    if type(fields["body"]) is not bytes:
        return False
    if not _skew_in_contract(max_skew_seconds):  # fail closed on an out-of-contract window
        return False
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
