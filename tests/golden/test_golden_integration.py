"""Golden cases that exercise the service machinery rather than pure logic:
G10 (manual approve), G11 (idempotent replay), G12 (upstream error → partial).
"""

import pytest
from sqlalchemy import text

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.checkstore import repo as checkstore
from kyc_tool.db.session import uow
from kyc_tool.db.tables import Case
from kyc_tool.domain.models import CheckStatus
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from kyc_tool.validators.base import CheckIntent

pytestmark = pytest.mark.postgres


def _seed_g3_state(session_factory, policy, case_id: str) -> None:
    with uow(session_factory) as session:
        if session.get(Case, case_id) is None:
            session.add(Case(id=case_id))
            session.flush()
        checkstore.apply_check_intents(
            session,
            case_id=case_id,
            intents=[
                CheckIntent("verified_email", CheckStatus.PASS, source="golden-fixture"),
                CheckIntent("website_verified", CheckStatus.PASS, source="golden-fixture"),
            ],
            rubric=policy.rubric,
            run_id=None,
        )


def test_g10_manual_approve_bypasses_gates_and_buy_locks(
    client, engine, session_factory, policy, post_event
):
    """G10: manual approve on a low-score case — approved_manual, gates
    bypassed, buy locked (no ORG-ID), audit row carries the reviewer."""
    case_id = "golden-G10"
    _seed_g3_state(session_factory, policy, case_id)

    response, _ = post_event(
        case_id,
        "reviewer.manual_approve",
        {"reviewer_id": "rev-42", "note": "verified offline"},
        actor={"type": "reviewer", "id": "rev-42"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["case_status"] == "approved_manual"
    assert body["buy_status"] == "buy_locked_org_id_required"

    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["status"] == "approved_manual"

    with engine.connect() as conn:
        decision = conn.execute(
            text("SELECT manual, reviewer_id, buy_enablement FROM decisions WHERE case_id=:c"),
            {"c": case_id},
        ).one()
        assert decision.manual is True
        assert decision.reviewer_id == "rev-42"
        assert decision.buy_enablement == "locked_org_id_required"
        audit_row = conn.execute(
            text(
                "SELECT actor FROM audit_log WHERE case_id=:c AND action='reviewer.manual_approve'"
            ),
            {"c": case_id},
        ).one()
        assert audit_row.actor == "rev-42"
        # AUDIT:C3 — no decision callback for manual approve
        outbox = conn.execute(
            text("SELECT count(*) FROM outbox WHERE case_id=:c"), {"c": case_id}
        ).scalar_one()
        assert outbox == 0


def test_g11_duplicate_event_replay(client, engine, post_event, worker, publisher):
    """G11: replaying an event with the same idempotency key returns the first
    result and does not double-run anything."""
    first, key = post_event(
        "golden-G11", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-ACME-1"}
    )
    assert first.status_code == 202
    worker.run_until_idle()
    publisher.process_pending()

    replay, _ = post_event(
        "golden-G11", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-ACME-1"}, key=key
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    worker.run_until_idle()

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM events")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 1


class _TimeoutAdapter:
    adapter_id = "rir_rdap"

    def input_hash(self, case_snapshot, event):
        return hash_inputs(self.adapter_id, event)

    def run(self, case_snapshot, event) -> AdapterOutput:
        raise TimeoutError("RDAP timed out")


def test_g12_upstream_timeout_partial_run(
    client, engine, session_factory, policy, settings, post_event, tmp_path,
    callback_capture, publisher,
):
    """G12: upstream RDAP timeout — run marked partial, prior checks intact,
    NO failing check created, decision computed from available live checks."""
    case_id = "golden-G12"
    _seed_g3_state(session_factory, policy, case_id)

    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(tmp_path / "evidence"),
        settings,
        adapters={"rir_rdap": _TimeoutAdapter()},
    )
    worker = Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
    )

    response, _ = post_event(
        case_id, "org_id.submitted", {"rir": "ripe", "org_handle": "ORG-XYZ-1"}
    )
    run_id = response.json()["run_id"]
    worker.run_until_idle()
    publisher.process_pending()

    run = client.get(f"/v1/runs/{run_id}").json()
    assert run["state"] == "COMPLETE"
    assert run["partial"] is True
    assert run["adapters"] == [
        {
            "adapter_id": "rir_rdap",
            "status": "upstream_error",
            "latency_ms": run["adapters"][0]["latency_ms"],
            "fetched_at": run["adapters"][0]["fetched_at"],
        }
    ]

    case = client.get(f"/v1/cases/{case_id}").json()
    live_types = {c["type"]: c["status"] for c in case["live_checks"]}
    assert live_types == {"verified_email": "pass", "website_verified": "pass"}
    assert "org_id_match" not in live_types  # upstream failure never creates a failing check
    assert callback_capture.requests[-1]["body"]["decision"] == "manual_review_insufficient"
