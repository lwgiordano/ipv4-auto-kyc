"""Pipeline worker process: `python -m kyc_tool.workers.pipeline_worker`."""

from kyc_tool.adapters.companies_house import CompaniesHouseAdapter
from kyc_tool.adapters.document_ocr import DocumentOcrAdapter
from kyc_tool.adapters.email_verification import EmailVerificationAdapter
from kyc_tool.adapters.gleif import GleifAdapter
from kyc_tool.adapters.ocr import JsonScanOcrEngine
from kyc_tool.adapters.rir_poc import FixturePocDirectory, RirPocAdapter
from kyc_tool.adapters.website_manual_review import WebsiteManualReviewAdapter
from kyc_tool.config import Settings, get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.orchestration.broker_gate import BrokerGate
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.policy.loader import load_policy
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import ObjectStore, make_object_store


def build_adapters(settings: Settings, store: ObjectStore) -> dict:
    """Production adapter registry (Phase 3 adds rir_rdap + floqer).

    TODO(integration): the POC directory is fixture-backed until the RDAP
    clients land; the OCR engine is the JSON-scan dev engine until the
    platform team picks a production OCR provider (AUDIT_FINDINGS §C4).
    """
    return {
        "email_verification": EmailVerificationAdapter(),
        "companies_house": CompaniesHouseAdapter(),
        "gleif": GleifAdapter(),
        "document_ocr": DocumentOcrAdapter(store, JsonScanOcrEngine()),
        "rir_poc": RirPocAdapter(FixturePocDirectory({})),
        "website_manual_review": WebsiteManualReviewAdapter(),
    }


def build_worker() -> Worker:
    settings = get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    policy = load_policy(settings.policy_dir)
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
        lease_seconds=settings.job_lease_seconds,
        backoff_base_seconds=settings.job_backoff_base_seconds,
        poll_seconds=settings.worker_poll_seconds,
        on_dead_letter=pipeline.on_dead_letter,
    )


if __name__ == "__main__":
    build_worker().run_forever()
