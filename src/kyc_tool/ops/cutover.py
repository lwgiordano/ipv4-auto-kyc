"""Canonical, machine-readable cutover records for operationally-sensitive config changes (re-audit
`8aba2df..2cee937` R3-F5/F6).

The operator surfaces (DEPLOYMENT, RUNBOOK, .env.example) mechanically match THIS record, and the
guard validates the record's invariants and mutation-tests `validate_cutover` here — so a doc rewrite
that preserves the asserted words but flips the rule ("raising may use a rolling restart") cannot
pass, because the guard checks the structured record, not file-wide tokens.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrainedCutover:
    """A config change that is NOT safe under a rolling restart. `steps` is ordered; starting
    publishers must be the last step, strictly after the zero-attestation and new-value attestation."""

    setting: str
    both_directions: bool  # raise AND lower both require the drained cutover
    disable_autoscale: bool  # rolling restart / autoscale must be disabled for the window
    publisher_roles: tuple[str, ...]  # EVERY role that runs an outbox publisher — all must stop
    attest_new_value: bool  # every new task definition must carry the exact new value before start
    steps: tuple[str, ...]


# Each publisher enforces the ceiling it STARTED with, so an old and a new publisher run different
# ceilings against the same rows during any rolling restart: lowering lets the old (higher) publisher
# send once past the new value; raising lets the old (lower) publisher dead-letter — and irreversibly
# redact a POC token — before the new (higher) one supplies the extra attempts. Hence: EITHER
# direction, drained.
OUTBOX_MAX_ATTEMPTS_CUTOVER = DrainedCutover(
    setting="KYC_OUTBOX_MAX_ATTEMPTS",
    both_directions=True,
    disable_autoscale=True,
    publisher_roles=("outbox_worker", "dev_worker"),
    attest_new_value=True,
    steps=(
        "disable autoscaling and rolling restart",
        "stop ALL outbox publishers of every role",
        "attest zero publishers are running",
        "attest every new task definition carries the exact new value",
        "start publishers on the new value",
    ),
)


def validate_cutover(record: DrainedCutover) -> list[str]:
    """Return the record's invariant violations (empty ⇒ valid). This is the CONSUMER the guard
    mutation-tests: each deliberate corruption below must produce at least one violation."""
    problems: list[str] = []
    if not record.both_directions:
        problems.append("both_directions must be true — a raise is not rolling-safe either")
    if not record.disable_autoscale:
        problems.append("disable_autoscale must be true — a rolling restart overlaps two ceilings")
    if not record.attest_new_value:
        problems.append("attest_new_value must be true — a stale task definition reintroduces drift")
    roles = record.publisher_roles
    if "outbox_worker" not in roles or "dev_worker" not in roles:
        problems.append("publisher_roles must name every publisher role (outbox_worker, dev_worker)")
    if len(set(roles)) != len(roles):
        problems.append("publisher_roles has a duplicated/aliased role")
    joined = " | ".join(record.steps).lower()
    start_idx = next((i for i, s in enumerate(record.steps) if s.lower().startswith("start")), -1)
    attest_zero_idx = next((i for i, s in enumerate(record.steps) if "attest zero" in s.lower()), -1)
    if start_idx == -1:
        problems.append("steps must include a start step")
    elif start_idx != len(record.steps) - 1:
        problems.append("the start step must be LAST")
    if attest_zero_idx == -1:
        problems.append("steps must attest zero publishers are running before start")
    elif start_idx != -1 and start_idx < attest_zero_idx:
        problems.append("start must come AFTER the zero attestation, not before")
    if "disable" not in joined or ("autoscal" not in joined and "rolling restart" not in joined):
        problems.append("steps must disable autoscaling/rolling restart")
    return problems
