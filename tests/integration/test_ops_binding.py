"""Ops-command hardening (Codex re-audit `f495de8` F3/F4/F10), against real Postgres.

Three properties, each proven through the REAL subprocess entry points:
- a smuggled `search_path` (URL `?options=-csearch_path=shadow,public`) cannot redirect any
  command to a shadow object — the governed `public` schema is what gets inspected/mutated;
- a conflicting lock refuses with the stable `OPS_COMMAND_LOCK_TIMEOUT` sentinel within the
  configured bound, changing nothing — never an indefinite hang;
- the reset's zero-claims guarantee holds under its exclusive fence: a claimant arriving in
  the read-back→commit window BLOCKS until the reset commits instead of falsifying it.
"""

import os
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def _run(module, url, *args, timeout_env="60"):
    return subprocess.run(
        [sys.executable, "-m", f"kyc_tool.ops.{module}", *args],
        env={**os.environ, "KYC_DATABASE_URL": url,
             "KYC_OPS_LOCK_TIMEOUT_SECONDS": timeout_env},
        capture_output=True, text=True, timeout=90,
    )


def _shadowed(url: str) -> str:
    """The audit's exact vector: a URL whose options put a `shadow` schema first."""
    return url + ("&" if "?" in url else "?") + "options=" + quote("-csearch_path=shadow,public")


def _seed_shadow(engine):
    """A shadow outbox + sequence that unqualified SQL would silently operate on."""
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA shadow"))
        conn.execute(text("CREATE TABLE shadow.outbox (LIKE public.outbox INCLUDING ALL)"))
        conn.execute(text("CREATE SEQUENCE shadow.outbox_id_seq"))


def test_shadow_search_path_cannot_redirect_reset_or_repair(pg):
    """Audit repro: with `shadow` first, reset printed zero while a `public` claim survived and
    repair repaired `shadow.outbox_id_seq`. Bound commands must operate on `public` regardless."""
    url = _fresh_db(pg, "kyc_ops_shadow")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    _seed_shadow(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO public.cases (id) VALUES ('c1')"))
        conn.execute(text(
            "INSERT INTO public.outbox (kind, case_id, ordering_stream, status, claim_token, "
            "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
            "gen_random_uuid(), now(), 'w1')"))
        conn.execute(text("SELECT setval('shadow.outbox_id_seq', 7, true)"))

    reset = _run("reset_interrupted_outbox_claims", _shadowed(url))
    assert reset.returncode == 0, reset.stdout + reset.stderr
    with engine.connect() as conn:  # the REAL claim was cleared, not a shadow no-op
        assert conn.execute(text(
            "SELECT count(*) FROM public.outbox WHERE claim_token IS NOT NULL")).scalar_one() == 0

    repair = _run("repair_outbox_sequence", _shadowed(url))
    assert repair.returncode == 0, repair.stdout + repair.stderr
    with engine.connect() as conn:
        pub = conn.execute(text(
            "SELECT last_value, is_called FROM public.outbox_id_seq")).one()
        sh = conn.execute(text("SELECT last_value FROM shadow.outbox_id_seq")).scalar_one()
    assert not pub.is_called, "public sequence must carry the repair (fresh RESTART)"
    assert sh == 7, "the shadow sequence must be untouched"
    engine.dispose()


def test_shadow_search_path_cannot_blind_the_diagnostic(pg):
    """An empty shadow.outbox must not make the diagnostic read a clean parity matrix while
    public.outbox carries the violation."""
    url = _fresh_db(pg, "kyc_ops_shadow_verify")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    _seed_shadow(engine)
    with engine.begin() as conn:  # a real violation in PUBLIC: decision with no callback
        conn.execute(text("INSERT INTO public.cases (id) VALUES ('c1')"))
        conn.execute(text(
            "INSERT INTO public.events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('e1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO public.runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('r1','c1','e1','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO public.decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual) VALUES "
            "('d1','c1','r1','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"))
    engine.dispose()

    proc = _run("verify_pr7b_core_backfill", _shadowed(url))
    assert proc.returncode != 0 and "BLOCKED_NO_AUTHORITATIVE_MAPPING" in proc.stdout


def test_unbound_database_refuses_before_touching_anything(pg):
    """No alembic_version = not the governed schema: every command refuses up front."""
    url = _fresh_db(pg, "kyc_ops_unbound")  # fresh DB, no migrations at all
    for module in ("verify_pr7b_core_backfill", "reset_interrupted_outbox_claims",
                   "repair_outbox_sequence"):
        proc = _run(module, url)
        assert proc.returncode != 0, module
        assert "alembic_version" in proc.stderr or "alembic_version" in proc.stdout, module


@pytest.mark.parametrize("module,extra", [
    ("verify_pr7b_core_backfill", ()),
    ("reset_interrupted_outbox_claims", ()),
    ("repair_outbox_sequence", ()),
])
def test_conflicting_lock_refuses_with_sentinel_within_bound_and_changes_nothing(pg, module, extra):
    url = _fresh_db(pg, f"kyc_ops_lock_{module[:12]}")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO public.cases (id) VALUES ('c1')"))
        conn.execute(text(
            "INSERT INTO public.outbox (kind, case_id, ordering_stream, status, claim_token, "
            "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
            "gen_random_uuid(), now(), 'w1')"))

    holder = engine.connect()
    try:
        tx = holder.begin()
        holder.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))
        started = time.monotonic()
        proc = _run(module, url, *extra, timeout_env="2")
        elapsed = time.monotonic() - started
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "OPS_COMMAND_LOCK_TIMEOUT" in proc.stderr
        assert elapsed < 30, f"{module} took {elapsed:.1f}s against a 2s bound"
        tx.rollback()
    finally:
        holder.close()
    with engine.connect() as conn:  # nothing changed under the refused command
        assert conn.execute(text(
            "SELECT count(*) FROM public.outbox WHERE claim_token IS NOT NULL")).scalar_one() == 1
    engine.dispose()


def test_reset_fence_blocks_a_claimant_arriving_in_the_readback_window(pg):
    """Audit F3's exact race, now impossible: a claim committed between the reset's zero
    read-back and its commit made `reported_reset=1` a lie. Under the ACCESS EXCLUSIVE fence
    the late claimant BLOCKS until the reset commits; its claim lands strictly AFTER, so the
    reset's 'zero at commit' statement stays true."""
    from kyc_tool.db.session import make_engine, make_session_factory
    from kyc_tool.ops import reset_interrupted_outbox_claims as resetter

    url = _fresh_db(pg, "kyc_reset_fence")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO public.cases (id) VALUES ('c1')"))
        conn.execute(text(
            "INSERT INTO public.outbox (kind, case_id, ordering_stream, status, claim_token, "
            "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
            "gen_random_uuid(), now(), 'w1')"))

    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    at_readback = threading.Event()
    release = threading.Event()
    late_done = threading.Event()

    def _barrier(conn, cursor, statement, params, context, executemany):
        # pause the reset AFTER its zero read-back, INSIDE the window the audit exploited
        if "count(*) from public.outbox where claim_token is not null" in statement.lower():
            at_readback.set()
            release.wait(timeout=20)

    def late_claimant():
        with create_engine(url).begin() as conn:
            conn.execute(text(  # must BLOCK on the reset's fence until it commits
                "INSERT INTO public.outbox (kind, case_id, ordering_stream, status, claim_token, "
                "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
                "gen_random_uuid(), now() + interval '60 seconds', 'late')"))
        late_done.set()

    sf = make_session_factory(make_engine(url))
    result: list[int] = []
    reset_thread = threading.Thread(target=lambda: result.append(resetter.reset_claims(sf)))
    late_thread = threading.Thread(target=late_claimant, daemon=True)
    registered = started = False
    try:
        event.listen(Engine, "before_cursor_execute", _barrier)
        registered = True
        reset_thread.start()
        started = True
        assert at_readback.wait(timeout=15), "reset never reached its read-back"
        late_thread.start()
        assert not late_done.wait(timeout=1.5), (
            "the late claimant committed inside the read-back window — the fence is gone and "
            "the reset's zero statement is a lie again"
        )
        release.set()
        reset_thread.join(timeout=20)
    finally:
        release.set()
        try:
            if started:
                reset_thread.join(timeout=20)
                assert not reset_thread.is_alive(), "reset thread outlived the test"
        finally:
            if registered:
                event.remove(Engine, "before_cursor_execute", _barrier)
    assert late_done.wait(timeout=15), "the late claimant must complete after the reset commits"
    assert result == [1], "the reset accounted exactly the pre-existing claim"
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT claimed_by FROM public.outbox WHERE claim_token IS NOT NULL")).fetchall()
    assert [r.claimed_by for r in rows] == ["late"], (
        "exactly the post-commit claim survives; zero-at-commit was true when stated"
    )
    engine.dispose()
