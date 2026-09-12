"""One server predicate governs full-population counts and paginated rows."""

import pytest
from sqlalchemy import text

from kyc_tool.db.audit import audit
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case, DecisionRow, Event, Run

pytestmark = pytest.mark.usefixtures("clean_db")


def manual(session, name, *, status="approved_manual", buy="buy_locked_org_id_required"):
    session.add(Case(id=name, company_name=name, status=status, buy_status=buy))
    session.flush()
    session.add(
        DecisionRow(
            case_id=name,
            run_id=None,
            decision="approve",
            score=0,
            gates_json={"bypassed": True},
            buy_enablement=buy,
            policy_shas={},
            manual=True,
            reviewer_id="reviewer",
        )
    )
    session.flush()


def test_counts_and_search_apply_before_pagination(client, session_factory):
    with uow(session_factory) as s:
        s.add_all([Case(id=f"new-{i:03d}", company_name=f"New Company {i}") for i in range(63)])
        manual(s, "approved", buy="buy_enabled")
    first = client.get("/ui/api/cases?filter=new&limit=20").json()
    assert first["total"] == 63
    assert len(first["cases"]) == first["limit"] == 20
    assert first["offset"] == 0
    last = client.get("/ui/api/cases?filter=new&limit=20&offset=60").json()
    assert last["total"] == 63 and len(last["cases"]) == 3
    beyond = client.get("/ui/api/cases?filter=new&offset=80").json()
    assert beyond["total"] == 63 and beyond["cases"] == []
    found = client.get("/ui/api/cases?filter=new&q=new-062").json()
    assert found["total"] == 1 and found["cases"][0]["id"] == "new-062"
    empty = client.get("/ui/api/cases?filter=approved&q=new").json()
    assert empty["total"] == 0 and empty["cases"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=-1", "offset=-1", "filter=surprise"])
def test_filter_and_pagination_bounds_are_validated(client, query):
    assert client.get("/ui/api/cases?" + query).status_code == 422


def test_filter_authority_manual_hold_pending_and_broken_pointers(client, session_factory):
    with uow(session_factory) as s:
        manual(s, "manual")
        manual(s, "sticky-held")
        s.add(
            Event(
                id="sticky-event",
                case_id="sticky-held",
                event_type="recalculate.requested",
                idempotency_key="sticky",
                payload_hash="sticky",
                event_sequence=1,
            )
        )
        s.flush()
        s.add(Run(id="sticky-run", case_id="sticky-held", triggering_event_id="sticky-event"))
        s.flush()
        s.add(
            DecisionRow(
                case_id="sticky-held",
                run_id="sticky-run",
                decision="manual_review_insufficient",
                score=100,
                gates_json={},
                buy_enablement="buy_locked_org_id_required",
                policy_shas={},
                manual=False,
                decision_sequence=1,
            )
        )
        audit(s, "run.decided", case_id="sticky-held", run_id="sticky-run", enforcement_held=True)
        manual(s, "approved", status="account_approved", buy="buy_enabled")
        manual(s, "held", status="manual_review_insufficient")
        # Positive raw verdict plus manual-review state is NOT effective approval.
        manual(s, "rejected", status="rejected")
        s.execute(text("UPDATE decisions SET decision='reject' WHERE case_id='rejected'"))
        s.add(Case(id="pending", status="kyc_pending"))
        manual(s, "broken")
        manual(s, "legacy-order")
        s.add(Case(id="empty-drift"))
        s.add(Case(id="empty-manual-drift"))
        s.flush()
        # Historical corruption simulation is intentionally scoped to this disposable DB.
        s.execute(text("SET LOCAL session_replication_role='replica'"))
        s.execute(text("UPDATE cases SET latest_decision_row_id='absent' WHERE id='broken'"))
        s.execute(text("UPDATE cases SET latest_decision_row_id='absent' WHERE id='empty-drift'"))
        s.execute(
            text("UPDATE cases SET latest_manual_decision_row_id='absent' WHERE id='empty-manual-drift'")
        )
        s.execute(text("UPDATE cases SET latest_decision_row_id=NULL WHERE id='legacy-order'"))
        s.execute(text("SET LOCAL session_replication_role='origin'"))

    def ids(filter):
        response = client.get("/ui/api/cases", params={"filter": filter}).json()
        assert response["total"] == len(response["cases"])
        return {r["id"] for r in response["cases"]}

    assert ids("approved") == {"approved", "manual", "sticky-held"}
    assert ids("review") == {"held", "pending", "broken", "empty-drift", "empty-manual-drift", "legacy-order"}
    # Buying locked is a subset of effective approvals, not a second decision state.
    assert ids("buy_locked") == {"manual", "sticky-held"}
    assert ids("rejected") == {"rejected"}
    assert ids("new") == {"pending"}
    assert ids("all") == {
        "approved",
        "manual",
        "sticky-held",
        "held",
        "pending",
        "broken",
        "empty-drift",
        "rejected",
        "legacy-order",
        "empty-manual-drift",
    }


@pytest.mark.parametrize("status", ["account_approved", "rejected"])
@pytest.mark.parametrize("manual_pointer", ["missing", "wrong_kind", "wrong_case"])
def test_manual_authority_drift_overrides_each_terminal_filter(
    client, session_factory, status, manual_pointer
):
    from tests.integration.test_read_latest_decision import _insert_chain

    with uow(session_factory) as s:
        s.add(
            Case(id="subject", company_name="Subject", status=status, buy_status="buy_locked_org_id_required")
        )
        s.flush()
        _insert_chain(s, "subject", "subject-run", 1, "primary")
        if status == "rejected":
            s.execute(text("UPDATE decisions SET decision='reject' WHERE case_id='subject'"))
        manual(s, "other")
        other_manual = s.execute(text("SELECT id FROM decisions WHERE case_id='other'")).scalar_one()
        target = {"missing": "absent-manual", "wrong_kind": "subject-run-d", "wrong_case": other_manual}[
            manual_pointer
        ]
        s.execute(text("SET LOCAL session_replication_role='replica'"))
        s.execute(text("UPDATE cases SET latest_manual_decision_row_id=:d WHERE id='subject'"), {"d": target})
        s.execute(text("SET LOCAL session_replication_role='origin'"))
    for terminal in ("approved", "buy_locked", "rejected", "new"):
        result = client.get("/ui/api/cases", params={"filter": terminal, "q": "subject"}).json()
        assert result["total"] == 0, (terminal, result)
        assert result["cases"] == []
    review = client.get("/ui/api/cases?filter=review&q=subject").json()
    assert review["total"] == 1
    assert [case["id"] for case in review["cases"]] == ["subject"]
    assert client.get("/ui/api/cases?filter=all&q=subject").json()["total"] == 1
