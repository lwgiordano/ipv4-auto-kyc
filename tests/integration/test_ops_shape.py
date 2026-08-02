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
