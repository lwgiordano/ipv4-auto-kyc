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

from kyc_tool.config import ProcessRole, Settings
from kyc_tool.outbox.publisher import OutboxPublisher
from tests.conftest import process_context


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
    process_role=process_context(ProcessRole.OUTBOX_WORKER))


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
def test_header_drip_returns_within_the_bound_and_the_publisher_keeps_working():
    """A receiver dribbling header bytes under the inactivity timeout held a bare send 32x past
    the nominal envelope. The publisher must RETURN within the bound (detaching the attempt as a
    retryable failure), and — sharing one client — the NEXT delivery still succeeds."""
    from kyc_tool.outbox.publisher import _AttemptDeadlineExceeded

    drip = _HeaderDripReceiver()
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.1),
    process_role=process_context(ProcessRole.OUTBOX_WORKER))  # bound: 4 x 0.1 = 0.4s
    try:
        request = publisher.http.build_request("POST", drip.url, content=b"{}")
        started = time.monotonic()
        with pytest.raises(_AttemptDeadlineExceeded):
            publisher._send_for_status(request)
        elapsed = time.monotonic() - started
        # the return is bounded by the queue timeout ALONE — no synchronous close()/join eating
        # into the lease (re-audit `8377440` F4). Generous ceiling for CI scheduling jitter.
        assert elapsed < 1.0, f"return took {elapsed:.2f}s — cleanup is not off the hot path"
    finally:
        drip.close()

    healthy = _Receiver()
    try:  # the shared client still delivers; no rebuild needed
        publisher._send_for_status(
            publisher.http.build_request("POST", healthy.url, content=b"{}")
        )
        assert healthy.connections == 1
    finally:
        healthy.close()
        publisher.close()


class _WedgedTransport(httpx.BaseTransport):
    """A transport stuck in something `client.close()` cannot interrupt — the model of a hung
    OS call (e.g. a resolver that never returns). Releasing `gate` un-wedges every attempt."""

    def __init__(self, gate: threading.Event):
        self.gate = gate

    def handle_request(self, request):
        self.gate.wait()
        return httpx.Response(200)


def test_return_bound_holds_even_when_close_would_block():
    """The specific F4 trigger: a client whose close() blocks forever. The deadline path must
    NOT call it synchronously, so the publisher still returns within the bound."""
    from kyc_tool.outbox.publisher import _AttemptDeadlineExceeded

    gate = threading.Event()

    class _UncloseableClient(httpx.Client):
        def close(self):
            gate.wait()  # close() itself wedges — the exact audited hang

    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.05),
        http_client=_UncloseableClient(transport=_WedgedTransport(gate)),
    process_role=process_context(ProcessRole.OUTBOX_WORKER))
    try:
        request = publisher.http.build_request("POST", "http://platform.test/x", content=b"{}")
        started = time.monotonic()
        with pytest.raises(_AttemptDeadlineExceeded):
            publisher._send_for_status(request)
        assert time.monotonic() - started < 1.0, "a blocking close() must not be on the hot path"
    finally:
        gate.set()


def test_capacity_is_a_hard_cap_that_stops_new_claims_not_a_log_line():
    """Re-audit `8377440` F5: wedged sends must not accrete past the cap. Beyond it the publisher
    reports at_capacity and process_once refuses to claim (returns False) — resources bounded,
    delivery stalled (safe), not one thread/connection per stuck send forever."""
    from kyc_tool.outbox.publisher import _MAX_ORPHAN_SENDS, _AttemptDeadlineExceeded

    gate = threading.Event()
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.02),
        http_client=httpx.Client(transport=_WedgedTransport(gate)),
    process_role=process_context(ProcessRole.OUTBOX_WORKER))
    try:
        # drive well PAST the cap; orphan count must saturate AT the cap, never exceed it
        for _ in range(_MAX_ORPHAN_SENDS + 5):
            if publisher.at_capacity():
                break
            request = publisher.http.build_request("POST", "http://platform.test/x", content=b"{}")
            with pytest.raises(_AttemptDeadlineExceeded):
                publisher._send_for_status(request)
        assert publisher.at_capacity()
        assert len(publisher._orphans) == _MAX_ORPHAN_SENDS, (
            f"orphans={len(publisher._orphans)} — the cap is not enforced as a ceiling"
        )
        # process_once must now REFUSE to claim (circuit breaker), reporting idle
        assert publisher.process_once() is False
        assert publisher._saturated
    finally:
        gate.set()
    publisher.close()


def test_close_is_bounded_not_linear_in_orphan_count():
    """close() waits ONE total budget for detached sends, not 0.5s each (re-audit `8377440` F5)."""
    from kyc_tool.outbox.publisher import _ORPHAN_DRAIN_SECONDS, _AttemptDeadlineExceeded

    gate = threading.Event()
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.02),
        http_client=httpx.Client(transport=_WedgedTransport(gate)),
    process_role=process_context(ProcessRole.OUTBOX_WORKER))
    try:
        while not publisher.at_capacity():
            request = publisher.http.build_request("POST", "http://platform.test/x", content=b"{}")
            with pytest.raises(_AttemptDeadlineExceeded):
                publisher._send_for_status(request)
    finally:
        started = time.monotonic()
        publisher.close()  # threads still wedged: bounded by the TOTAL drain budget, not N x it
        assert time.monotonic() - started < _ORPHAN_DRAIN_SECONDS + 1.0
        gate.set()


def test_close_is_bounded_even_when_http_close_blocks_forever():
    """Re-audit `538e55e..42e1c7d` F5: close()'s OWN self.http.close() must run inside the same
    total deadline. A client whose close() hangs otherwise wedges shutdown even at ZERO orphans —
    the exact 'CLOSE_ENTER, no return' the audit measured."""
    from kyc_tool.outbox.publisher import _ORPHAN_DRAIN_SECONDS

    gate = threading.Event()

    class _UncloseableClient(httpx.Client):
        def close(self):
            gate.wait()  # http.close() itself never returns

    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.05),
        http_client=_UncloseableClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    process_role=process_context(ProcessRole.OUTBOX_WORKER))
    try:
        assert publisher._orphans == []  # ZERO orphans — Codex's exact case
        started = time.monotonic()
        publisher.close()  # must return despite http.close() blocking forever
        assert time.monotonic() - started < _ORPHAN_DRAIN_SECONDS + 1.0, (
            "close() waited on a forever-blocking http.close() instead of bounding it"
        )
    finally:
        gate.set()


def test_run_forever_exits_on_saturation_for_supervised_restart():
    """Re-audit `538e55e..42e1c7d` F6: at the orphan cap, run_forever must become EXTERNALLY
    observable — raise OutboxSaturated so the worker exits nonzero and a supervisor restarts it —
    not sleep forever like an empty queue (which stalled delivery invisibly)."""
    from kyc_tool.outbox.publisher import (
        OutboxSaturated,
        _AttemptDeadlineExceeded,
    )

    gate = threading.Event()
    publisher = OutboxPublisher(
        lambda: None,
        Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.02),
        http_client=httpx.Client(transport=_WedgedTransport(gate)),
    process_role=process_context(ProcessRole.OUTBOX_WORKER))
    try:
        while not publisher.at_capacity():  # wedge sends until the cap is reached
            request = publisher.http.build_request("POST", "http://platform.test/x", content=b"{}")
            with pytest.raises(_AttemptDeadlineExceeded):
                publisher._send_for_status(request)
        # saturated: run_forever raises (does NOT sleep forever) before claiming anything new
        with pytest.raises(OutboxSaturated):
            publisher.run_forever(poll_seconds=0)
    finally:
        gate.set()
    publisher.close()


def test_daemon_send_threads_never_block_process_exit():
    """A publisher whose send is wedged in an uninterruptible call must not hang interpreter
    shutdown — the audit measured a child process unable to exit within two seconds."""
    import subprocess
    import sys as _sys

    script = """
import threading, httpx, sys
from kyc_tool.config import Settings
from kyc_tool.outbox.publisher import OutboxPublisher, _AttemptDeadlineExceeded
from kyc_tool.config import ProcessRole, validate_process_role

class Wedged(httpx.BaseTransport):
    def handle_request(self, request):
        threading.Event().wait()  # forever

pub = OutboxPublisher(lambda: None,
    Settings(platform_callback_url="http://platform.test", outbox_http_timeout_seconds=0.05),
    http_client=httpx.Client(transport=Wedged()),
    process_role=validate_process_role(Settings(), ProcessRole.OUTBOX_WORKER))
try:
    pub._send_for_status(pub.http.build_request("POST", "http://platform.test/x", content=b"{}"))
except _AttemptDeadlineExceeded:
    pass
print("DETACHED-OK", flush=True)
sys.exit(0)
"""
    proc = subprocess.run([_sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0 and "DETACHED-OK" in proc.stdout, proc.stdout + proc.stderr
