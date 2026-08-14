"""The published receiver transition table, executed.

Re-audit `4f23f23..97deeae` F2. The prose it replaces said: manual-current is protected, "otherwise
apply" the unique callback. Every reader agreed it looked right. Run it against automatic decision
B being current and older automatic decision A arriving late under an unseen run id, and it replaces
B with A — a newer decision silently overwritten by an older one, on a wire with no ordering
authority and a `decided_at` the document itself forbids sorting by.

The previous verifier asserted that the words "manual" and "not" appeared in a sentence, which the
broken version satisfied. So this file drives the table instead: `docs/contracts/receiver_reference.py`
implements it, the scenarios below are the findings' own repros, and a structural test binds each
branch to the published row it claims to implement.
"""

import dataclasses as _dataclasses

import pytest
from docs.contracts import predicates as _predicates
from docs.contracts.receiver_reference import (
    Callback,
    LedgerState,
    Outcome,
    TableIntegrityError,
    UnknownSourceError,
    decide,
    observe,
)
from docs.contracts.wire import (
    INTERIM,
    KNOWN_SOURCES,
    POST_024,
    RECEIVER_TRANSITIONS,
    WIRE,
    Transition,
)

CASE = "case-42"


def _rows(phase: str):
    return [t for t in RECEIVER_TRANSITIONS if t.phase == phase]


# ── the table is the claim, and it is total ───────────────────────────────────────────────────────
def test_the_effectiveness_claim_is_the_transition_table():
    assert WIRE.value("WIRE.CALLBACK.EFFECTIVENESS") is RECEIVER_TRANSITIONS


def test_every_phase_covers_its_whole_space():
    """Totality is the property the prose lacked. Each phase must handle: a duplicate, no current
    decision, a manual-current case, and an automatic-current case — with no row left implicit."""
    for phase in (INTERIM, POST_024):
        rows = _rows(phase)
        assert len(rows) == 4, f"{phase} has {len(rows)} rows; the space needs exactly four"
        joined = " ".join(r.condition.lower() for r in rows)
        assert "already in your accepted ledger" in joined
        assert "manual" in joined
        assert all(r.record and r.effective and r.why for r in rows)


def test_no_row_falls_through_to_an_otherwise():
    """The exact defect. A row whose condition is 'otherwise' is not a condition."""
    for transition in RECEIVER_TRANSITIONS:
        assert "otherwise" not in transition.condition.lower(), transition
    note = WIRE["WIRE.CALLBACK.EFFECTIVENESS"].note.lower()
    # The note used to promise "first match wins" over an "exhaustive" list — order-dependent
    # selection over rows nothing verified. It now states the property the enumeration actually
    # proves: the rows PARTITION the state space, so no state has zero answers or two.
    assert "partition" in note and "exactly one row" in note
    assert "otherwise" in note  # the note still names the banned branch explicitly


def test_the_reference_implementation_uses_every_published_row():
    """Each branch reports the row that decided it, so an implementation that quietly collapses two
    published rows into one behaviour cannot pass."""
    for phase in (INTERIM, POST_024):
        reached = set()
        for state, callback in _scenarios_for(phase):
            reached.add(decide(state, callback, phase=phase).row)
        assert reached == set(range(len(_rows(phase)))), f"{phase}: unreached rows"


def _scenarios_for(phase):
    seen = frozenset({"run-old"})
    duplicate = Callback(CASE, "run-old", decision_sequence=1)
    fresh = Callback(CASE, "run-new", decision_sequence=9)
    stale = Callback(CASE, "run-new", decision_sequence=1)
    return [
        (LedgerState(seen_run_ids=seen, current_source="automatic", high_water=5), duplicate),
        (LedgerState(), fresh if phase == POST_024 else Callback(CASE, "run-new")),
        (LedgerState(current_source="manual", high_water=5), fresh),
        (LedgerState(current_source="automatic", high_water=5),
         stale if phase == POST_024 else Callback(CASE, "run-new")),
        (LedgerState(current_source="automatic", high_water=5), fresh),
    ]


# ── the findings' own repros ──────────────────────────────────────────────────────────────────────
def test_a_late_older_automatic_decision_does_not_replace_the_current_one():
    """B is current. A is older but delayed, and arrives under a run id never seen. The old prose
    applied it because it was unique and the current source was not manual."""
    state = LedgerState(seen_run_ids=frozenset({"run-B"}), current_source="automatic")
    outcome = decide(state, Callback(CASE, "run-A"), phase=INTERIM)
    assert outcome.record is True, "it must still be acknowledged and recorded"
    assert outcome.effective is False, "B must remain the effective decision"


def test_a_manual_current_case_is_not_overturned_by_a_callback():
    state = LedgerState(current_source="manual")
    outcome = decide(state, Callback(CASE, "run-new"), phase=INTERIM)
    assert outcome.record is True and outcome.effective is False


def test_post_activation_manual_current_advances_the_mark_without_applying():
    """h=5, s=6, manual current: manual stays effective AND the high-water mark moves to 6."""
    state = LedgerState(current_source="manual", high_water=5)
    outcome = decide(state, Callback(CASE, "run-new", decision_sequence=6), phase=POST_024)
    assert outcome.effective is False, "the reviewer's decision must stand"
    assert outcome.advance_high_water is True, "a stale mark misjudges the next decision"
    assert outcome.record is True


def test_ordering_uses_decision_sequence_and_never_event_sequence():
    """ROADMAP D1: event_sequence is PR 2 ingest provenance, decision_sequence is the PR 7b
    callback-order authority. They invert whenever work completes out of admission order.

    e1 is admitted first (event 1) but runs slowly; e2 is admitted second (event 2) and decides
    first. Their DECISION ordinals are the other way round: e2 is decision 1, e1 is decision 2. A
    receiver ordering on event_sequence applies e2 at h=2 and then suppresses e1's later decision
    as stale (re-audit `4f23f23..122cc67` finding 1)."""
    state = LedgerState()
    # e2 decides first: event ordinal 2, decision ordinal 1
    first = decide(state, Callback(CASE, "run-e2", decision_sequence=1, event_sequence=2),
                   phase=POST_024)
    assert first.effective and first.advance_high_water
    state = LedgerState(seen_run_ids=frozenset({"run-e2"}), current_source="automatic",
                        high_water=1)
    # e1 decides second: event ordinal 1, decision ordinal 2 — it is the LATER decision and wins
    second = decide(state, Callback(CASE, "run-e1", decision_sequence=2, event_sequence=1),
                    phase=POST_024)
    assert second.effective, "the later DECISION must win, whatever its event ordinal says"
    assert second.advance_high_water

    # and event_sequence alone orders nothing: with no decision_sequence it never takes effect
    unordered = decide(LedgerState(current_source="automatic", high_water=1),
                       Callback(CASE, "r", event_sequence=99), phase=POST_024)
    assert unordered.record and not unordered.effective and not unordered.advance_high_water


def test_post_activation_applies_only_on_proven_order():
    automatic = LedgerState(current_source="automatic", high_water=5)
    assert decide(automatic, Callback(CASE, "r", decision_sequence=6), phase=POST_024).effective
    assert not decide(automatic, Callback(CASE, "r", decision_sequence=5), phase=POST_024).effective
    assert not decide(automatic, Callback(CASE, "r", decision_sequence=4), phase=POST_024).effective
    # no sequence at all is unordered, so it records without taking effect
    unordered = decide(automatic, Callback(CASE, "r"), phase=POST_024)
    assert unordered.record and not unordered.effective and not unordered.advance_high_water


def test_a_duplicate_is_acknowledged_and_changes_nothing():
    for phase in (INTERIM, POST_024):
        state = LedgerState(seen_run_ids=frozenset({"run-1"}), current_source="automatic",
                            high_water=5)
        outcome = decide(state, Callback(CASE, "run-1", decision_sequence=99), phase=phase)
        assert outcome == Outcome(row=0, record=False, effective=False, advance_high_water=False)


def test_an_ordinary_increasing_automatic_stream_applies_each_time():
    state = LedgerState()
    high_water = None
    for sequence, run_id in enumerate(("r1", "r2", "r3"), start=1):
        outcome = decide(state, Callback(CASE, run_id, decision_sequence=sequence), phase=POST_024)
        assert outcome.effective, f"{run_id} should apply"
        high_water = sequence if outcome.advance_high_water else high_water
        state = LedgerState(seen_run_ids=state.seen_run_ids | {run_id},
                            current_source="automatic", high_water=high_water)
    assert high_water == 3


def test_only_an_authenticated_release_restores_automatic_authority():
    """There is no row that hands authority back, in either phase. That is the point: it comes
    back through the platform-owned release protocol, not by a callback arriving."""
    for phase in (INTERIM, POST_024):
        for sequence in (None, 1, 99):
            state = LedgerState(current_source="manual", high_water=5)
            assert not decide(state, Callback(CASE, "r", decision_sequence=sequence),
                              phase=phase).effective
    note = WIRE["WIRE.CALLBACK.EFFECTIVENESS"].note
    assert "authenticated" in note and "release protocol" in note


# ── mutation: restoring the old behaviour must fail ───────────────────────────────────────────────
def test_restoring_otherwise_apply_fails_the_scenarios():
    """The guard's own failure mode. This is the algorithm the document used to publish."""

    def broken(state, callback, *, phase):
        if callback.run_id in state.seen_run_ids:
            return Outcome(0, False, False, False)
        if state.current_source == "manual":
            return Outcome(2, True, False, False)
        return Outcome(1, True, True, False)  # "otherwise apply"

    state = LedgerState(seen_run_ids=frozenset({"run-B"}), current_source="automatic")
    assert broken(state, Callback(CASE, "run-A"), phase=INTERIM).effective, (
        "the old algorithm applied the late older decision — if this stops being true the "
        "mutation no longer reproduces the finding"
    )
    assert not decide(state, Callback(CASE, "run-A"), phase=INTERIM).effective


@pytest.mark.parametrize("dropped", range(8))
def test_deleting_any_row_breaks_totality(dropped):
    survivors = tuple(t for i, t in enumerate(RECEIVER_TRANSITIONS) if i != dropped)
    for phase in (INTERIM, POST_024):
        rows = [t for t in survivors if t.phase == phase]
        if len(rows) != 4:
            break
    else:  # pragma: no cover — one of the two phases always loses a row
        raise AssertionError(f"dropping row {dropped} left both phases complete")


# ── the implementation is bound to the row's OUTCOME, not just its index ─────────────────────────
def test_every_outcome_matches_the_published_row_it_claims(  # noqa: C901
):
    """Re-audit `4f23f23..122cc67` finding 2. The implementation reported a row NUMBER and nothing
    compared its behaviour to that row's words, so rewriting a published row to
    `effective=YES — replace the manual approval` left the whole suite green: the code kept doing
    the right thing while the document told TechCraft the opposite."""
    for phase in (INTERIM, POST_024):
        rows = _rows(phase)
        for state, callback in _scenarios_for(phase):
            outcome = decide(state, callback, phase=phase)
            row = rows[outcome.row]
            assert outcome.record == row.records, (
                f"{phase} row {outcome.row}: implementation records={outcome.record}, "
                f"table says {row.record!r}")
            assert outcome.effective == row.becomes_effective, (
                f"{phase} row {outcome.row}: implementation effective={outcome.effective}, "
                f"table says {row.effective!r}")
            assert outcome.advance_high_water == row.advances_high_water, (
                f"{phase} row {outcome.row}: implementation advance={outcome.advance_high_water}, "
                f"table says {row.record!r}")


def test_the_prose_and_the_structured_outcome_agree():
    """The booleans are what the tests read; the prose is what TechCraft reads. If they diverge,
    the machine-checked half is not checking the published half."""
    for row in RECEIVER_TRANSITIONS:
        effective = row.effective.upper()
        says_yes = effective.startswith("YES")
        assert says_yes == row.becomes_effective, row
        if not row.records:
            assert "nothing new" in row.record.lower(), row
        if row.advances_high_water:
            assert "advance the high-water mark" in row.record.lower(), row
        else:
            assert "advance the high-water mark" not in row.record.lower(), row


@pytest.mark.parametrize("source", ["manual_release_pending", "MANUAL", "Automatic", "typo", ""])
def test_an_unrecognised_current_source_holds_instead_of_applying(source):
    """Every unknown source used to fall through to the automatic branch and TAKE EFFECT — the one
    case where you cannot tell whether a human decided this is exactly the case where applying is
    unsafe."""
    for phase in (INTERIM, POST_024):
        with pytest.raises(UnknownSourceError):
            decide(LedgerState(current_source=source, high_water=1),
                   Callback(CASE, "r", decision_sequence=9), phase=phase)


def test_the_two_known_sources_are_the_only_ones():
    assert {"manual", "automatic"} == KNOWN_SOURCES
    for source in (None, "manual", "automatic"):
        decide(LedgerState(current_source=source), Callback(CASE, "r", decision_sequence=1),
               phase=POST_024)


def test_mutating_any_published_outcome_field_fails_the_binding():
    """The guard's own failure mode: flip a row's outcome and the binding test must reject it."""
    import dataclasses

    for index, row in enumerate(RECEIVER_TRANSITIONS):
        mutated = dataclasses.replace(row, becomes_effective=not row.becomes_effective)
        rows = [t for t in RECEIVER_TRANSITIONS if t.phase == row.phase]
        position = rows.index(row)
        for state, callback in _scenarios_for(row.phase):
            outcome = decide(state, callback, phase=row.phase)
            if outcome.row == position:
                assert outcome.effective != mutated.becomes_effective, (
                    f"row {index} could be flipped without the binding noticing")
                break


# ── F10: the mutations that got through "four rows plus token presence", each RED ─────────────
#
# Codex widened the post-024 manual row to "MANUAL or AUTOMATIC" and every suite stayed green: the
# evaluator had its own hardcoded branches, and totality was checked as a row count. Rows now carry
# executable predicates, the evaluator executes them, and each phase must PARTITION the closed
# state space. Every mutation below is run through `partition_problems` — the same check the
# release verifier runs — and through `decide()`, which must HOLD (raise), never pick, on an
# ambiguous table.

def _phase_rows(phase):
    return [t for t in RECEIVER_TRANSITIONS if t.phase == phase]


def test_the_shipped_tables_partition_the_state_space():
    """Baseline: without this, every rejection below could be the checker failing on anything."""
    for phase in (INTERIM, POST_024):
        assert _predicates.partition_problems(_phase_rows(phase)) == []


def test_codex_overlap_widening_the_manual_row_is_caught():
    """The literal F10 mutation: post-024 row 2 claims MANUAL *or* AUTOMATIC, so the ABOVE x
    AUTOMATIC state has two competing answers — one says effective, one says not."""
    rows = _phase_rows(POST_024)
    widened = _dataclasses.replace(
        rows[2],
        when=_predicates.When(
            duplicate=frozenset({_predicates.FRESH}),
            source=frozenset({_predicates.SRC_MANUAL, _predicates.SRC_AUTOMATIC}),
            sequence=frozenset({_predicates.SEQ_ABOVE})))
    mutated = [rows[0], rows[1], widened, rows[3]]
    problems = _predicates.partition_problems(mutated)
    assert problems and all("competing answers" in x for x in problems), problems


def test_an_uncovered_sequence_relation_is_caught():
    """Drop SEQ_ABSENT from post-024 row 1: a callback with no decision_sequence matches nothing,
    and the receiver has no published answer for a state that arrives on day one."""
    rows = _phase_rows(POST_024)
    narrowed = _dataclasses.replace(
        rows[1],
        when=_predicates.When(
            duplicate=frozenset({_predicates.FRESH}),
            source=_predicates.ANY_SOURCE,
            sequence=frozenset({_predicates.SEQ_NOT_ABOVE})))
    mutated = [rows[0], narrowed, rows[2], rows[3]]
    problems = _predicates.partition_problems(mutated)
    assert problems and any("uncovered" in x for x in problems), problems


def test_a_deleted_row_is_caught():
    for phase in (INTERIM, POST_024):
        rows = _phase_rows(phase)
        for drop in range(len(rows)):
            mutated = rows[:drop] + rows[drop + 1:]
            assert _predicates.partition_problems(mutated), (phase, drop)


def test_a_shadowed_extra_row_is_caught():
    """A fifth row whose states are already claimed: with first-match semantics it is silently
    dead weight a reader still trusts; under the partition property it is competing answers."""
    rows = _phase_rows(INTERIM)
    shadow = _dataclasses.replace(rows[1], why="a shadowed restatement")
    assert _predicates.partition_problems([*rows, shadow])


def test_the_duplicate_row_leads_each_phase():
    """Evaluation order is part of the published contract even though a partition makes selection
    order-independent — the PDF prints the rows in order and names the duplicate check first."""
    for phase in (INTERIM, POST_024):
        first = _phase_rows(phase)[0]
        assert first.when.duplicate == frozenset({_predicates.DUP})
        assert first.when.source == _predicates.ANY_SOURCE


def test_decide_holds_rather_than_picking_on_an_ambiguous_table(monkeypatch):
    """If the table ever stops being a partition at runtime, the receiver must HOLD. Which of two
    matching rows wins would otherwise be an implementation accident — the F10 defect itself."""
    from docs.contracts import receiver_reference, wire

    rows = _phase_rows(POST_024)
    widened = _dataclasses.replace(
        rows[2],
        when=_predicates.When(
            duplicate=frozenset({_predicates.FRESH}),
            source=frozenset({_predicates.SRC_MANUAL, _predicates.SRC_AUTOMATIC}),
            sequence=frozenset({_predicates.SEQ_ABOVE})))
    mutated = tuple(_phase_rows(INTERIM)) + (rows[0], rows[1], widened, rows[3])
    monkeypatch.setattr(wire, "RECEIVER_TRANSITIONS", mutated)
    monkeypatch.setattr(receiver_reference, "RECEIVER_TRANSITIONS", mutated)
    state = LedgerState(seen_run_ids=frozenset(), current_source="automatic", high_water=1)
    with pytest.raises(TableIntegrityError):
        decide(state, Callback(case_id="c", run_id="r2", decision_sequence=9), phase=POST_024)


def test_an_unknown_source_is_refused_before_any_row_is_consulted():
    """The fail-closed gate lives in `observe`, ahead of matching, so no predicate widening can
    make an unclassifiable source eligible for any row."""
    state = LedgerState(seen_run_ids=frozenset(), current_source="manual_release_pending")
    with pytest.raises(UnknownSourceError):
        observe(state, Callback(case_id="c", run_id="r1"))


def test_a_condition_cannot_be_authored():
    """The text is the predicate's derivation; there is no field left to write prose into."""
    with pytest.raises(TypeError):
        Transition(phase=INTERIM, condition="otherwise apply", record="x", effective="x", why="x",
                   when=_predicates.When(duplicate=frozenset({_predicates.DUP}),
                                         source=_predicates.ANY_SOURCE,
                                         sequence=_predicates.ANY_SEQUENCE))


def test_a_full_space_predicate_cannot_derive_a_condition():
    """`When(everything)` derives the condition 'always' — the 'otherwise apply' defect wearing
    predicate syntax. Refused at derivation."""
    with pytest.raises(ValueError, match="otherwise apply"):
        _predicates.When(duplicate=_predicates.ANY_DUPLICATE,
                         source=_predicates.ANY_SOURCE,
                         sequence=_predicates.ANY_SEQUENCE).condition()
