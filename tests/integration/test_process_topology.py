# test_process_topology.py — ISOLATED interpreters (not shared sys.modules). §8.17
# needs more than an import probe: a LAZY `policy_store`/policy-file access inside
# build_publisher() / retention.main() is invisible to an import-only check. So
# CONSTRUCT/RUN each seam with the policy tree ABSENT and assert policy_store was
# never imported. `make_engine` is lazy (no eager connect), so build_publisher()
# constructs fine; retention.main() prunes (empty of recent rows) against a real DB.
import os
import subprocess
import sys


def _seam_imports_policy_store(seam_code: str, database_url: str) -> bool:
    # retention.main() logs `retention_pruned` to stdout BEFORE our marker, so a bare
    # `stdout.strip() == "True"` false-negatives on "log-line\nTrue" (reads as False,
    # masking a real lazy import). Parse a UNIQUE sentinel line instead.
    code = (f"import sys\n{seam_code}\n"
            "print('POLICY_STORE_IMPORTED=' + str('kyc_tool.policy_store' in sys.modules))\n")
    env = {**os.environ, "KYC_POLICY_DIR": "/nonexistent-policy-dir",  # policy files ABSENT
           "KYC_DATABASE_URL": database_url}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr
    marker = next(line for line in out.stdout.splitlines() if line.startswith("POLICY_STORE_IMPORTED="))
    return marker.split("=", 1)[1] == "True"

def test_outbox_publisher_constructs_without_policy_store(migrated):
    assert _seam_imports_policy_store(
        "from kyc_tool.workers.outbox_worker import build_publisher\nbuild_publisher()", migrated) is False

def test_retention_runs_without_policy_store(migrated):
    assert _seam_imports_policy_store(
        "from kyc_tool.workers.retention import main\nmain()", migrated) is False
