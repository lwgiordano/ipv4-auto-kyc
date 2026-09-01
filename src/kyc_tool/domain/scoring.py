"""Scoring engine — pure function over live (non-superseded) checks.

Points and categories come from the normative rubric (policy loader); this
module never hardcodes them. One live check per type is guaranteed by the
check store (partial unique index), but the fold is defensive anyway: a type
counts once (AUDIT: rubric caps 'ORG-ID at most 25; POC at most 25').
"""

from dataclasses import replace

from kyc_tool.domain.models import BrokerStatus, CheckStatus, CheckView, Gates, ScoreBreakdown
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.policy.types import ScoringRubric

LEGAL_PROOF_CATEGORY = "legal_business_proof"
CONTROL_PROOF_CATEGORY = "control_proof"


def score(live_checks: list[CheckView]) -> ScoreBreakdown:
    """Sum points of live passing checks, counting each check type once."""
    by_check: dict[str, int] = {}
    for check in live_checks:
        if check.status is not CheckStatus.PASS:
            continue
        existing = by_check.get(check.check_type, 0)
        by_check[check.check_type] = max(existing, check.points_awarded)
    return ScoreBreakdown(score=sum(by_check.values()), by_check=by_check)


def rubric_scoring_views(views: list[CheckView], rubric: ScoringRubric) -> list[CheckView]:
    """Re-price each live check from `rubric` by check_type (PR 6 flag-on). A type
    absent from the rubric contributes no points/category. Evidence (status,
    reason_codes, source) is untouched — PASS/FAIL re-judgment is PR 6b."""
    out = []
    for v in views:
        try:
            item = rubric.item(v.check_type)
            pts, cat = item.points, item.category
        except KeyError:
            pts, cat = 0, ""
        out.append(replace(v, points_awarded=pts, category=cat))
    return out


# Gate 5's single source of truth: every reason code that means "the live
# evidence set contradicts itself". A deliberate, explicit allow-list — NOT a
# suffix rule — so adding a conflict type is a reviewed one-line change here and
# routing codes like org_id_broker_conflict can never trip the gate by accident.
HARD_CONFLICT_REASON_CODES = frozenset(
    {
        ReasonCode.HARD_CONFLICT.value,
        ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value,
    }
)


def has_hard_conflict(live_checks: list[CheckView]) -> bool:
    """Gate 5. A conflict is a property of the live evidence set — validators
    stamp a HARD_CONFLICT_REASON_CODES member onto the conflicting check(s), so
    the flag survives recalculation from live checks alone."""
    return any(
        code in HARD_CONFLICT_REASON_CODES
        for check in live_checks
        for code in check.reason_codes
    )


def evaluate_gates(
    live_checks: list[CheckView],
    total_score: int,
    threshold: int,
    broker_status: BrokerStatus,
    allowed_broker_statuses: tuple[str, ...] = (
        BrokerStatus.CLEAR.value,
        BrokerStatus.ALLOWED_BROKER.value,
    ),
) -> Gates:
    """The five hard gates (AUDIT:A2). All must pass for auto-approval."""
    passing = [c for c in live_checks if c.status is CheckStatus.PASS]
    return Gates(
        score_met=total_score >= threshold,
        legal_proof=any(c.category == LEGAL_PROOF_CATEGORY for c in passing),
        control_proof=any(c.category == CONTROL_PROOF_CATEGORY for c in passing),
        broker_ok=broker_status.value in allowed_broker_statuses,
        no_hard_conflict=not has_hard_conflict(live_checks),
    )


def org_id_check_passed(live_checks: list[CheckView]) -> bool:
    """Buy enablement hinges on a live, passing ORG-ID check."""
    return any(
        c.check_type == "org_id_match" and c.status is CheckStatus.PASS for c in live_checks
    )
