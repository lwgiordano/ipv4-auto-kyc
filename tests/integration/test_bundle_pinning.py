# test_bundle_pinning.py — attestation + corrupt-startup at the REAL worker seam.
# structlog uses the default PrintLoggerFactory (api/app.py:46 sets NO logger_factory),
# so events bypass stdlib `logging` and `caplog` sees ZERO records —
# `structlog.testing.capture_logs()` is the sink (event dict keyed by `event` + kwargs).
# Both proofs drive `pipeline_worker.build_worker()` (the actual startup path), not a
# test-authored seed_and_verify/attest ordering.
import pytest
import structlog
from sqlalchemy import text

from kyc_tool.policy_store import repo as store
from tests.integration._bundle_helpers import bundle_x


def test_pipeline_worker_startup_attests_and_refuses_corrupt(
        session_factory, settings, engine, clean_db, post_event, monkeypatch):
    from kyc_tool.workers import pipeline_worker
    # SUCCESS (both flag states): build_worker() seeds → verifies → attests, returns a Worker
    for flag in (False, True):
        cfg = settings.model_copy(update={"enforce_bundle_pinning": flag})
        monkeypatch.setattr(pipeline_worker, "get_settings", lambda cfg=cfg: cfg)
        with structlog.testing.capture_logs() as logs:
            worker = pipeline_worker.build_worker()
        assert worker is not None
        rec = next(r for r in logs if r.get("event") == "bundle_pinning_ready")
        assert (rec["flag"] is flag and rec["engine_build_id"] == "eng-1"
                and rec["bundle_hash"] == bundle_x().bundle_hash)   # §8.18 deployment witness
    # CORRUPT process-bundle row (the single seeded row) → build_worker RAISES before
    # returning a Worker; nothing attested AND the queued job stays WHOLLY unclaimed (§8.2).
    with engine.begin() as c:
        c.execute(text("UPDATE policy_bundles SET files_json = jsonb_set("
                       "files_json,'{scoring_rubric.json}','\"%%%\"')"))
    post_event("c-corrupt", "email.verified",
               {"email": "o@acme.test", "domain": "acme.test", "verified_at": "2026-07-21T00:00:00Z"})
    cfg = settings.model_copy(update={"enforce_bundle_pinning": False})
    monkeypatch.setattr(pipeline_worker, "get_settings", lambda cfg=cfg: cfg)
    with structlog.testing.capture_logs() as logs, pytest.raises(store.BundleCorrupt):
        pipeline_worker.build_worker()                             # aborts before returning a Worker
    assert not [r for r in logs if r.get("event") == "bundle_pinning_ready"]
    with session_factory() as s:
        job = s.execute(text("SELECT status, attempts, locked_by FROM jobs "
                             "WHERE case_id='c-corrupt'")).one()
        assert job.status == "queued" and job.attempts == 0 and job.locked_by is None  # unclaimed
