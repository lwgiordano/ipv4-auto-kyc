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

import pytest
from docs.contracts.receiver_reference import Callback, LedgerState, Outcome, decide
from docs.contracts.wire import INTERIM, POST_024, RECEIVER_TRANSITIONS, WIRE

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
    assert "first match wins" in note and "exhaustive" in note


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
