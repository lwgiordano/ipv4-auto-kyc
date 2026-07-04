"""Pipeline worker process: `python -m kyc_tool.workers.pipeline_worker`."""

from kyc_tool.config import get_settings
from kyc_tool.db.session import make_engine, make_session_factory
from kyc_tool.orchestration.pipeline import Pipeline
from kyc_tool.policy.loader import load_policy
from kyc_tool.queue.worker import Worker
from kyc_tool.storage.object_store import make_object_store


def build_worker() -> Worker:
    settings = get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    policy = load_policy(settings.policy_dir)
    store = make_object_store(
        settings.object_store, fs_root=settings.object_store_root, s3_bucket=settings.s3_bucket
    )
    pipeline = Pipeline(session_factory, policy, store, settings)
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
