"""An executable implementation of the published receiver transition tables.

Same discipline as `signing_example.py`: the document publishes tables, and this is the algorithm
those tables describe, driven by scenario tests. A table nobody runs is prose in a grid — the
"otherwise apply" defect (re-audit `4f23f23..97deeae` F2) survived review precisely because it read
correctly and was never executed against a late-arriving older decision.

Two machines, dispatched on the case's effective source (gate audit `6c4f54a..91fbde3` finding 1):
the base table for {none, manual, automatic}, and the manual-release table for
`manual_release_pending` — the accepted activation design's third state, in which manual remains
effective and only the bound, unexpired, above-the-mark callback that re-passes every completion
CAS check may complete the release. Their spaces are disjoint on the source value and each is
partitioned by enumeration, so the product is total.

Inputs are validated BEFORE any row is consulted (finding 4): the ordering authority is a positive
BIGINT, so a fraction, a bool, a NaN mark, a negative, or a blank identity is one stable
fail-closed error — never an accidental TypeError and never a coerced comparison.

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
    RELEASE_TRANSITIONS,
    SOURCE_MANUAL,
    SOURCE_RELEASE_PENDING,
)

# The ordering authority is allocated per case as a positive database BIGINT; its storage ceiling
# is the domain's upper bound. Anything outside is not "a strange sequence", it is not a sequence.
_SEQUENCE_CEILING = 2**63 - 1


class UnknownSourceError(ValueError):
    """The receiver reported an effective source outside the closed set. Hold, never apply."""


class SequenceDomainError(ValueError):
    """A decision_sequence, high-water mark, or identity outside the authority's domain. One
    stable fail-closed error, raised before any row is consulted, with no state consulted or
    mutated — the alternative is a NaN mark silently making every later callback look above it
    (gate audit `6c4f54a..91fbde3` finding 4)."""


class ReleaseIntegrityError(ValueError):
    """Release fields must be all present or all absent, and a pending release must be complete.
    'A mismatch anywhere is an integrity_mismatch terminal': a partial binding is neither an
    ordinary callback nor a bound one, so it holds — it is never classified into a row."""


@dataclass(frozen=True)
class PendingRelease:
    """The platform-side record of one open release: its id, the manual event it was opened
    against, and the stored deadline (database time) that bounds it."""

    release_id: str
    requested_manual_event_id: str
    deadline: int  # database time; compared against the `now` handed to decide()


@dataclass(frozen=True)
class LedgerState:
    """What the receiver knows about one case before this callback arrives."""

    seen_run_ids: frozenset[str] = frozenset()
    current_source: str | None = None  # "manual", "automatic", "manual_release_pending", or None
    high_water: int | None = None  # post-024 only; a mark over decision_sequence
    current_manual_event_id: str | None = None  # the manual event currently in force, if any
    release: PendingRelease | None = None  # set exactly when current_source is release-pending


@dataclass(frozen=True)
class Callback:
    """One delivered decision callback.

    TWO ordinals, and only one of them orders decisions (re-audit `4f23f23..122cc67` finding 1).
    `event_sequence` is PR 2 ingest provenance — the per-case ordinal of the triggering event —
    and it is on the wire today. `decision_sequence` is the PR 7b callback-order authority and
    arrives with the activation unit. They diverge whenever work finishes out of admission order,
    which is ordinary: an event admitted first can decide second, so their ordinals invert.
    Ordering by `event_sequence` therefore suppresses the LATER decision as stale.

    The release fields are the 024 release binding: 'Ordinary callbacks carry every release field
    NULL; release callbacks carry them all non-NULL and equal.' Anything in between is an
    integrity mismatch, held before classification.
    """

    case_id: str
    run_id: str
    decision_sequence: int | None = None  # the ordering authority; arrives with 024
    event_sequence: int | None = None  # ingest provenance; NEVER an ordering authority
    release_id: str | None = None  # release binding; arrives with 024
    manual_event_id: str | None = None  # the manual event the release was opened against


@dataclass(frozen=True)
class Outcome:
    row: int  # index into the phase's rows — the published table row that decided this
    record: bool  # append to the accepted-run ledger
    effective: bool  # becomes the case's current decision
    advance_high_water: bool
    completes_release: bool = False  # release machine only; the base tables can never set it


class TableIntegrityError(RuntimeError):
    """The table stopped being a partition: a state matched zero rows or several. The receiver
    must HOLD on ambiguity, never pick — which answer wins would otherwise be an implementation
    accident, and that accident is the F10 defect itself."""


def _domain_int(value, *, floor: int, label: str) -> None:
    if type(value) is not int or value < floor or value > _SEQUENCE_CEILING:
        raise SequenceDomainError(
            f"{label} {value!r} is outside the ordering authority's domain "
            f"(an exact integer in [{floor}, {_SEQUENCE_CEILING}])"
        )


def _validate(state: LedgerState, callback: Callback) -> None:
    """Every domain check, before any classification. Exact types only: `True` is not 1 here,
    1.5 is not almost-2, and NaN is not a mark."""
    for label, value in (("case_id", callback.case_id), ("run_id", callback.run_id)):
        if type(value) is not str or not value.strip():
            raise SequenceDomainError(f"{label} must be a non-blank string, got {value!r}")
    if callback.decision_sequence is not None:
        _domain_int(callback.decision_sequence, floor=1, label="decision_sequence")
    if state.high_water is not None:
        _domain_int(state.high_water, floor=0, label="high_water")


def observe(state: LedgerState, callback: Callback) -> dict:
    """Reduce (state, callback) to the closed base-facet observation.

    FAIL CLOSED on an unrecognised source (re-audit `4f23f23..122cc67` finding 2), BEFORE any row
    is consulted: `MANUAL`, a typo, a variant — a source you cannot classify is precisely the
    case where you do not know whether a human decided this, so it holds. The release-pending
    source is KNOWN and is dispatched to its own machine by `decide`, never reduced here.
    """
    _validate(state, callback)
    if state.current_source is not None and state.current_source not in KNOWN_SOURCES:
        raise UnknownSourceError(
            f"current_source {state.current_source!r} is not one of {sorted(KNOWN_SOURCES)}; "
            "record the callback and hold the case rather than applying it"
        )
    if state.current_source == SOURCE_RELEASE_PENDING:
        raise UnknownSourceError(
            "a release-pending case is governed by the release table; observe() reduces only "
            "the base machine's facets"
        )
    if state.current_source is None:
        source = predicates.SRC_NONE
    elif state.current_source == SOURCE_MANUAL:
        source = predicates.SRC_MANUAL
    else:
        source = predicates.SRC_AUTOMATIC
    return {
        "duplicate": predicates.DUP if callback.run_id in state.seen_run_ids else predicates.FRESH,
        "source": source,
        "sequence": _sequence_facet(state, callback),
    }


def _sequence_facet(state: LedgerState, callback: Callback) -> str:
    if callback.decision_sequence is None:
        return predicates.SEQ_ABSENT
    if state.high_water is not None and callback.decision_sequence <= state.high_water:
        return predicates.SEQ_NOT_ABOVE
    return predicates.SEQ_ABOVE


def observe_release(state: LedgerState, callback: Callback, *, now: int) -> dict:
    """Reduce (state, callback, database-time) to the release machine's closed observation."""
    _validate(state, callback)
    if state.release is None:
        raise ReleaseIntegrityError(
            "current_source is manual_release_pending but the ledger carries no pending release "
            "record — an impossible state; hold the case"
        )
    if type(now) is not int:
        raise SequenceDomainError(
            f"now must be database time as an exact integer, got {now!r}"
        )
    bound_fields = (callback.release_id, callback.manual_event_id)
    if any(f is not None for f in bound_fields) and not all(f is not None for f in bound_fields):
        raise ReleaseIntegrityError(
            "release fields must be all present or all absent; a partial binding is an "
            "integrity_mismatch terminal, not a classifiable callback"
        )
    if callback.release_id is None:
        binding = predicates.BIND_NONE
    elif (callback.release_id == state.release.release_id
          and callback.manual_event_id == state.release.requested_manual_event_id):
        binding = predicates.BIND_MATCH
    else:
        binding = predicates.BIND_MISMATCH
    return {
        "duplicate": (predicates.DUP if callback.run_id in state.seen_run_ids
                      else predicates.FRESH),
        "binding": binding,
        "manual_event": (
            predicates.EVENT_UNCHANGED
            if state.current_manual_event_id == state.release.requested_manual_event_id
            else predicates.EVENT_CHANGED
        ),
        "deadline": (predicates.DEADLINE_LIVE if now < state.release.deadline
                     else predicates.DEADLINE_EXPIRED),
        "sequence": _sequence_facet(state, callback),
    }


def decide(state: LedgerState, callback: Callback, *, phase: str, now: int | None = None) -> Outcome:
    """Apply the published tables BY EXECUTING THEIR PREDICATES (re-audit `4cb2cb7` F10).

    The rows are the only logic: the observation is matched against each row's `when`, exactly
    one row may claim it, and the outcome is read off THAT ROW'S booleans — so editing a
    published row edits this function's behaviour, the scenario tests hold both at once, and the
    authority verifier holds both against an oracle derived from the accepted invariants.

    A release-pending case dispatches to the release table (post-024 only — the release protocol
    arrives with the activation unit, so that source in the interim phase is an impossible state
    and holds). Completion needs database time; a caller that cannot supply it cannot complete.
    """
    if phase not in (INTERIM, POST_024):
        raise ValueError(f"unknown phase {phase!r}")
    if state.current_source == SOURCE_RELEASE_PENDING:
        if phase != POST_024:
            raise UnknownSourceError(
                "manual_release_pending cannot exist before the activation unit; hold the case"
            )
        if now is None:
            raise SequenceDomainError(
                "deciding a release-pending case requires database time (now=)"
            )
        observed = observe_release(state, callback, now=now)
        rows = list(RELEASE_TRANSITIONS)
        matched = [index for index, row in enumerate(rows) if row.when.matches(observed)]
        if len(matched) != 1:
            raise TableIntegrityError(
                f"release state {observed} matches row(s) {matched or 'NONE'}"
            )
        row = rows[matched[0]]
        return Outcome(
            row=matched[0],
            record=row.records,
            effective=row.becomes_effective,
            advance_high_water=row.advances_high_water,
            completes_release=row.completes_release,
        )
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


def apply_manual_approval(state: LedgerState, *, manual_event_id: str) -> LedgerState:
    """Spec transition 4: a new manual approval takes the case lock and CANCELS any pending
    release outright. Both race orders end with the newer manual approval effective, and the
    formerly bound callback can never complete the cancelled release afterwards."""
    if type(manual_event_id) is not str or not manual_event_id.strip():
        raise SequenceDomainError(
            f"manual_event_id must be a non-blank string, got {manual_event_id!r}"
        )
    return LedgerState(
        seen_run_ids=state.seen_run_ids,
        current_source=SOURCE_MANUAL,
        high_water=state.high_water,
        current_manual_event_id=manual_event_id,
        release=None,
    )
