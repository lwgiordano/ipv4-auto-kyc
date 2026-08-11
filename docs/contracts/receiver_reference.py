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

from docs.contracts.wire import INTERIM, POST_024


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


def decide(state: LedgerState, callback: Callback, *, phase: str) -> Outcome:
    """Apply the published table. Rows are tried in order; the first match wins.

    Returns which row decided it, so a test can prove the implementation and the table agree row
    for row rather than merely agreeing on the final answer.
    """
    if phase == INTERIM:
        if callback.run_id in state.seen_run_ids:
            return Outcome(row=0, record=False, effective=False, advance_high_water=False)
        if state.current_source is None:
            return Outcome(row=1, record=True, effective=True, advance_high_water=False)
        if state.current_source == "manual":
            return Outcome(row=2, record=True, effective=False, advance_high_water=False)
        # AUTOMATIC current, different run. NOT "otherwise apply": interim has no ordering
        # authority, so an older decision delayed in flight is indistinguishable from a newer one.
        return Outcome(row=3, record=True, effective=False, advance_high_water=False)

    if phase == POST_024:
        if callback.run_id in state.seen_run_ids:
            return Outcome(row=0, record=False, effective=False, advance_high_water=False)
        sequence = callback.decision_sequence
        if sequence is None or (state.high_water is not None and sequence <= state.high_water):
            return Outcome(row=1, record=True, effective=False, advance_high_water=False)
        if state.current_source == "manual":
            # The mark advances even though the decision does not take effect: leaving it stale
            # would judge the first post-release automatic decision against the wrong baseline.
            return Outcome(row=2, record=True, effective=False, advance_high_water=True)
        return Outcome(row=3, record=True, effective=True, advance_high_water=True)

    raise ValueError(f"unknown phase {phase!r}")
