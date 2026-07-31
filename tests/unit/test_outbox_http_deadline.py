"""Outbox callback delivery: the status is the acknowledgement, the body is drained safely.

Two properties have to hold together, and an earlier revision traded one for the other. A
slow-dripping response body must not hold the claim lease (so the read is budgeted), AND the
connection must survive the exchange (so a delivery is not a fresh TCP+TLS handshake, and a
truncated body is still detected). Closing an unconsumed response gave the first and destroyed
the second — invisibly, because `MockTransport` has no socket to observe. The socket-level tests
here are the ones that can see it.
"""

import socket
import threading
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


def _publisher(client: httpx.Client, **settings) -> OutboxPublisher:
    return OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", **settings),
        http_client=client,
    )


def test_slow_response_body_cannot_outlive_the_configured_budget():
    """A receiver that drips its body sets its own pace, not the publisher's.

    The predecessor of this test asserted the body was never read AT ALL (`< 0.05s`). That is a
    stronger statement than the property needs and it cost connection reuse; what actually
    matters is that the drain is bounded, so the claim lease outlives the attempt.
    """
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_SlowBody())

    client = httpx.Client(transport=httpx.MockTransport(_handler), timeout=1.0)
    request = client.build_request("POST", "http://platform.test/kyc/decision", content=b"{}")

    started = time.monotonic()
    _publisher(client, outbox_http_timeout_seconds=0.05)._send_for_status(request)
    elapsed = time.monotonic() - started
    assert elapsed < 0.3, f"drain ran past its budget ({elapsed:.3f}s of a 0.6s body)"


def test_non_2xx_raises_before_the_body_is_drained():
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, stream=_SlowBody())

    client = httpx.Client(transport=httpx.MockTransport(_handler), timeout=1.0)
    request = client.build_request("POST", "http://platform.test/kyc/decision", content=b"{}")

    started = time.monotonic()
    with pytest.raises(httpx.HTTPStatusError):
        _publisher(client, outbox_http_timeout_seconds=0.05)._send_for_status(request)
    assert time.monotonic() - started < 0.05


# --- socket-level: what MockTransport cannot show ---------------------------------------------


class _Receiver:
    """A minimal keep-alive HTTP/1.1 server that counts ACCEPTED CONNECTIONS.

    Connection count is the observable that matters: with keep-alive intact, N sequential
    deliveries from one publisher share one connection.
    """

    def __init__(self, body: bytes = b'{"ok":true}', truncate: bool = False):
        self.body, self.truncate, self.connections = body, truncate, 0
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}/kyc/decision"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(5)
        try:
            while True:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    head += chunk
                length = next(
                    (int(line.split(b":")[1]) for line in head.split(b"\r\n")
                     if line.lower().startswith(b"content-length:")), 0
                )
                read = len(head.split(b"\r\n\r\n", 1)[1])
                while read < length:
                    read += len(conn.recv(65536))
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(self.body))
                if self.truncate:  # promised a body, closes without sending it
                    conn.close()
                    return
                conn.sendall(self.body)
        except OSError:
            return

    def close(self):
        self._sock.close()


def _deliver(receiver: _Receiver, times: int) -> None:
    client = httpx.Client(timeout=httpx.Timeout(5.0))
    publisher = _publisher(client, outbox_http_timeout_seconds=5.0)
    try:
        for _ in range(times):
            publisher._send_for_status(
                client.build_request("POST", receiver.url, content=b'{"case_id":"c1"}')
            )
    finally:
        client.close()


def test_sequential_deliveries_reuse_one_connection():
    """Four callbacks, one connection.

    Against the production `https` callback URL, a dropped connection is a full TCP+TLS
    handshake per delivery — and the receiver takes a broken pipe writing the response body it
    was never allowed to finish. Measured at 4 connections for 4 deliveries before the drain
    was restored.
    """
    receiver = _Receiver()
    try:
        _deliver(receiver, 4)
        assert receiver.connections == 1, (
            f"{receiver.connections} connections for 4 deliveries — keep-alive is gone; an "
            "unconsumed response cannot be pipelined, so httpcore discards the socket"
        )
    finally:
        receiver.close()


def test_truncated_2xx_is_a_failure_not_a_delivery():
    """`200` + `Content-Length: 11` + close, no body.

    A gateway that answers before its origin commits produces exactly this. Accepting it stamps
    a terminal witness — `delivered`, `published_at`, run COMPLETE — for bytes nobody kept, and
    nothing retries. Draining raises, so the row retries and the platform dedupes.
    """
    receiver = _Receiver(truncate=True)
    try:
        with pytest.raises(httpx.RemoteProtocolError):
            _deliver(receiver, 1)
    finally:
        receiver.close()


class _HeaderDripReceiver:
    """Sends the status line one byte at a time with gaps UNDER the inactivity timeout —
    the attack no per-operation timeout can stop, because each byte resets the budget."""

    def __init__(self, interval: float = 0.02):
        self.interval = interval
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}/kyc/decision"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._drip, args=(conn,), daemon=True).start()

    def _drip(self, conn):
        conn.settimeout(10)
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                head += conn.recv(65536)
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n" * 100:
                time.sleep(self.interval)
                conn.sendall(bytes([byte]))
        except OSError:
            return

    def close(self):
        self._sock.close()


def test_header_drip_is_cancelled_at_the_attempt_deadline_and_the_publisher_recovers():
    """A receiver dribbling header bytes under the inactivity timeout held a bare send 32x past
    the nominal envelope — stalling the single-threaded publisher and outliving the claim lease.
    The enforced deadline must cancel the attempt as a retryable failure, and the NEXT delivery
    must succeed on the rebuilt client."""
    from kyc_tool.outbox.publisher import _AttemptDeadlineExceeded

    drip = _HeaderDripReceiver()
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.1),
    )  # enforced deadline: 4 x 0.1 = 0.4s
    try:
        request = publisher.http.build_request("POST", drip.url, content=b"{}")
        started = time.monotonic()
        with pytest.raises(_AttemptDeadlineExceeded):
            publisher._send_for_status(request)
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, f"cancellation took {elapsed:.2f}s — the deadline is not enforced"
    finally:
        drip.close()

    healthy = _Receiver()
    try:  # the stuck client was replaced; delivery works again without a new publisher
        publisher._send_for_status(
            publisher.http.build_request("POST", healthy.url, content=b"{}")
        )
        assert healthy.connections == 1
    finally:
        healthy.close()
        publisher.http.close()
