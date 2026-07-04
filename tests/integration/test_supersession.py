"""Supersession semantics (07 §unit): chains of 3+, the live partial-unique
constraint, and same-transaction atomicity."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kyc_tool.checkstore import repo as checkstore
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case, Check
from kyc_tool.domain.models import CheckStatus
from kyc_tool.validators.base import CheckIntent

pytestmark = pytest.mark.postgres


@pytest.fixture()
def case_id(session_factory, clean_db):
    with uow(session_factory) as session:
        session.add(Case(id="case-supersession"))
    return "case-supersession"


def _apply(session_factory, policy, case_id, *intents):
    with uow(session_factory) as session:
        checkstore.apply_check_intents(
            session, case_id=case_id, intents=list(intents), rubric=policy.rubric, run_id=None
        )


def test_chain_of_three_keeps_one_live(session_factory, policy, case_id):
    for status in (CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.PASS):
        _apply(
            session_factory,
            policy,
            case_id,
            CheckIntent("official_registry_match", status, source="t"),
        )

    with uow(session_factory) as session:
        all_rows = checkstore.all_checks(session, case_id)
        live = checkstore.live_checks(session, case_id)
    assert len(all_rows) == 3
    assert len(live) == 1
    assert live[0].status == "pass"
    # the chain links: row1 -> row2 -> row3
    by_id = {c.id: c for c in all_rows}
    chain = [c for c in all_rows if c.superseded_by_check_id]
    assert len(chain) == 2
    for old in chain:
        assert by_id[old.superseded_by_check_id] is not None


def test_partial_unique_index_forbids_two_live_checks(engine, session_factory, policy, case_id):
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("verified_email", CheckStatus.PASS, source="t"),
    )
    with pytest.raises(IntegrityError), uow(session_factory) as session:
        # bypass the store on purpose — the DATABASE must reject a second live row
        session.add(
            Check(
                case_id=case_id,
                check_type="verified_email",
                status="pass",
                points_awarded=0,
                category="account_access",
                source="rogue-writer",
            )
        )
        session.flush()


def test_points_removed_when_superseded_by_fail(session_factory, policy, case_id):
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-A"}),
    )
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.FAIL, source="t", source_detail={"org_handle": "ORG-A"}),
    )
    with uow(session_factory) as session:
        live = checkstore.live_checks(session, case_id)
    assert [(c.check_type, c.status, c.points_awarded) for c in live] == [("org_id_match", "fail", 0)]


def test_org_id_change_cascades_to_associated_poc(session_factory, policy, case_id):
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-A"}),
        CheckIntent("poc_verified", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-A"}),
    )
    # same handle re-validation: POC stays
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-A"}),
    )
    with uow(session_factory) as session:
        live = {c.check_type: c.status for c in checkstore.live_checks(session, case_id)}
    assert live["poc_verified"] == "pass"

    # handle CHANGE: POC no longer associated -> cascaded supersession
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-B"}),
    )
    with uow(session_factory) as session:
        live = {
            c.check_type: (c.status, tuple(c.reason_codes)) for c in checkstore.live_checks(session, case_id)
        }
    assert live["poc_verified"][0] == "needs_review"
    assert "poc_not_associated" in live["poc_verified"][1]


def test_supersession_is_atomic_with_insert(engine, session_factory, policy, case_id):
    """A failed transaction leaves neither the successor nor the stamp."""
    _apply(session_factory, policy, case_id, CheckIntent("verified_email", CheckStatus.PASS, source="t"))
    with pytest.raises(RuntimeError), uow(session_factory) as session:
        checkstore.apply_check_intents(
            session,
            case_id=case_id,
            intents=[CheckIntent("verified_email", CheckStatus.FAIL, source="t")],
            rubric=policy.rubric,
            run_id=None,
        )
        raise RuntimeError("simulated crash before commit")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT status, superseded_by_check_id FROM checks "
                "WHERE case_id=:c AND check_type='verified_email'"
            ),
            {"c": case_id},
        ).fetchall()
    assert len(rows) == 1
    assert rows[0].status == "pass"
    assert rows[0].superseded_by_check_id is None
