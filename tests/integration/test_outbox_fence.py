"""The shared maintenance fence: one lock order for EVERY outbox-authority writer.

Re-audit `cbb783b` F4: `017` fenced the publisher but not retention, which updates the parent
`outbox` and then deletes from the child `outbox_delivery_attempts` — the exact opposite of a
migration's child-then-parent order, and invisible to the live-claim preflight because retention
holds no claim. These are the deterministic two-connection barriers that prove the cycle is gone.
"""

import threading
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.outbox.fence import MAINTENANCE_FENCE_KEY
from tests.integration.test_migration_015 import _claimed_pending
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def test_fence_key_matches_every_migration_that_takes_it():
    """A migration cannot import application code, so the constant is duplicated by necessity —
    and therefore asserted equal here. A drifted key silently disables the whole fence."""
    from pathlib import Path

    from kyc_tool.config import REPO_ROOT

    versions = Path(REPO_ROOT) / "alembic" / "versions"
    for name in ("017_outbox_authority_boundary.py", "018_outbox_transition_authority.py",
                 "019_outbox_dead_poc_redaction.py",
                 "020_outbox_redaction_uniformity.py",
                 "021_outbox_poison_recovery.py"):
        src = (versions / name).read_text()
        assert f"_FENCE_KEY = {MAINTENANCE_FENCE_KEY}" in src, f"{name} does not share the fence"


def _run_migration_in_thread(cfg, target, result: dict, *, downgrade=False):
    def _go():
        try:
            (command.downgrade if downgrade else command.upgrade)(cfg, target)
            result["outcome"] = "completed"
        except Exception as exc:  # noqa: BLE001 — the outcome IS the assertion
            result["outcome"] = str(exc)

    return threading.Thread(target=_go, name=f"migration-{target}")


def _seed_retention_shaped_work(eng):
    """A delivered callback with an attempt row: retention will UPDATE the parent (redaction) and
    DELETE from the child (the now-redundant attempt) — the two-relation shape F4 is about."""
    with eng.begin() as conn:
        # attempts are insert-only, so the row is BORN old rather than aged by an UPDATE
        row = _claimed_pending(conn, "cx", "cx-r1", 1, with_attempt=False)
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256, attempted_at) VALUES (gen_random_uuid(), :o, :t, "
            "'legacy', :s, now() - interval '9999 days')"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})
        conn.execute(text(
            "UPDATE outbox SET status='delivered', delivered_at=now(), "
            "callback_wire_sha256=:s, wire_version='legacy', claim_token=NULL, "
            "claim_lease_expires_at=NULL, claimed_by=NULL WHERE id=:i"),
            {"s": "a" * 64, "i": row.id})
    return row


def test_real_retention_and_the_018_upgrade_never_deadlock(pg):
    """Retention holds BOTH relations mid-transaction (parent updated, child deleted) while the
    upgrade starts. The upgrade must queue at the advisory fence — never take a table lock in the
    opposite order and cycle — then proceed once retention commits."""
    from kyc_tool.db.session import make_engine, make_session_factory
    from kyc_tool.workers.retention import prune

    url = _fresh_db(pg, "kyc_fence_retention_up")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    _seed_retention_shaped_work(eng)

    # retention on its own connection, paused mid-transaction by a barrier session
    holder = create_engine(url)
    conn = holder.connect()
    result: dict = {}
    t = _run_migration_in_thread(cfg, "head", result)
    try:
        tx = conn.begin()
        conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": MAINTENANCE_FENCE_KEY})
        conn.execute(text(  # retention's exact order: parent first, then child
            "UPDATE outbox SET payload_json='{\"redacted\": true}'::jsonb "
            "WHERE kind='decision_callback' AND status='delivered'"))
        conn.execute(text(
            "DELETE FROM outbox_delivery_attempts a USING outbox o "
            "WHERE o.id=a.outbox_id AND o.callback_wire_sha256 IS NOT NULL"))
        t.start()
        time.sleep(2)
        assert t.is_alive(), "the upgrade must QUEUE at the fence, not take table locks past it"
        tx.commit()
    finally:
        t.join(timeout=60)
        conn.close()
        holder.dispose()

    assert not t.is_alive()
    assert "40P01" not in result["outcome"] and "deadlock" not in result["outcome"].lower()
    assert result["outcome"] == "completed", result["outcome"]

    # and the REAL prune still runs to completion against the migrated schema
    engine = make_engine(url)
    counts = prune(make_session_factory(engine), retention_days=1)
    assert counts["outbox_callback_redacted"] >= 0
    engine.dispose()
    eng.dispose()


def test_real_retention_and_the_017_downgrade_never_deadlock(pg):
    """The same barrier against the downgrade direction, on the walk that still exists below
    `018`. It must serialize and then refuse on the witness — not deadlock."""
    url = _fresh_db(pg, "kyc_fence_retention_down")
    cfg = _config(url)
    command.upgrade(cfg, "017")
    eng = create_engine(url)
    _seed_retention_shaped_work(eng)

    holder = create_engine(url)
    conn = holder.connect()
    result: dict = {}
    t = _run_migration_in_thread(cfg, "016", result, downgrade=True)
    try:
        tx = conn.begin()
        conn.execute(text("SELECT pg_advisory_xact_lock_shared(:k)"), {"k": MAINTENANCE_FENCE_KEY})
        conn.execute(text(
            "UPDATE outbox SET payload_json='{\"redacted\": true}'::jsonb "
            "WHERE kind='decision_callback' AND status='delivered'"))
        conn.execute(text(
            "DELETE FROM outbox_delivery_attempts a USING outbox o "
            "WHERE o.id=a.outbox_id AND o.callback_wire_sha256 IS NOT NULL"))
        t.start()
        time.sleep(2)
        assert t.is_alive(), "the downgrade must QUEUE at the fence behind retention"
        tx.commit()
    finally:
        t.join(timeout=60)
        conn.close()
        holder.dispose()

    assert not t.is_alive()
    assert "40P01" not in result["outcome"] and "deadlock" not in result["outcome"].lower()
    # the digest retention just made redundant is still witness evidence: the walk refuses
    assert "MIGRATION_017_DOWNGRADE_REFUSED_WITNESS_IN_USE" in result["outcome"]
    with eng.connect() as c:
        assert c.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one() == "017"
    eng.dispose()


def test_prune_takes_the_fence_before_any_dml(pg):
    """The fence must be the FIRST statement of the transaction: one acquired after the parent is
    already locked fences nothing. Proven by holding the fence EXCLUSIVELY and showing `prune`
    blocks before it has written anything."""
    from kyc_tool.db.session import make_engine, make_session_factory
    from kyc_tool.workers.retention import prune

    url = _fresh_db(pg, "kyc_fence_prune_first")
    cfg = _config(url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    _seed_retention_shaped_work(eng)

    blocker = create_engine(url)
    bconn = blocker.connect()
    engine = make_engine(url)
    done: dict = {}

    def _prune():
        try:
            done["counts"] = prune(make_session_factory(engine), retention_days=1)
        except Exception as exc:  # noqa: BLE001
            done["error"] = str(exc)

    t = threading.Thread(target=_prune, name="prune")
    try:
        btx = bconn.begin()
        bconn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MAINTENANCE_FENCE_KEY})
        t.start()
        time.sleep(2)
        assert t.is_alive(), "prune must block at the fence"
        with eng.connect() as c:  # …and must not have written anything yet
            assert c.execute(text(
                "SELECT count(*) FROM outbox_delivery_attempts")).scalar_one() == 1
            assert c.execute(text(
                "SELECT payload_json->>'redacted' FROM outbox "
                "WHERE kind='decision_callback'")).scalar_one() is None
        btx.commit()
    finally:
        t.join(timeout=60)
        bconn.close()
        blocker.dispose()

    assert not t.is_alive() and "error" not in done, done
    assert done["counts"]["outbox_attempts_pruned"] == 1
    engine.dispose()
    eng.dispose()
