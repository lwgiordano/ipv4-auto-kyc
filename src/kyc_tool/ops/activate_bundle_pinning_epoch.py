"""Activate the bundle-pinning epoch (PR 6 §8.11/§8.15) — the single-row
identity gate that pins which bundle+engine build are the enforced ones.
The CLI compares the LOCALLY loaded policy bundle (this process's on-disk
policy, not the store) against `--expect-bundle-hash` before ever touching
the database — a valid but WRONG bundle (even one already seeded in
`policy_bundles`) is refused here, not merely by store-absence.
`store.activate_epoch` then re-checks `--expect-engine` against this
process's `ENGINE_BUILD_ID` and read-back-compares the written row, so a
mismatch on either axis leaves `bundle_pinning_epoch` untouched.

    python -m kyc_tool.ops.activate_bundle_pinning_epoch \\
        --expect-bundle-hash <sha256> --expect-engine <build-id>
"""

import argparse
import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory, uow
from kyc_tool.policy.loader import load_policy
from kyc_tool.policy_store import repo as store


def post_epoch_null_provenance(session) -> dict:
    """Alert payload (PR 6 §7): decisions/checks/runs recorded AFTER the epoch
    activated but missing their pinning provenance — decisions by
    `decided_at`, checks by `created_at`, runs by finding post-epoch
    decisions, joining to the runs they reference, and surfacing runs whose
    OWN `engine_build_id` is NULL — independent of whether the decision
    itself was stamped. Empty lists mean nothing to alert on. No epoch row
    yet means nothing has been activated, so there is nothing to check."""
    ep = session.execute(text("SELECT activated_at FROM bundle_pinning_epoch WHERE id=1")).first()
    if ep is None:
        return {"decisions": [], "checks": [], "runs": []}
    at = ep.activated_at
    dec = [
        r.id
        for r in session.execute(
            text("SELECT id FROM decisions WHERE decided_at > :at AND engine_build_id IS NULL"),
            {"at": at},
        )
    ]
    chk = [
        r.id
        for r in session.execute(
            text("SELECT id FROM checks WHERE created_at > :at AND policy_bundle_hash IS NULL"),
            {"at": at},
        )
    ]
    runs = [
        r.id
        for r in session.execute(
            text(
                "SELECT DISTINCT r.id FROM decisions d JOIN runs r ON r.id = d.run_id "
                "WHERE d.decided_at > :at AND r.engine_build_id IS NULL"
            ),
            {"at": at},
        )
    ]
    return {"decisions": dec, "checks": chk, "runs": runs}


def main() -> int:  # activate_bundle_pinning_epoch CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-bundle-hash", required=True)
    ap.add_argument("--expect-engine", required=True)
    args = ap.parse_args()
    settings = get_settings()
    local = load_policy(settings.policy_dir)  # compare LOCAL loaded bundle
    if local.bundle_hash != args.expect_bundle_hash:
        raise SystemExit(f"local bundle {local.bundle_hash} != --expect-bundle-hash")
    session_factory = make_session_factory(make_engine(settings.database_url))
    with uow(session_factory) as s:  # activate_epoch also checks --expect-engine
        store.activate_epoch(
            s, expect_bundle_hash=args.expect_bundle_hash, expect_engine=args.expect_engine
        )
    print("epoch activated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
