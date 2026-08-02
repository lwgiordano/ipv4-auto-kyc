"""Re-audit `5b0f0b8..b75a320` R4-F2: a nonpositive/aliased retention makes the
`now() - make_interval(days => N)` cutoff the FUTURE, so `prune()` would delete/redact CURRENT
immutable audit/evidence. `prune()` fails closed BEFORE opening a transaction, and `main()` validates
the production configuration BEFORE it opens an engine."""

import pytest
from sqlalchemy import text

from kyc_tool.workers import retention

pytestmark = pytest.mark.postgres


def _seed_fresh_audit_row(engine):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_log (case_id, at, actor, action, detail_json) "
                 "VALUES (NULL, now(), 'system', 'fresh', '{}')")
        )


@pytest.mark.parametrize("bad", [-1, 0, True, 1.5, "7"])
def test_prune_refuses_out_of_domain_retention_without_touching_rows(
    engine, session_factory, clean_db, bad
):
    """Nonpositive, boolean, non-integer, and non-numeric retention are all refused; the fresh row
    survives byte-identical because the guard fires before the transaction opens."""
    _seed_fresh_audit_row(engine)
    with pytest.raises(ValueError):
        retention.prune(session_factory, bad)
    with engine.connect() as conn:
        rows = [r.action for r in conn.execute(text("SELECT action FROM audit_log"))]
    assert rows == ["fresh"]  # not deleted by a future cutoff


def test_negative_retention_is_the_exact_data_loss_that_is_now_refused(
    engine, session_factory, clean_db
):
    """-1 is the reported P1: now() - interval '-1 day' is tomorrow, so every fresh row is 'past'
    retention. It must refuse and delete nothing."""
    _seed_fresh_audit_row(engine)
    with pytest.raises(ValueError):
        retention.prune(session_factory, -1)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == 1


def test_a_valid_positive_retention_still_prunes(engine, session_factory, clean_db):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_log (case_id, at, actor, action, detail_json) VALUES "
                 "(NULL, now() - interval '8 years', 'system', 'ancient', '{}'), "
                 "(NULL, now(), 'system', 'fresh', '{}')")
        )
    counts = retention.prune(session_factory, 7 * 365)
    assert counts["audit_log"] == 1
    with engine.connect() as conn:
        rows = [r.action for r in conn.execute(text("SELECT action FROM audit_log"))]
    assert rows == ["fresh"]


def test_main_validates_production_before_opening_an_engine(monkeypatch):
    """Process boundary: a production run with an unsafe config raises before make_engine/prune —
    deleting either the validation call or making it unconditional is visible here."""
    from kyc_tool import config

    bad = config.Settings().model_copy(
        update={"environment": "production", "retention_days": -1}
    )
    monkeypatch.setattr(retention, "get_settings", lambda: bad)
    monkeypatch.setattr(
        retention, "make_engine",
        lambda *a, **k: pytest.fail("engine created on an invalid production config"),
    )
    monkeypatch.setattr(
        retention, "prune",
        lambda *a, **k: pytest.fail("prune called on an invalid production config"),
    )
    with pytest.raises(config.ProductionConfigError):
        retention.main()
