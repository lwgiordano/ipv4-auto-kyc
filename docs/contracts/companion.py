"""The governed companion artifact: the signer as a file, not as glyphs on a page.

Re-audit `4f23f23..122cc67` finding 4. The published snippet was on one page with printed copy
markers, and a test proved it compiled — but only after reconstructing indentation from glyph
x-coordinates and Courier advance widths. A TechCraft reader has no such decoder. Exact
`pdftotext` output fails `compile()` with `IndentationError`, and `-layout` plus `textwrap.dedent`
fails at the docstring. The test proved the glyphs CONTAIN enough information to recover the
source, which is not the same as the document being copyable.

So the document stops promising direct copy. It names this file, states its SHA-256, and prints
the code as illustration. The bytes below are the artifact; the tests execute exactly these bytes
and assert the digest the PDF prints matches them. When distribution is approved, this file and
its digest go into the release manifest alongside the two PDFs.
"""

import hashlib
from pathlib import Path

from docs.contracts.signing_example import published_snippet

ARTIFACT_NAME = "kyc-signer-example.py"
ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "docs" / "artifacts"

_HEADER = '''"""Reference HMAC-v2 signer for the KYC tool's platform integration.

Shipped alongside the Platform Integration Contract. This file is the authoritative copy — the
code printed in the PDF is an illustration of it, and a PDF is not a reliable clipboard for
indentation-sensitive source.

Verify before use: run `shasum -a 256 {name}` and compare the result against the
sha256 printed beside this file's name in the Platform Integration Contract, section 4. This
file deliberately does not state its own digest: a digest cannot cover the text that quotes it,
and an earlier version that tried printed a value the documented command could never produce.

The tool's own test suite executes these exact bytes against the published test vector and
asserts they reproduce the published signature, so a mismatch means the file was altered in
transit, not that the algorithm changed.
"""

'''


def artifact_body() -> str:
    """The runnable source, without the header (which carries the header's own digest)."""
    return published_snippet() + "\n"


def artifact_text() -> str:
    """The complete file: header, then the runnable body. The header does NOT contain the
    digest — the digest is over these exact bytes, so the contract states it and the file
    points there."""
    return _HEADER.format(name=ARTIFACT_NAME) + artifact_body()


def artifact_digest() -> str:
    """SHA-256 over the COMPLETE shipped file — exactly what `shasum -a 256` prints for it.

    It was the runnable body only, quoted inside the file's own header, and the documented
    command therefore produced a different value than the document printed: a reader following
    the instruction concluded the file was tampered with. The digest the document states must be
    the digest the stated command produces."""
    return hashlib.sha256(artifact_text().encode()).hexdigest()


def artifact_path() -> Path:
    return ARTIFACT_DIR / ARTIFACT_NAME


def write_artifact() -> Path:
    path = artifact_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact_text())
    return path
