"""PR 7b-core repair revision 014 (re-audit `1f8412e` F1/F2/F3).

`013` was amended in place after it was committed, so two `013` histories exist in the wild:
the canonical one (no attempt table) and the briefly-published amended one (attempt table
included). `014` must repair BOTH into one schema — creating the attempt authority where it is
absent, adopting it only after exhaustive validation where it is present, and refusing a
malformed copy — and must be forward-only the moment any witness evidence exists.
"""

import threading
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres

# The exact DDL the briefly-published amended `013` executed — a database that ran it carries
# this table under a `013` stamp. Kept verbatim so the adoption test exercises the real artifact.
_AMENDED_013_DDL = [
    """
    CREATE TABLE outbox_delivery_attempts (
        attempt_id UUID NOT NULL,
        outbox_id BIGINT NOT NULL,
        claim_token UUID NOT NULL,
        wire_version TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        attempted_at TIMESTAMPTZ DEFAULT now() NOT NULL,
        PRIMARY KEY (attempt_id),
        CONSTRAINT fk_attempt_outbox FOREIGN KEY (outbox_id) REFERENCES outbox (id) ON DELETE CASCADE,
        CONSTRAINT ck_attempt_sha_shape CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_attempt_wire_vocab CHECK (wire_version IN ('legacy','sequenced'))
    )
    """,
    "CREATE INDEX ix_attempt_outbox ON outbox_delivery_attempts (outbox_id, attempted_at DESC)",
]


def _seed_callback(conn, case_id, run_id, seq, status, *, delivered=False):
    """One valid case→event→run→decision→callback chain, callback in `status`."""
    conn.execute(text("INSERT INTO cases (id, last_decision_sequence) VALUES (:c, :s) "
                      "ON CONFLICT (id) DO UPDATE SET last_decision_sequence = :s"),
                 {"c": case_id, "s": seq})
    conn.execute(text(
        "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
        "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"),
        {"e": f"{run_id}-ev", "c": case_id, "k": run_id, "s": seq})
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                      "VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
                 {"r": run_id, "c": case_id, "e": f"{run_id}-ev"})
    conn.execute(text(
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES "
        "(:d,:c,:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,:s)"),
        {"d": f"{run_id}-d", "c": case_id, "r": run_id, "s": seq})
    return conn.execute(text(
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status, "
        "delivered_at, payload_json) VALUES ('decision_callback',:c,:r,'decision',:s,:st,"
        + ("now()" if delivered else "NULL") + ",'{}'::jsonb) RETURNING id"),
        {"c": case_id, "r": run_id, "s": seq, "st": status}).scalar_one()


def _witness(conn, run_id):
    from kyc_tool.outbox import witness
    return conn.execute(
        text(witness.WITNESS_SELECT + " AND o.run_id = :r"), {"r": run_id}
    ).one().witness


def test_014_creates_attempt_authority_on_canonical_013(pg):
    """The main repair path: a database stamped by the CANONICAL 013 (no attempt table) gets the
    table at 014, and the full head schema is usable."""
    url = _fresh_db(pg, "kyc_mig_014_canonical")
    cfg = _config(url)
    command.upgrade(cfg, "013")
    eng = create_engine(url)
    with eng.connect() as conn:
        assert not conn.execute(
            text("SELECT to_regclass('outbox_delivery_attempts') IS NOT NULL")).scalar_one()
    command.upgrade(cfg, "014")
    with eng.begin() as conn:
        assert conn.execute(
            text("SELECT to_regclass('outbox_delivery_attempts') IS NOT NULL")).scalar_one()
        # the schema is genuinely usable end-to-end: a chain inserts, an attempt inserts
        oid = _seed_callback(conn, "c14a", "c14a-r1", 1, "pending")
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, gen_random_uuid(), "
            "'legacy', :s)"), {"o": oid, "s": "a" * 64})
    eng.dispose()


def test_014_adopts_a_valid_amended_013_table(pg):
    """A database that ran the briefly-published AMENDED 013 already has the exact table. 014
    must adopt it — with its data — rather than fail or recreate it."""
    url = _fresh_db(pg, "kyc_mig_014_amended")
    cfg = _config(url)
    command.upgrade(cfg, "013")
    eng = create_engine(url)
    with eng.begin() as conn:
        for ddl in _AMENDED_013_DDL:
            conn.execute(text(ddl))
        oid = _seed_callback(conn, "c14b", "c14b-r1", 1, "pending")
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, gen_random_uuid(), "
            "'legacy', :s)"), {"o": oid, "s": "b" * 64})
    command.upgrade(cfg, "014")
    with eng.connect() as conn:
        # data survived adoption, and the F2 backfill classified the attempt-bearing row
        assert conn.execute(text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one() == 1
        assert conn.execute(text(
            "SELECT witness_generation FROM outbox WHERE run_id='c14b-r1'")).scalar_one() == "attempt_v1"
    eng.dispose()


# one database PER param: a refused alembic upgrade leaves its engine's pooled connection open
# in this process, so reusing a name across params fails the next DROP DATABASE with ObjectInUse.
@pytest.mark.parametrize("mutilate, must_name, db", [
    ("ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT ck_attempt_sha_shape",
     "ck_attempt_sha_shape", "kyc_mig_014_bad_check"),
    ("ALTER TABLE outbox_delivery_attempts ALTER COLUMN claim_token DROP NOT NULL",
     "columns", "kyc_mig_014_bad_null"),
    ("DROP INDEX ix_attempt_outbox", "ix_attempt_outbox", "kyc_mig_014_bad_index"),
    ("ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT fk_attempt_outbox",
     "fk_attempt_outbox", "kyc_mig_014_bad_fk"),
])
def test_014_refuses_a_malformed_partial_table(pg, mutilate, must_name, db):
    """A half-right evidence table is worse than none: evidence written into it cannot be
    trusted later. Each mutilation must be refused BY NAME, before any DDL."""
    url = _fresh_db(pg, db)
    cfg = _config(url)
    command.upgrade(cfg, "013")
    eng = create_engine(url)
    with eng.begin() as conn:
        for ddl in _AMENDED_013_DDL:
            conn.execute(text(ddl))
        conn.execute(text(mutilate))
    with pytest.raises(Exception, match="MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH") as exc:
        command.upgrade(cfg, "014")
    assert must_name in str(exc.value)
    eng.dispose()


def test_pre_authority_pending_and_dead_rows_are_legacy_not_not_accepted(pg):
    """Re-audit F2 — THE false-assertion case. Before the attempt authority, the publisher sent
    HTTP before its delivered stamp, so a pre-014 pending/dead row may represent bytes the
    platform ACCEPTED. After the repair they must classify legacy_unwitnessed — never
    not_accepted, which asserts non-delivery — while a row created UNDER the attempt regime with
    no attempt genuinely is not_accepted. Driven through real migrations and the public query."""
    url = _fresh_db(pg, "kyc_mig_014_f2")
    cfg = _config(url)
    command.upgrade(cfg, "013")
    eng = create_engine(url)
    with eng.begin() as conn:  # 2xx-before-stamp survivors, seeded at schema 013
        _seed_callback(conn, "cf2", "cf2-r1", 1, "pending")
        _seed_callback(conn, "cf2", "cf2-r2", 2, "dead")
        _seed_callback(conn, "cf2", "cf2-r3", 3, "delivered", delivered=True)
    command.upgrade(cfg, "head")  # the witness query is head code; it needs the head schema
    with eng.begin() as conn:
        assert _witness(conn, "cf2-r1") == "legacy_unwitnessed"
        assert _witness(conn, "cf2-r2") == "legacy_unwitnessed"
        assert _witness(conn, "cf2-r3") == "legacy_unwitnessed"
        # a post-014 row created under the attempt regime, never staged: the real not_accepted
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('cf2-r4-ev','cf2','cf2-r4','h','x','{}'::jsonb,'{}'::jsonb,4)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('cf2-r4','cf2','cf2-r4-ev','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('cf2-r4-d','cf2','cf2-r4','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,4)"))
        conn.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status, payload_json, witness_generation) VALUES "
            "('decision_callback','cf2','cf2-r4','decision',4,'pending','{}'::jsonb,'attempt_v1')"))
    with eng.begin() as conn:
        assert _witness(conn, "cf2-r4") == "not_accepted"
    eng.dispose()


def test_014_downgrade_refuses_when_an_attempt_is_the_sole_evidence(pg):
    """Re-audit F3: an attempt-only pending row is exactly the send-before-stamp survivor; the
    attempt is its ONLY evidence. Downgrade must refuse with the stable sentinel."""
    url = _fresh_db(pg, "kyc_mig_014_f3a")
    cfg = _config(url)
    command.upgrade(cfg, "014")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "cf3", "cf3-r1", 1, "pending")
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, gen_random_uuid(), "
            "'legacy', :s)"), {"o": oid, "s": "c" * 64})
    with pytest.raises(Exception, match="MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE"):
        command.downgrade(cfg, "013")
    with eng.connect() as conn:  # schema AND evidence intact after the refusal
        assert conn.execute(text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one() == 1
        assert conn.execute(text(
            "SELECT version_num FROM alembic_version")).scalar_one() == "014"
    eng.dispose()


def test_014_downgrade_refuses_on_a_terminal_digest_and_013_refuses_too(pg):
    """A delivered row's digest is the exact accepted-bytes record; neither 014 nor 013 may
    destroy it. 014 refuses first; 013's own witness guard is exercised directly on a database
    whose digest was written at schema 013 (no attempt table involved)."""
    url = _fresh_db(pg, "kyc_mig_014_f3b")
    cfg = _config(url)
    command.upgrade(cfg, "013")
    eng = create_engine(url)
    with eng.begin() as conn:
        _seed_callback(conn, "cf3b", "cf3b-r1", 1, "delivered", delivered=True)
        conn.execute(text(
            "UPDATE outbox SET callback_wire_sha256 = :s, wire_version = 'legacy' "
            "WHERE run_id = 'cf3b-r1'"), {"s": "d" * 64})
    # still stamped 013: its own downgrade guard must refuse on the digest
    with pytest.raises(Exception, match="MIGRATION_013_DOWNGRADE_REFUSED_WITNESS_IN_USE"):
        command.downgrade(cfg, "012")
    command.upgrade(cfg, "014")
    with pytest.raises(Exception, match="MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE"):
        command.downgrade(cfg, "013")
    eng.dispose()


def test_013_downgrade_refuses_an_amended_history_it_cannot_govern(pg):
    """A database stamped '013' by the AMENDED file carries the attempt table but never applied
    014. 013's downgrade must not run underneath it and strand an orphan evidence table."""
    url = _fresh_db(pg, "kyc_mig_014_amended_refuse")
    cfg = _config(url)
    command.upgrade(cfg, "013")
    eng = create_engine(url)
    with eng.begin() as conn:
        for ddl in _AMENDED_013_DDL:
            conn.execute(text(ddl))
    with pytest.raises(Exception, match="MIGRATION_013_DOWNGRADE_REFUSED_AMENDED_HISTORY"):
        command.downgrade(cfg, "012")
    eng.dispose()


def test_unused_database_round_trips_head_012_head(pg):
    """With zero witnesses the full head→012→head path stays clean — forward-only applies to
    evidence, not to an unused schema."""
    url = _fresh_db(pg, "kyc_mig_014_roundtrip")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "012")
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT to_regclass('outbox_delivery_attempts') IS NOT NULL")).scalar_one()
    eng.dispose()


def test_014_downgrade_preflight_cannot_race_a_concurrent_attempt(pg):
    """TOCTOU (same class as 013's superseded preflight): without the ACCESS EXCLUSIVE lock on
    the attempts table, an attempt could commit between the count and the DROP and be destroyed
    unseen. The lock makes the concurrent insert WAIT; committed first, it must then refuse."""
    url = _fresh_db(pg, "kyc_mig_014_race")
    cfg = _config(url)
    command.upgrade(cfg, "014")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "cr", "cr-r1", 1, "pending")

    blocker = create_engine(url)
    conn = blocker.connect()
    result: dict = {}

    def run_downgrade():
        try:
            command.downgrade(cfg, "013")
            result["outcome"] = "downgraded"
        except Exception as exc:  # noqa: BLE001 — the outcome IS the assertion
            result["outcome"] = str(exc)

    t = threading.Thread(target=run_downgrade, name="downgrade-014")
    try:
        tx = conn.begin()
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, gen_random_uuid(), "
            "'legacy', :s)"), {"o": oid, "s": "e" * 64})  # uncommitted: holds ROW EXCLUSIVE
        t.start()
        time.sleep(2)
        assert t.is_alive(), "downgrade should be BLOCKED on the attempts-table lock"
        tx.commit()  # the attempt lands first...
    finally:
        t.join(timeout=30)
        conn.close()
        blocker.dispose()
    assert not t.is_alive(), "downgrade thread still running after join"
    # ...so the preflight sees it and refuses: evidence beats the downgrade, deterministically.
    assert "MIGRATION_014_DOWNGRADE_REFUSED_WITNESS_IN_USE" in result["outcome"]
    eng.dispose()
