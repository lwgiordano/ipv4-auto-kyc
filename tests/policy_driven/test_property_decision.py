"""Property test (07 §unit): the decision function is total and deterministic
over its entire input space, and its core invariants hold everywhere."""

from hypothesis import given
from hypothesis import strategies as st

from kyc_tool.domain.decision import decide
from kyc_tool.domain.models import BrokerStatus, BuyEnablement, Decision, Gates

gates_strategy = st.builds(
    Gates,
    score_met=st.booleans(),
    legal_proof=st.booleans(),
    control_proof=st.booleans(),
    broker_ok=st.booleans(),
    no_hard_conflict=st.booleans(),
)


@given(
    score=st.integers(min_value=0, max_value=1000),
    gates=gates_strategy,
    org_passed=st.booleans(),
    broker_status=st.sampled_from(list(BrokerStatus)),
)
def test_decide_is_total_deterministic_and_invariant(score, gates, org_passed, broker_status):
    first = decide(score, gates, org_passed, broker_status)
    second = decide(score, gates, org_passed, broker_status)
    assert first == second  # deterministic
    assert isinstance(first.decision, Decision)  # total

    # invariants
    assert (first.decision is Decision.REJECT) == (broker_status is BrokerStatus.BLOCKED)
    if first.decision is Decision.APPROVE:
        assert gates.all_pass and org_passed
    if first.decision is Decision.APPROVE_BUY_LOCKED:
        assert gates.all_pass and not org_passed
    assert (first.buy_enablement is BuyEnablement.ENABLED) == org_passed
