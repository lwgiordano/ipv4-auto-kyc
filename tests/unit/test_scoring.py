import re

from kyc_tool.config import REPO_ROOT
from kyc_tool.domain import scoring
from kyc_tool.domain.engine import ENGINE_BUILD_ID
from kyc_tool.domain.models import CheckStatus, CheckView
from kyc_tool.policy.loader import load_policy

RUBRIC = load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable").rubric


def test_engine_build_id_is_nonblank_versioned():
    assert re.fullmatch(r"eng-[1-9]\d*", ENGINE_BUILD_ID), ENGINE_BUILD_ID


def test_rubric_scoring_views_reprices_by_type():
    t = RUBRIC.check_types[0]
    item = RUBRIC.item(t)
    v = CheckView(check_type=t, status=CheckStatus.PASS, points_awarded=999,
                  category="stale", source="s", reason_codes=())
    out = scoring.rubric_scoring_views([v], RUBRIC)[0]
    assert out.points_awarded == item.points and out.category == item.category
    assert out.status is CheckStatus.PASS and out.check_type == t   # evidence unchanged


def test_rubric_scoring_views_zero_for_unknown_type():
    v = CheckView(check_type="not_in_rubric", status=CheckStatus.PASS,
                  points_awarded=50, category="c", source="s")
    out = scoring.rubric_scoring_views([v], RUBRIC)[0]
    assert out.points_awarded == 0 and out.category == ""
