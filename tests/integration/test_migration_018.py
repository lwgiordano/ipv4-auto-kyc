"""PR 7b-core transition-authority revision 018 (re-audit `cbb783b` F1/F2/F3/F6).

`017` drew the authority boundary; this suite proves the three places it did not reach — a
manifest that validated only the child table, an update guard that constrained only transitions
INTO `delivered`, and a downgrade that restored search-path-vulnerable authority — plus the
manual-attribution pointer that replaces sorting `decisions` by a random UUID.
"""

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migration_015 import _claimed_pending
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def _deliver_legally(conn, row, sha="a" * 64):
    conn.execute(text(
        "UPDATE outbox SET status='delivered', delivered_at=now(), "
        "callback_wire_sha256=:s, wire_version='legacy', "
        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
        "WHERE id=:i"), {"s": sha, "i": row.id})


def _supersede_legally(conn, oid):
    conn.execute(text(
        "UPDATE outbox SET status='superseded', resolved_at=now(), "
        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"), {"i": oid})


# --- F1: the manifest covers BOTH authority tables and every owned function -------------------

# Each mutation is applied at real `016`... `017`, then the 018 upgrade must refuse. Every one of
# these walked straight through `017`, whose validator checked the child table's shape and the
# parent's trigger NAMES only.
_DRIFT = [
    ("parent-fk-dropped", "ALTER TABLE outbox DROP CONSTRAINT fk_outbox_decision_triple"),
    ("parent-check-dropped", "ALTER TABLE outbox DROP CONSTRAINT ck_outbox_wire_witness_delivered"),
    ("parent-lifecycle-check-dropped",
     "ALTER TABLE outbox DROP CONSTRAINT ck_outbox_status_lifecycle"),
    ("parent-index-dropped", "DROP INDEX ix_outbox_live_status"),
    ("parent-notnull-dropped", "ALTER TABLE outbox ALTER COLUMN ordering_stream DROP NOT NULL"),
    ("parent-default-dropped", "ALTER TABLE outbox ALTER COLUMN witness_generation DROP DEFAULT"),
    ("child-index-dropped", "DROP INDEX ix_attempt_outbox"),
    ("child-check-dropped",
     "ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT ck_attempt_admission_vocab"),
    # the audit's exact mutilation: keep the NAME, gut the BODY
    ("function-body-gutted",
     "CREATE OR REPLACE FUNCTION outbox_witness_guard() RETURNS trigger LANGUAGE plpgsql "
     "AS $$ BEGIN RETURN NEW; END $$"),
    # same body, but the search_path pin removed — the shadow-relation door reopened
    ("function-search-path-unpinned",
     "ALTER FUNCTION outbox_no_delete_guard() RESET search_path"),
    ("trigger-disabled", "ALTER TABLE outbox DISABLE TRIGGER trg_outbox_no_delete"),
    ("trigger-redirected",
     "CREATE FUNCTION zz_noop() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; "
     "DROP TRIGGER trg_outbox_lifecycle_insert ON outbox; "
     "CREATE TRIGGER trg_outbox_lifecycle_insert BEFORE INSERT ON outbox "
     "FOR EACH ROW EXECUTE FUNCTION zz_noop()"),
    ("trigger-retimed",
     "DROP TRIGGER trg_outbox_no_delete ON outbox; "
     "CREATE TRIGGER trg_outbox_no_delete AFTER DELETE ON outbox "
     "FOR EACH ROW EXECUTE FUNCTION outbox_no_delete_guard()"),
    ("unexpected-trigger-added",
     "CREATE FUNCTION zz_extra() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; "
     "CREATE TRIGGER zz_extra_trg BEFORE UPDATE ON outbox "
     "FOR EACH ROW EXECUTE FUNCTION zz_extra()"),
]


@pytest.mark.parametrize("name, drift_sql", _DRIFT, ids=[d[0] for d in _DRIFT])
def test_upgrade_refuses_on_any_authority_drift(pg, name, drift_sql):
    """Every drift refuses with ONE stable sentinel, BEFORE any DDL: head stays `017` and the
    authority code the environment still has is left exactly as it was."""
    url = _fresh_db(pg, f"kyc_mig_018_drift_{name.replace('-', '_')[:26]}")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(drift_sql))

    with pytest.raises(RuntimeError, match="MIGRATION_018_AUTHORITY_MANIFEST_MISMATCH"):
        command.upgrade(cfg, "head")

    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "017"
        # refused BEFORE partial DDL: 018's own objects were never created
        assert conn.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name='cases' "
            "AND column_name='latest_manual_decision_row_id'")).scalar_one() == 0
    eng.dispose()


def test_canonical_017_to_018_path_upgrades(pg):
    """The manifest is exact, so it must accept the real thing — a drift check that refuses
    everything is not a check."""
    url = _fresh_db(pg, "kyc_mig_018_canonical")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "018"
    eng.dispose()


# --- F2: the complete transition matrix — a terminal row is FINAL ----------------------------

def test_superseded_callback_cannot_be_resurrected(pg):
    """THE resurrection: `017` constrained only transitions INTO `delivered`, so a superseded
    callback accepted `status='pending', resolved_at=NULL` — and a resurrected row is claimable
    and sendable. Both reopening paths now die at the database."""
    url = _fresh_db(pg, "kyc_mig_018_resurrect")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "cr", "cr-r1", 1, "pending")
        _supersede_legally(conn, oid)

    for tamper in (
        "UPDATE outbox SET status='pending', resolved_at=NULL WHERE id=:i",
        "UPDATE outbox SET status='dead', resolved_at=NULL WHERE id=:i",
    ):
        with pytest.raises(Exception, match="terminal"), eng.begin() as conn:
            conn.execute(text(tamper), {"i": oid})

    with eng.connect() as conn:  # still terminal, still unclaimable
        row = conn.execute(text(
            "SELECT status, resolved_at FROM outbox WHERE id=:i"), {"i": oid}).one()
    assert row.status == "superseded" and row.resolved_at is not None
    eng.dispose()


def test_legacy_delivered_with_null_digest_is_still_terminal(pg):
    """A legacy delivered row has no digest, so `017`'s write-once branch had nothing to freeze
    and the row could be reopened. Finality is a property of the STATUS, not of how much witness
    evidence happens to exist."""
    url = _fresh_db(pg, "kyc_mig_018_legacy_final")
    cfg = _config(url)
    # born-delivered is only expressible before 017's INSERT guard; seed it in the world that
    # produced it, then adopt upward through the real chain
    command.upgrade(cfg, "016")
    eng = create_engine(url)
    with eng.begin() as conn:
        legacy = _seed_callback(conn, "cl", "cl-r1", 1, "delivered", delivered=True)
    command.upgrade(cfg, "head")

    for tamper in (
        "UPDATE outbox SET status='pending', delivered_at=NULL WHERE id=:i",
        "UPDATE outbox SET status='dead', delivered_at=NULL WHERE id=:i",
    ):
        with pytest.raises(Exception, match="terminal"), eng.begin() as conn:
            conn.execute(text(tamper), {"i": legacy})

    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT status, callback_wire_sha256 FROM outbox WHERE id=:i"), {"i": legacy}).one()
    assert row.status == "delivered" and row.callback_wire_sha256 is None
    eng.dispose()


def test_terminal_metadata_is_frozen(pg):
    """Timestamps, the claim tuple, the attempt counter and the error text are all part of the
    terminal record. Only the governed payload redaction may still be written."""
    url = _fresh_db(pg, "kyc_mig_018_frozen")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cf", "cf-r1", 1)
        _deliver_legally(conn, row)

    for tamper in (
        "UPDATE outbox SET delivered_at = now() - interval '5 days' WHERE id=:i",
        "UPDATE outbox SET attempts = 99 WHERE id=:i",
        "UPDATE outbox SET last_error = 'rewritten' WHERE id=:i",
        "UPDATE outbox SET claim_token = gen_random_uuid(), "
        "claim_lease_expires_at = now() + interval '1 hour', claimed_by='w' WHERE id=:i",
        "UPDATE outbox SET next_attempt_at = now() + interval '1 day' WHERE id=:i",
    ):
        with pytest.raises(Exception, match="frozen"), eng.begin() as conn:
            conn.execute(text(tamper), {"i": row.id})

    with eng.begin() as conn:  # the one governed write still works
        conn.execute(text(
            "UPDATE outbox SET payload_json = '{\"redacted\": true}'::jsonb WHERE id=:i"),
            {"i": row.id})
    eng.dispose()


def test_legal_lifecycles_still_work_for_both_kinds(pg):
    """The matrix is exhaustive, not restrictive: every documented live transition survives."""
    url = _fresh_db(pg, "kyc_mig_018_legal")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        # decision callback: pending -> pending (retry/backoff) -> dead -> pending -> delivered
        row = _claimed_pending(conn, "cg", "cg-r1", 1, with_attempt=False)
        conn.execute(text(
            "UPDATE outbox SET attempts = attempts + 1, last_error='boom', "
            "next_attempt_at = now() + interval '1 minute', claim_token=NULL, "
            "claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"), {"i": row.id})
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": row.id})
        conn.execute(text(  # the governed requeue
            "UPDATE outbox SET status='pending', attempts=0, last_error=NULL, "
            "next_attempt_at=now() WHERE id=:i"), {"i": row.id})
        again = conn.execute(text(
            "UPDATE outbox SET claim_token=gen_random_uuid(), "
            "claim_lease_expires_at=now() + interval '1 hour', claimed_by='w' "
            "WHERE id=:i RETURNING id, claim_token"), {"i": row.id}).one()
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": again.id, "t": again.claim_token, "s": "b" * 64})
        _deliver_legally(conn, again, sha="b" * 64)

        # a second callback supersedes legally
        other = _seed_callback(conn, "cg", "cg-r2", 2, "pending")
        _supersede_legally(conn, other)

        # poc email: pending -> dead -> pending -> delivered, then retention deletes it
        conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) "
            "VALUES ('poc_email','cg','email','{}'::jsonb,'pending')"))
        pid = conn.execute(text(
            "SELECT id FROM outbox WHERE kind='poc_email'")).scalar_one()
        conn.execute(text("UPDATE outbox SET status='dead' WHERE id=:i"), {"i": pid})
        conn.execute(text("UPDATE outbox SET status='pending' WHERE id=:i"), {"i": pid})
        conn.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now() WHERE id=:i"), {"i": pid})
        assert conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": pid}).rowcount == 1

    with eng.connect() as conn:
        assert conn.execute(text(
            "SELECT status FROM outbox WHERE run_id='cg-r1'")).scalar_one() == "delivered"
    eng.dispose()


def test_poc_email_can_never_be_superseded(pg):
    """Supersession is decision-ordering semantics; the lifecycle CHECK says so and the matrix
    now agrees explicitly rather than by omission."""
    url = _fresh_db(pg, "kyc_mig_018_poc_sup")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO cases (id) VALUES ('cp') ON CONFLICT DO NOTHING"))
        pid = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status) "
            "VALUES ('poc_email','cp','email','{}'::jsonb,'pending') RETURNING id")).scalar_one()
    with pytest.raises(
        Exception, match="illegal poc-email transition|ck_outbox_status_lifecycle"
    ), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET status='superseded', resolved_at=now() WHERE id=:i"), {"i": pid})
    eng.dispose()


# --- F3: the unsafe downgrade is unreachable --------------------------------------------------

def test_018_is_forward_only_even_on_an_unused_database(pg):
    """`017`'s witness-free downgrade recreates unqualified, search-path-vulnerable functions.
    The walk is closed at `018` — including on a database with no evidence at all, where `017`
    would happily have proceeded."""
    url = _fresh_db(pg, "kyc_mig_018_forward_only")
    cfg = _config(url)
    command.upgrade(cfg, "head")

    with pytest.raises(RuntimeError, match="MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY"):
        command.downgrade(cfg, "017")
    with pytest.raises(RuntimeError, match="MIGRATION_018_DOWNGRADE_REFUSED_FORWARD_ONLY"):
        command.downgrade(cfg, "012")

    eng = create_engine(url)
    with eng.connect() as conn:  # nothing dropped underneath the authority
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "018"
        assert conn.execute(text(
            "SELECT count(*) FROM pg_proc WHERE proname = 'outbox_witness_guard'")).scalar_one() == 1
    eng.dispose()


def test_recreated_authority_still_pins_its_search_path(pg):
    """018 recreates the witness guard; the recreation must carry the pin, or the fix would
    reintroduce exactly what F3 is about."""
    url = _fresh_db(pg, "kyc_mig_018_pin")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        cfgs = {
            r.proname: list(r.proconfig or [])
            for r in conn.execute(text(
                "SELECT proname, proconfig FROM pg_proc "
                "WHERE proname IN ('outbox_witness_guard','cases_latest_manual_pointer')"))
        }
    assert cfgs["outbox_witness_guard"] == ["search_path=public, pg_catalog"]
    assert cfgs["cases_latest_manual_pointer"] == ["search_path=public, pg_catalog"]
    eng.dispose()


# --- F6: manual attribution follows a pointer, never a UUID sort ------------------------------

def _manual_decision(conn, *, case_id, decision_id, reviewer, seq):
    """One manual decision row (run_id NULL, no sequence — exactly what the real handler writes)."""
    conn.execute(text(
        "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
        "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"),
        {"e": f"{decision_id}-ev", "c": case_id, "k": f"{decision_id}-k", "s": seq})
    conn.execute(text(
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
        "buy_enablement, policy_shas, manual, reviewer_id) VALUES "
        "(:d,:c,NULL,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,true,:r)"),
        {"d": decision_id, "c": case_id, "r": reviewer})


def test_manual_pointer_follows_insert_order_not_uuid_order(pg):
    """`decisions.id` is a random UUID hex, so `ORDER BY id DESC` returned the LEXICALLY largest
    manual row. Approve first under an id that sorts high, then under one that sorts low: the
    attribution must name the SECOND reviewer."""
    url = _fresh_db(pg, "kyc_mig_018_manual_order")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('cm')"))
        _manual_decision(conn, case_id="cm", decision_id="ffff-first", reviewer="rev-A", seq=1)
        _manual_decision(conn, case_id="cm", decision_id="0000-second", reviewer="rev-B", seq=2)

    with eng.connect() as conn:
        pointed = conn.execute(text(
            "SELECT d.reviewer_id FROM cases c JOIN decisions d "
            "ON d.id = c.latest_manual_decision_row_id WHERE c.id='cm'")).scalar_one()
        by_uuid = conn.execute(text(
            "SELECT reviewer_id FROM decisions WHERE case_id='cm' AND manual "
            "ORDER BY id DESC LIMIT 1")).scalar_one()
    assert pointed == "rev-B", "the pointer must name the latest INSERT, not the largest UUID"
    assert by_uuid == "rev-A", "the old sort really does pick the wrong row — this is the defect"
    eng.dispose()


def test_manual_pointer_backfill_is_honest_about_unorderable_history(pg):
    """Exactly one legacy manual row is unambiguous and gets the pointer. Two or more cannot be
    ordered at all, so the case is left explicitly unresolved rather than guessed."""
    url = _fresh_db(pg, "kyc_mig_018_manual_backfill")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    with eng.begin() as conn:  # the pre-018 world: no pointer column, no trigger
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1'), ('c2'), ('c3')"))
        _manual_decision(conn, case_id="c1", decision_id="d-one", reviewer="rev-1", seq=1)
        _manual_decision(conn, case_id="c2", decision_id="d-two-a", reviewer="rev-2a", seq=1)
        _manual_decision(conn, case_id="c2", decision_id="d-two-b", reviewer="rev-2b", seq=2)
    command.upgrade(cfg, "head")

    with eng.connect() as conn:
        pointers = {
            r.id: r.latest_manual_decision_row_id
            for r in conn.execute(text(
                "SELECT id, latest_manual_decision_row_id FROM cases ORDER BY id"))
        }
    assert pointers["c1"] == "d-one"    # unambiguous
    assert pointers["c2"] is None       # two manual rows, no order — explicitly unresolved
    assert pointers["c3"] is None       # no manual approval at all
    eng.dispose()


def test_automatic_decisions_never_move_the_manual_pointer(pg):
    """The whole point of a second pointer: the verdict pointer follows the newest decision, the
    manual pointer stays on the manual act."""
    url = _fresh_db(pg, "kyc_mig_018_manual_sticky")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES ('cs', 0)"))
        _manual_decision(conn, case_id="cs", decision_id="d-manual", reviewer="rev-9", seq=1)
        # a later AUTOMATIC decision, through the same chain the pipeline writes
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('ev-auto','cs','k-auto','h','x','{}'::jsonb,'{}'::jsonb,2)"))
        conn.execute(text(
            "INSERT INTO runs (id, case_id, triggering_event_id, state) "
            "VALUES ('r-auto','cs','ev-auto','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('d-auto','cs','r-auto','manual_review_insufficient',40,'{}'::jsonb,'enabled',"
            "'{}'::jsonb,false,1)"))

    with eng.connect() as conn:
        row = conn.execute(text(
            "SELECT latest_decision_row_id, latest_manual_decision_row_id FROM cases "
            "WHERE id='cs'")).one()
    assert row.latest_decision_row_id == "d-auto"        # the verdict moved on
    assert row.latest_manual_decision_row_id == "d-manual"  # attribution did not
    eng.dispose()
