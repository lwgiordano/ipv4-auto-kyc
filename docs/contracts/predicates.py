"""A closed predicate algebra for the receiver transition table.

Re-audit `4cb2cb7` F10. The published rows carried their conditions as PROSE, and the reference
evaluator carried the same conditions as hardcoded `if` branches. Nothing compared the two: Codex
widened the post-025 manual row to "MANUAL or AUTOMATIC" — an overlap that makes two rows claim the
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

The published condition text is DERIVED from the predicate in a canonical grouped grammar that
PARSES BACK to the predicate, and the evaluator executes the same predicate objects. One
authority, three consumers: the page, the enumeration, and the reference implementation.
"""

import re
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

# ── the token legend: what each facet value MEANS, published once, beside the tables ──────────
#
# Gate audit `6c4f54a..91fbde3` finding 3. Conditions used to render as sentences assembled from
# per-value phrases; swapping the manual and automatic phrases derived conditions normally and
# told the reader the opposite predicate, and the flat "a, or b, AND c" text read as a
# disjunction of conjunctions. So the condition text now uses the TOKENS themselves in a
# canonical grouped grammar (parseable back to the predicate — see `parse_condition`), and this
# legend defines each token exactly once. The legend's verifier cross-binds each meaning to its
# own token and excludes sibling tokens' terms, so the swap is a failing mutation rather than a
# quiet reversal.

@dataclass(frozen=True)
class TokenSemantics:
    """One legend token's TYPED meaning: its facet, its executable checker over concrete
    (LedgerState, Callback, now) inputs, and the meaning sentence the legend derives (re-audit
    `1826661..b5c7a83` finding 8 — the legend was free prose held by keyword checks, so a
    paraphrase keeping the expected words could reverse the semantics). The checker is verified
    against the machines themselves by enumeration (`receiver_reference.token_semantics_problems`),
    the legend claim must EQUAL the derivation, and the derived meanings carry a reviewed digest
    pin in the authority test — so a reworded meaning is a re-pin, the act of review."""

    facet: str
    meaning: str
    check: object  # Callable[(state, callback, now)] -> bool, duck-typed to avoid an import cycle


TOKEN_SEMANTICS = {
    DUP: TokenSemantics(
        "duplicate", "the callback's (case_id, run_id) is already in your accepted ledger",
        lambda s, c, n: c.run_id in s.seen_run_ids),
    FRESH: TokenSemantics(
        "duplicate", "the callback's (case_id, run_id) is new to your ledger",
        lambda s, c, n: c.run_id not in s.seen_run_ids),
    SRC_NONE: TokenSemantics(
        "source", "no decision is currently in force for the case",
        lambda s, c, n: s.current_source is None),
    SRC_MANUAL: TokenSemantics(
        "source", "the decision currently in force is a manual approval",
        lambda s, c, n: s.current_source == "manual"),
    SRC_AUTOMATIC: TokenSemantics(
        "source", "the decision currently in force is an automatic one",
        lambda s, c, n: s.current_source == "automatic"),
    SEQ_ABSENT: TokenSemantics(
        "sequence", "the callback carries no decision_sequence",
        lambda s, c, n: c.decision_sequence is None),
    SEQ_NOT_ABOVE: TokenSemantics(
        "sequence", "the callback's decision_sequence is at or below your recorded high-water "
                    "mark for the case",
        lambda s, c, n: (c.decision_sequence is not None and s.high_water is not None
                         and c.decision_sequence <= s.high_water)),
    SEQ_ABOVE: TokenSemantics(
        "sequence", "the callback's decision_sequence is above the high-water mark: no mark "
                    "exists yet (the case's first sequenced callback) OR the sequence exceeds "
                    "the recorded mark",
        lambda s, c, n: (c.decision_sequence is not None
                         and (s.high_water is None or c.decision_sequence > s.high_water))),
}


# Multi-value rendering joins in a FIXED order so the derived text is deterministic.
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
        """The published condition, derived, in the CANONICAL GROUPED GRAMMAR (gate finding 3):
        `facet = value` for a singleton, `facet in {v1, v2}` for several — one braced group per
        facet, joined with AND, so no precedence is left for a reader to guess. A facet allowing
        its whole domain says nothing; a predicate over the full space is refused. The text
        round-trips: `parse_condition(when.condition()) == when`, and the verifier holds every
        published row to that."""
        parts = []
        for name, allowed, domain in (
            ("duplicate", self.duplicate, DUPLICATE_DOMAIN),
            ("source", self.source, SOURCE_DOMAIN),
            ("sequence", self.sequence, SEQUENCE_DOMAIN),
        ):
            if allowed == domain:
                continue
            ordered = [value for value in _FACET_ORDER[name] if value in allowed]
            if len(ordered) == 1:
                parts.append(f"{name} = {ordered[0]}")
            else:
                parts.append(f"{name} in {{{', '.join(ordered)}}}")
        if not parts:
            raise ValueError(
                "a predicate over the full space derives the condition 'always' — that is the "
                "'otherwise apply' defect wearing a new syntax, and no published row may say it"
            )
        return " AND ".join(parts)


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


_DOMAINS = {
    "duplicate": DUPLICATE_DOMAIN,
    "source": SOURCE_DOMAIN,
    "sequence": SEQUENCE_DOMAIN,
}

_SINGLE = re.compile(r"^(\w+) = (\w+)$")
_GROUP = re.compile(r"^(\w+) in \{(\w+(?:, \w+)*)\}$")


def parse_condition(text: str) -> When:
    """The RENDERED condition, parsed back to a predicate — the independent direction the grammar
    exists for (gate finding 3). An omitted facet means its whole domain; a facet named twice, an
    unknown facet or value, or any clause outside the grammar refuses. The verifier asserts
    `parse_condition(row.condition) == row.when` for every published row, so text and predicate
    cannot say different things."""
    allowed: dict[str, frozenset] = {}
    for clause in text.split(" AND "):
        single = _SINGLE.match(clause)
        group = _GROUP.match(clause)
        if single:
            name, values = single.group(1), [single.group(2)]
        elif group:
            name, values = group.group(1), group.group(2).split(", ")
        else:
            raise ValueError(f"clause {clause!r} is outside the condition grammar")
        if name not in _DOMAINS:
            raise ValueError(f"unknown facet {name!r}")
        if name in allowed:
            raise ValueError(f"facet {name!r} appears twice")
        unknown = set(values) - _DOMAINS[name]
        if unknown:
            raise ValueError(f"{name}: unknown value(s) {sorted(unknown)}")
        allowed[name] = frozenset(values)
    return When(
        duplicate=allowed.get("duplicate", DUPLICATE_DOMAIN),
        source=allowed.get("source", SOURCE_DOMAIN),
        sequence=allowed.get("sequence", SEQUENCE_DOMAIN),
    )


# ── the manual-release machine's own closed facets ────────────────────────────────────────────
#
# Gate audit `6c4f54a..91fbde3` finding 1. The base machine above is total over the sources
# {none, manual, automatic}; the accepted activation design has a third effective-source state,
# `manual_release_pending`, in which MANUAL REMAINS EFFECTIVE while a release is open. Rather
# than force those rules into the base facets (whose cartesian product would then carry
# unreachable release states for every non-pending source), the pending state gets its own
# machine: five closed facets, a partition proven by enumeration over all 72 states, and exactly
# one row that completes the release — the one that re-passes EVERY check the accepted design's
# completion CAS re-asserts.

# binding: how the callback's release fields relate to the case's pending release.
BIND_NONE = "ordinary"  # every release field NULL: an ordinary decision callback
BIND_MATCH = "bound"  # all release fields present and naming the pending release
BIND_MISMATCH = "foreign"  # all present, but naming some other release

# manual_event: does the case's CURRENT manual event still equal the one the release was
# opened against? (The rev-4 race: re-approval after the release opened.)
EVENT_UNCHANGED = "unchanged"
EVENT_CHANGED = "changed"

# deadline: where database time stands relative to the release's stored deadline.
DEADLINE_LIVE = "live"
DEADLINE_EXPIRED = "expired"

BINDING_DOMAIN = frozenset({BIND_NONE, BIND_MATCH, BIND_MISMATCH})
EVENT_DOMAIN = frozenset({EVENT_UNCHANGED, EVENT_CHANGED})
DEADLINE_DOMAIN = frozenset({DEADLINE_LIVE, DEADLINE_EXPIRED})

ANY_BINDING = BINDING_DOMAIN
ANY_EVENT = EVENT_DOMAIN
ANY_DEADLINE = DEADLINE_DOMAIN

RELEASE_TOKEN_SEMANTICS = {
    BIND_NONE: TokenSemantics(
        "binding", "the callback carries no release fields at all (an ordinary decision "
                   "callback)",
        lambda s, c, n: c.release_id is None),
    BIND_MATCH: TokenSemantics(
        "binding", "the callback carries every release field, naming the case's pending release",
        lambda s, c, n: (c.release_id is not None
                         and c.release_id == s.release.release_id
                         and c.manual_event_id == s.release.requested_manual_event_id)),
    BIND_MISMATCH: TokenSemantics(
        "binding", "the callback carries every release field, but names a different release",
        lambda s, c, n: (c.release_id is not None
                         and not (c.release_id == s.release.release_id
                                  and c.manual_event_id
                                  == s.release.requested_manual_event_id))),
    EVENT_UNCHANGED: TokenSemantics(
        "manual_event", "the case's current manual event is still the one the release was "
                        "opened against",
        lambda s, c, n: s.current_manual_event_id == s.release.requested_manual_event_id),
    EVENT_CHANGED: TokenSemantics(
        "manual_event", "the case has been re-approved: its current manual event is no longer "
                        "the one the release was opened against",
        lambda s, c, n: s.current_manual_event_id != s.release.requested_manual_event_id),
    DEADLINE_LIVE: TokenSemantics(
        "deadline", "database time is still before the release's stored deadline",
        lambda s, c, n: n < s.release.deadline),
    DEADLINE_EXPIRED: TokenSemantics(
        "deadline", "database time has reached the release's stored deadline",
        lambda s, c, n: n >= s.release.deadline),
}
TOKEN_SEMANTICS.update(RELEASE_TOKEN_SEMANTICS)


def legend() -> dict:
    """The published token legend, DERIVED from the typed semantics — the only way a meaning
    comes to exist."""
    return {token: semantics.meaning for token, semantics in TOKEN_SEMANTICS.items()}


# Derived views kept under the established names; consumers and the claim build from these.
FACET_LEGEND = {t: s_.meaning for t, s_ in TOKEN_SEMANTICS.items()
                if t not in RELEASE_TOKEN_SEMANTICS}
RELEASE_LEGEND = {t: s_.meaning for t, s_ in RELEASE_TOKEN_SEMANTICS.items()}

_RELEASE_DOMAINS = {
    "duplicate": DUPLICATE_DOMAIN,
    "binding": BINDING_DOMAIN,
    "manual_event": EVENT_DOMAIN,
    "deadline": DEADLINE_DOMAIN,
    "sequence": SEQUENCE_DOMAIN,
}

_RELEASE_ORDER = {
    "duplicate": (DUP, FRESH),
    "binding": (BIND_NONE, BIND_MATCH, BIND_MISMATCH),
    "manual_event": (EVENT_UNCHANGED, EVENT_CHANGED),
    "deadline": (DEADLINE_LIVE, DEADLINE_EXPIRED),
    "sequence": (SEQ_ABSENT, SEQ_NOT_ABOVE, SEQ_ABOVE),
}


@dataclass(frozen=True)
class ReleaseWhen:
    """One release-machine row's predicate: cartesian over the five release facets, with the
    same semantics, derivation, and round-trip grammar as `When`."""

    duplicate: frozenset
    binding: frozenset
    manual_event: frozenset
    deadline: frozenset
    sequence: frozenset

    def __post_init__(self) -> None:
        for name, domain in _RELEASE_DOMAINS.items():
            allowed = getattr(self, name)
            if not allowed:
                raise ValueError(f"{name}: an empty allowed set is a row that matches nothing")
            unknown = set(allowed) - domain
            if unknown:
                raise ValueError(f"{name}: {sorted(unknown)} not in the closed domain")

    def matches(self, observed: dict) -> bool:
        return all(observed[name] in getattr(self, name) for name in _RELEASE_DOMAINS)

    def condition(self) -> str:
        parts = []
        for name, domain in _RELEASE_DOMAINS.items():
            allowed = getattr(self, name)
            if allowed == domain:
                continue
            ordered = [value for value in _RELEASE_ORDER[name] if value in allowed]
            if len(ordered) == 1:
                parts.append(f"{name} = {ordered[0]}")
            else:
                parts.append(f"{name} in {{{', '.join(ordered)}}}")
        if not parts:
            raise ValueError(
                "a release predicate over the full space derives 'always' — refused for the "
                "same reason as the base machine's"
            )
        return " AND ".join(parts)


def parse_release_condition(text: str) -> ReleaseWhen:
    """`parse_condition`, for the release machine's grammar and domains."""
    allowed: dict[str, frozenset] = {}
    for clause in text.split(" AND "):
        single = _SINGLE.match(clause)
        group = _GROUP.match(clause)
        if single:
            name, values = single.group(1), [single.group(2)]
        elif group:
            name, values = group.group(1), group.group(2).split(", ")
        else:
            raise ValueError(f"clause {clause!r} is outside the condition grammar")
        if name not in _RELEASE_DOMAINS:
            raise ValueError(f"unknown facet {name!r}")
        if name in allowed:
            raise ValueError(f"facet {name!r} appears twice")
        unknown = set(values) - _RELEASE_DOMAINS[name]
        if unknown:
            raise ValueError(f"{name}: unknown value(s) {sorted(unknown)}")
        allowed[name] = frozenset(values)
    return ReleaseWhen(**{
        name: allowed.get(name, domain) for name, domain in _RELEASE_DOMAINS.items()
    })


def release_state_space():
    """Every observable release-pending state: 2 x 3 x 2 x 2 x 3 = 72."""
    for duplicate in sorted(DUPLICATE_DOMAIN):
        for binding in sorted(BINDING_DOMAIN):
            for manual_event in sorted(EVENT_DOMAIN):
                for deadline in sorted(DEADLINE_DOMAIN):
                    for sequence in sorted(SEQUENCE_DOMAIN):
                        yield {
                            "duplicate": duplicate,
                            "binding": binding,
                            "manual_event": manual_event,
                            "deadline": deadline,
                            "sequence": sequence,
                        }


def release_partition_problems(rows) -> list[str]:
    """Every release-pending state must match EXACTLY ONE row — same property, same report shape
    as `partition_problems`."""
    problems = []
    for observed in release_state_space():
        matched = [index for index, row in enumerate(rows) if row.when.matches(observed)]
        if len(matched) != 1:
            problems.append(
                f"release state {observed} matches row(s) {matched or 'NONE'} — "
                f"{'uncovered' if not matched else 'competing answers'}"
            )
    return problems
