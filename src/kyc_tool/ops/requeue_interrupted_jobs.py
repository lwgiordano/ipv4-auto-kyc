"""Cutover recovery one-shot (PR 5b §10 step 3). Run ONCE after ALL pipeline
workers are confirmed stopped and none have restarted, BEFORE new workers start.

With every worker stopped, every `status='running'` row is by definition an
interrupted job (there is no worker registry to prove liveness from `locked_by`,
and none is needed). This requeues them all — regardless of lease expiry —
WITHOUT consuming the forced-stop attempt (the claim pre-incremented `attempts`;
we decrement it back so an interrupted FINAL attempt is retried rather than
dead-lettered by the passive reaper). It MUST NOT run while any worker is live.

    python -m kyc_tool.ops.requeue_interrupted_jobs
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def requeue_interrupted(session_factory) -> int:
    """Requeue every running job; return the count. Raises if any remains."""
    with session_factory() as session:
        count = session.execute(
            text(
                "UPDATE jobs SET status='queued', attempts = attempts - 1, "
                "locked_by=NULL, lease_expires_at=NULL, updated_at=now() "
                "WHERE status='running'"
            )
        ).rowcount
        remaining = session.execute(
            text("SELECT count(*) FROM jobs WHERE status='running'")
        ).scalar_one()
        session.commit()
    if remaining != 0:
        raise RuntimeError(f"{remaining} jobs still running after requeue — workers not stopped?")
    return count


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    n = requeue_interrupted(session_factory)
    print(f"requeued {n} interrupted job(s); 0 running")
    return 0


if __name__ == "__main__":
    sys.exit(main())
