#!/usr/bin/env python3
"""Front the local dev stack so UI edits appear without restarting anything.

`scripts/dev.sh` runs the real thing -- Postgres, migrations, the worker, the API -- and that
API reads `console.html` once, at import. So editing the console, or pulling a branch that
changed it, does nothing until the whole stack comes down and back up, which costs the database
and everything in it.

This sits in front of that API and changes exactly two things:

  GET /ui          served from disk, read fresh every time, with a reloader spliced in
  GET /__ui_build  the current hash of that file

Everything else -- every API call, every POST, the real engine behind them -- is proxied through
untouched. So the console is live and the backend is real at the same time.

    python3 scripts/devproxy.py                       # front 127.0.0.1:8080 on 8099
    python3 scripts/devproxy.py --api ... --port ...
    python3 scripts/devproxy.py --seed                # post the demo walk once it is up
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import socketserver
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "src" / "kyc_tool" / "ui" / "console.html"

# Same origin as the console, so the poll is an ordinary request. Two seconds is under the time
# it takes to notice a stale screen and over the time a git pull needs to finish writing.
RELOADER = """
<script>
/* Injected by scripts/devproxy.py. The API caches console.html at import; this page asks the
   proxy for the file's current hash instead, and reloads when it moves. The hash fragment
   survives a reload, so you come back to the same company. */
(function(){var mine=%(build)s,miss=0,timer=0;
 timer=setInterval(function(){var x=new XMLHttpRequest();
   x.open("GET","/__ui_build?t="+Date.now(),true);
   x.onload=function(){miss=0;var v=x.responseText.trim();if(v&&v!==mine)location.reload()};
   x.onerror=function(){if(++miss>20)clearInterval(timer)};
   x.send()},2000)})();
</script>
"""

# The preview and this render the same screens. Each window says which one it is, in the title
# the Dock reads and in the sidebar, so nobody presses Send Message in the one that cannot send.
# Spliced in here, never in console.html: the deployed console carries neither.
MARK = """<style>
  /* In the sidebar footer, in the flow. A fixed corner badge sits on top of the refresh
     control that already lives there. */
  .runmark{display:flex;align-items:center;gap:var(--s2);
    font:var(--t-label);letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
  .runmark b{color:var(--text)}
</style>
<script>
(function(){
  var TAG="Full stack", LINE="<b>Full stack<\/b> temporary database";
  /* The router retitles the page per company, so the tag is reapplied on every set rather than
     written once into <title>. It goes first because the Dock and the window frame truncate
     from the right. */
  var t=Object.getOwnPropertyDescriptor(Document.prototype,"title");
  Object.defineProperty(document,"title",{configurable:true,
    get:function(){return t.get.call(document)},
    set:function(v){v=String(v);t.set.call(document,v.indexOf(TAG+" \u00b7 ")===0?v:TAG+" \u00b7 "+v)}});
  document.title=document.title;
  function place(){var f=document.querySelector(".side-foot");
    if(!f||f.querySelector(".runmark"))return !!f;
    var el=document.createElement("div");el.className="runmark";el.setAttribute("role","note");
    el.innerHTML=LINE;f.appendChild(el);return true}
  if(!place())document.addEventListener("DOMContentLoaded",place);
})();
</script>
"""

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
              "content-length"}


def console_html() -> tuple[bytes, str]:
    src = CONSOLE.read_text(encoding="utf-8")
    build = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
    at = src.rindex("</body>")
    page = src[:at] + MARK + (RELOADER % {"build": json.dumps(build)}) + src[at:]
    return page.encode("utf-8"), build


class Handler(http.server.BaseHTTPRequestHandler):
    api = "http://127.0.0.1:8080"
    protocol_version = "HTTP/1.1"

    def _raw(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(self.api + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in HOP_BY_HOP and k.lower() != "host":
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload, status, headers = r.read(), r.status, r.headers
        except urllib.error.HTTPError as e:          # 4xx/5xx are answers, not failures
            payload, status, headers = e.read(), e.code, e.headers
        except Exception as e:                        # the stack is still coming up, or gone
            self._raw(json.dumps({"detail": f"dev proxy: the API did not answer ({e})"}).encode(),
                      "application/json", 502)
            return
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/__ui_build":
            self._raw(console_html()[1].encode(), "text/plain; charset=utf-8")
        elif path in ("/ui", "/ui/"):
            self._raw(console_html()[0], "text/html; charset=utf-8")
        elif path == "/":
            self.send_response(302)
            self.send_header("Location", "/ui")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._proxy()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET  # every write goes to the real engine

    def log_message(self, *_a) -> None:
        pass


def seed(api: str) -> None:
    """The demo walk, so the console is never staring at an empty list."""
    def post(case: str, event: str, **over) -> str:
        t = json.load(urllib.request.urlopen(f"{api}/ui/api/event-templates"))["templates"]
        p = dict(t[event])
        p.update(over)
        p["occurred_at"] = datetime.now(UTC).isoformat()
        req = urllib.request.Request(
            f"{api}/ui/api/send-event",
            data=json.dumps({"case_id": case, "event_type": event, "payload": p}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return str(r.status)
        except Exception as e:
            return f"skipped ({e})"

    import time
    for event in ("kyb.run_requested", "email.verified", "org_id.submitted"):
        print(f"  seed demo-case-001 {event}: {post('demo-case-001', event)}")
        time.sleep(4)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8080")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--seed", action="store_true", help="post the demo walk on start")
    a = ap.parse_args()

    if not CONSOLE.exists():
        print(f"missing {CONSOLE} -- run this from a checkout", file=sys.stderr)
        return 2
    Handler.api = a.api.rstrip("/")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", a.port), Handler) as httpd:
        print(f"dev proxy on http://127.0.0.1:{a.port}/ui  ->  {Handler.api}")
        print(f"  the console is read from {CONSOLE.relative_to(ROOT)} on every request")
        if a.seed:
            import threading
            threading.Thread(target=seed, args=(Handler.api,), daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
