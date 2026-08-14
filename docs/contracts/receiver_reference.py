"""An executable implementation of the published receiver transition table.

Same discipline as `signing_example.py`: the document publishes a table, and this is the algorithm
that table describes, driven by scenario tests. A table nobody runs is prose in a grid — the
"otherwise apply" defect (re-audit `4f23f23..97deeae` F2) survived review precisely because it read
correctly and was never executed against a late-arriving older decision.

`tests/unit/test_receiver_state_machine.py` asserts two things: that this function reproduces every
scenario the findings describe, and that its branches correspond one-to-one, in order, with the rows
of `WIRE.CALLBACK.EFFECTIVENESS`. Neither can drift without the other failing.

This is NOT shipped inside the tool. The receiver is TechCraft's; this is our statement of what we
believe correct, in a form they can run.
"""

from dataclasses import dataclass

from docs.contracts import predicates
from docs.contracts.wire import (
    INTERIM,
    KNOWN_SOURCES,
    POST_024,
    RECEIVER_TRANSITIONS,
    SOURCE_MANUAL,
)


class UnknownSourceError(ValueError):
    """The receiver reported an effective source outside the closed set. Hold, never apply."""


@dataclass(frozen=True)
class LedgerState:
    """What the receiver knows about one case before this callback arrives."""

    seen_run_ids: frozenset[str] = frozenset()
    current_source: str | None = None  # "manual", "automatic", or None when nothing is effective
    high_water: int | None = None  # post-024 only; a mark over decision_sequence


@dataclass(frozen=True)
class Callback:
    """One delivered decision callback.

    TWO ordinals, and only one of them orders decisions (re-audit `4f23f23..122cc67` finding 1).
    `event_sequence` is PR 2 ingest provenance — the per-case ordinal of the triggering event —
    and it is on the wire today. `decision_sequence` is the PR 7b callback-order authority and
    arrives with the activation unit. They diverge whenever work finishes out of admission order,
    which is ordinary: an event admitted first can decide second, so their ordinals invert.
    Ordering by `event_sequence` therefore suppresses the LATER decision as stale.
    """

    case_id: str
    run_id: str
    decision_sequence: int | None = None  # the ordering authority; arrives with 024
    event_sequence: int | None = None  # ingest provenance; NEVER an ordering authority


@dataclass(frozen=True)
class Outcome:
    row: int  # index into the phase's rows — the published table row that decided this
    record: bool  # append to the accepted-run ledger
    effective: bool  # becomes the case's current decision
    advance_high_water: bool


class TableIntegrityError(RuntimeError):
    """The table stopped being a partition: a state matched zero rows or several. The receiver
    must HOLD on ambiguity, never pick — which answer wins would otherwise be an implementation
    accident, and that accident is the F10 defect itself."""


def observe(state: LedgerState, callback: Callback) -> dict:
    """Reduce (state, callback) to the closed facet observation the predicates match against.

    FAIL CLOSED on an unrecognised source (re-audit `4f23f23..122cc67` finding 2), BEFORE any row
    is consulted: `manual_release_pending`, `MANUAL`, a typo — a source you cannot classify is
    precisely the case where you do not know whether a human decided this, so it holds.
    """
    if state.current_source is not None and state.current_source not in KNOWN_SOURCES:
        raise UnknownSourceError(
            f"current_source {state.current_source!r} is not one of {sorted(KNOWN_SOURCES)}; "
            "record the callback and hold the case rather than applying it"
        )
    if state.current_source is None:
        source = predicates.SRC_NONE
    elif state.current_source == SOURCE_MANUAL:
        source = predicates.SRC_MANUAL
    else:
        source = predicates.SRC_AUTOMATIC
    if callback.decision_sequence is None:
        sequence = predicates.SEQ_ABSENT
    elif state.high_water is not None and callback.decision_sequence <= state.high_water:
        sequence = predicates.SEQ_NOT_ABOVE
    else:
        sequence = predicates.SEQ_ABOVE
    return {
        "duplicate": predicates.DUP if callback.run_id in state.seen_run_ids else predicates.FRESH,
        "source": source,
        "sequence": sequence,
    }


def decide(state: LedgerState, callback: Callback, *, phase: str) -> Outcome:
    """Apply the published table BY EXECUTING ITS PREDICATES (re-audit `4cb2cb7` F10).

    The previous version carried its own hardcoded branches beside the table, so the two could
    disagree and nothing noticed — widening a published row's condition changed what TechCraft was
    told and changed nothing here. Now the rows are the only logic: the observation is matched
    against each row's `when`, exactly one row may claim it, and the outcome is read off THAT
    ROW'S booleans — so editing a published row edits this function's behaviour, and the scenario
    tests hold both at once.
    """
    if phase not in (INTERIM, POST_024):
        raise ValueError(f"unknown phase {phase!r}")
    observed = observe(state, callback)
    rows = [t for t in RECEIVER_TRANSITIONS if t.phase == phase]
    matched = [index for index, row in enumerate(rows) if row.when.matches(observed)]
    if len(matched) != 1:
        raise TableIntegrityError(
            f"state {observed} matches row(s) {matched or 'NONE'} in phase {phase!r}"
        )
    row = rows[matched[0]]
    return Outcome(
        row=matched[0],
        record=row.records,
        effective=row.becomes_effective,
        advance_high_water=row.advances_high_water,
    )
