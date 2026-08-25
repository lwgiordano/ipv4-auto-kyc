"""Reference HMAC-v2 signer for the KYC tool's platform integration.

Shipped alongside the Platform Integration Contract. This file is the authoritative copy — the
code printed in the PDF is an illustration of it, and a PDF is not a reliable clipboard for
indentation-sensitive source.

Verify before use: run `shasum -a 256 kyc-signer-example.py` and compare the result against the
sha256 printed beside this file's name in the Platform Integration Contract, section 4. This
file deliberately does not state its own digest: a digest cannot cover the text that quotes it,
and an earlier version that tried printed a value the documented command could never produce.

The tool's own test suite executes these exact bytes against the published test vector and
asserts they reproduce the published signature, so a mismatch means the file was altered in
transit, not that the algorithm changed.
"""

import hashlib
import hmac

DIRECTION_PLATFORM_TO_TOOL = "platform->tool"
DIRECTION_TOOL_TO_PLATFORM = "tool->platform"


def canonical_string(
    *, key_id: str, direction: str, method: str, path_qs: str, timestamp: str,
    slot: str, body: bytes,
) -> str:
    """The exact value that gets signed: eight LF-joined lines, in this order.

    `slot` is the Idempotency-Key on event POSTs and "" everywhere else.
    `path_qs` is the raw request target, path plus query, undecoded.
    """
    return "\n".join([
        "v2",
        key_id,
        direction,
        method,
        path_qs,
        timestamp,
        slot,
        hashlib.sha256(body).hexdigest(),
    ])


def sign(secret: str, **fields) -> str:
    """Hex HMAC-SHA256 over the canonical string. Send as X-KYC-Signature-V2."""
    return hmac.new(
        secret.encode(), canonical_string(**fields).encode(), hashlib.sha256
    ).hexdigest()
