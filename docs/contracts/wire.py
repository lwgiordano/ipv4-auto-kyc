"""Wire, signing, event, callback, ordering, and retention claims.

Every value below is written out LITERALLY, as the document asserts it to TechCraft. It is not
computed from the code at import time, because a registry that derives its values from the
authority makes the authority test tautological: the point is that a human wrote a claim down and
a test independently proves the running system agrees (re-audit `82636da..9ac574f`, systemic
requirement). `tests/unit/test_contract_registry_authority.py` holds each literal against its
authority; `tests/unit/test_contract_rendering.py` proves the rendered PDF displays it.
"""

import json
from dataclasses import dataclass, field
from typing import ClassVar

from docs.contracts import (
    Claim,
    ClaimState,
    Registry,
    predicates,  # noqa: I001 — sibling module, not the package root
)
from docs.contracts.statements import statement

# The one deliberate runtime dependency (re-audit `1826661..b5c7a83` finding 6): the capability
# registry is RUNTIME state, owned by the dependency-neutral, stdlib-only
# `kyc_tool.capabilities`, and this document PROJECTS it — consumers below resolve through the
# registry at call time, so there is no docs-side copy to drift and no name sweep to fool.
# (The heavyweight `kyc_tool.config` is still mirrored, never imported.)
from kyc_tool.capabilities import (
    SLOT_ANSWER_ARTIFACT,
    SLOT_RETIREMENT_EVIDENCE,
    MissingCapability,
)
from kyc_tool.capabilities import (
    resolve as resolve_capability,
)

# ── the receiver's effectiveness decision, as a TOTAL transition table ─────────────────────────────
#
# Re-audit `4f23f23..97deeae` F2. The previous version was prose: "if the current source is a
# manual approval, record but do not apply; otherwise apply it as the current automatic decision."
# The "otherwise" is the defect. Automatic decision B is current; automatic decision A, older but
# delayed, arrives under a run id nobody has seen. It is not a duplicate and the current source is
# not manual, so the published algorithm says apply — and A replaces B. Prose with an "otherwise"
# branch cannot be checked for totality by reading it, which is why this is a table: every row is
# a condition, and a test asserts the rows cover the whole space in each phase.
#
# `docs/contracts/receiver_reference.py` implements exactly these rows and is driven by the
# scenario tests, so the table cannot drift from a working algorithm.


# The only values a receiver may report as the currently effective source. Anything else — a
# case variant, a typo — is UNKNOWN, and unknown must hold, not apply (re-audit
# `4f23f23..122cc67` finding 2). `manual_release_pending` is the accepted activation design's
# third state (gate audit `6c4f54a..91fbde3` finding 1): manual REMAINS EFFECTIVE while a release
# is open, and callbacks arriving in it are governed by the release table below — it is a known
# state with its own machine, not an error and not a variant of "manual".
SOURCE_MANUAL = "manual"
SOURCE_AUTOMATIC = "automatic"
SOURCE_RELEASE_PENDING = "manual_release_pending"
KNOWN_SOURCES = frozenset({SOURCE_MANUAL, SOURCE_AUTOMATIC, SOURCE_RELEASE_PENDING})


# ── closed outcome and reason records (re-audit `1826661..b5c7a83` finding 1) ─────────────────────
#
# The rows used to carry independently authored `record`/`effective`/`why` strings beside the
# verified booleans, so the page could say "NO — replace the manual approval" while the machine
# said the opposite and every check passed. An OutcomeKind is ONE closed record pairing the
# structured effects with the exact visible cells — text that contradicts its own booleans is
# unconstructable — and a row carries only the ids; every visible cell is derived. There is no
# normative authored prose adjacent to an outcome.


def outcome_record_problems(records, becomes_effective, advances_high_water, completes_release,
                            record_text, effective_text) -> list[str]:
    problems = []
    if becomes_effective and not effective_text.startswith("YES"):
        problems.append("an effective outcome's visible cell must start YES")
    if not becomes_effective and not effective_text.startswith("NO"):
        problems.append("a non-effective outcome's visible cell must start NO")
    if records and "nothing" in record_text.lower():
        problems.append("a recording outcome's cell may not say nothing is recorded")
    if not records and "nothing" not in record_text.lower():
        problems.append("a non-recording outcome's cell must say nothing new is recorded")
    if advances_high_water != ("advance the high-water mark" in record_text):
        problems.append("the advance phrase must appear exactly when the mark moves")
    if completes_release != ("COMPLETES" in effective_text):
        problems.append("COMPLETES must appear exactly when the release completes")
    return problems


@dataclass(frozen=True)
class OutcomeKind:
    """The structured effects AND the exact visible cells, one closed record."""

    records: bool
    becomes_effective: bool
    advances_high_water: bool
    completes_release: bool
    record_text: str
    effective_text: str

    def __post_init__(self) -> None:
        problems = outcome_record_problems(
            self.records, self.becomes_effective, self.advances_high_water,
            self.completes_release, self.record_text, self.effective_text)
        if problems:
            raise ValueError(f"OutcomeKind text contradicts its effects: {problems}")


OUTCOME_KINDS = {
    "ack_duplicate_interim": OutcomeKind(
        False, False, False, False,
        "nothing new; the row is already there",
        "NO CHANGE — acknowledge with 2xx and stop"),
    "apply_first": OutcomeKind(
        True, True, False, False,
        "the callback", "YES — it becomes the current automatic decision"),
    "record_manual_holds": OutcomeKind(
        True, False, False, False, "the callback", "NO"),
    "record_hold_review": OutcomeKind(
        True, False, False, False,
        "the callback", "NO — hold the case for review instead"),
    "ack_duplicate": OutcomeKind(
        False, False, False, False,
        "nothing new", "NO CHANGE — acknowledge with 2xx and stop"),
    "record_unordered": OutcomeKind(
        True, False, False, False,
        "the callback", "NO, and the high-water mark does NOT move"),
    "record_advance": OutcomeKind(
        True, False, True, False,
        "the callback, AND advance the high-water mark to its decision_sequence", "NO"),
    "apply_ordered": OutcomeKind(
        True, True, True, False,
        "the callback, AND advance the high-water mark to its decision_sequence", "YES"),
    "rel_complete": OutcomeKind(
        True, True, True, True,
        "the callback, AND advance the high-water mark",
        "YES — the release COMPLETES; automatic authority returns with this decision"),
    "rel_record_advance": OutcomeKind(
        True, False, True, False,
        "the callback, AND advance the high-water mark", "NO — manual remains in force"),
    "rel_record_only": OutcomeKind(
        True, False, False, False, "the callback", "NO — manual remains in force"),
    "rel_record_only_pending": OutcomeKind(
        True, False, False, False,
        "the callback", "NO — manual remains in force; the release stays pending"),
}

REASON_TEXTS = {
    "at_least_once_duplicates":
        "Delivery is at-least-once, so a duplicate is the expected case, not an error. "
        "Returning non-2xx to a duplicate makes us retry it forever.",
    "nothing_to_conflict": "Nothing to conflict with.",
    "manual_decided":
        "A reviewer decided this case. An automatic callback queued before that approval can "
        "arrive after it under a run id you have never seen; applying it silently overturns a "
        "human decision. Automatic authority returns only through the authenticated "
        "platform-owned release protocol.",
    "no_ordering_authority":
        "This is the row that must not read 'otherwise apply'. Interim has no wire ordering "
        "authority, so you cannot tell a newer decision from an older one that was delayed. "
        "decided_at will not tell you either (section 5). Applying the arrival that happens "
        "to land second silently replaces a newer decision with an older one.",
    "post_duplicates": "Same as interim: duplicates are expected.",
    "superseded_or_unordered":
        "It is superseded or unordered. Recording it keeps your audit trail complete without "
        "letting it take effect.",
    "mark_moves_manual":
        "The manual approval stays in force, but the mark still moves: otherwise every later "
        "automatic decision for the case is compared against a stale mark and the first one "
        "after a manual release would be judged by the wrong baseline.",
    "applies_on_proven_order":
        "This is the only row that applies an automatic decision, and it does so on proven "
        "DECISION order rather than on arrival order.",
    "rel_run_dedupe":
        "Run-id dedupe, same as both base tables: a duplicate (case_id, run_id) is the "
        "expected at-least-once delivery, acknowledged without change. Replaying a TERMINATED "
        "release_id is a different authority — durable terminal history, resolved before this "
        "table (see the replay rule).",
    "rel_complete_cas":
        "The ONLY completing row. Every check the platform's completion CAS re-asserts holds: "
        "the bound release matches the pending one, the case's current manual event is still "
        "the one the release was opened against, the deadline is unexpired by database time, "
        "and the sequence proves order.",
    "rel_foreign_advances":
        "An ordinary or foreign-release callback cannot complete this case's release, but the "
        "mark still moves on proven order — otherwise the first callback after completion "
        "would be judged against a stale baseline.",
    "rel_reapproved_above":
        "The case was re-approved after this release was opened. Completing now would "
        "replace an approval nobody released — the exact race the completion CAS exists to "
        "lose safely.",
    "rel_expired_above":
        "Past the stored deadline the release can only be EXPIRED by the platform's reaper "
        "or lazy transition — never completed by a late callback, however well bound.",
    "rel_no_order":
        "Recorded for the audit trail; without proven order the mark does not move either.",
    "rel_reapproved_no_order":
        "Re-approved since the release opened, and no proven order either: nothing about "
        "this arrival may change the case.",
    "rel_expired_no_order": "Expired AND without proven order: recorded, nothing else.",
    "rel_bound_no_order":
        "Bound, current, and unexpired — but completion also requires a sequence above the "
        "mark. 'The discarded pre-release callbacks never satisfy it': only the FRESH bound "
        "sequence completes.",
}


@dataclass(frozen=True)
class Transition:
    """One row of the receiver table.

    `when` is the row's PREDICATE — a subset of the closed receiver state space
    (`docs/contracts/predicates.py`) — and `condition`, the text the PDF prints, is DERIVED from
    it (re-audit `4cb2cb7` F10). `outcome` and `reason` are ids into the closed OUTCOME_KINDS and
    REASON_TEXTS records (re-audit `1826661..b5c7a83` finding 1): every visible cell — Record,
    Effective, Why — and every structured effect derives from those records, so there is no
    authored prose left beside an outcome to contradict it, and an OutcomeKind whose text fights
    its own booleans cannot even construct. The evaluator executes the same `when` objects, each
    phase's rows PARTITION the state space, and independent oracles hold the derived effects to
    the accepted invariants.
    """

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = (
        "phase", "condition", "record", "effective", "why")

    phase: str  # "interim" (today) or "post-024" (after ordered delivery is activated)
    when: predicates.When
    outcome: str  # id into OUTCOME_KINDS — effects and visible cells, one closed record
    reason: str  # id into REASON_TEXTS — the Why cell
    record: str = field(default="", init=False)
    effective: str = field(default="", init=False)
    why: str = field(default="", init=False)
    records: bool = field(default=False, init=False)
    becomes_effective: bool = field(default=False, init=False)
    advances_high_water: bool = field(default=False, init=False)
    condition: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOME_KINDS:
            raise ValueError(f"unknown outcome kind {self.outcome!r}")
        if self.reason not in REASON_TEXTS:
            raise ValueError(f"unknown reason {self.reason!r}")
        kind = OUTCOME_KINDS[self.outcome]
        if kind.completes_release:
            raise ValueError("the base tables can never complete a release")
        object.__setattr__(self, "record", kind.record_text)
        object.__setattr__(self, "effective", kind.effective_text)
        object.__setattr__(self, "why", REASON_TEXTS[self.reason])
        object.__setattr__(self, "records", kind.records)
        object.__setattr__(self, "becomes_effective", kind.becomes_effective)
        object.__setattr__(self, "advances_high_water", kind.advances_high_water)
        object.__setattr__(self, "condition", self.when.condition())


INTERIM = "interim"
POST_024 = "post-024"

@dataclass(frozen=True)
class TransitionSemantic:
    """One row identity's COMPLETE closed pairing (R-audit-3 `5c14537..6ef6fc7` finding 1).

    The rows used to pair an outcome id and a reason id freely at the row site, and the visible
    texts were keyword-screened prose — so two ids sharing the same Booleans could be swapped,
    and the source records could be inverted wholesale, without any verifier noticing. The
    pairing now lives HERE, one record per row identity; both published tables are DERIVED from
    this registry in their reviewed order; and the authority verifier pins the projection of
    the complete surface (every OutcomeKind field, every reason text, every pairing), so any
    edit anywhere on the surface is a re-pin — the act of review — never a quiet row edit."""

    table: str  # "base" | "release"
    phase: str | None  # base rows carry interim/post-024; release rows are post-024 by construction
    when: object
    outcome: str
    reason: str


TRANSITION_SEMANTICS: dict[str, TransitionSemantic] = {
    "interim.duplicate": TransitionSemantic(
        "base", INTERIM,
        predicates.When(duplicate=frozenset({predicates.DUP}),
                        source=predicates.ANY_SOURCE,
                        sequence=predicates.ANY_SEQUENCE),
        "ack_duplicate_interim", "at_least_once_duplicates"),
    "interim.fresh_none": TransitionSemantic(
        "base", INTERIM,
        predicates.When(duplicate=frozenset({predicates.FRESH}),
                        source=frozenset({predicates.SRC_NONE}),
                        sequence=predicates.ANY_SEQUENCE),
        "apply_first", "nothing_to_conflict"),
    "interim.fresh_manual": TransitionSemantic(
        "base", INTERIM,
        predicates.When(duplicate=frozenset({predicates.FRESH}),
                        source=frozenset({predicates.SRC_MANUAL}),
                        sequence=predicates.ANY_SEQUENCE),
        "record_manual_holds", "manual_decided"),
    "interim.fresh_automatic": TransitionSemantic(
        "base", INTERIM,
        predicates.When(duplicate=frozenset({predicates.FRESH}),
                        source=frozenset({predicates.SRC_AUTOMATIC}),
                        sequence=predicates.ANY_SEQUENCE),
        "record_hold_review", "no_ordering_authority"),
    "post.duplicate": TransitionSemantic(
        "base", POST_024,
        predicates.When(duplicate=frozenset({predicates.DUP}),
                        source=predicates.ANY_SOURCE,
                        sequence=predicates.ANY_SEQUENCE),
        "ack_duplicate", "post_duplicates"),
    "post.fresh_unordered": TransitionSemantic(
        "base", POST_024,
        predicates.When(duplicate=frozenset({predicates.FRESH}),
                        source=predicates.ANY_SOURCE,
                        sequence=frozenset({predicates.SEQ_ABSENT,
                                            predicates.SEQ_NOT_ABOVE})),
        "record_unordered", "superseded_or_unordered"),
    "post.fresh_manual_above": TransitionSemantic(
        "base", POST_024,
        predicates.When(duplicate=frozenset({predicates.FRESH}),
                        source=frozenset({predicates.SRC_MANUAL}),
                        sequence=frozenset({predicates.SEQ_ABOVE})),
        "record_advance", "mark_moves_manual"),
    "post.apply_ordered": TransitionSemantic(
        "base", POST_024,
        predicates.When(duplicate=frozenset({predicates.FRESH}),
                        source=frozenset({predicates.SRC_NONE,
                                          predicates.SRC_AUTOMATIC}),
                        sequence=frozenset({predicates.SEQ_ABOVE})),
        "apply_ordered", "applies_on_proven_order"),
}

_BASE_ROW_ORDER = (
    "interim.duplicate", "interim.fresh_none", "interim.fresh_manual",
    "interim.fresh_automatic", "post.duplicate", "post.fresh_unordered",
    "post.fresh_manual_above", "post.apply_ordered")


def base_transitions() -> tuple[Transition, ...]:
    """The published base table, DERIVED from the registry — the only way rows come to exist."""
    return tuple(
        Transition(phase=TRANSITION_SEMANTICS[key].phase, when=TRANSITION_SEMANTICS[key].when,
                   outcome=TRANSITION_SEMANTICS[key].outcome,
                   reason=TRANSITION_SEMANTICS[key].reason)
        for key in _BASE_ROW_ORDER)


RECEIVER_TRANSITIONS: tuple[Transition, ...] = base_transitions()

# ── the manual-release machine: what happens while a release is PENDING ───────────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 1. The accepted activation design's release protocol has a
# state the base table cannot express: `manual_release_pending`, in which manual remains effective
# and completion happens ONLY through the bound callback that re-passes every CAS check — pending
# release id, the release's requested manual event, the case's CURRENT manual event still being
# that same event, an unexpired deadline by database time, and a sequence above the mark. These
# rows are that machine, over five closed facets; they partition all 72 states (proven by
# enumeration), and exactly ONE row completes the release.


@dataclass(frozen=True)
class ReleaseTransition:
    """One row of the release-pending table. Same discipline as `Transition`: the predicate is
    checked, the condition parses back, and EVERY visible cell and structured effect derives from
    the closed OutcomeKind/ReasonKind records — including `completes_release`, this machine's one
    transition the base table cannot have (re-audit `1826661..b5c7a83` finding 1)."""

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = ("condition", "record", "effective", "why")

    when: predicates.ReleaseWhen
    outcome: str
    reason: str
    record: str = field(default="", init=False)
    effective: str = field(default="", init=False)
    why: str = field(default="", init=False)
    records: bool = field(default=False, init=False)
    becomes_effective: bool = field(default=False, init=False)
    advances_high_water: bool = field(default=False, init=False)
    completes_release: bool = field(default=False, init=False)
    condition: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOME_KINDS:
            raise ValueError(f"unknown outcome kind {self.outcome!r}")
        if self.reason not in REASON_TEXTS:
            raise ValueError(f"unknown reason {self.reason!r}")
        kind = OUTCOME_KINDS[self.outcome]
        object.__setattr__(self, "record", kind.record_text)
        object.__setattr__(self, "effective", kind.effective_text)
        object.__setattr__(self, "why", REASON_TEXTS[self.reason])
        object.__setattr__(self, "records", kind.records)
        object.__setattr__(self, "becomes_effective", kind.becomes_effective)
        object.__setattr__(self, "advances_high_water", kind.advances_high_water)
        object.__setattr__(self, "completes_release", kind.completes_release)
        object.__setattr__(self, "condition", self.when.condition())


TRANSITION_SEMANTICS.update({
    "release.duplicate": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.DUP}),
                               binding=predicates.ANY_BINDING,
                               manual_event=predicates.ANY_EVENT,
                               deadline=predicates.ANY_DEADLINE,
                               sequence=predicates.ANY_SEQUENCE),
        "ack_duplicate", "rel_run_dedupe"),
    "release.complete": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_MATCH}),
                               manual_event=frozenset({predicates.EVENT_UNCHANGED}),
                               deadline=frozenset({predicates.DEADLINE_LIVE}),
                               sequence=frozenset({predicates.SEQ_ABOVE})),
        "rel_complete", "rel_complete_cas"),
    "release.foreign_above": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_NONE,
                                                  predicates.BIND_MISMATCH}),
                               manual_event=predicates.ANY_EVENT,
                               deadline=predicates.ANY_DEADLINE,
                               sequence=frozenset({predicates.SEQ_ABOVE})),
        "rel_record_advance", "rel_foreign_advances"),
    "release.reapproved_above": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_MATCH}),
                               manual_event=frozenset({predicates.EVENT_CHANGED}),
                               deadline=predicates.ANY_DEADLINE,
                               sequence=frozenset({predicates.SEQ_ABOVE})),
        "rel_record_advance", "rel_reapproved_above"),
    "release.expired_above": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_MATCH}),
                               manual_event=frozenset({predicates.EVENT_UNCHANGED}),
                               deadline=frozenset({predicates.DEADLINE_EXPIRED}),
                               sequence=frozenset({predicates.SEQ_ABOVE})),
        "rel_record_advance", "rel_expired_above"),
    "release.foreign_no_order": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_NONE,
                                                  predicates.BIND_MISMATCH}),
                               manual_event=predicates.ANY_EVENT,
                               deadline=predicates.ANY_DEADLINE,
                               sequence=frozenset({predicates.SEQ_ABSENT,
                                                   predicates.SEQ_NOT_ABOVE})),
        "rel_record_only", "rel_no_order"),
    "release.reapproved_no_order": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_MATCH}),
                               manual_event=frozenset({predicates.EVENT_CHANGED}),
                               deadline=predicates.ANY_DEADLINE,
                               sequence=frozenset({predicates.SEQ_ABSENT,
                                                   predicates.SEQ_NOT_ABOVE})),
        "rel_record_only", "rel_reapproved_no_order"),
    "release.expired_no_order": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_MATCH}),
                               manual_event=frozenset({predicates.EVENT_UNCHANGED}),
                               deadline=frozenset({predicates.DEADLINE_EXPIRED}),
                               sequence=frozenset({predicates.SEQ_ABSENT,
                                                   predicates.SEQ_NOT_ABOVE})),
        "rel_record_only", "rel_expired_no_order"),
    "release.bound_no_order": TransitionSemantic(
        "release", None,
        predicates.ReleaseWhen(duplicate=frozenset({predicates.FRESH}),
                               binding=frozenset({predicates.BIND_MATCH}),
                               manual_event=frozenset({predicates.EVENT_UNCHANGED}),
                               deadline=frozenset({predicates.DEADLINE_LIVE}),
                               sequence=frozenset({predicates.SEQ_ABSENT,
                                                   predicates.SEQ_NOT_ABOVE})),
        "rel_record_only_pending", "rel_bound_no_order"),
})

_RELEASE_ROW_ORDER = (
    "release.duplicate", "release.complete", "release.foreign_above",
    "release.reapproved_above", "release.expired_above", "release.foreign_no_order",
    "release.reapproved_no_order", "release.expired_no_order", "release.bound_no_order")


def release_transitions() -> tuple[ReleaseTransition, ...]:
    """The published release table, DERIVED from the registry."""
    return tuple(
        ReleaseTransition(when=TRANSITION_SEMANTICS[key].when,
                          outcome=TRANSITION_SEMANTICS[key].outcome,
                          reason=TRANSITION_SEMANTICS[key].reason)
        for key in _RELEASE_ROW_ORDER)


RELEASE_TRANSITIONS: tuple[ReleaseTransition, ...] = release_transitions()


@dataclass(frozen=True)
class ReleaseReplayRule:
    """The DISTINCT terminal-release-replay authority (R-audit-3 finding 5): replaying a
    TERMINATED release id is resolved from durable history BEFORE the table, on the release
    identity — a different authority from the table's run-id dedupe row, published as its own
    rule so the two cannot be conflated again."""

    text: str


RELEASE_REPLAY_RULE = ReleaseReplayRule(
    text="REPLAY RULE (distinct from run-id dedupe): a bound callback naming a release that "
         "has already TERMINATED — completed, expired, or cancelled — is answered from the "
         "durable terminal history record BEFORE this table is consulted, and returns the "
         "original terminal outcome unchanged. The replay must present the complete original "
         "binding (the release id AND its requested manual event); a partial match is an "
         "integrity hold, never the original outcome.")


@dataclass(frozen=True)
class ValidationRule:
    """One invalid-input class the receiver must refuse BEFORE consulting history or a table
    (R-audit-3 finding 6), with its exact disposition and an EXECUTABLE specimen: the authority
    verifier constructs the specimen and proves the reference receiver refuses it, so the
    published contract and the executable boundary cannot drift."""

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = ("invalid_input", "disposition")

    invalid_input: str
    disposition: str
    specimen: object = None  # zero-arg callable -> (state, callback, kwargs); checked, not shown


def _specimen_partial_binding():
    from docs.contracts import receiver_reference as receiver
    return (receiver.LedgerState(current_source="automatic"),
            receiver.Callback(case_id="c", run_id="r", release_id="R1"),
            {"phase": POST_024})


def _specimen_unknown_source():
    from docs.contracts import receiver_reference as receiver
    return (receiver.LedgerState(current_source="a-source-nobody-defined"),
            receiver.Callback(case_id="c", run_id="r"), {"phase": POST_024})


def _specimen_release_disagreement():
    from docs.contracts import receiver_reference as receiver
    return (receiver.LedgerState(
                current_source="manual",
                release=receiver.PendingRelease(release_id="R1",
                                                requested_manual_event_id="M1",
                                                deadline=1000)),
            receiver.Callback(case_id="c", run_id="r"), {"phase": POST_024})


def _specimen_forged_deadline():
    from docs.contracts import receiver_reference as receiver
    release = receiver.PendingRelease(release_id="R1", requested_manual_event_id="M1",
                                      deadline=1000)
    object.__setattr__(release, "deadline", True)
    return (receiver.LedgerState(current_source="manual_release_pending",
                                 current_manual_event_id="M1", release=release),
            receiver.Callback(case_id="c", run_id="r", release_id="R1",
                              manual_event_id="M1"),
            {"phase": POST_024, "now": 0})


def _specimen_conflicting_history():
    from docs.contracts import receiver_reference as receiver
    return (receiver.LedgerState(
                current_source="manual", current_manual_event_id="M2",
                release_history=(
                    receiver.ReleaseTerminal(release_id="R9",
                                             requested_manual_event_id="M9",
                                             terminal="completed"),
                    receiver.ReleaseTerminal(release_id="R9",
                                             requested_manual_event_id="M9",
                                             terminal="expired"))),
            receiver.Callback(case_id="c", run_id="r"), {"phase": POST_024})


def _specimen_replay_wrong_manual():
    from docs.contracts import receiver_reference as receiver
    return (receiver.LedgerState(
                current_source="manual", current_manual_event_id="M2",
                release_history=(
                    receiver.ReleaseTerminal(release_id="R9",
                                             requested_manual_event_id="M9",
                                             terminal="completed"),)),
            receiver.Callback(case_id="c", run_id="r", release_id="R9",
                              manual_event_id="M-WRONG"),
            {"phase": POST_024, "now": 500})


RECEIVER_VALIDATION_RULES: tuple[ValidationRule, ...] = (
    ValidationRule(
        invalid_input="a PARTIAL release binding (release_id without manual_event_id, or the "
                      "reverse)",
        disposition="integrity_mismatch — HOLD; do not record, classify, or 2xx-and-forget it",
        specimen=_specimen_partial_binding),
    ValidationRule(
        invalid_input="a current_source outside the closed set",
        disposition="HOLD the case; never guess a table",
        specimen=_specimen_unknown_source),
    ValidationRule(
        invalid_input="source and pending-release records that disagree (a pending release "
                      "outside manual_release_pending, or that state without one)",
        disposition="HOLD — an impossible state, not a classifiable callback",
        specimen=_specimen_release_disagreement),
    ValidationRule(
        invalid_input="a nested field outside its exact domain (a Boolean deadline, a "
                      "non-string id), however the record was produced",
        disposition="HOLD — every nested record is revalidated at every decision boundary",
        specimen=_specimen_forged_deadline),
    ValidationRule(
        invalid_input="duplicate or conflicting terminal-history records for one release id, "
                      "or a release both active and terminal",
        disposition="HOLD before any history lookup — history must be uniquely binding",
        specimen=_specimen_conflicting_history),
    ValidationRule(
        invalid_input="a terminal-release replay whose manual binding does not match the "
                      "original",
        disposition="HOLD — a replay must present the complete original binding to be "
                    "answered from history",
        specimen=_specimen_replay_wrong_manual),
)


def receiver_surface_projection() -> str:
    """The COMPLETE receiver surface as one canonical string — every OutcomeKind field, every
    reason text, every row pairing in table order, and the replay rule. The authority verifier
    pins this projection's digest, so re-pairing ids, rewriting a source record, or flipping a
    structured effect without review has nowhere to hide (R-audit-3 finding 1)."""
    surface = {
        "outcome_kinds": {
            name: {"records": kind.records, "becomes_effective": kind.becomes_effective,
                   "advances_high_water": kind.advances_high_water,
                   "completes_release": kind.completes_release,
                   "record_text": kind.record_text, "effective_text": kind.effective_text}
            for name, kind in sorted(OUTCOME_KINDS.items())
        },
        "reason_texts": dict(sorted(REASON_TEXTS.items())),
        "rows": {
            key: {"table": semantic.table, "phase": semantic.phase,
                  "condition": semantic.when.condition(),
                  "outcome": semantic.outcome, "reason": semantic.reason}
            for key, semantic in sorted(TRANSITION_SEMANTICS.items())
        },
        "base_order": _BASE_ROW_ORDER,
        "release_order": _RELEASE_ROW_ORDER,
        "replay_rule": RELEASE_REPLAY_RULE.text,
        "validation_rules": [
            # R-audit-4 finding 4: the specimen IDENTITY is pinned too — swapping every rule's
            # executable specimen for one easy throw is a re-pin, not a silent pass
            {"invalid_input": rule.invalid_input, "disposition": rule.disposition,
             "specimen": rule.specimen.__name__}
            for rule in RECEIVER_VALIDATION_RULES
        ],
    }
    return json.dumps(surface, sort_keys=True)

# ── what 024 still needs from the platform, keyed to the live obligations ─────────────────────────
#
# Re-audit `4f23f23..97deeae` finding 10. The document asked for three things: where the accepted
# ledger lives, whether it distinguishes automatic from manual, and who signs. TechCraft could
# answer all three and 024 would remain non-buildable, because the live O1-O4 contract also needs
# decisions only the platform can make. Asking the wrong questions politely is still not asking.
#
# Keyed to the obligation ids in
# `.agents/superpowers/specs/2026-07-22-pr7b-activation-platform-ordering-design.md`, and the
# authority test parses that file's live obligation ids and requires every one of them covered.


@dataclass(frozen=True)
class PendingInput:
    """One decision 024 cannot be built without.

    `accept` is what makes this an acceptance contract rather than a questionnaire (re-audit
    `4f23f23..122cc67` finding 10). `answer_type` is free text describing the SHAPE of an answer;
    it cannot tell a usable answer from an unusable one. A v1 HMAC version, a local process clock,
    a per-case release id, a writer-role list missing the inline manual approve — each looks like
    a complete answer and each leaves 024 unsafe. `accept` SCREENS those out.

    Passing the screen resolves nothing (audit `4c3015a..cccd5f7` finding 11). A screen judges the
    CONTENT of a candidate answer; resolving the obligation requires a versioned answer ARTIFACT —
    approved, signed, validated against a schema by a named authority — and no such schema or
    authority exists. `resolution_problems` below refuses every artifact until that unit ships
    (.agents/ROADMAP.md §C, PR 7b-inputs), so O1-O4 stay PENDING no matter how complete an emailed
    reply looks.
    """

    obligation: str  # O1-O4, as the live spec names them
    owner: str
    question: str
    answer_type: str
    authority: str
    blocked_deliverable: str  # cannot be built until this is answered AND the artifact schema ships
    accept: object = None  # Callable[[dict], list[str]] -> reasons it is NOT acceptable
    must_reject: tuple[tuple[str, dict], ...] = ()  # named unusable answers, for the RED tests

    # `authority` is the internal spec reference and `accept`/`must_reject` are executable —
    # checked, never shown.
    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = (
        "obligation", "owner", "question", "answer_type", "blocked_deliverable")



# ── what makes each answer USABLE, not merely present ─────────────────────────────────────────────
#
# Each returns the reasons an answer is unacceptable; empty means the answer's CONTENT screens
# clean — see `resolution_problems` for why that still resolves nothing. These are the constraints
# the accepted design already fixes, written where a reviewer can see them rather than left
# implicit in a spec paragraph.
#
# The ceilings and floors marked "sanity screen" below are exactly that (audit `4c3015a..cccd5f7`
# finding 11): they refuse magnitudes and absences no real agreement could contain — a
# trillion-second deadline, a one-character recovery contract — and they are NOT the negotiated
# values. The negotiated finite maxima, closed vocabularies, and governed registries arrive with
# the versioned answer artifact (PR 7b-inputs) and are deliberately not set here.

_DEADLINE_SCREEN_CEILING_SECONDS = 30 * 24 * 3600  # a request open for a month is not a deadline
_REAPER_SCREEN_CEILING_SECONDS = 24 * 3600  # a reaper that runs less than daily is not a cadence
_KNOWN_PARTY_TOKENS = ("platform", "techcraft", "ipv4.global")  # who an allocator can belong to


def _accept_principal(answer: dict) -> list[str]:
    problems = []
    if not str(answer.get("principal", "")).strip():
        problems.append("no platform principal named; production requires a non-blank principal")
    if str(answer.get("hmac_version", "")).lower() != "v2":
        problems.append(
            "admission requires HMAC v2; a v1 signature is path-unbound and cannot authorise a "
            "release request")
    if not str(answer.get("key_id", "")).strip():
        problems.append("no key id named, so the principal cannot be bound to a verified signature")
    return problems


def _accept_deadline(answer: dict) -> list[str]:
    problems = []
    if str(answer.get("clock", "")).lower() not in {"platform db", "platform database"}:
        problems.append(
            "the deadline must be measured against the PLATFORM DATABASE clock; a local process "
            "clock cannot expire a request during a restart with no traffic")
    seconds = answer.get("ttl_seconds")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
        problems.append("the TTL must be a positive whole number of seconds")
    elif seconds > _DEADLINE_SCREEN_CEILING_SECONDS:
        problems.append(
            f"a TTL of {seconds} seconds is beyond any plausible release window — refused as a "
            "sanity screen; the negotiated finite maximum arrives with the versioned answer "
            "artifact, not here")
    return problems


def _accept_terminal_authority(answer: dict) -> list[str]:
    problems = []
    if str(answer.get("terminal_authority", "")).lower() != "platform":
        problems.append(
            "the platform must be the SOLE terminal and expiry authority; two systems cannot both "
            "own the final state under one lock")
    cadence = answer.get("reaper_cadence_seconds")
    if not isinstance(cadence, int) or isinstance(cadence, bool) or cadence <= 0:
        problems.append("the reaper needs an explicit bounded cadence in seconds")
    elif cadence > _REAPER_SCREEN_CEILING_SECONDS:
        problems.append(
            f"a reaper cadence of {cadence} seconds cannot expire anything in useful time — "
            "refused as a sanity screen; the negotiated cadence bound arrives with the versioned "
            "answer artifact, not here")
    recovery = str(answer.get("outcome_recovery", "")).strip()
    if not recovery:
        problems.append("no recovery path for a lost manual.release_outcome")
    elif len(recovery.split()) < 3:
        problems.append(
            f"{recovery!r} cannot describe a recovery mechanism — refused as a sanity screen; "
            "the closed mechanism vocabulary arrives with the versioned answer artifact, not here")
    return problems


def _accept_release_id(answer: dict) -> list[str]:
    problems = []
    if str(answer.get("scope", "")).lower() != "global":
        problems.append(
            "release_id must be GLOBALLY unique; a per-case scope admits the same id on a second "
            "case, which the design requires to be rejected")
    allocator = str(answer.get("allocated_by", "")).strip()
    if not allocator:
        problems.append("nobody is named as allocating the id")
    elif not any(token in allocator.lower() for token in _KNOWN_PARTY_TOKENS):
        problems.append(
            f"{allocator!r} is not identifiable as a party to this contract, so neither side can "
            "bind a governance obligation to it — refused as a sanity screen; the governed "
            "allocator registry arrives with the versioned answer artifact, not here")
    return problems


# The writer roles that must ALL be fenced. The inline manual approve is the one a quiescence
# preflight cannot see, so a matrix that omits it reads complete and is not.
REQUIRED_WRITER_ROLES = frozenset({
    "pipeline decide", "api inline reviewer.manual_approve", "outbox publisher",
})

# Our own executable process inventory, and it is OURS to demand a stance on (audit
# `4c3015a..cccd5f7` finding 11): a matrix naming only the writers reads complete while saying
# nothing about dev_worker or retention, and an unaccounted role is precisely where an unfenced
# writer hides. Mirrors kyc_tool.config.ProcessRole so this module stays import-pure; the
# authority test binds the two exactly, the same bind that holds plan.PLAN_ROLES.
ACCOUNTED_PROCESS_ROLES = ("api", "pipeline_worker", "outbox_worker", "retention", "dev_worker")
# The stance for OUR roles is not negotiated — it is DERIVED from the canonical capability map
# (kyc_tool.config.ROLE_CAPABILITIES; gate audit `6c4f54a..91fbde3` finding 9): a role is a
# writer exactly when it can write a decision or publish a callback. The previously accepted
# matrix called dev_worker a non-writer while its entry point constructs BOTH the pipeline
# worker and the outbox publisher; a stop-and-attest inventory built on that omits a live
# writer. This mirror is bound to the derived map by the authority test.
LOCAL_WRITER_STANCES = {
    "api": "writer",
    "pipeline_worker": "writer",
    "outbox_worker": "writer",
    "retention": "non-writer",
    "dev_worker": "writer",
}
_ROLE_STANCES = frozenset({"writer", "non-writer"})


def _accept_writer_matrix(answer: dict) -> list[str]:
    problems = []
    declared = {str(role).strip().lower() for role in answer.get("writer_roles", ())}
    missing = sorted(REQUIRED_WRITER_ROLES - declared)
    if missing:
        problems.append(f"the writer-role matrix omits {missing}")
    accounting = answer.get("process_roles")
    if not isinstance(accounting, dict):
        problems.append(
            "no per-process accounting: every process role we run must be declared a decision "
            "writer or a non-writer for the 024 window; a role the matrix never mentions is "
            "where an unfenced writer hides")
    else:
        names = {str(name) for name in accounting}
        unaccounted = sorted(set(ACCOUNTED_PROCESS_ROLES) - names)
        if unaccounted:
            problems.append(f"the accounting leaves process roles {unaccounted} without a stance")
        unknown = sorted(names - set(ACCOUNTED_PROCESS_ROLES))
        if unknown:
            problems.append(f"the accounting names process roles we do not run: {unknown}")
        vague = sorted(str(k) for k, v in accounting.items() if v not in _ROLE_STANCES)
        if vague:
            problems.append(
                f"{vague} carry a stance other than 'writer'/'non-writer'; a hedged stance "
                "accounts for nothing")
        wrong = sorted(
            role for role, stance in LOCAL_WRITER_STANCES.items()
            if role in accounting and accounting[role] in _ROLE_STANCES
            and accounting[role] != stance)
        if wrong:
            problems.append(
                f"{wrong} carry a stance that contradicts our canonical capability map — our "
                "side's roles are classified by what their entry points construct, not by "
                "agreement; dev_worker runs the pipeline AND the publisher and IS a writer")
    if answer.get("old_image_full_stop") is not True:
        problems.append(
            "the full maintenance stop during an old-image transition is not agreed; an old-image "
            "writer does not take the admission fence, so the fence proves nothing about it")
    return problems


PENDING_024_INPUTS: tuple[PendingInput, ...] = (
    PendingInput(
        obligation="O1",
        owner="TechCraft",
        question="Which platform principal is authorised to request a manual release, and what "
                 "identity does it present? Production requires a non-blank principal, and the "
                 "auth result must carry the verified HMAC VERSION, not just verified/not.",
        answer_type="principal identifier + key id + which HMAC version it signs with",
        authority="activation spec O1 (manual-release authority, executable and relationally bound)",
        blocked_deliverable="the manual.release_requested request model and its admission gate",
        accept=_accept_principal,
        must_reject=(
            ("a v1 signature", {"principal": "platform-svc", "hmac_version": "v1", "key_id": "k1"}),
            ("no principal", {"principal": "", "hmac_version": "v2", "key_id": "k1"}),
            ("no key id", {"principal": "platform-svc", "hmac_version": "v2", "key_id": " "}),
        ),
    ),
    PendingInput(
        obligation="O1",
        owner="TechCraft",
        question="What is the authoritative deadline or TTL on a release request, and whose clock "
                 "is it measured against?",
        answer_type="duration + the clock that owns it (platform DB time, per O2)",
        authority="activation spec O1 (request model: release id, requested manual event, "
                  "authoritative deadline/TTL)",
        blocked_deliverable="the release request model and the expiry reaper",
        accept=_accept_deadline,
        must_reject=(
            ("a local process clock", {"clock": "local process", "ttl_seconds": 900}),
            ("no TTL", {"clock": "platform db", "ttl_seconds": None}),
            ("a zero TTL", {"clock": "platform db", "ttl_seconds": 0}),
            ("a trillion-second TTL", {"clock": "platform db", "ttl_seconds": 10**12}),
        ),
    ),
    PendingInput(
        obligation="O2",
        owner="TechCraft",
        question="Confirm the platform is the SOLE terminal and expiry authority: its CAS/reaper "
                 "transaction writes the result and enqueues a signed, retried "
                 "manual.release_outcome. We mirror that event and never choose expiry ourselves. "
                 "What is the reaper's cadence, and how is a lost outcome recovered?",
        answer_type="written confirmation + reaper cadence + outcome redelivery/recovery contract",
        authority="activation spec O2 (two-system convergence; no-traffic expiry)",
        blocked_deliverable="the outcome mirror, the expiry path, and every convergence test",
        accept=_accept_terminal_authority,
        must_reject=(
            ("shared terminal authority", {"terminal_authority": "both",
                                           "reaper_cadence_seconds": 60,
                                           "outcome_recovery": "retry with replay"}),
            ("an unbounded reaper", {"terminal_authority": "platform",
                                     "reaper_cadence_seconds": None,
                                     "outcome_recovery": "retry with signed replay"}),
            ("no outcome recovery", {"terminal_authority": "platform",
                                     "reaper_cadence_seconds": 60, "outcome_recovery": ""}),
            ("a trillion-second reaper", {"terminal_authority": "platform",
                                          "reaper_cadence_seconds": 10**12,
                                          "outcome_recovery": "retry with signed replay"}),
            ("a one-character recovery", {"terminal_authority": "platform",
                                          "reaper_cadence_seconds": 60,
                                          "outcome_recovery": "x"}),
        ),
    ),
    PendingInput(
        obligation="O3",
        owner="TechCraft",
        question="Confirm release_id is GLOBALLY unique, not unique per case, and that a reused "
                 "id is rejected outright rather than admitted on a second case.",
        answer_type="written confirmation of global uniqueness + who allocates the id",
        authority="activation spec O3 (release-id scope and governance)",
        blocked_deliverable="the UNIQUE(release_id) constraint and its 409 path",
        accept=_accept_release_id,
        must_reject=(
            ("a per-case scope", {"scope": "per case", "allocated_by": "platform"}),
            ("no allocator", {"scope": "global", "allocated_by": ""}),
            ("an unknown allocator", {"scope": "global", "allocated_by": "somebody"}),
        ),
    ),
    PendingInput(
        obligation="O4",
        owner="both",
        question="Agree the complete WRITER-ROLE MATRIX for the 024 window: which processes on "
                 "each side write decisions — with an explicit writer-or-not stance for EVERY "
                 "process role we run (api, pipeline_worker, outbox_worker, retention, "
                 "dev_worker), because a role the matrix never mentions is where an unfenced "
                 "writer hides — and confirm that during any old-image transition the full "
                 "maintenance stop applies, because an old-image writer does not take the "
                 "admission fence and the fence proves nothing about it.",
        answer_type="role list per side + a stance on every process role + written agreement on "
                    "the old-image stop",
        authority="activation spec O4 (both decision writers fenced) + .agents/ROADMAP.md",
        blocked_deliverable="the activation migration's admission fence and its preflight",
        accept=_accept_writer_matrix,
        must_reject=(
            ("the inline manual approve omitted",
             {"writer_roles": ("pipeline decide", "outbox publisher"),
              "old_image_full_stop": True,
              "process_roles": {"api": "writer", "pipeline_worker": "writer",
                                "outbox_worker": "writer", "retention": "non-writer",
                                "dev_worker": "writer"}}),
            ("no old-image stop",
             {"writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": False,
              "process_roles": {"api": "writer", "pipeline_worker": "writer",
                                "outbox_worker": "writer", "retention": "non-writer",
                                "dev_worker": "writer"}}),
            ("dev_worker and retention unaccounted",
             {"writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": True,
              "process_roles": {"api": "writer", "pipeline_worker": "writer",
                                "outbox_worker": "writer"}}),
            ("dev_worker demoted to non-writer",
             {"writer_roles": tuple(REQUIRED_WRITER_ROLES), "old_image_full_stop": True,
              "process_roles": {"api": "writer", "pipeline_worker": "writer",
                                "outbox_worker": "writer", "retention": "non-writer",
                                "dev_worker": "non-writer"}}),
        ),
    ),
)

# ── why a screened answer still resolves nothing ──────────────────────────────────────────────────
#
# Audit `4c3015a..cccd5f7` finding 11, folded fail-closed. The acceptors above are content
# SCREENS. Resolving an obligation requires a versioned answer ARTIFACT — approved, signed,
# validated against a schema by a named authority — and no such schema or authority exists.
# `resolution_problems` therefore refuses EVERY artifact, on that absence rather than on content,
# so nothing in this repository can mark O1-O4 answered. The schema is a reserved unbuilt unit
# (.agents/ROADMAP.md §C, PR 7b-inputs); shipping it must replace the anchor AND rewrite the gate
# in the same change — the gate's other branch is a tripwire that says exactly that.

# ── the typed capability slots are RUNTIME state (re-audit `1826661..b5c7a83` finding 6) ──────────
#
# Gate audit finding 13 made the absences TYPED; this re-audit showed the slots were still
# documentation globals under `docs.contracts` — a package runtime is forbidden to import — so
# "shipping any part forces the gate forward" rested on a four-string ban that a provider with
# different identifiers walked straight past. The registry now lives in
# `kyc_tool.capabilities`: every consumer below resolves through it at CALL time, the published
# text derives from what it resolves, and only `register()` — the call production makes to ship
# a provider — can flip a slot, which trips every consumer's rewrite-me branch and fails the
# claim verifiers until the claims, the gates, and the ROADMAP unit move in the same change.
# The forcing guarantee is scoped where it is executable: an UNREGISTERED provider moves
# nothing, because nothing official consults anything but the registry.

# Absence anchor for the schema payload itself; the registry slot is what the gates consume.
ANSWER_ARTIFACT_SCHEMA: object = None


def resolution_problems(item: PendingInput, artifact: object) -> list[str]:
    """Why `artifact` cannot RESOLVE `item`. Non-empty for every input in the repository's
    current state — both branches refuse; only the change that ships the authority may open
    one, and it must rewrite this gate to consume it."""
    authority = resolve_capability(SLOT_ANSWER_ARTIFACT)
    if isinstance(authority, MissingCapability):
        return [
            f"{item.obligation}: unresolvable — {authority.reason}. Screening "
            "an answer's content is not resolution; the obligation stays PENDING until the "
            f"platform-artifact unit ships (.agents/ROADMAP.md §C, "
            f"{authority.roadmap_unit})."
        ]
    return [
        f"{item.obligation}: an answer-artifact authority is REGISTERED in "
        "kyc_tool.capabilities but this gate still refuses by default — rewrite "
        "resolution_problems to validate against it in the same change that ships it."
    ]


# ── rotation retirement is BLOCKED: the evidence does not exist ───────────────────────────────────
#
# Audit `4c3015a..cccd5f7` finding 3, folded fail-closed. The rotation procedure's two retirement
# steps consume evidence this system cannot produce: nothing durable records WHICH key id an
# inbound request verified under (the durable witness is version-aggregate; process-local counters
# reset and see one replica), and the only closed drained-cutover record attests an outbox-ceiling
# Settings value, not an HMAC signer target. So both steps are published BLOCKED where the reader
# would act, and each carries an executable gate that refuses every evidence object constructible
# today. The capability is a reserved unbuilt unit (.agents/ROADMAP.md §C, PR 5c); shipping it
# trips the gates' anchors and the claim's verifier, forcing this claim forward in the same change.

INBOUND_RETIREMENT_EVIDENCE_KINDS = frozenset(
    {"durable_per_key_fleet_witness", "signed_fleet_receipt"})
OUTBOUND_RETIREMENT_EVIDENCE_KINDS = frozenset({"hmac_signer_cutover_record"})

# Absence anchor: no versioned signer-fleet receipt schema exists. Same contract as
# ANSWER_ARTIFACT_SCHEMA above.
SIGNED_FLEET_RECEIPT_SCHEMA: object = None


def _refuse_inbound_retirement(evidence: object) -> list[str]:
    """Why `evidence` cannot authorize retiring the old INBOUND key id. Never empty today."""
    if type(evidence) is not dict:
        return ["retirement evidence must be a mapping naming its kind"]
    kind = evidence.get("kind")
    if kind not in INBOUND_RETIREMENT_EVIDENCE_KINDS:
        return [
            f"evidence kind {kind!r} cannot authorize retirement: only a durable fleet-wide "
            "per-key acceptance witness or a signed signer-fleet receipt can prove the old id "
            "is quiet. Process-local counters reset and reach one replica, the durable "
            "observation row is version-aggregate and never keyed by key id, and waiting any "
            "interval proves nothing about the traffic during it."
        ]
    authority = resolve_capability(SLOT_RETIREMENT_EVIDENCE)
    if not isinstance(authority, MissingCapability):
        return [
            "a retirement authority is REGISTERED in kyc_tool.capabilities but this gate "
            "still refuses by default — rewrite _refuse_inbound_retirement to consume it in "
            "the same change that ships it"
        ]
    if kind == "durable_per_key_fleet_witness":
        return [
            f"{authority.reason}: kyc_tool.api.hmac_witness has no "
            "inbound_zero_for_key, and hmac_v1_observation records acceptance per HMAC "
            f"version, not per key id (.agents/ROADMAP.md §C, "
            f"{authority.roadmap_unit} — reserved, unbuilt)"
        ]
    return [
        f"{authority.reason}: no signed fleet-receipt schema or verifying authority "
        f"exists (.agents/ROADMAP.md §C, {authority.roadmap_unit} — reserved, "
        "unbuilt)"
    ]


def _refuse_outbound_retirement(evidence: object) -> list[str]:
    """Why `evidence` cannot authorize TechCraft retiring the old OUTBOUND key. Never empty
    today."""
    if type(evidence) is not dict:
        return ["retirement evidence must be a mapping naming its kind"]
    kind = evidence.get("kind")
    if kind not in OUTBOUND_RETIREMENT_EVIDENCE_KINDS:
        return [
            f"evidence kind {kind!r} cannot authorize retirement: in a mixed fleet a callback "
            "observed under the new key id does not prove no replica still signs with the old "
            "one, and the closed drained-cutover record we can produce attests the outbox "
            "attempt ceiling — a Settings value, not a signer key target."
        ]
    authority = resolve_capability(SLOT_RETIREMENT_EVIDENCE)
    if not isinstance(authority, MissingCapability):
        return [
            "a retirement authority is REGISTERED in kyc_tool.capabilities but this gate "
            "still refuses by default — rewrite _refuse_outbound_retirement to consume it in "
            "the same change that ships it"
        ]
    return [
        f"{authority.reason}: kyc_tool.ops.cutover's closed record attests "
        "KYC_OUTBOX_MAX_ATTEMPTS, and no record names a target key id, secret digest, and the "
        f"exact attested publisher roles (.agents/ROADMAP.md §C, "
        f"{authority.roadmap_unit} — reserved, unbuilt)"
    ]


# ── the rotation procedure as CLOSED step records (gate finding 13, second half) ──────────────────
#
# The published rotation text used to be a tuple of free prose, so "EMERGENCY OVERRIDE: remove
# the old inbound key after one second; the retirement gate does not apply" could be appended and
# nothing noticed. The lines are now DERIVED: each phase is a RotationStep whose action comes
# from a closed set and whose sentence lives in a reviewed map; the two evidence-gated actions
# carry their BLOCKED suffixes only while the capability slot is missing; the rationale lines are
# a closed tuple. The verifier requires the claim value to EQUAL the derivation, so an alternate
# retirement path has no syntax to exist in.

ACT_DEPLOY_ROTATION_ENTRY = "deploy_rotation_entry"
ACT_CONFIRM_BOTH = "confirm_both_accepted"
ACT_SWITCH_SIGNER = "switch_signer"
ACT_PROVE_QUIET = "prove_old_id_quiet"
ACT_PROMOTE_REMOVE = "promote_then_remove"
ACT_ACCEPT_BOTH = "accept_old_and_new"
ACT_DRAIN_ATTEST = "hard_stop_attest_zero"
ACT_DEPLOY_SOLE_SIGNER = "deploy_sole_new_signer"
ACT_CONFIRM_NEW = "confirm_new_key_arrivals"
ACT_RETIRE_OLD = "retire_old_key"

ROTATION_ACTIONS = frozenset({
    ACT_DEPLOY_ROTATION_ENTRY, ACT_CONFIRM_BOTH, ACT_SWITCH_SIGNER, ACT_PROVE_QUIET,
    ACT_PROMOTE_REMOVE, ACT_ACCEPT_BOTH, ACT_DRAIN_ATTEST, ACT_DEPLOY_SOLE_SIGNER,
    ACT_CONFIRM_NEW, ACT_RETIRE_OLD,
})
GATED_ACTIONS = frozenset({ACT_PROVE_QUIET, ACT_RETIRE_OLD})

_ACTION_SENTENCES = {
    ACT_DEPLOY_ROTATION_ENTRY: "We deploy your NEW key id as a rotation entry alongside the old "
                               "ACTIVE one, fleet-wide.",
    ACT_CONFIRM_BOTH: "We confirm both are accepted.",
    ACT_SWITCH_SIGNER: "You switch your signer to the new id.",
    ACT_PROVE_QUIET: "We prove no request has arrived under the old id",
    ACT_PROMOTE_REMOVE: "We PROMOTE the new id to active with the old one demoted to a rotation "
                        "entry, then remove the old entry.",
    ACT_ACCEPT_BOTH: "You start accepting old and new.",
    ACT_DRAIN_ATTEST: "We hard-stop and attest ZERO publishers, so no replica is still signing "
                      "with the old key.",
    ACT_DEPLOY_SOLE_SIGNER: "We deploy the sole new signer.",
    ACT_CONFIRM_NEW: "We resume and you confirm callbacks are arriving under the new key id.",
    ACT_RETIRE_OLD: "You retire the old key",
}
_GATED_SUFFIXES = {
    ACT_PROVE_QUIET: " — the step that is BLOCKED today; see the gate table directly below.",
    ACT_RETIRE_OLD: " — BLOCKED today until we can hand you a signer-target attestation record, "
                    "which does not exist yet; see the gate table directly below.",
}


@dataclass(frozen=True)
class ActionSemantics:
    """What one rotation action NEEDS and what it MAKES TRUE (re-audit `1826661..b5c7a83`
    finding 7). The closed action set stopped a foreign step from existing, but a known action
    id could still be reordered or resemanticized and certify. The simulation below walks each
    direction's phases against these typed preconditions/effects, so safety comes from
    transition semantics, not from action names or the luck of a keyword index."""

    requires: frozenset
    effects: frozenset
    gated: bool = False  # consumes the retirement-evidence capability; BLOCKED while missing
    destructive: bool = False  # removes or retires a live credential


ROTATION_SEMANTICS: dict[str, ActionSemantics] = {
    ACT_DEPLOY_ROTATION_ENTRY: ActionSemantics(
        requires=frozenset(),
        effects=frozenset({"in.new_entry_deployed"})),
    ACT_CONFIRM_BOTH: ActionSemantics(
        requires=frozenset({"in.new_entry_deployed"}),
        effects=frozenset({"in.both_accepted_confirmed"})),
    ACT_SWITCH_SIGNER: ActionSemantics(
        requires=frozenset({"in.both_accepted_confirmed"}),
        effects=frozenset({"in.their_signer_new"})),
    ACT_PROVE_QUIET: ActionSemantics(
        requires=frozenset({"in.their_signer_new"}),
        effects=frozenset({"in.old_id_quiet_proven"}), gated=True),
    ACT_PROMOTE_REMOVE: ActionSemantics(
        requires=frozenset({"in.old_id_quiet_proven"}),
        effects=frozenset({"in.new_active_old_removed"}), destructive=True),
    ACT_ACCEPT_BOTH: ActionSemantics(
        requires=frozenset(),
        effects=frozenset({"out.both_accepted"})),
    ACT_DRAIN_ATTEST: ActionSemantics(
        requires=frozenset({"out.both_accepted"}),
        effects=frozenset({"out.fleet_drained_zero"})),
    ACT_DEPLOY_SOLE_SIGNER: ActionSemantics(
        requires=frozenset({"out.fleet_drained_zero"}),
        effects=frozenset({"out.our_signer_new"})),
    ACT_CONFIRM_NEW: ActionSemantics(
        requires=frozenset({"out.our_signer_new"}),
        effects=frozenset({"out.new_arrivals_confirmed"})),
    ACT_RETIRE_OLD: ActionSemantics(
        requires=frozenset({"out.new_arrivals_confirmed"}),
        effects=frozenset({"out.old_key_retired"}), gated=True, destructive=True),
}

_DIRECTION_TERMINAL_FACTS = {
    "INBOUND": "in.new_active_old_removed",
    "OUTBOUND": "out.old_key_retired",
}


@dataclass(frozen=True)
class RotationStep:
    """One phase of one rotation direction. `action` must come from the closed set — an
    emergency-override step has no syntax — and only the two evidence-gated actions may carry a
    BLOCKED suffix, which they do exactly while the retirement capability slot is missing."""

    direction: str
    number: int
    action: str

    def __post_init__(self) -> None:
        if self.direction not in ("INBOUND", "OUTBOUND"):
            raise ValueError(f"unknown direction {self.direction!r}")
        if self.action not in ROTATION_ACTIONS:
            raise ValueError(f"{self.action!r} is not in the closed action set")

    def sentence(self) -> str:
        text = _ACTION_SENTENCES[self.action]
        if self.action in GATED_ACTIONS:
            if not isinstance(resolve_capability(SLOT_RETIREMENT_EVIDENCE),
                              MissingCapability):
                raise RuntimeError(
                    "a retirement authority is registered in kyc_tool.capabilities: rewrite "
                    "the rotation derivation to say what is now true, in the same change"
                )
            return text + _GATED_SUFFIXES[self.action]
        return text


ROTATION_STEPS: tuple[RotationStep, ...] = (
    RotationStep("INBOUND", 1, ACT_DEPLOY_ROTATION_ENTRY),
    RotationStep("INBOUND", 2, ACT_CONFIRM_BOTH),
    RotationStep("INBOUND", 3, ACT_SWITCH_SIGNER),
    RotationStep("INBOUND", 4, ACT_PROVE_QUIET),
    RotationStep("INBOUND", 5, ACT_PROMOTE_REMOVE),
    RotationStep("OUTBOUND", 1, ACT_ACCEPT_BOTH),
    RotationStep("OUTBOUND", 2, ACT_DRAIN_ATTEST),
    RotationStep("OUTBOUND", 3, ACT_DEPLOY_SOLE_SIGNER),
    RotationStep("OUTBOUND", 4, ACT_CONFIRM_NEW),
    RotationStep("OUTBOUND", 5, ACT_RETIRE_OLD),
)

_DIRECTION_PREFIXES = {
    "INBOUND": "INBOUND (your key, verifying your calls to us) — five phases, in this order. ",
    "OUTBOUND": "OUTBOUND (our key, signing our callbacks to you) — THE OVERLAP IS YOURS TO "
                "HOLD. We sign with exactly one outbound key. ",
}

# Closed, reviewed rationale lines — the WHY beside the steps. Adding one is a reviewed edit
# here, never an append to the claim value, and the prose scan below refuses retirement
# semantics anywhere outside the derived lines.
ROTATION_RATIONALES = (
    "Phase 5 is not optional bookkeeping. Deleting the old entry while it is still the ACTIVE "
    "key leaves us with no active key and the new id resolvable only as a rotation entry — a "
    "state that verifies nothing once the entry is dropped. The promotion is what makes the new "
    "key primary.",
    "Phase 2 is why this is a drain and not a rolling deploy. In a mixed fleet, seeing one "
    "callback under the new key does not prove no replica is still signing with the old one; "
    "retiring early makes the next old-signed callback fail verification and dead-letter.",
    "Rotation secrets are full verification credentials and carry the same floor as the active "
    "key: at least 32 characters, a non-blank key id, and no collision with the active id.",
)


def _direction_line(direction: str) -> str:
    steps = [s for s in ROTATION_STEPS if s.direction == direction]
    assert [s.number for s in steps] == [1, 2, 3, 4, 5]
    return _DIRECTION_PREFIXES[direction] + " ".join(
        f"({s.number}) {s.sentence()}" for s in steps)


def rotation_lines() -> tuple[str, ...]:
    """The published rotation lines, derived — the ONLY way they come to exist."""
    return (
        _direction_line("INBOUND"),
        ROTATION_RATIONALES[0],
        _direction_line("OUTBOUND"),
        ROTATION_RATIONALES[1],
        ROTATION_RATIONALES[2],
    )


def rotation_semantics_problems() -> list[str]:
    """Simulate the published procedure against the typed action semantics; empty when every
    phase's preconditions are established by the effects before it and each direction reaches
    its terminal state. Reads module state at CALL time, so a mutated step tuple, sentence
    map, or semantics map is judged exactly as it would publish."""
    problems: list[str] = []
    if set(ROTATION_SEMANTICS) != ROTATION_ACTIONS:
        problems.append("the semantics map and the closed action set diverge")
    if {a for a, s in ROTATION_SEMANTICS.items() if s.gated} != GATED_ACTIONS:
        problems.append("the gated actions and the semantics map's gated flags diverge")
    actions = [s.action for s in ROTATION_STEPS]
    duplicated = sorted({a for a in actions if actions.count(a) > 1})
    if duplicated:
        problems.append(f"actions appear behind more than one phase number: {duplicated}")
    for direction in ("INBOUND", "OUTBOUND"):
        steps = sorted((s for s in ROTATION_STEPS if s.direction == direction),
                       key=lambda s: s.number)
        if [s.number for s in steps] != [1, 2, 3, 4, 5]:
            problems.append(
                f"{direction}: phases are not exactly 1-5: {[s.number for s in steps]}")
            continue
        established: set[str] = set()
        for step in steps:
            semantics = ROTATION_SEMANTICS.get(step.action)
            if semantics is None:
                problems.append(
                    f"{direction} phase {step.number}: {step.action!r} has no typed semantics")
                continue
            missing = semantics.requires - established
            if missing:
                problems.append(
                    f"{direction} phase {step.number} ({step.action}) publishes an instruction "
                    f"whose preconditions are not established: missing {sorted(missing)}")
            established |= semantics.effects
        terminal = _DIRECTION_TERMINAL_FACTS[direction]
        if terminal not in established:
            problems.append(f"{direction}: the procedure never establishes {terminal}")
    gated_effects = frozenset().union(
        *(s.effects for s in ROTATION_SEMANTICS.values() if s.gated))
    for action, semantics in ROTATION_SEMANTICS.items():
        if (semantics.destructive and not semantics.gated
                and not semantics.requires & gated_effects):
            problems.append(
                f"{action}: a destructive step does not sit behind an evidence-gated proof")
    return problems


def rotation_surface_projection() -> str:
    """The complete closed NORMATIVE rotation surface as one canonical string — every action's
    typed semantics, its published sentence, and its gated suffix. The authority verifier pins
    this projection's digest, so resemanticizing a known action id, rewriting a sentence, or
    widening a gate is a re-pin at the verifier boundary: the act of review. Rationales are
    nonnormative and pinned separately."""
    surface = {
        action: {
            "requires": sorted(semantics.requires),
            "effects": sorted(semantics.effects),
            "gated": semantics.gated,
            "destructive": semantics.destructive,
            "sentence": _ACTION_SENTENCES[action],
            "gated_suffix": _GATED_SUFFIXES.get(action),
        }
        for action, semantics in sorted(ROTATION_SEMANTICS.items())
    }
    # R-audit-3 finding 9: the direction PREFIXES and terminal facts are normative rendered
    # text too — inside this pinned projection, so "RETIRE THE OLD KEY FIRST" cannot enter
    # through an unpinned prefix with a regenerated claim.
    surface["__direction_prefixes__"] = dict(sorted(_DIRECTION_PREFIXES.items()))
    surface["__terminal_facts__"] = dict(sorted(_DIRECTION_TERMINAL_FACTS.items()))
    # R-audit-4 finding 3: the gate table's PUBLISHED text — why_blocked, unblocked_by, the
    # gated transition clause — is binding retirement instruction; it lives in this pinned
    # surface too, in order, so an EMERGENCY OVERRIDE planted in unblocked_by with a
    # regenerated claim is a re-pin, never a silent certification.
    surface["__retirement_gates__"] = [
        {field_name: getattr(gate, field_name) for field_name in gate.PUBLISHED_FIELDS}
        for gate in ROTATION_RETIREMENT_GATES
    ]
    return json.dumps(surface, sort_keys=True)


_RETIREMENT_TOKENS = ("retir", "remove the old", "delete the old", "deleting the old")


def rotation_prose_problems(lines) -> list[str]:
    """Any line outside the derivation that carries retirement semantics — the override
    specimen's shape. The equality check catches every foreign line; this names the dangerous
    ones specifically so the refusal reads as what it is."""
    derived = set(rotation_lines())
    return [
        f"foreign line carries retirement semantics: {line[:80]!r}"
        for line in lines
        if line not in derived and any(token in line.lower() for token in _RETIREMENT_TOKENS)
    ]


@dataclass(frozen=True)
class RetirementGate:
    """One rotation step published BLOCKED, with the executable gate that keeps it blocked.

    `transition` is the exact clause of WIRE.SIGN.ROTATION it gates — the verifier requires it
    verbatim in that claim's matching direction line, so rewriting the procedure around the gate
    (finding 3's wait-one-second replacement) breaks the bind instead of passing unnoticed.
    `refuse` returns the reasons an evidence object cannot authorize the step; it returns a
    non-empty list for EVERY object constructible in this repository's current state, which is
    what BLOCKED means here. `must_reject` names the specimens the tests hold red forever.
    """

    direction: str
    transition: str
    why_blocked: str
    unblocked_by: str
    refuse: object = None  # Callable[[object], list[str]] — checked, not shown
    must_reject: tuple[tuple[str, dict], ...] = ()  # checked, not shown

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = (
        "direction", "transition", "why_blocked", "unblocked_by")


ROTATION_RETIREMENT_GATES: tuple[RetirementGate, ...] = (
    RetirementGate(
        direction="INBOUND",
        transition="We prove no request has arrived under the old id",
        why_blocked=(
            "Nothing durable records WHICH key id a request verified under: the durable "
            "observation row counts per HMAC version, and process-local counters reset on "
            "restart and see a single replica. The proof cannot currently be produced, so the "
            "promote-and-remove step never starts."
        ),
        unblocked_by=(
            "A durable fleet-wide per-key acceptance witness with a defined zero window, or a "
            "signed signer-fleet cutover receipt with bounded observation — a reserved future "
            "unit on our roadmap, not shipped. Until then hold the overlap: both entries stay "
            "deployed and verifying, which is safe indefinitely."
        ),
        refuse=_refuse_inbound_retirement,
        must_reject=(
            ("aggregate version-level observation",
             {"kind": "aggregate_v1_observation", "accepted_count": 0, "window_days": 30}),
            ("process-local counters",
             {"kind": "process_local_counters", "old_key_hits": 0}),
            ("waiting one second",
             {"kind": "wait", "seconds": 1}),
            ("a perfectly shaped witness claim nothing shipped can back",
             {"kind": "durable_per_key_fleet_witness",
              "authority": "kyc_tool.api.hmac_witness.inbound_zero_for_key",
              "window_days": 30, "zero_confirmed": True}),
            ("a perfectly shaped receipt nothing shipped can verify",
             {"kind": "signed_fleet_receipt", "signed_by": "signer-fleet operator",
              "signature_verified": True, "observation_days": 7}),
        ),
    ),
    RetirementGate(
        direction="OUTBOUND",
        transition="You retire the old key",
        why_blocked=(
            "Retiring is safe only on our written attestation that zero publishers still sign "
            "with the old key, bound to that exact key. The hard-stop mechanism exists, but the "
            "closed cutover record it produces attests the outbox attempt ceiling, not a signer "
            "key target — no record type names the target key id and the attested publisher "
            "roles, and a callback observed under the new key id is not fleet attestation."
        ),
        unblocked_by=(
            "An HMAC-specific drained-cutover record naming the target key id, its secret "
            "digest, and the exact attested publisher roles — the same reserved future unit. "
            "Until then keep accepting BOTH keys and do not retire on observed traffic."
        ),
        refuse=_refuse_outbound_retirement,
        must_reject=(
            ("one callback observed under the new key id",
             {"kind": "observed_new_key_callback", "key_id": "new"}),
            ("the outbox-ceiling drain record",
             {"kind": "outbox_ceiling_drain_record", "setting": "KYC_OUTBOX_MAX_ATTEMPTS"}),
            ("a perfectly shaped signer record no shipped type can be",
             {"kind": "hmac_signer_cutover_record", "target_key_id": "new",
              "secret_digest": "0" * 64, "roles": ("outbox_worker", "dev_worker"),
              "attested_zero": True}),
        ),
    ),
)


# The published signature vector, bound as ONE record: change any part and the test that recomputes
# it through kyc_tool.security.sign_v2 fails. The body is stored as bytes so its length and hash
# are properties of the same object the document prints.
VECTOR_BODY = (
    b'{"event_type":"kyb.run_requested","occurred_at":"2026-08-04T12:00:00Z",'
    b'"actor":{"type":"user","id":"acct-42"},'
    b'"payload":{"company_legal_name":"ACME NETWORKS LTD"}}'
)

SIGNATURE_VECTOR = {
    "secret": "integration-test-secret-0123456789ab",
    "key_id": "techcraft-inbound-1",
    "direction": "platform->tool",
    "method": "POST",
    "path_qs": "/v1/cases/case-42/events",
    "timestamp": "1754400000",
    "slot": "evt-0001",
    "body": VECTOR_BODY,
    "body_bytes": 163,
    "body_sha256": "9beb5e66987b7818d9a0e24cd141d2f94048b1c8785e8d25b02a17303a5fe024",
    "canonical_lines": (
        "v2",
        "techcraft-inbound-1",
        "platform->tool",
        "POST",
        "/v1/cases/case-42/events",
        "1754400000",
        "evt-0001",
        "9beb5e66987b7818d9a0e24cd141d2f94048b1c8785e8d25b02a17303a5fe024",
    ),
    "signature": "0dc9ff66f1a9e096b0dcae311efe15799b88f9f7475f3999cee10aaebcbacdba",
}

@dataclass(frozen=True)
class EventRow:
    """One row of the accepted-events table.

    The printed payload cells are DERIVED from the structured field tuples (Wave 2 F5, the same
    move as `Transition`'s visible cells): the renderer used to compute `", ".join(...) or
    "(none)"` at the call site, which made the caller the author of a published cell. The join
    lives here now, so the projection of this row is fixed by the row alone.
    """

    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    note: str
    required_display: str = field(default="", init=False)
    optional_display: str = field(default="", init=False)

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = (
        "name", "required_display", "optional_display", "note")

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_display", ", ".join(self.required) or "(none)")
        object.__setattr__(self, "optional_display", ", ".join(self.optional) or "(none)")


EVENT_TABLE = (
    EventRow("kyb.run_requested", ("company_legal_name",),
             ("address", "contact", "jurisdiction", "platform_account_id", "registration_number",
              "website"),
             "First event for a case creates it; there is no registration call. Incomplete "
             "submissions ingest fine, and missing evidence simply never passes a check."),
    EventRow("email.verified", ("domain", "email", "verified_at"), (),
             "You verify the mailbox; we score it."),
    EventRow("org_id.submitted", ("org_handle", "rir"), (),
             "rir is one of arin, ripe, apnic, lacnic, afrinic."),
    EventRow("poc.submitted", ("poc_handle", "rir"), ("org_handle", "resource"),
             "Triggers our verification email to the RIR-listed address."),
    EventRow("poc.token_verified", ("token", "token_id", "verified_at"), (),
             "You host the verify page and echo the raw token from the email link. We store "
             "only its digest, and tokens expire after 72 hours."),
    EventRow("document.uploaded", ("doc_type", "object_ref"), (),
             "object_ref points at the object your side wrote under uploads/."),
    EventRow("website.review_completed", ("result", "reviewer_id", "task_id"), ("reason_codes",),
             "result is pass or fail. SENSITIVE EVENT: see the reviewer-actor rule; the task "
             "must be an open website task on the same case."),
    EventRow("reviewer.manual_approve", ("reviewer_id",), ("note",),
             "SENSITIVE EVENT: see the reviewer-actor rule. Handled inline: no run, no "
             "callback, and it returns 200 rather than 202."),
    EventRow("recalculate.requested", (), (),
             "Re-scores from stored evidence with no new fetches, and does not re-run the "
             "broker screen."),
)


# Replay tolerance, one source: the claim value and the ingest-headers row both derive from
# this constant, and the claim's authority verifier binds it to
# kyc_tool.security.MAX_HMAC_SKEW_SECONDS.
_SKEW_SECONDS = 300


@dataclass(frozen=True)
class HeaderSpec:
    """One required ingest header: its exact name, the value shape, and why it exists.

    These three cells were authored inline at the render call before Wave 2 F5, which left the
    Value and Why columns as caller prose no authority ever saw. They are registry rows now, so
    the table's complete matrix is derived from this claim and the header-name column stays
    bound to the request verifier by the claim's authority test.
    """

    name: str
    value_note: str
    why: str

    PUBLISHED_FIELDS: ClassVar[tuple[str, ...]] = ("name", "value_note", "why")


INGEST_HEADERS = (
    HeaderSpec(
        "Idempotency-Key",
        "unique per logical event",
        "a replay returns the stored response verbatim; the same key with a different payload "
        "is a 409, so retries are always safe",
    ),
    HeaderSpec(
        "X-KYC-Timestamp",
        "unix seconds",
        f"replay window is {_SKEW_SECONDS}s either side",
    ),
    HeaderSpec("X-KYC-Key-Id", "your inbound key id", "supports zero-downtime key rotation"),
    HeaderSpec(
        "X-KYC-Signature-V2",
        "hex HMAC-SHA256",
        "section 4; binds method, path, idempotency key, and body",
    ),
)

WIRE = Registry(
    name="wire",
    claims=(
        # ── ingestion ─────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.INGEST.PATH",
            value="POST /v1/cases/{case_id}/events",
            authority="kyc_tool.api.routes_events.post_event",
        ),
        Claim(
            id="WIRE.INGEST.HEADERS",
            value=INGEST_HEADERS,
            authority="kyc_tool.api.routes_events.post_event + kyc_tool.api.auth",
            table_headers=("Header", "Value", "Why"),
        ),
        Claim(
            id="WIRE.INGEST.STATUS",
            value=(
                (202, "accepted and queued — the normal result for a new event"),
                (200, "replay of a seen Idempotency-Key (stored response verbatim), or an inline "
                      "reviewer.manual_approve, which runs without a queued job"),
                (400, "Idempotency-Key header missing"),
                (401, "signature invalid"),
                (409, "same Idempotency-Key with a different payload, or a review-task state "
                      "conflict"),
                (422, "malformed envelope, malformed payload, or an invalid reviewer actor"),
                (404, "review task not found"),
            ),
            authority="kyc_tool.events.ingest.ingest_event",
            table_headers=("Code", "Meaning"),
        ),
        Claim(
            id="WIRE.INGEST.EXTRA_FIELDS",
            value={
                "envelope": "forbid",
                "payload": "allow",
                "prose": "Unknown fields at the TOP LEVEL of the envelope are rejected with 422. "
                         "Unknown fields inside payload (and inside actor) are preserved. This is "
                         "not a symmetric must-ignore contract.",
            },
            authority="kyc_tool.api.schemas.EventEnvelope.model_config / PAYLOAD_MODELS",
        ),
        Claim(
            id="WIRE.INGEST.ORDERING",
            value="Events for one case are serialized by the order we admit them under that "
                  "case's lock. Send same-case events one at a time and wait for acceptance if "
                  "order matters: two submitted concurrently are admitted in lock-acquisition "
                  "order, which need not match your send order.",
            authority="kyc_tool.events.ingest (Case FOR UPDATE admission)",
        ),
        Claim(
            id="WIRE.ACTOR.SENSITIVE",
            value={
                "events": ("website.review_completed", "reviewer.manual_approve"),
                "actor_type": "reviewer",
                "rule": "actor.type must equal 'reviewer', and actor.id must equal "
                        "payload.reviewer_id exactly — case-sensitive, after trimming surrounding "
                        "whitespace, with neither blank. Anything else is 422.",
                "trap": "The generic envelope example shows actor.type 'user'. Copying it for "
                        "either sensitive event fails.",
            },
            authority="kyc_tool.events.review_guard.reviewer_actor_reason",
        ),
        Claim(
            id="WIRE.EVENT.TABLE",
            value=EVENT_TABLE,
            authority="kyc_tool.api.schemas.EventType / PAYLOAD_MODELS required+optional fields",
            table_headers=("event_type", "Required payload", "Optional payload", "Notes"),
        ),
        # ── signing ───────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.SIGN.CANONICAL",
            value=("v2", "key_id", "direction", "method", "path?query", "timestamp", "slot",
                   "sha256(body) hex"),
            authority="kyc_tool.security.canonical_v2",
        ),
        Claim(
            id="WIRE.SIGN.DIRECTIONS",
            value={"platform_to_tool": "platform->tool", "tool_to_platform": "tool->platform"},
            authority="kyc_tool.security.DIRECTION_INBOUND / DIRECTION_OUTBOUND",
        ),
        Claim(
            id="WIRE.SIGN.SKEW_SECONDS",
            value=_SKEW_SECONDS,
            authority="kyc_tool.security.MAX_HMAC_SKEW_SECONDS",
        ),
        Claim(
            id="WIRE.SIGN.COMPANION",
            value="The runnable signer ships as a FILE alongside this document: "
                  "kyc-signer-example.py. Verify it with `shasum -a 256 kyc-signer-example.py` "
                  "before use. The code printed below is an illustration of that file — a PDF is "
                  "not a reliable clipboard for indentation-sensitive source, so copying it off "
                  "the page is not supported and we do not ask you to.",
            authority="docs.contracts.companion (digest recomputed over the shipped bytes)",
            exclusive_terms=("kyc-signer-example.py",),
        ),
        Claim(
            id="WIRE.SIGN.VECTOR",
            value=SIGNATURE_VECTOR,
            authority="kyc_tool.security.sign_v2 via docs.contracts.signing_example.sign",
        ),
        Claim(
            id="WIRE.SIGN.V1_SUNSET",
            value="Both v1 sunset dates are set with you at cutover and are unset in our config "
                  "today. The inbound date additionally cannot take effect until an observation "
                  "window records zero v1 traffic, so a date alone never cuts off a live sender. "
                  "Ship v2 from day one and none of this applies.",
            authority=".env.example (both sunset vars unset) + docs/DEPLOYMENT.md §2",
        ),
        Claim(
            id="WIRE.SIGN.ROTATION",
            # DERIVED from the closed step records (gate finding 13): free prose cannot add an
            # alternate retirement path, because the verifier requires value == rotation_lines().
            value=rotation_lines(),
            authority="kyc_tool.config.hmac_extra_key_violations + kyc_tool.api.auth._inbound_secret "
                      "+ kyc_tool.outbox.publisher (one outbound signer) + "
                      "WIRE.SIGN.ROTATION_RETIREMENT (both retirement steps gated BLOCKED)",
        ),
        Claim(
            id="WIRE.SIGN.ROTATION_RETIREMENT",
            value=ROTATION_RETIREMENT_GATES,
            authority=".agents/ROADMAP.md §C (PR 5c, reserved unbuilt) + executable absence "
                      "anchors: kyc_tool.api.hmac_witness closed function inventory, hmac table "
                      "closed column inventory, kyc_tool.ops.cutover closed record inventory, "
                      "SIGNED_FLEET_RECEIPT_SCHEMA",
            state=ClaimState.BLOCKED,
            table_headers=("Direction", "Blocked step", "Why it cannot be exercised today",
                           "What unblocks it"),
        ),
        # ── callbacks ─────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.CALLBACK.PATH",
            value="POST <your-base>/kyc/decision",
            authority="kyc_tool.outbox.publisher (callback URL + '/kyc/decision')",
        ),
        Claim(
            id="WIRE.CALLBACK.FIELDS",
            value=("case_id", "run_id", "event_id", "decision", "score", "gates",
                   "buy_enablement", "checks", "decided_at"),
            authority="kyc_tool.api.schemas.DecisionCallback (required fields)",
        ),
        Claim(
            id="WIRE.CALLBACK.OPTIONAL_FIELDS",
            value=("event_sequence", "enforcement_held"),
            authority="kyc_tool.api.schemas.DecisionCallback (optional fields)",
        ),
        Claim(
            id="WIRE.CALLBACK.GATES",
            value=("score_met", "legal_proof", "control_proof", "broker_ok", "no_hard_conflict"),
            authority="kyc_tool.api.schemas.GatesBody",
        ),
        Claim(
            id="WIRE.CALLBACK.DECISIONS",
            value=("approve", "approve_buy_locked", "manual_review_insufficient", "reject"),
            authority="kyc_tool.api.schemas.DecisionCallback.decision literal",
        ),
        Claim(
            id="WIRE.CALLBACK.DELIVERY",
            value=(
                "Each automated decision enqueues one callback row in our transactional outbox.",
                "An eligible row is delivered AT LEAST ONCE until you return 2xx or it "
                "dead-letters, so duplicates are expected; dedupe on (case_id, run_id).",
                "SUPPRESSED means zero sends: a row we can locally prove obsolete is never put on "
                "the wire at all.",
                "DEAD-LETTERED does NOT mean zero sends. A dead row exhausted its attempts "
                "without us ever witnessing a 2xx, and it may have made up to eight HTTP "
                "attempts. If one of those committed on your side and the response was lost, you "
                "have applied a decision we recorded as undelivered. Before we requeue a dead "
                "row, check your accepted ledger for that (case_id, run_id).",
                "Manual approvals by your reviewers send nothing at all.",
                "Do not assume a one-to-one match between decisions we make and callbacks you "
                "receive.",
            ),
            authority="AUDIT_FINDINGS.md A6 + kyc_tool.outbox.publisher module docstring",
        ),
        Claim(
            id="WIRE.CALLBACK.VALIDATION",
            value=RECEIVER_VALIDATION_RULES,
            authority="docs/contracts/receiver_reference.py _validate/validate_state — every "
                      "rule's specimen is executed by the authority verifier and must refuse",
            table_headers=("Invalid input", "Disposition"),
        ),
        Claim(
            id="WIRE.CALLBACK.RECEIVER_TXN",
            value=(
                "Verify the signature.",
                "Validate the callback and your ledger state (the validation table above); "
                "HOLD anything invalid.",
                "In ONE transaction: dedupe on (case_id, run_id); decide whether this VALID "
                "callback becomes EFFECTIVE using the algorithm below; record it in your "
                "accepted ledger either way.",
                "COMMIT.",
                "Only then return 2xx.",
                "If the commit fails or its outcome is uncertain, return non-2xx or drop the "
                "connection so we retry.",
            ),
            authority="the accepted receiver design (activation design doc) + publisher "
                      "terminalizes on 2xx",
            exclusive_terms=("return 2xx", "COMMIT"),
        ),
        Claim(
            id="WIRE.CALLBACK.EFFECTIVENESS",
            value=RECEIVER_TRANSITIONS,
            authority="AUDIT_FINDINGS.md A6 residual reverts + the accepted receiver design + "
                      "docs/contracts/receiver_reference.py (executable, scenario-tested) + the "
                      "invariant oracle in the authority test (every row and the executed "
                      "decision held against it)",
            table_headers=("Phase", "When this row applies", "Record", "Effective?", "Why"),
        ),
        Claim(
            id="WIRE.CALLBACK.LEGEND",
            value=tuple(sorted(predicates.legend().items())),
            authority="docs.contracts.predicates FACET_LEGEND/RELEASE_LEGEND, cross-bound: each "
                      "meaning must carry its own token's distinguishing term and never its "
                      "paired sibling's",
            table_headers=("Token", "Meaning"),
        ),
        Claim(
            id="WIRE.CALLBACK.RELEASE",
            value=RELEASE_TRANSITIONS,
            authority="the accepted activation design's manual-release machine (states, "
                      "completion CAS, expiry, cancellation) + docs/contracts/"
                      "receiver_reference.py (executable, scenario-tested) + the release oracle "
                      "in the authority test; partition proven over all 72 states",
            state=ClaimState.PENDING,
            table_headers=("When this row applies", "Record", "Effective?", "Why"),
        ),
        Claim(
            id="WIRE.CALLBACK.RETRY",
            value={
                "attempts": 8,
                "base_seconds": 10,
                "delays": (10, 20, 40, 80, 160, 320, 640),
                "jitter": False,
                "backoff_total_seconds": 1270,
                # backoff plus every attempt's 40s hard wall, rounded UP: an operator waiting on a
                # dead-letter should never be told a shorter window than the real worst case.
                "worst_case_seconds": 1590,
                "backoff_total_minutes": 22,
                "worst_case_minutes": 27,
            },
            authority="kyc_tool.queue.backoff.saturating_backoff_seconds + Settings "
                      "outbox_backoff_base_seconds / outbox_max_attempts",
        ),
        Claim(
            id="WIRE.CALLBACK.WAIT_BOUND",
            value="Respond within 10 seconds. Each attempt carries a 40-second hard wall (4 x the "
                  "10s HTTP phase timeout). Past it we STOP WAITING, detach the attempt, and "
                  "account a retryable failure. We cannot retract a request already in flight, so "
                  "a detached attempt may still reach you and still take effect. Dedupe is "
                  "mandatory, not advisory.",
            authority="kyc_tool.outbox.publisher (detached attempt accounting; no cancellation)",
        ),
        Claim(
            id="WIRE.CALLBACK.COMPLETION",
            value="For an automated decision that produces an eligible callback, that callback is "
                  "the completion signal and nothing needs polling. It is not a universal "
                  "completion signal: manual approvals produce no callback at all, a suppressed "
                  "row is never sent, and a dead-lettered row was sent without a witnessed "
                  "success rather than not sent.",
            authority="AUDIT_FINDINGS.md A6 + kyc_tool.events.ingest (manual approve is inline)",
        ),
        # ── ordering ──────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.ORDERING.NO_DECIDED_AT",
            value="decided_at is a display timestamp and is not an ordering key. The wire value "
                  "is stamped from the deciding worker's wall clock, and different workers decide "
                  "for one case, so ordinary clock skew between hosts can give the older decision "
                  "the later timestamp. The database column of that name is worse: it holds "
                  "transaction-start time, which inverts against the lock-serialized commit "
                  "order. Our own migration tooling is barred from falling back to it.",
            authority="AUDIT_FINDINGS.md D-7bcore + docs/RUNBOOK.md step 0.5 + "
                      "kyc_tool.db.tables.Case.latest_decision_row_id",
            exclusive_terms=("ordering key", "decided_at is"),
        ),
        Claim(
            id="WIRE.ORDERING.INTERIM",
            value="Until ordered delivery is activated: dedupe exact repeats on (case_id, "
                  "run_id), and resolve same-case conflicts either through an ordering authority "
                  "you own or by holding them for review. A reviewer's manual approval sends you "
                  "nothing and an automatic callback queued before it can arrive after it, so "
                  "treat a manual approval as authoritative over any automatic decision that "
                  "arrives later for that case.",
            authority="AUDIT_FINDINGS.md A6 residual reverts",
        ),
        Claim(
            id="WIRE.ORDERING.SEQUENCE_DOMAINS",
            value=(
                "event_sequence is INGEST PROVENANCE: the per-case ordinal of the event that "
                "triggered a run. It is on the wire today. It is NEVER an ordering authority for "
                "decisions, and you must not sort or dedupe decisions by it.",
                "decision_sequence is the CALLBACK-ORDER AUTHORITY. It is allocated per case when "
                "the decision is made, and it goes on the wire with the activation unit (024). "
                "The post-024 high-water mark in section 3 is a high-water mark over "
                "decision_sequence and nothing else.",
                "They differ whenever work completes out of admission order, which is normal: an "
                "event admitted first can decide second. Order them by event_sequence and the "
                "later decision loses to the earlier one.",
            ),
            authority=".agents/ROADMAP.md D1 (PR 2 event_sequence / PR 7b decision_sequence) + "
                      "the activation spec wire-emission section",
            exclusive_terms=("INGEST PROVENANCE", "CALLBACK-ORDER AUTHORITY"),
        ),
        Claim(
            id="WIRE.ORDERING.PENDING_INPUTS",
            value=PENDING_024_INPUTS,
            table_headers=("#", "Owner", "What we need to know", "Answer shape",
                           "Blocked deliverable — answer alone does not unblock"),
            authority=".agents/superpowers/specs/2026-07-22-pr7b-activation-platform-ordering-"
                      "design.md open blockers O1-O4 (parsed live by the authority test) + "
                      "docs.contracts.wire.resolution_problems (refuses every artifact until the "
                      "answer-artifact schema ships; .agents/ROADMAP.md §C, PR 7b-inputs)",
            state=ClaimState.PENDING,
        ),
        Claim(
            id="WIRE.ORDERING.BOOTSTRAP_024",
            value="Ordered delivery needs a signed bootstrap of per-case high-water marks from "
                  "your accepted-run ledger, because your ledger is the authority on what you "
                  "actually applied. The design is accepted but NOT BUILT, and its schema is not "
                  "final. Do not implement against a draft. What we already know it must carry: "
                  "the complete universe of durable callback rows rather than a per-case latest; "
                  "your accepted-run ledger kept separate from the currently effective source "
                  "per case (an automatic callback versus a manual approval); a floor for "
                  "manual-current cases; exact two-sided coverage so both sides can prove "
                  "convergence; request and response digests; and a freshly signed response "
                  "envelope. We will publish the schema when the activation unit is built.",
            authority=".agents/ROADMAP.md PR 7b-activation (migration 024, pending)",
            state=ClaimState.PENDING,
        ),
        Claim(
            id="WIRE.ORDERING.INTEGRITY_MISMATCH",
            value="integrity_mismatch is a POST-ACTIVATION terminal state. No code path emits it "
                  "today; it arrives with the activation unit.",
            authority=".agents/ROADMAP.md PR 7b-activation",
            state=ClaimState.PENDING,
        ),
        # ── retention ─────────────────────────────────────────────────────────────────────────
        Claim(
            id="WIRE.RETENTION.BY_KIND",
            value=(
                ("decision callbacks",
                 "Bodies are redacted in place after the retention window; the row and its "
                 "ordering identity remain."),
                ("POC verification emails",
                 "The body is redacted as soon as the row reaches delivered or dead, not at the "
                 "retention window; delivered rows are DELETED later. A dead POC email cannot be "
                 "requeued because its token is already gone, so recovery is a fresh "
                 "poc.submitted."),
            ),
            authority="kyc_tool.workers.retention + kyc_tool.outbox.publisher terminal writes",
            table_headers=("Kind", "What happens"),
        ),
        Claim(
            id="WIRE.RETENTION.WINDOW_DAYS",
            value=2555,
            authority="kyc_tool.config.Settings.retention_days default",
        ),
        # ── published statements: each sentence SELECTED by an executed fact ─────────────
        #
        # These were `Claim.note` — prose beside a claim that no verifier read (Wave-2 audit
        # finding 1). Each is its own claim now, valued by a `Statement`: the published text
        # is `alternatives[answer]`, and the claim's verifier EXECUTES the fact and refuses a
        # declared answer the running system contradicts.
        Claim(
            id="WIRE.CALLBACK.ACK_CONSEQUENCE",
            value=statement("ack_before_commit", "unrecoverable", {
                "unrecoverable":
                    "A 2xx returned before your commit is unrecoverable: we mark the row delivered and "
                    "at-least-once cannot help you. This is the single most important requirement on "
                    "your side.",
                "recovered":
                    "A 2xx returned before your commit is safe: we retry the row until your side "
                    "confirms it, so a lost commit costs nothing.",
            }),
            authority="kyc_tool.outbox.publisher (a 2xx terminalizes the row: delivered, no further attempt)",
        ),
        Claim(
            id="WIRE.SIGN.DIRECTION_FORM",
            value=statement("direction_token_form", "literal_only", {
                "literal_only": "Literal tokens. Prose like 'inbound' will not verify.",
                "prose_ok": "Either spelling works: prose also verifies alongside the literal token.",
            }),
            authority="kyc_tool.security.verify_v2 executed against a prose direction",
        ),
        Claim(
            id="WIRE.SIGN.COMPANION_PROOF",
            value=statement("companion_is_executed", "executed", {
                "executed":
                    "Our test suite executes the file's exact bytes against the published vector and "
                    "asserts they reproduce the published signature, so a digest mismatch means the file "
                    "changed in transit rather than the algorithm changing.",
                "unread":
                    "The shipped file is not executed by our suite, so a digest mismatch tells you only "
                    "that the bytes differ.",
            }),
            authority="docs.contracts.signing_example executed against WIRE.SIGN.VECTOR",
        ),
        Claim(
            id="WIRE.CALLBACK.OPTIONAL_FIELD_RULE",
            value=statement("optional_field_handling", "tolerate_and_preserve", {
                "tolerate_and_preserve":
                    "Tolerate and preserve both. enforcement_held carries the computed decision while "
                    "the enforcement hold is active; the outer decision stays authoritative.",
                "may_drop":
                    "You may drop either field: nothing downstream depends on them being kept.",
            }),
            authority="kyc_tool.api.schemas.DecisionCallback optional fields + enforcement_held semantics",
        ),
        Claim(
            id="WIRE.CALLBACK.VALIDATION_ORDER",
            value=statement("validation_order", "validate_first", {
                "validate_first":
                    "VALIDATE BEFORE YOU CLASSIFY: after the signature verifies, check the callback's "
                    "shape and your own ledger's integrity BEFORE consulting the replay history or "
                    "either table. Each row below is an input that must be HELD, not recorded or "
                    "acknowledged as processed — a malformed callback acknowledged with 2xx is "
                    "unrecoverable under at-least-once delivery.",
                "classify_first":
                    "You may classify first and validate afterwards: consult the tables, then check the "
                    "callback's shape and your ledger's integrity.",
            }),
            authority="docs.contracts.receiver_reference (_validate runs before classification)",
        ),
        Claim(
            id="WIRE.CALLBACK.ACK_VS_APPLY",
            value=statement("ack_vs_apply", "different_decisions", {
                "different_decisions":
                    "ACKNOWLEDGING a callback and APPLYING it are different decisions: acknowledge and "
                    "record every VALID callback (validation table above; invalid input is HELD, never "
                    "recorded), then consult this table for whether it takes effect. "
                    "Conditions are written in a fixed grammar over the legend's tokens — each clause "
                    "names a facet, and every clause must hold. Within a phase the rows PARTITION the "
                    "receiver's state space — every state matches exactly one row, checked by "
                    "enumeration — so there is no 'otherwise' branch to fall through to and no state "
                    "with two answers. Automatic authority over a manual-current case returns ONLY "
                    "through the authenticated platform-owned release protocol, and while a release is "
                    "OPEN the case is in manual_release_pending and the release table below governs "
                    "instead.",
                "same_decision":
                    "Acknowledging a callback and applying it are the same: a callback you record is a "
                    "callback that has taken effect.",
            }),
            authority="docs.contracts.receiver_reference + the phase partition enumeration",
        ),
        Claim(
            id="WIRE.CALLBACK.LEGEND_CLOSURE",
            value=statement("legend_closure", "closed", {
                "closed":
                    "Every condition in the two tables is built from exactly these tokens; a token means "
                    "this and nothing else.",
                "open":
                    "The conditions read as ordinary English, and other terms may appear beside these.",
            }),
            authority="docs.contracts.predicates.legend() covering every published condition token",
        ),
        Claim(
            id="WIRE.CALLBACK.RELEASE_STATE",
            value=statement("release_protocol_state", "post_024_only", {
                # the replay rule is itself a typed record (RELEASE_REPLAY_RULE), so this
                # alternative composes two derivations rather than restating either
                "post_024_only":
                    "POST-024 ONLY: the release protocol arrives with the activation unit, so "
                    "this table is the accepted design, not a wire you can exercise today. "
                    "While a release is pending, MANUAL REMAINS EFFECTIVE. Exactly one row "
                    "completes the release; a new manual approval cancels the pending release "
                    "outright; expiry is driven by the platform's stored deadline, never by "
                    "waiting for traffic. " + RELEASE_REPLAY_RULE.text,
                "live":
                    "The release protocol is live today, so this table describes a wire you can exercise "
                    "now.",
            }),
            authority=".agents/ROADMAP.md PR 7b-activation (024 pending; no code path emits a release state)",
        ),
        Claim(
            id="WIRE.ORDERING.ORDINAL_AUTHORITY",
            value=statement("ordering_ordinal", "only_one_orders", {
                "only_one_orders":
                    "Two ordinals for one case, and only one of them orders decisions. Getting this "
                    "backwards is silent: both are monotonic per case, so a receiver built on the wrong "
                    "one looks correct until two runs overlap.",
                "both_order":
                    "Two ordinals for one case, and either ordinal orders decisions equally well.",
            }),
            authority="kyc_tool.api.schemas.DecisionCallback + the D1 sequence domains "
                      "(decision_sequence orders decisions, event_sequence does not)",
        ),
        Claim(
            id="WIRE.ORDERING.OBLIGATION_STATE",
            value=statement("obligation_resolution", "unresolvable", {
                "unresolvable":
                    "These are the decisions 024 cannot be built without, and every one of them is the "
                    "platform's to make. Answering the three questions in section 1.1 alone leaves the "
                    "unit blocked. And no reply can RESOLVE an obligation yet: resolution requires a "
                    "versioned, approved and signed answer artifact, and that schema and its verifying "
                    "authority are a future unit of ours that has not shipped — so O1-O4 remain PENDING "
                    "however complete an emailed answer looks. What we can do with your answers today is "
                    "screen their content and agree them in principle.",
                "resolvable":
                    "A complete emailed answer clears the obligation it addresses, so O1-O4 close as "
                    "your replies arrive.",
            }),
            authority="docs.contracts.wire.resolution_problems (refuses every artifact "
                      "until the answer-artifact schema ships)",
        ),
    ),
)
