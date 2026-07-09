"""OCR engine interface.

TODO(integration): the production engine (Textract/Document AI/…) is a
platform-team decision. The fake engine reads JSON "scans" — the recorded
document fixtures are JSON bodies with the extracted fields inline, which
keeps CI free of binary OCR dependencies. A tesseract-backed engine can slot
in behind the same protocol without touching the adapter.
"""

import json
from typing import Protocol


class OcrEngine(Protocol):
    def extract(self, data: bytes, doc_type: str) -> dict:
        """Returns {name, address, number, jurisdiction} (missing keys allowed)."""
        ...


class JsonScanOcrEngine:
    """Dev/test engine: the 'document' is JSON with a top-level 'fields' map."""

    def extract(self, data: bytes, doc_type: str) -> dict:
        try:
            return dict(json.loads(data).get("fields", {}))
        except (ValueError, AttributeError, TypeError):
            # Real binary uploads (PDF/PNG) raise UnicodeDecodeError here; the
            # fixture engine must degrade to "no fields", never crash the adapter.
            return {}
