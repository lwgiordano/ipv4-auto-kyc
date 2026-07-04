"""Scoring tests GENERATED from scoring_rubric.json — one per check type.
No point values appear in this file; they flow from the loader."""

from kyc_tool.config import REPO_ROOT
from kyc_tool.domain.models import BrokerStatus, CheckStatus, CheckView
from kyc_tool.domain.scoring import (
    CONTROL_PROOF_CATEGORY,
    LEGAL_PROOF_CATEGORY,
    evaluate_gates,
    score,
)
from kyc_tool.policy.loader import load_policy

BUNDLE = load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable")


def _view(item, status: CheckStatus) -> CheckView:
    return CheckView(
        check_type=item.check_type,
        status=status,
        points_awarded=item.points,
        category=item.category,
        source=item.source,
    )


def pytest_generate_tests(metafunc):
    if "rubric_item" in metafunc.fixturenames:
        metafunc.parametrize(
            "rubric_item", BUNDLE.rubric.items, ids=[i.check_type for i in BUNDLE.rubric.items]
        )


def test_single_pass_check_scores_its_rubric_points(rubric_item):
    breakdown = score([_view(rubric_item, CheckStatus.PASS)])
    assert breakdown.score == rubric_item.points
    assert breakdown.by_check == {rubric_item.check_type: rubric_item.points}


def test_fail_scores_zero(rubric_item):
    assert score([_view(rubric_item, CheckStatus.FAIL)]).score == 0


def test_needs_review_scores_zero(rubric_item):
    assert score([_view(rubric_item, CheckStatus.NEEDS_REVIEW)]).score == 0


def test_single_item_gate_category(rubric_item):
    views = [_view(rubric_item, CheckStatus.PASS)]
    gates = evaluate_gates(
        views, rubric_item.points, BUNDLE.rubric.threshold, BrokerStatus.CLEAR
    )
    assert gates.legal_proof == (rubric_item.category == LEGAL_PROOF_CATEGORY)
    assert gates.control_proof == (rubric_item.category == CONTROL_PROOF_CATEGORY)


def test_duplicate_live_type_counts_once(rubric_item):
    views = [_view(rubric_item, CheckStatus.PASS), _view(rubric_item, CheckStatus.PASS)]
    assert score(views).score == rubric_item.points


def test_all_pass_totals_rubric_sum_and_crosses_threshold():
    views = [_view(item, CheckStatus.PASS) for item in BUNDLE.rubric.items]
    total = score(views).score
    assert total == sum(item.points for item in BUNDLE.rubric.items)
    assert total >= BUNDLE.rubric.threshold  # full evidence must be approvable
