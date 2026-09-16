"""LinkedIn deterministic match: ALL four fields or no points."""

import pytest

from kyc_tool.domain.models import CheckStatus
from kyc_tool.validators.linkedin import linkedin_intent

SNAPSHOT = {
    "company_legal_name": "Acme Networks Ltd",
    "website": "https://acme.example",
    "contact": {"name": "Jane Doe", "title": "Director"},
}

LINKEDIN_FULL = {
    "person_name": "Jane Doe",
    "company": "ACME NETWORKS LTD",
    "title": "Director",
    "company_domain": "acme.example",
}


def test_full_deterministic_match_passes():
    intent = linkedin_intent({"linkedin": LINKEDIN_FULL}, SNAPSHOT)
    assert intent.status is CheckStatus.PASS


def test_no_linkedin_data_produces_no_check():
    assert linkedin_intent({"linkedin": {}}, SNAPSHOT) is None
    assert linkedin_intent({}, SNAPSHOT) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("person_name", "John Doe"),
        ("company", "Acme Holdings Ltd"),
        ("title", "Engineer"),
        ("company_domain", "other.example"),
    ],
)
def test_any_single_field_mismatch_fails(field, value):
    intent = linkedin_intent({"linkedin": {**LINKEDIN_FULL, field: value}}, SNAPSHOT)
    assert intent.status is CheckStatus.FAIL


SPLIT = {**LINKEDIN_FULL, "first_name": "Jane", "last_name": "Doe"}


def test_split_names_match_across_a_middle_initial_the_full_name_would_have_failed_on():
    """The platform holds the full legal name, LinkedIn shows the everyday one. Field-to-field is
    still EXACT — it only stops the middle initial being compared against nothing."""
    snapshot = {**SNAPSHOT, "contact": {"name": "Jane A. Doe", "title": "Director",
                                        "first_name": "Jane", "last_name": "Doe"}}
    assert linkedin_intent({"linkedin": LINKEDIN_FULL}, snapshot).status is CheckStatus.FAIL
    assert linkedin_intent({"linkedin": SPLIT}, snapshot).status is CheckStatus.PASS


def test_split_names_that_disagree_still_fail():
    snapshot = {**SNAPSHOT, "contact": {"name": "Jane Doe", "title": "Director",
                                        "first_name": "Jane", "last_name": "Roe"}}
    assert linkedin_intent({"linkedin": SPLIT}, snapshot).status is CheckStatus.FAIL


def test_a_one_sided_split_falls_back_to_the_unchanged_full_name_comparison():
    """Halves are never compared against a whole: with only LinkedIn carrying the split, the
    person_name-vs-contact.name path is exactly what it was."""
    assert linkedin_intent({"linkedin": SPLIT}, SNAPSHOT).status is CheckStatus.PASS
    mismatch = linkedin_intent({"linkedin": {**SPLIT, "person_name": "John Doe"}}, SNAPSHOT)
    assert mismatch.status is CheckStatus.FAIL


def test_the_company_matches_any_discovered_alias_of_the_submitted_name():
    """LinkedIn shows the trading brand where the platform submitted the legal name. The
    comparison stays exact after normalization — it just runs against each candidate."""
    brand = {**LINKEDIN_FULL, "company": "Acme Networks"}
    assert linkedin_intent({"linkedin": brand}, SNAPSHOT).status is CheckStatus.FAIL
    with_alias = linkedin_intent(
        {"linkedin": brand, "aliases": ["Acme Group", "acme networks"]}, SNAPSHOT
    )
    assert with_alias.status is CheckStatus.PASS
    off_list = linkedin_intent(
        {"linkedin": {**brand, "company": "Acme Holdings"}, "aliases": ["Acme Group"]}, SNAPSHOT
    )
    assert off_list.status is CheckStatus.FAIL


def test_source_detail_carries_the_profile_provenance_only_when_reported():
    intent = linkedin_intent(
        {"linkedin": LINKEDIN_FULL, "linkedin_source": "web_search", "web_verified": True},
        SNAPSHOT,
    )
    assert intent.source_detail == {"linkedin": LINKEDIN_FULL, "linkedin_source": "web_search",
                                    "web_verified": True}
    bare = linkedin_intent({"linkedin": LINKEDIN_FULL}, SNAPSHOT)
    assert bare.source_detail == {"linkedin": LINKEDIN_FULL}
