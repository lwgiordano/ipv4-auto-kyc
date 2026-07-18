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
