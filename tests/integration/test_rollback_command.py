"""The EXACT documented rollback command must be valid against a real HEAD database.

Upgrading only to `013` and rolling back would never execute the repair (`014`) and hardening
(`015`) downgrades a production head actually walks (re-audit `4dfdf8a` F6) — the unused path,
the witness refusal, and the sentinel all have to come from the real multi-revision command.

`alembic.ini` ships an empty `sqlalchemy.url`, so env.py (`_database_url`) falls through to
`KYC_DATABASE_URL`; the command below runs from REPO_ROOT with that env pointing at the dedicated
DB."""

import os
import re
import subprocess
import sys

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from kyc_tool.config import REPO_ROOT
from tests import roadmap
from tests.integration.test_migrations import _fresh_db

pytestmark = pytest.mark.postgres

# EXACTLY the argv the docs prescribe: `python -m alembic -c alembic.ini downgrade 012`
# (run from REPO_ROOT). Invoked through the RUNNING interpreter, never a hardcoded
# ".venv/bin/alembic": CI installs with `pip install -e '.[dev]'` and has no .venv, so the
# hardcoded path raises FileNotFoundError there while passing locally.
_ALEMBIC = [sys.executable, "-m", "alembic"]

# Derived, never transcribed: the plan block this file came from pinned head '021' by hand
# while its own assertions said '022' — the same stale-number class the artifact gate now
# derives from ROADMAP §C. The head comes from §C (pinned to the live Alembic chain by
# test_migration_lineage); the revision the walk STOPS at is the topmost unconditional
# forward-only refusal, read from the migrations themselves. Revisions above it (023 today)
# have validation-only no-op downgrades, so the walk passes through them first.
_EXPECTED_HEAD = f"{roadmap.head(roadmap.records()):03d}"
_STOP = max(
    m.group(1)
    for p in (REPO_ROOT / "alembic" / "versions").glob("*.py")
    for m in re.finditer(r"MIGRATION_(\d{3})_DOWNGRADE_REFUSED_FORWARD_ONLY", p.read_text())
)
_STOP_SENTINEL = f"MIGRATION_{_STOP}_DOWNGRADE_REFUSED_FORWARD_ONLY"


def _head_db(pg, name):
    url = _fresh_db(pg, name)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    with eng.connect() as conn:
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    eng.dispose()
    assert head == _EXPECTED_HEAD, (
        f"the documented rollback must start from the REAL head {_EXPECTED_HEAD}, got {head}"
    )
    return url


def test_documented_rollback_command_refuses_at_the_forward_only_head(pg):
    """Even a WITNESS-FREE database refuses: `018` is forward-only, so the exact documented
    command stops at the outermost authority rather than walking the chain. This is the case
    `017` would have walked cheerfully — straight into unqualified, caller-search-path
    authority functions (re-audit `cbb783b` F3)."""
    url = _head_db(pg, "kyc_rollback_cmd")
    env = {**os.environ, "KYC_DATABASE_URL": url}
    refused = subprocess.run([*_ALEMBIC, "-c", "alembic.ini", "downgrade", "012"],
                             cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0
    assert _STOP_SENTINEL in refused.stdout + refused.stderr
    eng = create_engine(url)
    with eng.connect() as conn:
        # The walk runs in ONE transaction: 023's validation-only no-op executes, then 022's
        # refusal rolls the WHOLE command back — so the schema does not move at all. The
        # sentinel names 022 (the revision that refused); the version stays at the head.
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _EXPECTED_HEAD
    eng.dispose()

    # the SAME argv without the positional revision is the invalid form the docs must never use
    bad = subprocess.run([*_ALEMBIC, "-c", "alembic.ini", "downgrade"],
                         cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode != 0


def test_documented_rollback_command_refuses_on_witness_evidence(pg):
    """With one staged attempt on the books, the SAME documented command must still refuse —
    at the topmost forward-only refusal, BEFORE 017's witness guard is even consulted — with
    the staged evidence intact: the flag/image rollback branch of the runbook, proven through
    the real command."""
    url = _head_db(pg, "kyc_rollback_cmd_witness")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES "
            "('e1','c1','k1','h','x','{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('r1','c1','e1','PUBLISH_DECISION')"))
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('d1','c1','r1','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,1)"))
        # born unclaimed (017's lifecycle INSERT guard), then claimed — the legal road
        oid = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, "
            "status) VALUES ('decision_callback','c1','r1','decision',1,'pending') "
            "RETURNING id")).scalar_one()
        row = conn.execute(text(
            "UPDATE outbox SET claim_token = gen_random_uuid(), "
            "claim_lease_expires_at = now() + interval '1 hour', claimed_by = 'w' "
            "WHERE id = :i RETURNING id, claim_token"), {"i": oid}).one()
        conn.execute(text(
            "INSERT INTO outbox_delivery_attempts (attempt_id, outbox_id, claim_token, "
            "wire_version, request_sha256) VALUES (gen_random_uuid(), :o, :t, 'legacy', :s)"),
            {"o": row.id, "t": row.claim_token, "s": "a" * 64})

    env = {**os.environ, "KYC_DATABASE_URL": url}
    refused = subprocess.run([*_ALEMBIC, "-c", "alembic.ini", "downgrade", "012"],
                             cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0
    assert _STOP_SENTINEL in refused.stdout + refused.stderr
    with eng.connect() as conn:  # nothing was dropped underneath the evidence; one txn, full rollback
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _EXPECTED_HEAD
        assert conn.execute(text("SELECT count(*) FROM outbox_delivery_attempts")).scalar_one() == 1
    eng.dispose()
