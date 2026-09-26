"""LinkedIn deterministic match: person name AND company identity (the domain) or no points;
company name and title are recorded for the reviewer, not compared (03 §2 as amended)."""

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


def test_a_name_mismatch_fails():
    intent = linkedin_intent({"linkedin": {**LINKEDIN_FULL, "person_name": "John Doe"}}, SNAPSHOT)
    assert intent.status is CheckStatus.FAIL


def test_company_name_and_title_are_recorded_not_decisive():
    """The first live run: LinkedIn said `Epsilon Telecommunications, a KT company` and
    `Director, Digital Strategy & Business Development` for a submission of the bare legal name
    and `Director of Product`. Same person, same domain — that is the company."""
    tagline = {**LINKEDIN_FULL, "company": "Acme Networks Ltd, a KT company", "title": "Engineer"}
    intent = linkedin_intent({"linkedin": tagline}, SNAPSHOT)
    assert intent.status is CheckStatus.PASS
    assert intent.source_detail["recorded"] == {
        "company_name_matches": False, "title_matches": False
    }
    exact = linkedin_intent({"linkedin": LINKEDIN_FULL}, SNAPSHOT)
    assert exact.source_detail["recorded"] == {"company_name_matches": True, "title_matches": True}


def test_a_present_but_different_domain_never_falls_back_to_the_name():
    other = {**LINKEDIN_FULL, "company_domain": "other.example"}  # name is still exact
    assert linkedin_intent({"linkedin": other}, SNAPSHOT).status is CheckStatus.FAIL


def test_without_a_submitted_website_the_domain_cannot_match():
    snapshot = {k: v for k, v in SNAPSHOT.items() if k != "website"}
    assert linkedin_intent({"linkedin": LINKEDIN_FULL}, snapshot).status is CheckStatus.FAIL


def test_with_no_domain_on_either_side_the_exact_name_is_the_only_identity_left():
    """The widening the amendment buys, recorded deliberately: the fallback is conditioned on
    LinkedIn reporting no domain, not on the submission carrying one."""
    snapshot = {k: v for k, v in SNAPSHOT.items() if k != "website"}
    no_domain = {**LINKEDIN_FULL, "company_domain": ""}
    assert linkedin_intent({"linkedin": no_domain}, snapshot).status is CheckStatus.PASS
    off = {**no_domain, "company": "Acme Holdings"}
    assert linkedin_intent({"linkedin": off}, snapshot).status is CheckStatus.FAIL


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
    """Only when LinkedIn reports no company domain does the name decide. LinkedIn then shows
    the trading brand where the platform submitted the legal name; the comparison stays exact
    after normalization — it just runs against each candidate."""
    brand = {**LINKEDIN_FULL, "company": "Acme Networks", "company_domain": ""}
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
    recorded = {"company_name_matches": True, "title_matches": True}
    assert intent.source_detail == {"linkedin": LINKEDIN_FULL, "recorded": recorded,
                                    "linkedin_source": "web_search", "web_verified": True}
    bare = linkedin_intent({"linkedin": LINKEDIN_FULL}, SNAPSHOT)
    assert bare.source_detail == {"linkedin": LINKEDIN_FULL, "recorded": recorded}
