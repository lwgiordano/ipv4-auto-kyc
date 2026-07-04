"""ORG-ID validator: pass rule, fail reasons, and the five needs_review routes."""

import pytest

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.org_id import org_id_intent

SNAPSHOT = {
    "company_legal_name": "Acme Networks Ltd",
    "address": "1 Main Street, London, EC1A 1AA",
    "org_id": {"rir": "arin", "org_handle": "ORG-ACME-1"},
}

RDAP_OK = {
    "found": True,
    "rir": "arin",
    "org_handle": "ORG-ACME-1",
    "entity_name": "ACME NETWORKS LTD",
    "address": "1 Main Street, London, EC1A 1AA",
    "address_missing_or_stale": False,
}


def test_exact_match_passes_with_org_handle_detail():
    intent = org_id_intent(RDAP_OK, SNAPSHOT)
    assert intent.status is CheckStatus.PASS
    assert intent.source_detail["org_handle"] == "ORG-ACME-1"  # cascade key
    assert intent.reason_codes == ()


def test_handle_not_found_fails():
    intent = org_id_intent({"found": False, "org_handle": "ORG-ACME-1"}, SNAPSHOT)
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.ORG_ID_HANDLE_NOT_FOUND.value in intent.reason_codes


def test_name_mismatch_fails_not_fuzzy():
    intent = org_id_intent({**RDAP_OK, "entity_name": "Acme Network Ltd"}, SNAPSHOT)
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.ORG_ID_NAME_MISMATCH.value in intent.reason_codes


def test_address_material_match_via_postcode():
    intent = org_id_intent({**RDAP_OK, "address": "Acme House, EC1A 1AA, GB"}, SNAPSHOT)
    assert intent.status is CheckStatus.PASS


def test_address_material_match_via_containment():
    intent = org_id_intent({**RDAP_OK, "address": "1 Main Street, London"}, SNAPSHOT)
    assert intent.status is CheckStatus.PASS


def test_address_mismatch_fails():
    intent = org_id_intent({**RDAP_OK, "address": "99 Other Road, Berlin, 10115"}, SNAPSHOT)
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.ORG_ID_ADDRESS_MISMATCH.value in intent.reason_codes


@pytest.mark.parametrize(
    "flag,reason",
    [
        ("parent_subsidiary_ambiguity", ReasonCode.ORG_ID_PARENT_SUBSIDIARY_AMBIGUITY),
        ("address_missing_or_stale", ReasonCode.ORG_ID_ADDRESS_MISSING_OR_STALE),
        ("related_entity_only", ReasonCode.ORG_ID_RELATED_ENTITY_ONLY),
        ("resources_held_by_provider", ReasonCode.ORG_ID_RESOURCES_HELD_BY_PROVIDER),
        ("via_broad_search", ReasonCode.ORG_ID_BROAD_NAME_SEARCH_ONLY),
    ],
)
def test_needs_review_routing_awards_nothing(flag, reason):
    """The catalog's five routing rules: human review, never silent points."""
    intent = org_id_intent({**RDAP_OK, flag: True}, SNAPSHOT)
    assert intent.status is CheckStatus.NEEDS_REVIEW
    assert reason.value in intent.reason_codes


def test_conflicting_entity_passes_but_stamps_hard_conflict():
    """G6 wiring: the conflict reason makes gate 5 fail while the check itself
    reflects the deterministic pass."""
    intent = org_id_intent({**RDAP_OK, "conflicting_entity": True}, SNAPSHOT)
    assert intent.status is CheckStatus.PASS
    assert ReasonCode.HARD_CONFLICT.value in intent.reason_codes
