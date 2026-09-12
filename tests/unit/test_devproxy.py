"""Real local HTTP regression checks; no application database or live writes."""

import http.server
import json
import runpy
import socket
import threading
from contextlib import ExitStack
from pathlib import Path

import pytest


@pytest.fixture
def proxy():
    handler = runpy.run_path(str(Path(__file__).parents[2] / "scripts/devproxy.py"))["Handler"]
    received = []

    class Upstream(http.server.BaseHTTPRequestHandler):
        def do_PUT(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((dict(self.headers), len(body)))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    with ExitStack() as stack:
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        handler.api = f"http://127.0.0.1:{upstream.server_port}"
        front = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        for server in (upstream, front):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
        yield front.server_port, received


def request(proxy, headers, body=b"", path="/ui/api/configuration/points"):
    port, _ = proxy
    wire = (
        f"PUT {path} HTTP/1.1\r\nHost: localhost:{port}\r\n"
        f"Origin: http://localhost:{port}\r\nConnection: close\r\n{headers}\r\n\r\n"
    ).encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(wire)
        conn.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(conn)
        response.begin()
        return response.status, response.read()


def test_browser_host_and_origin_reach_upstream_unchanged(proxy):
    assert request(proxy, "Content-Length: 2", b"{}")[0] == 200
    headers, length = proxy[1][0]
    assert headers["Host"] == f"localhost:{proxy[0]}"
    assert headers["Origin"] == f"http://localhost:{proxy[0]}"
    assert length == 2


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ("Content-Length: 33554433", b"", 413),
        ("Content-Length: -1", b"", 400),
        ("Content-Length: nope", b"", 400),
        ("Content-Length: +2", b"{}", 400),
        ("Content-Length: 2\r\nContent-Length: 2", b"{}", 400),
        ("Content-Length: 2, 2", b"{}", 400),
        ("Transfer-Encoding: chunked", b"0\r\n\r\n", 400),
        ("Transfer-Encoding: identity\r\nContent-Length: 2", b"{}", 400),
        ("Content-Length: 10", b"{}", 400),
    ],
)
def test_bad_configuration_framing_is_refused_without_forwarding(proxy, headers, body, status):
    actual, payload = request(proxy, headers, body, path="/ui/api/configuration/brokers?test=1")
    assert actual == status
    assert json.loads(payload)["detail"]
    assert proxy[1] == []


def test_configuration_limit_includes_exactly_32_mib(proxy):
    assert request(proxy, "Content-Length: 33554432", b" " * 33554432)[0] == 200
    assert proxy[1][0][1] == 33554432


@pytest.mark.parametrize("path", ["/ui/api/configuration/points", "/ui/api/%63onfiguration/points"])
def test_oversized_configuration_refuses_before_body_and_closes_connection(proxy, path):
    with socket.create_connection(("127.0.0.1", proxy[0]), timeout=2) as conn:
        conn.sendall((f"PUT {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 33554433\r\n\r\n").encode())
        response = http.client.HTTPResponse(conn)
        response.begin()
        assert response.status == 413
        response.read()
        assert conn.recv(1) == b""
    assert proxy[1] == []
