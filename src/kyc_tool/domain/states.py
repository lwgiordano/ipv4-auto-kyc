"""Run state machine — legal transitions per machine_readable/state_machine.json."""

from kyc_tool.domain.models import RunState

# (from, to) pairs. FAILED is reachable from any non-terminal state.
LEGAL_TRANSITIONS: frozenset[tuple[RunState, RunState]] = frozenset(
    {
        (RunState.QUEUED, RunState.RESOLVE_INPUTS),
        (RunState.RESOLVE_INPUTS, RunState.BROKER_GATE),
        (RunState.BROKER_GATE, RunState.RUN_ADAPTERS),
        # exact blocked broker match: short-circuit to reject
        (RunState.BROKER_GATE, RunState.DECIDE),
        (RunState.RUN_ADAPTERS, RunState.VALIDATE),
        (RunState.VALIDATE, RunState.WRITE_CHECKS),
        (RunState.WRITE_CHECKS, RunState.SCORE),
        (RunState.SCORE, RunState.DECIDE),
        (RunState.DECIDE, RunState.PUBLISH_DECISION),
        (RunState.PUBLISH_DECISION, RunState.COMPLETE),
    }
)

TERMINAL_STATES: frozenset[RunState] = frozenset({RunState.COMPLETE, RunState.FAILED})


def can_transition(from_state: RunState, to_state: RunState) -> bool:
    if to_state is RunState.FAILED:
        return from_state not in TERMINAL_STATES
    return (from_state, to_state) in LEGAL_TRANSITIONS


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES
