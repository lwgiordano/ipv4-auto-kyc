import json
import shutil
from pathlib import Path

from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import build_bundle, load_policy, read_policy_files

NORMATIVE = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"

def raw_x() -> dict[str, bytes]:
    return read_policy_files(NORMATIVE)

def bundle_x():
    return build_bundle(raw_x())

def make_bundle_y(tmp_path: Path, *, name: str = "policy_y", check_type: str | None = None,
                  points: int | None = None, category: str | None = None,
                  threshold: int | None = None):
    """Copy the 7 normative files to tmp_path/<name> and optionally rewrite one rubric
    item's points+category and/or the top-level threshold, yielding a DIFFERENT valid
    bundle Y (different bundle_hash). Returns (policy_dir, bundle). NOTE: flag-off
    scoring reads each check's STAMPED points, so a Y that changes only an item's
    points does NOT change a flag-off decision — the `threshold` lever is what makes a
    flag-off process-bundle-Y run diverge from a flag-on pinned-X run (§8.8)."""
    dst = tmp_path / name
    shutil.copytree(NORMATIVE, dst)
    rubric = json.loads((dst / "scoring_rubric.json").read_bytes())
    if check_type is not None:
        for item in rubric["items"]:
            if item["check_type"] == check_type:
                item["points"], item["category"] = points, category
    if threshold is not None:
        rubric["threshold"] = threshold
    (dst / "scoring_rubric.json").write_text(json.dumps(rubric, indent=2))
    return dst, load_policy(dst)
