"""POC token binding + single-use (remediation item 5).

A token proves exactly one identity — (case, token_id, digest, rir, poc_handle,
org/resource) — and exactly once. These pure-validator tests pin the binding
mismatches and consumption guard the tightened `poc_token_intent` enforces; the
DB-level single-use stamp is exercised in tests/integration/test_identity_invalidation.py.
"""

from datetime import UTC, datetime, timedelta

from kyc_tool.domain.models import CheckStatus
from kyc_tool.domain.reasons import ReasonCode
from kyc_tool.validators.poc import hash_token, poc_token_intent

TOKEN = "secret-token"


def _token_row(**overrides) -> dict:
    row = {
        "id": "tok-1",
        "token_hash": hash_token(TOKEN),
        "expired_at": datetime.now(UTC) + timedelta(hours=1),
        "verified_at": None,
        "consumed_at": None,
        "poc_handle": "JD123-ARIN",
        "rir": "arin",
        "org_handle": "ORG-ACME-1",
        "resource": None,
    }
    row.update(overrides)
    return row


def _event(token_id: str = "tok-1") -> dict:
    return {"token_id": token_id, "token_digest": hash_token(TOKEN)}


def _snapshot(**poc) -> dict:
    base = {"poc_handle": "JD123-ARIN", "rir": "arin", "org_handle": "ORG-ACME-1"}
    base.update(poc)
    return {"poc": base}


def _intent(event: dict, row: dict, snapshot: dict):
    return poc_token_intent(event, {"poc_tokens": [row]}, snapshot)


def test_token_bound_to_different_org_fails():
    # token minted for ORG-ACME-1; the POC now claims ORG-OTHER-9 → mismatch
    intent = _intent(_event(), _token_row(), _snapshot(org_handle="ORG-OTHER-9"))
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_BINDING_MISMATCH.value in intent.reason_codes


def test_token_bound_to_different_poc_handle_fails():
    intent = _intent(_event(), _token_row(poc_handle="ZZ999-ARIN"), _snapshot())
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_BINDING_MISMATCH.value in intent.reason_codes


def test_token_minted_for_different_rir_fails():
    intent = _intent(_event(), _token_row(rir="ripe"), _snapshot(rir="arin"))
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_BINDING_MISMATCH.value in intent.reason_codes


def test_org_handle_case_and_format_differences_still_bind():
    # canonicalization: "org-acme-1" and "ORG-ACME-1" are the same identity
    intent = _intent(_event(), _token_row(org_handle="org-acme-1"), _snapshot(org_handle="ORG-ACME-1"))
    assert intent.status is CheckStatus.PASS


def test_resource_bound_token_matches_current_resource():
    # a POC that vouches for a resource (no org) binds to that resource
    row = _token_row(org_handle=None, resource="192.0.2.0/24")
    snap = _snapshot(org_handle=None, resource="192.0.2.0/24")
    assert _intent(_event(), row, snap).status is CheckStatus.PASS


def test_resource_bound_token_rejects_changed_resource():
    row = _token_row(org_handle=None, resource="192.0.2.0/24")
    snap = _snapshot(org_handle=None, resource="198.51.100.0/24")
    intent = _intent(_event(), row, snap)
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_BINDING_MISMATCH.value in intent.reason_codes


def test_consumed_token_cannot_verify_twice():
    intent = _intent(_event(), _token_row(consumed_at=datetime.now(UTC)), _snapshot())
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_CONSUMED.value in intent.reason_codes


def test_already_verified_token_is_single_use():
    # legacy/defensive: verified_at set but consumed_at null (pre-009 rows) is
    # still spent — the guard checks both stamps
    intent = _intent(_event(), _token_row(verified_at=datetime.now(UTC)), _snapshot())
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_CONSUMED.value in intent.reason_codes


def test_token_id_must_match_the_presented_id():
    # right digest, wrong id (an attacker replays a digest against another row)
    intent = _intent(_event(token_id="tok-OTHER"), _token_row(), _snapshot())
    assert intent.status is CheckStatus.FAIL
    assert ReasonCode.POC_TOKEN_INVALID.value in intent.reason_codes
