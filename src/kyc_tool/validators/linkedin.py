"""LinkedIn-tied-to-company validator (+20 supporting).

Pass rule (03 §2): person name AND current company AND title AND company
domain ALL deterministically match between Floqer's LinkedIn data and the
platform submission. Anything less is a fail (or no check at all when Floqer
produced no LinkedIn data) — Floqer alone never awards other points.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import domain_of, norm_equal

SOURCE = "floqer_discovery_plus_deterministic_match"


def linkedin_intent(floqer_normalized: dict, case_snapshot: dict) -> CheckIntent | None:
    linkedin = floqer_normalized.get("linkedin") or {}
    if not linkedin:
        return None  # no discovery data — no check, no points, nothing to fail

    contact = case_snapshot.get("contact") or {}
    submitted_domain = domain_of(
        case_snapshot.get("website") or case_snapshot.get("company_domain")
    )

    checks = (
        norm_equal(linkedin.get("person_name"), contact.get("name")),
        norm_equal(linkedin.get("company"), case_snapshot.get("company_legal_name")),
        norm_equal(linkedin.get("title"), contact.get("title")),
        bool(submitted_domain)
        and domain_of(linkedin.get("company_domain")) == submitted_domain,
    )
    detail = {"linkedin": linkedin}
    if all(checks):
        return CheckIntent(
            "linkedin_company_match", CheckStatus.PASS, source=SOURCE, source_detail=detail
        )
    return CheckIntent(
        "linkedin_company_match",
        CheckStatus.FAIL,
        reason_codes=(ReasonCode.LINKEDIN_FIELDS_MISMATCH.value,),
        source=SOURCE,
        source_detail=detail,
    )
