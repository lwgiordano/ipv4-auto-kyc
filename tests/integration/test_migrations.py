"""Phase 0 acceptance: all tables migrate up and down cleanly."""

import json
import threading
import time

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text

from kyc_tool.config import REPO_ROOT, ProcessRole
from kyc_tool.db.session import make_engine as _mk_engine
from kyc_tool.db.session import make_session_factory as _mk_sf
from kyc_tool.outbox.publisher import OutboxPublisher

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

    # 018 is forward-only by design (re-audit `cbb783b` F3 — walking below it would restore
    # search-path-vulnerable authority functions), so the down-to-base leg runs from 017. The
    # 017->018->refusal path is proven separately in test_migration_018.py.
    alembic_command.upgrade(cfg, "017")
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
    # 017, not head: 018 is forward-only, so a head-anchored walk would refuse at 018 and never
    # reach the 010 blocker this test is about (018's own refusal is proven in its own suite).
    alembic_command.upgrade(cfg, "017")

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
     "'{}'::jsonb,true,'eng-1')", "decision_engine_id"),
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
    alembic_command.upgrade(cfg, "017")        # 018 is forward-only; walk from below it
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
    "epoch_engine": ("ck_epoch_engine_nonblank", _VALID_BUNDLE + "; INSERT INTO bundle_pinning_epoch "
        "(id, activated_at, bundle_hash, engine_build_id) VALUES (1, now(), 'h', :blank)"),
    "run_engine": ("ck_runs_engine_build_id_nonblank", _VALID_CASE + "; " + _VALID_EVENT
        + "; INSERT INTO runs (id, case_id, triggering_event_id, state, engine_build_id) "
        "VALUES ('r','c','ev','QUEUED',:blank)"),
    "decision_engine": ("ck_decisions_engine_build_id_nonblank", _VALID_CASE
        + "; INSERT INTO decisions (id, case_id, decision, score, "
        "gates_json, buy_enablement, policy_shas, manual, engine_build_id) VALUES ('d','c','x',0,"
        "'{}'::jsonb,'buy_locked_org_id_required','{}'::jsonb,true,:blank)"),
    "check_bundle": ("ck_checks_policy_bundle_hash_nonblank", _VALID_CASE
        + "; INSERT INTO checks (id, case_id, check_type, status, "
        "points_awarded, category, source, policy_bundle_hash) "
        "VALUES ('k','c','verified_email','pass',10,'x','seed',:blank)"),
}


@pytest.mark.parametrize("surface", list(_NONBLANK_SURFACES))
@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "ws"])
def test_011_nonblank_checks_reject_blank(pg, surface, blank):
    from sqlalchemy.exc import IntegrityError

    constraint_name, sql = _NONBLANK_SURFACES[surface]
    url = _fresh_db(pg, f"kyc_mig_011_nb_{surface}_{len(blank)}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "head")
    engine = create_engine(url)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        for stmt in sql.split("; "):
            conn.execute(text(stmt), {"blank": blank})  # extra param ignored where unused
    assert constraint_name in str(exc.value)  # F3: the INTENDED btrim CHECK, not a 013 shape CHECK
    engine.dispose()


# P1 fix: migration 011 adds the three provenance CHECKs NOT VALID (metadata-only,
# no scan); revision 012 VALIDATEs them separately (SHARE UPDATE EXCLUSIVE, does not
# block concurrent writers). `pg_constraint.convalidated` is the ground truth for
# "has this CHECK been proven against pre-existing rows yet".
_PINNING_CHECK_NAMES = (
    "ck_checks_policy_bundle_hash_nonblank",
    "ck_runs_engine_build_id_nonblank",
    "ck_decisions_engine_build_id_nonblank",
)
_PINNING_CONVALIDATED_QUERY = text(
    "SELECT conname, convalidated FROM pg_constraint WHERE conname IN "
    "('ck_checks_policy_bundle_hash_nonblank', 'ck_runs_engine_build_id_nonblank', "
    "'ck_decisions_engine_build_id_nonblank')"
)


def test_011_checks_not_valid_then_012_validates(pg):
    """011's three provenance CHECKs land NOT VALID (convalidated=false); 012's
    VALIDATE CONSTRAINT flips all three to true without weakening enforcement.

    Rows are populated at revision 010 — before the provenance columns exist —
    so 011 adds them all-NULL on already-populated cases/events/runs/checks/
    decisions, the real "validate would have to scan existing rows" shape a
    rolling deploy hits on a production-sized audit table."""
    url = _fresh_db(pg, "kyc_mig_011_notvalid")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "010")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_VALID_CASE))
        conn.execute(text(_VALID_EVENT))
        conn.execute(
            text(
                "INSERT INTO runs (id, case_id, triggering_event_id, state) "
                "VALUES ('r','c','ev','QUEUED')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, "
                "category, source) VALUES ('k','c','verified_email','pass',10,'x','seed')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO decisions (id, case_id, decision, score, gates_json, "
                "buy_enablement, policy_shas, manual) VALUES ('d','c',"
                "'manual_review_insufficient',0,'{}'::jsonb,'buy_locked_org_id_required',"
                "'{}'::jsonb,true)"
            )
        )

    alembic_command.upgrade(cfg, "011")  # NOT VALID adds only — must not scan/validate yet
    with engine.connect() as conn:
        rows = {r.conname: r.convalidated for r in conn.execute(_PINNING_CONVALIDATED_QUERY)}
    assert set(rows) == set(_PINNING_CHECK_NAMES)
    assert all(v is False for v in rows.values()), rows

    alembic_command.upgrade(cfg, "head")  # 012 VALIDATEs all three
    with engine.connect() as conn:
        rows = {r.conname: r.convalidated for r in conn.execute(_PINNING_CONVALIDATED_QUERY)}
    assert set(rows) == set(_PINNING_CHECK_NAMES)
    assert all(v is True for v in rows.values()), rows

    from sqlalchemy.exc import IntegrityError

    # Enforcement is orthogonal to validity (NOT VALID already enforced new rows) —
    # this just confirms 012's VALIDATE didn't loosen anything.
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, "
                "category, source, policy_bundle_hash) VALUES ('k2','c','provenance_probe',"
                "'pass',10,'x','seed','   ')"
            )
        )
    assert "ck_checks_policy_bundle_hash_nonblank" in str(exc.value)
    engine.dispose()


def test_012_validate_does_not_block_writers(pg):
    """Concurrent-writer witness: VALIDATE CONSTRAINT takes SHARE UPDATE EXCLUSIVE,
    which is compatible with a concurrent uncommitted writer's RowExclusiveLock, so
    it must complete without hitting a short lock_timeout. If a future regression
    folds VALIDATE back into a validating ADD CONSTRAINT (ACCESS EXCLUSIVE), this
    blocks on connection A and times out."""
    url = _fresh_db(pg, "kyc_mig_012_lock")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "011")

    engineA = create_engine(url)
    engineB = create_engine(url)
    connA = engineA.connect()
    txA = connA.begin()
    try:
        connA.execute(text(_VALID_CASE))
        connA.execute(
            text(
                "INSERT INTO checks (id, case_id, check_type, status, points_awarded, "
                "category, source) VALUES ('k','c','verified_email','pass',10,'x','seed')"
            )
        )  # uncommitted: holds a RowExclusiveLock on checks, never committed nor rolled back yet

        with engineB.begin() as connB:  # separate connection: the concurrent-writer witness
            connB.execute(text("SET lock_timeout = '5s'"))
            connB.execute(
                text("ALTER TABLE checks VALIDATE CONSTRAINT ck_checks_policy_bundle_hash_nonblank")
            )  # must succeed without raising — a validating ADD here would block on A and time out
    finally:
        txA.rollback()
        connA.close()
    engineA.dispose()
    engineB.dispose()


# --- PR 7b-core: migration 013 (outbox stream separation + local decision ordering) ---

def _seed_legacy_callback(
    conn, *, case_id, run_id, decision_id, ev_seq, status="pending", decided_at=None, payload=None
):
    """Seed one valid case→event→run→automatic-decision→decision_callback chain at schema
    012 (no ordering_stream / decision_sequence columns yet), the callback in `status`
    (pending | delivered | dead). outbox.id order is enqueue order; decided_at defaults to
    txn-start now() unless pinned; `payload` (a JSON string) defaults to '{}' — the restore
    acceptance test passes a nested non-ASCII body because an empty one matches anything.
    Runs inside the caller's transaction. Returns outbox id."""
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": case_id})
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
            "actor_json, payload_json, event_sequence) VALUES (:e,:c,:k,'h','kyb.run_requested',"
            "'{}'::jsonb,'{}'::jsonb,:s)"
        ),
        {"e": run_id + "-ev", "c": case_id, "k": run_id, "s": ev_seq},
    )
    conn.execute(
        text("INSERT INTO runs (id, case_id, triggering_event_id, "
             "state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
        {"r": run_id, "c": case_id, "e": run_id + "-ev"},
    )
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
            "policy_shas, manual, decided_at) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,'enabled',"
            "'{}'::jsonb,false, COALESCE(:t, now()))"
        ),
        {"d": decision_id, "c": case_id, "r": run_id, "t": decided_at},
    )
    # a delivered callback carries delivered_at; pending/dead do not (012 has no lifecycle CHECK yet,
    # but seed the FINAL-schema-valid shape so later tasks' constraints stay green on this fixture).
    da = "now()" if status == "delivered" else "NULL"
    oid = conn.execute(
        text(
            f"INSERT INTO outbox (kind, case_id, run_id, payload_json, status, delivered_at) "
            f"VALUES ('decision_callback',:c,:r,CAST(:p AS jsonb),:st,{da}) RETURNING id"
        ),
        {"c": case_id, "r": run_id, "st": status, "p": payload if payload is not None else "{}"},
    ).scalar_one()
    return oid


def test_013_upgrade_sets_stream_and_notnull_metadata(pg):
    """Upgrade over the FULL legacy shape: pending + delivered(timestamped) + dead callbacks,
    a poc_email, and a manual decision — all must survive the SET NOT NULL + CHECKs."""
    url = _fresh_db(pg, "kyc_mig_013_streams")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_legacy_callback(conn, case_id="c1", run_id="r1", decision_id="d1", ev_seq=1, status="pending")
        _seed_legacy_callback(conn, case_id="c1", run_id="r2", decision_id="d2", ev_seq=2, status="delivered")
        _seed_legacy_callback(conn, case_id="c1", run_id="r3", decision_id="d3", ev_seq=3, status="dead")
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status) "
                          "VALUES ('poc_email','c1','{\"to\":\"a@b\"}'::jsonb,'pending')"))
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                          "buy_enablement, policy_shas, manual) VALUES "
                          "('dm','c1',NULL,'approve',0,'{}'::jsonb,"
                          "'enabled','{}'::jsonb,true)"))

    alembic_command.upgrade(cfg, "013")

    with engine.connect() as conn:
        streams = {
            r.kind: r.ordering_stream
            for r in conn.execute(text("SELECT kind, ordering_stream FROM outbox"))
        }
        assert streams == {"decision_callback": "decision", "poc_email": "email"}
        # real SET NOT NULL — the column metadata, not merely a value CHECK (F4)
        meta = {
            r.column_name: r.is_nullable
            for r in conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name='outbox' AND column_name IN ('ordering_stream','case_id')"
                )
            )
        }
        assert meta == {"ordering_stream": "NO", "case_id": "NO"}
        assert conn.execute(
            text("SELECT last_decision_sequence FROM cases WHERE id='c1'")
        ).scalar_one() == 3  # backfilled: c1 has three automatic decisions (pending/delivered/dead)
    engine.dispose()


def test_013_up_down_up_clean_no_supersession(pg):
    url = _fresh_db(pg, "kyc_mig_013_roundtrip")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    alembic_command.downgrade(cfg, "012")
    alembic_command.upgrade(cfg, "013")  # clean re-upgrade on a no-superseded DB

def test_013_claim_indexes_are_partial_to_the_claimable_set(pg):
    """re-review 0ca264b F3: 013 makes decision_callback rows non-prunable, so `outbox` grows
    without bound. Both claim indexes must therefore be PARTIAL to `status='pending'` — otherwise
    an ever-growing tail of delivered/superseded terminals enters the claim path's index and its
    plan. Pins the predicates from pg_indexes so a full index cannot creep back, and asserts the
    downgrade restores 006's full form."""
    url = _fresh_db(pg, "kyc_mig_013_partial_idx")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.connect() as conn:
        defs = {
            r.indexname: r.indexdef
            for r in conn.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename='outbox'"))
        }
    for name in ("ix_outbox_claim", "ix_outbox_stream_claim"):
        assert "WHERE (status = 'pending'::text)" in defs[name], defs[name]
    # status is pinned by the predicate, so it must not also sit in the key
    assert "status" not in defs["ix_outbox_stream_claim"].split(" WHERE ")[0]

    alembic_command.downgrade(cfg, "012")
    with engine.connect() as conn:
        legacy = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename='outbox' "
            "AND indexname='ix_outbox_claim'")).scalar_one()
    assert "WHERE" not in legacy and "status" in legacy  # 006's full (status, next_attempt_at)
    engine.dispose()


# INSERT negatives: each row is otherwise valid; only the constrained column is bad. Each case
# pins the constraint (or NOT NULL message) it is INTENDED to trip — same discipline as
# _NONBLANK_SURFACES. Without the pin, a later-task constraint that rejects the row for an
# unrelated reason (Task 4's kind/stream identity CHECK rejects every ('poc_email','decision')
# shape) would keep these green while the constraint under test silently disappeared.
_OUTBOX_LIFECYCLE_BAD = {
    # PR 7b-core (Task 4): ck_outbox_kind_stream_identity's two OR-branches require the
    # (kind, ordering_stream) pair to be exactly one of the two valid shapes, so an
    # out-of-vocab kind or stream ALWAYS also violates identity — no seed-row shape can
    # isolate ck_outbox_kind_vocab / ck_outbox_ordering_stream_vocab from it anymore.
    # Postgres evaluates CHECK constraints in alphabetical-by-name order (verified against
    # real Postgres), and "ck_outbox_kind_stream_identity" sorts before both — so it is the
    # one that deterministically fires now. Re-pinned to the constraint that ACTUALLY (and
    # reproducibly) rejects the row post-013, per the still-exact-name-match discipline.
    "unknown_kind": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('unknown','c1','decision','pending')"),
    "null_stream": ('null value in column "ordering_stream"',
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1',NULL,'pending')"),
    "unknown_stream": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','carrier-pigeon','pending')"),
    "orphan_case": ("fk_outbox_case_id",
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','nope','email','pending')"),
    "null_case": ('null value in column "case_id"',
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email',NULL,'email','pending')"),
    "pending_delivered_at": ("ck_outbox_status_lifecycle",  # pending ⇒ delivered_at NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at) "
        "VALUES ('poc_email','c1','email','pending', now())"),
    "pending_resolved_at": ("ck_outbox_status_lifecycle",  # pending ⇒ resolved_at NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
        "VALUES ('poc_email','c1','email','pending', now())"),
    "delivered_no_delivered_at": ("ck_outbox_status_lifecycle",  # delivered ⇒ delivered_at NOT NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','email','delivered')"),
    "dead_with_claim": ("ck_outbox_status_lifecycle",  # dead ⇒ claim tuple all-NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, claim_token, "
        "claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email','dead', "
        "gen_random_uuid(), now(), 'w1')"),
    "orphan_claimed_by": ("ck_outbox_status_lifecycle",  # partial claim tuple
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, claimed_by) "
        "VALUES ('poc_email','c1','email','pending','w1')"),
    "superseded_null_resolved": ("ck_outbox_status_lifecycle",  # superseded ⇒ resolved_at NOT NULL
        # (also trips the F8 decision-only conjunct; the dedicated F8 negatives below isolate it)
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','email','superseded')"),
    "delivered_with_claim": ("ck_outbox_status_lifecycle",  # delivered ⇒ claim tuple all-NULL
        "INSERT INTO outbox (kind, case_id, ordering_stream, status, delivered_at, "
        "claim_token, claim_lease_expires_at, claimed_by) VALUES ('poc_email','c1','email',"
        "'delivered', now(), gen_random_uuid(), now(), 'w1')"),
}


@pytest.mark.parametrize("case", list(_OUTBOX_LIFECYCLE_BAD))
def test_013_outbox_lifecycle_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    expected, sql = _OUTBOX_LIFECYCLE_BAD[case]
    url = _fresh_db(pg, f"kyc_mig_013_neg_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(sql))
    assert expected in str(exc.value)  # the INTENDED constraint, not an unrelated later one
    engine.dispose()


def test_013_outbox_lifecycle_update_negative(pg):
    """A live pending row cannot be UPDATEd into an illegal (superseded, NULL resolved_at)
    tuple — the lifecycle CHECK fires at commit on UPDATE too, not only INSERT."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_upd_neg")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(
            text(
                "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                "VALUES ('poc_email','c1','email','pending')"
            )
        )
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET status='superseded' WHERE case_id='c1'"))
    assert "ck_outbox_status_lifecycle" in str(exc.value)  # the INTENDED constraint, by name
    engine.dispose()


def test_013_superseded_is_decision_only_insert_negative(pg):
    """Re-audit F8: a poc_email cannot be INSERTed as 'superseded' even with an otherwise
    VALID superseded shape (resolved_at set, claim tuple all-NULL) — only the decision-only
    kind conjunct of ck_outbox_status_lifecycle rejects it, asserted by name."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_sup_poc_ins")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status, resolved_at) "
                          "VALUES ('poc_email','c1','email','superseded', now())"))
    assert "ck_outbox_status_lifecycle" in str(exc.value)
    engine.dispose()


def test_013_superseded_is_decision_only_update_negative(pg):
    """Re-audit F8 UPDATE variant: a live pending poc_email cannot be UPDATEd into
    'superseded' even when resolved_at is set in the same statement (the future-bug /
    operator-UPDATE path that would silently zero-send an email AND make downgrade refuse)."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_sup_poc_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                          "VALUES ('poc_email','c1','email','pending')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET status='superseded', resolved_at=now() "
                          "WHERE case_id='c1'"))
    assert "ck_outbox_status_lifecycle" in str(exc.value)
    engine.dispose()


def test_013_backfill_orders_by_outbox_id_and_delivers_without_false_supersession(pg, settings):
    """Threaded inversion (rev-4 F1): B's txn starts FIRST and fixes its decided_at via
    SELECT now(); A then runs fully and enqueues its callback FIRST (lower outbox.id) with a
    LATER decided_at; only then is B released to enqueue (higher outbox.id). So
    B.decided_at < A.decided_at while A.outbox_id < B.outbox_id. The backfill must rank by
    outbox.id (A=seq 1, B=seq 2); delivering both via the REAL publisher then sends A→B, both
    delivered, neither superseded, B last. Mutation ORDER BY d.decided_at reverses assignment
    and (at head, with the guard) supersedes B — failing this complete test."""
    url = _fresh_db(pg, "kyc_mig_013_inversion")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    eng = create_engine(url)
    with eng.begin() as conn:  # commit the case FIRST (both chains reference it)
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))

    clock_fixed = threading.Event()
    a_done = threading.Event()

    b_engine = create_engine(url)  # owned by the test, disposed in the finally below

    def run_b():
        cb = b_engine.connect()
        tx = cb.begin()
        cb.execute(text("SELECT now()"))  # fixes B's txn-start now() (B.decided_at) EARLY
        clock_fixed.set()
        a_done.wait(timeout=10)  # let A fully commit first (A gets the lower outbox.id)
        cb.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                        "actor_json, payload_json, event_sequence) VALUES ('evB','c1','rB','h','x',"
                        "'{}'::jsonb,'{}'::jsonb,2)"))
        cb.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                        "VALUES ('rB','c1','evB','PUBLISH_DECISION')"))
        cb.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                        "buy_enablement, policy_shas, manual) VALUES ('dB','c1','rB','approve',10,"
                        "'{}'::jsonb,'enabled','{}'::jsonb,false)"))
        cb.execute(text("INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                        "VALUES ('decision_callback','c1','rB', "
                        "CAST('{\"run_id\":\"rB\"}' AS jsonb),'pending')"))
        tx.commit()
        cb.close()

    tb = threading.Thread(target=run_b, name="inversion-B")
    tb.start()
    # B parks inside `a_done.wait()` holding an OPEN transaction. If anything below raises before
    # `a_done.set()`, B blocks for its full timeout on a connection nobody closes, and the engine
    # it borrowed from is never disposed — so the release, the join and the dispose all belong in
    # a finally, and each wait must be ASSERTED: a silent `wait()` timeout here would let the test
    # proceed with B's clock unfixed and quietly stop proving the inversion it exists to prove.
    try:
        assert clock_fixed.wait(timeout=10), "B never fixed its transaction clock"
        time.sleep(0.1)  # ensure A's txn-start now() is strictly LATER than B's fixed clock
        with eng.begin() as connA:  # A runs fully + commits → lower outbox.id, later decided_at
            connA.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, "
                               "event_type, actor_json, payload_json, event_sequence) VALUES "
                               "('evA','c1','rA','h','x','{}'::jsonb,'{}'::jsonb,1)"))
            connA.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                               "VALUES ('rA','c1','evA','PUBLISH_DECISION')"))
            connA.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, "
                               "gates_json, buy_enablement, policy_shas, manual) VALUES "
                               "('dA','c1','rA','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"))
            connA.execute(text("INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
                               "VALUES ('decision_callback','c1','rA', "
                               "CAST('{\"run_id\":\"rA\"}' AS jsonb),'pending')"))
    finally:
        a_done.set()
        tb.join(timeout=15)
        b_engine.dispose()
    assert not tb.is_alive(), "thread B still running after join — its transaction is still open"

    with eng.connect() as conn:
        a_dt, a_oid = conn.execute(text("SELECT d.decided_at, o.id FROM decisions d "
                                        "JOIN outbox o ON o.run_id=d.run_id WHERE d.id='dA'")).one()
        b_dt, b_oid = conn.execute(text("SELECT d.decided_at, o.id FROM decisions d "
                                        "JOIN outbox o ON o.run_id=d.run_id WHERE d.id='dB'")).one()
    assert b_dt < a_dt and a_oid < b_oid  # the inversion is real and deterministic

    alembic_command.upgrade(cfg, "013")

    with eng.connect() as conn:
        seqs = {r.id: r.decision_sequence
                for r in conn.execute(text("SELECT id, decision_sequence FROM decisions WHERE case_id='c1'"))}
        assert seqs == {"dA": 1, "dB": 2}  # by outbox.id, NOT decided_at
        assert conn.execute(text("SELECT last_decision_sequence FROM cases WHERE id='c1'")).scalar_one() == 2

    # deliver both via the REAL publisher against this dedicated DB; assert order + terminals.
    # The publisher is head-schema code — it commits a pre-HTTP attempt row (014) before every
    # send — so bring the DB to head first, exactly as production would before publishers start.
    alembic_command.upgrade(cfg, "head")
    order: list[str] = []

    def handler(request):
        order.append(json.loads(request.content).get("run_id"))
        return httpx.Response(200)

    pub = OutboxPublisher(_mk_sf(_mk_engine(url)), settings,
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                                 process_role=ProcessRole.OUTBOX_WORKER)
    assert pub.process_pending() == 2
    assert order == ["rA", "rB"]  # A (seq 1) delivered before B (seq 2)
    with eng.connect() as conn:
        statuses = dict(conn.execute(text(
            "SELECT run_id, status FROM outbox WHERE case_id='c1' ORDER BY decision_sequence")).all())
    assert statuses == {"rA": "delivered", "rB": "delivered"}  # neither superseded; B last
    eng.dispose()


# One invalid legacy state per parity check (schema 012). Imported by Task 7's CLI test so the
# CLI and the migration are proven to refuse on the SAME matrix. Each is a complete INSERT set.
_BAD_CASE = "INSERT INTO cases (id) VALUES ('c1')"
_BAD_EV = ("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
           "payload_json, event_sequence) VALUES (:e,'c1',:e,'h','x','{}'::jsonb,'{}'::jsonb,:s)")
_BAD_RUN = "INSERT INTO runs (id, case_id, triggering_event_id, state) VALUES (:r,'c1',:e,'PUBLISH_DECISION')"

_PARITY_BAD_SEEDS: dict[str, list[str]] = {
    "missing_callback": [_BAD_CASE, _BAD_EV, _BAD_RUN,  # decision, no callback
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)"],
    "orphan_callback": [_BAD_CASE,  # callback whose run has no automatic decision
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1','ghost','{}'::jsonb,'pending')"],
    "duplicate_callback_per_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',:r,'{}'::jsonb,'pending')",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',:r,'{}'::jsonb,'pending')"],
    "duplicate_auto_decision_per_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d1','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d2','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',:r,'{}'::jsonb,'pending')"],
    "null_run_callback": [_BAD_CASE,  # callback with run_id NULL (also orphan — either name refuses)
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c1',NULL,'{}'::jsonb,'pending')"],
    "null_or_orphan_outbox_case": [_BAD_CASE,  # poc_email with case_id NULL
        "INSERT INTO outbox (kind, case_id, payload_json, "
        "status) VALUES ('poc_email',NULL,'{}'::jsonb,'pending')"],
    "callback_case_ne_decision_case": [_BAD_CASE, "INSERT INTO cases (id) VALUES ('c2')", _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "  # callback on c2, decision on c1
        "VALUES ('decision_callback','c2',:r,'{}'::jsonb,'pending')"],
    "decision_case_ne_run_case": [_BAD_CASE, "INSERT INTO cases (id) VALUES ('c2')", _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, "
        "gates_json, buy_enablement, "  # run on c1, decision on c2
        "policy_shas, manual) VALUES ('d','c2',:r,'approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false)",
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('decision_callback','c2',:r,'{}'::jsonb,'pending')"],
    "unknown_kind": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, "
        "status) VALUES ('weird','c1','{}'::jsonb,'pending')"],
    "poc_row_with_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO outbox (kind, case_id, run_id, payload_json, status) "
        "VALUES ('poc_email','c1',:r,'{}'::jsonb,'pending')"],
    "manual_decision_with_run": [_BAD_CASE, _BAD_EV, _BAD_RUN,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',:r,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true)"],
    "auto_decision_null_run": [_BAD_CASE,
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled','{}'::jsonb,false)"],
    # Legacy lifecycle rows (valid case/kind, no run) that pass every OTHER parity check but the
    # final ck_outbox_status_lifecycle rejects — the exact "step-0 green, migration fails mid-outage"
    # gap (F2). Each targets invalid_legacy_outbox_lifecycle.
    "lifecycle_pending_delivered_at": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
        "VALUES ('poc_email','c1','{}'::jsonb,'pending', now())"],
    "lifecycle_delivered_null_at": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
        "VALUES ('poc_email','c1','{}'::jsonb,'delivered', NULL)"],
    "lifecycle_dead_delivered_at": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
        "VALUES ('poc_email','c1','{}'::jsonb,'dead', now())"],
    "lifecycle_unknown_status": [_BAD_CASE,
        "INSERT INTO outbox (kind, case_id, payload_json, status) "
        "VALUES ('poc_email','c1','{}'::jsonb,'weird_status')"],
}

# Seed key → the PARITY_CHECKS name that MUST appear in the refusal (some rows trip several checks;
# this pins the one each seed targets). missing_callback additionally prints the BLOCKED sentinel.
_PARITY_SEED_VIOLATION = {
    "missing_callback": "missing_callback",
    "orphan_callback": "orphan_callback",
    "duplicate_callback_per_run": "duplicate_callback_per_run",
    "duplicate_auto_decision_per_run": "duplicate_auto_decision_per_run",
    "null_run_callback": "null_run_callback",
    "null_or_orphan_outbox_case": "null_or_orphan_outbox_case",
    "callback_case_ne_decision_case": "callback_case_ne_decision_case",
    "decision_case_ne_run_case": "decision_case_ne_run_case",
    "unknown_kind": "unknown_kind",
    "poc_row_with_run": "poc_row_with_run",
    "manual_decision_with_run": "manual_decision_with_run",
    "auto_decision_null_run": "auto_decision_null_run",
    "lifecycle_pending_delivered_at": "invalid_legacy_outbox_lifecycle",
    "lifecycle_delivered_null_at": "invalid_legacy_outbox_lifecycle",
    "lifecycle_dead_delivered_at": "invalid_legacy_outbox_lifecycle",
    "lifecycle_unknown_status": "invalid_legacy_outbox_lifecycle",
}


def _seed_parity_bad(engine, name):
    with engine.begin() as conn:
        for stmt in _PARITY_BAD_SEEDS[name]:
            conn.execute(text(stmt), {"e": "ev", "r": "r", "s": 1})


def test_013_backfill_refuses_missing_callback_byte_stable(pg):
    """A valid manual=false decision with NO surviving callback → fail closed with the exact
    BLOCKED_NO_AUTHORITATIVE_MAPPING sentinel + ids (no decided_at fallback)."""
    url = _fresh_db(pg, "kyc_mig_013_missing_cb")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, "missing_callback")
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:  # migration raises RuntimeError; alembic propagates it
        alembic_command.upgrade(cfg, "013")
    msg = str(exc.value)
    assert "BLOCKED_NO_AUTHORITATIVE_MAPPING" in msg and "'d'" in msg  # sentinel + actionable id


@pytest.mark.parametrize("name", list(_PARITY_BAD_SEEDS))
def test_013_upgrade_refuses_parity_violation_before_ddl(pg, name):
    """Every invalid legacy state makes the 013 upgrade refuse BEFORE any DDL — the parity
    preflight runs first, so after the failure the 013 columns are absent (txn rolled back)."""
    url = _fresh_db(pg, f"kyc_mig_013_parity_{name}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    _seed_parity_bad(engine, name)
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:
        alembic_command.upgrade(cfg, "013")
    msg = str(exc.value)
    assert _PARITY_SEED_VIOLATION[name] in msg or "BLOCKED_NO_AUTHORITATIVE_MAPPING" in msg
    engine = create_engine(url)
    with engine.connect() as conn:
        cols = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='outbox'"))}
    assert "claim_token" not in cols  # no DDL landed — refusal was before the column adds
    engine.dispose()


def test_013_valid_lifecycle_rows_upgrade_clean(pg):
    """Valid pending / delivered(timestamped) / dead poc_email rows are NOT flagged by the
    lifecycle parity check and survive the 013 upgrade + its ck_outbox_status_lifecycle."""
    url = _fresh_db(pg, "kyc_mig_013_valid_lifecycle")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'pending')"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status, delivered_at) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'delivered', now())"))
        conn.execute(text("INSERT INTO outbox (kind, case_id, payload_json, status) "
                          "VALUES ('poc_email','c1','{}'::jsonb,'dead')"))
    engine.dispose()
    alembic_command.upgrade(cfg, "013")  # no refusal; all three legacy rows are valid


def _mk_case(conn, c="c1"):
    conn.execute(text("INSERT INTO cases (id) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": c})


def _mk_auto_decision(conn, *, d, c, r, seq, ev_seq):
    conn.execute(
        text(
            "INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, actor_json, "
            "payload_json, event_sequence) VALUES (:e,:c,:k,'h','x','{}'::jsonb,'{}'::jsonb,:s)"
        ),
        {"e": r + "-ev", "c": c, "k": r, "s": ev_seq},
    )
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, "
                      "state) VALUES (:r,:c,:e,'PUBLISH_DECISION')"),
                 {"r": r, "c": c, "e": r + "-ev"})
    conn.execute(
        text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
            "policy_shas, manual, decision_sequence) VALUES (:d,:c,:r,'approve',10,'{}'::jsonb,'enabled',"
            "'{}'::jsonb,false,:seq)"
        ),
        {"d": d, "c": c, "r": r, "seq": seq},
    )


def test_013_two_runs_same_case_sequence_rejected(pg):
    """Backstopped by uq_decisions_case_decision_sequence — NOT the outbox partial index.
    Two distinct valid runs/decisions at the same (case_id, positive decision_sequence)
    must fail at commit; multiple manual NULL-sequence decisions stay legal."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_dupseq")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        _mk_auto_decision(conn, d="dB", c="c1", r="rB", seq=1, ev_seq=2)  # same case+seq, other run
    assert "uq_decisions_case_decision_sequence" in str(exc.value)
    with engine.begin() as conn:  # multiple manual NULL-sequence decisions remain legal
        for i in range(2):
            conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                              "buy_enablement, policy_shas, manual) VALUES "
                              "(:d,'c1',NULL,'approve',0,'{}'::jsonb,"
                              "'enabled','{}'::jsonb,true)"), {"d": f"dm{i}"})
    engine.dispose()


def test_013_two_decisions_same_run_rejected(pg):
    """uq_decisions_run_id: at most one automatic decision per run."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_duprun")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:  # same run_id, distinct seq/id
        conn.execute(text("INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
                          "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
                          "('dB','c1','rA','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,2)"))
    assert "uq_decisions_run_id" in str(exc.value)
    engine.dispose()


# INSERT negatives on `decisions` — each names the constraint it targets.
_DECISION_IDENTITY_BAD = {
    "manual_with_run": ("ck_decisions_manual_sequence",  # manual row carrying a run_id
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled','{}'::jsonb,true)"),
    "manual_with_seq": ("ck_decisions_manual_sequence",  # manual row carrying a decision_sequence
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,true,1)"),
    "auto_zero_seq": ("ck_decisions_manual_sequence",  # automatic row with zero sequence
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,false,0)"),
    "auto_negative_seq": ("ck_decisions_manual_sequence",  # automatic row with negative sequence
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,false,-1)"),
    "auto_null_run": ("ck_decisions_manual_sequence",  # automatic row with NULL run
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual, decision_sequence) VALUES ('d','c1',NULL,'approve',0,'{}'::jsonb,'enabled',"
        "'{}'::jsonb,false,1)"),
    "auto_null_sequence": ("ck_decisions_manual_sequence",  # automatic row, run set, sequence NULL (F2 hole)
        "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, buy_enablement, "
        "policy_shas, manual) VALUES ('d','c1','rX','approve',0,'{}'::jsonb,'enabled','{}'::jsonb,false)"),
}


@pytest.mark.parametrize("case", list(_DECISION_IDENTITY_BAD))
def test_013_decision_identity_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    expected, sql = _DECISION_IDENTITY_BAD[case]
    url = _fresh_db(pg, f"kyc_mig_013_di_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                          "actor_json, payload_json, event_sequence) VALUES ('rX-ev','c1','rX','h','x',"
                          "'{}'::jsonb,'{}'::jsonb,1)"))
        conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                          "VALUES ('rX','c1','rX-ev','PUBLISH_DECISION')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(sql))
    assert expected in str(exc.value)
    engine.dispose()


@pytest.mark.parametrize("bad", ["0", "NULL"], ids=["zero", "auto_null_sequence"])
def test_013_decision_identity_update_negative(pg, bad):
    """UPDATE variants: a valid automatic decision cannot be UPDATEd into a zero OR NULL
    sequence — ck_decisions_manual_sequence fires on UPDATE too (F2)."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, f"kyc_mig_013_di_upd_{bad.lower()}")  # Postgres folds unquoted "NULL" -> "null"
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(f"UPDATE decisions SET decision_sequence={bad} WHERE id='dA'"))
    assert "ck_decisions_manual_sequence" in str(exc.value)
    engine.dispose()


# INSERT negatives on `outbox` — each names the constraint it targets.
_OUTBOX_BINDING_BAD = {
    "callback_null_run": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1',NULL,'decision',1,'pending')"),
    # ck_outbox_kind_stream_identity, verified against REAL Postgres (see task-4-report.md
    # follow-up fix): a callback row with decision_sequence 0 or negative structurally violates
    # BOTH this CHECK and fk_outbox_decision_triple (no decisions row with a non-positive
    # sequence can ever exist, since ck_decisions_manual_sequence requires decision_sequence > 0
    # on every automatic row) — but Postgres validates CHECK/NOT NULL constraints synchronously
    # in the executor BEFORE a FK's AFTER-ROW trigger ever runs, so for a row violating both, the
    # CHECK's error is what Postgres actually raises; the FK trigger never gets a chance to fire.
    # Confirmed by running these two cases: psycopg.errors.CheckViolation, "violates check
    # constraint \"ck_outbox_kind_stream_identity\"" — not a ForeignKeyViolation. Pinning this
    # name (not fk_outbox_decision_triple) is what closes the false-green hole: disabling this
    # CHECK in the migration flips these two cases to failing (proven by the mutation test),
    # because the only thing left to reject the row is a ForeignKeyViolation whose message never
    # contains "ck_outbox_kind_stream_identity".
    "callback_zero_seq": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',0,'pending')"),
    "callback_negative_seq": ("ck_outbox_kind_stream_identity",
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',-1,'pending')"),
    "callback_null_sequence": ("ck_outbox_kind_stream_identity",  # callback, run set, sequence NULL (F2 hole)
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, status) "
        "VALUES ('decision_callback','c1','rA','decision','pending')"),
    "email_sequenced": ("ck_outbox_kind_stream_identity",  # email carrying a sequence
        "INSERT INTO outbox (kind, case_id, ordering_stream, decision_sequence, status) "
        "VALUES ('poc_email','c1','email',1,'pending')"),
    "email_with_run": ("ck_outbox_kind_stream_identity",  # email carrying a run_id
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','rA','email','pending')"),
    "callback_stream_swap": ("ck_outbox_kind_stream_identity",  # callback on the email stream
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','email',1,'pending')"),
    "email_stream_swap": ("ck_outbox_kind_stream_identity",  # email on the decision stream
        "INSERT INTO outbox (kind, case_id, ordering_stream, status) "
        "VALUES ('poc_email','c1','decision','pending')"),
    "callback_wrong_seq": ("fk_outbox_decision_triple",  # (rA,c1,2) — no matching decision
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',2,'pending')"),
    "callback_wrong_case": ("fk_outbox_decision_triple",  # (rA,c2,1) — decision is (rA,c1,1)
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c2','rA','decision',1,'pending')"),
    "callback_wrong_run": ("fk_outbox_decision_triple",  # (ghost,c1,1) — no such decision
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','ghost','decision',1,'pending')"),
    "dup_callback": ("uq_outbox_decision_callback_run",  # two callbacks for the same run
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',1,'pending'); "
        "INSERT INTO outbox (kind, case_id, run_id, ordering_stream, decision_sequence, status) "
        "VALUES ('decision_callback','c1','rA','decision',1,'pending')"),
}


@pytest.mark.parametrize("case", list(_OUTBOX_BINDING_BAD))
def test_013_outbox_binding_insert_negatives(pg, case):
    from sqlalchemy.exc import IntegrityError

    expected, sql = _OUTBOX_BINDING_BAD[case]
    url = _fresh_db(pg, f"kyc_mig_013_ob_{case}")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn, "c1")
        _mk_case(conn, "c2")  # for callback_wrong_case (a valid, different case)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)  # (rA,c1,1) exists
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        for stmt in sql.split("; "):
            conn.execute(text(stmt))
    assert expected in str(exc.value)
    engine.dispose()


def test_013_outbox_binding_update_negative(pg):
    """UPDATE variant: a valid callback cannot be UPDATEd onto the wrong (run,case,seq) —
    the triple FK fires on UPDATE too."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_ob_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, "
                          "ordering_stream, decision_sequence, status) "
                          "VALUES ('decision_callback','c1','rA','decision',1,'pending')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET decision_sequence=9 WHERE run_id='rA'"))  # (rA,c1,9) absent
    assert "fk_outbox_decision_triple" in str(exc.value)
    engine.dispose()


def test_013_outbox_callback_null_sequence_update_negative(pg):
    """UPDATE variant (F2): a valid callback cannot be UPDATEd to a NULL decision_sequence —
    ck_outbox_kind_stream_identity requires the sequence IS NOT NULL for a callback row."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_ob_upd_null")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, "
                          "ordering_stream, decision_sequence, status) "
                          "VALUES ('decision_callback','c1','rA','decision',1,'pending')"))
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET decision_sequence=NULL WHERE run_id='rA'"))
    assert "ck_outbox_kind_stream_identity" in str(exc.value)
    engine.dispose()


def _seed_two_cases_two_runs(conn):
    """Re-audit F2 seed: two REAL cases with valid runs/decisions, plus a third run (rX,
    under c1) that carries NO decision yet — so the composite-FK negatives below are
    otherwise valid (no uq_decisions_run_id / per-case-sequence collision can mask the FK)."""
    _mk_case(conn, "c1")
    _mk_case(conn, "c2")
    _mk_auto_decision(conn, d="d1", c="c1", r="r1", seq=1, ev_seq=1)   # r1 belongs to c1
    _mk_auto_decision(conn, d="d2", c="c2", r="r2", seq=2, ev_seq=1)   # r2 belongs to c2
    conn.execute(text("INSERT INTO events (id, case_id, idempotency_key, payload_hash, event_type, "
                      "actor_json, payload_json, event_sequence) VALUES ('rX-ev','c1','rX','h','x',"
                      "'{}'::jsonb,'{}'::jsonb,2)"))
    conn.execute(text("INSERT INTO runs (id, case_id, triggering_event_id, state) "
                      "VALUES ('rX','c1','rX-ev','PUBLISH_DECISION')"))  # rX belongs to c1


def test_013_decision_case_must_match_run_case_insert_negative(pg):
    """Re-audit F2: fk_decisions_run_case binds decisions(run_id, case_id) → runs(id, case_id).
    A decision citing run rX (which belongs to c1) under case c2 passes BOTH single-column FKs
    and every uniqueness constraint — only the composite FK rejects it, asserted BY NAME."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_runcase_ins")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_two_cases_two_runs(conn)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO decisions (id, case_id, run_id, decision, score, gates_json, "
            "buy_enablement, policy_shas, manual, decision_sequence) VALUES "
            "('dx','c2','rX','approve',10,'{}'::jsonb,'enabled','{}'::jsonb,false,3)"))
    assert "fk_decisions_run_case" in str(exc.value)
    engine.dispose()


def test_013_decision_case_must_match_run_case_update_negative(pg):
    """Re-audit F2 UPDATE variant: re-pointing a valid automatic decision at the OTHER real
    case (its run stays r1, which belongs to c1) must fail on fk_decisions_run_case — the
    seeds' distinct sequences guarantee no unique constraint can mask it."""
    from sqlalchemy.exc import IntegrityError

    url = _fresh_db(pg, "kyc_mig_013_runcase_upd")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _seed_two_cases_two_runs(conn)
    with pytest.raises(IntegrityError) as exc, engine.begin() as conn:
        conn.execute(text("UPDATE decisions SET case_id='c2' WHERE id='d1'"))
    assert "fk_decisions_run_case" in str(exc.value)
    engine.dispose()


def test_013_orm_and_live_fk_parity(pg):
    """Re-audit F2+F9: the named relational constraints exist BOTH in ORM metadata
    (Base.metadata is Alembic's comparison target — a live-only FK reports drift and hides
    dependency ordering) AND in the live migrated DB: fk_outbox_case_id,
    fk_outbox_decision_triple, fk_decisions_run_case, uq_runs_id_case_id."""
    from kyc_tool.db.tables import DecisionRow, Outbox, Run

    orm_outbox_fks = {fk.constraint.name for fk in Outbox.__table__.foreign_keys}
    assert {"fk_outbox_case_id", "fk_outbox_decision_triple"} <= orm_outbox_fks
    orm_decision_fks = {fk.constraint.name for fk in DecisionRow.__table__.foreign_keys}
    assert "fk_decisions_run_case" in orm_decision_fks
    assert any(c.name == "uq_runs_id_case_id" for c in Run.__table__.constraints)

    url = _fresh_db(pg, "kyc_mig_013_fk_parity")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    insp = inspect(engine)
    live_outbox = {fk["name"] for fk in insp.get_foreign_keys("outbox")}
    assert {"fk_outbox_case_id", "fk_outbox_decision_triple"} <= live_outbox
    live_decisions = {fk["name"] for fk in insp.get_foreign_keys("decisions")}
    assert "fk_decisions_run_case" in live_decisions
    live_runs_uniques = {u["name"] for u in insp.get_unique_constraints("runs")}
    assert "uq_runs_id_case_id" in live_runs_uniques
    engine.dispose()


def test_013_downgrade_refuses_with_superseded_row(pg):
    """Separate real refusal test: a seeded superseded row makes the real alembic downgrade
    refuse byte-stably (RuntimeError with the stable message). Re-audit F8: superseded is
    decision-only, so the seed is a VALID automatic decision+callback chain whose callback
    is superseded — a superseded poc_email is impossible by CHECK."""
    url = _fresh_db(pg, "kyc_mig_013_down_refuse")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, ordering_stream, "
                          "decision_sequence, status, resolved_at) VALUES "
                          "('decision_callback','c1','rA','decision',1,'superseded', now())"))
    engine.dispose()
    with pytest.raises(RuntimeError) as exc:  # migration raises RuntimeError; alembic propagates it
        alembic_command.downgrade(cfg, "012")
    assert "superseded outbox row" in str(exc.value)


def test_013_downgrade_lock_prevents_concurrent_supersede(pg):
    """Drive the REAL migration downgrade() in a thread; pause it with a global
    after_cursor_execute barrier fired at its superseded preflight — which runs AFTER the
    production LOCK TABLE ... ACCESS EXCLUSIVE. A concurrent pending→superseded UPDATE must
    then FAIL with a lock timeout (SQLSTATE 55P03): it cannot slip between the preflight and
    the DDL. The seed is a VALID pending decision callback (re-audit F8 — the concurrent
    supersession must be lifecycle-legal, or the mutation witness would be masked by the
    kind conjunct instead of proving the race). MUTATION: moving/removing the LOCK (so the
    preflight holds only ACCESS SHARE) lets that UPDATE COMMIT — the except-OperationalError
    branch is never entered and the explicit pytest.fail fires."""
    import threading

    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import OperationalError

    url = _fresh_db(pg, "kyc_mig_013_down_lock")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        _mk_case(conn)
        _mk_auto_decision(conn, d="dA", c="c1", r="rA", seq=1, ev_seq=1)
        conn.execute(text("INSERT INTO outbox (kind, case_id, run_id, ordering_stream, "
                          "decision_sequence, status, next_attempt_at) VALUES "
                          "('decision_callback','c1','rA','decision',1,'pending', now())"))

    at_preflight = threading.Event()
    release = threading.Event()
    _PREFLIGHT = "from outbox where status='superseded'"  # the downgrade's count(*) preflight

    def _barrier(conn, cursor, statement, params, context, executemany):
        if _PREFLIGHT in statement.lower():
            at_preflight.set()          # downgrade holds ACCESS EXCLUSIVE here
            release.wait(timeout=15)    # hold the downgrade txn BETWEEN preflight and DDL

    down_err: list[Exception] = []

    def run_downgrade():
        try:
            alembic_command.downgrade(cfg, "012")
        except Exception as e:  # noqa: BLE001
            down_err.append(e)

    t = threading.Thread(target=run_downgrade)
    engineB = create_engine(url)
    # `event.listen` is process-wide, so register it INSIDE the try whose finally removes
    # it: a raise from Thread.start() would otherwise leak the barrier into every later
    # test in the session.
    event.listen(Engine, "after_cursor_execute", _barrier)
    try:
        t.start()
        assert at_preflight.wait(timeout=15)  # paused right after the preflight
        connB = engineB.connect()
        # Re-audit F5: begin B's transaction BEFORE any execute — SQLAlchemy 2 autobegin
        # would otherwise already own the transaction and connB.begin() would raise
        # InvalidRequestError before the test ever reached the lock. SET LOCAL is used
        # because the setting is now transaction-scoped by design.
        txB = connB.begin()  # manage B's txn EXPLICITLY so an unexpected success is COMMITTED
        connB.execute(text("SET LOCAL lock_timeout='2s'"))
        try:
            connB.execute(text("UPDATE outbox SET status='superseded', resolved_at=now() "
                               "WHERE case_id='c1'"))
        except OperationalError as exc:
            assert exc.orig.sqlstate == "55P03"  # correct code: blocked by ACCESS EXCLUSIVE
            txB.rollback()
        else:
            # MUTATION path (lock removed): the UPDATE slipped in — COMMIT it so the stranded
            # superseded state is really created, then fail the test.
            txB.commit()
            pytest.fail("concurrent pending→superseded committed between preflight and DDL — "
                        "the ACCESS EXCLUSIVE lock did not hold")
        finally:
            connB.close()
    finally:
        release.set()
        t.join(timeout=15)
        event.remove(Engine, "after_cursor_execute", _barrier)
        engineB.dispose()
    assert not t.is_alive()  # the downgrade thread terminated (a hung timeout must NOT pass)
    assert down_err == []    # ... and completed cleanly on the no-superseded DB
    engine.dispose()

def test_barrier_listener_never_leaks_when_thread_start_raises(pg, monkeypatch):
    """F9: `Thread.join()` on a never-started thread raises RuntimeError. If cleanup joined
    unconditionally, that raise would abort the `finally` BEFORE `event.remove()` and the
    process-wide barrier would contaminate every later test. Force the exact failure and prove
    registration is still undone."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    def _boom(self):
        raise RuntimeError("injected: Thread.start() failed after listener registration")

    monkeypatch.setattr(threading.Thread, "start", _boom)
    url = _fresh_db(pg, "kyc_mig_013_leak_guard")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "013")

    def _barrier(conn, cursor, statement, params, context, executemany):  # pragma: no cover
        pass

    registered = started = False
    t = threading.Thread(target=lambda: None)
    with pytest.raises(RuntimeError, match="injected"):
        try:
            event.listen(Engine, "after_cursor_execute", _barrier)
            registered = True
            t.start()
            started = True
        finally:
            try:
                if started:
                    t.join(timeout=5)
            finally:
                if registered:
                    event.remove(Engine, "after_cursor_execute", _barrier)

    # the whole point: the hook is gone despite the raise, and nothing was left running
    assert not event.contains(Engine, "after_cursor_execute", _barrier)
    assert not t.is_alive()


# The runbook's executable restore acceptance predicate (Task 9 Step 5, step 0.6c). POSITIVE and
# fail-closed: it asserts the restored row EXISTS and matches every recorded component, and must
# return EXACTLY ONE row. A negative "select the mismatches, expect zero rows" formulation is
# prohibited — an absent row, or one restored under the wrong run_id, matches nothing and is then
# indistinguishable from an exact match (re-review 0ca264b P2).
# EVERY schema-012 outbox column is recorded, restored and positively compared. The procedure runs
# BEFORE 013, so it must not name a 013-only column: `resolved_at` and the claim tuple do not exist
# yet, while `attempts`, `next_attempt_at`, `last_error` and `created_at` DO and are part of the row.
# `IS NOT DISTINCT FROM` throughout so a NULL matches a NULL rather than yielding UNKNOWN.
_RESTORE_ACCEPTANCE_SQL = text(
    "SELECT 1 AS accepted FROM outbox o JOIN decisions d ON d.id = :decision_id "
    "WHERE o.id = :original_outbox_id AND o.kind = :original_kind "
    "AND o.case_id = :case_id AND o.run_id IS NOT DISTINCT FROM :run_id "
    "AND d.case_id = o.case_id AND d.run_id IS NOT DISTINCT FROM o.run_id "
    "AND encode(sha256(convert_to(o.payload_json::text,'UTF8')),'hex') = :body_digest "
    "AND o.status = :original_status "
    "AND o.delivered_at IS NOT DISTINCT FROM :original_delivered_at "
    "AND o.attempts = :original_attempts "
    "AND o.next_attempt_at IS NOT DISTINCT FROM :original_next_attempt_at "
    "AND o.last_error IS NOT DISTINCT FROM :original_last_error "
    "AND o.created_at IS NOT DISTINCT FROM :original_created_at"
)

# Read-only, monotonic sequence precondition (step 0.6d). The id the sequence would hand the NEXT
# writer must already be PAST the restored id. is_called is load-bearing: on a never-called sequence
# last_value is the id nextval will RETURN, not one already consumed. This never writes — a live
# `setval(GREATEST(max(id), last_value))` can rewind the sequence under a concurrent nextval (a
# non-transactional object; LOCK TABLE does not fence it) and is prohibited (re-review 0ca264b P1).
_SEQ_HIGH_WATER_SQL = text(
    "SELECT last_value + (CASE WHEN is_called THEN 1 ELSE 0 END) AS next_id FROM outbox_id_seq"
)


def test_012_restore_acceptance_rejects_default_id_then_accepts_original(pg):
    """Re-audit F1 (+ re-review 0ca264b P1/P2) — the restore acceptance contract, on schema 012
    with the REAL diagnostic CLI and the REAL 013 upgrade:
    (1) two callbacks (ids captured), full evidence tuples recorded (simulating the backup);
    (2) the OLDER callback is deleted (simulating a retention prune);
    (3) the read-only sequence precondition holds — a pruned historical id is BELOW the
        high-water mark, so no sequence write is needed (and none is performed);
    (4) an ABSENT row is REJECTED (the fail-open hole the negative predicate had);
    (5) a DEFAULT-id INSERT restore is REJECTED (its id differs — silent order reversal);
    (6) each evidence component, mutated alone, is REJECTED (every one is load-bearing);
    (7) restored with the EXACT original id AND original lifecycle fields → exactly one
        accepted row, the real diagnostic CLI runs clean, 013 upgrades, and old/new map to
        decision_sequence 1/2 (order authority preserved)."""
    import datetime as _dt
    import os
    import subprocess
    import sys

    url = _fresh_db(pg, "kyc_mig_012_restore_acceptance")
    cfg = _config(url)
    alembic_command.upgrade(cfg, "012")
    engine = create_engine(url)
    with engine.begin() as conn:
        # A non-empty, NESTED, non-ASCII body: with '{}' the restore round-trip proved nothing,
        # because any empty payload matches any other (re-review 6a408a3 F6). Non-ASCII also
        # exercises the digest's UTF-8 handling.
        payload_a = json.dumps({
            "decision": "approve", "buy_enablement": "enabled",
            "gates": {"score_met": True, "legal_proof": True, "no_hard_conflict": True},
            "checks": [{"type": "website_verified", "source": "reviewer:José Ω"}],
        }, ensure_ascii=False)
        oid_a = _seed_legacy_callback(conn, case_id="c1", run_id="rA", decision_id="dA",
                                      ev_seq=1, status="delivered", payload=payload_a)
        oid_b = _seed_legacy_callback(conn, case_id="c1", run_id="rB", decision_id="dB",
                                      ev_seq=2, status="pending")
        # A is a PREVIOUSLY RETRIED callback: without attempts/next_attempt_at/last_error/created_at
        # in the evidence tuple, a restore that silently reset its retry clock and error history
        # would still pass the predicate (re-review 0ca264b F4).
        conn.execute(text(
            "UPDATE outbox SET attempts=3, last_error='upstream 503', "
            "next_attempt_at=now() - interval '2 days' WHERE id=:i"), {"i": oid_a})
        assert oid_a < oid_b  # A is the authoritative OLDER callback
        # the evidence tuple the operator captures FROM BACKUP before restoring — identity,
        # body digest, AND the original lifecycle fields (substituting now() is prohibited)
        evidence = {
            r.run_id: {"decision_id": d, "run_id": r.run_id, "case_id": r.case_id,
                       "original_outbox_id": r.id, "body_digest": r.digest,
                       "original_status": r.status, "original_delivered_at": r.delivered_at,
                       "original_attempts": r.attempts,
                       "original_next_attempt_at": r.next_attempt_at,
                       "original_last_error": r.last_error,
                       "original_created_at": r.created_at, "original_kind": r.kind,
                       "_payload": r.payload_json}
            for r, d in zip(
                conn.execute(text(
                    "SELECT id, kind, run_id, case_id, status, delivered_at, attempts, "
                    "next_attempt_at, last_error, created_at, payload_json, "
                    "encode(sha256(convert_to(payload_json::text,'UTF8')),'hex') AS digest "
                    "FROM outbox ORDER BY id")).all(),
                ["dA", "dB"], strict=True,
            )
        }
    ev = evidence["rA"]
    assert ev["original_status"] == "delivered" and ev["original_delivered_at"] is not None
    with engine.begin() as conn:  # simulate the retention prune of the OLD delivered callback
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": oid_a})

    def _accepted(conn, e):
        return conn.execute(_RESTORE_ACCEPTANCE_SQL, e).fetchall()

    with engine.begin() as conn:
        # (3) READ-ONLY precondition: the next id the sequence would hand out is already past
        # the restored id, so the gap is safe to fill and NO sequence write is required.
        next_id = conn.execute(_SEQ_HIGH_WATER_SQL).scalar_one()
        assert next_id > ev["original_outbox_id"]
        # (4) fail-closed on an ABSENT row: the pre-rev-10 negative predicate returned zero
        # mismatches here and would have called this "accepted".
        assert _accepted(conn, ev) == []

    with engine.begin() as conn:  # (5) the PROHIBITED default-id restore
        bad_id = conn.execute(text(
            "INSERT INTO outbox (kind, case_id, run_id, payload_json, status, delivered_at) "
            "VALUES ('decision_callback','c1','rA','{}'::jsonb,'delivered', now()) RETURNING id"
        )).scalar_one()
        assert bad_id > oid_b  # a fresh id — the restored row would rank NEWER than rB
        assert _accepted(conn, ev) == []  # REJECTED: the id does not match the evidence
        conn.execute(text("DELETE FROM outbox WHERE id=:i"), {"i": bad_id})  # operator undoes it

    with engine.begin() as conn:  # (7a) the governed restore: EXACT id AND exact lifecycle
        conn.execute(text(
            "INSERT INTO outbox (id, kind, case_id, run_id, payload_json, status, delivered_at, "
            "attempts, next_attempt_at, last_error, created_at) "
            "VALUES (:i,:k,:c,:r,CAST(:p AS jsonb),:st,:da,:at,:na,:le,:ca)"
        ), {"i": ev["original_outbox_id"], "k": ev["original_kind"],
            "p": json.dumps(ev["_payload"]), "c": ev["case_id"], "r": ev["run_id"],
            "st": ev["original_status"], "da": ev["original_delivered_at"],
            "at": ev["original_attempts"], "na": ev["original_next_attempt_at"],
            "le": ev["original_last_error"], "ca": ev["original_created_at"]})
        assert _accepted(conn, ev) == [(1,)]  # ACCEPTED: exactly one row, every component matched
        # the body really round-tripped — not an empty placeholder that would match anything
        stored = conn.execute(text("SELECT payload_json FROM outbox WHERE id=:i"),
                              {"i": ev["original_outbox_id"]}).scalar_one()
        assert stored == ev["_payload"] and stored["checks"][0]["source"] == "reviewer:José Ω"

        # (6) every evidence component is separately load-bearing: mutate one at a time, and
        # the acceptance predicate must reject the (unchanged, correctly restored) row.
        mutations = {
            "decision_id": "d-nope",
            "run_id": "r-nope",
            "case_id": "c-nope",
            "original_outbox_id": ev["original_outbox_id"] + 1000,
            "body_digest": "0" * 64,
            "original_kind": "poc_email",
            "original_status": "pending",
            "original_delivered_at": ev["original_delivered_at"] + _dt.timedelta(seconds=1),
            "original_attempts": ev["original_attempts"] + 1,
            "original_next_attempt_at": (
                ev["original_next_attempt_at"] + _dt.timedelta(seconds=1)
                if ev["original_next_attempt_at"] is not None
                else _dt.datetime.now(_dt.UTC)
            ),
            "original_last_error": "not the recorded error",
            "original_created_at": ev["original_created_at"] + _dt.timedelta(seconds=1),
        }
        for key, bad in mutations.items():
            assert _accepted(conn, {**ev, key: bad}) == [], f"{key} is not load-bearing"

        # a TAMPERED stored body must be rejected BEFORE the exact backup body is accepted —
        # otherwise the digest column proves nothing about what was actually restored.
        conn.execute(text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:i"),
                     {"i": ev["original_outbox_id"],
                      "p": json.dumps({**ev["_payload"], "decision": "reject"})})
        assert _accepted(conn, ev) == []            # rejected: stored body no longer matches
        conn.execute(text("UPDATE outbox SET payload_json = CAST(:p AS jsonb) WHERE id=:i"),
                     {"i": ev["original_outbox_id"], "p": json.dumps(ev["_payload"])})
        assert _accepted(conn, ev) == [(1,)]        # exact backup body restored -> accepted again

    proc = subprocess.run(  # the REAL diagnostic must now be clean
        [sys.executable, "-m", "kyc_tool.ops.verify_pr7b_core_backfill"],
        env={**os.environ, "KYC_DATABASE_URL": url},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    alembic_command.upgrade(cfg, "013")  # the real migration ranks by outbox.id
    with engine.connect() as conn:
        seqs = {r.id: r.decision_sequence for r in conn.execute(
            text("SELECT id, decision_sequence FROM decisions WHERE case_id='c1'"))}
    assert seqs == {"dA": 1, "dB": 2}  # the restored OLD callback keeps sequence 1
    engine.dispose()
