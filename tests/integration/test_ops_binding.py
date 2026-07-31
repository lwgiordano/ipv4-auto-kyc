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


# verify runs on schema 012 (its exact-phase requirement, F12); the mutators on 013.
@pytest.mark.parametrize("module,rev", [
    ("verify_pr7b_core_backfill", "012"),
    ("reset_interrupted_outbox_claims", "013"),
    ("repair_outbox_sequence", "013"),
])
def test_conflicting_lock_refuses_with_sentinel_within_bound_and_changes_nothing(pg, module, rev):
    url = _fresh_db(pg, f"kyc_ops_lock_{module[:12]}")
    command.upgrade(_config(url), rev)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO public.cases (id) VALUES ('c1')"))
        if rev == "013":  # 012 has no claim columns
            conn.execute(text(
                "INSERT INTO public.outbox (kind, case_id, ordering_stream, status, claim_token, "
                "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','pending', "
                "gen_random_uuid(), now(), 'w1')"))

    holder = engine.connect()
    try:
        tx = holder.begin()
        holder.execute(text("LOCK TABLE public.outbox IN ACCESS EXCLUSIVE MODE"))
        started = time.monotonic()
        proc = _run(module, url, timeout_env="2")
        elapsed = time.monotonic() - started
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "OPS_COMMAND_LOCK_TIMEOUT" in proc.stderr, proc.stdout + proc.stderr
        assert elapsed < 30, f"{module} took {elapsed:.1f}s against a 2s bound"
        tx.rollback()
    finally:
        holder.close()
    if rev == "013":
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


def test_bind_sets_statement_timeout_from_its_own_setting(pg):
    """Re-audit `538e55e..42e1c7d` F11: statement_timeout is a SEPARATE governed budget SET BY
    bind() from KYC_OPS_STATEMENT_TIMEOUT_SECONDS — not a constant derived from the lock, and (the
    prior test's gap) actually exercised THROUGH bind(). Call bind(), read back SHOW
    statement_timeout, and prove a query past the bound is canceled (57014). Nothing mutated."""
    from sqlalchemy.exc import OperationalError

    from kyc_tool.db.session import make_engine, make_session_factory
    from kyc_tool.ops import binding

    url = _fresh_db(pg, "kyc_ops_stmt_bind")
    command.upgrade(_config(url), "012")
    sf = make_session_factory(make_engine(url))
    with sf() as s:
        binding.bind(s, lock_timeout_seconds=1, statement_timeout_seconds=2, exact_revision="012")
        assert s.execute(text("SHOW statement_timeout")).scalar_one() == "2s"  # from the setting
        with pytest.raises(OperationalError) as ei:  # the bound is real, not just declared
            s.execute(text("SELECT pg_sleep(5)"))
        assert binding.is_statement_timeout(ei.value), "57014 must classify as statement timeout"
        s.rollback()


def test_bind_refuses_a_statement_budget_at_or_below_the_lock_budget(pg):
    """A statement ceiling <= the lock budget would cancel the lock wait before lock_timeout fires
    and misclassify the failure; bind() refuses it as a governed BindingRefused, changing nothing."""
    from kyc_tool.db.session import make_engine, make_session_factory
    from kyc_tool.ops import binding

    url = _fresh_db(pg, "kyc_ops_stmt_coherent")
    command.upgrade(_config(url), "012")
    sf = make_session_factory(make_engine(url))
    with sf() as s, pytest.raises(binding.BindingRefused, match="OPS_COMMAND_SCHEMA_REFUSED"):
        binding.bind(s, lock_timeout_seconds=5, statement_timeout_seconds=5, exact_revision="012")


def test_multi_head_alembic_version_refuses_instead_of_reading_one_arbitrary_row(pg):
    """Re-audit `538e55e..42e1c7d` F4: bind() read one arbitrary row via .scalar(), so a multi-head
    `{012, 999}` satisfied an exact/floor check against whichever row returned (verify printed
    'schema-012 parity matrix clean' and exited 0). bind() now reads the FULL version set, requires
    cardinality one, and refuses — a governed sentinel, nonzero, no traceback, nothing certified."""
    url = _fresh_db(pg, "kyc_ops_multihead")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    with engine.begin() as conn:  # inject a second head — an invalid migration state
        conn.execute(text("INSERT INTO public.alembic_version (version_num) VALUES ('999')"))
    engine.dispose()

    proc = _run("verify_pr7b_core_backfill", url)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "OPS_COMMAND_SCHEMA_REFUSED" in proc.stderr, proc.stderr
    assert "schema-012 parity matrix clean" not in proc.stdout  # never certified a phase it didn't check
    assert "Traceback" not in proc.stderr  # governed refusal, not a crash


def test_repair_and_restore_refuse_a_non_owner_before_mutating(pg):
    """Re-audit `8377440` F13: ALTER SEQUENCE needs OWNERSHIP, not ALL privileges. A non-owner
    with full grants must refuse at the preflight (before any DML/DDL), with the stable
    OPS_COMMAND_NOT_SEQUENCE_OWNER sentinel — not discover it mid-maintenance."""
    url = _fresh_db(pg, "kyc_ops_owner")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE ROLE kyc_nonowner LOGIN PASSWORD 'x'"))
        conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO kyc_nonowner"))
        conn.execute(text("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO kyc_nonowner"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO kyc_nonowner"))
    engine.dispose()
    # rebuild the URL as the non-owner role
    from sqlalchemy.engine import make_url
    nonowner_url = make_url(url).set(username="kyc_nonowner", password="x").render_as_string(
        hide_password=False)

    proc = _run("repair_outbox_sequence", nonowner_url)
    assert proc.returncode != 0 and "OPS_COMMAND_NOT_SEQUENCE_OWNER" in proc.stderr, proc.stderr
    eng = create_engine(url)
    with eng.connect() as conn:  # nothing mutated
        called = conn.execute(text("SELECT is_called FROM public.outbox_id_seq")).scalar_one()
        assert not called
    with eng.begin() as conn:
        conn.execute(text("DROP OWNED BY kyc_nonowner"))
        conn.execute(text("DROP ROLE kyc_nonowner"))
    eng.dispose()


def test_verify_refuses_any_phase_other_than_exactly_012(pg):
    """Re-audit `8377440` F12: verify unconditionally printed 'schema-012 parity matrix clean'.
    On 011 (too early) and 013/head (too late) it must refuse, not certify a phase it never
    checked. Only exactly 012 may print the success line."""
    from kyc_tool.ops import binding  # noqa: F401 (import proves module wiring is intact)

    cfg = _config(_fresh_db(pg, "kyc_verify_phase_probe"))  # noqa: F841 (placeholder for parity)
    for rev, expect_ok in [("011", False), ("012", True), ("013", False), ("head", False)]:
        url = _fresh_db(pg, f"kyc_verify_phase_{rev}")
        command.upgrade(_config(url), rev)
        proc = subprocess.run(
            [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
            env={**os.environ, "KYC_DATABASE_URL": url}, capture_output=True, text=True, timeout=60,
        )
        if expect_ok:
            assert proc.returncode == 0 and "schema-012 parity matrix clean" in proc.stdout
        else:
            assert proc.returncode != 0, f"rev {rev} must refuse"
            assert "schema-012 parity matrix clean" not in proc.stdout, f"rev {rev} was certified"
            # F4: a wrong-phase refusal is a GOVERNED sentinel, not a Python traceback
            assert "Traceback" not in proc.stderr, f"rev {rev} tracebacked instead of refusing"
            assert "OPS_COMMAND_SCHEMA_REFUSED" in proc.stderr, proc.stderr


def test_ops_prerequisites_preflight_reports_role_and_phase_read_only(pg):
    """Re-audit `538e55e..42e1c7d` F12: an operator must VERIFY role, sequence owner, schema phase,
    and timeout budgets BEFORE pausing service — a read-only preflight that needs NO maintenance
    stop and takes no ACCESS EXCLUSIVE lock. As the owning role it exits 0, reports ownership +
    phase + budgets, and mutates nothing (the sequence is untouched)."""
    url = _fresh_db(pg, "kyc_ops_preflight_ok")
    command.upgrade(_config(url), "012")
    proc = _run("verify_pr7b_ops_prerequisites", url)  # no writer stop, no lock — runs live
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "owns_sequence: True" in proc.stdout and "schema_revision: 012" in proc.stdout
    assert "statement_timeout_seconds" in proc.stdout  # reports both budgets
    assert "Traceback" not in proc.stderr
    engine = create_engine(url)
    with engine.connect() as conn:  # read-only: nothing mutated
        assert conn.execute(text("SELECT is_called FROM public.outbox_id_seq")).scalar_one() is False
    engine.dispose()


def test_ops_prerequisites_preflight_flags_a_non_owner_before_the_outage(pg):
    """A wrong (non-owning) production credential is caught by the read-only preflight BEFORE the
    outage — nonzero, reports it does not own the sequence — instead of being discovered inside
    maintenance at ALTER SEQUENCE. Nothing is mutated."""
    url = _fresh_db(pg, "kyc_ops_preflight_nonowner")
    command.upgrade(_config(url), "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE ROLE kyc_pf_nonowner LOGIN PASSWORD 'x'"))
        conn.execute(text("GRANT ALL ON ALL TABLES IN SCHEMA public TO kyc_pf_nonowner"))
        conn.execute(text("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO kyc_pf_nonowner"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO kyc_pf_nonowner"))
    engine.dispose()
    from sqlalchemy.engine import make_url
    nonowner_url = make_url(url).set(username="kyc_pf_nonowner", password="x").render_as_string(
        hide_password=False)

    proc = _run("verify_pr7b_ops_prerequisites", nonowner_url)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "own" in (proc.stdout + proc.stderr).lower()  # reports the ownership gap

    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP OWNED BY kyc_pf_nonowner"))
        conn.execute(text("DROP ROLE kyc_pf_nonowner"))
    eng.dispose()
