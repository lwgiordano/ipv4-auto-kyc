"""Phase 2 acceptance: deterministic adapters against recorded fixtures.

Full runs must produce correct checks and decisions for: clean UK company,
blocked broker (short-circuit, adapters skipped), allowed broker, document
match, website pass via simulated reviewer, and the POC token round-trip.
Raw responses land in object storage; upstream errors never create checks.
"""

import json
import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.ocr import JsonScanOcrEngine
from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.outbox.emails import LoggingEmailSender
from kyc_tool.outbox.publisher import OutboxPublisher
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import FsStore

pytestmark = pytest.mark.postgres

RECORDED = Path(__file__).parent.parent / "fixtures" / "recorded"

ACME_KYB = {
    "company_legal_name": "Acme Networks Ltd",
    "address": "1 Main Street, London, EC1A 1AA",
    "registration_number": "12345678",
    "jurisdiction": "GB",
    "website": "https://acme.example",
}

POC_DIRECTORY = {
    "arin:JD123-ARIN": {
        "found": True,
        "associated_org_handles": ["ORG-ACME-1"],
        "rir_listed_email": "noc@acme.example",
    },
    "ripe:HIDDEN-RIPE": {
        "found": True,
        "associated_org_handles": ["ORG-HIDE-1"],
        "rir_listed_email": None,
    },
}


def _registry_transport(request: httpx.Request) -> httpx.Response:
    if "/search/companies" in request.url.path:
        return httpx.Response(
            200, json=json.loads((RECORDED / "companies_house_acme.json").read_text())
        )
    if "lei-records" in request.url.path:
        return httpx.Response(200, json={"data": []})
    return httpx.Response(404)


@pytest.fixture()
def evidence_store(settings):
    return FsStore(settings.object_store_root)


@pytest.fixture()
def phase2_pipeline(session_factory, policy, settings, evidence_store):
    transport = httpx.MockTransport(_registry_transport)
    adapters = {
        "email_verification": EmailVerificationAdapter(),
        "companies_house": CompaniesHouseAdapter(
            client=httpx.Client(transport=transport, base_url="https://ch.test")
        ),
        "gleif": GleifAdapter(
            client=httpx.Client(transport=transport, base_url="https://gleif.test")
        ),
        "document_ocr": DocumentOcrAdapter(evidence_store, JsonScanOcrEngine()),
        "rir_poc": RirPocAdapter(FixturePocDirectory(POC_DIRECTORY)),
        "website_manual_review": WebsiteManualReviewAdapter(),
    }
    return Pipeline(
        session_factory,
        policy,
        evidence_store,
        settings,
        adapters=adapters,
        broker_matcher=BrokerGate(),
    )


@pytest.fixture()
def phase2_worker(session_factory, phase2_pipeline):
    return Worker(
        session_factory,
        {"run_transition": phase2_pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=phase2_pipeline.on_dead_letter,
    )


@pytest.fixture()
def email_sender():
    return LoggingEmailSender()


@pytest.fixture()
def phase2_publisher(session_factory, settings, callback_capture, email_sender):
    return OutboxPublisher(
        session_factory,
        settings,
        http_client=httpx.Client(transport=httpx.MockTransport(callback_capture.handler)),
        email_sender=email_sender,
    )


def _live_checks(client, case_id: str) -> dict[str, str]:
    return {
        c["type"]: c["status"] for c in client.get(f"/v1/cases/{case_id}").json()["live_checks"]
    }


def test_clean_uk_company_full_run(client, engine, post_event, phase2_worker, phase2_publisher, settings):
    response, _ = post_event("case-uk-1", "kyb.run_requested", ACME_KYB)
    run_id = response.json()["run_id"]
    phase2_worker.run_until_idle()
    phase2_publisher.process_pending()

    checks = _live_checks(client, "case-uk-1")
    assert checks["official_registry_match"] == "pass"
    assert "website_verified" not in checks  # manual — task instead

    tasks = client.get("/v1/review-tasks?status=open").json()["tasks"]
    website_tasks = [t for t in tasks if t["task_type"] == "website"]
    assert len(website_tasks) == 1
    assert website_tasks[0]["context"]["domain"] == "acme.example"

    run = client.get(f"/v1/runs/{run_id}").json()
    ch = next(a for a in run["adapters"] if a["adapter_id"] == "companies_house")
    assert ch["status"] == "ok"
    with engine.connect() as conn:
        raw_ref = conn.execute(
            text("SELECT raw_ref FROM adapter_results WHERE run_id=:r AND adapter_id='companies_house'"),
            {"r": run_id},
        ).scalar_one()
    assert raw_ref and raw_ref.startswith("fs://")
    raw = FsStore(settings.object_store_root).get(raw_ref)
    assert b"ACME NETWORKS LTD" in raw  # untouched upstream bytes archived

    # registry alone is far below threshold
    assert client.get("/v1/cases/case-uk-1").json()["latest_decision"] == "manual_review_insufficient"


def test_email_verified_event_creates_both_checks(client, post_event, phase2_worker, phase2_publisher):
    post_event("case-uk-2", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    post_event(
        "case-uk-2",
        "email.verified",
        {"email": "ops@acme.example", "domain": "acme.example", "verified_at": "2026-07-04T10:00:00Z"},
    )
    phase2_worker.run_until_idle()
    checks = _live_checks(client, "case-uk-2")
    assert checks["verified_email"] == "pass"
    assert checks["verified_company_email"] == "pass"  # AUDIT:D5 — both from one event


def test_blocked_broker_short_circuits(
    client, engine, post_event, phase2_worker, phase2_publisher, callback_capture
):
    response, _ = post_event(
        "case-larus", "kyb.run_requested", {**ACME_KYB, "company_legal_name": "Larus"}
    )
    run_id = response.json()["run_id"]
    phase2_worker.run_until_idle()
    phase2_publisher.process_pending()

    case = client.get("/v1/cases/case-larus").json()
    assert case["latest_decision"] == "reject"
    assert case["status"] == "rejected"
    assert case["broker_status"] == "blocked"
    with engine.connect() as conn:
        adapter_rows = conn.execute(
            text("SELECT count(*) FROM adapter_results WHERE run_id=:r"), {"r": run_id}
        ).scalar_one()
        checks = conn.execute(
            text("SELECT count(*) FROM checks WHERE case_id='case-larus'")
        ).scalar_one()
    assert adapter_rows == 0  # short-circuit: no enrichment spend
    assert checks == 0
    assert callback_capture.requests[-1]["body"]["decision"] == "reject"
    assert callback_capture.requests[-1]["body"]["gates"]["broker_ok"] is False


def test_broker_near_miss_does_not_match(client, post_event, phase2_worker):
    post_event(
        "case-nearmiss", "kyb.run_requested", {**ACME_KYB, "company_legal_name": "Larus Networks Group"}
    )
    phase2_worker.run_until_idle()
    case = client.get("/v1/cases/case-nearmiss").json()
    assert case["broker_status"] == "clear"  # fuzzy matching is OUT of scope
    assert case["latest_decision"] != "reject"


def test_allowed_broker_tagged_and_continues(client, engine, post_event, phase2_worker):
    response, _ = post_event(
        "case-ipxo", "kyb.run_requested", {**ACME_KYB, "company_legal_name": "IPXO"}
    )
    run_id = response.json()["run_id"]
    phase2_worker.run_until_idle()
    case = client.get("/v1/cases/case-ipxo").json()
    assert case["broker_status"] == "allowed_broker"
    with engine.connect() as conn:
        adapter_rows = conn.execute(
            text("SELECT count(*) FROM adapter_results WHERE run_id=:r"), {"r": run_id}
        ).scalar_one()
    assert adapter_rows > 0  # allowed brokers continue through scoring


def test_broker_gate_on_org_id_event_d1(client, engine, post_event, phase2_worker):
    """AUDIT:D1 — a blocked broker's ORG-ID submitted later must reject."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE broker_entities SET org_ids = ARRAY['ORG-LARUS-1'] WHERE name='Larus'")
        )
    post_event("case-d1", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    assert client.get("/v1/cases/case-d1").json()["broker_status"] == "clear"

    post_event("case-d1", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-LARUS-1"})
    phase2_worker.run_until_idle()
    case = client.get("/v1/cases/case-d1").json()
    assert case["broker_status"] == "blocked"
    assert case["latest_decision"] == "reject"


def test_document_match_awards_legal_proof(client, post_event, phase2_worker, evidence_store):
    doc_ref = evidence_store.put(
        "uploads/acme-cert.json",
        json.dumps(
            {"fields": {"name": "ACME NETWORKS LTD", "number": "12345678", "jurisdiction": "GB"}}
        ).encode(),
    )
    post_event("case-doc", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    post_event(
        "case-doc",
        "document.uploaded",
        {"object_ref": doc_ref, "doc_type": "registration_certificate"},
    )
    phase2_worker.run_until_idle()
    checks = _live_checks(client, "case-doc")
    assert checks["business_document_verified"] == "pass"


def test_website_review_completion_writes_check_and_rescore(client, post_event, phase2_worker):
    post_event("case-web", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    tasks = client.get("/v1/review-tasks?status=open").json()["tasks"]
    task = next(t for t in tasks if t["case_id"] == "case-web" and t["task_type"] == "website")

    complete = client.post(
        f"/v1/review-tasks/{task['id']}/complete",
        json={"result": "pass", "reviewer_id": "rev-7"},
    )
    assert complete.status_code == 202
    phase2_worker.run_until_idle()

    checks_response = client.get("/v1/cases/case-web").json()
    checks = {c["type"]: c for c in checks_response["live_checks"]}
    assert checks["website_verified"]["status"] == "pass"
    assert checks["website_verified"]["source"] == "reviewer:rev-7"

    tasks_after = client.get("/v1/review-tasks?status=open").json()["tasks"]
    assert not [t for t in tasks_after if t["case_id"] == "case-web"]

    # AUDIT:D4 — the endpoint is idempotent via the synthesized event
    replay = client.post(
        f"/v1/review-tasks/{task['id']}/complete",
        json={"result": "pass", "reviewer_id": "rev-7"},
    )
    assert replay.status_code == 200


def test_website_task_dedupe_on_rerun(client, post_event, phase2_worker):
    """AUDIT:D2 — repeat KYB runs must not open duplicate website tasks."""
    post_event("case-dedupe", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    post_event("case-dedupe", "kyb.run_requested", ACME_KYB)  # new key, same case
    phase2_worker.run_until_idle()
    tasks = client.get("/v1/review-tasks?status=open").json()["tasks"]
    assert len([t for t in tasks if t["case_id"] == "case-dedupe"]) == 1


def test_poc_token_round_trip(client, engine, post_event, phase2_worker, phase2_publisher, email_sender):
    post_event("case-poc", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    post_event(
        "case-poc",
        "poc.submitted",
        {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"},
    )
    phase2_worker.run_until_idle()
    phase2_publisher.process_pending()

    assert len(email_sender.sent) == 1
    email = email_sender.sent[0]
    assert email["to"] == "noc@acme.example"  # the RIR-LISTED address, never user-submitted
    token = re.search(r"token: (\S+)", email["body"]).group(1)

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT token_hash, verified_at FROM poc_tokens WHERE case_id='case-poc'")
        ).one()
    assert row.verified_at is None
    assert token not in row.token_hash  # hash-at-rest, raw token never stored

    post_event(
        "case-poc",
        "poc.token_verified",
        {"token_id": "ignored", "verified_at": "2026-07-04T11:00:00Z", "token": token},
    )
    phase2_worker.run_until_idle()

    checks = _live_checks(client, "case-poc")
    assert checks["poc_verified"] == "pass"
    with engine.connect() as conn:
        verified_at = conn.execute(
            text("SELECT verified_at FROM poc_tokens WHERE case_id='case-poc'")
        ).scalar_one()
    assert verified_at is not None


def test_poc_resend_supersedes_old_token(
    client, engine, post_event, phase2_worker, phase2_publisher, email_sender
):
    post_event("case-poc2", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    for key_hint in ("first", "second"):
        post_event(
            "case-poc2",
            "poc.submitted",
            {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"},
            key=f"poc2-{key_hint}",
            force_new_body=True,
        )
        phase2_worker.run_until_idle()
        phase2_publisher.process_pending()

    assert len(email_sender.sent) == 2
    old_token = re.search(r"token: (\S+)", email_sender.sent[0]["body"]).group(1)
    new_token = re.search(r"token: (\S+)", email_sender.sent[1]["body"]).group(1)

    # the OLD token was expired by the resend
    post_event(
        "case-poc2",
        "poc.token_verified",
        {"token_id": "x", "verified_at": "2026-07-04T11:00:00Z", "token": old_token},
    )
    phase2_worker.run_until_idle()
    checks = _live_checks(client, "case-poc2")
    assert checks["poc_verified"] in ("fail",)

    post_event(
        "case-poc2",
        "poc.token_verified",
        {"token_id": "y", "verified_at": "2026-07-04T11:05:00Z", "token": new_token},
        force_new_body=True,
    )
    phase2_worker.run_until_idle()
    assert _live_checks(client, "case-poc2")["poc_verified"] == "pass"


def test_poc_hidden_email_creates_review_task_no_check(client, post_event, phase2_worker):
    """e2e G9: hidden RIR email → poc_email_unavailable task, no check, decision unchanged."""
    post_event("case-hidden", "kyb.run_requested", ACME_KYB)
    phase2_worker.run_until_idle()
    before = client.get("/v1/cases/case-hidden").json()["latest_decision"]

    post_event(
        "case-hidden",
        "poc.submitted",
        {"rir": "ripe", "poc_handle": "HIDDEN-RIPE", "org_handle": "ORG-HIDE-1"},
    )
    phase2_worker.run_until_idle()

    checks = _live_checks(client, "case-hidden")
    assert "poc_verified" not in checks
    tasks = client.get("/v1/review-tasks?status=open").json()["tasks"]
    poc_tasks = [
        t for t in tasks if t["case_id"] == "case-hidden" and t["task_type"] == "poc_email_unavailable"
    ]
    assert len(poc_tasks) == 1
    assert client.get("/v1/cases/case-hidden").json()["latest_decision"] == before
