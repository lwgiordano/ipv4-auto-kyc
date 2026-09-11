"""Live configuration schema: immutable history, honest legacy pins, safe downgrade."""

import json
import threading
import time
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from kyc_tool.config import REPO_ROOT
from kyc_tool.policy.loader import read_policy_files
from kyc_tool.policy_store.repo import store_bundle
from kyc_tool.ui.salesforce_projection import FIELD_SOURCES
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


@pytest.fixture
def configuration_db(pg):
    url = _fresh_db(pg, "kyc_mig_024_" + uuid.uuid4().hex[:10])
    cfg = _config(url)
    command.upgrade(cfg, "023")
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    yield engine, cfg
    engine.dispose()


def _revision(conn, *, parent=None, kind="baseline"):
    bundle = store_bundle(conn, read_policy_files(REPO_ROOT / "KYC_Tool_Build_Package" / "machine_readable"))
    revision = conn.execute(
        text(
            "INSERT INTO configuration_revisions "
            "(parent_revision, policy_bundle_hash, brokers_json, mappings_json, created_by, change_kind) "
            "VALUES (:p, :h, '[]', :m, 'test-admin', :k) RETURNING id"
        ),
        {"p": parent, "h": bundle, "k": kind, "m": json.dumps({source: source for source in FIELD_SOURCES})},
    ).scalar_one()
    return revision, bundle


def _run(conn, revision, bundle):
    conn.execute(text("INSERT INTO cases (id) VALUES ('c')"))
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES ('e','c','e','h','x','{}','{}',1)"
        )
    )
    conn.execute(
        text(
            "INSERT INTO runs (id, case_id, triggering_event_id, state, configuration_revision, "
            "policy_bundle_hash) VALUES ('r','c','e','QUEUED',:r,:h)"
        ),
        {"r": revision, "h": bundle},
    )


def test_023_to_head_installs_configuration_without_activation(configuration_db):
    engine, _ = configuration_db
    schema = inspect(engine)
    assert {"configuration_revisions", "configuration_state", "configuration_requests"} <= set(
        schema.get_table_names()
    )
    assert "outbox_ordering_activation" not in schema.get_table_names()
    columns = {c["name"]: c for c in schema.get_columns("runs")}
    for name in ("configuration_revision", "matched_broker_entity_id", "matched_identifier_class"):
        assert columns[name]["nullable"]
    assert str(columns["configuration_revision"]["type"]) == "BIGINT"
    revision_columns = {c["name"]: c for c in schema.get_columns("configuration_revisions")}
    assert set(revision_columns) == {
        "id",
        "schema_version",
        "parent_revision",
        "policy_bundle_hash",
        "brokers_json",
        "mappings_json",
        "created_at",
        "created_by",
        "change_kind",
    }
    assert revision_columns["id"]["identity"]["always"]
    assert {
        tuple(fk["constrained_columns"]) for fk in schema.get_foreign_keys("configuration_revisions")
    } == {("parent_revision",), ("policy_bundle_hash",)}
    assert {c["name"] for c in schema.get_check_constraints("configuration_revisions")} == {
        "ck_configuration_revision_id",
        "ck_configuration_schema_version",
        "ck_configuration_parent",
        "ck_configuration_bundle_hash",
        "ck_configuration_brokers_array",
        "ck_configuration_mappings_object",
        "ck_configuration_created_by",
        "ck_configuration_change_kind",
        "ck_configuration_baseline_parent",
    }
    assert {c["name"] for c in schema.get_columns("configuration_state")} == {"id", "active_revision"}
    assert {c["name"] for c in schema.get_columns("configuration_requests")} == {
        "request_id",
        "request_digest",
        "result_revision",
        "changed",
        "created_at",
    }
    assert {tuple(fk["constrained_columns"]) for fk in schema.get_foreign_keys("configuration_state")} == {
        ("active_revision",)
    }
    assert {tuple(fk["constrained_columns"]) for fk in schema.get_foreign_keys("configuration_requests")} == {
        ("result_revision",)
    }
    assert any(
        fk["constrained_columns"] == ["configuration_revision", "policy_bundle_hash"]
        for fk in schema.get_foreign_keys("runs")
    )
    assert any(
        c["column_names"] == ["id", "policy_bundle_hash"]
        for c in schema.get_unique_constraints("configuration_revisions")
    )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM configuration_state")).scalar_one() == 0
        triggers = set(conn.execute(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")).scalars())
        assert {
            "configuration_revisions_immutable",
            "configuration_requests_immutable",
            "runs_configuration_pin_immutable",
        } <= triggers


@pytest.mark.parametrize(
    "operation",
    ["UPDATE configuration_revisions SET brokers_json='[]'", "DELETE FROM configuration_revisions"],
)
def test_configuration_revision_is_immutable(configuration_db, operation):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, _ = _revision(conn)
    with engine.begin() as conn, pytest.raises(DBAPIError, match="CONFIGURATION_IMMUTABLE"):
        conn.execute(text(operation + " WHERE id=:r"), {"r": revision})


@pytest.mark.parametrize(
    "operation", ["UPDATE configuration_requests SET changed=false", "DELETE FROM configuration_requests"]
)
def test_configuration_request_is_immutable(configuration_db, operation):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, _ = _revision(conn)
        conn.execute(
            text(
                "INSERT INTO configuration_requests (request_id, request_digest, result_revision, changed) "
                "VALUES (:id,:d,:r,true)"
            ),
            {"id": uuid.uuid4(), "d": "a" * 64, "r": revision},
        )
    with engine.begin() as conn, pytest.raises(DBAPIError, match="CONFIGURATION_IMMUTABLE"):
        conn.execute(text(operation))


@pytest.mark.parametrize(
    "assignment",
    [
        "schema_version=2",
        "change_kind='bogus'",
        "brokers_json='{}'",
        "brokers_json='null'",
        "mappings_json='[]'",
        "created_by=''",
        "parent_revision=0",
    ],
)
def test_invalid_revision_shapes_refuse(configuration_db, assignment):
    engine, _ = configuration_db
    column, value = assignment.split("=", 1)
    with engine.begin() as conn:
        _, bundle = _revision(conn)
    values = {
        "schema_version": "1",
        "change_kind": "'baseline'",
        "brokers_json": "'[]'",
        "mappings_json": "'{}'",
        "created_by": "'test-admin'",
        "parent_revision": "NULL",
    }
    values[column] = value
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(
            text(
                "INSERT INTO configuration_revisions (policy_bundle_hash," + ",".join(values) + ") "
                "VALUES (:h," + ",".join(values.values()) + ")"
            ),
            {"h": bundle},
        )


def test_invalid_active_reference_refuses(configuration_db):
    engine, _ = configuration_db
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text("INSERT INTO configuration_state VALUES (1, 999999)"))


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n"])
def test_request_digest_is_exact_lowercase_sha256(configuration_db, digest):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, _ = _revision(conn)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(
            text(
                "INSERT INTO configuration_requests (request_id, request_digest, result_revision, changed) "
                "VALUES (:id,:d,:r,true)"
            ),
            {"id": uuid.uuid4(), "d": digest, "r": revision},
        )


def test_singleton_id_and_parent_kind_constraints_refuse(configuration_db):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, _ = _revision(conn)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(text("INSERT INTO configuration_state VALUES (2,:r)"), {"r": revision})
    with engine.begin() as conn, pytest.raises(DBAPIError):
        _revision(conn, kind="points")
    with engine.begin() as conn, pytest.raises(DBAPIError):
        _revision(conn, parent=revision, kind="baseline")


def test_legacy_run_remains_unversioned_and_cannot_be_retroactively_pinned(configuration_db):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, bundle = _revision(conn)
        _run(conn, None, bundle)
        conn.execute(text("UPDATE runs SET state='BROKER_GATE' WHERE id='r'"))
        assert conn.execute(text("SELECT configuration_revision FROM runs")).scalar_one() is None
    with engine.begin() as conn, pytest.raises(DBAPIError, match="CONFIGURATION_PIN_IMMUTABLE"):
        conn.execute(text("UPDATE runs SET configuration_revision=:r"), {"r": revision})


def test_run_bundle_pair_and_null_bypass_refuse(configuration_db):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, _ = _revision(conn)
    for bundle in ("f" * 64, None):
        with engine.begin() as conn, pytest.raises(DBAPIError):
            _run(conn, revision, bundle)


def test_run_pin_is_immutable_but_ordinary_update_is_legal(configuration_db):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, bundle = _revision(conn)
        other, _ = _revision(conn, parent=revision, kind="brokers")
        _run(conn, revision, bundle)
        conn.execute(
            text("UPDATE runs SET state='BROKER_GATE', configuration_revision=:r WHERE id='r'"),
            {"r": revision},
        )
    for sql in (
        "configuration_revision=NULL",
        "configuration_revision=" + str(other),
        "policy_bundle_hash=NULL",
    ):
        with engine.begin() as conn, pytest.raises(DBAPIError, match="CONFIGURATION_PIN_IMMUTABLE"):
            conn.execute(text("UPDATE runs SET " + sql + " WHERE id='r'"))


@pytest.mark.parametrize("entity,match", [("b", None), (None, "domains"), ("b", "fuzzy")])
def test_match_provenance_requires_closed_paired_values(configuration_db, entity, match):
    engine, _ = configuration_db
    with engine.begin() as conn:
        revision, bundle = _revision(conn)
        _run(conn, revision, bundle)
    with engine.begin() as conn, pytest.raises(DBAPIError):
        conn.execute(
            text("UPDATE runs SET matched_broker_entity_id=:e, matched_identifier_class=:m"),
            {"e": entity, "m": match},
        )


def test_unused_schema_can_downgrade(configuration_db):
    engine, cfg = configuration_db
    command.downgrade(cfg, "023")
    assert "configuration_revisions" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")


@pytest.mark.parametrize("used", ["baseline", "edit", "run"])
def test_downgrade_refuses_after_configuration_use(configuration_db, used):
    engine, cfg = configuration_db
    with engine.begin() as conn:
        revision, bundle = _revision(conn)
        if used == "baseline":
            conn.execute(text("INSERT INTO configuration_state VALUES (1,:r)"), {"r": revision})
        elif used == "edit":
            _revision(conn, parent=revision, kind="points")
        else:
            _run(conn, revision, bundle)
    with pytest.raises(RuntimeError, match="CONFIGURATION_DOWNGRADE_REFUSED"):
        command.downgrade(cfg, "023")
    assert "configuration_revisions" in inspect(engine).get_table_names()


def test_downgrade_refuses_busy_request_insert_before_fk_without_deadlock(configuration_db):
    """I1: parent-before-child blocking locks must not kill a valid request INSERT.

    Two concurrent transactions run the real INSERT and Alembic downgrade. A
    separate observer holds an advisory barrier solely to expose the INSERT's
    natural child-table-lock-before-FK interval; production takes no such lock.
    """
    engine, cfg = configuration_db
    with engine.begin() as conn:
        revision, _ = _revision(conn)
    request_id = uuid.uuid4()
    barrier = 240025
    outcome = {}
    writer_pid = []
    downgrade_done = threading.Event()
    cfg.set_main_option(
        "sqlalchemy.url",
        engine.url.render_as_string(hide_password=False)
        + "?application_name=configuration_downgrade_race&options=-cstatement_timeout=10000",
    )

    def insert_request():
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '10s'"))
                writer_pid.append(conn.execute(text("SELECT pg_backend_pid()")).scalar_one())
                conn.execute(
                    text(
                        "INSERT INTO configuration_requests "
                        "(request_id, request_digest, result_revision, changed) "
                        "SELECT :id,:d,:r,true FROM (SELECT pg_advisory_xact_lock(:barrier)) AS pause"
                    ),
                    {"id": request_id, "d": "a" * 64, "r": revision, "barrier": barrier},
                )
            outcome["writer"] = "committed"
        except Exception as exc:
            outcome["writer"] = exc

    def downgrade():
        try:
            command.downgrade(cfg, "023")
            outcome["downgrade"] = "unexpected success"
        except Exception as exc:
            outcome["downgrade"] = exc
        finally:
            downgrade_done.set()

    def wait_until(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, f"race setup timed out: {outcome}"
            time.sleep(0.01)

    writer = threading.Thread(target=insert_request, daemon=True)
    migration = threading.Thread(target=downgrade, daemon=True)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        observer.execute(text("SELECT pg_advisory_lock(:key)"), {"key": barrier})
        try:
            writer.start()
            wait_until(
                lambda: (
                    writer_pid
                    and observer.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid "
                            "AND relation='configuration_requests'::regclass "
                            "AND mode='RowExclusiveLock' AND granted) "
                            "AND EXISTS (SELECT 1 FROM pg_locks WHERE pid=:pid "
                            "AND locktype='advisory' AND NOT granted)"
                        ),
                        {"pid": writer_pid[0]},
                    ).scalar_one()
                )
            )
            migration.start()
            # Correct NOWAIT refusal finishes immediately. On the old code,
            # prove the exact dangerous parent-held/child-waiting interleaving
            # before releasing INSERT into its FK check.
            wait_until(
                lambda: (
                    downgrade_done.is_set()
                    or observer.execute(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks parent JOIN pg_locks child "
                            "ON child.pid=parent.pid JOIN pg_stat_activity a ON a.pid=parent.pid "
                            "WHERE a.application_name='configuration_downgrade_race' "
                            "AND parent.relation='configuration_revisions'::regclass "
                            "AND parent.mode='AccessExclusiveLock' AND parent.granted "
                            "AND child.relation='configuration_requests'::regclass "
                            "AND child.mode='AccessExclusiveLock' AND NOT child.granted)"
                        )
                    ).scalar_one()
                )
            )
        finally:
            observer.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": barrier})
            writer.join(timeout=12)
            if migration.ident is not None:
                migration.join(timeout=12)
    assert not writer.is_alive() and not migration.is_alive(), outcome
    states = {
        name: getattr(getattr(value, "orig", None), "sqlstate", None) for name, value in outcome.items()
    }
    assert "40P01" not in states.values(), f"deadlock killed a transaction: {states}; {outcome}"
    assert outcome["writer"] == "committed", outcome
    assert isinstance(outcome["downgrade"], RuntimeError), outcome
    assert "MIGRATION_024_CONFIGURATION_DOWNGRADE_BUSY" in str(outcome["downgrade"])
    assert outcome["downgrade"].__cause__.orig.sqlstate == "55P03"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "024"
        assert conn.execute(text("SELECT count(*) FROM configuration_revisions")).scalar_one() == 1
        assert (
            conn.execute(
                text("SELECT result_revision FROM configuration_requests WHERE request_id=:id"),
                {"id": request_id},
            ).scalar_one()
            == revision
        )
