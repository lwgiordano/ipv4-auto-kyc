"""Repo-root conftest: put the repo root on sys.path so `docs.contracts` (the typed contract
registries the TechCraft documents render from) is importable by both the tests and the
generators. Only `src` is installed; the registries deliberately live outside `src/kyc_tool` so a
documentation edit never moves the engine source hash."""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
