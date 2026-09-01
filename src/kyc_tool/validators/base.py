"""Validator contract — stage 2 of fetch → validate → record. PURE.

A validator turns (normalized evidence, submitted data, context) into check
intents with reason codes. No I/O: anything a validator needs from the
database (e.g. POC token records) is fetched by orchestration and passed in
via `ValidationContext`.
"""

from dataclasses import dataclass, field

from kyc_tool.domain.models import CheckStatus


@dataclass(frozen=True, slots=True)
class CheckIntent:
    """What the check store should record for one piece of evidence."""

    check_type: str
    status: CheckStatus
    reason_codes: tuple[str, ...] = ()
    source: str = ""
    source_detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Read-only inputs orchestration assembles for the pure validators."""

    case_snapshot: dict
    event_type: str
    event_payload: dict
    adapter_outputs: dict[str, dict]  # adapter_id -> normalized (status ok only)
    live_checks: tuple = ()  # domain CheckView tuple (cross-check/conflict detection)
    extras: dict = field(default_factory=dict)  # e.g. poc token record, review task
