"""Decision engine — a total, deterministic pure function.

Implements machine_readable/decision_policy.json in priority order. Per
AUDIT:A1 the prose decision table under-covers the input space; the JSON's
"otherwise" catch-all (manual_review_insufficient) is what's built. Per
AUDIT:B3, v1's only reject trigger is the exact blocked-broker match — the
spec's undefined "hard_block" is not invented here.

Kept enum-in/enum-out so the documented future extension
(manual_review_risk_flag, decision_policy.json §future_extension_not_in_v1)
is additive.
"""

from dataclasses import replace

from kyc_tool.domain.models import (
    BrokerStatus,
    BuyEnablement,
    Decision,
    DecisionResult,
    Gates,
    RejectReason,
)

POSITIVE_DECISIONS = (Decision.APPROVE, Decision.APPROVE_BUY_LOCKED)


def hold_positive_for_manual_review(result: DecisionResult) -> DecisionResult:
    """Emergency enforcement overlay (temporary — remove with remediation items
    3–5). While the approval-grade validators are known-permissive, the tool
    must not emit an auto-enforceable positive decision, so an approve /
    approve_buy_locked outcome is downgraded to the manual-review holding state.
    Score, gates and buy-enablement are preserved unchanged; the true computed
    decision is recorded in the audit trail at the call site."""
    if result.decision in POSITIVE_DECISIONS:
        return replace(result, decision=Decision.MANUAL_REVIEW_INSUFFICIENT)
    return result


def buy_enablement_for(org_id_passed: bool) -> BuyEnablement:
    """Single source of truth for the buy-enablement rule: buying is enabled
    only when a live ORG-ID check has passed, else locked pending ORG-ID.
    Used by decide() and the reviewer.manual_approve path so the rule can never
    diverge between the two."""
    return BuyEnablement.ENABLED if org_id_passed else BuyEnablement.LOCKED_ORG_ID_REQUIRED


def decide(
    total_score: int,
    gates: Gates,
    org_id_passed: bool,
    broker_status: BrokerStatus,
) -> DecisionResult:
    buy = buy_enablement_for(org_id_passed)

    # priority 1 — reject (short-circuit path lands here too)
    if broker_status is BrokerStatus.BLOCKED:
        return DecisionResult(
            decision=Decision.REJECT,
            score=total_score,
            gates=gates,
            buy_enablement=buy,
            reject_reason=RejectReason.BLOCKED_BROKER_EXACT_MATCH,
        )

    # priority 2 — approve (all five gates AND a passed ORG-ID check)
    if gates.all_pass and org_id_passed:
        return DecisionResult(
            decision=Decision.APPROVE, score=total_score, gates=gates, buy_enablement=buy
        )

    # priority 3 — approve with buying locked (all gates, no passed ORG-ID)
    if gates.all_pass and not org_id_passed:
        return DecisionResult(
            decision=Decision.APPROVE_BUY_LOCKED,
            score=total_score,
            gates=gates,
            buy_enablement=buy,
        )

    # priority 4 — the catch-all holding state (auto-clears on recalculation)
    return DecisionResult(
        decision=Decision.MANUAL_REVIEW_INSUFFICIENT,
        score=total_score,
        gates=gates,
        buy_enablement=buy,
    )
