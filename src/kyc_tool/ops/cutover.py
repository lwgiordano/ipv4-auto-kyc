"""Canonical cutover records for operationally-sensitive config changes (re-audit `8aba2df..2cee937`
R3-F5/F6, hardened under `5b0f0b8..b75a320` R4-F5).

The rule is a CLOSED phase machine, not free-form strings: `validate_cutover` requires the actions to
be exactly `disable_restart -> stop -> attest_zero -> attest_new_value -> start`, each exactly once and
in order, with the role-bearing phases naming the exact publisher roles and `attest_new_value` naming
the exact setting. A rewrite that deletes the stop step, drops the new-value attestation, moves the
zero attestation before the stop, or changes the last phase to "start no publishers" is refused —
none of those is expressible as a valid machine.

`render_cutover` renders the record to a canonical, marker-delimited block; the operator surfaces
(DEPLOYMENT, RUNBOOK, .env.example) embed that exact block, and the guard extracts each surface's block
and compares it to the render — so the docs are derived from the record, not matched by file-wide token
presence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CutoverAction(Enum):
    DISABLE_RESTART = "disable_restart"  # autoscaling/rolling restart off for the window
    STOP = "stop"  # stop ALL publishers of the exact roles
    ATTEST_ZERO = "attest_zero"  # attest zero publishers of the exact roles are running
    ATTEST_NEW_VALUE = "attest_new_value"  # attest every new task def carries the exact value
    START = "start"  # start publishers of the exact roles — LAST


# The one legal ordering. `validate_cutover` requires exactly this, each action once.
CUTOVER_SEQUENCE: tuple[CutoverAction, ...] = (
    CutoverAction.DISABLE_RESTART,
    CutoverAction.STOP,
    CutoverAction.ATTEST_ZERO,
    CutoverAction.ATTEST_NEW_VALUE,
    CutoverAction.START,
)
_ROLE_BEARING = {CutoverAction.STOP, CutoverAction.ATTEST_ZERO, CutoverAction.START}
_MARKER = "cutover"  # surfaces delimit the rendered block with "<marker>:<setting>:start|end"


@dataclass(frozen=True)
class CutoverPhase:
    action: CutoverAction
    roles: tuple[str, ...] = ()  # for stop/attest_zero/start
    setting: str | None = None  # for attest_new_value


@dataclass(frozen=True)
class DrainedCutover:
    """A config change that is NOT safe under a rolling restart, as a closed phase machine."""

    setting: str
    both_directions: bool  # raise AND lower both require the drained cutover
    publisher_roles: tuple[str, ...]  # EVERY role that runs a publisher
    phases: tuple[CutoverPhase, ...] = field(default_factory=tuple)


# Each publisher enforces the ceiling it STARTED with, so an old and a new publisher run different
# ceilings against the same rows during any rolling restart: lowering lets the old (higher) publisher
# send once past the new value; raising lets the old (lower) publisher dead-letter — and irreversibly
# redact a POC token — before the new (higher) one supplies the extra attempts. Hence: EITHER
# direction, drained.
OUTBOX_MAX_ATTEMPTS_CUTOVER = DrainedCutover(
    setting="KYC_OUTBOX_MAX_ATTEMPTS",
    both_directions=True,
    publisher_roles=("outbox_worker", "dev_worker"),
    phases=(
        CutoverPhase(CutoverAction.DISABLE_RESTART),
        CutoverPhase(CutoverAction.STOP, roles=("outbox_worker", "dev_worker")),
        CutoverPhase(CutoverAction.ATTEST_ZERO, roles=("outbox_worker", "dev_worker")),
        CutoverPhase(CutoverAction.ATTEST_NEW_VALUE, setting="KYC_OUTBOX_MAX_ATTEMPTS"),
        CutoverPhase(CutoverAction.START, roles=("outbox_worker", "dev_worker")),
    ),
)


def validate_cutover(record: DrainedCutover) -> list[str]:
    """Return the record's violations (empty ⇒ valid). This is the CONSUMER the guard mutation-tests:
    every corruption below must produce at least one violation."""
    problems: list[str] = []
    if not record.both_directions:
        problems.append("both_directions must be true — a raise is not rolling-safe either")
    if "outbox_worker" not in record.publisher_roles or "dev_worker" not in record.publisher_roles:
        problems.append("publisher_roles must name every publisher role (outbox_worker, dev_worker)")
    if len(set(record.publisher_roles)) != len(record.publisher_roles):
        problems.append("publisher_roles has a duplicated/aliased role")

    actions = tuple(p.action for p in record.phases)
    if actions != CUTOVER_SEQUENCE:
        problems.append(
            f"phase sequence {[a.value for a in actions]} is not the required exact-once, "
            f"in-order {[a.value for a in CUTOVER_SEQUENCE]}"
        )
        return problems  # ordering is the spine; role/setting checks below assume it holds

    for phase in record.phases:
        if phase.action in _ROLE_BEARING and tuple(phase.roles) != record.publisher_roles:
            problems.append(
                f"{phase.action.value} names roles {list(phase.roles)}, not the exact "
                f"publisher roles {list(record.publisher_roles)}"
            )
        if phase.action is CutoverAction.ATTEST_NEW_VALUE and phase.setting != record.setting:
            problems.append(
                f"attest_new_value names {phase.setting!r}, not the cutover setting {record.setting!r}"
            )
    return problems


_PHRASE = {
    CutoverAction.DISABLE_RESTART: "disable autoscaling and rolling restart",
    CutoverAction.STOP: "stop ALL publishers of roles: {roles}",
    CutoverAction.ATTEST_ZERO: "attest zero publishers running of roles: {roles}",
    CutoverAction.ATTEST_NEW_VALUE: "attest every new task definition carries {setting}",
    CutoverAction.START: "start publishers of roles: {roles}",
}


def render_cutover(record: DrainedCutover) -> list[str]:
    """Canonical ordered lines rendered FROM the record. The operator surfaces embed exactly these
    (comment-prefixed as needed); the guard compares each surface's marked block to this output."""
    lines: list[str] = [
        f"{record.setting}: both-direction DRAINED publisher cutover (NOT a rolling restart)"
    ]
    for i, phase in enumerate(record.phases, 1):
        text = _PHRASE[phase.action].format(
            roles=", ".join(phase.roles), setting=phase.setting or ""
        )
        lines.append(f"{i}. {text}")
    return lines


def marker(record: DrainedCutover, edge: str) -> str:
    """The start/end marker line a surface uses to delimit the rendered block."""
    return f"{_MARKER}:{record.setting}:{edge}"


def extract_block(surface_text: str, record: DrainedCutover) -> list[str]:
    """Pull the record's marked block out of a surface (the content lines strictly between the start
    and end marker lines, with a leading comment `#` stripped) so it can be compared to
    render_cutover. Raises if the markers are absent or malformed."""
    start, end = marker(record, "start"), marker(record, "end")
    lines = surface_text.splitlines()
    si = next((i for i, line in enumerate(lines) if start in line), None)
    ei = next((i for i, line in enumerate(lines) if end in line), None)
    if si is None or ei is None or ei <= si:
        raise ValueError(f"surface is missing a well-formed {start!r}..{end!r} block")
    out: list[str] = []
    for raw in lines[si + 1 : ei]:
        line = raw.strip()
        if line.startswith("#"):
            line = line[1:].strip()  # .env comment prefix
        if line:
            out.append(line)
    return out
