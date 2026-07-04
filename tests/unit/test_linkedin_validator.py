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
