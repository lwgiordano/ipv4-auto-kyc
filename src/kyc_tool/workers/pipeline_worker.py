"""Pipeline worker process: `python -m kyc_tool.workers.pipeline_worker`."""

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.floqer import FixtureFloqerClient, FloqerAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.ocr import JsonScanOcrEngine
from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter
from kyc_tool.adapters.rir_rdap.adapter import RirRdapAdapter
from kyc_tool.adapters.rir_rdap.afrinic import AfrinicStrategy
from kyc_tool.adapters.rir_rdap.apnic import ApnicStrategy
from kyc_tool.adapters.rir_rdap.arin import ArinStrategy
from kyc_tool.adapters.rir_rdap.lacnic import LacnicStrategy
from kyc_tool.adapters.rir_rdap.ripe import RipeStrategy
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.config import (
    STUB_ADAPTERS_PROFILE,
    STUB_OCR_ENGINE,
    ProcessRole,
    Settings,
    get_settings,
    validate_process_role,
)
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.policy.loader import load_policy
from kyc_tool.policy_store.repo import attest, seed_and_verify
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import ObjectStore, make_object_store


def _make_ocr_engine(name: str):
    """Select the OCR engine. Only the dev stub exists today; a non-stub value
    fails loudly rather than silently falling back (real engine: item 12)."""
    if name == STUB_OCR_ENGINE:
        return JsonScanOcrEngine()
    raise NotImplementedError(
        f"OCR engine {name!r} is not implemented yet (remediation item 12)"
    )


def build_adapters(settings: Settings, store: ObjectStore) -> dict:
    """Adapter registry selected by settings.adapters_profile / ocr_engine.

    TODO(integration) (AUDIT_FINDINGS §C4): the only implemented profile is the
    fixture stub (Floqer fixture client, fixture POC directory, JSON-scan OCR).
    validate_for_production() refuses to boot a production worker on any stub;
    the real providers land with the executable-contract work (item 12).
    """
    if settings.adapters_profile == STUB_ADAPTERS_PROFILE:
        floqer_client = FixtureFloqerClient({})
        poc_directory = FixturePocDirectory({})
    else:
        raise NotImplementedError(
            f"adapters_profile {settings.adapters_profile!r} is not implemented yet "
            "(remediation item 12)"
        )
    return {
        "email_verification": EmailVerificationAdapter(),
        "companies_house": CompaniesHouseAdapter(),
        "gleif": GleifAdapter(),
        "floqer_company_enrichment": FloqerAdapter(floqer_client),
        "rir_rdap": RirRdapAdapter(
            {
                "arin": ArinStrategy(),
                "ripe": RipeStrategy(),
                "apnic": ApnicStrategy(),
                "lacnic": LacnicStrategy(),
                "afrinic": AfrinicStrategy(),
            }
        ),
        "document_ocr": DocumentOcrAdapter(store, _make_ocr_engine(settings.ocr_engine)),
        "rir_poc": RirPocAdapter(poc_directory),
        "website_manual_review": WebsiteManualReviewAdapter(),
    }


def build_worker() -> Worker:
    settings = get_settings()
    validate_process_role(settings, ProcessRole.PIPELINE_WORKER)  # fail-closed before any DB access
    session_factory = make_session_factory(make_engine(settings.database_url))
    policy = load_policy(settings.policy_dir)
    h = seed_and_verify(session_factory, settings.policy_dir)
    attest(flag=settings.enforce_bundle_pinning, bundle_hash=h)
    store = make_object_store(
        settings.object_store, fs_root=settings.object_store_root, s3_bucket=settings.s3_bucket
    )
    pipeline = Pipeline(
        session_factory,
        policy,
        store,
        settings,
        adapters=build_adapters(settings, store),
        broker_matcher=BrokerGate(),
    )
    return Worker(
        session_factory,
        {"run_transition": pipeline.handle_job},
        process_role=ProcessRole.PIPELINE_WORKER,
        lease_seconds=settings.job_lease_seconds,
        backoff_base_seconds=settings.job_backoff_base_seconds,
        poll_seconds=settings.worker_poll_seconds,
        on_dead_letter=pipeline.on_dead_letter,
    )


if __name__ == "__main__":
    build_worker().run_forever()
