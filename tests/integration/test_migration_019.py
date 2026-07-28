"""PR 7b-core payload-rule repair revision 019.

`017` permitted the governed payload redaction only on `delivered`/`superseded`, and `018`
inherited that rule unchanged. The publisher scrubs a POC email's raw token in two places — after
delivery (terminal, permitted) and when the row gives up and goes `dead` — and the second one was
refused, which rolled back the whole terminal transaction: the row never died, the raw token
stayed at rest, and the raise escaped the outbox worker's loop.
"""

import httpx
import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


class _DeadProvider:
    """An email provider that is always down — the give-up path, driven for real."""

    def send(self, to, subject, body):
        raise RuntimeError("provider down")


def test_dead_poc_email_scrubs_its_raw_token(session_factory, settings, clean_db):
    """THE defect, through the real publisher: a POC email that exhausts its retries must reach
    `dead` AND leave no raw token at rest. Before 019 the redaction was refused, the transaction
    rolled back, and both halves of that sentence were false."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cp') ON CONFLICT DO NOTHING"))
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cp','email',"
            "'{\"to\":\"x@y\",\"subject\":\"s\",\"body\":\"TOKEN-SECRET\"}'::jsonb,'pending')"))
        s.commit()

    one_shot = settings.model_copy(
        update={"outbox_max_attempts": 1, "outbox_backoff_base_seconds": 0})
    pub = OutboxPublisher(
        session_factory, one_shot,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        email_sender=_DeadProvider(),
    )
    pub.process_once()  # must not raise: the raise used to escape the worker loop entirely

    with session_factory() as s:
        row = s.execute(text(
            "SELECT status, payload_json FROM outbox WHERE kind='poc_email'")).one()
    assert row.status == "dead", f"the row never reached dead: {row.status}"
    assert row.payload_json == {"redacted": True}, f"raw token still at rest: {row.payload_json}"


def test_dead_decision_callback_kept_its_body_at_019(pg):
    """019's deliberate exception, pinned AT 019 — revision 020 REMOVED it.

    The reasoning it encoded ("a dead callback must stay requeueable") was sound; putting it in
    DDL was not, because it had no expiry and foreclosed retention's own stated policy. 020 makes
    the redaction uniform and moves the requeue protection to the endpoint. This test stays as the
    record of what 019 did, at the revision where it was true; `test_migration_020.py` holds the
    superseding behaviour."""
    url = _fresh_db(pg, "kyc_mig_019_dead_cb")
    cfg = _config(url)
    command.upgrade(cfg, "019")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "cd", "cd-r1", 1, "pending")
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": oid})

    with pytest.raises(Exception, match="keeps its body"), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET payload_json = '{\"redacted\": true}'::jsonb WHERE id=:i"),
            {"i": oid})

    with eng.begin() as conn:  # and the requeue it protects still works
        conn.execute(text(
            "UPDATE outbox SET status='pending', attempts=0, last_error=NULL WHERE id=:i"),
            {"i": oid})
    eng.dispose()


def test_pending_payload_is_still_immutable_and_arbitrary_values_still_refused(pg):
    """019 widens exactly one door. A pending body still cannot change at all, and no status
    admits a value other than the governed redaction."""
    url = _fresh_db(pg, "kyc_mig_019_narrow")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        pending = _seed_callback(conn, "cn", "cn-r1", 1, "pending")
        dead_poc = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cn','email','{\"body\":\"t\"}'::jsonb,'pending') RETURNING id"
        )).scalar_one()
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": dead_poc})

    with pytest.raises(Exception, match="immutable while the row is pending"), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET payload_json='{\"redacted\": true}'::jsonb WHERE id=:i"),
            {"i": pending})
    for target in (pending, dead_poc):
        with pytest.raises(Exception, match="governed\\s+redaction value"), eng.begin() as conn:
            conn.execute(text(
                "UPDATE outbox SET payload_json='{\"forged\": true}'::jsonb WHERE id=:i"),
                {"i": target})

    with eng.begin() as conn:  # the widened door itself
        conn.execute(text(
            "UPDATE outbox SET payload_json='{\"redacted\": true}'::jsonb WHERE id=:i"),
            {"i": dead_poc})
    eng.dispose()


def test_019_refuses_to_replace_a_guard_it_did_not_write(pg):
    """Same discipline as 018's manifest: the repair pins the body it is repairing, so it cannot
    silently overwrite a witness guard some other change installed."""
    url = _fresh_db(pg, "kyc_mig_019_drift")
    cfg = _config(url)
    command.upgrade(cfg, "018")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION outbox_witness_guard() RETURNS trigger "
            "LANGUAGE plpgsql SET search_path = public, pg_catalog "
            "AS $$ BEGIN RETURN NEW; END $$"))
    with pytest.raises(RuntimeError, match="MIGRATION_019_AUTHORITY_MANIFEST_MISMATCH"):
        command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "018"
    eng.dispose()


def test_019_is_forward_only(pg):
    """019's OWN refusal, pinned at 019: 020 refuses one revision above it."""
    url = _fresh_db(pg, "kyc_mig_019_forward")
    cfg = _config(url)
    command.upgrade(cfg, "019")
    with pytest.raises(RuntimeError, match="MIGRATION_019_DOWNGRADE_REFUSED_FORWARD_ONLY"):
        command.downgrade(cfg, "018")
    eng = create_engine(url)
    with eng.connect() as conn:  # the schema does not move
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "019"
    eng.dispose()


def test_outbox_id_is_immutable(pg):
    """`outbox.id` IS the order — retention documents "id = order", 013's backfill orders by it,
    and _CLAIM_SQL picks the FIFO head with min(id). 017 and 018 froze every other identity field
    and left this one rewritable, so a row's position could be rewritten in place."""
    url = _fresh_db(pg, "kyc_mig_019_id")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        pending = _seed_callback(conn, "ci", "ci-r1", 1, "pending")
        poc = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status, "
            "delivered_at) VALUES ('poc_email','ci','email','{}'::jsonb,'pending',NULL) "
            "RETURNING id")).scalar_one()
        conn.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now() WHERE id=:i"), {"i": poc})

    for target in (pending, poc):  # live row AND terminal row
        with pytest.raises(Exception, match="identity is immutable"), eng.begin() as conn:
            conn.execute(text("UPDATE outbox SET id = id + 900000 WHERE id=:i"), {"i": target})

    with eng.connect() as conn:
        ids = {r[0] for r in conn.execute(text("SELECT id FROM outbox"))}
    assert ids == {pending, poc}, "the ordering authority moved"
    eng.dispose()
