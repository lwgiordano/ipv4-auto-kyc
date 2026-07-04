from kyc_tool.domain.models import RunState
from kyc_tool.domain.states import can_transition, is_terminal


def test_happy_path_transitions():
    path = [
        RunState.QUEUED,
        RunState.RESOLVE_INPUTS,
        RunState.BROKER_GATE,
        RunState.RUN_ADAPTERS,
        RunState.VALIDATE,
        RunState.WRITE_CHECKS,
        RunState.SCORE,
        RunState.DECIDE,
        RunState.PUBLISH_DECISION,
        RunState.COMPLETE,
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert can_transition(a, b), f"{a} -> {b} should be legal"


def test_broker_short_circuit():
    assert can_transition(RunState.BROKER_GATE, RunState.DECIDE)


def test_failed_reachable_from_non_terminal_only():
    assert can_transition(RunState.RUN_ADAPTERS, RunState.FAILED)
    assert not can_transition(RunState.COMPLETE, RunState.FAILED)
    assert not can_transition(RunState.FAILED, RunState.FAILED)


def test_illegal_jumps_rejected():
    assert not can_transition(RunState.QUEUED, RunState.DECIDE)
    assert not can_transition(RunState.SCORE, RunState.RUN_ADAPTERS)


def test_terminal_states():
    assert is_terminal(RunState.COMPLETE) and is_terminal(RunState.FAILED)
    assert not is_terminal(RunState.QUEUED)
