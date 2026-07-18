"""Outbound callback signing (PR 5a §3): decision callbacks dual-emit v1 + v2
until the outbound sunset, then v2 only. v2 binds the outbound direction + path
so the platform can migrate without a coordinated flip."""

import json

import httpx
import pytest

from kyc_tool import security
from kyc_tool.outbox.publisher import OutboxPublisher
from tests.conftest import TEST_SECRET

pytestmark = pytest.mark.postgres

PAYLOAD = {"case_id": "c-1", "decision": "approve", "run_id": "r-1"}


def _deliver(session_factory, settings):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200)

    pub = OutboxPublisher(
        session_factory, settings, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    pub._deliver_decision_callback(PAYLOAD)
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


def test_callback_v1_dropped_after_outbound_sunset(session_factory, settings):
    s = _outbound_settings(settings, hmac_v1_outbound_sunset_at="2000-01-01T00:00:00Z")
    headers = _deliver(session_factory, s)["headers"]
    assert "x-kyc-signature-v2" in headers  # v2 always
    assert "x-kyc-signature" not in headers  # v1 retired outbound
