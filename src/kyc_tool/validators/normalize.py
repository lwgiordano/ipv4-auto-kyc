"""Deterministic normalization for exact matching.

The spec allows normalization "for case/punctuation, not fuzzy": lowercase,
strip punctuation, collapse whitespace. Nothing else — no abbreviation
expansion, no edit distance, no token reordering.
"""

import re
from urllib.parse import urlparse

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def norm(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    no_punct = _PUNCT.sub(" ", lowered)
    return _WS.sub(" ", no_punct).strip()


def norm_equal(a: str | None, b: str | None) -> bool:
    return norm(a) != "" and norm(a) == norm(b)


def canon_id(value: str | None) -> str:
    """Canonicalize an identifier (RIR org handle, POC handle) for exact
    comparison: case- and whitespace-insensitive, no punctuation collapsing."""
    return (value or "").strip().lower()


def domain_of(value: str | None) -> str:
    """Extract a bare lowercase domain from an email, URL, or naked host.

    A leading `www.` is a host label, not part of the registrable domain, and it can only exist
    under the registrant of that domain — so `www.acme.example` and `acme.example` are the same
    domain here: an equivalence class one party controls, which is what keeps it exact rather
    than fuzzy.
    """
    if not value:
        return ""
    value = value.strip().lower()
    if "@" in value:
        host = value.rsplit("@", 1)[1]
    elif "//" in value:
        host = urlparse(value).hostname or ""
    else:
        host = value.split("/", 1)[0]
    return host[4:] if host.startswith("www.") else host
