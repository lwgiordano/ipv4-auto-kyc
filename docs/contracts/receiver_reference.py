"""An executable implementation of the published receiver transition tables.

Same discipline as `signing_example.py`: the document publishes tables, and this is the algorithm
those tables describe, driven by scenario tests. A table nobody runs is prose in a grid — the
"otherwise apply" defect (re-audit `4f23f23..97deeae` F2) survived review precisely because it read
correctly and was never executed against a late-arriving older decision.

Two machines, dispatched on the case's effective source (gate audit `6c4f54a..91fbde3` finding 1):
the base table for {none, manual, automatic}, and the manual-release table for
`manual_release_pending`. Their spaces are disjoint on the source value and each is partitioned by
enumeration, so the product is total.

EVERYTHING is validated before ANY table is consulted (re-audit `1826661..b5c7a83` finding 2 —
the previous version checked release-field integrity only inside the pending branch, so a partial
binding reached the ordinary table, blank matching ids completed a release, and `0 < True` made a
Boolean deadline live): exact built-in types everywhere, non-blank identities, bounded integer
time excluding Boolean, closed source domain checked after the type so an unhashable value cannot
reach a membership test, cross-field source/release invariants, and release fields globally
both-absent (ordinary) or both-valid (bound). Bound callbacks route through DURABLE RELEASE
HISTORY: a replay of a completed/expired/cancelled release answers from the persisted terminal
record and changes nothing; a bound callback naming a release this ledger never knew holds. Every
refusal is one stable `ReceiverIntegrityError` subclass, raised before a row is consulted, with
no state consulted or mutated.

This is NOT shipped inside the tool. The receiver is TechCraft's; this is our statement of what we
believe correct, in a form they can run.
"""

from dataclasses import dataclass

from docs.contracts import predicates
from docs.contracts.wire import (
    INTERIM,
    KNOWN_SOURCES,
    POST_025,
    RECEIVER_TRANSITIONS,
    RELEASE_TRANSITIONS,
    SOURCE_MANUAL,
    SOURCE_RELEASE_PENDING,
)

# The ordering authority is allocated per case as a positive database BIGINT; its storage ceiling
# is the domain's upper bound. The same ceiling bounds database time values: a deadline or clock
# outside it is not "a strange time", it is not a time.
_SEQUENCE_CEILING = 2**63 - 1

RELEASE_TERMINALS = frozenset({"completed", "expired", "cancelled"})


class ReceiverIntegrityError(ValueError):
    """The one stable fail-closed refusal: malformed, hostile, or inconsistent input, refused
    BEFORE any row is consulted, with no state consulted or mutated."""


class UnknownSourceError(ReceiverIntegrityError):
    """The receiver reported an effective source outside the closed set. Hold, never apply."""


class SequenceDomainError(ReceiverIntegrityError):
    """A decision_sequence, high-water mark, time value, or identity outside the authority's
    domain (gate audit `6c4f54a..91fbde3` finding 4)."""


class ReleaseIntegrityError(ReceiverIntegrityError):
    """Release fields must be all present or all absent, non-blank when present, and consistent
    with the ledger's release state. 'A mismatch anywhere is an integrity_mismatch terminal.'"""


def _exact_str(value, *, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise SequenceDomainError(f"{label} must be a non-blank string, got {value!r}")


def _domain_int(value, *, floor: int, label: str) -> None:
    if type(value) is not int or value < floor or value > _SEQUENCE_CEILING:
        raise SequenceDomainError(
            f"{label} {value!r} is outside the authority's domain "
            f"(an exact integer in [{floor}, {_SEQUENCE_CEILING}])"
        )


@dataclass(frozen=True)
class PendingRelease:
    """The platform-side record of one open release: its id, the manual event it was opened
    against, and the stored deadline (database time) that bounds it. Validated at construction —
    a blank id or a Boolean deadline is not a strange release, it is not a release."""

    release_id: str
    requested_manual_event_id: str
    deadline: int  # database time; compared against the `now` handed to decide()

    def __post_init__(self) -> None:
        _exact_str(self.release_id, label="release_id")
        _exact_str(self.requested_manual_event_id, label="requested_manual_event_id")
        _domain_int(self.deadline, floor=1, label="deadline")


@dataclass(frozen=True)
class ReleaseTerminal:
    """One durable terminal release record: how a past release ended, retaining the COMPLETE
    immutable binding (R-audit-3 finding 2 — a record keyed on release id alone answered a
    wrong-manual replay as the original outcome). Replaying its bound callback answers from
    this, never from a table, and only on a full-binding match."""

    release_id: str
    requested_manual_event_id: str
    terminal: str  # completed | expired | cancelled

    def __post_init__(self) -> None:
        _exact_str(self.release_id, label="release_id")
        _exact_str(self.requested_manual_event_id, label="requested_manual_event_id")
        if type(self.terminal) is not str or self.terminal not in RELEASE_TERMINALS:
            raise ReleaseIntegrityError(
                f"terminal {self.terminal!r} is not one of {sorted(RELEASE_TERMINALS)}"
            )


@dataclass(frozen=True)
class LedgerState:
    """What the receiver knows about one case before this callback arrives."""

    seen_run_ids: frozenset[str] = frozenset()
    current_source: str | None = None  # "manual", "automatic", "manual_release_pending", or None
    high_water: int | None = None  # post-025 only; a mark over decision_sequence
    current_manual_event_id: str | None = None  # the manual event currently in force, if any
    release: PendingRelease | None = None  # set exactly when current_source is release-pending
    release_history: tuple[ReleaseTerminal, ...] = ()  # durable terminal release records


@dataclass(frozen=True)
class Callback:
    """One delivered decision callback.

    TWO ordinals, and only one of them orders decisions (re-audit `4f23f23..122cc67` finding 1).
    `event_sequence` is PR 2 ingest provenance — the per-case ordinal of the triggering event —
    and it is on the wire today. `decision_sequence` is the PR 7b callback-order authority and
    arrives with the activation unit. They diverge whenever work finishes out of admission order,
    which is ordinary: an event admitted first can decide second, so their ordinals invert.
    Ordering by `event_sequence` therefore suppresses the LATER decision as stale.

    The release fields are the 025 release binding: 'Ordinary callbacks carry every release field
    NULL; release callbacks carry them all non-NULL and equal.' Anything in between is an
    integrity mismatch, held before classification — for EVERY source, not only pending.
    """

    case_id: str
    run_id: str
    decision_sequence: int | None = None  # the ordering authority; arrives with 025
    event_sequence: int | None = None  # ingest provenance; NEVER an ordering authority
    release_id: str | None = None  # release binding; arrives with 025
    manual_event_id: str | None = None  # the manual event the release was opened against


@dataclass(frozen=True)
class Outcome:
    row: int  # index into the phase's rows; -1 for a durable-history replay
    record: bool  # append to the accepted-run ledger
    effective: bool  # becomes the case's current decision
    advance_high_water: bool
    completes_release: bool = False  # release machine only; the base tables can never set it
    release_replay: str | None = None  # the terminal a bound replay answered from, if any


class TableIntegrityError(RuntimeError):
    """The table stopped being a partition: a state matched zero rows or several. The receiver
    must HOLD on ambiguity, never pick — which answer wins would otherwise be an implementation
    accident, and that accident is the F10 defect itself."""


def validate_state(state: LedgerState) -> None:
    """RECURSIVE, exact-type revalidation of the ledger state — the trust boundary at EVERY
    public decision/manual-approval entry (R-audit-3 finding 3: constructor validation is not a
    boundary against forged or mutated nested records; a frozen PendingRelease whose deadline
    was later forged to Boolean True completed a release at now=0). Every nested field, every
    uniqueness/disjointness relationship, one stable fail-closed integrity result — BEFORE any
    history or table lookup."""
    if state.high_water is not None:
        _domain_int(state.high_water, floor=0, label="high_water")
    if type(state.seen_run_ids) not in (set, frozenset):
        raise SequenceDomainError(
            f"seen_run_ids must be a set of run ids, got {type(state.seen_run_ids).__name__}"
        )
    for member in state.seen_run_ids:
        _exact_str(member, label="seen_run_ids member")
    source = state.current_source
    if source is not None and type(source) is not str:
        raise UnknownSourceError(
            f"current_source must be a string from the closed set or None, got "
            f"{type(source).__name__}"
        )
    if source is not None and source not in KNOWN_SOURCES:
        raise UnknownSourceError(
            f"current_source {source!r} is not one of {sorted(KNOWN_SOURCES)}; "
            "record the callback and hold the case rather than applying it"
        )
    if state.current_manual_event_id is not None:
        _exact_str(state.current_manual_event_id, label="current_manual_event_id")
    if state.release is not None and not isinstance(state.release, PendingRelease):
        raise ReleaseIntegrityError("release must be a PendingRelease or None")
    if type(state.release_history) is not tuple or any(
        not isinstance(record, ReleaseTerminal) for record in state.release_history
    ):
        raise ReleaseIntegrityError("release_history must be a tuple of ReleaseTerminal records")
    if state.release is not None:
        # revalidate the nested record's OWN fields — frozen is not forgery-proof
        _exact_str(state.release.release_id, label="release.release_id")
        _exact_str(state.release.requested_manual_event_id,
                   label="release.requested_manual_event_id")
        _domain_int(state.release.deadline, floor=1, label="release.deadline")
    seen_terminal_ids = []
    for record in state.release_history:
        _exact_str(record.release_id, label="release_history release_id")
        _exact_str(record.requested_manual_event_id,
                   label="release_history requested_manual_event_id")
        if type(record.terminal) is not str or record.terminal not in RELEASE_TERMINALS:
            raise ReleaseIntegrityError(
                f"history terminal {record.terminal!r} is not one of "
                f"{sorted(RELEASE_TERMINALS)}"
            )
        seen_terminal_ids.append(record.release_id)
    if len(seen_terminal_ids) != len(set(seen_terminal_ids)):
        raise ReleaseIntegrityError(
            "release_history carries more than one terminal record for one release id — "
            "which answer wins would be an accident; hold the case"
        )
    if state.release is not None and state.release.release_id in seen_terminal_ids:
        raise ReleaseIntegrityError(
            "a release is both PENDING and TERMINAL on this ledger — an impossible state; "
            "hold the case"
        )
    # cross-field source/release invariants: a pending release exists exactly in the
    # release-pending state
    if (source == SOURCE_RELEASE_PENDING) != (state.release is not None):
        raise ReleaseIntegrityError(
            "current_source and the pending release record disagree: manual_release_pending "
            "iff a PendingRelease is held — an impossible state; hold the case"
        )


def _validate(state: LedgerState, callback: Callback) -> None:
    """State (recursive) plus callback domains plus the cross-record binding rule, before any
    classification. Exact types only: `True` is not 1 here, 1.5 is not almost-2, NaN is not a
    mark, and an unhashable source never reaches a membership test."""
    validate_state(state)
    for label, value in (("case_id", callback.case_id), ("run_id", callback.run_id)):
        _exact_str(value, label=label)
    if callback.decision_sequence is not None:
        _domain_int(callback.decision_sequence, floor=1, label="decision_sequence")
    # R-audit-4 finding 4: EVERY tolerated field is validated — event_sequence is ingest
    # provenance, never ordering authority, but a bool/str/negative value is still not a
    # callback this boundary may classify.
    if callback.event_sequence is not None:
        _domain_int(callback.event_sequence, floor=1, label="event_sequence")
    # release fields on the callback: globally both-absent or both-valid (finding 2 — this used
    # to be checked only inside the pending branch, so a partial binding reached the ordinary
    # table)
    bound_fields = (callback.release_id, callback.manual_event_id)
    if any(f is not None for f in bound_fields):
        if not all(f is not None for f in bound_fields):
            raise ReleaseIntegrityError(
                "release fields must be all present or all absent; a partial binding is an "
                "integrity_mismatch terminal, not a classifiable callback"
            )
        _exact_str(callback.release_id, label="release_id")
        _exact_str(callback.manual_event_id, label="manual_event_id")


def observe(state: LedgerState, callback: Callback) -> dict:
    """Reduce (state, callback) to the closed base-facet observation. `decide` is the only
    dispatcher; this reduces the base machine's facets and refuses everything else."""
    _validate(state, callback)
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
    _domain_int(now, floor=0, label="now")
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
    one row may claim it, and the outcome is read off THAT ROW'S derived effects — so editing a
    published row edits this function's behaviour, the scenario tests hold both at once, and the
    authority verifier holds both against an oracle derived from the accepted invariants.

    Validation runs FIRST, for every path. A bound callback then routes: a replay of a terminal
    release answers from durable history; a pending case dispatches to the release table
    (post-025 only — the protocol arrives with the activation unit); a bound callback naming a
    release this ledger has no record of holds.
    """
    if phase not in (INTERIM, POST_025):
        raise ValueError(f"unknown phase {phase!r}")
    _validate(state, callback)
    bound = callback.release_id is not None
    if bound:
        if phase != POST_025:
            raise ReleaseIntegrityError(
                "release bindings arrive with the activation unit; a bound callback before it "
                "is an impossible state — hold"
            )
        replayed = next(
            (r for r in state.release_history if r.release_id == callback.release_id), None)
        if replayed is not None:
            # 'Replaying it returns the original outcome and changes nothing, including after
            # completed or expired.' The durable record answers; no table is consulted — and
            # only on the COMPLETE original binding (R-audit-3 finding 2): a matching release
            # id with the wrong manual event is not the original callback, it is an integrity
            # hold.
            if callback.manual_event_id != replayed.requested_manual_event_id:
                raise ReleaseIntegrityError(
                    f"a bound callback replays terminal release {callback.release_id!r} but "
                    "its manual binding does not match the original — hold"
                )
            return Outcome(row=-1, record=False, effective=False, advance_high_water=False,
                           completes_release=False, release_replay=replayed.terminal)
        if state.current_source != SOURCE_RELEASE_PENDING:
            raise ReleaseIntegrityError(
                f"a bound callback names release {callback.release_id!r} but this ledger holds "
                "no pending release and no terminal record of it — hold"
            )
    if state.current_source == SOURCE_RELEASE_PENDING:
        if phase != POST_025:
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
    release outright, recording the terminal durably. Both race orders end with the newer manual
    approval effective, and the formerly bound callback can never complete the cancelled release
    afterwards — its replay answers from the durable record."""
    _exact_str(manual_event_id, label="manual_event_id")
    validate_state(state)  # the same trust boundary as decide(); a forged ledger never writes
    history = state.release_history
    if state.release is not None:
        history = (*history, ReleaseTerminal(
            release_id=state.release.release_id,
            requested_manual_event_id=state.release.requested_manual_event_id,
            terminal="cancelled"))
    return LedgerState(
        seen_run_ids=state.seen_run_ids,
        current_source=SOURCE_MANUAL,
        high_water=state.high_water,
        current_manual_event_id=manual_event_id,
        release=None,
        release_history=history,
    )


def token_semantics_problems() -> list[str]:
    """Every legend token's executable semantics, held to the machines themselves (re-audit
    finding 8): over realized concrete (LedgerState, Callback, now) inputs covering both state
    spaces, the observed facet equals the token exactly when the token's checker holds."""
    problems = []
    base_realizations = []
    for source in (None, "manual", "automatic"):
        for run_id in ("r-seen", "r-new"):
            for sequence in (None, 3, 9):
                for mark in (None, 5):  # R-audit-3 finding 4: the ABSENT mark is realized too
                    state = LedgerState(
                        seen_run_ids=frozenset({"r-seen"}), current_source=source,
                        high_water=mark,
                        current_manual_event_id="M1" if source == "manual" else None)
                    base_realizations.append(
                        (state,
                         Callback(case_id="c", run_id=run_id, decision_sequence=sequence),
                         500))
    release_realizations = []
    for run_id in ("r-seen", "r-new"):
        # every binding-field mismatch independently (R-audit-3 finding 4): correct/correct,
        # wrong id/correct manual, CORRECT ID/WRONG MANUAL, both wrong, and unbound
        for binding in ((None, None), ("R1", "M1"), ("R-other", "M1"),
                        ("R1", "M-wrong"), ("R-other", "M-wrong")):
            for current_event in ("M1", "M2"):
                for now in (500, 1000):
                    for sequence in (None, 3, 9):
                        state = LedgerState(
                            seen_run_ids=frozenset({"r-seen"}),
                            current_source=SOURCE_RELEASE_PENDING, high_water=5,
                            current_manual_event_id=current_event,
                            release=PendingRelease(release_id="R1",
                                                   requested_manual_event_id="M1",
                                                   deadline=1000))
                        release_realizations.append(
                            (state,
                             Callback(case_id="c", run_id=run_id, decision_sequence=sequence,
                                      release_id=binding[0], manual_event_id=binding[1]),
                             now))
    for token, semantics in predicates.TOKEN_SEMANTICS.items():
        if semantics.facet in ("duplicate", "sequence"):
            realizations = base_realizations + release_realizations
        elif semantics.facet == "source":
            realizations = base_realizations
        else:
            realizations = release_realizations
        for state, callback, now in realizations:
            if state.current_source == SOURCE_RELEASE_PENDING:
                observed = observe_release(state, callback, now=now)
            else:
                observed = observe(state, callback)
            facet_value = observed.get(semantics.facet)
            if facet_value is None:
                continue
            holds = semantics.check(state, callback, now)
            if (facet_value == token) != holds:
                problems.append(
                    f"{token}: checker says {holds} but the machine observes "
                    f"{facet_value!r} for {observed}"
                )
    return problems
