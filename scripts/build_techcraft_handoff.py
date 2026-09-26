#!/usr/bin/env python3
"""Build the checked-in combined TechCraft handoff from reviewed public copies."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "docs" / "TECHCRAFT_HANDOFF.md"


def _assembly_module():
    path = Path(__file__).resolve().parent / "handoff" / "assembly.py"
    spec = importlib.util.spec_from_file_location("handoff_assembly", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load handoff assembly: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ASSEMBLY = _assembly_module()


def build(
    root: Path = REPO_ROOT,
    *,
    docs_dir: Path | None = None,
    label: str | None = None,
    commit: str | None = None,
) -> str:
    """Return the combined handoff while preserving the historical build API."""
    reviewed_docs = docs_dir or root / "scripts" / "handoff" / "docs"
    return _ASSEMBLY.build_combined_handoff(reviewed_docs, label=label, commit=commit)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the document")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build(), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
