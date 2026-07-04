"""Phase 3 acceptance: ORG-ID pass/needs_review per rules, Floqer never awards
points alone, and the buy-lock upgrade path (approve_buy_locked → org_id pass
→ approve with buying enabled)."""

import json

import httpx
import pytest
from sqlalchemy import text

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.ocr import JsonScanOcrEngine
from kyc_tool.adapters.rir_rdap.adapter import FixtureRirStrategy, RirRdapAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore
from tests.integration.test_phase2_adapters import ACME_KYB, _registry_transport

pytestmark = pytest.mark.postgres

RDAP_RECORDS = {
    "ORG-ACME-1": {
        "org_handle": "ORG-ACME-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
    },
    "ORG-AMBIG-1": {
        "org_handle": "ORG-AMBIG-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
        "parent_subsidiary_ambiguity": True,
    },
    "ORG-CONFLICT-1": {
        "org_handle": "ORG-CONFLICT-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
        "conflicting_entity": True,
    },
}

FLOQER_RECORDS = {
    "acme networks ltd": {
        "company_domain": "acme.example",
        "website": "https://acme.example",
        "linkedin": {
            "person_name": "Jane Doe",
            "company": "Acme Networks Ltd",
            "title": "Director",
            "company_domain": "acme.example",
        },
        "aliases": ["Acme Networks"],
        "registry_candidates": [{"registry": "companies_house", "number": "12345678"}],
    }
}

ACME_KYB_WITH_CONTACT = {**ACME_KYB, "contact": {"name": "Jane Doe", "title": "Director"}}


@pytest.fixture()
def phase3_pipeline(session_factory, policy, settings):
    store = FsStore(settings.object_store_root)
    transport = httpx.MockTransport(_registry_transport)
    adapters = {
        "email_verification": EmailVerificationAdapter(),
        "companies_house": CompaniesHouseAdapter(
            client=httpx.Client(transport=transport, base_url="https://ch.test")
        ),
        "gleif": GleifAdapter(
            client=httpx.Client(transport=transport, base_url="https://gleif.test")
        ),
        "floqer_company_enrichment": FloqerAdapter(FixtureFloqerClient(FLOQER_RECORDS)),
        "rir_rdap": RirRdapAdapter({"arin": FixtureRirStrategy(RDAP_RECORDS)}),
        "document_ocr": DocumentOcrAdapter(store, JsonScanOcrEngine()),
        "website_manual_review": WebsiteManualReviewAdapter(),
    }
    return Pipeline(
        session_factory, policy, store, settings, adapters=adapters, broker_matcher=BrokerGate()
    )


@pytest.fixture()
def phase3_worker(session_factory, phase3_pipeline):
    return Worker(
        session_factory,
        {"run_transition": phase3_pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=phase3_pipeline.on_dead_letter,
    )


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
    # and discovery context was persisted for downstream seeding
    with engine.connect() as conn:
        snapshot = conn.execute(
            text("SELECT submitted_json FROM cases WHERE id='case-floqer'")
        ).scalar_one()
    assert snapshot["floqer_context"]["company_domain"] == "acme.example"


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
            {"fields": {"name": "ACME NETWORKS LTD", "number": "12345678", "jurisdiction": "GB"}}
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
