"""DEV-ONLY worker: the real pipeline + outbox wired with fixture-backed
adapters, so the whole system runs locally with zero external credentials.

`python -m kyc_tool.workers.dev_worker` (scripts/dev.sh starts it for you).

The fixtures recognise one demo company — **Acme Networks Ltd** — end to end:
registry match (Companies House shape), Floqer discovery with a matching
LinkedIn profile, ORG-ACME-1 at ARIN, POC JD123-ARIN with a visible RIR email.
Everything else behaves like a real miss (fail / needs_review / no data),
which is exactly what you want for debugging validators.
"""

import json
import threading

import httpx

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.ocr import JsonScanOcrEngine
from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter
from kyc_tool.adapters.rir_rdap.adapter import FixtureRirStrategy, RirRdapAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.outbox.publisher import OutboxPublisher
from kyc_tool.policy.loader import load_policy
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import make_object_store

ACME_CH = {
    "items": [
        {
            "title": "ACME NETWORKS LTD",
            "company_number": "12345678",
            "company_status": "active",
            "address_snippet": "1 Main Street, London, EC1A 1AA",
        }
    ]
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
    }
}

RDAP_RECORDS = {
    "ORG-ACME-1": {
        "org_handle": "ORG-ACME-1",
        "entity_name": "ACME NETWORKS LTD",
        "address": "1 Main Street, London, EC1A 1AA",
    }
}

POC_DIRECTORY = {
    "arin:JD123-ARIN": {
        "found": True,
        "associated_org_handles": ["ORG-ACME-1"],
        "rir_listed_email": "noc@acme.example",
    }
}


def _registry_transport(request: httpx.Request) -> httpx.Response:
    if "/search/companies" in request.url.path and "acme" in str(request.url).lower():
        return httpx.Response(200, json=ACME_CH)
    if "/search/companies" in request.url.path:
        return httpx.Response(200, json={"items": []})
    return httpx.Response(200, json={"data": []})


def build_dev_adapters(store) -> dict:
    transport = httpx.MockTransport(_registry_transport)
    return {
        "email_verification": EmailVerificationAdapter(),
        "companies_house": CompaniesHouseAdapter(
            client=httpx.Client(transport=transport, base_url="https://ch.dev.local")
        ),
        "gleif": GleifAdapter(
            client=httpx.Client(transport=transport, base_url="https://gleif.dev.local")
        ),
        "floqer_company_enrichment": FloqerAdapter(FixtureFloqerClient(FLOQER_RECORDS)),
        "rir_rdap": RirRdapAdapter({"arin": FixtureRirStrategy(RDAP_RECORDS)}),
        "rir_poc": RirPocAdapter(FixturePocDirectory(POC_DIRECTORY)),
        "document_ocr": DocumentOcrAdapter(store, JsonScanOcrEngine()),
        "website_manual_review": WebsiteManualReviewAdapter(),
    }


def main() -> None:
    settings = get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    policy = load_policy(settings.policy_dir)
    store = make_object_store(
        settings.object_store, fs_root=settings.object_store_root, s3_bucket=settings.s3_bucket
    )
    # Seed a demo document so the composer's document.uploaded template works.
    store.put(
        "uploads/doc.json",
        json.dumps(
            {"fields": {"name": "ACME NETWORKS LTD", "number": "12345678", "jurisdiction": "GB"}}
        ).encode(),
    )
    pipeline = Pipeline(
        session_factory,
        policy,
        store,
        settings,
        adapters=build_dev_adapters(store),
        broker_matcher=BrokerGate(),
    )
    worker = Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        lease_seconds=settings.job_lease_seconds,
        backoff_base_seconds=settings.job_backoff_base_seconds,
        poll_seconds=settings.worker_poll_seconds,
        on_dead_letter=pipeline.on_dead_letter,
    )
    publisher = OutboxPublisher(session_factory, settings)
    threading.Thread(target=worker.run_forever, daemon=True).start()
    print("dev worker: pipeline (fixture adapters) + outbox running", flush=True)
    publisher.run_forever(poll_seconds=settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
