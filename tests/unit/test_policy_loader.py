from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import build_bundle, load_policy, read_policy_files

DIR = REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"

def test_build_bundle_from_raw_matches_disk_load():
    disk = load_policy(DIR)
    rebuilt = build_bundle(read_policy_files(DIR), policy_dir=None)   # DB origin
    assert rebuilt.bundle_hash == disk.bundle_hash
    assert rebuilt.shas == disk.shas
    assert rebuilt.rubric == disk.rubric
    assert rebuilt.policy_dir is None and disk.policy_dir == DIR

def test_read_policy_files_returns_all_seven_as_bytes():
    from kyc_tool.policy.loader import POLICY_FILES
    raw = read_policy_files(DIR)
    assert set(raw) == set(POLICY_FILES)
    assert all(isinstance(v, bytes) for v in raw.values())
