"""HMAC dual-accept verifier (PR 5a §2/§3): v2 is sticky (any v2 header ⇒
v2-only, no v1 fallback); v1 is accepted only before the inbound sunset."""

import json

import pytest
from fastapi.testclient import TestClient

from kyc_tool.api.app import create_app
from tests.conftest import TEST_SECRET, envelope, sign_headers, sign_headers_v2

pytestmark = pytest.mark.postgres

KYB = {"company_legal_name": "Acme Networks Ltd", "jurisdiction": "GB"}


def _client(settings, session_factory, policy):
    return TestClient(create_app(settings, session_factory=session_factory, policy=policy))


def _body():
    return json.dumps(envelope("kyb.run_requested", KYB)).encode()


def test_v2_header_present_but_invalid_never_falls_back_to_v1(
    dual_accept_settings, session_factory, policy, clean_db
):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _body()
    headers = sign_headers(body)  # a VALID v1 signature ...
    headers["X-KYC-Signature-V2"] = "deadbeef"  # ... plus a bogus v2 assertion
    headers["X-KYC-Key-Id"] = "kyc-platform-1"
    r = c.post("/v1/cases/acme/events", content=body, headers=headers)
    assert r.status_code == 401  # v2 asserted ⇒ v2-only, no v1 fallback


def test_valid_v2_authenticates_and_binds_path(dual_accept_settings, session_factory, policy, clean_db):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _body()
    good = sign_headers_v2(body, method="POST", path_qs="/v1/cases/acme/events")
    assert c.post("/v1/cases/acme/events", content=body, headers=good).status_code == 202
    # same signature replayed to a different case path → 401 (path-bound)
    assert c.post("/v1/cases/evil/events", content=body, headers=good).status_code == 401


def test_unknown_key_id_rejected(dual_accept_settings, session_factory, policy, clean_db):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _body()
    bad = sign_headers_v2(body, method="POST", path_qs="/v1/cases/acme/events", key_id="nope")
    assert c.post("/v1/cases/acme/events", content=body, headers=bad).status_code == 401


def test_v1_only_accepted_before_inbound_sunset(dual_accept_settings, session_factory, policy, clean_db):
    c = _client(dual_accept_settings, session_factory, policy)
    body = _body()
    assert c.post("/v1/cases/acme/events", content=body, headers=sign_headers(body)).status_code == 202


def test_v1_rejected_after_inbound_sunset(settings, session_factory, policy, clean_db):
    past = settings.model_copy(
        update={
            "platform_hmac_secret": TEST_SECRET,
            "hmac_v1_inbound_sunset_at": "2000-01-01T00:00:00Z",
        }
    )
    c = _client(past, session_factory, policy)
    body = _body()
    assert c.post("/v1/cases/acme/events", content=body, headers=sign_headers(body)).status_code == 401
