"""The HMAC v2 reference implementation published to TechCraft, as EXECUTABLE code.

This module is the source the PDF prints. It is imported and run by the contract tests against
`kyc_tool.security.sign_v2`, so the snippet a reader copies is the snippet that was proven to
produce the published digest (re-audit `82636da..9ac574f` F9: the previous version printed a
hand-written snippet nobody ever executed, and printed a body that was never bound to the printed
hash).

Deliberately dependency-free and standard-library only: a reader must be able to paste it into any
Python 3 process. Everything below the marker is what the document embeds verbatim.
"""

import hashlib
import hmac

# --- BEGIN PUBLISHED SNIPPET ---
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
# --- END PUBLISHED SNIPPET ---


_BEGIN = "# --- BEGIN PUBLISHED SNIPPET ---"
_END = "# --- END PUBLISHED SNIPPET ---"


def published_snippet() -> str:
    """The snippet source, read from THIS file between the markers, so the document can never
    drift from the code the tests execute."""
    from pathlib import Path

    text = Path(__file__).read_text()
    start = text.index(_BEGIN) + len(_BEGIN)
    return text[start : text.index(_END)].strip("\n")
