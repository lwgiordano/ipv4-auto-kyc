"""PR 7b-core authority-boundary revision 017 (re-audit `15d875d` F1/F3/F4/F5 + folded F2).

`016` bound admission to provenance; `15d875d` walked the remaining lifecycle edges: an expired
claimant could still stage evidence, a delivered row could be born rather than earned, the
ordering authority itself could be deleted or re-identified, and maintenance still reasoned
about writer lock ORDER instead of sharing a fence with writers. `017` closes each at the
database and takes one shared advisory fence for maintenance and witness writers.
"""

import threading
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migration_015 import _claimed_pending
from tests.integration.test_migrations import _config, _fresh_db, _mk_auto_decision, _mk_case

pytestmark = pytest.mark.postgres

_FENCE_KEY = 720170001


def _deliver_legally(conn, row, sha="a" * 64):
    """The full front door: terminal UPDATE matching the admitted attempt under the live claim."""
    conn.execute(text(
        "UPDATE outbox SET status='delivered', delivered_at=now(), "
        "callback_wire_sha256=:s, wire_version='legacy', "
        "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
        "WHERE id=:i"), {"s": sha, "i": row.id})


# --- F4: an expired lease is reclaimable, and a reclaimable claim stages NOTHING ---

def test_expired_lease_cannot_stage_evidence(pg):
    """The claim SQL already treats an expired lease as reclaimable; admission now agrees. The
    still-set token of an expired claimant admits no attempt — and the moment the lease is
    honestly live again, the same INSERT is admitted with provenance."""
    url = _fresh_db(pg, "kyc_mig_017_lease")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cl", "cl-r1", 1, with_attempt=False)
        conn.execute(text(
            "UPDATE outbox SET claim_lease_expires_at = now() - interval '1 second' "
            "WHERE id = :i"), {"i": row.id})

    with pytest.raises(Exception, match="lease expired"), eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})

    with eng.begin() as conn:  # the unexpired contrast: same token, live lease → admitted
        conn.execute(text(
            "UPDATE outbox SET claim_lease_expires_at = now() + interval '1 hour' "
            "WHERE id = :i"), {"i": row.id})
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})
        adm = conn.execute(text(
            "SELECT admission FROM outbox_delivery_attempts WHERE outbox_id=:o"),
            {"o": row.id}).scalar_one()
    assert adm == "admission_v1"
    eng.dispose()


# --- F1: delivered is EARNED, never born; and only from pending ---

def test_decision_callback_cannot_be_born_anywhere_but_pending(pg):
    """Every UPDATE guard is bypassable by INSERTing the end state directly — so the INSERT
    itself is guarded: a decision callback enters pending, unwitnessed, unclaimed, unresolved."""
    url = _fresh_db(pg, "kyc_mig_017_born")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)

    head = ("INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "payload_json, ")
    for fabrication_cols, fabrication_vals in [
        ("status, delivered_at", "'delivered', now()"),                    # born-delivered
        ("status", "'dead'"),                                              # born-dead
        ("status, callback_wire_sha256, wire_version",
         f"'pending', '{'a' * 64}', 'legacy'"),                            # born-witnessed
        ("status, claim_token, claim_lease_expires_at, claimed_by",
         "'pending', gen_random_uuid(), now() + interval '1 hour', 'w'"),  # born-claimed
        ("status, resolved_at", "'pending', now()"),                       # born-resolved
    ]:
        with pytest.raises(Exception, match="enter the outbox pending"), eng.begin() as conn:
            conn.execute(text(
                head + fabrication_cols + ") VALUES ('decision_callback','c1','rA','decision',1,"
                "'{}'::jsonb, " + fabrication_vals + ")"))
    eng.dispose()


def test_dead_to_delivered_is_closed(pg):
    """`delivered` is reachable only from `pending`: a dead row re-entering the delivered state
    would mint acceptance out of a terminal failure. (018 states the same rule through its
    complete per-kind transition matrix, so the refusal message differs above 017 — this case is
    pinned to the revision that introduced it.)"""
    url = _fresh_db(pg, "kyc_mig_017_deadpath")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cd", "cd-r1", 1, with_attempt=False)
        conn.execute(text(
            "UPDATE outbox SET status='dead', claim_token=NULL, claim_lease_expires_at=NULL, "
            "claimed_by=NULL WHERE id=:i"), {"i": row.id})
    with pytest.raises(Exception, match="reachable only from pending"), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now() WHERE id=:i"),
            {"i": row.id})
    eng.dispose()


# --- F3: the ordering authority cannot be deleted or re-identified ---

def test_decision_callbacks_are_undeletable(pg):
    """Positive evidence (delivered + digest), negative evidence (attempt_v1, nothing staged),
    and plain pending rows are all the durable per-case ordering record; DELETE is refused for
    every one of them. poc_email retention deletion stays legal."""
    url = _fresh_db(pg, "kyc_mig_017_nodelete")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        negative = _seed_callback(conn, "cn", "cn-r1", 1, "pending", generation="attempt_v1")
        row = _claimed_pending(conn, "cn", "cn-r2", 2)
        _deliver_legally(conn, row)
        # retention's legal pass: the attempt is redundant once the digest exists…
        conn.execute(text("DELETE FROM outbox_delivery_attempts WHERE outbox_id=:o"),
                     {"o": row.id})
        conn.execute(text(
            "INSERT INTO outbox (kind, case_id, ordering_stream, payload_json, status, "
            "delivered_at) VALUES ('poc_email','cn','email','{}'::jsonb,'delivered',now())"))

    for target in (negative, row.id):  # …but neither callback row itself may go
        with pytest.raises(Exception, match="deletion refused"), eng.begin() as conn:
            conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": target})

    with eng.begin() as conn:
        assert conn.execute(text(
            "DELETE FROM outbox WHERE kind='poc_email'")).rowcount == 1
    eng.dispose()


def test_outbox_identity_is_immutable(pg):
    """kind/case_id/run_id/ordering_stream/decision_sequence ARE the ordering authority;
    rewriting any of them re-attributes recorded history."""
    url = _fresh_db(pg, "kyc_mig_017_identity")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "ci", "ci-r1", 1, "pending")

    for tamper in (
        "kind = 'poc_email'",
        "case_id = 'someone-else'",
        "run_id = 'other-run'",
        "ordering_stream = 'email'",
        "decision_sequence = 99",
    ):
        with pytest.raises(Exception, match="identity is immutable"), eng.begin() as conn:
            conn.execute(text(f"UPDATE outbox SET {tamper} WHERE id = :i"), {"i": oid})
    eng.dispose()


def test_payload_immutable_while_sendable_redactable_once_terminal(pg):
    """The payload is the exact body a claimant would transmit: while the row can still be sent
    (pending/dead) it cannot change AT ALL — not even to the redaction value. Once delivered,
    the ONLY legal rewrite is the governed redaction; arbitrary terminal rewrites stay refused."""
    url = _fresh_db(pg, "kyc_mig_017_payload")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        pending = _seed_callback(conn, "cp", "cp-r1", 1, "pending")
        row = _claimed_pending(conn, "cp", "cp-r2", 2)
        _deliver_legally(conn, row)

    for target, value in (
        (pending, "'{\"forged\": true}'::jsonb"),
        (pending, "'{\"redacted\": true}'::jsonb"),   # redaction is for TERMINAL rows only
        (row.id, "'{\"forged\": true}'::jsonb"),
    ):
        with pytest.raises(Exception, match="payload"), eng.begin() as conn:
            conn.execute(text(
                f"UPDATE outbox SET payload_json = {value} WHERE id = :i"), {"i": target})

    with eng.begin() as conn:  # the one governed rewrite
        conn.execute(text(
            "UPDATE outbox SET payload_json = '{\"redacted\": true}'::jsonb WHERE id = :i"),
            {"i": row.id})
        assert conn.execute(text(
            "SELECT payload_json->>'redacted' FROM outbox WHERE id=:i"),
            {"i": row.id}).scalar_one() == "true"
    eng.dispose()


# --- F5: ONE fence for maintenance and writers; quiescence is machine-checked ---

def test_upgrade_refuses_while_claims_are_live(pg):
    """The cutover's 'publishers drained' precondition is enforced, not assumed: a live
    (unexpired) claim refuses the upgrade with the documented sentinel; released, it proceeds."""
    url = _fresh_db(pg, "kyc_mig_017_quiesce")
    cfg = _config(url)
    command.upgrade(cfg, "016")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cq", "cq-r1", 1, with_attempt=False)

    with pytest.raises(RuntimeError, match="MIGRATION_017_PREFLIGHT_LIVE_CLAIMS"):
        command.upgrade(cfg, "017")

    with eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
            "WHERE id=:i"), {"i": row.id})
    command.upgrade(cfg, "017")
    with eng.connect() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "017"
    eng.dispose()


def test_017_downgrade_and_fenced_writer_never_deadlock(pg):
    """The writer takes the advisory fence SHARED then writes child+parent; the downgrade takes
    it EXCLUSIVE before any table lock. They can only QUEUE — the downgrade waits at the fence,
    proceeds when the writer commits, and then refuses on the witness the writer just wrote."""
    url = _fresh_db(pg, "kyc_mig_017_fence")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cf", "cf-r1", 1, with_attempt=False)

    result: dict = {}

    def run_downgrade():
        try:
            command.downgrade(cfg, "016")
            result["outcome"] = "downgraded"
        except Exception as exc:  # noqa: BLE001 — the outcome IS the assertion
            result["outcome"] = str(exc)

    writer = create_engine(url)
    conn = writer.connect()
    t = threading.Thread(target=run_downgrade, name="downgrade-017")
    try:
        tx = conn.begin()
        conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": _FENCE_KEY})
        conn.execute(text(  # the publisher's exact order: fence, then child write + parent lock
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "b" * 64})
        t.start()
        time.sleep(2)
        assert t.is_alive(), "downgrade must QUEUE at the fence behind the writer, not proceed"
        tx.commit()
    finally:
        t.join(timeout=30)
        conn.close()
        writer.dispose()
    assert not t.is_alive()
    assert "40P01" not in result["outcome"] and "deadlock" not in result["outcome"].lower()
    assert "MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE" in result["outcome"]
    eng.dispose()


# --- F2 (folded part): full structural validation; functions resolve ONE schema ---

def test_upgrade_refuses_on_structural_drift(pg):
    """The validator covers the complete structural surface BEFORE recreating any code: a
    missing index and a stray enabled trigger each refuse with the shared mismatch sentinel."""
    for name, drift_sql in (
        ("idx", "DROP INDEX ix_attempt_outbox"),
        ("trig",
         "CREATE FUNCTION zz_noop() RETURNS trigger LANGUAGE plpgsql AS "
         "$$ BEGIN RETURN NEW; END $$; "
         "CREATE TRIGGER zz_extra BEFORE INSERT ON outbox_delivery_attempts "
         "FOR EACH ROW EXECUTE FUNCTION zz_noop()"),
    ):
        url = _fresh_db(pg, f"kyc_mig_017_drift_{name}")
        cfg = _config(url)
        command.upgrade(cfg, "016")
        eng = create_engine(url)
        with eng.begin() as conn:
            conn.execute(text(drift_sql))
        with pytest.raises(RuntimeError, match="MIGRATION_014_ATTEMPT_AUTHORITY_MISMATCH"):
            command.upgrade(cfg, "head")
        eng.dispose()


def test_authority_functions_ignore_the_session_search_path(pg):
    """Same-named tables in a schema EARLIER on the caller's search path must be invisible to
    the guards: every authority function pins search_path and qualifies its relations, so the
    admission read and the terminal-witness EXISTS consult the real public tables."""
    url = _fresh_db(pg, "kyc_mig_017_shadow")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "cs", "cs-r1", 1, with_attempt=False)
        conn.execute(text("CREATE SCHEMA shadow"))
        conn.execute(text(  # empty doppelgängers with every column the guards consult
            "CREATE TABLE shadow.outbox (id bigint, kind text, status text, claim_token uuid, "
            "claimed_by text, claim_lease_expires_at timestamptz, callback_wire_sha256 text)"))
        conn.execute(text(
            "CREATE TABLE shadow.outbox_delivery_attempts (attempt_id uuid, outbox_id bigint, "
            "claim_token uuid, wire_version text, request_sha256 text, admission text)"))

    with eng.begin() as conn:
        conn.execute(text("SET LOCAL search_path TO shadow, public"))
        # unpinned, the admission trigger would look up the parent in shadow.outbox (empty)
        # and refuse; pinned, it admits against the real row
        conn.execute(text(
            "INSERT INTO public.outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})
        # unpinned, the witness EXISTS would scan shadow.outbox_delivery_attempts (empty)
        # and refuse the legal terminal
        conn.execute(text(
            "UPDATE public.outbox SET status='delivered', delivered_at=now(), "
            "callback_wire_sha256=:s, wire_version='legacy', "
            "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
            "WHERE id=:i"), {"s": "a" * 64, "i": row.id})
    with eng.connect() as conn:
        assert conn.execute(text(
            "SELECT status FROM outbox WHERE id=:i"), {"i": row.id}).scalar_one() == "delivered"
    eng.dispose()


def test_unused_database_round_trips_through_017(pg):
    """017's own round trip, pinned to 017: `018` is forward-only once installed, so the walk
    below it is closed by design and is proven refused in test_migration_018.py."""
    url = _fresh_db(pg, "kyc_mig_017_roundtrip")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    command.downgrade(cfg, "012")
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    with eng.begin() as conn:
        assert conn.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "017"
        row = _claimed_pending(conn, "cr", "cr-r1", 1)  # the full legal write path still works
        _deliver_legally(conn, row)
    eng.dispose()
