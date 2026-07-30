"""Outbox callback HTTP budget is a wall-clock budget, not an HTTPX read inactivity timeout."""

import time

import httpx
import pytest

from kyc_tool.config import Settings
from kyc_tool.outbox.publisher import OutboxPublisher


class _SlowDrip(httpx.SyncByteStream):
    def __iter__(self):
        for _ in range(5):
            time.sleep(0.03)
            yield b"x"


def test_callback_send_enforces_total_deadline():
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowDrip())

    client = httpx.Client(transport=httpx.MockTransport(_handler), timeout=1.0)
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.05),
        http_client=client,
    )
    request = client.build_request("POST", "http://platform.test/kyc/decision", content=b"{}")

    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException, match="total HTTP budget"):
        publisher._send_with_total_deadline(request)
    assert time.monotonic() - started < 0.15
