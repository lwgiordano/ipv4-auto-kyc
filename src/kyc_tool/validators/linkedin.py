"""LinkedIn-tied-to-company validator (+20 supporting).

Pass rule (03 §2): person name AND current company AND title AND company
domain ALL deterministically match between Floqer's LinkedIn data and the
platform submission. Anything less is a fail (or no check at all when Floqer
produced no LinkedIn data) — Floqer alone never awards other points.

Two of the four comparisons are FIELD-level rather than string-level, which
keeps them exact: a person is compared first name to first name and last name
to last name when both sides carry the split, so a middle name present in one
place is not a mismatch; and the company is compared against the small set of
names that denote the same company (the submitted legal name plus Floqer's
aliases). Everything is still `norm_equal` — case/punctuation only, no token
reordering, no partial or fuzzy matching, no name inferred from the other.
"""

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.base import CheckIntent
from kyc_tool.validators.normalize import domain_of, norm_equal

SOURCE = "floqer_discovery_plus_deterministic_match"
_PROVENANCE_KEYS = ("linkedin_source", "web_verified")


def _name_matches(linkedin: dict, contact: dict) -> bool:
    """Split first/last when BOTH sides carry it, else the whole-name comparison."""
    split = ("first_name", "last_name")
    if all(linkedin.get(k) and contact.get(k) for k in split):
        return all(norm_equal(linkedin[k], contact[k]) for k in split)
    return norm_equal(linkedin.get("person_name"), contact.get("name"))


def linkedin_intent(floqer_normalized: dict, case_snapshot: dict) -> CheckIntent | None:
    linkedin = floqer_normalized.get("linkedin") or {}
    if not linkedin:
        return None  # no discovery data — no check, no points, nothing to fail

    contact = case_snapshot.get("contact") or {}
    submitted_domain = domain_of(
        case_snapshot.get("website") or case_snapshot.get("company_domain")
    )

    # LinkedIn shows the brand a company trades under where the platform submits its legal name:
    # the aliases Floqer discovered are the same company under another of its names, so they are
    # candidates for the SAME exact comparison — not a relaxation of it.
    company_names = (
        case_snapshot.get("company_legal_name"), *(floqer_normalized.get("aliases") or [])
    )

    checks = (
        _name_matches(linkedin, contact),
        any(norm_equal(linkedin.get("company"), name) for name in company_names),
        norm_equal(linkedin.get("title"), contact.get("title")),
        bool(submitted_domain)
        and domain_of(linkedin.get("company_domain")) == submitted_domain,
    )
    detail = {"linkedin": linkedin}
    detail.update({k: floqer_normalized[k] for k in _PROVENANCE_KEYS if k in floqer_normalized})
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
