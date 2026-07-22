"""Phase 0 acceptance: all tables migrate up and down cleanly."""

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from kyc_tool.config import REPO_ROOT

pytestmark = pytest.mark.postgres

EXPECTED_TABLES = {
    "cases",
    "events",
    "audit_log",
    "runs",
    "jobs",
    "adapter_results",
    "checks",
    "decisions",
    "review_tasks",
    "poc_tokens",
    "broker_entities",
    "outbox",
    "hmac_v1_observation",
    "hmac_signature_stats",
    "policy_bundles",
    "bundle_pinning_epoch",
}


def _fresh_db(pg: str, name: str) -> str:
    admin = create_engine(pg, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
        conn.execute(text(f"CREATE DATABASE {name}"))
    admin.dispose()
    return pg.rsplit("/", 1)[0] + "/" + name


def _config(url: str) -> AlembicConfig:
    cfg = AlembicConfig(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_downgrade_upgrade(pg: str):
    # A dedicated database so the session-scoped migrated schema is untouched.
    admin = create_engine(pg, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS kyc_migration_test"))
        conn.execute(text("CREATE DATABASE kyc_migration_test"))
    admin.dispose()
    url = pg.rsplit("/", 1)[0] + "/kyc_migration_test"
    cfg = _config(url)

    alembic_command.upgrade(cfg, "head")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES

    # broker seed landed (migration 005 reads the normative JSON)
    with engine.connect() as conn:
        names = {r.name for r in conn.execute(text("SELECT name FROM broker_entities"))}
    assert {"Larus", "Brander", "InterLIR", "Silicon Desert", "IP Trading", "IPXO"} <= names

    alembic_command.downgrade(cfg, "base")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()

    alembic_command.upgrade(cfg, "head")
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    engine.dispose()


def test_010_downgrade_refuses_after_cross_case_reuse(pg: str):
    """PR 5a: once (case-a,key) and (case-b,key) coexist, the old global unique
    on events.idempotency_key cannot be recreated — 010's downgrade must refuse
    loudly rather than delete immutable audit events."""
    url = _fresh_db(pg, "kyc_migration_refuse_test")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.begin() as conn:
        for cid in ("case-a", "case-b"):
            conn.execute(text("INSERT INTO cases (id) VALUES (:c)"), {"c": cid})
            conn.execute(
                text(
                    "INSERT INTO events (id, case_id, idempotency_key, payload_hash, "
                    "event_type, actor_json, payload_json, event_sequence, sequence_backfilled) "
                    "VALUES (:id, :c, 'shared-key', 'h', 'email.verified', "
                    "CAST('{}' AS jsonb), CAST('{}' AS jsonb), 1, false)"
                ),
                {"id": cid + "-ev", "c": cid},
            )
    engine.dispose()

    with pytest.raises(Exception) as exc:  # noqa: PT011 — alembic wraps the RuntimeError
        alembic_command.downgrade(cfg, "009")
    assert "cross-case" in str(exc.value).lower()


# Each entry populates exactly ONE downgrade blocker; all 5 are listed below and
# are complete valid INSERTs against today's schema (every NOT NULL column without
# a default is supplied — cases/events/runs/checks/decisions per db/tables.py). A
# bundle row is inserted first where an FK/hash is needed. No further cases to add.
_BUNDLE = "INSERT INTO policy_bundles (bundle_hash, files_json) VALUES ('h', '{}'::jsonb)"


@pytest.mark.parametrize("populate_sql, ids", [
    (_BUNDLE, "bundle_row"),
    (_BUNDLE + "; INSERT INTO bundle_pinning_epoch (id, activated_at, bundle_hash, "
     "engine_build_id) VALUES (1, now(), 'h', 'eng-1')", "epoch_row"),
    ("INSERT INTO cases (id) VALUES ('c'); "
     "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
     "payload_json, event_sequence) VALUES ('ev','c','k','ph','email.verified','{}'::jsonb,"
     "'{}'::jsonb,1); "  # events.event_sequence is NN with no DB default since migration 008
     "INSERT INTO runs (id, case_id, triggering_event_id, state, engine_build_id) "
     "VALUES ('r','c','ev','QUEUED','eng-1')", "run_engine_id"),   # runs.triggering_event_id NN FK
    ("INSERT INTO cases (id) VALUES ('c'); INSERT INTO checks (id, case_id, check_type, "
     "status, points_awarded, category, source, policy_bundle_hash) "
     "VALUES ('k','c','verified_email','pass',10,'x','seed','h')", "check_bundle_hash"),
    ("INSERT INTO cases (id) VALUES ('c'); INSERT INTO decisions (id, case_id, decision, "
     "score, gates_json, buy_enablement, policy_shas, manual, engine_build_id) "
     "VALUES ('d','c','manual_review_insufficient',0,'{}'::jsonb,'buy_locked_org_id_required',"
     "'{}'::jsonb,false,'eng-1')", "decision_engine_id"),
])
def test_011_downgrade_refuses_after_use(pg, populate_sql, ids):
    url = _fresh_db(pg, f"kyc_mig_011_{ids}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in populate_sql.split("; "):
            conn.execute(text(stmt))
    engine.dispose()
    with pytest.raises(Exception) as exc:      # alembic wraps the RuntimeError
        alembic_command.downgrade(cfg, "010")
    assert "forward-only" in str(exc.value).lower()


def test_011_downgrade_clean_when_unused(pg):
    url = _fresh_db(pg, "kyc_mig_011_clean")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "010")      # empty schema → clean
    alembic_command.upgrade(cfg, "head")


# §8.16: the `CHECK (btrim(col) <> '')` guards must REJECT blank/whitespace on every
# constrained surface. Without these negative tests, dropping any CHECK leaves the
# suite green. Each row is otherwise valid; only the constrained column is `:blank`.
_VALID_BUNDLE = "INSERT INTO policy_bundles (bundle_hash, files_json) VALUES ('h','{}'::jsonb)"
_VALID_CASE = "INSERT INTO cases (id) VALUES ('c')"
_VALID_EVENT = ("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                "actor_json, payload_json, event_sequence) VALUES ('ev','c','k','ph','email.verified',"
                "'{}'::jsonb,'{}'::jsonb,1)")  # event_sequence is NN with no DB default (migration 008)
_NONBLANK_SURFACES = {
    "epoch_engine": _VALID_BUNDLE + "; INSERT INTO bundle_pinning_epoch "
        "(id, activated_at, bundle_hash, engine_build_id) VALUES (1, now(), 'h', :blank)",
    "run_engine": _VALID_CASE + "; " + _VALID_EVENT + "; INSERT INTO runs "
        "(id, case_id, triggering_event_id, state, engine_build_id) VALUES ('r','c','ev','QUEUED',:blank)",
    "decision_engine": _VALID_CASE + "; INSERT INTO decisions (id, case_id, decision, score, "
        "gates_json, buy_enablement, policy_shas, manual, engine_build_id) VALUES ('d','c','x',0,"
        "'{}'::jsonb,'buy_locked_org_id_required','{}'::jsonb,false,:blank)",
    "check_bundle": _VALID_CASE + "; INSERT INTO checks (id, case_id, check_type, status, "
        "points_awarded, category, source, policy_bundle_hash) "
        "VALUES ('k','c','verified_email','pass',10,'x','seed',:blank)",
}


@pytest.mark.parametrize("surface", list(_NONBLANK_SURFACES))
@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "ws"])
def test_011_nonblank_checks_reject_blank(pg, surface, blank):
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_011_nb_{surface}_{len(blank)}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    engine = create_engine(url)
    with pytest.raises(IntegrityError), engine.begin() as conn:  # CHECK (btrim(col) <> '') violation
        for stmt in _NONBLANK_SURFACES[surface].split("; "):
            conn.execute(text(stmt), {"blank": blank})  # extra param ignored where unused
    engine.dispose()
