"""Start the v1 observation clock AFTER the non-hot cutover (PR 5a §6a).

Non-network, idempotent compare-and-set: sets `observation_started_at` only when
it is NULL. A rerun reports "already active" and NEVER resets a live window, so
it is safe to run more than once. Run once, post-drain, after every old API
replica is gone and every new instance is readiness-verified:

    python -m kyc_tool.ops.activate_hmac_v1_observation
"""

import sys

from sqlalchemy import text

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory


def activate(session_factory) -> bool:
    """Returns True if this call started the clock, False if already active."""
    with session_factory() as session:
        row = session.execute(
            text(
                "UPDATE hmac_v1_observation SET observation_started_at = now() "
                "WHERE observation_started_at IS NULL RETURNING id"
            )
        ).one_or_none()
        session.commit()
        return row is not None


def main() -> int:
    session_factory = make_session_factory(make_engine(get_settings().database_url))
    if activate(session_factory):
        print("v1 observation activated (clock started now)")
    else:
        print("v1 observation already active — no change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
