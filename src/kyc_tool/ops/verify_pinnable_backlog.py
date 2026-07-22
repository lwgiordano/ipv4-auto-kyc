"""Deploy preflight (PR 6 §8.11): every runnable/requeueable `run_transition`
job must be scoreable once bundle pinning is enforced. A job is un-pinnable
when its run's creation-pin `policy_bundle_hash` is NULL, absent from
`policy_bundles`, or corrupt — any of those would dead-letter the job at
`Pipeline.resolve_bundle` under flag-on (orchestration/pipeline.py). Run this
BEFORE flipping `enforce_bundle_pinning` on; a nonzero exit blocks the cutover.

    python -m kyc_tool.ops.verify_pinnable_backlog
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.policy_store import repo as store


def verify_pinnable_backlog(session_factory) -> list[str]:
    """Return the run_ids of every queued/running/dead run_transition job whose
    creation-pin bundle can't be resolved. Empty list == the backlog is safe to
    cut over."""
    bad = []
    with session_factory() as s:
        rows = s.execute(
            text(
                "SELECT DISTINCT r.id AS run_id, r.policy_bundle_hash AS h FROM jobs j "
                "JOIN runs r ON r.id = (j.payload_json->>'run_id') "
                "WHERE j.kind='run_transition' AND j.status IN ('queued','running','dead')"
            )
        ).all()
        for run_id, h in rows:
            if h is None:
                bad.append(run_id)
                continue
            try:
                if store.load_bundle(s, h) is None:
                    bad.append(run_id)
            except store.BundleCorrupt:
                bad.append(run_id)
    return bad


def main() -> int:  # deploy preflight: nonzero exit blocks the cutover
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    bad = verify_pinnable_backlog(session_factory)
    print(f"un-pinnable run_transition jobs: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
