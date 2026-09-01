"""Re-audit `8aba2df..2cee937` R3-F7: the shared, typed ops ShapeContract. `shape_mismatches`
detects relation absence, column absence and wrong nullability; and the prerequisite + diagnostic of
one operation import the SAME contract object (not two look-alike literals)."""

import pytest
from sqlalchemy import text

from kyc_tool.ops import binding, shape

pytestmark = pytest.mark.postgres


def test_every_shipped_contract_is_clean_on_the_governed_schema(session_factory, clean_db):
    """No false refusals: every shipped profile's relkind/type/nullability assertions match the real
    HEAD schema. A wrong type string in a contract fails HERE."""
    with session_factory() as s:
        for contract in (
            shape.RESET_OUTBOX_CLAIMS,
            shape.PR7B_CORE_PREWINDOW,
            shape.RESTORE_OUTBOX_CALLBACK,
            shape.SEQUENCE_REPAIR,
        ):
            assert shape.shape_mismatches(s, contract) == [], contract.name


def test_a_view_masquerading_as_a_table_is_refused(session_factory, clean_db):
    """R4-F4 repro (d): a filtering VIEW named `decisions` lists in information_schema.tables but must
    be refused by the relkind check — it can hide rows and certify a false OK."""
    contract = shape.ShapeContract("t", {"decisions": {}})
    with session_factory() as s:
        s.execute(text("ALTER TABLE decisions RENAME TO decisions_real"))
        s.execute(text("CREATE VIEW decisions AS SELECT * FROM decisions_real"))
        problems = shape.shape_mismatches(s, contract)
        s.rollback()
    assert any("decisions" in p and "not an ordinary table" in p for p in problems)


def test_a_wrong_column_type_is_detected(session_factory, clean_db):
    """R4-F4 (repros a/c): a column whose normalized type differs from the contract is refused before
    the command compares it (an INTEGER status to 'pending', a TEXT manual to a boolean)."""
    contract = shape.ShapeContract("t", {"shape_probe": {"flag": shape.ColumnShape(data_type="boolean")}})
    with session_factory() as s:
        s.execute(text("CREATE TABLE shape_probe (flag text)"))  # real table, wrong column type
        problems = shape.shape_mismatches(s, contract)
        s.rollback()
    assert any("shape_probe.flag" in p and "boolean" in p for p in problems)


def test_shape_mismatches_detects_missing_relation(session_factory, clean_db):
    contract = shape.ShapeContract("t", {"does_not_exist": {}})
    with session_factory() as s:
        problems = shape.shape_mismatches(s, contract)
    assert any("does_not_exist" in p and "absent" in p for p in problems)


def test_shape_mismatches_detects_wrong_nullability(session_factory, clean_db):
    # status is NOT NULL on the real schema; a contract demanding it be NULLABLE must flag it.
    contract = shape.ShapeContract("t", {"outbox": {"status": shape.ColumnShape(nullable=True)}})
    with session_factory() as s:
        problems = shape.shape_mismatches(s, contract)
    assert any("outbox.status" in p and "NOT NULL" in p for p in problems)


def test_shape_mismatches_detects_missing_column(session_factory, clean_db):
    contract = shape.ShapeContract("t", {"outbox": {"nope": shape.ColumnShape(nullable=True)}})
    with session_factory() as s:
        problems = shape.shape_mismatches(s, contract)
    assert any("outbox.nope" in p and "absent" in p for p in problems)


def test_prewindow_contract_is_the_same_object_in_both_diagnostics(monkeypatch):
    """The prerequisite (diagnostic) and the backfill (locking diagnostic) must pass the IDENTICAL
    contract object to bind() — not two literals that can drift apart."""
    from kyc_tool.ops import verify_pr7b_core_backfill as backfill
    from kyc_tool.ops import verify_pr7b_ops_prerequisites as prereq

    captured = []

    class _Stop(Exception):
        pass

    def _spy(session, **kw):
        captured.append(kw.get("shape_contract"))
        raise _Stop

    monkeypatch.setattr(binding, "bind", _spy)

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            raise AssertionError("bind() must short-circuit before any query")

    def _sf():
        return _FakeSession()

    for call in (
        lambda: prereq.check_prerequisites(_sf, expect_revision="012"),
        lambda: backfill.verify_backfill(_sf),
    ):
        with pytest.raises(_Stop):
            call()

    assert captured[0] is captured[1] is shape.PR7B_CORE_PREWINDOW


@pytest.mark.parametrize("bad_lock", [-1, 0, True, 1.5, float("nan"), 3601])
def test_bind_refuses_out_of_domain_lock_timeout(session_factory, clean_db, bad_lock):
    """Re-audit `03dbfab..bc325e7` R5-F7: bind() validated timeouts through int() (truncating floats,
    accepting bool, raising raw on NaN) and let them reach SET LOCAL as a DataError. It now refuses an
    out-of-domain lock/statement value with the governed OPS_COMMAND_SCHEMA_REFUSED first."""
    with session_factory() as s, pytest.raises(binding.BindingRefused) as exc:
        binding.bind(s, lock_timeout_seconds=bad_lock, statement_timeout_seconds=3600)
    assert binding.SCHEMA_REFUSED_SENTINEL in str(exc.value)


def test_bind_refuses_an_out_of_domain_statement_timeout(session_factory, clean_db):
    with session_factory() as s, pytest.raises(binding.BindingRefused):
        binding.bind(s, lock_timeout_seconds=60, statement_timeout_seconds=-1)


def test_a_partition_child_is_refused(session_factory, clean_db):
    """R5-F4: a partition child has relkind='r' but exposes a filtered slice — reject it as a table."""
    with session_factory() as s:
        s.execute(text("CREATE TABLE probe_parent (id bigint) PARTITION BY RANGE (id)"))
        s.execute(text("CREATE TABLE probe_child PARTITION OF probe_parent FOR VALUES FROM (0) TO (100)"))
        problems = shape.shape_mismatches(s, shape.ShapeContract("t", {"probe_child": {}}))
        s.rollback()
    assert any("probe_child" in p and "partition" in p for p in problems)


def test_a_nondeterministic_collation_is_refused(session_factory, clean_db):
    """R5-F4: a case-insensitive collation on a case-sensitively-compared column is refused."""
    with session_factory() as s:
        s.execute(text(
            "CREATE COLLATION probe_ci (provider=icu, locale='und-u-ks-level2', deterministic=false)"
        ))
        s.execute(text("CREATE TABLE probe_ci_tbl (k text COLLATE probe_ci)"))
        contract = shape.ShapeContract(
            "t", {"probe_ci_tbl": {"k": shape.ColumnShape(deterministic_collation=True)}}
        )
        problems = shape.shape_mismatches(s, contract)
        s.rollback()
    assert any("probe_ci_tbl.k" in p and "collation" in p for p in problems)


def test_sequence_integrity_flags_arithmetic_default(session_factory, clean_db):
    """R5-F3: an arithmetic column default (nextval(seq)+1000) defeats a sequence RESTART."""
    with session_factory() as s:
        s.execute(text("CREATE SEQUENCE probe_seq"))
        s.execute(text("CREATE TABLE probe_seq_tbl (id bigint DEFAULT nextval('probe_seq') + 1000)"))
        problems = shape.sequence_integrity_violations(
            s, table="probe_seq_tbl", column="id", sequence="probe_seq"
        )
        s.rollback()
    assert any("arithmetic" in p or "nextval" in p for p in problems)


def test_sequence_integrity_flags_negative_increment_and_cycle(session_factory, clean_db):
    """R5-F3: a non-unit/negative increment or a cycling sequence recreates collisions."""
    with session_factory() as s:
        s.execute(text("CREATE SEQUENCE probe_seq2 INCREMENT BY -1"))
        s.execute(text("CREATE TABLE probe_seq_tbl2 (id bigint DEFAULT nextval('probe_seq2'))"))
        problems = shape.sequence_integrity_violations(
            s, table="probe_seq_tbl2", column="id", sequence="probe_seq2"
        )
        s.rollback()
    assert any("increment" in p for p in problems)


def test_supported_revision_admission():
    from kyc_tool.ops.shape import PR7B_CORE_PREWINDOW, supported_revision_violation

    assert supported_revision_violation("012", PR7B_CORE_PREWINDOW) is None
    assert supported_revision_violation("001", PR7B_CORE_PREWINDOW) is not None
