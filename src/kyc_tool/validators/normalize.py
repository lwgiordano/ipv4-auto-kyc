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


def domain_of(value: str | None) -> str:
    """Extract a bare lowercase domain from an email, URL, or naked host."""
    if not value:
        return ""
    value = value.strip().lower()
    if "@" in value:
        return value.rsplit("@", 1)[1]
    if "//" in value:
        return urlparse(value).hostname or ""
    return value.split("/", 1)[0]
