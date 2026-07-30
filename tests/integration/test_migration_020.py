"""PR 7b-core redaction-uniformity revision 020 (adversarial review of the released `019`).

`019` stated "a body that can still be sent is immutable" and implemented only half of it: the
rule keyed on `OLD.status`, never on what the row was BECOMING. This suite proves both ends are
closed, that the `dead`-callback carve-out is gone, and that its justification now lives at the
requeue endpoint where it belongs.
"""

import httpx
import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres

_REDACT = "'{\"redacted\": true}'::jsonb"


def _dead_poc(conn, case_id="cu", *, redact=True):
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
    oid = conn.execute(text(
        "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
        "('poc_email',:c,'email','{\"to\":\"a@b\",\"subject\":\"s\",\"body\":\"TOKEN\"}'::jsonb,"
        "'pending') RETURNING id"), {"c": case_id}).scalar_one()
    if redact:
        conn.execute(
            text(f"UPDATE outbox SET status='dead', payload_json={_REDACT} WHERE id=:i"),
            {"i": oid},
        )
    else:
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": oid})
    return oid


def test_a_redacted_row_can_never_be_made_sendable_again(pg):
    """THE hole `019` left: keying on OLD.status alone accepted `dead + redact + reopen`, in one
    statement or two, producing a `pending` POC email with no recipient — claimed FIRST by
    `min(id)` and failing on every claim, blocking its whole stream, with the body unrestorable."""
    url = _fresh_db(pg, "kyc_mig_020_reopen")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _dead_poc(conn)

    # (a) the one-statement form
    with pytest.raises(Exception, match="never be MADE sendable again"), eng.begin() as conn:
        conn.execute(text(
            f"UPDATE outbox SET status='pending', payload_json={_REDACT}, attempts=0 "
            "WHERE id=:i"), {"i": oid})

    # (b) the two-statement form — the second never enters the payload branch at all
    with eng.begin() as conn:
        conn.execute(text(f"UPDATE outbox SET payload_json={_REDACT} WHERE id=:i"), {"i": oid})
    with pytest.raises(Exception, match="never be MADE sendable again"), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET status='pending', attempts=0 WHERE id=:i"), {"i": oid})

    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT status, payload_json FROM outbox WHERE id=:i"), {"i": oid}).one()
    assert row.status == "dead" and row.payload_json == {"redacted": True}
    eng.dispose()


def test_an_unredacted_dead_row_can_still_be_requeued(pg):
    """The rule must not break the requeue it protects: a dead row whose body survives is still
    reopenable, for both kinds."""
    url = _fresh_db(pg, "kyc_mig_020_requeue_ok")
    cfg = _config(url)
    command.upgrade(cfg, "020")
    eng = create_engine(url)
    with eng.begin() as conn:
        poc = _dead_poc(conn, "cv", redact=False)
        cb = _seed_callback(conn, "cv", "cv-r1", 1, "pending")
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": cb})
    with eng.begin() as conn:
        for target in (poc, cb):
            conn.execute(text(
                "UPDATE outbox SET status='pending', attempts=0, last_error=NULL WHERE id=:i"),
                {"i": target})
    with eng.connect() as conn:
        assert conn.execute(text(
            "SELECT count(*) FROM outbox WHERE status='pending'")).scalar_one() == 2
    eng.dispose()


def test_a_dead_decision_callback_may_now_be_redacted(pg):
    """`019` made this a permanent DDL carve-out, which foreclosed retention's own stated policy:
    a dead callback's body carries reviewer-derived `checks[].source` and was kept forever."""
    url = _fresh_db(pg, "kyc_mig_020_dead_cb")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        # the body is written AT INSERT: a pending payload is immutable, which is the rule this
        # revision keeps intact while widening the redaction door
        _seed_callback(conn, "cw", "cw-r1", 1, "pending")
        cb = conn.execute(text(
            "SELECT id FROM outbox WHERE run_id='cw-r1'")).scalar_one()
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": cb})
    with eng.begin() as conn:
        conn.execute(text(f"UPDATE outbox SET payload_json={_REDACT} WHERE id=:i"), {"i": cb})
    with eng.connect() as conn:
        assert conn.execute(text(
            "SELECT payload_json FROM outbox WHERE id=:i"), {"i": cb}).scalar_one() == {
                "redacted": True}
    eng.dispose()


def test_retention_now_reaches_dead_callbacks(session_factory, clean_db):
    """Aged on `created_at`, since a dead row has neither delivered_at nor resolved_at."""
    from kyc_tool.workers.retention import prune

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cx', 1)"))
        s.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('cx-ev','cx','cx-k','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        s.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                       "VALUES ('cx-r1','cx','cx-ev','PUBLISH_DECISION')"))
        s.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('cx-d','cx','cx-r1','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,1)"))
        oid = s.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status, payload_json, created_at) VALUES ('decision_callback','cx','cx-r1',"
            "'decision',1,'pending','{\"checks\":[{\"source\":\"reviewer:alice\"}]}'::jsonb,"
            "now() - interval '9999 days') RETURNING id")).scalar_one()
        s.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": oid})
        s.commit()

    counts = prune(session_factory, retention_days=7 * 365)
    assert counts["outbox_callback_redacted"] == 1
    with session_factory() as s:
        assert s.execute(text(
            "SELECT payload_json FROM outbox WHERE id=:i"), {"i": oid}).scalar_one() == {
                "redacted": True}


def test_requeue_refuses_a_redacted_dead_callback(client, engine, post_event):
    """The carve-out's justification, relocated: the endpoint refuses a scrubbed row of EITHER
    kind and names the right remedy for each."""
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cy', 1)"))
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('cy-ev','cy','cy-k','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('cy-r1','cy','cy-ev','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('cy-d','cy','cy-r1','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,1)"))
        oid = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status, payload_json) VALUES ('decision_callback','cy','cy-r1','decision',1,"
            "'pending','{}'::jsonb) RETURNING id")).scalar_one()
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": oid})
        conn.execute(text(f"UPDATE outbox SET payload_json={_REDACT} WHERE id=:i"), {"i": oid})

    resp = client.post(f"/ui/api/requeue/outbox/{oid}",
                       headers={"Authorization": "Bearer test-admin-token"})
    assert resp.status_code == 409
    assert "re-emit the decision" in resp.json()["detail"]


def test_020_validates_the_whole_code_surface(pg):
    """`019` claimed `018`'s discipline and checked ONE function; 13 of 14 drifts walked through.
    Each of the three most dangerous now refuses."""
    for name, drift in (
        ("search-path-unpinned", "ALTER FUNCTION outbox_no_delete_guard() RESET search_path"),
        ("trigger-disabled", "ALTER TABLE outbox DISABLE TRIGGER trg_outbox_no_delete"),
        ("unexpected-trigger",
         "CREATE FUNCTION zz_x() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; "
         "CREATE TRIGGER zz_x_trg BEFORE UPDATE ON outbox FOR EACH ROW EXECUTE FUNCTION zz_x()"),
        ("manual-pointer-gutted",
         "CREATE OR REPLACE FUNCTION cases_latest_manual_pointer() RETURNS trigger "
         "LANGUAGE plpgsql SET search_path = public, pg_catalog AS $$ BEGIN RETURN NEW; END $$"),
    ):
        url = _fresh_db(pg, f"kyc_mig_020_drift_{name.replace('-', '_')[:24]}")
        cfg = _config(url)
        command.upgrade(cfg, "019")
        eng = create_engine(url)
        with eng.begin() as conn:
            conn.execute(text(drift))
        with pytest.raises(RuntimeError, match="MIGRATION_020_AUTHORITY_CODE_MISMATCH"):
            command.upgrade(cfg, "head")
        with eng.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")).scalar_one() == "019"
        eng.dispose()


def test_the_poisoned_row_scenario_is_unreachable_end_to_end(session_factory, settings, clean_db):
    """The review's measured impact, as a regression: a scrubbed POC email cannot re-enter the
    claim path, so it cannot block its stream. Driven through the real publisher.

    NOTE (revision 021): the closing `sent == [...]` assertion is NOT the discriminating part —
    a dead row is invisible to `_CLAIM_SQL` on every revision. The `pytest.raises` in the middle
    is what this test proves. The stronger end-to-end property — that a row ALREADY pending and
    redacted no longer stops the whole outbox — is in `test_migration_021.py`, because 020's own
    fix made that case fatal."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    sent: list = []

    class _Sender:
        def send(self, to, subject, body):
            sent.append(to)

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cz') ON CONFLICT DO NOTHING"))
        first = s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cz','email','{\"to\":\"a@b\",\"subject\":\"s\",\"body\":\"T\"}'::jsonb,"
            "'pending') RETURNING id")).scalar_one()
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cz','email','{\"to\":\"c@d\",\"subject\":\"s\",\"body\":\"T2\"}'::jsonb,"
            "'pending')"))
        s.execute(
            text(f"UPDATE outbox SET status='dead', payload_json={_REDACT} WHERE id=:i"),
            {"i": first},
        )
        s.commit()

    # the poisoning UPDATE is refused at the database…
    with session_factory() as s, pytest.raises(Exception, match="never be MADE sendable again"):
        s.execute(text("UPDATE outbox SET status='pending' WHERE id=:i"), {"i": first})

    # …so the stream's later email delivers normally
    pub = OutboxPublisher(
        session_factory, settings,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        email_sender=_Sender())
    pub.process_pending()
    assert sent == ["c@d"], f"the healthy email did not get through: {sent}"
