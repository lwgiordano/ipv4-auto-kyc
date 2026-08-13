"""Durable, cross-replica v1 acceptance witness + diagnostic counters (PR 5a §6).

The API scales horizontally, so in-process counters reset and reach only one
replica — they cannot witness platform-wide v1=0. These helpers persist the
signal in the DB instead.

The v1 acceptance witness is FAIL-CLOSED: if an accepted v1 request cannot be
recorded, the caller must reject it, so real v1 traffic is never silently
invisible. v2 / rejected counters are diagnostic (best-effort availability).
"""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


class WitnessUnavailable(RuntimeError):
    """The durable v1 witness update did not land — the caller must fail closed."""


def record_v1_accepted(session: Session) -> None:
    """Atomically bump the durable v1 witness. Raises if the seeded row is
    absent so the caller rejects the request rather than under-counting."""
    rows = session.execute(
        text(
            "UPDATE hmac_v1_observation "
            "SET accepted_count = accepted_count + 1, last_accepted_at = now() "
            "WHERE id = 1"
        )
    ).rowcount
    if rows != 1:
        raise WitnessUnavailable("hmac_v1_observation row missing")


# NOTE: the diagnostic bump_stat(v2_accepted|rejected) writer was REMOVED (re-audit
# `d569a15..4938840` F1) — the rejected path must not perform a synchronous DB write. The counter is
# now process-local in api.auth._DIAGNOSTIC_COUNTS. The hmac_signature_stats table is left in place
# (frozen migration) and unused; the observation unit may repurpose it behind a bounded async sink.


def inbound_v1_zero(session: Session, window_days: int, now: datetime) -> bool:
    """The sunset zero predicate: safe to reach `hmac_v1_inbound_sunset_at` only
    when observation has run for the full window AND no v1 was accepted within
    it. An absent/unseeded row means "observation never started" (NOT "zero")."""
    # Total on its own, so a caller that bypasses `api.auth._inbound_v1_zero` cannot resurrect
    # re-gate finding 2. A malformed window (True, 0.5, -1, 0) is numeric enough for the day
    # comparisons below and collapses them, turning unreadable evidence into "zero proven" and
    # retiring live v1 traffic. An unusable window is not a zero window.
    if type(window_days) is not int or window_days < 1:
        return False
    row = session.execute(
        text("SELECT observation_started_at, last_accepted_at FROM hmac_v1_observation WHERE id = 1")
    ).one_or_none()
    if row is None or row.observation_started_at is None:
        return False
    if (now - row.observation_started_at).days < window_days:
        return False
    accepted_within_window = (
        row.last_accepted_at is not None and (now - row.last_accepted_at).days < window_days
    )
    return not accepted_within_window


def observation_state(session: Session) -> dict:
    """Witness snapshot for /v1/metrics."""
    row = session.execute(
        text(
            "SELECT observation_started_at, accepted_count, last_accepted_at "
            "FROM hmac_v1_observation WHERE id = 1"
        )
    ).one_or_none()
    if row is None:
        return {"active": False}
    return {
        "active": row.observation_started_at is not None,
        "observation_started_at": (
            row.observation_started_at.isoformat() if row.observation_started_at else None
        ),
        "accepted_count": row.accepted_count,
        "last_accepted_at": row.last_accepted_at.isoformat() if row.last_accepted_at else None,
    }
