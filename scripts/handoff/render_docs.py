"""Render the exported handoff, using only the documents in that package."""

import contextlib
import html
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from markdown_it import MarkdownIt

CSS = """
@page { size: A4; margin: 19mm 16mm 20mm;
  @bottom-left { content: "IPv4.Global · Closed staging"; font: 8pt Arial; color: #596c79; }
  @bottom-right { content: counter(page); font: 8pt Arial; color: #596c79; }
}
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 1080px; padding: 36px; color: #233344;
  background: #fff; font: 16px/1.55 Arial, sans-serif; }
h1, h2, h3, h4 { color: #123b50; line-height: 1.22; break-after: avoid; }
h1 { font-size: 30px; margin: 44px 0 20px; border-bottom: 2px solid #137781;
  padding-bottom: 12px; }
h2 { font-size: 23px; margin: 32px 0 14px; }
h3 { font-size: 18px; margin: 24px 0 10px; }
p, ul, ol { margin: 10px 0 16px; }
li { margin: 5px 0; }
a { color: #006d83; text-underline-offset: 3px; }
table { border-collapse: collapse; width: 100%; table-layout: fixed;
  margin: 18px 0 24px; font-size: 13px; line-height: 1.45; }
th, td { text-align: left; vertical-align: top; padding: 9px 10px;
  border: 1px solid #cbd5dc; overflow-wrap: anywhere; }
th { background: #eaf1f4; color: #123b50; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
pre { padding: 12px 14px; border: 1px solid #d4dee5; background: #f4f7f9;
  white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 monospace; }
code { font: 0.9em/1.5 monospace; overflow-wrap: anywhere; }
blockquote { margin: 18px 0; padding: 1px 16px; border-left: 3px solid #137781; }
.release { font-size: 13px; color: #596c79; border-bottom: 1px solid #cbd5dc;
  padding-bottom: 16px; }
@media print {
  body { padding: 0; max-width: none; font-size: 10pt; line-height: 1.42; }
  h1 { font-size: 21pt; break-before: page; }
  h1 + h1 { break-before: avoid; margin-top: 8px; }
  h1:first-of-type { break-before: auto; margin-top: 10px; }
  h2 { font-size: 15pt; margin-top: 24px; }
  h3 { font-size: 12pt; }
  table { font-size: 8pt; }
  th, td { padding: 6px; }
  pre { font-size: 8pt; }
  a { color: inherit; }
  p { orphans: 3; widows: 3; }
}
@media (max-width: 600px) { body { padding: 18px; } table { font-size: 11px; } }
"""


def render(root: Path) -> None:
    """Build HTML and PDF from the combined Markdown already staged by the exporter."""
    md = root / "docs" / "TECHCRAFT_HANDOFF.md"
    source = md.read_text(encoding="utf-8")
    parser = MarkdownIt("commonmark", {"html": False}).enable("table")
    body = parser.render(source)
    title = "IPv4.Global KYC/KYB — TechCraft handoff"
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
        '<p class="release">Closed-staging integration handoff · '
        "See START-HERE.md and MANIFEST.json for release identification.</p>"
        f"{body}</body></html>"
    )
    output = md.with_suffix(".html")
    output.write_text(page, encoding="utf-8")
    candidates = (
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    browser = next((p for p in candidates if p and Path(p).is_file()), None)
    if browser is None:
        raise RuntimeError("Chrome or Chromium is required to build the handoff PDF")
    pdf = md.with_suffix(".pdf")
    pdf.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="handoff-print-") as profile:
        command = [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            "--no-first-run",
            "--disable-extensions",
            "--disable-background-networking",
        ]
        if os.geteuid() == 0:
            command.append("--no-sandbox")
        command.extend(
            [
                f"--user-data-dir={profile}",
                f"--print-to-pdf={pdf}",
                output.as_uri(),
            ]
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                # Chrome can keep auxiliary processes alive after saving on macOS.
                # Wait for a complete PDF, then stop this isolated renderer process group.
                if (
                    pdf.exists()
                    and pdf.stat().st_size > 1000
                    and pdf.read_bytes().rstrip().endswith(b"%%EOF")
                ):
                    break
                if process.poll() is not None:
                    raise RuntimeError("Handoff PDF renderer exited without a complete PDF")
                time.sleep(0.2)
            else:
                raise RuntimeError("Handoff PDF render timed out")
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    import argparse

    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("root", type=Path)
    render(args.parse_args().root.resolve())
