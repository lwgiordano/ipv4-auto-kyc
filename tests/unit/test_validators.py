"""Pure-validator unit tests (07 §unit: pass/fail/needs_review per pass rule)."""

from datetime import UTC, datetime, timedelta

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.documents import document_intent
from kyc_tool.validators.email import email_intents
from kyc_tool.validators.normalize import domain_of, norm, norm_equal
from kyc_tool.validators.poc import hash_token, poc_token_intent
from kyc_tool.validators.registry import registry_intent
from kyc_tool.validators.website import website_intent

ACME = {
    "company_legal_name": "Acme Networks Ltd",
    "address": "1 Main Street, London, EC1A 1AA",
    "registration_number": "12345678",
    "website": "https://acme.example",
}


# --- normalize -------------------------------------------------------------


def test_norm_case_and_punctuation_only():
    assert norm("ACME Networks, Ltd.") == "acme networks ltd"
    assert norm_equal("ACME NETWORKS LTD", "Acme Networks Ltd.")
    # NOT fuzzy: abbreviation expansion must not match
    assert not norm_equal("Acme Networks Limited", "Acme Networks Ltd")


def test_domain_of_variants():
    assert domain_of("https://acme.example/about") == "acme.example"
    assert domain_of("ops@acme.example") == "acme.example"
    assert domain_of("ACME.EXAMPLE") == "acme.example"


# --- email (AUDIT:D5) ------------------------------------------------------


def _email_norm(email: str, verified: bool = True) -> dict:
    return {"verified": verified, "email": email, "domain": domain_of(email)}


def test_company_email_yields_both_passes():
    intents = {i.check_type: i for i in email_intents(_email_norm("ops@acme.example"), ACME)}
    assert intents["verified_email"].status is CheckStatus.PASS
    assert intents["verified_company_email"].status is CheckStatus.PASS


def test_free_inbox_passes_only_any_email():
    intents = {i.check_type: i for i in email_intents(_email_norm("bob@gmail.com"), ACME)}
    assert intents["verified_email"].status is CheckStatus.PASS
    assert intents["verified_company_email"].status is CheckStatus.FAIL
    assert (
        ReasonCode.EMAIL_FREE_OR_DISPOSABLE_DOMAIN.value
        in intents["verified_company_email"].reason_codes
    )


def test_wrong_business_domain_fails_company_check():
    intents = {i.check_type: i for i in email_intents(_email_norm("ops@other.example"), ACME)}
    assert intents["verified_company_email"].status is CheckStatus.FAIL
    assert ReasonCode.EMAIL_DOMAIN_MISMATCH.value in intents["verified_company_email"].reason_codes


def test_unverified_email_fails():
    intents = email_intents(_email_norm("ops@acme.example", verified=False), ACME)
    assert [i.status for i in intents] == [CheckStatus.FAIL]


# --- registry ---------------------------------------------------------------

CH_OK = {
    "candidates": [
        {
            "legal_name": "ACME NETWORKS LTD",
            "company_number": "12345678",
            "status": "active",
            "address": "1 Main Street, London, EC1A 1AA",
        }
    ]
}


def test_registry_exact_match_passes():
    intent = registry_intent({"companies_house": CH_OK}, ACME)
    assert intent.status is CheckStatus.PASS
    assert intent.source == "companies_house"


def test_registry_inactive_company_fails():
    inactive = {"candidates": [{**CH_OK["candidates"][0], "status": "dissolved"}]}
    intent = registry_intent({"companies_house": inactive}, ACME)
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.REGISTRY_COMPANY_INACTIVE.value in intent.reason_codes


def test_registry_name_mismatch_fails_not_fuzzy():
    other = {"candidates": [{**CH_OK["candidates"][0], "legal_name": "ACME NETWORK LTD"}]}
    intent = registry_intent({"companies_house": other}, ACME)
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.REGISTRY_NAME_MISMATCH.value in intent.reason_codes


def test_registry_number_mismatch_fails():
    other = {"candidates": [{**CH_OK["candidates"][0], "company_number": "999"}]}
    intent = registry_intent({"companies_house": other}, ACME)
    assert ReasonCode.REGISTRY_NUMBER_MISMATCH.value in intent.reason_codes


def test_registry_no_adapters_yields_nothing():
    assert registry_intent({}, ACME) is None


def test_registry_gleif_fallback_passes():
    gleif = {
        "candidates": [
            {
                "legal_name": "Acme Networks Ltd",
                "company_number": "12345678",
                "status": "ACTIVE",
                "address": "1 Main Street, London, EC1A 1AA",
            }
        ]
    }
    intent = registry_intent({"gleif": gleif}, ACME)
    assert intent.status is CheckStatus.PASS
    assert intent.source == "gleif"


# --- documents ---------------------------------------------------------------


def test_document_match_passes():
    normalized = {
        "extracted": {
            "name": "ACME NETWORKS LTD",
            "address": "1 Main Street, London, EC1A 1AA",
            "number": "12345678",
            "jurisdiction": "GB",
        }
    }
    intent = document_intent(normalized, {**ACME, "jurisdiction": "GB"}, ())
    assert intent.status is CheckStatus.PASS


def test_document_field_mismatch_fails():
    normalized = {"extracted": {"name": "Different Co", "number": "12345678"}}
    intent = document_intent(normalized, ACME, ())
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.DOCUMENT_FIELDS_MISMATCH.value in intent.reason_codes


def test_document_unreadable_fails():
    intent = document_intent({"extracted": {}}, ACME, ())
    assert ReasonCode.DOCUMENT_UNREADABLE.value in intent.reason_codes


def test_document_name_only_never_passes():
    # PR 3 fail-closed: a document showing only a matching name (no address/
    # number/jurisdiction) can never award legal proof — it routes to review
    intent = document_intent({"extracted": {"name": "ACME NETWORKS LTD"}}, ACME, ())
    assert intent.status is CheckStatus.NEEDS_REVIEW
    assert ReasonCode.DOCUMENT_EVIDENCE_INCOMPLETE.value in intent.reason_codes


def test_document_conflicting_with_registry_stamps_hard_conflict():
    # PR 3 item 4: a document number contradicting the live registry record
    # fails AND stamps HARD_CONFLICT so gate 5 fails
    from kyc_tool.domain.models import CheckView

    registry = CheckView(
        check_type="official_registry_match",
        status=CheckStatus.PASS,
        points_awarded=25,
        category="legal_business_proof",
        source="companies_house",
        reason_codes=(),
        source_detail={"company_number": "12345678"},
    )
    normalized = {
        "extracted": {
            "name": "ACME NETWORKS LTD",
            "address": "1 Main Street, London, EC1A 1AA",
            "number": "99999999",  # contradicts the registry's 12345678
            "jurisdiction": "GB",
        }
    }
    intent = document_intent(normalized, {**ACME, "jurisdiction": "GB"}, (registry,))
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.DOCUMENT_REGISTRY_CONFLICT.value in intent.reason_codes
    assert ReasonCode.HARD_CONFLICT.value in intent.reason_codes


# --- website (human verdict) --------------------------------------------------


def test_website_reviewer_verdict_maps_to_check():
    passed = website_intent({"result": "pass", "reviewer_id": "rev-1", "task_id": "t1"})
    assert passed.status is CheckStatus.PASS
    assert passed.source == "reviewer:rev-1"
    failed = website_intent(
        {"result": "fail", "reviewer_id": "rev-1", "task_id": "t1", "reason_codes": ["website_parked"]}
    )
    assert failed.status is CheckStatus.FAIL
    assert failed.reason_codes == ("website_parked",)


# --- poc token ------------------------------------------------------------------


def _token_row(token: str, *, expired: bool = False) -> dict:
    return {
        "id": "tok-1",
        "token_hash": hash_token(token),
        "expired_at": datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
        "verified_at": None,
    }


SNAPSHOT_POC = {"poc": {"poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"}}


def test_poc_valid_token_passes_with_association_detail():
    intent = poc_token_intent(
        {"token": "secret-token"}, {"poc_tokens": [_token_row("secret-token")]}, SNAPSHOT_POC
    )
    assert intent.status is CheckStatus.PASS
    assert intent.source_detail["org_handle"] == "ORG-ACME-1"


def test_poc_wrong_token_fails():
    intent = poc_token_intent(
        {"token": "wrong"}, {"poc_tokens": [_token_row("secret-token")]}, SNAPSHOT_POC
    )
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_INVALID.value in intent.reason_codes


def test_poc_expired_token_fails():
    intent = poc_token_intent(
        {"token": "secret-token"},
        {"poc_tokens": [_token_row("secret-token", expired=True)]},
        SNAPSHOT_POC,
    )
    assert ReasonCode.POC_TOKEN_EXPIRED.value in intent.reason_codes


def test_poc_missing_raw_token_needs_review():
    intent = poc_token_intent({"token_id": "tok-1"}, {"poc_tokens": []}, SNAPSHOT_POC)
    assert intent.status is CheckStatus.NEEDS_REVIEW


def test_poc_without_association_target_needs_review():
    # PR 3 fail-closed: a verified token with no ORG-ID/resource to vouch for
    # can't award control proof — a human decides
    snap = {"poc": {"poc_handle": "JD123-ARIN"}}  # no org_handle, no resource
    intent = poc_token_intent(
        {"token": "secret-token"}, {"poc_tokens": [_token_row("secret-token")]}, snap
    )
    assert intent.status is CheckStatus.NEEDS_REVIEW
    assert ReasonCode.POC_NO_ASSOCIATION_TARGET.value in intent.reason_codes


# --- email domain-forgery (PR 3) ---------------------------------------------


def test_email_payload_domain_conflict_fails_both_checks():
    # attacker@gmail.com + domain=company.example: the declared domain
    # contradicts the address, so NEITHER check may pass
    normalized = {"verified": True, "email": "attacker@gmail.com", "domain": "company.example"}
    intents = {
        i.check_type: i
        for i in email_intents(normalized, {**ACME, "website": "https://company.example"})
    }
    assert intents["verified_email"].status is CheckStatus.FAIL
    assert intents["verified_company_email"].status is CheckStatus.FAIL
    assert (
        ReasonCode.EMAIL_PAYLOAD_DOMAIN_CONFLICT.value
        in intents["verified_email"].reason_codes
    )
