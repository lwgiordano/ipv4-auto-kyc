"""LinkedIn-tied-to-company validator (+20 supporting).

Pass rule (03 §2, amended 2026-09-16): person name AND company identity
deterministically match between Floqer's LinkedIn data and the platform
submission. Company identity is the company domain; only when LinkedIn
reports no domain does the exact company-name / alias comparison decide.
Current company name and title are still compared and RECORDED in the
check's source_detail for the reviewer — they no longer decide, because
LinkedIn display names carry taglines and titles are self-described.
Anything less than name + identity is a fail (or no check at all when Floqer
produced no LinkedIn data) — Floqer alone never awards other points.

The comparisons are FIELD-level rather than string-level, which keeps them
exact: a person is compared first name to first name and last name to last
name when both sides carry the split, so a middle name present in one place
is not a mismatch; the company name is compared against the small set of
names that denote the same company (the submitted legal name plus Floqer's
aliases). Everything is `norm_equal` / `domain_of` — case/punctuation only,
no token reordering, no partial or fuzzy matching, no name inferred from
the other.
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


def _company_identity(linkedin: dict, submitted_domain: str, company_names: tuple) -> bool:
    """The domain is the identity. Only a LinkedIn record with NO domain falls back to the exact
    company-name / alias comparison — a present domain that differs is a mismatch, full stop."""
    linkedin_domain = domain_of(linkedin.get("company_domain"))
    if linkedin_domain:
        return bool(submitted_domain) and linkedin_domain == submitted_domain
    return any(norm_equal(linkedin.get("company"), name) for name in company_names)


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
        _company_identity(linkedin, submitted_domain, company_names),
    )
    # Recorded for the reviewer, not decisive (03 §2 as amended).
    recorded = {
        "company_name_matches": any(
            norm_equal(linkedin.get("company"), name) for name in company_names
        ),
        "title_matches": norm_equal(linkedin.get("title"), contact.get("title")),
    }
    detail = {"linkedin": linkedin, "recorded": recorded}
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
