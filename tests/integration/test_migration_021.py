"""PR 7b-core poison-recovery revision 021 (adversarial review of the released `020`).

`020` closed a real hole and opened a worse one: its new arm asserted a STATE rather than a
TRANSITION, so it fired on the CLAIM of an already-poisoned row. The claim has no exception
handler and takes the `min(id)` head, so that turned a bounded, self-healing degradation into an
unbounded outage of every stream. A guard must stop the bad write, never strand the row.
"""

import httpx
import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.config import ProcessRole
from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres

_REDACT = "'{\"redacted\": true}'::jsonb"


def _poisoned_pending_poc(conn, case_id="cp"):
    """A pending row with a destroyed body — only reachable by INSERT before 021 closed it."""
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
    return conn.execute(text(
        f"INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
        f"('poc_email',:c,'email',{_REDACT},'pending') RETURNING id"), {"c": case_id}).scalar_one()


# --- P1: an already-poisoned row must stay CLAIMABLE so it can drain itself -------------------

def test_a_poisoned_row_can_still_be_claimed_and_dead_lettered(pg):
    """THE regression `020` introduced. Every one of these UPDATEs was refused at `020`, and the
    claim's refusal exits the worker process — which, since the claim takes the `min(id)` head,
    stops EVERY case and BOTH streams. They must all pass, so the row retries, dies, and drains."""
    url = _fresh_db(pg, "kyc_mig_021_claimable")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    # At head the state is unreachable — 021's INSERT guard and its transition rule both refuse
    # it, and the upgrade preflight refuses to run with one present. This is therefore a row that
    # PREDATES the guards, reproduced the only honest way: with the trigger disabled, exactly as
    # a 019/020-era deployment would have left it behind.
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE outbox DISABLE TRIGGER trg_outbox_lifecycle_insert"))
        oid = _poisoned_pending_poc(conn)
        conn.execute(text("ALTER TABLE outbox ENABLE TRIGGER trg_outbox_lifecycle_insert"))

    with eng.begin() as conn:  # the publisher's CLAIM
        conn.execute(text(
            "UPDATE outbox SET claim_token=gen_random_uuid(), "
            "claim_lease_expires_at=now() + interval '1 hour', claimed_by='w' WHERE id=:i"),
            {"i": oid})
    with eng.begin() as conn:  # _record_failure's retry branch
        conn.execute(text(
            "UPDATE outbox SET attempts=attempts+1, last_error='boom', "
            "next_attempt_at=now() + interval '1 minute', claim_token=NULL, "
            "claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"), {"i": oid})
    with eng.begin() as conn:  # and finally the dead-letter that drains it
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": oid})

    with eng.connect() as conn:
        assert conn.execute(text(
            "SELECT status FROM outbox WHERE id=:i"), {"i": oid}).scalar_one() == "dead"
    eng.dispose()


def test_making_a_redacted_row_sendable_is_still_refused(pg):
    """The rule `020` meant to state, still stated: a scrubbed body may not BECOME sendable."""
    url = _fresh_db(pg, "kyc_mig_021_becoming")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('cb') ON CONFLICT DO NOTHING"))
        oid = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cb','email','{\"to\":\"a@b\"}'::jsonb,'pending') RETURNING id"
        )).scalar_one()
        conn.execute(
            text(f"UPDATE outbox SET status='dead', payload_json={_REDACT} WHERE id=:i"),
            {"i": oid},
        )

    for tamper in (
        f"UPDATE outbox SET status='pending', payload_json={_REDACT} WHERE id=:i",
        "UPDATE outbox SET status='pending' WHERE id=:i",
    ):
        with pytest.raises(Exception, match="never be MADE sendable again"), eng.begin() as conn:
            conn.execute(text(tamper), {"i": oid})
    eng.dispose()


# --- the invariant is maintained at INSERT, for BOTH kinds -----------------------------------

@pytest.mark.parametrize("kind, extra_cols, extra_vals", [
    ("poc_email", "", ""),
    ("decision_callback", ", run_id, decision_sequence", ", 'cq-r1', 2"),
])
def test_no_row_may_be_born_pending_with_a_redacted_body(pg, kind, extra_cols, extra_vals):
    """`017`'s guard covered only decision callbacks and never looked at the payload; poc_email
    had no INSERT guard at all. That is how the state `020` crashed on became reachable."""
    url = _fresh_db(pg, f"kyc_mig_021_born_{kind[:8]}")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        if kind == "decision_callback":
            _seed_callback(conn, "cq", "cq-r0", 1, "pending")
            conn.execute(text(
                "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES "
                "('cq-r1-ev','cq','cq-r1','h','x','{}'::jsonb,'{}'::jsonb,2)"))
            conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                              "VALUES ('cq-r1','cq','cq-r1-ev','PUBLISH_DECISION')"))
            conn.execute(text(
                "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
                "('cq-d1','cq','cq-r1','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,2)"))
        else:
            conn.execute(text("INSERT INTO cases (id) VALUES ('cq') ON CONFLICT DO NOTHING"))

    stream = "decision" if kind == "decision_callback" else "email"
    with pytest.raises(Exception, match="born pending with a redacted body"), eng.begin() as conn:
        conn.execute(text(
            f"INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status"
            f"{extra_cols}) VALUES ('{kind}','cq','{stream}',{_REDACT},'pending'{extra_vals})"))
    eng.dispose()


# --- the upgrade refuses rather than silently arming the outage ------------------------------

@pytest.mark.parametrize("claimed", [False, True])
def test_upgrade_refuses_when_a_poisoned_row_already_exists(pg, claimed):
    """`020` held ACCESS EXCLUSIVE and never looked, so upgrading a database damaged by a `019`
    deployment armed the outage in silence. This names the rows and the recovery SQL must work for
    both unclaimed and claimed poisoned rows."""
    url = _fresh_db(pg, f"kyc_mig_021_preflight_{'claimed' if claimed else 'open'}")
    cfg = _config(url)
    command.upgrade(cfg, "020")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _poisoned_pending_poc(conn, "cr")
        if claimed:
            # At 020 this corrupted state can only be present as a historical row that predates
            # the stricter guard. Model that boundary explicitly; the production path being
            # audited is the 021 preflight/remediation, not 020's known claim-time failure.
            conn.execute(text("ALTER TABLE outbox DISABLE TRIGGER trg_outbox_witness_guard"))
            conn.execute(text(
                "UPDATE outbox SET claim_token=gen_random_uuid(), "
                "claim_lease_expires_at=now()+interval '1 hour', claimed_by='w' WHERE id=:i"
            ), {"i": oid})
            conn.execute(text("ALTER TABLE outbox ENABLE TRIGGER trg_outbox_witness_guard"))

    with pytest.raises(RuntimeError, match="MIGRATION_021_PREFLIGHT_UNSENDABLE_PENDING_ROWS") as e:
        command.upgrade(cfg, "head")
    assert f"id={oid}" in str(e.value), "the operator must be told WHICH rows"
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "020"

    with eng.begin() as conn:  # the documented retirement, then it upgrades
        conn.execute(text(
            "UPDATE outbox SET status='dead', last_error='body redacted; unsendable', "
            "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
            f"WHERE status='pending' AND payload_json = {_REDACT}"))
    command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "023"
    eng.dispose()


# --- the last unpinned authority function ----------------------------------------------------

def test_the_automatic_pointer_function_is_now_pinned_and_validated(pg):
    """`014` created `kyc_set_latest_decision_row` with no `search_path` and an unqualified
    relation, and `020`'s "complete code surface" preflight did not pin it — the same overclaim
    `020` charged `019` with. Gutting it at `020` let the upgrade succeed with the verdict pointer
    left NULL."""
    url = _fresh_db(pg, "kyc_mig_021_pointer_drift")
    cfg = _config(url)
    command.upgrade(cfg, "020")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION kyc_set_latest_decision_row() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"))
    with pytest.raises(RuntimeError, match="MIGRATION_021_AUTHORITY_CODE_MISMATCH"):
        command.upgrade(cfg, "head")
    eng.dispose()

    url2 = _fresh_db(pg, "kyc_mig_021_pointer_pinned")
    cfg2 = _config(url2)
    command.upgrade(cfg2, "head")
    eng2 = create_engine(url2)
    with eng2.connect() as conn:
        cfgs = conn.execute(text(
            "SELECT proconfig FROM pg_proc WHERE proname='kyc_set_latest_decision_row'"
        )).scalar_one()
    assert cfgs == ["search_path=public, pg_catalog"], "021 must pin what 014 left unpinned"
    with eng2.begin() as conn:  # and it still maintains the pointer it is responsible for
        _seed_callback(conn, "cs", "cs-r1", 1, "pending")
        assert conn.execute(text(
            "SELECT latest_decision_row_id FROM cases WHERE id='cs'")).scalar_one() == "cs-r1-d"
    eng2.dispose()


# --- end to end: the outage is gone --------------------------------------------------------

def test_a_poisoned_row_no_longer_stops_the_whole_outbox(session_factory, settings, clean_db):
    """The measured `020` failure, as a regression: at `020` `process_pending` raised out of the
    claim and NOTHING delivered — including rows in other cases and other streams. The poisoned
    row must now fail, dead-letter, and let everything else through."""
    from kyc_tool.outbox.publisher import OutboxPublisher

    sent: list = []

    class _Sender:
        def send(self, to, subject, body):
            sent.append(to)

    with session_factory() as s:
        s.execute(text("INSERT INTO cases (id) VALUES ('cz') ON CONFLICT DO NOTHING"))
        # a poisoned row FIRST (lowest id => the claim head), seeded past the INSERT guard the
        # only way left: born pending with a body, then scrubbed while dead, then retired-not.
        poison = s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cz','email','{\"to\":\"x@y\",\"subject\":\"s\",\"body\":\"T\"}'::jsonb,"
            "'pending') RETURNING id")).scalar_one()
        s.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) VALUES "
            "('poc_email','cz','email','{\"to\":\"good@x\",\"subject\":\"s\",\"body\":\"B\"}'"
            "::jsonb,'pending')"))
        s.commit()

    one_shot = settings.model_copy(
        update={"outbox_max_attempts": 1, "outbox_backoff_base_seconds": 0})
    pub = OutboxPublisher(
        session_factory, one_shot,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        email_sender=_Sender(), process_role=ProcessRole.OUTBOX_WORKER)

    # scrub the head row's body the legal way (dead -> redact), then let it be reopened is
    # impossible — so instead prove the SURVIVING danger: a row already pending+redacted.
    # a row that predates the guards: pending AND redacted. At 020 the CLAIM of this row raised
    # out of process_once and killed the worker, and because it is the min(id) head NOTHING else
    # delivered. It must now be claimed, fail, dead-letter, and get out of the way.
    with session_factory() as s:
        s.execute(text("ALTER TABLE outbox DISABLE TRIGGER trg_outbox_witness_guard"))
        s.execute(text(f"UPDATE outbox SET payload_json={_REDACT} WHERE id=:i"), {"i": poison})
        s.execute(text("ALTER TABLE outbox ENABLE TRIGGER trg_outbox_witness_guard"))
        s.commit()

    pub.process_pending()  # must not raise: the poisoned head fails and dead-letters
    pub.process_pending()  # and the stream drains behind it

    with session_factory() as s:
        head = s.execute(text(
            "SELECT status FROM outbox WHERE id=:i"), {"i": poison}).scalar_one()
    assert head == "dead", f"the poisoned head did not drain itself: {head}"
    assert sent == ["good@x"], f"the healthy email did not get through: {sent}"
