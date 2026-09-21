import re

from kyc_tool.config import REPO_ROOT
from kyc_tool.domain import scoring
from kyc_tool.domain.engine import ENGINE_BUILD_ID
from kyc_tool.domain.models import BrokerStatus, CheckStatus, CheckView
from kyc_tool.policy.loader import load_policy

RUBRIC = load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable").rubric


def test_engine_build_id_is_nonblank_versioned():
    assert re.fullmatch(r"eng-[1-9]\d*", ENGINE_BUILD_ID), ENGINE_BUILD_ID


def test_rubric_scoring_views_reprices_by_type():
    t = RUBRIC.check_types[0]
    item = RUBRIC.item(t)
    v = CheckView(
        check_type=t,
        status=CheckStatus.PASS,
        points_awarded=999,
        category="stale",
        source="s",
        reason_codes=(),
    )
    out = scoring.rubric_scoring_views([v], RUBRIC)[0]
    assert out.points_awarded == item.points and out.category == item.category
    assert out.status is CheckStatus.PASS and out.check_type == t  # evidence unchanged


def test_rubric_scoring_views_zero_for_unknown_type():
    v = CheckView(
        check_type="not_in_rubric", status=CheckStatus.PASS, points_awarded=50, category="c", source="s"
    )
    out = scoring.rubric_scoring_views([v], RUBRIC)[0]
    assert out.points_awarded == 0 and out.category == ""


def _view(check_type: str, points: int = 25, category: str = "supporting") -> CheckView:
    return CheckView(
        check_type=check_type,
        status=CheckStatus.PASS,
        points_awarded=points,
        category=category,
        source="test",
    )


def test_active_registry_with_email_and_org_id_has_no_independent_control_proof():
    """Old policy categories cannot turn public matches into independent control."""
    old_items = tuple(
        item.model_copy(
            update={
                "category": (
                    "control_proof"
                    if item.check_type in {"verified_company_email", "org_id_match"}
                    else item.category
                )
            }
        )
        for item in RUBRIC.items
    )
    old_rubric = RUBRIC.model_copy(update={"version": "legacy-test", "items": old_items})
    repriced = scoring.rubric_scoring_views(
        [
            _view("official_registry_match"),
            _view("verified_company_email"),
            _view("org_id_match"),
        ],
        old_rubric,
    )
    gates = scoring.evaluate_gates(repriced, 100, 100, BrokerStatus.CLEAR)
    assert gates.control_proof is False


def test_verified_poc_is_control_proof_even_without_control_category():
    views = [_view("poc_verified", category="supporting")]
    gates = scoring.evaluate_gates(views, 25, 100, BrokerStatus.CLEAR)
    assert gates.control_proof is True
