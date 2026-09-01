"""Tiny local stand-in for the platform's decision-callback endpoint.

scripts/dev.sh starts it so outbox deliveries have somewhere real to land;
every callback (with its HMAC headers) is appended to .substrate/state/callbacks.log
and echoed to stdout.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

LOG = Path(".substrate/state/callbacks.log")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        record = {
            "path": self.path,
            "signature": (self.headers.get("X-KYC-Signature") or "")[:16] + "…",
            "body": json.loads(body or b"{}"),
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"callback ← {record['body'].get('case_id')}: "
              f"{record['body'].get('decision')} (score {record['body'].get('score')})", flush=True)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # quiet access log
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9099
    print(f"dev receiver (fake platform) on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
