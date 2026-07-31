"""Post-013 outbox claim reset (PR 7b-core §Rollout). Run ONCE after ALL outbox publishers
are confirmed stopped and none have restarted, for post-013 future stops and the post-013
rollback path ONLY. It refuses pre-013 schema (the claim columns don't exist yet), clears only
rows with the COMPLETE claim tuple, PRESERVES next_attempt_at (an interrupted old claim simply
waits until its already-recorded due time), and read-back-asserts zero claim tuples — rolling
back atomically (never a partial reset) if any remain. It MUST NOT run while a publisher is live.

    python -m kyc_tool.ops.reset_interrupted_outbox_claims
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def reset_claims(session_factory) -> int:
    """Clear every complete outbox claim tuple; return the count. Refuses pre-013; on a
    surviving tuple it rolls back BEFORE commit and raises (atomic — no partial reset)."""
    with session_factory() as session:
        has_col = session.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='outbox' AND column_name='claim_token'"
            )
        ).first()
        if has_col is None:
            raise RuntimeError(
                "reset_interrupted_outbox_claims refuses pre-013 schema: no claim_token column "
                "(an interrupted pre-013 claim is encoded only in next_attempt_at)"
            )
        count = session.execute(
            text(
                "UPDATE outbox SET claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
                "WHERE status='pending' AND claim_token IS NOT NULL "
                "AND claim_lease_expires_at IS NOT NULL AND claimed_by IS NOT NULL"
            )
        ).rowcount
        remaining = session.execute(
            text(
                "SELECT count(*) FROM outbox WHERE claim_token IS NOT NULL "
                "OR claim_lease_expires_at IS NOT NULL OR claimed_by IS NOT NULL"
            )
        ).scalar_one()
        if remaining != 0:  # roll back the whole reset BEFORE committing — never a partial clear
            session.rollback()
            raise RuntimeError(f"{remaining} outbox claim tuple(s) remain — publishers not stopped?")
        session.commit()
    return count


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    n = reset_claims(session_factory)
    print(f"reset {n} interrupted outbox claim(s); 0 claim tuples remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
