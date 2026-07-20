"""Ops console: page serves, JSON endpoints, composer drives the real
pipeline, requeue repairs dead letters, and the gate flag hides everything."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from kyc_tool.api.app import create_app
from tests.integration.shared import ACME_KYB_WITH_CONTACT

pytestmark = pytest.mark.postgres


def test_console_page_and_gate(settings, session_factory, policy, clean_db):
    on = TestClient(create_app(settings, session_factory=session_factory, policy=policy))
    page = on.get("/ui")
    assert page.status_code == 200
    assert "Ops Console" in page.text

    off_settings = settings.model_copy(update={"ui_enabled": False})
    off = TestClient(create_app(off_settings, session_factory=session_factory, policy=policy))
    assert off.get("/ui").status_code == 404
    assert off.get("/ui/api/overview").status_code == 404


def test_overview_shape(client):
    body = client.get("/ui/api/overview").json()
    assert body["policy"]["bundle_hash"]
    assert "runs_by_state" in body["metrics"]
    assert body["config"]["hmac_secret_set"] is True
    assert body["dead_jobs"] == []


def test_composer_drives_pipeline_and_case_full_projects(client, phase3_worker, publisher):
    sent = client.post(
        "/ui/api/send-event",
        json={"case_id": "ui-acme", "event_type": "kyb.run_requested",
              "payload": ACME_KYB_WITH_CONTACT},
    )
    assert sent.status_code == 202
    assert sent.json()["jobs_queued"] >= 1
    phase3_worker.run_until_idle()
    publisher.process_pending()

    cases = client.get("/ui/api/cases?q=ui-acme").json()["cases"]
    assert cases[0]["id"] == "ui-acme"

    full = client.get("/ui/api/cases/ui-acme/full").json()
    assert full["score"]["threshold"] == 100
    assert len(full["score"]["items"]) == 8  # rubric-ordered, includes missing
    assert full["salesforce"]["Broker_Status__c"] == "Clear"
    assert full["salesforce"]["Website_Review_Status__c"] == "Open"  # task created
    assert any(a["action"] == "run.decided" for a in full["audit"])
    assert full["field_sources"]["KYC_Status__c"].startswith("case status")


def test_composer_website_review_completed_binds_reviewer_actor(client, engine, phase3_worker):
    """PR 5b final-review fix (spec §8.8): in DEV settings, the composer builds
    a genuine {"type":"reviewer","id":<reviewer_id>} actor for
    website.review_completed (routes.py send_event _SENSITIVE branch) — NOT a
    system/ops-console actor — so it passes the SAME reviewer-actor floor
    production enforces (events/ingest.py reviewer_actor_reason), rather than
    bypassing it. Proven end-to-end: an open website task closes with the
    actor-derived reviewer id after draining the worker."""
    sent = client.post(
        "/ui/api/send-event",
        json={"case_id": "ui-website-review", "event_type": "kyb.run_requested",
              "payload": ACME_KYB_WITH_CONTACT},
    )
    assert sent.status_code == 202
    phase3_worker.run_until_idle()

    with engine.connect() as conn:
        task_id = conn.execute(
            text(
                "SELECT id FROM review_tasks WHERE case_id=:c AND task_type='website' "
                "AND status='open'"
            ),
            {"c": "ui-website-review"},
        ).scalar_one()

    resp = client.post(
        "/ui/api/send-event",
        json={
            "case_id": "ui-website-review",
            "event_type": "website.review_completed",
            "payload": {"task_id": task_id, "result": "pass", "reviewer_id": "rev-1"},
        },
    )
    assert resp.status_code == 202  # accepted — the dev composer's actor passes the floor

    phase3_worker.run_until_idle()

    with engine.connect() as conn:
        status, reviewer_id = conn.execute(
            text("SELECT status, reviewer_id FROM review_tasks WHERE id=:id"), {"id": task_id}
        ).one()
        check_source = conn.execute(
            text(
                "SELECT source FROM checks WHERE case_id=:c AND check_type='website_verified' "
                "AND superseded_by_check_id IS NULL"
            ),
            {"c": "ui-website-review"},
        ).scalar_one()
    assert status == "done"
    assert reviewer_id == "rev-1"
    assert check_source == "reviewer:rev-1"  # actor-derived, same binding production enforces


def test_composer_validates_payloads(client):
    bad = client.post(
        "/ui/api/send-event",
        json={"case_id": "ui-bad", "event_type": "org_id.submitted",
              "payload": {"rir": "not-a-rir", "org_handle": "X"}},
    )
    assert bad.status_code == 422


def test_event_templates_cover_all_types(client, policy):
    body = client.get("/ui/api/event-templates").json()
    assert set(body["event_types"]) == set(policy.events.event_types)
    assert set(body["templates"]) == set(body["event_types"])


def test_integrations_report_classifies_stubs(client):
    body = client.get("/ui/api/integrations").json()
    by_id = {a["adapter_id"]: a for a in body["adapters"]}
    assert len(by_id) == 9
    assert by_id["floqer_company_enrichment"]["status"] == "stub"
    assert by_id["rir_poc"]["status"] == "stub"
    assert by_id["document_ocr"]["status"] == "dev"
    assert by_id["website_manual_review"]["status"] == "manual"
    assert by_id["broker_policy"]["status"] == "live"
    assert body["email_sender"]["status"] == "stub"
    assert body["platform_callback"]["status"] in ("live", "needs-config")


def test_probe_reports_not_probeable_for_stub(client):
    body = client.post("/ui/api/integrations/floqer_company_enrichment/probe").json()
    assert body["probeable"] is False


def test_requeue_outbox_dead_row(client, engine, post_event, worker):
    post_event("ui-requeue", "recalculate.requested", {})
    worker.run_until_idle()
    with engine.begin() as conn:
        outbox_id = conn.execute(
            text("UPDATE outbox SET status='dead' WHERE case_id='ui-requeue' RETURNING id")
        ).scalar_one()
    assert client.post(f"/ui/api/requeue/outbox/{outbox_id}").status_code == 200
    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM outbox WHERE id=:id"), {"id": outbox_id}
        ).scalar_one()
    assert status == "pending"
    assert client.post(f"/ui/api/requeue/outbox/{outbox_id}").status_code == 409  # not dead now


def test_requeue_refuses_redacted_dead_poc_email(client, engine, post_event):
    """Audit round 2, F4: a dead poc_email's payload was redacted (token
    scrubbed) — requeueing it would just crash delivery. The endpoint refuses;
    recovery is a fresh poc.submitted."""
    post_event("ui-redacted", "recalculate.requested", {})  # creates the case row
    with engine.begin() as conn:
        outbox_id = conn.execute(
            text(
                "INSERT INTO outbox (kind, case_id, payload_json, status, attempts) "
                "VALUES ('poc_email', 'ui-redacted', CAST(:p AS jsonb), 'dead', 8) "
                "RETURNING id"
            ),
            {"p": '{"redacted": true}'},
        ).scalar_one()
    resp = client.post(f"/ui/api/requeue/outbox/{outbox_id}")
    assert resp.status_code == 409
    assert "redacted" in resp.json()["detail"]
    with engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM outbox WHERE id=:id"), {"id": outbox_id}
        ).scalar_one()
    assert status == "dead"  # untouched


def test_requeue_dead_job_resets_failed_run(client, engine, post_event):
    response, _ = post_event("ui-deadjob", "recalculate.requested", {})
    run_id = response.json()["run_id"]
    with engine.begin() as conn:
        job_id = conn.execute(
            text("UPDATE jobs SET status='dead', last_error='boom' WHERE case_id='ui-deadjob' RETURNING id")
        ).scalar_one()
        conn.execute(text("UPDATE runs SET state='FAILED', error='boom' WHERE id=:r"), {"r": run_id})
    body = client.post(f"/ui/api/requeue/job/{job_id}").json()
    assert body["run_reset"] == run_id
    with engine.connect() as conn:
        job_status, run_state = conn.execute(
            text(
                "SELECT (SELECT status FROM jobs WHERE id=:j), (SELECT state FROM runs WHERE id=:r)"
            ),
            {"j": job_id, "r": run_id},
        ).one()
    assert job_status == "queued"
    assert run_state == "QUEUED"


def _admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer t"}


@pytest.fixture()
def prod_ui_client(settings, session_factory, policy, clean_db) -> TestClient:
    """Production-hardened settings (shape of `hardened()` in
    tests/unit/test_production_config.py) with the ops console enabled and an
    admin token set, so the composer's production bar (PR 5b Task 4) can be
    exercised end-to-end against the real ephemeral Postgres."""
    prod_settings = settings.model_copy(
        update={
            "environment": "production",
            "auth_disabled": False,
            "platform_hmac_secret": "s" * 40,
            "platform_callback_url": "https://platform.example/kyc",
            "object_store": "s3",
            "s3_bucket": "kyc-evidence",
            "ocr_engine": "tesseract",
            "email_provider": "ses",
            "adapters_profile": "real",
            "read_auth_required": True,
            "ui_enabled": True,
            "ui_admin_token": "t",
            "hmac_inbound_key_id": "kyc-platform-1",
            "hmac_inbound_secret": "i" * 40,
            "hmac_outbound_key_id": "kyc-tool-1",
            "hmac_outbound_secret": "o" * 40,
            "hmac_v1_inbound_sunset_at": "2026-09-01T00:00:00Z",
            "hmac_v1_outbound_sunset_at": "2026-10-01T00:00:00Z",
            "hmac_v1_observation_window_days": 14,
        }
    )
    return TestClient(create_app(prod_settings, session_factory=session_factory, policy=policy))


SENSITIVE = ["website.review_completed", "reviewer.manual_approve"]


@pytest.mark.parametrize("event_type", SENSITIVE)
def test_composer_bars_sensitive_types_in_production(prod_ui_client, event_type):
    r = prod_ui_client.post(
        "/ui/api/send-event",
        json={"case_id": "c1", "event_type": event_type, "payload": {}},
        headers=_admin_headers(),
    )
    assert r.status_code == 403


def test_composer_allows_scoring_events_in_production(prod_ui_client):
    r = prod_ui_client.post(
        "/ui/api/send-event",
        json={
            "case_id": "c1",
            "event_type": "kyb.run_requested",
            "payload": {"company_legal_name": "Acme"},
        },
        headers=_admin_headers(),
    )
    assert r.status_code != 403
