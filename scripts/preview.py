#!/usr/bin/env python3
"""Build and serve the ops console on captured sample data, with no stack behind it.

`scripts/dev.sh` is the real thing: Postgres, migrations, a worker, the API. This is the other
end of the range -- one HTML file, the standard library, and a JSON fixture -- for looking at
the console's design without standing anything up.

    python3 scripts/preview.py            # build, serve, open a browser, reload on change
    python3 scripts/preview.py --build-only OUT.html
    python3 scripts/preview.py --no-open --port 7788

Serving rather than opening a file:// URL is what makes the reload work. The page polls a build
id; when the id changes it reloads itself, so a `git pull` here becomes a repaint there without
anyone touching the window.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "src" / "kyc_tool" / "ui" / "console.html"
FIXTURE = Path(__file__).resolve().parent / "preview_data.json"
LANDING = "#/case/demo-case-001"

# The console's own fetch, answered from the fixture. Every non-GET is refused in one place, so
# the preview can never look like it sent something to a platform that is not there.
SHIM = """
/* Read-only preview: the console's own screens on a captured sample, with nothing to send to.
   Served by scripts/preview.py -- run it again, or leave it running, to pick up source edits. */
const __DATA=%(data)s;
const __BUILD=%(build)s;
window.fetch=async(url,opts)=>{
  const u=String(url),method=((opts&&opts.method)||"GET").toUpperCase();
  const path=u.split("?")[0],q=new URLSearchParams(u.split("?")[1]||"");
  const json=(status,body)=>new Response(JSON.stringify(body),
    {status,headers:{"Content-Type":"application/json"}});
  if(path==="/__build")return new Response(__BUILD);
  if(method!=="GET")return json(422,
    {detail:"This is a read-only preview on sample data: nothing can be sent from here."});
  if(path==="/ui/api/overview")return json(200,__DATA.overview);
  if(path==="/ui/api/cases"){const s=(q.get("q")||"").toLowerCase();
    return json(200,{cases:__DATA.cases.cases.filter(c=>!s
      ||c.id.toLowerCase().includes(s)
      ||(c.company_name||"").toLowerCase().includes(s))})}
  const m=path.match(/^\\/ui\\/api\\/cases\\/([^/]+)\\/full$/);
  if(m){const d=__DATA.full[decodeURIComponent(m[1])];
    return d?json(200,d):json(404,{detail:"case not found"})}
  if(path==="/ui/api/policy")return json(200,__DATA.policy);
  if(path==="/ui/api/integrations")return json(200,__DATA.integrations);
  if(path==="/ui/api/event-templates")return json(200,__DATA.templates);
  return json(404,{detail:"not in this preview"});
};
/* The preview opens on a company rather than the overview: the company page is the screen the
   design work has been about. */
if(!location.hash)location.hash=%(landing)s;
/* Reload when the file behind this page changes. XHR rather than the shimmed fetch, and it keeps
   the scroll position, so an edit here repaints there without losing your place. */
(function(){if(location.protocol==="file:")return;
  let miss=0,timer=0;
  timer=setInterval(()=>{const x=new XMLHttpRequest();
    x.open("GET","/__build?t="+Date.now(),true);
    x.onload=()=>{miss=0;const v=x.responseText.trim();if(v&&v!==__BUILD)location.reload()};
    /* The server is gone -- someone closed the app. Stop asking rather than log forever. */
    x.onerror=()=>{if(++miss>20)clearInterval(timer)};
    x.send()},2000)})();
"""


# A preview and the real stack render the same screens, which is how someone ends up pressing
# Send Message here and wondering why nothing sent. So the window says which one it is, in the
# title the Dock reads and in the sidebar. Spliced in here, never in console.html: the deployed
# console carries neither.
# A raw string: the JS needs <\/b> so the literal cannot close the <script> around it,
# and \/ is not a Python escape -- unraw, it is a SyntaxWarning on every import.
MARK = r"""<style>
  /* In the sidebar footer, in the flow. A fixed corner badge sits on top of the refresh
     control that already lives there. */
  .runmark{display:flex;align-items:center;gap:var(--s2);
    font:var(--t-label);letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
  .runmark b{color:var(--text)}
</style>
<script>
(function(){
  var TAG="Preview", LINE="<b>Preview<\/b> sample data, nothing sends";
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

def build(landing: str = LANDING) -> tuple[str, str]:
    """The console, with the shim spliced in at the top of its own script block."""
    src = CONSOLE.read_text(encoding="utf-8")
    data = FIXTURE.read_text(encoding="utf-8")
    build_id = hashlib.sha256((src + data).encode("utf-8")).hexdigest()[:16]
    shim = SHIM % {
        "data": json.dumps(json.loads(data), separators=(",", ":")),
        "build": json.dumps(build_id),
        "landing": json.dumps(landing),
    }
    at = src.index("<script>", src.index("</style>")) + len("<script>")
    page = src[:at] + shim + src[at:]
    return page.replace("</body>", MARK + "</body>", 1), build_id


def _git_pull() -> str | None:
    """Best effort. A preview that cannot reach the remote still shows the working tree."""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only", "--quiet"],
                           capture_output=True, text=True, timeout=60)
        return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200]
    except Exception as exc:  # git missing, no network, detached head
        return str(exc)[:200]


class _Handler(http.server.BaseHTTPRequestHandler):
    page = b""
    build_id = ""

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
        if self.path.startswith("/__build"):
            self._send(type(self).build_id.encode(), "text/plain; charset=utf-8")
        else:
            self._send(type(self).page, "text/html; charset=utf-8")

    def log_message(self, *_a) -> None:  # one line per rebuild is enough output
        pass


def serve(port: int, open_browser: bool, pull: bool, interval: float) -> int:
    page, build_id = build()
    _Handler.page, _Handler.build_id = page.encode("utf-8"), build_id

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with _Server(("127.0.0.1", port), _Handler) as httpd:
        actual = httpd.server_address[1]
        url = f"http://127.0.0.1:{actual}/"
        print(f"KYC console preview on {url}  (build {build_id})")
        print("  sample data, nothing to send to. Ctrl-C stops it.")
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        if open_browser:
            webbrowser.open(url)

        try:
            while True:
                time.sleep(interval)
                if pull:
                    err = _git_pull()
                    if err:
                        print(f"  (pull skipped: {err})")
                try:
                    page, new_id = build()
                except Exception as exc:
                    print(f"  (build failed, keeping the last good page: {exc})")
                    continue
                if new_id != _Handler.build_id:
                    _Handler.page, _Handler.build_id = page.encode("utf-8"), new_id
                    print(f"  rebuilt {new_id} -- the open window reloads itself")
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-only", metavar="OUT",
                    help="write one self-contained file and exit (opens with file://)")
    ap.add_argument("--port", type=int, default=7788, help="0 picks a free one")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-pull", action="store_true", help="do not git pull before rebuilding")
    ap.add_argument("--interval", type=float, default=20.0, help="seconds between checks")
    a = ap.parse_args()

    for path in (CONSOLE, FIXTURE):
        if not path.exists():
            print(f"missing {path.relative_to(ROOT)} -- run this from a checkout of the repo",
                  file=sys.stderr)
            return 2

    if a.build_only:
        page, build_id = build()
        Path(a.build_only).write_text(page, encoding="utf-8")
        print(f"{a.build_only}  (build {build_id})")
        return 0

    return serve(a.port, not a.no_open, not a.no_pull, a.interval)


if __name__ == "__main__":
    raise SystemExit(main())
