"""The conformance kit against the real app and the real publisher.

`send` runs in-process over the TestClient transport (the kit takes its HTTP client as an
argument for exactly this), and the receiver is checked against bytes and headers the production
signing path produced — not a re-implementation of it.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kyc_tool import conformance
from kyc_tool.api.app import create_app
from kyc_tool.config import ProcessRole
from kyc_tool.outbox.publisher import OutboxPublisher
from tests.callback_bodies import valid_callback_body
from tests.conftest import TEST_SECRET, process_context

pytestmark = pytest.mark.postgres

OUTBOUND_KEY_ID = "kyc-tool-1"
OUTBOUND_SECRET = "conformance-outbound-test-value"  # a test value, like the rest of this suite


def _failed(rows) -> set[str]:
    return {row.check for row in rows if not row.ok}


def test_send_passes_every_documented_check_against_the_real_app(
    dual_accept_settings, session_factory, policy, clean_db
):
    client = TestClient(create_app(dual_accept_settings, session_factory=session_factory, policy=policy))
    rows = conformance.send(
        client, "conformance-case-1",
        v1_secret=TEST_SECRET, inbound_secret=TEST_SECRET, inbound_key_id="kyc-platform-1",
    )
    assert [(row.check, row.expected, row.got) for row in rows if not row.ok] == []
    assert {row.check for row in rows} == (
        {f"walk:{name}" for name, _, _ in conformance.walk()}
        | {"negative:wrong_signature", "negative:stale_timestamp",
           "negative:signup_missing_contact", "negative:unknown_event_type",
           "v1:accepted", "replay:first", "replay:status", "replay:body",
           "read:status", "read:keys"})


def _signed_callback(session_factory, settings, body=None):
    """(path, headers, body) as the OUTBOX PUBLISHER signs them — dual-emitting v1 and v2, with a
    path-prefixed callback base so the literal-path binding is exercised."""
    settings = settings.model_copy(update={
        "platform_hmac_secret": TEST_SECRET,
        "platform_callback_url": "https://platform.test/hooks",
        "hmac_outbound_key_id": OUTBOUND_KEY_ID,
        "hmac_outbound_secret": OUTBOUND_SECRET,
        "hmac_v1_outbound_sunset_at": "2999-01-01T00:00:00Z",
    })
    publisher = OutboxPublisher(
        session_factory, settings,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        process_role=process_context(ProcessRole.OUTBOX_WORKER, settings),
    )
    request, _ = publisher._build_callback_request(body or valid_callback_body())
    return request.url.raw_path.decode("ascii"), request.headers, request.content


def _verify(path, headers, body, seen=None):
    return conformance.verify_callback(
        path, headers, body, outbound_secret=OUTBOUND_SECRET, outbound_key_id=OUTBOUND_KEY_ID,
        v1_secret=TEST_SECRET, seen=set() if seen is None else seen,
    )


def test_receiver_accepts_a_real_publisher_callback_and_says_when_it_is_a_duplicate(
    session_factory, settings
):
    path, headers, body = _signed_callback(session_factory, settings)
    assert path == "/hooks/kyc/decision"  # the literal target the v2 signature binds
    seen = set()

    first = _verify(path, headers, body, seen)
    assert _failed(first) == set()
    assert {row.check: row.got for row in first}["dedupe"] == "first delivery"

    again = _verify(path, headers, body, seen)
    assert _failed(again) == set()
    assert "duplicate" in {row.check: row.got for row in again}["dedupe"]


def test_receiver_fails_the_named_rule_for_each_broken_callback(session_factory, settings):
    path, headers, body = _signed_callback(session_factory, settings)

    tampered = httpx.Headers(headers)
    tampered["X-KYC-Signature-V2"] = "0" * 64
    assert _failed(_verify(path, tampered, body)) == {"signature.v2"}

    assert _failed(_verify("/kyc/decision", headers, body)) == {"signature.v2"}  # wrong path

    no_key_id = httpx.Headers(headers)
    del no_key_id["X-KYC-Key-Id"]
    assert _failed(_verify(path, no_key_id, body)) == {"signature.key_id", "signature.v2"}

    without_run_id = json.dumps(
        {k: v for k, v in json.loads(body).items() if k != "run_id"}).encode()
    assert "body.required_keys" in _failed(_verify(path, headers, without_run_id))


def test_receiver_rejects_an_undocumented_decision_and_gate_set(session_factory, settings):
    path, headers, body = _signed_callback(
        session_factory, settings, body=valid_callback_body(decision="approve"))
    payload = json.loads(body)
    payload["decision"] = "approve_everything"
    payload["gates"].pop("broker_ok")
    broken = json.dumps(payload).encode()
    failed = _failed(_verify(path, headers, broken))
    assert {"body.decision", "body.gates"} <= failed
