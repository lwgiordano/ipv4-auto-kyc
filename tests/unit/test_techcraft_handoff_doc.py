"""The checked-in combined TechCraft document must be what the generator produces now.

It is assembled from three sources that people edit, so the shipped copy goes stale in
silence. This is the one check that notices.
"""

import runpy
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "build_techcraft_handoff.py"


def test_checked_in_document_matches_a_fresh_build():
    build = runpy.run_path(str(GENERATOR))["build"]
    doc = REPO_ROOT / "docs" / "TECHCRAFT_HANDOFF.md"
    # pytest.fail, not assert: the documents run to thousands of lines and the assertion diff
    # buries the one instruction that fixes this.
    if doc.read_text(encoding="utf-8") != build(REPO_ROOT):
        pytest.fail(
            "docs/TECHCRAFT_HANDOFF.md is stale: run "
            "`.venv/bin/python scripts/build_techcraft_handoff.py` and commit the result"
        )
