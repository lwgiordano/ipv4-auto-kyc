"""Policy drift guard: a normative file may only change alongside a deliberate
baseline update — and, where the file carries a version, a version bump."""

import json
from pathlib import Path

from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import POLICY_FILES, load_policy

BASELINE = json.loads((Path(__file__).parent / "policy_baseline.json").read_text())
BUNDLE = load_policy(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable")


def test_every_policy_file_is_pinned():
    assert set(BASELINE) == set(POLICY_FILES)


def test_policy_files_match_baseline():
    for filename, pin in BASELINE.items():
        actual_sha = BUNDLE.shas[filename]
        if actual_sha == pin["sha256"]:
            continue
        raw = json.loads(
            (REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable" / filename).read_bytes()
        )
        version = raw.get("version") if isinstance(raw, dict) else None
        assert version != pin["version"], (
            f"{filename} changed (sha {pin['sha256'][:12]}→{actual_sha[:12]}) without a "
            "version bump. Bump the file's version AND update policy_baseline.json "
            "deliberately — policy changes must be auditable."
        )
        raise AssertionError(
            f"{filename} changed. If intentional, update tests/policy_driven/"
            "policy_baseline.json in the same commit."
        )
