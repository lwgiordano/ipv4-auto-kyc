"""PR 7b-core hardening revision 015 (re-audit `4dfdf8a` F2/F3/F5).

`014` made the witness tables exist; `015` makes them an authority: raw SQL can no longer
manufacture, mutate, or destroy witness evidence, the amended-history validator compares exact
normalized definitions rather than substrings, and the downgrade's lock order cannot deadlock
with a live writer.
"""

import threading
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migration_014 import _AMENDED_013_DDL, _seed_callback
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def _claimed_pending(conn, case_id, run_id, seq, *, with_attempt=True):
    """A live-claimed pending callback (+ optionally its staged attempt): the legal start."""
    oid = _seed_callback(conn, case_id, run_id, seq, "pending")
    row = conn.execute(text(
        "UPDATE outbox SET claim_token = gen_random_uuid(), "
        "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
        "WHERE id = :i RETURNING id, claim_token"), {"i": oid}).one()
    if with_attempt:
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})
    return row


# --- F2: the fabrication matrix — every path raw SQL had is now refused by the database ---

@pytest.mark.parametrize("name, setup_sql, tamper_sql, must_match", [
    ("attempt-for-unclaimed-row", None,
     "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, wire_version, "
     "request_sha256) VALUES (gen_random_uuid(), {oid}, gen_random_uuid(), 'legacy', '{sha}')",
     "not claimed by"),
    ("attempt-under-wrong-token", "claim",
     "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, wire_version, "
     "request_sha256) VALUES (gen_random_uuid(), {oid}, gen_random_uuid(), 'legacy', '{sha}')",
     "not claimed by"),
    ("generation-flip-to-legacy", "gen-attempt-v1",
     "UPDATE outbox SET witness_generation = 'legacy' WHERE id = {oid}",
     "witness_generation is immutable"),
    ("generation-flip-to-attempt-v1", "force-legacy",
     "UPDATE outbox SET witness_generation = 'attempt_v1' WHERE id = {oid}",
     "witness_generation is immutable"),
    ("arbitrary-digest-on-pending", None,
     "UPDATE outbox SET callback_wire_sha256 = '{sha}', wire_version = 'legacy' WHERE id = {oid}",
     "pending->delivered"),
])
def test_witness_fabrication_refused(pg, name, setup_sql, tamper_sql, must_match):
    url = _fresh_db(pg, f"kyc_mig_015_fab_{name.replace('-', '_')[:24]}")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "cf", "cf-r1", 1, "pending")
        if setup_sql == "gen-attempt-v1":
            # generation is INSERT-time-only (flips are the thing under test), so rebuild the
            # outbox row as attempt_v1: delete the seeded one and insert its replacement
            conn.execute(text("DELETE FROM outbox WHERE id = :i"), {"i": oid})
            oid = conn.execute(text(
                "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
                "status, payload_json, witness_generation) VALUES "
                "('decision_callback','cf','cf-r1','decision',1,'pending','{}'::jsonb,"
                "'attempt_v1') RETURNING id")).scalar_one()
        elif setup_sql == "claim":
            conn.execute(text(
                "UPDATE outbox SET claim_token = gen_random_uuid(), "
                "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
                "WHERE id = :i"), {"i": oid})
    with pytest.raises(Exception, match=must_match), eng.begin() as conn:
        conn.execute(text(tamper_sql.format(oid=oid, sha="d" * 64)))
    eng.dispose()


def test_delivered_digest_can_never_be_cleared_or_rewritten(pg):
    """THE conversion attack: deliver legitimately, let retention prune the now-redundant
    attempt, then clear the digest — the row would classify not_accepted despite local
    acceptance. Both the clear and the rewrite die at the trigger, before and after pruning."""
    url = _fresh_db(pg, "kyc_mig_015_writeonce")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cw", "cw-r1", 1)
        conn.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now(), "
            "callback_wire_sha256=:s, wire_version='legacy', "
            "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
            "WHERE id=:i"), {"s": "a" * 64, "i": row.id})

    for tamper in (
        "UPDATE outbox SET callback_wire_sha256 = NULL, wire_version = NULL WHERE id = :i",
        "UPDATE outbox SET callback_wire_sha256 = :other WHERE id = :i",
    ):
        with pytest.raises(Exception, match="write-once"), eng.begin() as conn:
            conn.execute(text(tamper), {"i": row.id, "other": "e" * 64})

    with eng.begin() as conn:  # retention's legal path still works: the attempt is redundant
        deleted = conn.execute(text(
            "DELETE FROM outbox_delivery_attempts a USING outbox o "
            "WHERE o.id = a.outbox_id AND o.callback_wire_sha256 IS NOT NULL")).rowcount
    assert deleted == 1
    with pytest.raises(Exception, match="write-once"), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET callback_wire_sha256 = NULL, wire_version = NULL WHERE id = :i"),
            {"i": row.id})
    eng.dispose()


def test_terminal_digest_requires_matching_attempt_at_the_database(pg):
    """Defense in depth below the application fence: the pending->delivered UPDATE itself is
    refused when no attempt under the live claim matches the digest/encoding — wrong digest,
    wrong encoding, or an attempt staged under another row all fail."""
    url = _fresh_db(pg, "kyc_mig_015_terminal")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "ct", "ct-r1", 1)          # attempt sha = 'a'*64, legacy
    terminal = (
        "UPDATE outbox SET status='delivered', delivered_at=now(), "
        "callback_wire_sha256=:s, wire_version=:v, "
        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"
    )
    for sha, version in (("f" * 64, "legacy"), ("a" * 64, "sequenced")):
        with pytest.raises(Exception, match="no attempt"), eng.begin() as conn:
            conn.execute(text(terminal), {"s": sha, "v": version, "i": row.id})
    with eng.begin() as conn:  # the matching one succeeds
        conn.execute(text(terminal), {"s": "a" * 64, "v": "legacy", "i": row.id})
    eng.dispose()


# --- F3: the validator compares exact definitions, complete sets, bound to the schema ---

@pytest.mark.parametrize("name, mutilate, db", [
    ("weakened-same-name-check",
     ["ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT ck_attempt_wire_vocab",
      "ALTER TABLE outbox_delivery_attempts ADD CONSTRAINT ck_attempt_wire_vocab "
      "CHECK (wire_version IS NOT NULL)"],
     "kyc_mig_015_weak_check"),
    ("weakened-sha-check",
     ["ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT ck_attempt_sha_shape",
      "ALTER TABLE outbox_delivery_attempts ADD CONSTRAINT ck_attempt_sha_shape "
      "CHECK (request_sha256 IS NOT NULL)"],
     "kyc_mig_015_weak_sha"),
    ("extra-unique-breaks-retries",
     ["ALTER TABLE outbox_delivery_attempts ADD CONSTRAINT uq_sneaky UNIQUE (outbox_id)"],
     "kyc_mig_015_uniq"),
    ("wrong-fk-action",
     ["ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT fk_attempt_outbox",
      "ALTER TABLE outbox_delivery_attempts ADD CONSTRAINT fk_attempt_outbox "
      "FOREIGN KEY (outbox_id) REFERENCES outbox(id)"],
     "kyc_mig_015_fk"),
    ("extra-user-trigger",
     ["CREATE FUNCTION sneaky() RETURNS trigger LANGUAGE plpgsql AS "
      "$$ BEGIN RETURN NEW; END $$",
      "CREATE TRIGGER trg_sneaky BEFORE INSERT ON outbox_delivery_attempts "
      "FOR EACH ROW EXECUTE FUNCTION sneaky()"],
     "kyc_mig_015_trig"),
    ("widened-index",
     ["DROP INDEX ix_attempt_outbox",
      "CREATE UNIQUE INDEX ix_attempt_outbox ON outbox_delivery_attempts (outbox_id)"],
     "kyc_mig_015_idx"),
])
def test_015_refuses_noncanonical_authority(pg, name, mutilate, db):
    """Each mutilation kept 014's names — and each passed 014's substring validation. 015's
    exact-definition comparison refuses all of them before creating any trigger."""
    url = _fresh_db(pg, db)
    cfg = _config(url)
    command.upgrade(cfg, "014")
    eng = create_engine(url)
    with eng.begin() as conn:
        for ddl in mutilate:
            conn.execute(text(ddl))
    with pytest.raises(Exception, match="MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"):
        command.upgrade(cfg, "015")
    with eng.connect() as conn:  # stamped at the prior revision; nothing half-applied
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "014"
        assert conn.execute(text(
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_outbox_attempts_admission'"
        )).scalar_one() == 0
    eng.dispose()


def test_015_rejects_a_shadow_schema_table(pg):
    """A same-named table in another schema on the search path must not satisfy — or poison —
    the validation, which is bound to current_schema()."""
    url = _fresh_db(pg, "kyc_mig_015_shadow")
    cfg = _config(url)
    command.upgrade(cfg, "014")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("CREATE SCHEMA shadow"))
        conn.execute(text(
            "CREATE TABLE shadow.outbox_delivery_attempts (attempt_id uuid PRIMARY KEY)"))
    command.upgrade(cfg, "015")  # succeeds: the REAL table validates; the shadow is invisible
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "015"
    eng.dispose()


def test_amended_and_canonical_histories_still_adopt_through_015(pg):
    """The two legitimate 013 histories both reach head with the full authority active."""
    for db, amended in (("kyc_mig_015_canon", False), ("kyc_mig_015_amend", True)):
        url = _fresh_db(pg, db)
        cfg = _config(url)
        command.upgrade(cfg, "013")
        eng = create_engine(url)
        if amended:
            with eng.begin() as conn:
                for ddl in _AMENDED_013_DDL:
                    conn.execute(text(ddl))
        command.upgrade(cfg, "head")
        with eng.begin() as conn:
            assert conn.execute(text(
                "SELECT count(*) FROM pg_trigger WHERE tgname IN "
                "('trg_outbox_attempts_admission','trg_outbox_witness_guard')"
            )).scalar_one() == 2
            _claimed_pending(conn, "ok", "ok-r1", 1)  # the legal write path works end to end
        eng.dispose()


# --- F5: the downgrade lock order cannot deadlock with a live attempt writer ---

def test_015_downgrade_and_live_attempt_writer_never_deadlock(pg):
    """The writer's order is child-table-write then parent-row (admission trigger FOR UPDATE);
    015's downgrade now locks child-first too, so the two can only QUEUE, never cycle. The writer
    holds both relations mid-transaction while the downgrade starts; assert the downgrade blocks
    (no 40P01), then — the attempt having committed first — refuses on the witness."""
    url = _fresh_db(pg, "kyc_mig_015_deadlock")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cd", "cd-r1", 1, with_attempt=False)

    result: dict = {}

    def run_downgrade():
        try:
            command.downgrade(cfg, "014")
            result["outcome"] = "downgraded"
        except Exception as exc:  # noqa: BLE001 — the outcome IS the assertion
            result["outcome"] = str(exc)

    writer = create_engine(url)
    conn = writer.connect()
    t = threading.Thread(target=run_downgrade, name="downgrade-015")
    try:
        tx = conn.begin()
        token = conn.execute(text(
            "SELECT claim_token FROM outbox WHERE id = :i"), {"i": row.id}).scalar_one()
        conn.execute(text(  # holds child RowExclusive AND the parent row lock (trigger)
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": token, "s": "b" * 64})
        t.start()
        time.sleep(2)
        assert t.is_alive(), "downgrade must QUEUE behind the writer, not proceed"
        tx.commit()
    finally:
        t.join(timeout=30)
        conn.close()
        writer.dispose()
    assert not t.is_alive()
    assert "40P01" not in result["outcome"] and "deadlock" not in result["outcome"].lower()
    assert "MIGRATION_015_DOWNGRADE_REFUSED_WITNESS_IN_USE" in result["outcome"]
    eng.dispose()


def test_unused_database_round_trips_through_015(pg):
    url = _fresh_db(pg, "kyc_mig_015_roundtrip")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "012")
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "015"
    eng.dispose()


def test_wrong_fk_target_refused(pg):
    """FK pointing at a different table entirely — same name, wrong REFERENCES — is refused."""
    url = _fresh_db(pg, "kyc_mig_015_fk_target")
    cfg = _config(url)
    command.upgrade(cfg, "014")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text(
            "ALTER TABLE outbox_delivery_attempts DROP CONSTRAINT fk_attempt_outbox"))
        conn.execute(text(
            "ALTER TABLE outbox_delivery_attempts ADD COLUMN sneak_case text"))
    with pytest.raises(Exception, match="MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"):
        command.upgrade(cfg, "015")
    eng.dispose()
