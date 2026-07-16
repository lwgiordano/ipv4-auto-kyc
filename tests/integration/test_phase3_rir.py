"""Phase 3 acceptance: ORG-ID pass/needs_review per rules, Floqer never awards
points alone, and the buy-lock upgrade path (approve_buy_locked → org_id pass
→ approve with buying enabled)."""

import json

import pytest
from sqlalchemy import text

from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.integration.shared import ACME_KYB, ACME_KYB_WITH_CONTACT, FLOQER_RECORDS

pytestmark = pytest.mark.postgres


def _live(client, case_id):
    return {
        c["type"]: c for c in client.get(f"/v1/cases/{case_id}").json()["live_checks"]
    }


def test_org_id_pass_fixture(client, post_event, phase3_worker):
    post_event("case-org-pass", "kyb.run_requested", ACME_KYB)
    phase3_worker.run_until_idle()
    post_event("case-org-pass", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-ACME-1"})
    phase3_worker.run_until_idle()
    checks = _live(client, "case-org-pass")
    assert checks["org_id_match"]["status"] == "pass"


def test_org_id_needs_review_routing_no_points(client, post_event, phase3_worker):
    post_event("case-org-review", "kyb.run_requested", ACME_KYB)
    phase3_worker.run_until_idle()
    post_event(
        "case-org-review", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-AMBIG-1"}
    )
    phase3_worker.run_until_idle()
    checks = _live(client, "case-org-review")
    org = checks["org_id_match"]
    assert org["status"] == "needs_review"
    assert org["points"] == 0
    assert "org_id_parent_subsidiary_ambiguity" in org["reason_codes"]


def test_org_id_not_found_fails(client, post_event, phase3_worker):
    post_event("case-org-404", "kyb.run_requested", ACME_KYB)
    phase3_worker.run_until_idle()
    post_event("case-org-404", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-NOPE-9"})
    phase3_worker.run_until_idle()
    assert _live(client, "case-org-404")["org_id_match"]["status"] == "fail"


def test_floqer_never_awards_points_alone(client, engine, post_event, session_factory, policy, settings):
    """Floqer runs alone (no registries, no email): the ONLY check it may feed
    is the LinkedIn match; nothing else appears."""
    adapters = {
        "floqer_company_enrichment": FloqerAdapter(FixtureFloqerClient(FLOQER_RECORDS)),
    }
    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(settings.object_store_root),
        settings,
        adapters=adapters,
        broker_matcher=BrokerGate(),
    )
    worker = Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
    )
    post_event("case-floqer", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    worker.run_until_idle()

    checks = _live(client, "case-floqer")
    assert set(checks) == {"linkedin_company_match"}  # never email/org/poc/registry
    assert checks["linkedin_company_match"]["status"] == "pass"
    # discovery context is seeded IN-RUN only (for later adapters in the same
    # run) — it is NEVER written back to the case snapshot, so it can't leak into
    # a later run's frozen inputs (PR 2: cross-run isolation)
    with engine.connect() as conn:
        snapshot = conn.execute(
            text("SELECT submitted_json FROM cases WHERE id='case-floqer'")
        ).scalar_one()
    assert "floqer_context" not in snapshot


def test_linkedin_mismatch_awards_nothing(client, post_event, session_factory, policy, settings):
    adapters = {
        "floqer_company_enrichment": FloqerAdapter(FixtureFloqerClient(FLOQER_RECORDS)),
    }
    pipeline = Pipeline(
        session_factory,
        policy,
        FsStore(settings.object_store_root),
        settings,
        adapters=adapters,
        broker_matcher=BrokerGate(),
    )
    worker = Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline.on_dead_letter,
    )
    # submitted contact differs from discovered LinkedIn person
    post_event(
        "case-li-miss",
        "kyb.run_requested",
        {**ACME_KYB, "contact": {"name": "Bob Smith", "title": "CTO"}},
    )
    worker.run_until_idle()
    checks = _live(client, "case-li-miss")
    assert checks["linkedin_company_match"]["status"] == "fail"
    assert checks["linkedin_company_match"]["points"] == 0


def test_g6_hard_conflict_blocks_approval_e2e(client, post_event, phase3_worker, evidence_store):
    """G6 end-to-end: high score, all evidence, but the RIR entity conflicts —
    gate 5 fails, case stays manual_review_insufficient."""
    post_event("case-g6", "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    phase3_worker.run_until_idle()
    post_event(
        "case-g6",
        "email.verified",
        {"email": "ops@acme.example", "domain": "acme.example", "verified_at": "2026-07-04T10:00:00Z"},
    )
    phase3_worker.run_until_idle()
    post_event("case-g6", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-CONFLICT-1"})
    phase3_worker.run_until_idle()

    case = client.get("/v1/cases/case-g6").json()
    assert case["score"] >= 100
    assert case["gates"]["no_hard_conflict"] is False
    assert case["latest_decision"] == "manual_review_insufficient"


def test_buy_lock_upgrade_path_e2e(
    client, post_event, phase3_worker, publisher, callback_capture, evidence_store
):
    """Phase 3 acceptance: approve_buy_locked → org_id pass event → approve
    with buying enabled (G7a/G7b through the whole service)."""
    case_id = "case-upgrade"
    post_event(case_id, "kyb.run_requested", ACME_KYB_WITH_CONTACT)
    phase3_worker.run_until_idle()
    # registry (+25) + linkedin (+20) so far
    post_event(
        case_id,
        "email.verified",
        {"email": "ops@acme.example", "domain": "acme.example", "verified_at": "2026-07-04T10:00:00Z"},
    )
    phase3_worker.run_until_idle()
    # +25 company email +10 any inbox = 80: still insufficient
    assert client.get(f"/v1/cases/{case_id}").json()["latest_decision"] == "manual_review_insufficient"

    doc_ref = evidence_store.put(
        "uploads/upgrade-cert.json",
        json.dumps(
            {
                "fields": {
                    "name": "ACME NETWORKS LTD",
                    "address": "1 Main Street, London, EC1A 1AA",
                    "number": "12345678",
                    "jurisdiction": "GB",
                }
            }
        ).encode(),
    )
    post_event(case_id, "document.uploaded", {"object_ref": doc_ref, "doc_type": "registration_certificate"})
    phase3_worker.run_until_idle()
    publisher.process_pending()

    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["score"] >= 100
    assert case["latest_decision"] == "approve_buy_locked"  # all gates, no ORG-ID
    assert case["buy_status"] == "buy_locked_org_id_required"

    post_event(case_id, "org_id.submitted", {"rir": "arin", "org_handle": "ORG-ACME-1"})
    phase3_worker.run_until_idle()
    publisher.process_pending()

    case = client.get(f"/v1/cases/{case_id}").json()
    assert case["latest_decision"] == "approve"
    assert case["status"] == "account_approved"
    assert case["buy_status"] == "buy_enabled"

    upgrade_callbacks = [
        r["body"] for r in callback_capture.requests if r["body"]["case_id"] == case_id
    ]
    assert upgrade_callbacks[-2]["decision"] == "approve_buy_locked"
    assert upgrade_callbacks[-2]["buy_enablement"] == "locked_org_id_required"
    assert upgrade_callbacks[-1]["decision"] == "approve"
    assert upgrade_callbacks[-1]["buy_enablement"] == "enabled"
