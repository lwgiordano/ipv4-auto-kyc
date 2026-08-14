"""A closed predicate algebra for the receiver transition table.

Re-audit `4cb2cb7` F10. The published rows carried their conditions as PROSE, and the reference
evaluator carried the same conditions as hardcoded `if` branches. Nothing compared the two: Codex
widened the post-024 manual row to "MANUAL or AUTOMATIC" — an overlap that makes two rows claim the
same state — and every receiver and authority test stayed green, because "totality" was checked as
four rows plus token presence, and the evaluator never read the rows it was supposedly
implementing.

So conditions become DATA. The receiver's decision inputs form a finite space — three facets, each
with a closed domain — and a row's predicate is a subset of that space, written as one allowed set
per facet. That representation buys the property prose can never have: overlap, shadowing, and
coverage are all COMPUTABLE, exactly, by enumeration. Each phase's rows must partition the space —
every reachable state matches exactly one row — so an overlapping predicate, an uncovered state, a
deleted row, and a redundant fifth row are all the same failure: some state no longer has exactly
one answer.

The published condition text is DERIVED from the predicate (each facet value carries its phrase),
and the evaluator executes the same predicate objects. One authority, three consumers: the page,
the enumeration, and the reference implementation.
"""

from dataclasses import dataclass

# ── the facets, each a closed domain ──────────────────────────────────────────────────────────
# duplicate: has this (case_id, run_id) been accepted before?
DUP = "duplicate"
FRESH = "fresh"

# source: what kind of decision is currently effective for the case?
SRC_NONE = "none"
SRC_MANUAL = "manual"
SRC_AUTOMATIC = "automatic"

# sequence: how does the callback's decision_sequence relate to the case's high-water mark?
# (Interim rows allow the whole domain — the facet exists there but cannot influence the answer.)
SEQ_ABSENT = "absent"
SEQ_NOT_ABOVE = "not_above"
SEQ_ABOVE = "above"

DUPLICATE_DOMAIN = frozenset({DUP, FRESH})
SOURCE_DOMAIN = frozenset({SRC_NONE, SRC_MANUAL, SRC_AUTOMATIC})
SEQUENCE_DOMAIN = frozenset({SEQ_ABSENT, SEQ_NOT_ABOVE, SEQ_ABOVE})

ANY_DUPLICATE = DUPLICATE_DOMAIN
ANY_SOURCE = SOURCE_DOMAIN
ANY_SEQUENCE = SEQUENCE_DOMAIN

# ── the phrase each facet value contributes to the derived condition ──────────────────────────
# These are the words TechCraft reads, so they are written for the page, not for the enum.
_PHRASES = {
    DUP: "the (case_id, run_id) is already in your accepted ledger",
    FRESH: "the (case_id, run_id) is new to your ledger",
    SRC_NONE: "the case has no currently effective decision",
    SRC_MANUAL: "the currently effective source is a MANUAL approval",
    SRC_AUTOMATIC: "the currently effective source is AUTOMATIC",
    SEQ_ABSENT: "the callback carries no decision_sequence",
    SEQ_NOT_ABOVE: "its decision_sequence is at or below your recorded high-water mark for the "
                   "case",
    SEQ_ABOVE: "its decision_sequence exceeds the high-water mark",
}

# Multi-value phrasing joins in a FIXED order so the derived text is deterministic.
_FACET_ORDER = {
    "duplicate": (DUP, FRESH),
    "source": (SRC_NONE, SRC_MANUAL, SRC_AUTOMATIC),
    "sequence": (SEQ_ABSENT, SEQ_NOT_ABOVE, SEQ_ABOVE),
}


@dataclass(frozen=True)
class When:
    """One row's predicate: the set of states it claims, one allowed set per facet.

    Semantics are cartesian: the row matches a state exactly when every facet's observed value is
    in that facet's allowed set. `frozenset` per facet keeps the denotation a plain subset of the
    finite space, which is what makes partitioning checkable by enumeration rather than argued in
    prose.
    """

    duplicate: frozenset
    source: frozenset
    sequence: frozenset

    def __post_init__(self) -> None:
        for name, allowed, domain in (
            ("duplicate", self.duplicate, DUPLICATE_DOMAIN),
            ("source", self.source, SOURCE_DOMAIN),
            ("sequence", self.sequence, SEQUENCE_DOMAIN),
        ):
            if not allowed:
                raise ValueError(f"{name}: an empty allowed set is a row that matches nothing")
            unknown = set(allowed) - domain
            if unknown:
                raise ValueError(f"{name}: {sorted(unknown)} not in the closed domain")

    def matches(self, observed: dict) -> bool:
        return (
            observed["duplicate"] in self.duplicate
            and observed["source"] in self.source
            and observed["sequence"] in self.sequence
        )

    def condition(self) -> str:
        """The published condition, derived. A facet that allows its whole domain says nothing;
        a facet allowing several values joins them with 'or'."""
        parts = []
        for name, allowed, domain in (
            ("duplicate", self.duplicate, DUPLICATE_DOMAIN),
            ("source", self.source, SOURCE_DOMAIN),
            ("sequence", self.sequence, SEQUENCE_DOMAIN),
        ):
            if allowed == domain:
                continue
            ordered = [value for value in _FACET_ORDER[name] if value in allowed]
            parts.append(", or ".join(_PHRASES[value] for value in ordered))
        if not parts:
            raise ValueError(
                "a predicate over the full space derives the condition 'always' — that is the "
                "'otherwise apply' defect wearing a new syntax, and no published row may say it"
            )
        return ", AND ".join(parts)


def state_space():
    """Every observable receiver state, enumerated. 2 x 3 x 3 = 18 per phase."""
    for duplicate in sorted(DUPLICATE_DOMAIN):
        for source in sorted(SOURCE_DOMAIN):
            for sequence in sorted(SEQUENCE_DOMAIN):
                yield {"duplicate": duplicate, "source": source, "sequence": sequence}


def partition_problems(rows) -> list[str]:
    """Every state must match EXACTLY ONE row. Zero is an uncovered state — the receiver has no
    answer. Two is an overlap or a shadowed row — the receiver has competing answers, and which
    one wins becomes an implementation accident, which is the F10 defect itself. The list is a
    report rather than an assert so RED tests can inspect what a mutation breaks."""
    problems = []
    for observed in state_space():
        matched = [index for index, row in enumerate(rows) if row.when.matches(observed)]
        if len(matched) != 1:
            problems.append(
                f"state {observed} matches row(s) {matched or 'NONE'} — "
                f"{'uncovered' if not matched else 'competing answers'}"
            )
    return problems
