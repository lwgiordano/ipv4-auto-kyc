"""PR 3 (item 4): gate 5 keys on the centralized HARD_CONFLICT_REASON_CODES
allow-list — document/registry conflicts fail the gate, routing codes never do,
and the check remains failed even if a producer stops stamping HARD_CONFLICT."""

from kyc_tool.domain.decision import decide
from kyc_tool.domain.models import BrokerStatus, CheckStatus, CheckView, Decision
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.domain.scoring import (
    HARD_CONFLICT_REASON_CODES,
    evaluate_gates,
    has_hard_conflict,
    org_id_check_passed,
    score,
)


def _check(reason_codes, *, status=CheckStatus.FAIL):
    return CheckView(
        check_type="business_document_verified",
        status=status,
        points_awarded=0,
        category="legal_business_proof",
        source="test",
        reason_codes=tuple(reason_codes),
    )


def test_document_registry_conflict_trips_gate5():
    assert has_hard_conflict([_check([ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value])]) is True


def test_hard_conflict_reason_trips_gate5():
    assert has_hard_conflict([_check([ReasonCode.HARD_CONFLICT.value], status=CheckStatus.PASS)]) is True


def test_broker_conflict_routing_code_does_not_trip_gate5():
    # a *_CONFLICT-suffixed code that is NOT in the allow-list must not fail the gate
    assert has_hard_conflict([_check([ReasonCode.ORG_ID_BROKER_CONFLICT.value])]) is False


def test_clean_evidence_has_no_conflict():
    assert has_hard_conflict([_check([], status=CheckStatus.PASS)]) is False


def test_allow_list_membership_is_the_regression_guard():
    # even if documents.py stops stamping HARD_CONFLICT, the gate still fails on
    # document_registry_conflict because the SET (not a producer) is the SoT
    assert ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value in HARD_CONFLICT_REASON_CODES
    assert ReasonCode.HARD_CONFLICT.value in HARD_CONFLICT_REASON_CODES
    assert ReasonCode.ORG_ID_BROKER_CONFLICT.value not in HARD_CONFLICT_REASON_CODES


def _cv(check_type, points, category, *, status=CheckStatus.PASS, reasons=()):
    return CheckView(check_type, status, points, category, "test", tuple(reasons))


def test_high_score_with_document_conflict_never_approves():
    """PR 3: a case worth >= 105 points that also carries a document/registry
    conflict must never auto-approve — gate 5 fails, so it holds for review."""
    views = [
        _cv("official_registry_match", 25, "legal_business_proof"),
        _cv("org_id_match", 25, "control_proof"),
        _cv("verified_company_email", 25, "email_proof"),
        _cv("linkedin_company_match", 20, "supporting"),
        _cv("website_verified", 10, "supporting"),
        _cv(
            "business_document_verified",
            0,
            "legal_business_proof",
            status=CheckStatus.FAIL,
            reasons=[ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value],
        ),
    ]
    breakdown = score(views)
    assert breakdown.score >= 105  # comfortably over the 100 threshold
    gates = evaluate_gates(views, breakdown.score, 100, BrokerStatus.CLEAR)
    assert gates.score_met is True
    assert gates.no_hard_conflict is False  # the conflict trips gate 5
    assert gates.all_pass is False
    result = decide(breakdown.score, gates, org_id_check_passed(views), BrokerStatus.CLEAR)
    assert result.decision is Decision.MANUAL_REVIEW_INSUFFICIENT  # never approve
