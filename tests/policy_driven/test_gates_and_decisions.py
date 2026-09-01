"""Decision truth table: all 2^5 gate combinations × ORG-ID passed × broker
status (AUDIT:B4 — approval branches on the ORG-ID input, a 6th dimension)."""

import itertools

import pytest

from kyc_tool.domain.decision import decide
from kyc_tool.domain.models import BrokerStatus, Decision, Gates

COMBOS = list(
    itertools.product(
        [False, True],  # score_met
        [False, True],  # legal_proof
        [False, True],  # control_proof
        [False, True],  # broker_ok
        [False, True],  # no_hard_conflict
        [False, True],  # org_id_passed
        list(BrokerStatus),
    )
)


@pytest.mark.parametrize(
    "score_met,legal,control,broker_ok,no_conflict,org_passed,broker_status", COMBOS
)
def test_decision_truth_table(score_met, legal, control, broker_ok, no_conflict, org_passed, broker_status):
    gates = Gates(
        score_met=score_met,
        legal_proof=legal,
        control_proof=control,
        broker_ok=broker_ok,
        no_hard_conflict=no_conflict,
    )
    result = decide(150 if score_met else 50, gates, org_passed, broker_status)

    if broker_status is BrokerStatus.BLOCKED:
        expected = Decision.REJECT  # priority 1, regardless of anything else
    elif gates.all_pass and org_passed:
        expected = Decision.APPROVE
    elif gates.all_pass:
        expected = Decision.APPROVE_BUY_LOCKED
    else:
        expected = Decision.MANUAL_REVIEW_INSUFFICIENT  # AUDIT:A1 catch-all

    assert result.decision is expected
    assert (result.buy_enablement.value == "enabled") == org_passed


def test_only_all_pass_approves():
    """No combination with a failing gate may yield either approval decision."""
    for combo in COMBOS:
        score_met, legal, control, broker_ok, no_conflict, org_passed, broker_status = combo
        gates = Gates(score_met, legal, control, broker_ok, no_conflict)
        if gates.all_pass:
            continue
        result = decide(999, gates, org_passed, broker_status)
        assert result.decision not in (Decision.APPROVE, Decision.APPROVE_BUY_LOCKED)
