"""Re-audit `8aba2df..2cee937` R3-F7: the shared, typed ops ShapeContract. `shape_mismatches`
detects relation absence, column absence and wrong nullability; and the prerequisite + diagnostic of
one operation import the SAME contract object (not two look-alike literals)."""

import pytest

from kyc_tool.ops import binding, shape

pytestmark = pytest.mark.postgres


def test_shape_mismatches_is_clean_on_the_governed_schema(session_factory, clean_db):
    with session_factory() as s:
        assert shape.shape_mismatches(s, shape.RESET_OUTBOX_CLAIMS) == []
        assert shape.shape_mismatches(s, shape.PR7B_CORE_PREWINDOW) == []


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
