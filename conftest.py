"""Repo-root conftest: put the repo root on sys.path so `docs.contracts` (the typed contract
registries the TechCraft documents render from) is importable by both the tests and the
generators. Only `src` is installed; the registries deliberately live outside `src/kyc_tool` so a
documentation edit never moves the engine source hash."""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The source handoff (scripts/package_handoff.sh) ships the product tree without the internal
# working files (.agents/…) and without `.git`, which every checkout has. Two suite groups
# cannot exist in that tree by construction: the first three parse .agents artifacts, and the
# docs-release-pipeline suites verify claim registries whose executable verifiers read the
# roadmap and whose publish path snapshots git provenance. Only the handoff tree skips
# collecting them; in a checkout a missing governed file must fail the suite loudly, so
# nothing is ever filtered here.
if not (Path(__file__).resolve().parent / ".git").exists():
    collect_ignore = [
        "tests/integration/test_rollback_command.py",
        "tests/unit/test_migration_lineage.py",
        "tests/unit/test_plan_artifact_static.py",
        "tests/unit/test_contract_registry_authority.py",
        "tests/unit/test_contract_rendering.py",
        "tests/unit/test_document_model.py",
    ]
