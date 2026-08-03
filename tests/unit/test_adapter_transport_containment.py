"""Re-audit `7d1c435..827bc0f` F3/F6 REDs against a REAL local HTTP socket (not MockTransport):
the governed transport's deadline is total wall-clock time — rate wait + headers + streamed body —
and every governed response is byte-contained (declared length, chunked overflow, gzip expansion),
failing closed as non-retryable. The RIR strategy, previously a direct `client.get` bypass, now
refuses to place even one wire call on an expired budget."""

import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from kyc_tool.adapters.retry import (
    BudgetExhausted,
    RetryBudget,
    UpstreamResponseTooLarge,
    budget_scope,
    get_with_retry,
)

_CAP = 64_000  # governed byte cap for these tests


class _Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):  # noqa: N802 — http.server API
        _Handler.hits[self.path] = _Handler.hits.get(self.path, 0) + 1
        try:
            if self.path.startswith("/drip"):
                # headers immediately, then 1 byte per 40ms: each chunk beats any inactivity
                # timeout, so ONLY an absolute wall-clock deadline can stop the read. The declared
                # length sits UNDER the byte cap so the preflight passes and time is the decider.
                self.send_response(200)
                self.send_header("Content-Length", "50000")
                self.end_headers()
                for _ in range(50000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.04)
            elif self.path.startswith("/declared-huge"):
                self.send_response(200)
                self.send_header("Content-Length", str(16 * 1024 * 1024))
                self.end_headers()
                self.wfile.write(b"x" * 4096)  # starts sending; the client must not read it out
            elif self.path.startswith("/chunked-huge"):
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                blob = b"y" * 8192
                for _ in range(64):  # 512 KiB total, no Content-Length to preflight
                    self.wfile.write(f"{len(blob):x}\r\n".encode() + blob + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            elif self.path.startswith("/gzip-bomb"):
                body = gzip.compress(b"z" * (4 * _CAP))  # tiny wire, 4× the cap decoded
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/big-503"):
                self.send_response(503)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                blob = b"e" * 8192
                for _ in range(64):
                    self.wfile.write(f"{len(blob):x}\r\n".encode() + blob + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            else:  # /ok — gzip round-trip sanity
                body = gzip.compress(b'{"a": 1}')
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the governed client aborts refused bodies mid-write — expected


@pytest.fixture(scope="module")
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def _budget(remaining: float, cap: int | None = _CAP) -> RetryBudget:
    return RetryBudget(
        deadline_monotonic=time.monotonic() + remaining, max_response_bytes=cap
    )


def test_drip_fed_body_cannot_outlive_the_absolute_deadline(server_url):
    """The audit's witness: bytes arriving under the inactivity timeout kept a 0.12s budget busy
    for the full body. The absolute per-chunk deadline check must abort within the budget (plus
    one chunk of slack), not after 100000 × 40ms."""
    client = httpx.Client(base_url=server_url, timeout=5.0)
    started = time.monotonic()
    with budget_scope(_budget(0.3)), pytest.raises(BudgetExhausted):
        get_with_retry(client, "/drip", attempts=1)
    assert time.monotonic() - started < 2.0  # aborted at ~0.3s, not the 4000s full-body time


def test_declared_oversized_content_length_refuses_before_reading(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    started = time.monotonic()
    with budget_scope(_budget(30.0)), pytest.raises(UpstreamResponseTooLarge):
        get_with_retry(client, "/declared-huge", attempts=3)
    assert time.monotonic() - started < 2.0  # preflight refusal — the 16 MiB body was never read
    assert _Handler.hits["/declared-huge"] == 1  # non-retryable: exactly one physical call


def test_chunked_overflow_without_length_fails_closed_mid_stream(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)), pytest.raises(UpstreamResponseTooLarge):
        get_with_retry(client, "/chunked-huge", attempts=3)
    assert _Handler.hits["/chunked-huge"] == 1


def test_gzip_expansion_bomb_trips_the_decoded_cap(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)), pytest.raises(UpstreamResponseTooLarge):
        get_with_retry(client, "/gzip-bomb", attempts=3)
    assert _Handler.hits["/gzip-bomb"] == 1  # wire bytes were under the cap; DECODED bytes tripped


def test_oversized_5xx_fails_closed_with_one_call(server_url):
    """A huge error body must not be buffered for the transient classifier — containment first."""
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)), pytest.raises(UpstreamResponseTooLarge):
        get_with_retry(client, "/big-503", attempts=3)
    assert _Handler.hits["/big-503"] == 1


def test_contained_response_round_trips_for_the_caller(server_url):
    """The reconstructed (decoded, capped) response still serves the adapter contract: status,
    json(), and no stale Content-Encoding header that would double-decode."""
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)):
        response = get_with_retry(client, "/ok", attempts=1)
    assert response.status_code == 200
    assert response.json() == {"a": 1}
    assert "content-encoding" not in response.headers


def test_expired_budget_means_zero_rir_wire_calls(server_url):
    """The RIR strategy previously bypassed the governed helper entirely; now an already-expired
    budget refuses before the FIRST wire call — the audit's `RetryBudget(deadline=0, clock=1)`
    witness placed one call, this places none."""
    from kyc_tool.adapters.rir_rdap.base import RdapStrategy

    strategy = RdapStrategy(client=httpx.Client(base_url=server_url, timeout=5.0))
    strategy.entity_path = "/rir/{handle}"
    expired = RetryBudget(deadline_monotonic=0.0, clock=lambda: 1.0, max_response_bytes=_CAP)
    with budget_scope(expired), pytest.raises(BudgetExhausted):
        strategy.lookup_org("ORG-1")
    assert "/rir/ORG-1" not in _Handler.hits  # ZERO physical calls on a spent budget
