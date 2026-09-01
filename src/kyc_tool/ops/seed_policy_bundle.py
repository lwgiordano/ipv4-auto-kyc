"""Seed a policy bundle into the durable store (PR 6 §8.11) — compute-then-
compare-then-store, so a mismatched policy directory NEVER lands a row: the
bundle is built from `policy_dir` and its hash compared against
`--expect-hash` (the hash an operator names from a reviewed source, e.g. the
deploy manifest) BEFORE any write. A mismatch raises `BundleCorrupt` with
nothing persisted; only a match calls `store_bundle`.

    python -m kyc_tool.ops.seed_policy_bundle --expect-hash <sha256>
"""

import argparse
import sys

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.policy.loader import build_bundle, read_policy_files
from kyc_tool.policy_store import repo as store


def seed_policy_bundle(session_factory, policy_dir, expect_hash: str) -> None:
    """Build the bundle at `policy_dir`; store it ONLY if its hash matches
    `expect_hash`. Raises BundleCorrupt (no row written) on any mismatch."""
    raw = read_policy_files(policy_dir)
    computed = build_bundle(raw).bundle_hash
    if computed != expect_hash:
        raise store.BundleCorrupt(f"{policy_dir} hashes {computed} != expected {expect_hash}")
    with uow(session_factory) as s:
        store.store_bundle(s, raw)


def main() -> int:  # seed_policy_bundle CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-hash", required=True)
    args = ap.parse_args()
    settings = get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    seed_policy_bundle(session_factory, settings.policy_dir, args.expect_hash)
    print("bundle seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
