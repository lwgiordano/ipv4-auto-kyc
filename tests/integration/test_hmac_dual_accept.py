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


def test_cross_case_redirect_lifecycle(settings, session_factory, policy, clean_db):
    """The headline proof (spec §1/§7). The cross-case redirect:
    (a) a captured v1-only event replays to another case BEFORE the inbound
        sunset — the documented residual risk of dual-accept, not a regression;
    (b) a v2 signature captured for case A is REJECTED when replayed to case B;
    (c) after the inbound sunset, the v1-only replay is rejected.
    The hole is closed for v2 immediately, and for everyone only at (c)."""
    pre = settings.model_copy(
        update={
            "platform_hmac_secret": TEST_SECRET,
            "hmac_inbound_key_id": "kyc-platform-1",
            "hmac_inbound_secret": TEST_SECRET,
            "hmac_v1_inbound_sunset_at": "2999-01-01T00:00:00Z",
            "hmac_v1_observation_window_days": 14,
        }
    )
    c = _client(pre, session_factory, policy)
    body = _body()

    # (a) v1-only, pre-sunset: the SAME captured signature works against case B
    h = sign_headers(body, key="captured-key")
    assert c.post("/v1/cases/case-a/events", content=body, headers=h).status_code == 202
    assert c.post("/v1/cases/case-b/events", content=body, headers=h).status_code == 202

    # (b) v2 captured for case-a, replayed to case-b → 401 (path-bound)
    v2 = sign_headers_v2(body, method="POST", path_qs="/v1/cases/case-a/events", key="v2-key")
    assert c.post("/v1/cases/case-b/events", content=body, headers=v2).status_code == 401

    # (c) v1-only AFTER the inbound sunset → 401
    post = settings.model_copy(
        update={"platform_hmac_secret": TEST_SECRET, "hmac_v1_inbound_sunset_at": "2000-01-01T00:00:00Z"}
    )
    c2 = _client(post, session_factory, policy)
    replay = sign_headers(body, key="captured-key-2")
    assert c2.post("/v1/cases/case-a/events", content=body, headers=replay).status_code == 401


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
