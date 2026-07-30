"""Outbox callback delivery acknowledges by status, never by consuming response bodies."""

import time

import httpx
import pytest

from kyc_tool.config import Settings
from kyc_tool.outbox.publisher import OutboxPublisher


class _SlowBody(httpx.SyncByteStream):
    def __iter__(self):
        for _ in range(20):
            time.sleep(0.03)
            yield b"x"


def test_callback_send_does_not_consume_slow_response_body():
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowBody())

    client = httpx.Client(transport=httpx.MockTransport(_handler), timeout=1.0)
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.05),
        http_client=client,
    )
    request = client.build_request("POST", "http://platform.test/kyc/decision", content=b"{}")

    started = time.monotonic()
    publisher._send_for_status(request)
    assert time.monotonic() - started < 0.05


def test_callback_send_raises_for_non_2xx_without_body_consumption():
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=_SlowBody())

    client = httpx.Client(transport=httpx.MockTransport(_handler), timeout=1.0)
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.05),
        http_client=client,
    )
    request = client.build_request("POST", "http://platform.test/kyc/decision", content=b"{}")

    started = time.monotonic()
    with pytest.raises(httpx.HTTPStatusError):
        publisher._send_for_status(request)
    assert time.monotonic() - started < 0.05
