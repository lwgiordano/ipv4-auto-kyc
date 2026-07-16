"""Item 5 — identity invalidation + POC token single-use, end-to-end.

Proves at the pipeline/DB level what the pure validators assert in isolation: a
verified POC token cannot be replayed, and an identity change supersedes
identity-bound proof even when the revalidation adapter never runs. rir_rdap is
deliberately ABSENT here (mirroring an RDAP outage), so any invalidation on
org_id.submitted must come from the event, not from a fresh org_id_match.
"""

import re

import httpx
import pytest

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from tests.integration.shared import ACME_KYB, POC_DIRECTORY, registry_transport

pytestmark = pytest.mark.postgres


@pytest.fixture()
def pipeline_no_rdap(session_factory, policy, settings, evidence_store):
    transport = httpx.MockTransport(registry_transport)
    adapters = {
        "email_verification": EmailVerificationAdapter(),
        "companies_house": CompaniesHouseAdapter(
            client=httpx.Client(transport=transport, base_url="https://ch.test")
        ),
        "gleif": GleifAdapter(client=httpx.Client(transport=transport, base_url="https://gleif.test")),
        "rir_poc": RirPocAdapter(FixturePocDirectory(POC_DIRECTORY)),
        "website_manual_review": WebsiteManualReviewAdapter(),
        # rir_rdap intentionally omitted — org_id.submitted yields no org_id_match
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
def worker_no_rdap(session_factory, pipeline_no_rdap):
    return Worker(
        session_factory,
        {"run_transition": pipeline_no_rdap.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=pipeline_no_rdap.on_dead_letter,
    )


def _live(client, case_id):
    return {c["type"]: c for c in client.get(f"/v1/cases/{case_id}").json()["live_checks"]}


def _verify_poc(client, post_event, worker, publisher, email_sender, case_id):
    """kyb + poc.submitted + poc.token_verified → poc_verified PASS.
    Returns the (raw_token, token_id) the platform captured from the email."""
    post_event(case_id, "kyb.run_requested", ACME_KYB)
    worker.run_until_idle()
    post_event(
        case_id,
        "poc.submitted",
        {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"},
    )
    worker.run_until_idle()
    publisher.process_pending()

    body = email_sender.sent[-1]["body"]
    token = re.search(r"token: (\S+)", body).group(1)
    token_id = re.search(r"reference: (\S+)", body).group(1)
    post_event(
        case_id,
        "poc.token_verified",
        {"token_id": token_id, "verified_at": "2026-07-04T11:00:00Z", "token": token},
    )
    worker.run_until_idle()
    assert _live(client, case_id)["poc_verified"]["status"] == "pass"
    return token, token_id


def test_verified_token_cannot_be_replayed(client, post_event, worker_no_rdap, publisher, email_sender):
    token, token_id = _verify_poc(client, post_event, worker_no_rdap, publisher, email_sender, "case-replay")
    # an attacker replays the captured token under a fresh idempotency key
    post_event(
        "case-replay",
        "poc.token_verified",
        {"token_id": token_id, "verified_at": "2026-07-04T12:00:00Z", "token": token},
    )
    worker_no_rdap.run_until_idle()

    poc = _live(client, "case-replay")["poc_verified"]
    assert poc["status"] == "fail"
    assert poc["points"] == 0
    assert "poc_token_consumed" in poc["reason_codes"]


def test_org_change_invalidates_poc_without_revalidation_adapter(
    client, post_event, worker_no_rdap, publisher, email_sender
):
    _verify_poc(client, post_event, worker_no_rdap, publisher, email_sender, "case-idchange")

    # a DIFFERENT ORG-ID arrives; rir_rdap is absent so nothing re-proves the new
    # org — the stale poc_verified must still drop its PASS and its points
    post_event("case-idchange", "org_id.submitted", {"rir": "arin", "org_handle": "ORG-OTHER-9"})
    worker_no_rdap.run_until_idle()

    poc = _live(client, "case-idchange")["poc_verified"]
    assert poc["status"] == "needs_review"
    assert poc["points"] == 0
    assert "poc_not_associated" in poc["reason_codes"]


def test_token_bound_to_prior_org_fails_after_org_change(
    client, post_event, worker_no_rdap, publisher, email_sender
):
    # mint a token bound to ORG-ACME-1 but DON'T verify it yet
    post_event("case-rebind", "kyb.run_requested", ACME_KYB)
    worker_no_rdap.run_until_idle()
    post_event(
        "case-rebind",
        "poc.submitted",
        {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-ACME-1"},
    )
    worker_no_rdap.run_until_idle()
    publisher.process_pending()
    body = email_sender.sent[-1]["body"]
    token = re.search(r"token: (\S+)", body).group(1)
    token_id = re.search(r"reference: (\S+)", body).group(1)

    # the POC's org association changes before the token is verified
    post_event(
        "case-rebind",
        "poc.submitted",
        {"rir": "arin", "poc_handle": "JD123-ARIN", "org_handle": "ORG-OTHER-9"},
        force_new_body=True,
    )
    worker_no_rdap.run_until_idle()

    # verifying the stale token cannot pass — it proves the OLD identity
    post_event(
        "case-rebind",
        "poc.token_verified",
        {"token_id": token_id, "verified_at": "2026-07-04T11:30:00Z", "token": token},
    )
    worker_no_rdap.run_until_idle()
    assert _live(client, "case-rebind")["poc_verified"]["status"] == "fail"
