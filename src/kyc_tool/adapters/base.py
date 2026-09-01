"""Adapter contract — stage 1 of fetch → validate → record.

Adapters ONLY fetch and normalize. They never touch the database (enforced by
import-linter), never judge evidence, and always surface the untouched
upstream response so orchestration can archive it before normalization is
trusted. Side effects that belong to an adapter's semantics (review tasks, POC
tokens) are DESCRIBED in `normalized` and performed by orchestration.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from kyc_tool.domain.models import AdapterStatus


@dataclass(frozen=True, slots=True)
class AdapterOutput:
    adapter_id: str
    status: AdapterStatus
    raw: bytes | None = None  # untouched upstream bytes (archived before use)
    normalized: dict = field(default_factory=dict)
    error: str | None = None


class Adapter(Protocol):
    adapter_id: str

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        """Hash of the inputs this adapter actually reads — the resumability
        key: a retried RUN_ADAPTERS stage skips (run, adapter, hash) triples
        already recorded."""
        ...

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput: ...


def hash_inputs(*parts: object) -> str:
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode()
    ).hexdigest()
