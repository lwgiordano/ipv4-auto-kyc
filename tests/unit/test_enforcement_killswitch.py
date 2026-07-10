"""The temporary enforcement overlay: while approval-grade validators are
known-permissive (remediation items 3–5), auto-enforceable positive decisions
are held for manual review. Pure-function behaviour; the pipeline wiring is
covered by the integration suite."""

from kyc_tool.domain.decision import decide, hold_positive_for_manual_review
from kyc_tool.domain.models import BrokerStatus, Decision, Gates

ALL_PASS = Gates(True, True, True, True, True)


def test_approve_is_held_for_manual_review():
    approved = decide(120, ALL_PASS, org_id_passed=True, broker_status=BrokerStatus.CLEAR)
    assert approved.decision is Decision.APPROVE

    held = hold_positive_for_manual_review(approved)
    assert held.decision is Decision.MANUAL_REVIEW_INSUFFICIENT
    # score, gates and buy-enablement are preserved unchanged
    assert held.score == approved.score
    assert held.gates == approved.gates
    assert held.buy_enablement == approved.buy_enablement


def test_approve_buy_locked_is_held():
    locked = decide(120, ALL_PASS, org_id_passed=False, broker_status=BrokerStatus.CLEAR)
    assert locked.decision is Decision.APPROVE_BUY_LOCKED
    assert hold_positive_for_manual_review(locked).decision is Decision.MANUAL_REVIEW_INSUFFICIENT


def test_reject_is_not_touched():
    rejected = decide(0, ALL_PASS, org_id_passed=False, broker_status=BrokerStatus.BLOCKED)
    assert rejected.decision is Decision.REJECT
    assert hold_positive_for_manual_review(rejected) is rejected


def test_manual_review_is_not_touched():
    gates = Gates(False, True, True, True, True)  # score not met
    manual = decide(50, gates, org_id_passed=False, broker_status=BrokerStatus.CLEAR)
    assert manual.decision is Decision.MANUAL_REVIEW_INSUFFICIENT
    assert hold_positive_for_manual_review(manual) is manual
