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
}


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
