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


# --- item 5: event-driven identity invalidation -------------------------------


def _invalidate(session_factory, case_id, event_type, payload):
    with uow(session_factory) as session:
        checkstore.supersede_stale_identity_proof(
            session, case_id=case_id, event_type=event_type, payload=payload, run_id=None
        )


def _live(session_factory, case_id):
    with uow(session_factory) as session:
        return {
            c.check_type: (c.status, tuple(c.reason_codes)) for c in checkstore.live_checks(session, case_id)
        }


def test_org_change_invalidates_proof_independent_of_adapter(session_factory, policy, case_id):
    # a verified ORG-ID and the POC associated to it
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-A"}),
        CheckIntent(
            "poc_verified",
            CheckStatus.PASS,
            source="t",
            source_detail={"poc_handle": "JD-1", "org_handle": "ORG-A"},
        ),
    )
    # a DIFFERENT ORG-ID is submitted; the revalidation adapter never runs (no new
    # org_id_match intent) — the stale proofs must lose PASS anyway (fail-closed)
    _invalidate(session_factory, case_id, "org_id.submitted", {"rir": "arin", "org_handle": "ORG-B"})
    live = _live(session_factory, case_id)
    assert live["org_id_match"][0] == "needs_review"
    assert "org_id_revalidation_pending" in live["org_id_match"][1]
    assert live["poc_verified"][0] == "needs_review"
    assert "poc_not_associated" in live["poc_verified"][1]


def test_same_org_resubmission_is_a_noop(session_factory, policy, case_id):
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent("org_id_match", CheckStatus.PASS, source="t", source_detail={"org_handle": "ORG-A"}),
        CheckIntent(
            "poc_verified",
            CheckStatus.PASS,
            source="t",
            source_detail={"poc_handle": "JD-1", "org_handle": "ORG-A"},
        ),
    )
    # canonical-equal handle ("org-a" vs "ORG-A") is NOT an identity change
    _invalidate(session_factory, case_id, "org_id.submitted", {"rir": "arin", "org_handle": "org-a"})
    live = _live(session_factory, case_id)
    assert live["org_id_match"][0] == "pass"
    assert live["poc_verified"][0] == "pass"


def test_resource_bound_poc_survives_org_change(session_factory, policy, case_id):
    # a POC that vouched for a RESOURCE (no org recorded) is not tied to the
    # ORG-ID, so an ORG-ID change must leave it live
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent(
            "poc_verified",
            CheckStatus.PASS,
            source="t",
            source_detail={"poc_handle": "JD-1", "resource": "192.0.2.0/24"},
        ),
    )
    _invalidate(session_factory, case_id, "org_id.submitted", {"rir": "arin", "org_handle": "ORG-B"})
    assert _live(session_factory, case_id)["poc_verified"][0] == "pass"


def test_poc_handle_change_supersedes_prior_poc(session_factory, policy, case_id):
    _apply(
        session_factory,
        policy,
        case_id,
        CheckIntent(
            "poc_verified",
            CheckStatus.PASS,
            source="t",
            source_detail={"poc_handle": "JD-1", "org_handle": "ORG-A"},
        ),
    )
    _invalidate(session_factory, case_id, "poc.submitted", {"rir": "arin", "poc_handle": "XX-9"})
    live = _live(session_factory, case_id)
    assert live["poc_verified"][0] == "needs_review"
    assert "poc_not_associated" in live["poc_verified"][1]
