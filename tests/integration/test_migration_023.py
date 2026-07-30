"""PR 7b-core revision 023: cross-table authority FK/pointer validation."""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


SENTINEL = "MIGRATION_023_CROSS_TABLE_AUTHORITY_MISMATCH"


def _insert_auto_chain(conn, *, case_id: str, run_id: str, seq: int) -> None:
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "(:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"
        ),
        {"e": f"{run_id}-ev", "c": case_id, "k": run_id, "s": seq},
    )
    conn.execute(
        text(
            "INSERT INTO runs (id, case_id, triggering_event_id, state) "
            "VALUES (:r,:c,:e,'PUBLISH_DECISION')"
        ),
        {"r": run_id, "c": case_id, "e": f"{run_id}-ev"},
    )
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "(:d,:c,:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,:s)"
        ),
        {"d": f"{run_id}-d", "c": case_id, "r": run_id, "s": seq},
    )


def _insert_manual(conn, *, case_id: str, decision_id: str, reviewer: str, event_seq: int) -> None:
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "(:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"
        ),
        {"e": f"{decision_id}-ev", "c": case_id, "k": decision_id, "s": event_seq},
    )
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
            "(:d,:c,NULL,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,true,:r)"
        ),
        {"d": decision_id, "c": case_id, "r": reviewer},
    )


def _assert_refuses_from_021(url: str, needle: str) -> None:
    cfg = _config(url)
    with pytest.raises(RuntimeError, match=SENTINEL) as exc:
        command.upgrade(cfg, "head")
    assert needle in str(exc.value)
    eng = create_engine(url)
    with eng.connect() as conn:
        # Alembic runs this upgrade-to-head attempt transactionally: a 023 refusal
        # rolls the attempted 022 step back too, leaving the DB at its last
        # committed pre-attempt revision.
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "021"
    eng.dispose()


def test_023_refuses_missing_cross_table_authority_constraints(pg):
    """022 validated triggers/functions only. 023 must also validate the authority FKs/uniques."""
    url = _fresh_db(pg, "kyc_mig_023_missing_fks")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE outbox DROP CONSTRAINT fk_outbox_decision_triple"))
        conn.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_decision"))
        conn.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_manual_decision"))

    _assert_refuses_from_021(url, "missing")
    eng.dispose()


def test_023_refuses_cross_case_latest_decision_pointer_after_fk_drift(pg):
    url = _fresh_db(pg, "kyc_mig_023_cross_latest")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_decision"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c-a', 0)"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c-b', 1)"))
        _insert_auto_chain(conn, case_id="c-b", run_id="c-b-r", seq=1)
        conn.execute(
            text("UPDATE cases SET latest_decision_row_id='c-b-r-d' WHERE id='c-a'")
        )

    _assert_refuses_from_021(url, "latest_decision_row_id")
    eng.dispose()


def test_023_refuses_cross_case_latest_manual_pointer_after_fk_drift(pg):
    url = _fresh_db(pg, "kyc_mig_023_cross_manual")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE cases DROP CONSTRAINT fk_cases_latest_manual_decision"))
        conn.execute(text("INSERT INTO cases (id) VALUES ('c-a')"))
        conn.execute(text("INSERT INTO cases (id) VALUES ('c-b')"))
        _insert_manual(conn, case_id="c-b", decision_id="manual-b", reviewer="rev-b", event_seq=1)
        conn.execute(
            text("UPDATE cases SET latest_manual_decision_row_id='manual-b' WHERE id='c-a'")
        )

    _assert_refuses_from_021(url, "latest_manual_decision_row_id")
    eng.dispose()


def test_023_refuses_cross_case_outbox_decision_triple_after_fk_drift(pg):
    url = _fresh_db(pg, "kyc_mig_023_cross_outbox")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE outbox DROP CONSTRAINT fk_outbox_decision_triple"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c-a', 0)"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('c-b', 1)"))
        _insert_auto_chain(conn, case_id="c-b", run_id="c-b-r", seq=1)
        conn.execute(
            text(
                "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
                "payload_json, status, witness_generation) VALUES "
                "('decision_callback','c-a','c-b-r','decision',1,'{}'::jsonb,'pending',"
                "'attempt_v1')"
            )
        )

    _assert_refuses_from_021(url, "fk_outbox_decision_triple data")
    eng.dispose()


def test_023_canonical_head_keeps_authority_constraints(pg):
    url = _fresh_db(pg, "kyc_mig_023_canonical")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT conname, convalidated FROM pg_constraint "
                "WHERE conname IN ('fk_outbox_decision_triple', 'fk_cases_latest_decision', "
                "'fk_cases_latest_manual_decision') ORDER BY conname"
            )
        ).all()
    assert rows == [
        ("fk_cases_latest_decision", True),
        ("fk_cases_latest_manual_decision", True),
        ("fk_outbox_decision_triple", True),
    ]
    eng.dispose()
