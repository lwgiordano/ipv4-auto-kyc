"""PR 10b slice 1: the supervised (fork-per-call) executor — the unconditionally-killable
occupancy bound. The in-process transport proves the deadline at header/chunk/EOF boundaries but
cannot bound time BETWEEN bytes; with `hard_kill` the whole physical fetch dies at the absolute
deadline no matter what the peer does. Mock transports bypass (not process-portable); typed
containment errors relay across the process boundary intact."""

import gzip
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from kyc_tool.adapters.executor import process_portable, supervised_fetch
from kyc_tool.adapters.retry import (
    BudgetExhausted,
    RetryBudget,
    UpstreamResponseTooLarge,
    budget_scope,
    get_with_retry,
)

_CAP = 64_000
_SMALL_BOMB = gzip.compress(b"z" * (4 * _CAP))  # tiny wire, 4× the cap decoded


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802 — http.server API
        try:
            if self.path.startswith("/bomb"):
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(_SMALL_BOMB)))
                self.end_headers()
                self.wfile.write(_SMALL_BOMB)
            else:  # /ok — gzip round-trip through the child
                body = gzip.compress(b'{"ok": true}')
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _EndlessDripHandler(socketserver.StreamRequestHandler):
    """Raw TCP: drips response-header bytes at 20ms each with a ~4000-byte padded header line —
    ~80s to complete. Inactivity timeouts never trip; no chunk/EOF boundary is ever reached. Only
    a kill can bound the worker's occupancy."""

    def handle(self):
        while self.rfile.readline().strip():
            pass
        header = b"HTTP/1.1 200 OK\r\nX-Pad: " + b"p" * 4000 + b"\r\nContent-Length: 0\r\n\r\n"
        for byte in header:
            try:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.02)


@pytest.fixture(scope="module")
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def drip_url():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _EndlessDripHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def _hard_budget(remaining: float) -> RetryBudget:
    return RetryBudget(
        deadline_monotonic=time.monotonic() + remaining,
        max_response_bytes=_CAP,
        hard_kill=True,
    )


def test_hard_kill_bounds_occupancy_against_an_endless_header_drip(drip_url):
    """THE claim this executor exists for: the drip needs ~80s to finish its headers and never
    reaches a cooperative checkpoint; the parent terminates the fork at deadline+margin, so the
    WORKER is free in ~2s — occupancy is bounded, not merely the result refused."""
    client = httpx.Client(base_url=drip_url, timeout=30.0)  # inactivity never trips at 20ms/byte
    started = time.monotonic()
    with budget_scope(_hard_budget(0.5)), pytest.raises(BudgetExhausted, match="HARD-KILLED"):
        get_with_retry(client, "/", attempts=1)
    assert time.monotonic() - started < 5.0  # ~0.5s deadline + 1s margin + fork/kill overhead


def test_supervised_success_round_trips_through_the_child(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_hard_budget(10.0)):
        response = get_with_retry(client, "/ok", attempts=1)
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "content-encoding" not in response.headers  # child decoded + stripped, like in-process


def test_containment_errors_relay_typed_across_the_process_boundary(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_hard_budget(10.0)), pytest.raises(UpstreamResponseTooLarge):
        get_with_retry(client, "/bomb", attempts=1)


def test_transport_errors_relay_and_classify(server_url):
    """A child-side connect failure comes back as an httpx.TransportError so the retry loop's
    transient classification behaves exactly like the in-process path."""
    client = httpx.Client(base_url="http://127.0.0.1:9", timeout=0.3)  # nothing listens on 9
    with budget_scope(_hard_budget(5.0)), pytest.raises(httpx.TransportError):
        get_with_retry(client, "/x", attempts=1)


def test_mock_transports_bypass_the_fork():
    calls = []

    def responder(request):
        calls.append(1)
        return httpx.Response(200, content=b"")

    client = httpx.Client(transport=httpx.MockTransport(responder), base_url="https://u.test")
    assert process_portable(client) is False
    with budget_scope(_hard_budget(10.0)):
        response = get_with_retry(client, "/x", attempts=1)
    assert response.status_code == 200 and calls == [1]  # in-process path, no fork


def test_process_portable_predicate(server_url):
    assert process_portable(httpx.Client(base_url=server_url)) is True
    assert process_portable(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    ) is False


def test_supervised_fetch_refuses_a_spent_budget_without_forking(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    spent = RetryBudget(deadline_monotonic=0.0, clock=lambda: 1.0, hard_kill=True)
    with pytest.raises(BudgetExhausted):
        supervised_fetch(client, "/ok", None, spent, None)
