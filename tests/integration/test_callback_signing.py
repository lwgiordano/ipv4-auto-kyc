"""Outbound callback signing (PR 5a §3): decision callbacks dual-emit v1 + v2
until the outbound sunset, then v2 only. v2 binds the outbound direction + path
so the platform can migrate without a coordinated flip."""

import json

import httpx
import pytest

from kyc_tool import security
from kyc_tool.config import ProcessRole
from kyc_tool.outbox.publisher import OutboxPublisher
from tests.conftest import TEST_SECRET, process_context

pytestmark = pytest.mark.postgres

PAYLOAD = {"case_id": "c-1", "decision": "approve", "run_id": "r-1"}


def _deliver(session_factory, settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200)

    pub = OutboxPublisher(
        session_factory, settings, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    , process_role=process_context(ProcessRole.OUTBOX_WORKER))
    # Build-and-send only: this suite is about what goes on the wire, so it deliberately does NOT
    # go through `_deliver_decision_callback`, which additionally commits an attempt row and
    # therefore needs a real claimed outbox row. The attempt authority is proven in
    # tests/integration/test_outbox_attempts.py.
    request, _ = pub._build_callback_request(PAYLOAD)
    pub.http.send(request).raise_for_status()
    return captured


def _outbound_settings(settings, **over):
    return settings.model_copy(
        update={
            "platform_hmac_secret": TEST_SECRET,
            "platform_callback_url": "https://platform.test",
            "hmac_outbound_key_id": "kyc-tool-1",
            "hmac_outbound_secret": "outbound-secret-value",
            **over,
        }
    )


def test_callback_dual_emits_and_v2_binds_direction_and_path(session_factory, settings):
    s = _outbound_settings(settings, hmac_v1_outbound_sunset_at="2999-01-01T00:00:00Z")
    h = _deliver(session_factory, s)
    headers, body = h["headers"], h["body"]

    assert "x-kyc-signature" in headers  # v1 still emitted (pre-sunset)
    assert "x-kyc-signature-v2" in headers
    assert headers["x-kyc-key-id"] == "kyc-tool-1"
    assert security.verify_v2(
        s.hmac_outbound_secret,
        headers["x-kyc-signature-v2"],
        max_skew_seconds=300,
        key_id="kyc-tool-1",
        direction=security.DIRECTION_OUTBOUND,
        method="POST",
        path_qs="/kyc/decision",
        timestamp=headers["x-kyc-timestamp"],
        slot="",
        body=body,
    )
    assert body == json.dumps(PAYLOAD).encode()


def test_callback_signs_literal_prefixed_path(session_factory, settings):
    """Audit finding 1: a callback base with a path prefix POSTs to
    /hooks/kyc/decision; the v2 signature must bind THAT literal target, not a
    hard-coded /kyc/decision (else a conforming receiver rejects every v2
    callback)."""
    s = _outbound_settings(
        settings,
        platform_callback_url="https://platform.test/hooks",
        hmac_v1_outbound_sunset_at="2999-01-01T00:00:00Z",
    )
    h = _deliver(session_factory, s)
    headers, body = h["headers"], h["body"]

    assert h["url"] == "https://platform.test/hooks/kyc/decision"

    def _verifies(path_qs: str) -> bool:
        return security.verify_v2(
            s.hmac_outbound_secret,
            headers["x-kyc-signature-v2"],
            max_skew_seconds=300,
            key_id="kyc-tool-1",
            direction=security.DIRECTION_OUTBOUND,
            method="POST",
            path_qs=path_qs,
            timestamp=headers["x-kyc-timestamp"],
            slot="",
            body=body,
        )

    assert _verifies("/hooks/kyc/decision")  # the ACTUAL request target
    assert not _verifies("/kyc/decision")  # the old hard-coded path no longer matches


@pytest.mark.parametrize(
    ("base", "expected_wire_path"),
    [
        ("https://platform.test/café", "/caf%C3%A9/kyc/decision"),  # non-ASCII → %-encoded
        ("https://platform.test/a/../hooks", "/hooks/kyc/decision"),  # dot-segments stripped
        ("https://platform.test/hooks", "/hooks/kyc/decision"),  # plain ASCII prefix
    ],
)
def test_callback_signs_the_httpx_wire_path(session_factory, settings, base, expected_wire_path):
    """Audit re-finding 1: httpx normalizes the URL (percent-encoding non-ASCII,
    stripping dot-segments) before it sends, so the v2 signature must bind the
    literal wire path — a pre-normalized string disagrees with what's received."""
    s = _outbound_settings(
        settings, platform_callback_url=base, hmac_v1_outbound_sunset_at="2999-01-01T00:00:00Z"
    )
    h = _deliver(session_factory, s)
    headers, body = h["headers"], h["body"]

    wire_path = httpx.URL(h["url"]).raw_path.decode("ascii")
    assert wire_path == expected_wire_path  # what httpx actually put on the wire

    assert security.verify_v2(
        s.hmac_outbound_secret,
        headers["x-kyc-signature-v2"],
        max_skew_seconds=300,
        key_id="kyc-tool-1",
        direction=security.DIRECTION_OUTBOUND,
        method="POST",
        path_qs=wire_path,
        timestamp=headers["x-kyc-timestamp"],
        slot="",
        body=body,
    )


def test_callback_v1_dropped_after_outbound_sunset(session_factory, settings):
    s = _outbound_settings(settings, hmac_v1_outbound_sunset_at="2000-01-01T00:00:00Z")
    headers = _deliver(session_factory, s)["headers"]
    assert "x-kyc-signature-v2" in headers  # v2 always
    assert "x-kyc-signature" not in headers  # v1 retired outbound
