"""Durable, fail-closed v1 acceptance witness (PR 5a §6). The witness gates the
inbound sunset: it must never let real v1 traffic go silently unrecorded, and an
inactive/unseeded row must never satisfy the zero predicate."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from kyc_tool.api import hmac_witness as w

pytestmark = pytest.mark.postgres


def test_inactive_never_zero_regardless_of_wallclock(session_factory, clean_db):
    # clean_db reseeds the witness row INACTIVE (observation_started_at NULL)
    with session_factory() as s:
        assert w.inbound_v1_zero(s, window_days=1, now=datetime(2030, 1, 1, tzinfo=UTC)) is False


def test_zero_needs_observation_age_and_no_recent_accept(session_factory, clean_db):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as s:
        s.execute(
            text("UPDATE hmac_v1_observation SET observation_started_at = :t WHERE id = 1"),
            {"t": start},
        )
        s.commit()
    with session_factory() as s:
        assert w.inbound_v1_zero(s, 14, now=start + timedelta(days=10)) is False  # too young
        assert w.inbound_v1_zero(s, 14, now=start + timedelta(days=20)) is True  # old, no accepts
    with session_factory() as s:
        w.record_v1_accepted(s)
        s.commit()
    with session_factory() as s:
        assert w.inbound_v1_zero(s, 14, now=start + timedelta(days=20)) is False  # recent accept


def test_record_v1_accepted_raises_when_row_absent(session_factory, clean_db):
    with session_factory() as s:
        s.execute(text("DELETE FROM hmac_v1_observation"))
        s.commit()
    with session_factory() as s, pytest.raises(w.WitnessUnavailable):
        w.record_v1_accepted(s)


# NOTE: test_bump_stat_upserts was REMOVED with hmac_witness.bump_stat (re-audit
# `d569a15..4938840` F1) — the diagnostic v2/rejected counters are now process-local in api.auth, not
# a DB upsert. The rejected-path zero-DB guarantee is proven in
# tests/integration/test_metrics_auth.py; the surfaced counters in tests/integration/test_metrics_hmac.py.
