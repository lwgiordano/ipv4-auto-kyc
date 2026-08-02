"""PR 7b-core: the drained sequence repair, through its REAL entry point, on a divergent lineage."""

import os
import subprocess
import sys
import time

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.ops import repair_outbox_sequence as repairer
from tests.integration.test_migrations import _config, _fresh_db

pytestmark = pytest.mark.postgres


def _seed_rows(engine, n):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cases (id) VALUES ('c1')"))
        for _ in range(n):
            conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                              "VALUES ('poc_email','c1','email','pending')"))
        return conn.execute(text("SELECT max(id) FROM outbox")).scalar_one()


def test_repair_restarts_divergent_sequence_and_next_allocation_is_exact(pg):
    """Divergent lineage: rows exist above the sequence's position (what a restore from another
    lineage leaves behind). The REAL CLI must move the sequence past every existing id, and the
    NEXT allocation must be exactly max(id)+1 — checked on this disposable DB by consuming one."""
    url = _fresh_db(pg, "kyc_seq_repair")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    max_id = _seed_rows(engine, 5)
    with engine.begin() as conn:      # rewind BELOW the data: the divergent-lineage state
        conn.execute(text("ALTER SEQUENCE outbox_id_seq RESTART WITH 1"))

    proc = subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.repair_outbox_sequence"],
        env={**os.environ, "KYC_DATABASE_URL": url}, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"next allocation will be {max_id + 1}" in proc.stdout

    with engine.begin() as conn:      # disposable DB: consuming one id here is the proof
        assert conn.execute(text("SELECT nextval('outbox_id_seq')")).scalar_one() == max_id + 1
    engine.dispose()


def test_repair_refuses_an_unknown_alembic_stamp_without_mutating(pg):
    """Re-audit `d569a15..4938840` F8: repair supplies no floor/exact revision, so bind()'s graph
    resolution was skipped and an unknown '999' stamp let it mutate. bind() now ALWAYS resolves the
    singleton stamp through the migration graph; an unknown revision is refused with the governed
    sentinel and the divergent sequence is left untouched."""
    url = _fresh_db(pg, "kyc_seq_unknown_stamp")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    _seed_rows(engine, 3)
    with engine.begin() as conn:
        conn.execute(text("ALTER SEQUENCE outbox_id_seq RESTART WITH 1"))    # divergent; would repair
        conn.execute(text("UPDATE alembic_version SET version_num='999'"))   # unknown stamp

    proc = subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.repair_outbox_sequence"],
        env={**os.environ, "KYC_DATABASE_URL": url}, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert "OPS_COMMAND_SCHEMA_REFUSED" in (proc.stdout + proc.stderr)
    assert "999" in (proc.stdout + proc.stderr)
    assert "Traceback" not in proc.stderr
    with engine.begin() as conn:      # NOT mutated — the sequence is still rewound to 1
        assert conn.execute(text("SELECT nextval('outbox_id_seq')")).scalar_one() == 1
    engine.dispose()


def test_repair_is_fail_closed_on_a_bad_readback(pg, monkeypatch):
    """The read-back is the whole safety property: if the sequence is not exactly where the
    repair intended, it must RAISE and roll back rather than report success. The earlier psql
    block printed a boolean and exited 0, so a bad repair looked like a good one."""
    url = _fresh_db(pg, "kyc_seq_repair_failclosed")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    _seed_rows(engine, 3)
    sf = make_session_factory(make_engine(url))
    real_text = repairer.text

    def _poison(sql):  # make the ALTER a no-op so the read-back cannot match
        return real_text("SELECT 1") if sql.startswith("ALTER SEQUENCE") else real_text(sql)

    monkeypatch.setattr(repairer, "text", _poison)
    with pytest.raises(RuntimeError, match="FAILED read-back"):
        repairer.repair_sequence(sf)
    engine.dispose()


def test_repair_takes_the_access_exclusive_fence(pg):
    """The drain fence is part of the procedure. A second connection holding ROW EXCLUSIVE must
    block the repair — proving the LOCK is real and not decorative."""
    url = _fresh_db(pg, "kyc_seq_repair_fence")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    _seed_rows(engine, 2)
    blocker = create_engine(url)
    conn = blocker.connect()
    proc = None
    try:
        tx = conn.begin()
        conn.execute(text("INSERT INTO outbox (kind, case_id, ordering_stream, status) "
                          "VALUES ('poc_email','c1','email','pending')"))  # holds ROW EXCLUSIVE
        proc = subprocess.Popen(
            [sys.executable, "-m", "kyc_tool.ops.repair_outbox_sequence"],
            env={**os.environ, "KYC_DATABASE_URL": url},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(3)
        assert proc.poll() is None      # BLOCKED on ACCESS EXCLUSIVE
        tx.rollback()
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, out + err
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=30)
        conn.close()
        blocker.dispose()
        engine.dispose()


def _run_repair(url):
    return subprocess.run(
        [sys.executable, "-m", "kyc_tool.ops.repair_outbox_sequence"],
        env={**os.environ, "KYC_DATABASE_URL": url}, capture_output=True, text=True, timeout=60,
    )


def test_repair_refuses_an_arithmetic_id_default(pg):
    """Re-audit `03dbfab..bc325e7` R5-F3: a `nextval(seq)+1000` default makes RESTART report a
    success it cannot deliver (next allocation is +1000). Refuse it, mutate nothing."""
    url = _fresh_db(pg, "kyc_seq_arith")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE outbox ALTER COLUMN id SET DEFAULT nextval('outbox_id_seq') + 1000"))
    proc = _run_repair(url)
    engine.dispose()
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "OPS_COMMAND_SCHEMA_REFUSED" in out and "arithmetic" in out
    assert "Traceback" not in proc.stderr


def test_repair_refuses_a_negative_increment_sequence(pg):
    """R5-F3: INCREMENT BY -1 allocates downward and collides — refuse before RESTART."""
    url = _fresh_db(pg, "kyc_seq_neg")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("ALTER SEQUENCE outbox_id_seq INCREMENT BY -1"))
    proc = _run_repair(url)
    engine.dispose()
    assert proc.returncode != 0
    assert "OPS_COMMAND_SCHEMA_REFUSED" in (proc.stdout + proc.stderr)


def test_repair_refuses_a_stamped_but_missing_outbox(pg):
    """Re-audit R5-F5: bind() resolved the outbox sequence before checking outbox existence, so a
    stamped-but-missing outbox tracebacked. It now refuses cleanly."""
    url = _fresh_db(pg, "kyc_seq_no_outbox")
    command.upgrade(_config(url), "013")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE outbox CASCADE"))
    proc = _run_repair(url)
    engine.dispose()
    assert proc.returncode != 0
    out = proc.stdout + proc.stderr
    assert "OPS_COMMAND_SCHEMA_REFUSED" in out and "outbox" in out
    assert "Traceback" not in proc.stderr
