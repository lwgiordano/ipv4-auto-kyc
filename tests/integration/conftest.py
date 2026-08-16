"""Integration-suite fixtures: the fully-wired Phase 3 pipeline (all adapters
against recorded fixtures) shared by the phase-3 and phase-4 suites."""

import httpx
import pytest

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.ocr import JsonScanOcrEngine
from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter
from kyc_tool.adapters.rir_rdap.adapter import FixtureRirStrategy, RirRdapAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.config import ProcessRole
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.queue.worker import Worker
from tests.conftest import bound_process
from tests.integration.shared import (
    FLOQER_RECORDS,
    POC_DIRECTORY,
    RDAP_RECORDS,
    registry_transport,
)


@pytest.fixture()
def phase3_pipeline(session_factory, policy, settings, evidence_store):
    transport = httpx.MockTransport(registry_transport)
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
        "rir_poc": RirPocAdapter(FixturePocDirectory(POC_DIRECTORY)),
        "document_ocr": DocumentOcrAdapter(evidence_store, JsonScanOcrEngine()),
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
def phase3_worker(session_factory, phase3_pipeline):
    return Worker(
        session_factory,
        {"run_transition": phase3_pipeline.handle_job},
        backoff_base_seconds=0,
        on_dead_letter=phase3_pipeline.on_dead_letter,
    **bound_process(ProcessRole.PIPELINE_WORKER))
