"""Re-audit `7d1c435..827bc0f` F3/F6 REDs against a REAL local HTTP socket (not MockTransport):
the governed transport's deadline is total wall-clock time — rate wait + headers + streamed body —
and every governed response is byte-contained (declared length, chunked overflow, gzip expansion),
failing closed as non-retryable. The RIR strategy, previously a direct `client.get` bypass, now
refuses to place even one wire call on an expired budget."""

import gzip
import socketserver
import threading
import time
import tracemalloc
import zlib
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
# Precomputed OUTSIDE any traced window: the big-bomb test measures the CLIENT's decode peak with
# tracemalloc, and building 32 MiB inside the handler thread would pollute that measurement.
_BIG_BOMB = gzip.compress(b"z" * (32 * 1024 * 1024))
_SMALL_BOMB = gzip.compress(b"z" * (4 * _CAP))


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
            elif self.path.startswith("/gzip-bomb-big"):
                # the audit's allocation witness: ~32 KiB wire expanding to 32 MiB decoded — the
                # cap must fire BEFORE the decompressed body is materialized
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(_BIG_BOMB)))
                self.end_headers()
                self.wfile.write(_BIG_BOMB)
            elif self.path.startswith("/gzip-bomb"):
                self.send_response(200)  # tiny wire, 4× the cap decoded
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(_SMALL_BOMB)))
                self.end_headers()
                self.wfile.write(_SMALL_BOMB)
            elif self.path.startswith("/gzip-truncated"):
                # incompressible payload so 40 bytes is genuinely MID-stream, not the whole thing
                body = gzip.compress(bytes(range(256)) * 16)[: 40]
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/deflate-ok"):
                body = zlib.compress(b'{"d": 2}')  # zlib-wrapped deflate (the conforming form)
                self.send_response(200)
                self.send_header("Content-Encoding", "deflate")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/brotli-claimed"):
                self.send_response(200)
                self.send_header("Content-Encoding", "br")  # never negotiated: we pin gzip
                self.send_header("Content-Length", "4")
                self.end_headers()
                self.wfile.write(b"XXXX")
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


def test_gzip_bomb_is_refused_before_allocation_not_after(server_url):
    """R10-F3: iter_bytes() decoded a whole wire chunk before any cap could look at it — the
    audit measured an 81 MB peak for a 64 KB cap. The bounded incremental decoder (zlib
    max_length) must keep peak memory far under the decompressed size. Mutating the transport
    back to iter_bytes() blows this bound and fails here."""
    client = httpx.Client(base_url=server_url, timeout=10.0)
    tracemalloc.start()
    try:
        with budget_scope(_budget(30.0)), pytest.raises(UpstreamResponseTooLarge):
            get_with_retry(client, "/gzip-bomb-big", attempts=1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8_000_000, f"decoder materialized the bomb: {peak} byte peak for a 64 KB cap"


def test_truncated_gzip_fails_as_decoding_error_not_evidence(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)), pytest.raises(httpx.DecodingError):
        get_with_retry(client, "/gzip-truncated", attempts=1)


def test_zlib_wrapped_deflate_decodes_fine(server_url):
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)):
        response = get_with_retry(client, "/deflate-ok", attempts=1)
    assert response.json() == {"d": 2}


def test_unnegotiated_encoding_is_rejected_without_decoding(server_url):
    """We pin Accept-Encoding: gzip; a server claiming br/zstd/multiple encodings is refused
    outright — never fed to a decoder we did not agree to."""
    client = httpx.Client(base_url=server_url, timeout=5.0)
    with budget_scope(_budget(30.0)), pytest.raises(httpx.DecodingError):
        get_with_retry(client, "/brotli-claimed", attempts=1)


# ── R10-F2: the header phase cannot smuggle a result past the deadline ────────────────────────────
class _HeaderDripHandler(socketserver.StreamRequestHandler):
    """Raw TCP: reads the request, then drips the RESPONSE HEADER bytes one at a time — each byte
    beats any inactivity timeout, no body chunk ever runs a deadline check (the audit's witness
    returned 200 after 2.17s under a 0.2s budget)."""

    def handle(self):
        while self.rfile.readline().strip():
            pass  # consume request line + headers
        for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n":
            try:
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.01)


@pytest.fixture(scope="module")
def drip_server_url():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HeaderDripHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_header_drip_with_empty_body_cannot_return_success_past_the_deadline(drip_server_url):
    """The full header takes ~0.4s to drip; the budget is 0.15s. The post-header deadline proof
    must refuse the response — a slow empty-body 200 is not evidence. (Worker OCCUPANCY during
    the drip is inactivity-bound — the honest residual named for the supervised executor.)"""
    client = httpx.Client(base_url=drip_server_url, timeout=5.0)
    started = time.monotonic()
    with budget_scope(_budget(0.15)), pytest.raises(BudgetExhausted):
        get_with_retry(client, "/", attempts=1)
    assert time.monotonic() - started < 3.0  # refused at header completion, no retry pile-up


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
