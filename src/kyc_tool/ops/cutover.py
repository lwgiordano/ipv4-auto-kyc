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
# The CLOSED set of publisher roles (re-audit `03dbfab..bc325e7` R5-F9): a third "mystery_worker"
# consistently copied into every role-bearing phase must NOT validate.
_ALLOWED_ROLES = frozenset({"outbox_worker", "dev_worker"})
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
    # EXACT closed role set (not "contains both"): an extra/aliased role is refused.
    if set(record.publisher_roles) != _ALLOWED_ROLES:
        problems.append(
            f"publisher_roles {list(record.publisher_roles)} is not the exact closed set "
            f"{sorted(_ALLOWED_ROLES)}"
        )
    if len(set(record.publisher_roles)) != len(record.publisher_roles):
        problems.append("publisher_roles has a duplicated/aliased role")

    actions = tuple(p.action for p in record.phases)
    if actions != CUTOVER_SEQUENCE:
        problems.append(
            f"phase sequence {[a.value for a in actions]} is not the required exact-once, "
            f"in-order {[a.value for a in CUTOVER_SEQUENCE]}"
        )
        return problems  # ordering is the spine; role/setting checks below assume it holds

    # Discriminated phase fields (re-audit R5-F9): a role-bearing phase names EXACTLY the roles and
    # carries no setting; attest_new_value names EXACTLY the setting and carries no roles; the
    # disable_restart phase carries neither. Inapplicable fields are forbidden, not ignored.
    for phase in record.phases:
        role_bearing = phase.action in _ROLE_BEARING
        is_new_value = phase.action is CutoverAction.ATTEST_NEW_VALUE
        if role_bearing and tuple(phase.roles) != record.publisher_roles:
            problems.append(
                f"{phase.action.value} names roles {list(phase.roles)}, not the exact "
                f"publisher roles {list(record.publisher_roles)}"
            )
        if not role_bearing and phase.roles:
            problems.append(f"{phase.action.value} must not carry roles ({list(phase.roles)})")
        if is_new_value and phase.setting != record.setting:
            problems.append(
                f"attest_new_value names {phase.setting!r}, not the cutover setting {record.setting!r}"
            )
        if not is_new_value and phase.setting is not None:
            problems.append(f"{phase.action.value} must not carry a setting ({phase.setting!r})")
    return problems


def attest_new_value(record: DrainedCutover, *, target: int, observed: dict[str, object]) -> list[str]:
    """Executable attestation for the attest_new_value phase (re-audit `03dbfab..bc325e7` R5-F2). The
    record names only the SETTING; a fleet whose every task carries the variable but the OLD value, or
    whose two roles disagree, would otherwise be attested and started. `observed` maps each publisher
    role to the value its new task definition carries (None ⇒ variable absent/unobserved). Returns
    violations (empty ⇒ every role's new task carries exactly `target`, a valid outbox_max_attempts)."""
    from kyc_tool.config import numeric_domain_of, numeric_value_violation

    problems: list[str] = []
    bad_target = numeric_value_violation(numeric_domain_of("outbox_max_attempts"), target)
    if bad_target:
        problems.append(f"reviewed target is invalid: {bad_target}")
    for role in sorted(_ALLOWED_ROLES):
        if role not in observed:
            problems.append(f"no task definition observed for role {role!r} (missing task)")
        elif observed[role] is None:
            problems.append(f"role {role!r} task does not carry {record.setting} (unobserved value)")
        elif observed[role] != target:
            problems.append(
                f"role {role!r} carries {record.setting}={observed[role]!r}, not the reviewed "
                f"target {target!r} (stale value or per-role disagreement)"
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
    starts = [i for i, line in enumerate(lines) if start in line]
    ends = [i for i, line in enumerate(lines) if end in line]
    # EXACTLY one block (re-audit `03dbfab..bc325e7` R5-F9): a safe first block followed by a second,
    # unsafe marked procedure must NOT be silently accepted by matching only the first markers.
    if len(starts) != 1 or len(ends) != 1:
        raise ValueError(
            f"surface must contain exactly one {start!r}..{end!r} block "
            f"(found {len(starts)} start / {len(ends)} end markers)"
        )
    si, ei = starts[0], ends[0]
    if ei <= si:
        raise ValueError(f"surface has a malformed {start!r}..{end!r} block (end before start)")
    out: list[str] = []
    for raw in lines[si + 1 : ei]:
        line = raw.strip()
        if line.startswith("#"):
            line = line[1:].strip()  # .env comment prefix
        if line:
            out.append(line)
    return out
