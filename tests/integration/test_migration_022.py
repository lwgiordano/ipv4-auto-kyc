"""PR 7b-core revision 022: authority manifest + terminal/data invariants.

This is the owner-audit repair for 018-021. The prior repairs fixed real holes, but their
preflights still accepted trigger surfaces that looked right by name while no longer enforcing
anything in ordinary sessions.
"""

import json

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migration_018 import _manual_decision
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


_REDACTED = "'{\"redacted\": true}'::jsonb"


def _seed_poc(conn, *, case_id="cp", status="pending", payload=None) -> int:
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
    body = payload or {"to": "poc@example.test", "subject": "s", "body": "token"}
    return conn.execute(
        text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) "
            "VALUES ('poc_email', :c, 'email', CAST(:p AS jsonb), :status) RETURNING id"
        ),
        {"c": case_id, "p": json.dumps(body), "status": status},
    ).scalar_one()


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            "ALTER TABLE outbox ENABLE REPLICA TRIGGER trg_outbox_no_delete",
            "mode R",
        ),
        (
            """
            CREATE FUNCTION kyc_noop_delete_guard() RETURNS trigger
            LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$
            BEGIN
                RETURN OLD;
            END $$
            """,
            "trg_outbox_no_delete",
        ),
    ],
)
def test_022_refuses_trigger_mode_or_same_name_redirection(pg, mutate, needle):
    """A same-name trigger is not authority unless it fires in origin mode and targets the
    reviewed function. 020/021 checked names only; 022 must refuse before DDL."""
    url = _fresh_db(pg, f"kyc_mig_022_trigger_{abs(hash(needle)) % 10000}")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(mutate))
        if "kyc_noop_delete_guard" in mutate:
            conn.execute(text("DROP TRIGGER trg_outbox_no_delete ON outbox"))
            conn.execute(text(
                "CREATE TRIGGER trg_outbox_no_delete BEFORE DELETE ON outbox "
                "FOR EACH ROW EXECUTE FUNCTION kyc_noop_delete_guard()"
            ))

    with pytest.raises(RuntimeError, match="MIGRATION_022_AUTHORITY_SURFACE_MISMATCH") as exc:
        command.upgrade(cfg, "head")
    assert needle in str(exc.value)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "021"
    eng.dispose()


def test_022_head_keeps_origin_trigger_authority(pg):
    """The repaired head rejects ordinary deletes and attempt tampering in a normal session."""
    url = _fresh_db(pg, "kyc_mig_022_authority_live")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "cauth", "cauth-r1", 1, "pending", generation="attempt_v1")
        claim = conn.execute(
            text(
                "UPDATE outbox SET claim_token=gen_random_uuid(), "
                "claim_lease_expires_at=now()+interval '1 hour', claimed_by='w' "
                "WHERE id=:i RETURNING claim_token"
            ),
            {"i": oid},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO outbox_delivery_attempts "
                "(attempt_id, outbox_id, claim_token, wire_version, request_sha256) "
                "VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"
            ),
            {"o": oid, "t": claim, "s": "a" * 64},
        )
    with pytest.raises(Exception, match="deletion refused"), eng.begin() as conn:
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": oid})
    with pytest.raises(Exception, match="outbox_delivery_attempts is insert-only"), eng.begin() as conn:
        attempt = conn.execute(
            text("SELECT attempt_id FROM outbox_delivery_attempts WHERE outbox_id=:i"),
            {"i": oid},
        ).scalar_one()
        conn.execute(
            text("UPDATE outbox_delivery_attempts SET request_sha256=:s WHERE attempt_id=:a"),
            {"a": attempt, "s": "b" * 64},
        )
    eng.dispose()


def test_poc_terminal_transitions_must_atomically_redact(pg):
    url = _fresh_db(pg, "kyc_mig_022_poc_redact")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)

    for status in ("dead", "delivered"):
        with eng.begin() as conn:
            oid = _seed_poc(conn, case_id=f"c-{status}")
        with pytest.raises(Exception, match="poc_email terminal rows must be redacted"), eng.begin() as conn:
            extra = ", delivered_at=now()" if status == "delivered" else ""
            conn.execute(
                text(f"UPDATE outbox SET status=:s{extra} WHERE id=:i"),
                {"s": status, "i": oid},
            )
        with eng.begin() as conn:
            extra = ", delivered_at=now()" if status == "delivered" else ""
            conn.execute(
                text(f"UPDATE outbox SET status=:s, payload_json={_REDACTED}{extra} WHERE id=:i"),
                {"s": status, "i": oid},
            )
            payload = conn.execute(
                text("SELECT payload_json FROM outbox WHERE id=:i"),
                {"i": oid},
            ).scalar_one()
            assert payload == {"redacted": True}
    eng.dispose()


def test_022_redacts_legacy_terminal_poc_rows_before_enforcing(pg):
    url = _fresh_db(pg, "kyc_mig_022_legacy_poc")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        dead = _seed_poc(conn, case_id="legacy-dead", status="pending")
        delivered = _seed_poc(conn, case_id="legacy-delivered", status="pending")
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": dead})
        conn.execute(
            text("UPDATE outbox SET status='delivered', delivered_at=now() WHERE id=:i"),
            {"i": delivered},
        )
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        rows = conn.execute(
            text("SELECT payload_json FROM outbox WHERE kind='poc_email' ORDER BY case_id")
        ).scalars().all()
    assert rows == [{"redacted": True}, {"redacted": True}]
    eng.dispose()


def test_created_at_is_immutable_in_every_state(pg):
    url = _fresh_db(pg, "kyc_mig_022_created_at")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        pending = _seed_callback(conn, "ctime", "ctime-r1", 1, "pending", generation="attempt_v1")
        dead = _seed_callback(conn, "ctime", "ctime-r2", 2, "pending", generation="attempt_v1")
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": dead})
        delivered = _seed_callback(conn, "ctime", "ctime-r3", 3, "pending", generation="attempt_v1")
        claim = conn.execute(
            text(
                "UPDATE outbox SET claim_token=gen_random_uuid(), "
                "claim_lease_expires_at=now()+interval '1 hour', claimed_by='w' "
                "WHERE id=:i RETURNING claim_token"
            ),
            {"i": delivered},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO outbox_delivery_attempts "
                "(attempt_id, outbox_id, claim_token, wire_version, request_sha256) "
                "VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"
            ),
            {"o": delivered, "t": claim, "s": "a" * 64},
        )
        conn.execute(
            text(
                "UPDATE outbox SET status='delivered', delivered_at=now(), "
                "callback_wire_sha256=:s, wire_version='legacy', claim_token=NULL, "
                "claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"
            ),
            {"i": delivered, "s": "a" * 64},
        )

    for oid in (pending, dead, delivered):
        with pytest.raises(Exception, match="created_at is immutable"), eng.begin() as conn:
            conn.execute(
                text("UPDATE outbox SET created_at = now() + interval '100 years' WHERE id=:i"),
                {"i": oid},
            )
    eng.dispose()


def test_manual_pointer_must_reference_a_manual_decision(pg):
    url = _fresh_db(pg, "kyc_mig_022_manual_guard")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cm', 1)"))
        _manual_decision(conn, case_id="cm", decision_id="d-manual", reviewer="rev", seq=1)
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('ev-auto','cm','k-auto','h','x','{}'::jsonb,'{}'::jsonb,2)"
        ))
        conn.execute(text(
            "INSERT INTO runs (id, case_id, triggering_event_id, state) "
            "VALUES ('r-auto','cm','ev-auto','PUBLISH_DECISION')"
        ))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('d-auto','cm','r-auto','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,1)"
        ))
    with (
        pytest.raises(Exception, match="latest_manual_decision_row_id must reference manual"),
        eng.begin() as conn,
    ):
        conn.execute(text("UPDATE cases SET latest_manual_decision_row_id='d-auto' WHERE id='cm'"))
    with pytest.raises(Exception, match="decision manual flag is immutable"), eng.begin() as conn:
        conn.execute(text("UPDATE decisions SET manual=false WHERE id='d-manual'"))
    eng.dispose()


def test_022_refuses_existing_nonmanual_manual_pointer(pg):
    url = _fresh_db(pg, "kyc_mig_022_bad_manual_pointer")
    cfg = _config(url)
    command.upgrade(cfg, "021")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE cases DISABLE TRIGGER ALL"))
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cbad', 1)"))
        conn.execute(text("ALTER TABLE cases ENABLE TRIGGER ALL"))
        _seed_callback(conn, "cbad", "cbad-r1", 1, "pending", generation="attempt_v1")
        conn.execute(text(
            "UPDATE cases SET latest_manual_decision_row_id='cbad-r1-d' WHERE id='cbad'"
        ))
    with pytest.raises(RuntimeError, match="MIGRATION_022_MANUAL_POINTER_MISMATCH"):
        command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "021"
    eng.dispose()
