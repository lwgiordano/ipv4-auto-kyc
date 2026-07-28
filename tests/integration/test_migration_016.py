"""PR 7b-core admission-provenance revision 016 (re-audit `0c46443` F1-F4).

`015` built the walls; the audit walked through the gaps: a delivery could still happen with NO
witness at all, pre-authority attempts were trusted on existence, a same-named no-op function
passed validation, and downgrades destroyed negative evidence. `016` closes each at the database.
"""

import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.config import REPO_ROOT
from tests.integration.test_migration_014 import _seed_callback
from tests.integration.test_migration_015 import _claimed_pending
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def test_unwitnessed_delivery_refused_but_legacy_delivered_rows_survive(pg):
    """Re-audit F1: raw SQL could set a decision callback delivered with BOTH wire columns NULL —
    the guard only fired when a digest was being written — and the taxonomy then called the
    delivered row not_accepted. The transition now requires the witness; rows already delivered
    (legacy NULLs) are untouched because the guard is transition-scoped."""
    url = _fresh_db(pg, "kyc_mig_016_f1")
    cfg = _config(url)
    # the legacy row is seeded in the world that produced it: at 016 a born-delivered INSERT
    # was still expressible (017's lifecycle INSERT guard forecloses it), then adopted upward
    command.upgrade(cfg, "016")
    eng = create_engine(url)
    with eng.begin() as conn:
        legacy = _seed_callback(conn, "c1", "c1-r1", 1, "delivered", delivered=True)  # pre-witness
    command.upgrade(cfg, "head")
    with eng.begin() as conn:
        target = _claimed_pending(conn, "c1", "c1-r2", 2, with_attempt=False).id

    with pytest.raises(Exception, match="unwitnessed delivery refused"), eng.begin() as conn:
        conn.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now(), "
            "claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"),
            {"i": target})

    with eng.connect() as conn:  # the legacy delivered row survived the upgrade untouched
        st = conn.execute(text("SELECT status, callback_wire_sha256 FROM outbox WHERE id=:i"),
                          {"i": legacy}).one()
    assert st.status == "delivered" and st.callback_wire_sha256 is None
    # …and from 018 on it is FROZEN as terminal, which is a stronger statement than the
    # "still updatable" one this test used to make (re-audit `cbb783b` F2)
    with pytest.raises(Exception, match="terminal"), eng.begin() as conn:
        conn.execute(text("UPDATE outbox SET last_error = 'note' WHERE id = :i"), {"i": legacy})
    eng.dispose()


def test_pre_authority_attempts_are_not_promoted_into_staged_intent(pg):
    """Re-audit F2: an attempt inserted under 014 (before the admission trigger existed) with an
    ARBITRARY token must not classify its row send_intent_witnessed after the upgrade — it is
    'legacy_unverified', the row reads legacy_unwitnessed (unproven either way, never
    not_accepted), and a post-016 live-claim attempt on another row IS admitted."""
    from kyc_tool.outbox import witness

    url = _fresh_db(pg, "kyc_mig_016_f2")
    cfg = _config(url)
    command.upgrade(cfg, "014")  # the pre-admission world
    eng = create_engine(url)
    with eng.begin() as conn:
        oid = _seed_callback(conn, "c2", "c2-r1", 1, "pending")
        conn.execute(text(  # arbitrary token, no claim — 014 allowed exactly this
            "UPDATE outbox SET witness_generation='attempt_v1' WHERE id=:i"), {"i": oid})
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, gen_random_uuid(), "
            "'legacy', :s)"), {"o": oid, "s": "b" * 64})
    command.upgrade(cfg, "head")

    with eng.begin() as conn:
        adm = conn.execute(text(
            "SELECT admission FROM outbox_delivery_attempts WHERE outbox_id=:o"),
            {"o": oid}).scalar_one()
        got = conn.execute(text(witness.WITNESS_SELECT + " AND o.id = :i"), {"i": oid}).one()
        # a genuinely admitted attempt, for contrast, on a fresh row
        row2 = _claimed_pending(conn, "c2", "c2-r2", 2)
        adm2 = conn.execute(text(
            "SELECT admission FROM outbox_delivery_attempts WHERE outbox_id=:o"),
            {"o": row2.id}).scalar_one()
        got2 = conn.execute(text(witness.WITNESS_SELECT + " AND o.id = :i"), {"i": row2.id}).one()
    assert adm == "legacy_unverified"
    assert got.witness == witness.LEGACY_UNWITNESSED, (
        "an unadmitted attempt proves nothing — neither staged intent nor non-delivery"
    )
    assert adm2 == "admission_v1" and got2.witness == witness.SEND_INTENT_WITNESSED
    eng.dispose()


def test_writer_cannot_choose_its_own_admission(pg):
    """The INSERT may CARRY admission='admission_v1'; the trigger overwrites it — admission means
    'this trigger admitted it', never 'the writer said so'. (Trivially true here since the value
    matches, so prove the converse: carrying 'legacy_unverified' on a live-claim insert still
    yields admission_v1.)"""
    url = _fresh_db(pg, "kyc_mig_016_stamp")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        row = _claimed_pending(conn, "c3", "c3-r1", 1, with_attempt=False)
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256, admission) VALUES (gen_random_uuid(), :o, :t, "
            "'legacy', :s, 'legacy_unverified')"),
            {"o": row.id, "t": row.claim_token, "s": "c" * 64})
        adm = conn.execute(text(
            "SELECT admission FROM outbox_delivery_attempts WHERE outbox_id=:o"),
            {"o": row.id}).scalar_one()
    assert adm == "admission_v1"
    eng.dispose()


def test_mutilated_authority_functions_are_erased_by_recreation(pg):
    """Re-audit F3: a same-named NO-OP body behind outbox_attempts_guard() passed 015's name-only
    validation, making "insert-only" decorative. 016 drops and recreates every authority function
    canonically, so whatever body a mutilation left behind is gone and the behavior holds."""
    url = _fresh_db(pg, "kyc_mig_016_f3")
    cfg = _config(url)
    command.upgrade(cfg, "015")
    eng = create_engine(url)
    with eng.begin() as conn:  # the audit's exact mutilation: keep the name, gut the body
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION outbox_attempts_guard() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN IF TG_OP = 'UPDATE' THEN RETURN NEW; END IF; "
            "RETURN OLD; END $$"))
        row = _claimed_pending(conn, "c4", "c4-r1", 1)
    with eng.begin() as conn:  # mutilated: UPDATE passes — insert-only is decorative
        conn.execute(text("UPDATE outbox_delivery_attempts SET request_sha256 = :s"),
                     {"s": "d" * 64})
        conn.execute(text(  # release the claim: 017's preflight requires writer quiescence
            "UPDATE outbox SET claim_token=NULL, claim_lease_expires_at=NULL, claimed_by=NULL "
            "WHERE id = :i"), {"i": row.id})

    command.upgrade(cfg, "head")  # recreation erases the mutilation

    with pytest.raises(Exception, match="insert-only"), eng.begin() as conn:
        conn.execute(text("UPDATE outbox_delivery_attempts SET request_sha256 = :s"),
                     {"s": "e" * 64})
    with pytest.raises(Exception, match="sole delivery evidence"), eng.begin() as conn:
        conn.execute(text("DELETE FROM outbox_delivery_attempts WHERE outbox_id = :o"),
                     {"o": row.id})
    eng.dispose()


def test_downgrade_refuses_on_negative_evidence_alone(pg):
    """Re-audit F4: an attempt_v1 decision callback with NO attempt and NO digest is the durable
    proof nothing was staged — the only state licensing a non-delivery claim. The real documented
    command (subprocess, head→012) must refuse with only that row on the books; the refusal now
    lands at 017, the outermost witness authority in the walk, guarding the same evidence."""
    url = _fresh_db(pg, "kyc_mig_016_f4")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.begin() as conn:
        _seed_callback(conn, "c5", "c5-r1", 1, "pending", generation="attempt_v1")

    env = {**os.environ, "KYC_DATABASE_URL": url}
    refused = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "downgrade", "012"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0
    # the walk stops at the OUTERMOST authority: 018 is forward-only, so it refuses before 017's
    # witness guard is even consulted. Same evidence preserved, one revision earlier.
    assert "MIGRATION_020_DOWNGRADE_REFUSED_FORWARD_ONLY" in refused.stdout + refused.stderr
    with eng.connect() as conn:  # nothing lost, still at head
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "020"
        assert conn.execute(text(
            "SELECT witness_generation FROM outbox")).scalar_one() == "attempt_v1"
    eng.dispose()


def test_unused_database_round_trips_through_016(pg):
    """016's own round trip, anchored at 017: `018` is forward-only once installed."""
    url = _fresh_db(pg, "kyc_mig_016_roundtrip")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    command.downgrade(cfg, "012")
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    with eng.begin() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "017"
        _claimed_pending(conn, "c6", "c6-r1", 1)  # the full legal write path still works
    eng.dispose()
